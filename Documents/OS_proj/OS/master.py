#!/usr/bin/env python3

"""
master.py — Process monitor with main-thread Matplotlib rendering,
child-process tracking, dynamic tiered range histogram binning,
trading-style dynamic DB date-time axis scaling, and explicit 'close_event' cleanup.
"""

import os
import gc
import sys
import time
import math
import queue
import signal
import argparse
import logging
import threading
from datetime import datetime, timedelta
from mysql.connector import Error

import psutil
import matplotlib.dates as mdates

from Documents.OS_proj.STATS.ContiniousRandomVariable import ContiniousRandomVariable
from Documents.OS_proj.STATS.DiscreteRandomVariable import DiscreteRandomVariable
from Documents.OS_proj.STATS.RandomVariable import RandomVariable

from Documents.OS_proj.STATS.Univariate.Distributions import (
    ContiniousDistribution,
    DiscreteDistribution,
)

from Documents.OS_proj.STATS.Bivariate.Plots import (
    ScatterPlot,
    DiscreteFrequencyTable,
)
from Documents.OS_proj.DataBaseConnector.mySQL_Backend import Database


logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(message)s",
)

log = logging.getLogger("master")

try:
    PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")
except (AttributeError, ValueError):
    PAGE_SIZE = 4096

# ---------------------------------------------------------
# Window / refresh constants
# ---------------------------------------------------------

SAMPLING_INTERVAL     = 1.0                                        # seconds between RSS reads
WINDOW_DURATION       = 30                                         # seconds per histogram window
SAMPLES_PER_WINDOW    = int(WINDOW_DURATION / SAMPLING_INTERVAL)   # = 30
LIVE_REFRESH_INTERVAL = 1.0                                        # matplotlib repaint cadence
DATABASE_REFRESH      = 300.0                                      # seconds (5 min)


# ---------------------------------------------------------
# Process discovery
# ---------------------------------------------------------

def get_all_pids():
    try:
        return [p.pid for p in psutil.process_iter()]
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return []


def get_process_name(pid):
    try:
        return psutil.Process(pid).name()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None


def get_pids_by_names(names):
    if not names:
        return set()
    names_clean = {n.lower().replace(".exe", "") for n in names}
    matching_pids = set()

    for proc in psutil.process_iter(["pid", "name"]):
        try:
            proc_name = proc.info["name"]
            if proc_name:
                clean_proc_name = proc_name.lower().replace(".exe", "")
                if clean_proc_name in names_clean:
                    matching_pids.add(proc.info["pid"])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return matching_pids


# ---------------------------------------------------------
# Kernel metrics
# ---------------------------------------------------------

def get_max_ram(pid: int) -> int:
    """Returns total system RAM limit as a hard fallback ceiling in KB."""
    if sys.platform != "win32":
        try:
            with open(f"/proc/{pid}/limits", "r") as f:
                for line in f:
                    if line.startswith("Max resident set"):
                        parts = line.split()
                        soft  = parts[3]
                        if soft != "unlimited":
                            return int(soft) // 1024   # bytes -> KB
                        break
        except (FileNotFoundError, PermissionError, ValueError, IndexError):
            pass

    return psutil.virtual_memory().total // 1024


# ---------------------------------------------------------
# Custom Dynamic Tiered Partitioning Strategy
# ---------------------------------------------------------

def partition_to_continious_rv_custom_dynamic(
    pid: int, current_rss_kb: float, max_system_ram_kb: float
):
    """Applies custom tier logic against a dynamic process-scoped upper limit."""
    dynamic_upper_kb = min(
        max_system_ram_kb, max(128 * 1024, current_rss_kb * 1.5)
    )

    dynamic_upper_mb = dynamic_upper_kb / 1024
    dynamic_upper_gb = dynamic_upper_mb / 1024

    if dynamic_upper_mb <= 128:
        num_bins = 64
    elif dynamic_upper_mb <= 512:
        num_bins = 100
    elif dynamic_upper_gb < 1.0:
        num_bins = 100
    else:
        if dynamic_upper_gb <= 4:
            num_bins = 8
        elif dynamic_upper_gb <= 8:
            num_bins = 8
        elif dynamic_upper_gb <= 16:
            num_bins = 8
        elif dynamic_upper_gb <= 32:
            num_bins = 8
        else:
            num_bins = 16

    lower_kb = 0.0
    step     = (dynamic_upper_kb - lower_kb) / num_bins
    edges    = [lower_kb + i * step for i in range(num_bins + 1)]

    return (
        [ContiniousRandomVariable(i, edges[i], edges[i + 1]) for i in range(num_bins)],
        num_bins,
    )


