#!/usr/bin/env python3

"""
master.py — Process monitor with main-thread Matplotlib rendering,
child-process tracking, and custom dynamic tiered range histogram binning.
"""

import os
import sys
import time
import math
import queue
import signal
import argparse
import logging
import threading

import psutil

from Documents.OS_proj.STATS.ContiniousRandomVariable import ContiniousRandomVariable
from Documents.OS_proj.STATS.DiscreteRandomVariable import DiscreteRandomVariable
from Documents.OS_proj.STATS.RandomVariable import RandomVariable

from Documents.OS_proj.STATS.Univariate.Distributions import (
    ContiniousDistribution,
    DiscreteDistribution
)

from Documents.OS_proj.STATS.Bivariate.Plots import (
    ScatterPlot,
    DiscreteFrequencyTable
)


logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(message)s"
)

log = logging.getLogger("master")

try:
    PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")
except (AttributeError, ValueError):
    PAGE_SIZE = 4096

# ---------------------------------------------------------
# Window constants
# ---------------------------------------------------------

SAMPLING_INTERVAL     = 1.0                                       # seconds between RSS reads
WINDOW_DURATION       = 30                                        # seconds per histogram window
SAMPLES_PER_WINDOW    = int(WINDOW_DURATION / SAMPLING_INTERVAL)  # = 30
LIVE_REFRESH_INTERVAL = 1.0                                       # seconds between live plot redraws


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

    for proc in psutil.process_iter(['pid', 'name']):
        try:
            proc_name = proc.info['name']
            if proc_name:
                clean_proc_name = proc_name.lower().replace(".exe", "")
                if clean_proc_name in names_clean:
                    matching_pids.add(proc.info['pid'])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return matching_pids


# ---------------------------------------------------------
# Kernel metrics
# ---------------------------------------------------------

def get_max_ram(pid: int) -> int:
    """
    Returns total system RAM limit as a hard fallback ceiling in KB.
    """
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

def partition_to_continious_rv_custom_dynamic(pid: int, current_rss_kb: float, max_system_ram_kb: float):
    """
    Applies custom tier logic against a dynamic process-scoped upper limit.
    """
    # Dynamic upper limit: 1.5x current footprint (at least 128MB), capped by max system memory
    dynamic_upper_kb = min(max_system_ram_kb, max(128 * 1024, current_rss_kb * 1.5))
    
    dynamic_upper_mb = dynamic_upper_kb / 1024
    dynamic_upper_gb = dynamic_upper_mb / 1024

    # Tier selection based on process memory footprint
    if dynamic_upper_mb <= 128:
        # Tier 1: Small process (< 128 MB) -> 64 bins (~2 MB width)
        num_bins = 64
    elif dynamic_upper_mb <= 512:
        # Tier 2: Medium process (< 512 MB) -> 100 bins (~5 MB width)
        num_bins = 100
    elif dynamic_upper_gb < 1.0:
        # Tier 3: Sub-gigabyte process (< 1 GB) -> 100 bins (~10 MB width)
        num_bins = 100
    else:
        # Tier 4: Large process (> 1 GB) -> Scale bins by max capacity
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
    step = (dynamic_upper_kb - lower_kb) / num_bins

    edges = [lower_kb + i * step for i in range(num_bins + 1)]
    
    return [
        ContiniousRandomVariable(i, edges[i], edges[i + 1])
        for i in range(num_bins)
    ], num_bins


class ProcessHistogram:

    def __init__(self, pid: int, max_system_ram_kb: float):
        self.pid                = pid
        self.max_system_ram_kb = max_system_ram_kb
        self.samples            = []
        self.sample_count       = 0

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

        # Generate bins dynamically using custom tiered rules
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

        return ContiniousDistribution(
            f"Dynamic Tiered RAM Histogram PID {self.pid}",
            bins,
            frequencies
        )

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


# ---------------------------------------------------------
# Non-blocking Console Input Thread
# ---------------------------------------------------------

class AsyncConsoleInput:

    def __init__(self):
        self.input_queue = queue.Queue()
        self.thread = threading.Thread(target=self._listen, daemon=True)
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
            fd_count = proc.num_handles() if sys.platform == "win32" else len(proc.open_files())
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

        log.info(f"Target PIDs: {sorted(self.target_pids) if self.target_pids else 'None'}")

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
            print(f"Watch mode enabled for: '{self.active_watch_mode}'. Type 'watch --stop' to end.")
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
                    p for p, snap in latest_snapshots.items()
                    if snap["name"].lower() == target_name
                ]
                if not target_pids:
                    print(f"No monitored process found matching name '{sub_tokens[0]}'")
                    return

            monitor.show_live_figures(target_pids)
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
                func(snapshot.get("pid") if flag == "--l" else snapshot)
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
        print(f"PID: {snapshot['pid']:<7} | Name: {snapshot['name']:<25} | RAM: {snapshot['rss_kb']} KB")

    def show_pids_ram_MB(self, snapshot: dict):
        print(f"PID: {snapshot['pid']:<7} | Name: {snapshot['name']:<25} | RAM: {snapshot['rss_kb'] / 1024:.2f} MB")

    def show_pids_ram_GB(self, snapshot: dict):
        print(f"PID: {snapshot['pid']:<7} | Name: {snapshot['name']:<25} | RAM: {(snapshot['rss_kb'] / 1024) / 1024:.4f} GB")

    def show_pids_io(self, snapshot: dict):
        print(f"{snapshot['pid']} read: {snapshot['io_read_kb']} KB, write: {snapshot['io_write_kb']} KB")

    def show_pids_io_MB(self, snapshot: dict):
        print(f"{snapshot['pid']} read: {snapshot['io_read_kb'] / 1024:.2f} MB, write: {snapshot['io_write_kb'] / 1024:.2f} MB")

    def show_pids_io_GB(self, snapshot: dict):
        print(f"{snapshot['pid']} read: {(snapshot['io_read_kb'] / 1024) / 1024:.4f} GB, write: {(snapshot['io_write_kb'] / 1024) / 1024:.4f} GB")

    def show_pids_read(self, snapshot: dict):
        print(f"{snapshot['pid']} read: {snapshot['io_read_kb']} KB")

    def show_pids_read_MB(self, snapshot: dict):
        print(f"{snapshot['pid']} read: {snapshot['io_read_kb'] / 1024:.2f} MB")

    def show_pids_read_GB(self, snapshot: dict):
        print(f"{snapshot['pid']} read: {(snapshot['io_read_kb'] / 1024) / 1024:.4f} GB")

    def show_pids_write(self, snapshot: dict):
        print(f"{snapshot['pid']} write: {snapshot['io_write_kb']} KB")

    def show_pids_write_MB(self, snapshot: dict):
        print(f"{snapshot['pid']} write: {snapshot['io_write_kb'] / 1024:.2f} MB")

    def show_pids_write_GB(self, snapshot: dict):
        print(f"{snapshot['pid']} write: {(snapshot['io_write_kb'] / 1024) / 1024:.4f} GB")

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

