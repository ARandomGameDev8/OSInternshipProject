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
    RLIMIT_RSS is the actual RSS cap — unlike RLIMIT_AS which is virtual
    address space, not physical RAM. The original code also ignored the pid
    argument entirely and queried the monitor's own limits instead.
    If the limit is 'unlimited', falls back to total system RAM which is the
    true physical ceiling for any process.

    On Windows: /proc does not exist, falls back to total system RAM.
    """
    if sys.platform != "win32":
        try:
            with open(f"/proc/{pid}/limits", "r") as f:
                for line in f:
                    if line.startswith("Max resident set"):
                        # format: "Max resident set    <soft>    <hard>    bytes"
                        parts = line.split()
                        soft  = parts[3]
                        if soft != "unlimited":
                            return int(soft) // 1024   # bytes -> KB
                        break   # unlimited -> fall through
        except (FileNotFoundError, PermissionError, ValueError, IndexError):
            pass

    return psutil.virtual_memory().total // 1024


def partition_to_continious_rv(RAM_interval: list, num_bins: int = None):
    """
    Partition [lower_kb, upper_kb] into non-overlapping ContiniousRandomVariable
    bins. All bounds are in KB. Each bin's val (midpoint) is set by the now-fixed
    ContiniousRandomVariable.__init__.

    Uses log-spaced edges so resolution is highest at low RAM values (where most
    processes spend most of their time) and coarser at the high end (rare spikes).
    This matches the empirical shape of RSS distributions better than uniform bins.
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

    # Smaller ranges get more bins for finer resolution.
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

    # Log-spaced edges over [lower_kb, upper_kb].
    # The +1/-1 shift avoids log(0) while keeping the first edge at lower_kb.
    log_min = math.log(1)
    log_max = math.log(range_kb + 1)

    edges     = [
        lower_kb + math.exp(log_min + (log_max - log_min) * i / num_bins) - 1
        for i in range(num_bins + 1)
    ]
    edges[0]  = lower_kb   # force exact endpoints against float drift
    edges[-1] = upper_kb

    # ContiniousRandomVariable.__init__ is now fixed (double underscores),
    # so lowerBound, upperBound and val (midpoint) are all set correctly
    # by the constructor — no manual patching needed here.
    return [
        ContiniousRandomVariable(i, edges[i], edges[i + 1])
        for i in range(num_bins)
    ]


# ---------------------------------------------------------
# FSM STATE 1 — Histogram
#
# Accumulates RSS samples into fixed bins for one process
# over one WINDOW_DURATION window.
# Bins are built once from RAM_max and never change.
# Only the frequency counts reset between windows.
# ---------------------------------------------------------

class ProcessHistogram:

    def __init__(self, pid: int, bins: list):
        self.pid          = pid
        self.bins         = bins
        self.frequency    = [0] * len(bins)
        self.sample_count = 0

    def record(self, rss_kb: float):
        """Bucket rss_kb into the correct bin and increment its frequency."""
        for i, crv in enumerate(self.bins):
            if crv.getLower() <= rss_kb < crv.getUpper():
                self.frequency[i] += 1
                return
        # rss_kb == upper_kb exactly (edge case) -> last bin
        self.frequency[-1] += 1

    def reset_counts(self):
        """Zero frequency counts for the next window. Bins stay fixed."""
        self.frequency    = [0] * len(self.bins)
        self.sample_count = 0

    def increment_sample(self):
        self.sample_count += 1

    def is_window_complete(self) -> bool:
        return self.sample_count >= SAMPLES_PER_WINDOW


# ---------------------------------------------------------
# FSM STATE 2 — Scatter accumulator
#
# Grows one (time_ns, ri) pair per completed window.
# Each observation is stored as a ContiniousRandomVariable
# with lowerBound == upperBound == val == the point value,
# and frequency 1, which is what ContiniousDistribution and
# ScatterPlot expect.
# ---------------------------------------------------------