class ProcessHistogram:

    def __init__(self, pid: int, max_system_ram_kb: float):
        self.pid               = pid
        self.max_system_ram_kb = max_system_ram_kb
        self.samples           = []
        self.sample_count      = 0
        self.last_distribution = None

    def record(self, rss_kb: float):
        self.samples.append(rss_kb)

    def increment_sample(self):
        self.sample_count += 1

    def is_window_complete(self) -> bool:
        return self.sample_count >= SAMPLES_PER_WINDOW

    def get_distribution(self):
        if not self.samples:
            return None

        peak_rss = max(self.samples)
        bins, num_bins = partition_to_continious_rv_custom_dynamic(
            self.pid, peak_rss, self.max_system_ram_kb
        )

        frequencies = [0] * num_bins
        for val in self.samples:
            placed = False
            for i, crv in enumerate(bins):
                if crv.getLower() <= val < crv.getUpper():
                    frequencies[i] += 1
                    placed = True
                    break
            if not placed:
                frequencies[-1] += 1

        dist = ContiniousDistribution(
            f"Dynamic Tiered RAM Histogram PID {self.pid}",
            bins,
            frequencies,
        )
        self.last_distribution = dist
        return dist

    def reset_counts(self):
        self.samples      = []
        self.sample_count = 0


# ---------------------------------------------------------
# RAM Graph Data Container
# ---------------------------------------------------------

class ProcessRAM_Graph:

    def __init__(self, pid: int, name: str):
        self.pid         = pid
        self.proc_name   = name
        self.time_points = []
        self.ri_points   = []

    def add_point(self, time_ns: int, ri: float):
        self.time_points.append(float(time_ns))
        self.ri_points.append(ri)

    def has_data(self) -> bool:
        return len(self.time_points) > 0

    def compute_velocity(self) -> float:
        if len(self.ri_points) < 2:
            return 0.0

        deltas = []
        for i in range(1, len(self.ri_points)):
            dt = self.time_points[i] - self.time_points[i - 1]
            if dt > 0:
                deltas.append((self.ri_points[i] - self.ri_points[i - 1]) / dt)

        return sum(deltas) / len(deltas) if deltas else 0.0

    def compute_acceleration(self, previous_velocity: float, previous_time_ns: float) -> float:
        current_velocity = self.compute_velocity()
        if previous_time_ns <= 0:
            return 0.0

        current_time_ns = self.time_points[-1] if self.time_points else 0.0
        dt = current_time_ns - previous_time_ns
        if dt <= 0:
            return 0.0

        return (current_velocity - previous_velocity) / dt


# ---------------------------------------------------------
# Database Integration Layer
# ---------------------------------------------------------