STATE_HISTOGRAM = 1
STATE_SCATTER   = 2


# ---------------------------------------------------------
# Main Monitor Class
# ---------------------------------------------------------

class Monitor:

    def __init__(self, filt):
        self.filter          = filt
        self.previous_cpu    = {}
        self.data            = {}
        self.running         = True
        self.commands        = Commands()
        self.input_handler   = AsyncConsoleInput()

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

    def _init_pid(self, pid: int, name: str) -> bool:
        ram_max_kb = get_max_ram(pid)

        self.ram_max[pid]    = ram_max_kb
        self.histograms[pid] = ProcessHistogram(pid, max_system_ram_kb=ram_max_kb)
        self.RAM_graph[pid]  = ProcessRAM_Graph(pid, name)
        self.fsm_state[pid]  = STATE_HISTOGRAM

        log.info("Monitoring PID %d (%s) with Dynamic Tiered Range Binning", pid, name)
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
        self.fsm_state[pid] = STATE_HISTOGRAM

    def show_live_figures(self, target_pids: list):
        import matplotlib.pyplot as plt

        plt.ion()
        self.live_target_pids = target_pids

        num_plots = min(len(target_pids), 3)
        if num_plots == 0:
            print("No processes to plot.")
            return

        if self.fig is None or not plt.fignum_exists(self.fig.number):
            self.fig, self.axes = plt.subplots(num_plots, 1, figsize=(10, 8))
            if num_plots == 1:
                self.axes = [self.axes]
        
        self.live_plot_active = True
        print(f"Live GUI Plotting enabled for PIDs: {target_pids}")

    def _update_live_plot_frame(self):
        if not self.live_plot_active:
            return

        import matplotlib.pyplot as plt

        if not plt.fignum_exists(self.fig.number):
            self.live_plot_active = False
            return

        pids = self.live_target_pids[:3]

        for i, pid in enumerate(pids):
            ax = self.axes[i]
            ax.clear()

            graph = self.RAM_graph.get(pid)
            if graph and graph.has_data():
                ax.plot(
                    graph.time_points,
                    graph.ri_points,
                    color="royalblue",
                    linewidth=1.8,
                    marker="o",
                    markersize=4,
                )
                ax.set_title(f"{graph.proc_name} (PID {pid}) — ri vs time_ns")
            else:
                name = get_process_name(pid) or "Process"
                ax.set_title(f"{name} (PID {pid}) — waiting for first 30s window…")

            ax.set_xlabel("time (ns)")
            ax.set_ylabel("ri = mode_kb / RAM_max")
            ax.set_ylim(0, 1)
            ax.grid(True)

        self.fig.canvas.draw_idle()
        self.fig.canvas.flush_events()
        plt.pause(0.001)

    def loop(self, interval: float):
        latest_snapshots = {}
        last_sample_time = 0
        last_plot_time   = 0

        while self.running:
            now = time.time()
            current_time = time.time_ns()

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
                            round((delta_cpu / 100) / (delta_time / 1e9) * 100, 2)
                            if delta_time > 0 else 0
                        )
                    else:
                        snapshot["cpu_percent"] = 0

                    self.previous_cpu[pid] = snapshot
                    self.data.setdefault(pid, []).append(snapshot)

                    if self.fsm_state[pid] == STATE_HISTOGRAM:
                        self.histograms[pid].record(snapshot["rss_kb"])
                        self.histograms[pid].increment_sample()
                        if self.histograms[pid].is_window_complete():
                            self.fsm_state[pid] = STATE_SCATTER

                    if self.fsm_state[pid] == STATE_SCATTER:
                        self._finalize_window(pid)

                if self.commands.active_watch_mode:
                    self.commands.render_watch_frame(latest_snapshots, self)

            cmd = self.input_handler.poll()
            if cmd:
                self.commands.execute(cmd, latest_snapshots, self)

            if self.live_plot_active and (now - last_plot_time >= LIVE_REFRESH_INTERVAL):
                self._update_live_plot_frame()
                last_plot_time = now

            time.sleep(0.05)

    def stop(self):
        self.running = False


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

    def handle_stop(s, f):
        log.info("Stopping monitor...")
        monitor.stop()

    signal.signal(signal.SIGINT, handle_stop)
    if hasattr(signal, 'SIGTERM'):
        signal.signal(signal.SIGTERM, handle_stop)

    try:
        monitor.loop(args.interval)
    except KeyboardInterrupt:
        log.info("Monitoring stopped by user")


if __name__ == "__main__":
    main()