class ProcessRAM_Graph:
    """
    Accumulates (time_ns, ri) pairs across windows for one process.
    Both are plain numbers — no distribution wrapper needed.
    Plotted directly with matplotlib.
    """

    def __init__(self, pid: int, name: str):
        self.pid        = pid
        self.proc_name  = name
        self.time_points = []   # list[float]  — time_ns at end of each window
        self.ri_points   = []   # list[float]  — mode_kb / RAM_max for that window

    def add_point(self, time_ns: int, ri: float):
        self.time_points.append(float(time_ns))
        self.ri_points.append(ri)

    def has_data(self) -> bool:
        return len(self.time_points) > 0

    def plot(self):
        """Plot ri vs time_ns directly — no distribution or ScatterPlot wrapper."""
        import matplotlib.pyplot as plt

        plt.figure(figsize=(10, 5))
        plt.scatter(self.time_points, self.ri_points, color="blue", s=30)
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
# Monitor  —  two-state FSM per process
#
#   STATE 1  (HISTOGRAM)
#     Each loop tick: record rss_kb into histogram, increment sample count.
#     After SAMPLES_PER_WINDOW samples -> transition to STATE 2 same tick.
#
#   STATE 2  (SCATTER)
#     Build ContiniousDistribution from histogram frequency counts.
#     mode_kb  = dist.getMode()       <- midpoint of most-frequent bin, in KB
#     ri       = mode_kb / RAM_max_kb <- normalized ratio in [0, 1]
#     Append (time_ns, ri) to scatter accumulator.
#     Reset histogram counts (bins stay fixed).
#     -> back to STATE 1 immediately (same tick).
# ---------------------------------------------------------

STATE_HISTOGRAM = 1
STATE_SCATTER   = 2


class Monitor:

    def __init__(self, filt):
        self.filter       = filt
        self.previous_cpu = {}
        self.data         = {}
        self.running      = True

        self.target_pids  = filt.get_target_pids()
        if not self.target_pids:
            log.warning("No target processes found")

        # All per-pid structures initialized lazily on first snapshot
        self.fsm_state  = {}   # pid -> STATE_HISTOGRAM | STATE_SCATTER
        self.ram_max    = {}   # pid -> int (KB), fixed for the session
        self.histograms = {}   # pid -> ProcessHistogram
        self.RAM_graph   = {}   # pid -> ProcessScatter

    def _init_pid(self, pid: int, name: str) -> bool:
        """Initialize all per-pid structures on first snapshot for this pid."""
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
        """
        STATE 2: build distribution from the completed window's frequency counts,
        extract mode, compute ri, append scatter point, reset histogram, return
        to STATE 1. Runs on the same tick as the transition from STATE 1.
        """
        hist = self.histograms[pid]

        dist = ContiniousDistribution(
            f"RAM histogram PID {pid}",
            hist.bins,
            hist.frequency[:]   # copy so the upcoming reset does not corrupt dist
        )

        mode_kb    = dist.getMode()           # midpoint of the most-frequent bin (KB)
        ram_max_kb = self.ram_max[pid]
        ri         = mode_kb / ram_max_kb     # normalized ratio in [0, 1]
        ts         = time.time_ns()

        self.RAM_graph[pid].add_point(ts, ri)

        hist.reset_counts()
        self.fsm_state[pid] = STATE_HISTOGRAM

    def loop(self, interval: float):
        while self.running:
            current_time = time.time_ns()

            for pid in self.target_pids:
                if not psutil.pid_exists(pid):
                    if pid in self.previous_cpu:
                        log.info(f"Process {pid} has terminated")
                        del self.previous_cpu[pid]
                    continue

                snapshot = read_process(pid, current_time)
                if not snapshot:
                    continue

                # Lazy init on first encounter
                if pid not in self.fsm_state:
                    if not self._init_pid(pid, snapshot["name"]):
                        continue

                # CPU %
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

                # STATE 1: record sample into histogram
                if self.fsm_state[pid] == STATE_HISTOGRAM:
                    self.histograms[pid].record(snapshot["rss_kb"])
                    self.histograms[pid].increment_sample()
                    if self.histograms[pid].is_window_complete():
                        self.fsm_state[pid] = STATE_SCATTER

                # STATE 2: finalize window, reset, return to STATE 1 (same tick)
                if self.fsm_state[pid] == STATE_SCATTER:
                    self._finalize_window(pid)

            time.sleep(interval)

    def stop(self):
        self.running = False

    def show_scatter(self, pid: int):
        """Display the scatter plot for a pid. Call after monitoring ends."""
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