class DATABASE_INTEGRATION:

    def __init__(self, time_period_save_to_memory: float = DATABASE_REFRESH):
        self.time_period_save_to_memory = time_period_save_to_memory
        self.available = False
        self.db: Database | None = None

        try:
            self.db = Database()
            self.available = True
            log.info("DATABASE_INTEGRATION: connected to MySQL backend.")
        except Error as e:
            log.warning(
                "DATABASE_INTEGRATION: could not connect to MySQL — "
                "behaviour analysis disabled. Error: %s", e
            )

    def ensure_process_exists(self, pid: int, burst_time: float = 0.0):
        if not self.available:
            return
        try:
            existing = self.db.get_process(pid=pid)
            if existing is None:
                self.db.insert_process(pid=pid, burst_time=burst_time, status="running")
                log.debug("DB: inserted process PID %d", pid)
        except Error as e:
            log.error("DB ensure_process_exists(%d): %s", pid, e)

    def update_process(
        self,
        pid: int,
        burst_time: float,
        waiting_time: float,
        completion_time: float,
        turnaround_time: float,
        status: str,
    ):
        if not self.available:
            return
        try:
            existing = self.db.get_process(pid=pid)
            if existing is None:
                self.db.insert_process(
                    pid=pid,
                    burst_time=burst_time,
                    waiting_time=waiting_time,
                    completion_time=completion_time,
                    turnaround_time=turnaround_time,
                    status=status,
                )
            else:
                self.db.update_process_scheduling(
                    pid=pid,
                    burst_time=burst_time,
                    waiting_time=waiting_time,
                    completion_time=completion_time,
                    turnaround_time=turnaround_time,
                )
                self.db.update_process_status(pid=pid, status=status)
        except Error as e:
            log.error("DB update_process(%d): %s", pid, e)

    def add_process_statistics(
        self,
        pid: int,
        snapshot_time: datetime,
        mean: float,
        variance: float,
        standard_deviation: float,
        mode: float,
        velocity: float,
        acceleration: float,
    ):
        if not self.available:
            return
        try:
            self.db.insert_process_stats(
                pid=pid,
                time_snapshot=snapshot_time,
                mean_ram=mean,
                variance=variance,
                standard_deviation=standard_deviation,
                mode_ram=mode,
                velocity_ram=velocity,
                acceleration_ram=acceleration,
            )
            log.debug(
                "DB snapshot PID %d  mean=%.6f GB  vel=%.6e  acc=%.6e",
                pid, mean, velocity, acceleration,
            )
        except Error as e:
            log.error("DB add_process_statistics(%d): %s", pid, e)

    def get_statistical_behavior_of_process(self, pid: int):
        if not self.available:
            return []
        try:
            return self.db.get_process_stats(pid)
        except Error as e:
            log.error("DB get_process_stats(%d): %s", pid, e)
            return []

    def get_latest_stats_all(self):
        if not self.available:
            return []
        try:
            return self.db.get_latest_stats()
        except Error as e:
            log.error("DB get_latest_stats: %s", e)
            return []

    def _apply_dynamic_time_axis(self, fig, ax, times):
        """
        Configures dynamic date/time formatting with a focused trading-style view.
        Defaults to zooming into the most recent activity window while allowing 
        full interactive pan/zoom out to broader historical ranges.
        """
        clean_times = []
        for t in times:
            if isinstance(t, str):
                try:
                    clean_times.append(datetime.fromisoformat(t))
                except ValueError:
                    clean_times.append(t)
            else:
                clean_times.append(t)

        if not clean_times:
            return clean_times

        locator = mdates.AutoDateLocator(minticks=4, maxticks=10)
        formatter = mdates.ConciseDateFormatter(locator)

        ax.xaxis.set_major_locator(locator)
        ax.xaxis.set_major_formatter(formatter)

        latest_time = max(clean_times)
        if len(clean_times) == 1:
            ax.set_xlim(
                latest_time - timedelta(minutes=5),
                latest_time + timedelta(minutes=5)
            )
        else:
            earliest_time = min(clean_times)
            window_start = max(earliest_time, latest_time - timedelta(minutes=30))
            padding = timedelta(seconds=15)
            ax.set_xlim(window_start, latest_time + padding)

        fig.autofmt_xdate()
        return clean_times

    def live_plot_ram_velocity(self, pid: int):
        rows = self.get_statistical_behavior_of_process(pid)
        if not rows:
            print(f"[DB Plot] No stats data for PID {pid}")
            return

        import matplotlib.pyplot as plt

        raw_times = [r["timeSnapshot"] for r in rows]
        vels      = [float(r["VelocityRAM"] or 0) for r in rows]

        fig, ax = plt.subplots(figsize=(10, 4))
        clean_times = self._apply_dynamic_time_axis(fig, ax, raw_times)

        ax.plot(clean_times, vels, color="darkorange", linewidth=1.8, marker="o", markersize=4)
        ax.set_title(f"PID {pid} — RAM Velocity over time (DB history)")
        ax.set_xlabel("Snapshot time")
        ax.set_ylabel("VelocityRAM  (ri / ns)")
        ax.grid(True)
        fig.tight_layout()
        self._render_interactive(fig)

    def live_plot_ram_acceleration(self, pid: int):
        rows = self.get_statistical_behavior_of_process(pid)
        if not rows:
            print(f"[DB Plot] No stats data for PID {pid}")
            return

        import matplotlib.pyplot as plt

        raw_times = [r["timeSnapshot"] for r in rows]
        accs      = [float(r["AcclerationRAM"] or 0) for r in rows]

        fig, ax = plt.subplots(figsize=(10, 4))
        clean_times = self._apply_dynamic_time_axis(fig, ax, raw_times)

        ax.plot(clean_times, accs, color="crimson", linewidth=1.8, marker="s", markersize=4)
        ax.set_title(f"PID {pid} — RAM Acceleration over time (DB history)")
        ax.set_xlabel("Snapshot time")
        ax.set_ylabel("AccelerationRAM  (ri / ns²)")
        ax.grid(True)
        fig.tight_layout()
        self._render_interactive(fig)

    def live_plot_variance(self, pid: int):
        rows = self.get_statistical_behavior_of_process(pid)
        if not rows:
            print(f"[DB Plot] No stats data for PID {pid}")
            return

        import matplotlib.pyplot as plt

        raw_times = [r["timeSnapshot"] for r in rows]
        vars_     = [float(r["variance"] or 0) for r in rows]

        fig, ax = plt.subplots(figsize=(10, 4))
        clean_times = self._apply_dynamic_time_axis(fig, ax, raw_times)

        ax.plot(clean_times, vars_, color="purple", linewidth=1.8, marker="^", markersize=4)
        ax.set_title(f"PID {pid} — RAM Variance over time (DB history)")
        ax.set_xlabel("Snapshot time")
        ax.set_ylabel("Variance  (GB²)")
        ax.grid(True)
        fig.tight_layout()
        self._render_interactive(fig)

    def live_plot_mean(self, pid: int):
        rows = self.get_statistical_behavior_of_process(pid)
        if not rows:
            print(f"[DB Plot] No stats data for PID {pid}")
            return

        import matplotlib.pyplot as plt

        raw_times = [r["timeSnapshot"] for r in rows]
        means     = [float(r["meanRAM"] or 0) for r in rows]

        fig, ax = plt.subplots(figsize=(10, 4))
        clean_times = self._apply_dynamic_time_axis(fig, ax, raw_times)

        ax.plot(clean_times, means, color="steelblue", linewidth=1.8, marker="D", markersize=4)
        ax.set_title(f"PID {pid} — Mean RAM over time (DB history)")
        ax.set_xlabel("Snapshot time")
        ax.set_ylabel("Mean RAM  (GB)")
        ax.grid(True)
        fig.tight_layout()
        self._render_interactive(fig)

    def _render_interactive(self, fig):
        import matplotlib.pyplot as plt

        try:
            plt.ion()
            fig.show()
            fig.canvas.draw_idle()
            fig.canvas.flush_events()
            plt.pause(0.001)
        except Exception as e:
            log.error("DB Plot rendering error: %s", e)

    def close(self):
        if self.available and self.db:
            try:
                self.db.close()
            except Exception:
                pass


