#!/usr/bin/env python3

"""
master.py — Process monitor for Windows/Linux.

Uses psutil for cross-platform process monitoring.
Reads process information directly from the OS.

Usage:

    python3 master.py

        Monitor all accessible processes


    python3 master.py --pid 1234 5678

        Monitor specific PIDs


    python3 master.py --name nginx python

        Monitor processes by name
"""

import os
import sys
import time
import math
import signal
import select
import argparse
import logging
import psutil

from Documents.OS_proj.STATS.ContiniousRandomVariable import ContiniousRandomVariable
from Documents.STATS.Univariate.Distributions import ContiniousDistribution

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

SAMPLING_INTERVAL  = 1.0                                        # seconds between RSS reads
WINDOW_DURATION    = 30                                         # seconds per histogram window
SAMPLES_PER_WINDOW = int(WINDOW_DURATION / SAMPLING_INTERVAL)  # = 30


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
    names_lower   = {n.lower() for n in names}
    matching_pids = set()
    for proc in psutil.process_iter(['pid', 'name']):
        try:
            proc_name = proc.info['name']
            if proc_name and proc_name.lower() in names_lower:
                matching_pids.add(proc.info['pid'])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return matching_pids


# ---------------------------------------------------------
# Kernel metrics
# ---------------------------------------------------------