# ---------------------------------------------------------
# Non-blocking Console Input Thread
# ---------------------------------------------------------

class AsyncConsoleInput:

    def __init__(self):
        self.input_queue = queue.Queue()
        self.thread      = threading.Thread(target=self._listen, daemon=True)
        self.thread.start()

    def _listen(self):
        while True:
            try:
                line = sys.stdin.readline()
                if line:
                    self.input_queue.put(line.strip())
            except Exception:
                break

    def poll(self):
        try:
            return self.input_queue.get_nowait()
        except queue.Empty:
            return None


# ---------------------------------------------------------
# Process Snapshot Reader
# ---------------------------------------------------------

def read_process(pid: int, current_time: int):
    try:
        proc     = psutil.Process(pid)
        mem_info = proc.memory_info()
        rss_kb   = mem_info.rss // 1024

        cpu_times = proc.cpu_times()
        cpu_ticks = int((cpu_times.user + cpu_times.system) * 100)

        threads = proc.num_threads()

        try:
            fd_count = (
                proc.num_handles()
                if sys.platform == "win32"
                else len(proc.open_files())
            )
        except (psutil.AccessDenied, AttributeError):
            fd_count = 0

        try:
            io       = proc.io_counters()
            read_kb  = io.read_bytes  // 1024
            write_kb = io.write_bytes // 1024
        except (psutil.AccessDenied, AttributeError):
            read_kb = write_kb = 0

        return {
            "pid":          pid,
            "name":         proc.name(),
            "rss_kb":       rss_kb,
            "threads":      threads,
            "fd_count":     fd_count,
            "io_read_kb":   read_kb,
            "io_write_kb":  write_kb,
            "cpu_ticks":    cpu_ticks,
            "timestamp_ns": current_time,
        }

    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None


# ---------------------------------------------------------
# Filter
# ---------------------------------------------------------

class ProcessFilter:

    def __init__(self, pids, names):
        self.pids  = set(pids)
        self.names = set(names)

        if not self.pids and not self.names:
            self.target_pids = set(get_all_pids())
        else:
            self.target_pids = set(self.pids)
            if self.names:
                self.target_pids.update(get_pids_by_names(self.names))

        log.info(
            f"Target PIDs: {sorted(self.target_pids) if self.target_pids else 'None'}"
        )

    def get_target_pids(self):
        return self.target_pids


# ---------------------------------------------------------
# Command Interpreter
# ---------------------------------------------------------

class Commands:

    def __init__(self):
        self.active_watch_mode = None

        self.commFlags = {
            "--l":       self.showListPids,
            "--lr":      self.show_pids_ram,
            "--lrmb":    self.show_pids_ram_MB,
            "--lrgb":    self.show_pids_ram_GB,
            "--io":      self.show_pids_io,
            "--iomb":    self.show_pids_io_MB,
            "--iogb":    self.show_pids_io_GB,
            "--read":    self.show_pids_read,
            "--readmb":  self.show_pids_read_MB,
            "--readgb":  self.show_pids_read_GB,
            "--write":   self.show_pids_write,
            "--writemb": self.show_pids_write_MB,
            "--writegb": self.show_pids_write_GB,
            "--lt":      self.show_pids_threads,
            "--lfd":     self.show_pids_fd,
        }

        self.commRootList = {
            "pids":            self.show_pids,
            "curr --tick":     self.show_curr_tick,
            "curr --tns":      self.show_curr_tns,
            "monitor --stop":  self.stop_monitor,
            "watch --stop":    self.stop_watch_mode,
        }

    def execute(self, cmd_str: str, latest_snapshots: dict, monitor=None):
        cmd = cmd_str.strip()
        if not cmd:
            return

        tokens = cmd.split()

        if cmd == "monitor --stop":
            self.stop_monitor(monitor)
            return

        if cmd == "watch --stop":
            self.stop_watch_mode(None)
            return

        if "--watch" in cmd:
            self.active_watch_mode = cmd.replace("--watch", "").strip()
            print(
                f"Watch mode enabled for: '{self.active_watch_mode}'. "
                "Type 'watch --stop' to end."
            )
            return

        if cmd.startswith("monitor --live figshow"):
            sub_tokens  = tokens[3:]
            target_pids = []

            if not sub_tokens:
                target_pids = list(latest_snapshots.keys())
            elif sub_tokens[0].isdigit():
                pid = int(sub_tokens[0])
                if pid in latest_snapshots:
                    target_pids = [pid]
                else:
                    print(f"PID {pid} is not currently monitored.")
                    return
            else:
                target_name = sub_tokens[0].lower()
                target_pids = [
                    p
                    for p, snap in latest_snapshots.items()
                    if snap["name"].lower() == target_name
                ]
                if not target_pids:
                    print(
                        f"No monitored process found matching name '{sub_tokens[0]}'"
                    )
                    return

            monitor.show_live_figures(target_pids)
            return

        if cmd.startswith("db plot") and monitor is not None:
            if len(tokens) < 4 or not tokens[3].isdigit():
                print("Usage: db plot --vel|--acc|--var|--mean <pid>")
                return

            flag = tokens[2]
            pid_arg = int(tokens[3])

            if flag not in {"--vel", "--acc", "--var", "--mean"}:
                print(f"Unknown db plot flag: {flag}")
                return

            monitor.queue_db_plot_request(flag, pid_arg)
            print(f"Queued DB plot request: {flag} PID {pid_arg}")
            return

        if tokens[0] == "pids" and len(tokens) > 1 and tokens[1].isdigit():
            target_pid = int(tokens[1])
            if target_pid not in latest_snapshots:
                print(f"PID {target_pid} not found in monitored snapshots.")
                return

            snapshot = latest_snapshots[target_pid]
            if len(tokens) == 2:
                self.show_pids(snapshot)
            else:
                flag = tokens[2]
                if flag in self.commFlags:
                    func = self.commFlags[flag]
                    func(snapshot.get("pid") if flag == "--l" else snapshot)
                else:
                    print(f"Unknown flag: {flag}")
            return

        if cmd in self.commRootList:
            func = self.commRootList[cmd]
            for snapshot in latest_snapshots.values():
                func(snapshot)
            return

        flag = cmd.replace("pids ", "").strip()
        if flag in self.commFlags:
            func = self.commFlags[flag]
            for snapshot in latest_snapshots.values():
                func(
                    snapshot.get("pid") if flag == "--l" else snapshot
                )
            return

        print(f"Unknown command: {cmd_str}")

    def render_watch_frame(self, latest_snapshots: dict, monitor):
        if not self.active_watch_mode:
            return
        os.system("cls" if sys.platform == "win32" else "clear")
        print(f"=== LIVE CLI MONITORING MODE [{self.active_watch_mode}] ===")
        print("Type 'watch --stop' and hit Enter to exit watch mode.\n")
        self.execute(self.active_watch_mode, latest_snapshots, monitor)

    def stop_watch_mode(self, snapshot=None):
        self.active_watch_mode = None
        print("\nWatch mode stopped.")

    def show_pids(self, snapshot: dict):
        retText = ""
        for key, val in snapshot.items():
            retText += f"{key}: {val}\n"
        print(retText)

    def showListPids(self, pid: int):
        print(pid)

    def show_pids_ram(self, snapshot: dict):
        print(
            f"PID: {snapshot['pid']:<7} | Name: {snapshot['name']:<25} | "
            f"RAM: {snapshot['rss_kb']} KB"
        )

    def show_pids_ram_MB(self, snapshot: dict):
        print(
            f"PID: {snapshot['pid']:<7} | Name: {snapshot['name']:<25} | "
            f"RAM: {snapshot['rss_kb'] / 1024:.2f} MB"
        )

    def show_pids_ram_GB(self, snapshot: dict):
        print(
            f"PID: {snapshot['pid']:<7} | Name: {snapshot['name']:<25} | "
            f"RAM: {(snapshot['rss_kb'] / 1024) / 1024:.4f} GB"
        )

    def show_pids_io(self, snapshot: dict):
        print(
            f"{snapshot['pid']} read: {snapshot['io_read_kb']} KB, "
            f"write: {snapshot['io_write_kb']} KB"
        )

    def show_pids_io_MB(self, snapshot: dict):
        print(
            f"{snapshot['pid']} read: {snapshot['io_read_kb'] / 1024:.2f} MB, "
            f"write: {snapshot['io_write_kb'] / 1024:.2f} MB"
        )

    def show_pids_io_GB(self, snapshot: dict):
        print(
            f"{snapshot['pid']} read: {(snapshot['io_read_kb'] / 1024) / 1024:.4f} GB, "
            f"write: {(snapshot['io_write_kb'] / 1024) / 1024:.4f} GB"
        )

    def show_pids_read(self, snapshot: dict):
        print(f"{snapshot['pid']} read: {snapshot['io_read_kb']} KB")

    def show_pids_read_MB(self, snapshot: dict):
        print(f"{snapshot['pid']} read: {snapshot['io_read_kb'] / 1024:.2f} MB")

    def show_pids_read_GB(self, snapshot: dict):
        print(
            f"{snapshot['pid']} read: {(snapshot['io_read_kb'] / 1024) / 1024:.4f} GB"
        )

    def show_pids_write(self, snapshot: dict):
        print(f"{snapshot['pid']} write: {snapshot['io_write_kb']} KB")

    def show_pids_write_MB(self, snapshot: dict):
        print(f"{snapshot['pid']} write: {snapshot['io_write_kb'] / 1024:.2f} MB")

    def show_pids_write_GB(self, snapshot: dict):
        print(
            f"{snapshot['pid']} write: {(snapshot['io_write_kb'] / 1024) / 1024:.4f} GB"
        )

    def show_pids_threads(self, snapshot: dict):
        print(f"{snapshot['pid']} threads: {snapshot['threads']}")

    def show_pids_fd(self, snapshot: dict):
        print(f"{snapshot['pid']} handles/fd: {snapshot['fd_count']}")

    def show_curr_tick(self, snapshot: dict):
        print(f"{snapshot['pid']} cpu ticks: {snapshot['cpu_ticks']}")

    def show_curr_tns(self, snapshot: dict):
        print(f"{snapshot['pid']} timestamp ns: {snapshot['timestamp_ns']}")

    def stop_monitor(self, monitor):
        if monitor:
            monitor.stop()
            log.info("Monitor stopped via command")