def get_max_ram(pid: int) -> int:
    """
    Return the hard RAM ceiling (in KB) for the given PID.

    On Linux: reads /proc/<pid>/limits for 'Max resident set' (RLIMIT_RSS).
    If the limit is 'unlimited', falls back to total system RAM.

    On Windows: falls back to total system RAM.
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


def partition_to_continious_rv(RAM_interval: list, num_bins: int = None):
    """
    Partition [lower_kb, upper_kb] into non-overlapping ContiniousRandomVariable bins.
    """
    lower_kb = RAM_interval[0]
    upper_kb = RAM_interval[1]

    if upper_kb <= 0:
        log.error("partition_to_continious_rv: upper bound must be > 0")
        return None
    if lower_kb < 0:
        lower_kb = 0

    range_kb = upper_kb - lower_kb
    range_mb = range_kb / 1024
    range_gb = range_mb / 1024

    if num_bins is None:
        if range_gb < 1:
            if range_mb < 128:
                num_bins = 10
            elif range_mb < 256:
                num_bins = 9
            elif range_mb < 512:
                num_bins = 9
            else:
                num_bins = 8
        elif range_gb < 4:
            num_bins = 8
        elif range_gb < 16:
            num_bins = 8
        elif range_gb < 64:
            num_bins = 7
        elif range_gb < 128:
            num_bins = 6
        else:
            log.error("partition_to_continious_rv: RAM range exceeds 128 GB")
            return None

    log_min = math.log(1)
    log_max = math.log(range_kb + 1)

    edges     = [
        lower_kb + math.exp(log_min + (log_max - log_min) * i / num_bins) - 1
        for i in range(num_bins + 1)
    ]
    edges[0]  = lower_kb
    edges[-1] = upper_kb

    return [
        ContiniousRandomVariable(i, edges[i], edges[i + 1])
        for i in range(num_bins)
    ]


# ---------------------------------------------------------
# FSM STATE 1 — Histogram
# ---------------------------------------------------------

class ProcessHistogram:

    def __init__(self, pid: int, bins: list):
        self.pid          = pid
        self.bins         = bins
        self.frequency    = [0] * len(bins)
        self.sample_count = 0

    def record(self, rss_kb: float):
        for i, crv in enumerate(self.bins):
            if crv.getLower() <= rss_kb < crv.getUpper():
                self.frequency[i] += 1
                return
        self.frequency[-1] += 1

    def reset_counts(self):
        self.frequency    = [0] * len(self.bins)
        self.sample_count = 0

    def increment_sample(self):
        self.sample_count += 1

    def is_window_complete(self) -> bool:
        return self.sample_count >= SAMPLES_PER_WINDOW


# ---------------------------------------------------------
# FSM STATE 2 — RAM Graph Data Container
# ---------------------------------------------------------

class ProcessRAM_Graph:

    def __init__(self, pid: int, name: str):
        self.pid         = pid
        self.proc_name   = name
        self.time_points = []
        self.ri_points    = []

    def add_point(self, time_ns: int, ri: float):
        self.time_points.append(float(time_ns))
        self.ri_points.append(ri)

    def has_data(self) -> bool:
        return len(self.time_points) > 0

    def plot(self):
        import matplotlib.pyplot as plt

        plt.figure(figsize=(10, 5))
        plt.plot(self.time_points, self.ri_points, color="blue", linewidth=2, marker='o')
        plt.title(f"{self.proc_name} (PID {self.pid}) — RAM usage ratio over time")
        plt.xlabel("time (ns)")
        plt.ylabel("ri = mode_kb / RAM_max")
        plt.ylim(0, 1)
        plt.grid(True)
        plt.tight_layout()
        plt.show()


# ---------------------------------------------------------
# Read one process snapshot
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
# Filtering
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
# Command execution handler
# ---------------------------------------------------------

class Commands:

    def __init__(self):
        self.commFlags = {
            "--l": self.showListPids,
            "--lr": self.show_pids_ram,
            "--lrmb": self.show_pids_ram_MB,
            "--lrgb": self.show_pids_ram_GB,
            "--io": self.show_pids_io,
            "--iomb": self.show_pids_io_MB,
            "--iogb": self.show_pids_io_GB,
            "--read": self.show_pids_read,
            "--readmb": self.show_pids_read_MB,
            "--readgb": self.show_pids_read_GB,
            "--write": self.show_pids_write,
            "--writemb": self.show_pids_write_MB,
            "--writegb": self.show_pids_write_GB,
            "--lt": self.show_pids_threads,
            "--lfd": self.show_pids_fd,
        }

        self.commRootList = {
            "pids": self.show_pids,
            "curr --tick": self.show_curr_tick,
            "curr --tns": self.show_curr_tns,
            "monitor --stop": self.stop_monitor,
        }

    def execute(self, cmd_str: str, latest_snapshots: dict, monitor=None):
        cmd = cmd_str.strip()
        if not cmd:
            return

        tokens = cmd.split()

        if cmd == "monitor --stop":
            self.stop_monitor(monitor)
            return

        # ---------------------------------------------------------
        # monitor --live figshow command suite
        # ---------------------------------------------------------
        if cmd.startswith("monitor --live figshow"):
            sub_tokens = tokens[3:]
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

        # ---------------------------------------------------------
        # pids <pid> [--filter]
        # ---------------------------------------------------------
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

        # ---------------------------------------------------------
        # name <process_name> [--filter]
        # ---------------------------------------------------------
        if tokens[0] == "name" and len(tokens) > 1:
            target_name = tokens[1].lower()
            matching_snaps = [
                snap for snap in latest_snapshots.values()
                if snap["name"].lower() == target_name
            ]
            if not matching_snaps:
                print(f"No monitored processes matching name '{tokens[1]}'")
                return

            flag = tokens[2] if len(tokens) > 2 else None
            for snap in matching_snaps:
                if not flag:
                    self.show_pids(snap)
                elif flag in self.commFlags:
                    func = self.commFlags[flag]
                    func(snap.get("pid") if flag == "--l" else snap)
                else:
                    print(f"Unknown flag: {flag}")
                    break
            return

        # ---------------------------------------------------------
        # Standard root commands across all snapshots
        # ---------------------------------------------------------
        if cmd in self.commRootList:
            func = self.commRootList[cmd]
            for snapshot in latest_snapshots.values():
                func(snapshot)
            return

        # Direct flag execution across all snapshots (e.g., "pids --lr")
        flag = cmd.replace("pids ", "").strip()
        if flag in self.commFlags:
            func = self.commFlags[flag]
            for snapshot in latest_snapshots.values():
                func(snapshot.get("pid") if flag == "--l" else snapshot)
            return

        print(f"Unknown command: {cmd_str}")

    def show_pids(self, snapshot: dict):
        retText = ""
        for key, val in snapshot.items():
            retText += f"{key}: {val}\n"
        print(retText)

    def showListPids(self, pid: int):
        print(pid)

    def show_pids_ram(self, snapshot: dict):
        print(f"{snapshot['pid']} ({snapshot['name']}) ram: {snapshot['rss_kb']} KB")

    def show_pids_ram_MB(self, snapshot: dict):
        print(f"{snapshot['pid']} ({snapshot['name']}) ram: {snapshot['rss_kb'] / 1024:.2f} MB")

    def show_pids_ram_GB(self, snapshot: dict):
        print(f"{snapshot['pid']} ({snapshot['name']}) ram: {(snapshot['rss_kb'] / 1024) / 1024:.4f} GB")

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
# Monitor — two-state FSM per process
# ---------------------------------------------------------

STATE_HISTOGRAM = 1
STATE_SCATTER   = 2


class Monitor:

    def __init__(self, filt):
        self.filter       = filt
        self.previous_cpu = {}
        self.data         = {}
        self.running      = True
        self.commands     = Commands()

        self.target_pids  = filt.get_target_pids()
        if not self.target_pids:
            log.warning("No target processes found")

        self.fsm_state  = {}
        self.ram_max    = {}
        self.histograms = {}
        self.RAM_graph   = {}

    def _init_pid(self, pid: int, name: str) -> bool:
        ram_max_kb = get_max_ram(pid)
        bins       = partition_to_continious_rv([0, ram_max_kb])
        if bins is None:
            log.error(f"PID={pid}: could not build bins, skipping")
            return False

        self.ram_max[pid]    = ram_max_kb
        self.histograms[pid] = ProcessHistogram(pid, bins)
        self.RAM_graph[pid]   = ProcessRAM_Graph(pid, name)
        self.fsm_state[pid]  = STATE_HISTOGRAM

        log.info("Monitoring PID %d (%s)", pid, name)
        return True

    def _finalize_window(self, pid: int):
        hist = self.histograms[pid]

        dist = ContiniousDistribution(
            f"RAM histogram PID {pid}",
            hist.bins,
            hist.frequency[:]
        )

        mode_kb    = dist.getMode()
        ram_max_kb = self.ram_max[pid]
        ri         = mode_kb / ram_max_kb
        ts         = time.time_ns()

        self.RAM_graph[pid].add_point(ts, ri)

        hist.reset_counts()
        self.fsm_state[pid] = STATE_HISTOGRAM

    def show_live_figures(self, target_pids: list):
        """Displays live line graphs of ri vs time_ns for target processes."""
        import matplotlib.pyplot as plt
        from matplotlib.widgets import Button

        if not target_pids:
            print("No processes selected for live view.")
            return

        page_size = 3
        current_page = [0]
        total_pages = math.ceil(len(target_pids) / page_size)

        fig, axes = plt.subplots(3, 1, figsize=(10, 8))
        if page_size == 1:
            axes = [axes]
        plt.subplots_adjust(bottom=0.15)

        def update_plots():
            start_idx = current_page[0] * page_size
            page_pids = target_pids[start_idx:start_idx + page_size]

            for ax in axes:
                ax.clear()

            for i, pid in enumerate(page_pids):
                ax = axes[i]
                graph = self.RAM_graph.get(pid)
                if graph and graph.has_data():
                    # Plot continuous line graph over time_ns with point markers
                    ax.plot(graph.time_points, graph.ri_points, color="blue", linewidth=1.8, marker="o", markersize=4)
                    ax.set_title(f"{graph.proc_name} (PID {pid}) — ri (mode_kb / RAM_max) vs time_ns")
                else:
                    proc_name = get_process_name(pid) or "Process"
                    ax.set_title(f"{proc_name} (PID {pid}) — Waiting for window data...")

                ax.set_xlabel("time (ns)")
                ax.set_ylabel("ri")
                ax.set_ylim(0, 1)
                ax.grid(True)

            # Clear unused axes on the page if less than 3 processes remaining
            for j in range(len(page_pids), 3):
                axes[j].clear()
                axes[j].axis('off')

            fig.suptitle(f"Live Line Monitor — Page {current_page[0] + 1} of {total_pages}", fontsize=12)
            fig.canvas.draw_idle()

        # Navigation controls for pagination
        ax_prev = plt.axes([0.7, 0.02, 0.1, 0.05])
        ax_next = plt.axes([0.81, 0.02, 0.1, 0.05])
        btn_prev = Button(ax_prev, 'Previous')
        btn_next = Button(ax_next, 'Next')

        def prev_page(event):
            if current_page[0] > 0:
                current_page[0] -= 1
                update_plots()

        def next_page(event):
            if current_page[0] < total_pages - 1:
                current_page[0] += 1
                update_plots()

        btn_prev.on_clicked(prev_page)
        btn_next.on_clicked(next_page)

        update_plots()
        plt.show(block=False)

    def _check_console_input(self, latest_snapshots: dict):
        if sys.platform != "win32":
            if select.select([sys.stdin], [], [], 0)[0]:
                line = sys.stdin.readline()
                if line:
                    self.commands.execute(line.strip(), latest_snapshots, self)
        else:
            import msvcrt
            if msvcrt.kbhit():
                line = sys.stdin.readline()
                if line:
                    self.commands.execute(line.strip(), latest_snapshots, self)

    def loop(self, interval: float):
        latest_snapshots = {}
        while self.running:
            current_time = time.time_ns()

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

            self._check_console_input(latest_snapshots)
            time.sleep(interval)

    def stop(self):
        self.running = False

    def show_scatter(self, pid: int):
        if pid not in self.RAM_graph or not self.RAM_graph[pid].has_data():
            log.warning(
                f"PID={pid}: no scatter data yet — need at least one full "
                f"{WINDOW_DURATION}s window"
            )
            return
        self.RAM_graph[pid].plot()


# ---------------------------------------------------------
# Main
# ---------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pid",      nargs="*", type=int, default=[])
    parser.add_argument("--name",     nargs="*", default=[])
    parser.add_argument("--interval", type=float, default=SAMPLING_INTERVAL)
    args = parser.parse_args()

    try:
        import psutil
    except ImportError:
        print("ERROR: psutil is required. Install with: pip install psutil")
        sys.exit(1)

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