# ---------------------------------------------------------
# FSM State Constants
# ---------------------------------------------------------

STATE_HISTOGRAM       = 1
STATE_LIVE_PLOT       = 2
STATE_UPDATE_DATABASE = 3


# ---------------------------------------------------------
# Main Monitor Class
# ---------------------------------------------------------

class Monitor:

    def __init__(self, filt):
        self.filter        = filt
        self.previous_cpu  = {}
        self.data          = {}
        self.running       = True
        self.commands      = Commands()
        self.input_handler = AsyncConsoleInput()

        self.target_pids = filt.get_target_pids()
        if not self.target_pids:
            log.warning("No target processes found")

        self.fsm_state  = {}
        self.ram_max    = {}
        self.histograms = {}
        self.RAM_graph  = {}

        self.live_plot_active = False
        self.live_target_pids = []
        self.fig              = None
        self.axes             = None
        self.active_db_fig    = None

        self.db_plot_requests = queue.Queue()

        self.db_integration = DATABASE_INTEGRATION(
            time_period_save_to_memory=DATABASE_REFRESH
        )

        self.last_db_flush_time    = {}
        self.last_velocity         = {}
        self.last_velocity_time_ns = {}

    def _on_close(self, event):
        """
        Explicitly triggered when user clicks 'X' on any plot figure.
        Resets figure pointers, closes all backend windows, and forces GC.
        """
        self.live_plot_active = False
        self.fig = None
        self.axes = None
        self.active_db_fig = None

        import matplotlib.pyplot as plt
        plt.close("all")
        gc.collect()
        log.info("Plot window closed via 'X'; resources explicitly released.")

    def _init_pid(self, pid: int, name: str) -> bool:
        ram_max_kb = get_max_ram(pid)

        self.ram_max[pid]    = ram_max_kb
        self.histograms[pid] = ProcessHistogram(pid, max_system_ram_kb=ram_max_kb)
        self.RAM_graph[pid]  = ProcessRAM_Graph(pid, name)
        self.fsm_state[pid]  = STATE_HISTOGRAM

        self.last_db_flush_time[pid]    = time.time()
        self.last_velocity[pid]         = 0.0
        self.last_velocity_time_ns[pid] = 0.0

        self.db_integration.ensure_process_exists(pid, burst_time=0.0)

        log.info(
            "Monitoring PID %d (%s) with Dynamic Tiered Range Binning", pid, name
        )
        return True

    def _finalize_window(self, pid: int):
        hist = self.histograms[pid]
        dist = hist.get_distribution()

        if dist is None:
            hist.reset_counts()
            self.fsm_state[pid] = STATE_HISTOGRAM
            return

        mode_kb    = dist.getMode()
        ram_max_kb = self.ram_max[pid]
        ri         = mode_kb / ram_max_kb
        ts         = time.time_ns()

        self.RAM_graph[pid].add_point(ts, ri)
        hist.reset_counts()

        elapsed_since_flush = time.time() - self.last_db_flush_time.get(pid, 0.0)
        if elapsed_since_flush >= DATABASE_REFRESH:
            self.fsm_state[pid] = STATE_UPDATE_DATABASE
        else:
            self.fsm_state[pid] = STATE_HISTOGRAM

    def _save_to_database(self, pid: int):
        hist  = self.histograms.get(pid)
        graph = self.RAM_graph.get(pid)

        if hist is None or hist.last_distribution is None or graph is None or not graph.has_data():
            self.last_db_flush_time[pid] = time.time()
            self.fsm_state[pid]          = STATE_HISTOGRAM
            return

        dist = hist.last_distribution

        # Raw statistics from ContiniousDistribution are in KB
        mean_kb     = dist.getMean()
        variance_kb = dist.getVariance()
        std_dev_kb  = dist.getStandardDeviation()
        mode_kb     = dist.getMode()

        # Convert KB -> GB before saving into MySQL database
        KB_TO_GB    = 1024.0 * 1024.0
        mean_gb     = mean_kb / KB_TO_GB
        variance_gb = variance_kb / (KB_TO_GB ** 2)
        std_dev_gb  = std_dev_kb / KB_TO_GB
        mode_gb     = mode_kb / KB_TO_GB

        velocity     = graph.compute_velocity()
        acceleration = graph.compute_acceleration(
            previous_velocity = self.last_velocity.get(pid, 0.0),
            previous_time_ns  = self.last_velocity_time_ns.get(pid, 0.0),
        )

        snapshot_time = datetime.now()

        self.db_integration.add_process_statistics(
            pid                = pid,
            snapshot_time      = snapshot_time,
            mean               = mean_gb,
            variance           = variance_gb,
            standard_deviation = std_dev_gb,
            mode               = mode_gb,
            velocity           = velocity,
            acceleration       = acceleration,
        )

        current_time_ns = graph.time_points[-1] if graph.time_points else 0.0
        self.last_velocity[pid]         = velocity
        self.last_velocity_time_ns[pid] = current_time_ns
        self.last_db_flush_time[pid]    = time.time()

        log.info(
            "DB flush PID %d  mean=%.6f GB  var=%.6e GB²  vel=%.4e  acc=%.4e",
            pid, mean_gb, variance_gb, velocity, acceleration,
        )

        self.fsm_state[pid] = STATE_HISTOGRAM

    def show_live_figures(self, target_pids: list):
        import matplotlib.pyplot as plt

        self._on_close(None)

        plt.ion()
        self.live_target_pids = target_pids

        num_plots = min(len(target_pids), 3)
        if num_plots == 0:
            print("No processes to plot.")
            return

        self.fig, self.axes = plt.subplots(num_plots, 1, figsize=(10, 8))
        if num_plots == 1:
            self.axes = [self.axes]

        self.fig.canvas.mpl_connect("close_event", self._on_close)
        self.live_plot_active = True
        print(f"Live GUI Plotting enabled for PIDs: {target_pids}")

    def _update_live_plot_frame(self, force_redraw: bool = False):
        if not self.live_plot_active or self.fig is None:
            return

        import matplotlib.pyplot as plt

        if not plt.fignum_exists(self.fig.number):
            self._on_close(None)
            return

        try:
            if force_redraw:
                pids = self.live_target_pids[:3]

                for i, pid in enumerate(pids):
                    ax = self.axes[i]
                    ax.clear()

                    graph = self.RAM_graph.get(pid)
                    if graph and graph.has_data():
                        times = list(graph.time_points)
                        ris   = list(graph.ri_points)
                        ax.plot(
                            times,
                            ris,
                            color="royalblue",
                            linewidth=1.8,
                            marker="o",
                            markersize=4,
                        )
                        ax.set_title(f"{graph.proc_name} (PID {pid}) — ri vs time_ns")
                    else:
                        name = get_process_name(pid) or "Process"
                        ax.set_title(f"{name} (PID {pid}) — waiting for first 30 s window…")

                    ax.set_xlabel("time (ns)")
                    ax.set_ylabel("ri = mode_kb / RAM_max")
                    ax.set_ylim(0, 1)
                    ax.grid(True)

                self.fig.canvas.draw_idle()

            # Pump OS window events to avoid "Not Responding" freeze
            self.fig.canvas.start_event_loop(0.001)

        except Exception as e:
            log.error("Error updating live plot frame: %s", e)
            self._on_close(None)

    def queue_db_plot_request(self, flag: str, pid: int):
        self.db_plot_requests.put((flag, pid))

    def _render_pending_db_plots(self):
        import matplotlib.pyplot as plt

        while True:
            try:
                flag, pid = self.db_plot_requests.get_nowait()
            except queue.Empty:
                break

            self._on_close(None)

            if flag == "--vel":
                self.db_integration.live_plot_ram_velocity(pid)
            elif flag == "--acc":
                self.db_integration.live_plot_ram_acceleration(pid)
            elif flag == "--var":
                self.db_integration.live_plot_variance(pid)
            elif flag == "--mean":
                self.db_integration.live_plot_mean(pid)

            curr_fig = plt.gcf()
            if curr_fig:
                self.active_db_fig = curr_fig
                curr_fig.canvas.mpl_connect("close_event", self._on_close)

    def loop(self, interval: float):
        latest_snapshots = {}
        last_sample_time = 0.0
        last_plot_time   = 0.0

        while self.running:
            now          = time.time()
            current_time = time.time_ns()

            self._render_pending_db_plots()

            if now - last_sample_time >= interval:
                last_sample_time = now

                for pid in list(self.target_pids):
                    if not psutil.pid_exists(pid):
                        if pid in self.previous_cpu:
                            log.info(f"Process {pid} has terminated")
                            del self.previous_cpu[pid]
                        continue

                    snapshot = read_process(pid, current_time)
                    if not snapshot:
                        continue

                    latest_snapshots[pid] = snapshot

                    if pid not in self.fsm_state:
                        if not self._init_pid(pid, snapshot["name"]):
                            continue

                    old = self.previous_cpu.get(pid)
                    if old:
                        delta_cpu  = snapshot["cpu_ticks"] - old["cpu_ticks"]
                        delta_time = snapshot["timestamp_ns"] - old["timestamp_ns"]
                        snapshot["cpu_percent"] = (
                            round(
                                (delta_cpu / 100) / (delta_time / 1e9) * 100, 2
                            )
                            if delta_time > 0
                            else 0
                        )
                    else:
                        snapshot["cpu_percent"] = 0

                    self.previous_cpu[pid] = snapshot
                    self.data.setdefault(pid, []).append(snapshot)

                    if self.fsm_state[pid] == STATE_HISTOGRAM:
                        self.histograms[pid].record(snapshot["rss_kb"])
                        self.histograms[pid].increment_sample()
                        if self.histograms[pid].is_window_complete():
                            self.fsm_state[pid] = STATE_LIVE_PLOT

                    if self.fsm_state[pid] == STATE_LIVE_PLOT:
                        self._finalize_window(pid)

                    if self.fsm_state[pid] == STATE_UPDATE_DATABASE:
                        self._save_to_database(pid)

                if self.commands.active_watch_mode:
                    self.commands.render_watch_frame(latest_snapshots, self)

            cmd = self.input_handler.poll()
            if cmd:
                self.commands.execute(cmd, latest_snapshots, self)

            # Continuous OS GUI event loop pumping for both Live and DB plots
            import matplotlib.pyplot as plt

            if self.live_plot_active and self.fig is not None:
                should_redraw = (now - last_plot_time >= LIVE_REFRESH_INTERVAL)
                self._update_live_plot_frame(force_redraw=should_redraw)
                if should_redraw:
                    last_plot_time = now

            elif self.active_db_fig is not None:
                if not plt.fignum_exists(self.active_db_fig.number):
                    self._on_close(None)
                else:
                    try:
                        self.active_db_fig.canvas.start_event_loop(0.001)
                    except Exception as e:
                        log.error("DB Plot event loop error: %s", e)
                        self._on_close(None)

            time.sleep(0.05)

    def stop(self):
        self.running = False
        self._on_close(None)
        self.db_integration.close()
        log.info("Monitor stopped; DB connection closed.")


# ---------------------------------------------------------
# Entry Point
# ---------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pid",      nargs="*", type=int, default=[])
    parser.add_argument("--name",     nargs="*", default=[])
    parser.add_argument("--interval", type=float, default=SAMPLING_INTERVAL)
    args = parser.parse_args()

    filt    = ProcessFilter(args.pid, args.name)
    monitor = Monitor(filt)

    if not monitor.target_pids:
        log.error("No target processes found. Exiting.")
        return

    log.info(f"Monitoring started — tracking {len(monitor.target_pids)} processes")
    log.info(
        f"Window: {SAMPLES_PER_WINDOW} samples x {args.interval}s "
        f"= {WINDOW_DURATION}s per scatter point"
    )
    log.info(
        f"DB flush period: {DATABASE_REFRESH}s  "
        f"({'connected' if monitor.db_integration.available else 'DISCONNECTED — stats will not be saved'})"
    )

    def handle_stop(s, f):
        log.info("Stopping monitor...")
        monitor.stop()

    signal.signal(signal.SIGINT, handle_stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, handle_stop)

    try:
        monitor.loop(args.interval)
    except KeyboardInterrupt:
        log.info("Monitoring stopped by user")


if __name__ == "__main__":
    main()