#!/usr/bin/env python3

"""
master.py — Native Windows ETW Call Tracer with configurable live GUI 
rendering per PID (Size, Velocity, Acceleration for malloc/free calls),
dynamic tiered range histogram binning, and MySQL persistence.
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
import warnings
import ctypes
from ctypes import wintypes
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

# Suppress Matplotlib tight_layout user warnings gracefully
warnings.filterwarnings("ignore", category=UserWarning, module="matplotlib")

# ---------------------------------------------------------
# Window / refresh constants
# ---------------------------------------------------------

SAMPLING_INTERVAL     = 1.0                                        
WINDOW_DURATION       = 300                                        
SAMPLES_PER_WINDOW    = int(WINDOW_DURATION / SAMPLING_INTERVAL)   
LIVE_REFRESH_INTERVAL = 1.0                                        
DATABASE_REFRESH      = 300.0                                      

# Microsoft-Windows-Heap Provider GUID
HEAP_PROVIDER_GUID = "{E61C8C90-1C48-4E58-B78A-0611367E442B}"


# ---------------------------------------------------------
# Ctypes Native Windows ETW Consumer Engine
# ---------------------------------------------------------

class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_byte * 8),
    ]

    def __init__(self, guid_str):
        super().__init__()
        import uuid
        u = uuid.UUID(guid_str)
        self.Data1 = u.time_low
        self.Data2 = u.time_mid
        self.Data3 = u.time_hi_version
        for i, b in enumerate(u.bytes[8:]):
            self.Data4[i] = b

class EVENT_DESCRIPTOR(ctypes.Structure):
    _fields_ = [
        ("Id", wintypes.WORD),
        ("Version", ctypes.c_ubyte),
        ("Channel", ctypes.c_ubyte),
        ("Level", ctypes.c_ubyte),
        ("Opcode", ctypes.c_ubyte),
        ("Task", wintypes.WORD),
        ("Keyword", ctypes.c_uint64),
    ]

class EVENT_HEADER(ctypes.Structure):
    _fields_ = [
        ("Size", wintypes.WORD),
        ("HeaderType", wintypes.WORD),
        ("Flags", wintypes.WORD),
        ("EventProperty", wintypes.WORD),
        ("ThreadId", wintypes.DWORD),
        ("ProcessId", wintypes.DWORD),
        ("TimeStamp", ctypes.c_int64),
        ("ProviderId", GUID),
        ("EventDescriptor", EVENT_DESCRIPTOR),
        ("KernelTime", wintypes.DWORD),
        ("UserTime", wintypes.DWORD),
        ("ActivityId", GUID),
    ]

class ETW_BUFFER_HEADER(ctypes.Structure):
    _fields_ = [("Reserved", ctypes.c_byte * 64)]

class EVENT_RECORD(ctypes.Structure):
    pass

EVENT_RECORD_PTR = ctypes.POINTER(EVENT_RECORD)

EVENT_RECORD._fields_ = [
    ("EventHeader", EVENT_HEADER),
    ("BufferContext", ctypes.c_byte * 4),
    ("ExtendedDataCount", wintypes.WORD),
    ("UserDataLength", wintypes.WORD),
    ("ExtendedData", ctypes.c_void_p),
    ("UserData", ctypes.c_void_p),
    ("UserContext", ctypes.c_void_p),
]

EVENT_RECORD_CALLBACK = ctypes.WINFUNCTYPE(None, EVENT_RECORD_PTR)

class EVENT_TRACE_LOGFILE(ctypes.Structure):
    _fields_ = [
        ("LoggerName", wintypes.LPWSTR),
        ("LogFileName", wintypes.LPWSTR),
        ("LogFileMode", wintypes.ULONG),
        ("BufferLength", wintypes.ULONG),
        ("BufferSize", wintypes.ULONG),
        ("Filled", wintypes.ULONG),
        ("EventsLost", wintypes.ULONG),
        ("OldEventCallback", ctypes.c_void_p),
        ("BufferCallback", ctypes.c_void_p),
        ("BuffersRead", wintypes.ULONG),
        ("EventCallback", EVENT_RECORD_CALLBACK),
        ("Context", ctypes.c_void_p),
    ]

class EVENT_TRACE_PROPERTIES(ctypes.Structure):
    _fields_ = [
        ("Wnode_BufferSize", wintypes.ULONG),
        ("Wnode_Guid", GUID),
        ("Wnode_ClientContext", wintypes.ULONG),
        ("Wnode_Flags", wintypes.ULONG),
        ("BufferSize", wintypes.ULONG),
        ("MinimumBuffers", wintypes.ULONG),
        ("MaximumBuffers", wintypes.ULONG),
        ("MaximumFileSize", wintypes.ULONG),
        ("LogFileMode", wintypes.ULONG),
        ("FlushTimer", wintypes.ULONG),
        ("EnableFlags", wintypes.ULONG),
        ("AgeLimit", wintypes.LONG),
        ("NumberOfBuffers", wintypes.ULONG),
        ("FreeBuffers", wintypes.ULONG),
        ("EventsLost", wintypes.ULONG),
        ("BuffersWritten", wintypes.ULONG),
        ("LogBuffersLost", wintypes.ULONG),
        ("RealTimeBuffersLost", wintypes.ULONG),
        ("LoggerThreadId", wintypes.HANDLE),
        ("LogFileNameOffset", wintypes.ULONG),
        ("LoggerNameOffset", wintypes.ULONG),
    ]


class NativeETWConsumer:
    def __init__(self, target_pids, event_callback):
        self.target_pids = target_pids
        self.user_callback = event_callback
        self.session_name = f"HeapTracerSession_{os.getpid()}"
        self.running = False
        self.trace_handle = None
        self.session_handle = None

        self._advapi32 = ctypes.windll.advapi32
        self._c_callback = EVENT_RECORD_CALLBACK(self._internal_callback)

    def _internal_callback(self, record_ptr):
        if not record_ptr:
            return
        rec = record_ptr.contents
        pid = rec.EventHeader.ProcessId

        if pid in self.target_pids:
            event_id = rec.EventHeader.EventDescriptor.Id
            user_data = rec.UserData
            data_len = rec.UserDataLength

            if event_id in (1, 33) and data_len >= 8:
                alloc_bytes = ctypes.cast(user_data, ctypes.POINTER(ctypes.c_uint64)).contents.value
                self.user_callback(pid, "malloc", alloc_bytes)

            elif event_id in (2, 34) and data_len >= 8:
                free_bytes = ctypes.cast(user_data, ctypes.POINTER(ctypes.c_uint64)).contents.value
                self.user_callback(pid, "free", free_bytes)

    def start(self):
        self.running = True
        
        props_size = ctypes.sizeof(EVENT_TRACE_PROPERTIES) + 512
        buf = ctypes.create_string_buffer(props_size)
        props = ctypes.cast(buf, ctypes.POINTER(EVENT_TRACE_PROPERTIES)).contents
        props.Wnode_BufferSize = props_size
        props.Wnode_Flags = 0x00020000
        props.LogFileMode = 0x00000100
        props.LoggerNameOffset = ctypes.sizeof(EVENT_TRACE_PROPERTIES)

        session_guid = GUID("{11223344-5566-7788-9900-AABBCCDDEEFF}")
        props.Wnode_Guid = session_guid

        sess_handle = wintypes.HANDLE()
        self._advapi32.ControlTraceW(0, self.session_name, ctypes.byref(props), 1)

        res = self._advapi32.StartTraceW(ctypes.byref(sess_handle), self.session_name, ctypes.byref(props))
        if res != 0:
            log.warning("ETW StartTrace failed with code %d (Run terminal as Administrator)", res)
            return

        self.session_handle = sess_handle
        heap_guid = GUID(HEAP_PROVIDER_GUID)

        self._advapi32.EnableTraceEx2(
            sess_handle,
            ctypes.byref(heap_guid),
            1,
            5,
            0xFFFFFFFFFFFFFFFF,
            0,
            0,
            None
        )

        logfile = EVENT_TRACE_LOGFILE()
        logfile.LoggerName = ctypes.c_wchar_p(self.session_name)
        logfile.LogFileMode = 0x00000100 | 0x10000000
        logfile.EventCallback = self._c_callback

        t_handle = self._advapi32.OpenTraceW(ctypes.byref(logfile))
        self.trace_handle = t_handle

        def _process_thread():
            handles = (wintypes.HANDLE * 1)(t_handle)
            self._advapi32.ProcessTrace(handles, 1, None, None)

        threading.Thread(target=_process_thread, daemon=True).start()
        log.info("Native Windows ETW Session started successfully for Microsoft-Windows-Heap provider.")

    def stop(self):
        if self.session_handle:
            props_size = ctypes.sizeof(EVENT_TRACE_PROPERTIES) + 512
            buf = ctypes.create_string_buffer(props_size)
            props = ctypes.cast(buf, ctypes.POINTER(EVENT_TRACE_PROPERTIES)).contents
            props.Wnode_BufferSize = props_size
            self._advapi32.ControlTraceW(self.session_handle, self.session_name, ctypes.byref(props), 1)


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


def get_max_ram(pid: int) -> int:
    return psutil.virtual_memory().total // 1024


# ---------------------------------------------------------
# Dynamic Tiered Partitioning Strategy
# ---------------------------------------------------------

def partition_to_continious_rv_custom_dynamic(
    pid: int, current_val_kb: float, max_system_ram_kb: float
):
    dynamic_upper_kb = min(
        max_system_ram_kb, max(128 * 1024, current_val_kb * 1.5)
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
        num_bins = 16 if dynamic_upper_gb > 32 else 8

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
        self.samples           = {"malloc": [], "free": []}
        self.sample_count      = 0
        self.last_distribution = {"malloc": None, "free": None}

    def record(self, event_type: str, size_kb: float):
        if event_type in self.samples:
            self.samples[event_type].append(size_kb)

    def increment_sample(self):
        self.sample_count += 1

    def is_window_complete(self) -> bool:
        return self.sample_count >= SAMPLES_PER_WINDOW

    def _build_dist(self, key: str):
        data = self.samples[key]
        if not data:
            return None

        peak_val = max(data)
        bins, num_bins = partition_to_continious_rv_custom_dynamic(
            self.pid, peak_val, self.max_system_ram_kb
        )

        frequencies = [0] * num_bins
        for val in data:
            placed = False
            for i, crv in enumerate(bins):
                if crv.getLower() <= val < crv.getUpper():
                    frequencies[i] += 1
                    placed = True
                    break
            if not placed:
                frequencies[-1] += 1

        return ContiniousDistribution(
            f"Heap {key.capitalize()} Distribution PID {self.pid}",
            bins,
            frequencies,
        )

    def get_distributions(self):
        dist_malloc = self._build_dist("malloc")
        dist_free   = self._build_dist("free")
        
        self.last_distribution = {"malloc": dist_malloc, "free": dist_free}
        return self.last_distribution

    def reset_counts(self):
        self.samples      = {"malloc": [], "free": []}
        self.sample_count = 0


# ---------------------------------------------------------
# Heap Graph Data Container
# ---------------------------------------------------------

class ProcessRAM_Graph:

    KB_TO_GB = 1024.0 * 1024.0

    def __init__(self, pid: int, name: str):
        self.pid         = pid
        self.proc_name   = name
        self.time_points = []
        
        self.mallocSize  = []
        self.freeSize    = []

    def add_malloc_event(self, time_ns: int, size_bytes: float):
        size_kb = size_bytes / 1024.0
        self.time_points.append(float(time_ns))
        self.mallocSize.append(size_kb / self.KB_TO_GB)
        self.freeSize.append(0.0)

    def add_free_event(self, time_ns: int, size_bytes: float):
        freed_kb = size_bytes / 1024.0
        self.time_points.append(float(time_ns))
        self.mallocSize.append(0.0)
        self.freeSize.append(freed_kb / self.KB_TO_GB)

    def has_data(self) -> bool:
        return len(self.time_points) > 0

    @staticmethod
    def compute_kinematics(series, time_points):
        if len(series) < 2:
            return [0.0] * len(series), [0.0] * len(series)

        vels = [0.0]
        for j in range(1, len(series)):
            dt = (time_points[j] - time_points[j - 1]) / 1e9
            vels.append((series[j] - series[j - 1]) / dt if dt > 0 else 0.0)

        accs = [0.0]
        for j in range(1, len(vels)):
            dt = (time_points[j] - time_points[j - 1]) / 1e9
            accs.append((vels[j] - vels[j - 1]) / dt if dt > 0 else 0.0)

        return vels, accs


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
        except Error as e:
            log.error("DB ensure_process_exists(%d): %s", pid, e)

    def add_process_statistics(
        self,
        pid: int,
        snapshot_time: datetime,
        mean: float,
        variance: float,
        standard_deviation: float,
        mode: float,
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

    def _apply_dynamic_time_axis(self, fig, ax, times):
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

        locator   = mdates.AutoDateLocator(minticks=4, maxticks=10)
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
            window_start  = max(earliest_time, latest_time - timedelta(minutes=30))
            padding       = timedelta(seconds=15)
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
        ax.set_title(f"PID {pid} — Heap Velocity over time (DB history)")
        ax.set_xlabel("Snapshot time")
        ax.set_ylabel("Velocity  (GB / ns)")
        ax.grid(True)
        try:
            fig.tight_layout()
        except Exception:
            pass
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
        ax.set_title(f"PID {pid} — Heap Variance over time (DB history)")
        ax.set_xlabel("Snapshot time")
        ax.set_ylabel("Variance  (GB²)")
        ax.grid(True)
        try:
            fig.tight_layout()
        except Exception:
            pass
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
        ax.set_title(f"PID {pid} — Mean Heap over time (DB history)")
        ax.set_xlabel("Snapshot time")
        ax.set_ylabel("Mean Heap  (GB)")
        ax.grid(True)
        try:
            fig.tight_layout()
        except Exception:
            pass
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

        return {
            "pid":          pid,
            "name":         proc.name(),
            "rss_kb":       rss_kb,
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

    def get_target_pids(self):
        return self.target_pids


# ---------------------------------------------------------
# Command Interpreter
# ---------------------------------------------------------

class Commands:

    def __init__(self):
        self.active_watch_mode = None

    def execute(self, cmd_str: str, latest_snapshots: dict, monitor=None):
        cmd = cmd_str.strip()
        if not cmd:
            return

        tokens = cmd.split()

        if cmd.startswith("monitor --live figshow"):
            mode = "size"
            sub_tokens = tokens[3:]

            if sub_tokens and sub_tokens[0] == "--vel":
                mode = "velocity"
                sub_tokens = sub_tokens[1:]
            elif sub_tokens and sub_tokens[0] == "--acc":
                mode = "acceleration"
                sub_tokens = sub_tokens[1:]

            target_pids = []
            if not sub_tokens:
                target_pids = list(latest_snapshots.keys())
            elif sub_tokens[0].isdigit():
                pid = int(sub_tokens[0])
                if pid in latest_snapshots:
                    target_pids = [pid]

            monitor.show_live_figures(target_pids, mode=mode)
            return

        if cmd.startswith("db plot") and monitor is not None:
            if len(tokens) >= 4 and tokens[3].isdigit():
                flag    = tokens[2]
                pid_arg = int(tokens[3])
                monitor.queue_db_plot_request(flag, pid_arg)


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

        self.fsm_state  = {}
        self.ram_max    = {}
        self.histograms = {}
        self.RAM_graph  = {}

        self.live_plot_active = False
        self.live_plot_mode   = "size"
        self.live_target_pids = []
        self.fig              = None
        self.axes             = None
        self.active_db_fig    = None

        self.db_plot_requests = queue.Queue()
        self.db_integration   = DATABASE_INTEGRATION(time_period_save_to_memory=DATABASE_REFRESH)
        self.last_db_flush_time = {}

        self.etw_consumer = NativeETWConsumer(
            target_pids=self.target_pids,
            event_callback=self._handle_heap_call_event
        )
        self.etw_consumer.start()

    def _handle_heap_call_event(self, pid: int, event_type: str, size_bytes: int):
        now_ns = time.time_ns()

        if pid not in self.RAM_graph:
            self._init_pid(pid, get_process_name(pid) or "Process")

        graph = self.RAM_graph[pid]
        hist  = self.histograms[pid]

        size_kb = size_bytes / 1024.0

        if event_type == "malloc":
            graph.add_malloc_event(now_ns, size_bytes)
            hist.record("malloc", size_kb)
        elif event_type == "free":
            graph.add_free_event(now_ns, size_bytes)
            hist.record("free", size_kb)

    def _on_close(self, event):
        self.live_plot_active = False
        self.fig = None
        self.axes = None
        self.active_db_fig = None

        import matplotlib.pyplot as plt
        plt.close("all")
        gc.collect()

    def _init_pid(self, pid: int, name: str) -> bool:
        ram_max_kb = get_max_ram(pid)

        self.ram_max[pid]    = ram_max_kb
        self.histograms[pid] = ProcessHistogram(pid, max_system_ram_kb=ram_max_kb)
        self.RAM_graph[pid]  = ProcessRAM_Graph(pid, name)
        self.fsm_state[pid]  = STATE_HISTOGRAM

        self.last_db_flush_time[pid] = time.time()
        self.db_integration.ensure_process_exists(pid, burst_time=0.0)
        return True

    def _finalize_window(self, pid: int):
        hist = self.histograms[pid]
        dists = hist.get_distributions()

        if dists["malloc"] is None and dists["free"] is None:
            hist.reset_counts()
            self.fsm_state[pid] = STATE_HISTOGRAM
            return

        elapsed_since_flush = time.time() - self.last_db_flush_time.get(pid, 0.0)
        if elapsed_since_flush >= DATABASE_REFRESH:
            self.fsm_state[pid] = STATE_UPDATE_DATABASE
        else:
            hist.reset_counts()
            self.fsm_state[pid] = STATE_HISTOGRAM

    def _save_to_database(self, pid: int):
        hist  = self.histograms.get(pid)
        graph = self.RAM_graph.get(pid)

        if hist is None or hist.last_distribution["malloc"] is None:
            hist.reset_counts()
            self.last_db_flush_time[pid] = time.time()
            self.fsm_state[pid]          = STATE_HISTOGRAM
            return

        dist = hist.last_distribution["malloc"]

        mean_kb     = dist.getMean()
        variance_kb = dist.getVariance()
        std_dev_kb  = dist.getStandardDeviation()
        mode_kb     = dist.getMode()

        KB_TO_GB    = 1024.0 * 1024.0
        mean_gb     = mean_kb  / KB_TO_GB
        variance_gb = variance_kb / (KB_TO_GB ** 2)
        std_dev_gb  = std_dev_kb / KB_TO_GB
        mode_gb     = mode_kb  / KB_TO_GB

        snapshot_time = datetime.now()

        self.db_integration.add_process_statistics(
            pid                = pid,
            snapshot_time      = snapshot_time,
            mean               = mean_gb,
            variance           = variance_gb,
            standard_deviation = std_dev_gb,
            mode               = mode_gb,
        )

        hist.reset_counts()

        self.last_db_flush_time[pid] = time.time()
        self.fsm_state[pid] = STATE_HISTOGRAM

    def show_live_figures(self, target_pids: list, mode: str = "size"):
        import matplotlib.pyplot as plt

        self._on_close(None)
        plt.ion()
        self.live_target_pids = target_pids
        self.live_plot_mode   = mode

        num_pids = min(len(target_pids), 3)
        if num_pids == 0:
            return

        # 2 Subplots per PID (Malloc & Free for active mode)
        num_rows = num_pids * 2
        self.fig, self.axes = plt.subplots(num_rows, 1, figsize=(10, 3.5 * num_rows))
        
        if num_rows == 1:
            self.axes = [self.axes]
        else:
            self.axes = list(self.axes)

        self.fig.canvas.mpl_connect("close_event", self._on_close)
        self.live_plot_active = True

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
                    ax_malloc = self.axes[i * 2]
                    ax_free   = self.axes[i * 2 + 1]

                    ax_malloc.clear()
                    ax_free.clear()

                    graph = self.RAM_graph.get(pid)
                    if graph and graph.has_data():
                        t0    = graph.time_points[0]
                        times = [(t - t0) / 1e9 for t in graph.time_points]

                        m_vels, m_accs = ProcessRAM_Graph.compute_kinematics(graph.mallocSize, graph.time_points)
                        f_vels, f_accs = ProcessRAM_Graph.compute_kinematics(graph.freeSize, graph.time_points)

                        label = f"{graph.proc_name} (PID {pid})"

                        if self.live_plot_mode == "size":
                            ax_malloc.plot(times, graph.mallocSize, color="royalblue", linewidth=1.5, marker="o", markersize=3)
                            ax_malloc.set_title(f"{label} — Malloc Allocation Size")
                            ax_malloc.set_ylabel("Allocated (GB)")

                            ax_free.plot(times, graph.freeSize, color="teal", linewidth=1.5, marker="s", markersize=3)
                            ax_free.set_title(f"{label} — Free Deallocation Size")
                            ax_free.set_ylabel("Deallocated (GB)")

                        elif self.live_plot_mode == "velocity":
                            ax_malloc.plot(times, m_vels, color="darkorange", linewidth=1.5, marker="o", markersize=3)
                            ax_malloc.set_title(f"{label} — Malloc Allocation Velocity")
                            ax_malloc.set_ylabel("Velocity (GB/s)")

                            ax_free.plot(times, f_vels, color="darkgreen", linewidth=1.5, marker="s", markersize=3)
                            ax_free.set_title(f"{label} — Free Deallocation Velocity")
                            ax_free.set_ylabel("Velocity (GB/s)")

                        elif self.live_plot_mode == "acceleration":
                            ax_malloc.plot(times, m_accs, color="crimson", linewidth=1.5, marker="o", markersize=3)
                            ax_malloc.set_title(f"{label} — Malloc Allocation Acceleration")
                            ax_malloc.set_ylabel("Accel (GB/s²)")

                            ax_free.plot(times, f_accs, color="purple", linewidth=1.5, marker="s", markersize=3)
                            ax_free.set_title(f"{label} — Free Deallocation Acceleration")
                            ax_free.set_ylabel("Accel (GB/s²)")

                        ax_malloc.grid(True)
                        ax_free.grid(True)

                    ax_free.set_xlabel("Time (seconds since start)")

                try:
                    self.fig.tight_layout()
                except Exception:
                    pass
                self.fig.canvas.draw_idle()

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

            if flag == "--var":
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
                        continue

                    snapshot = read_process(pid, current_time)
                    if not snapshot:
                        continue

                    latest_snapshots[pid] = snapshot

                    if pid not in self.fsm_state:
                        if not self._init_pid(pid, snapshot["name"]):
                            continue

                    if self.fsm_state[pid] == STATE_HISTOGRAM:
                        self.histograms[pid].increment_sample()
                        if self.histograms[pid].is_window_complete():
                            self.fsm_state[pid] = STATE_LIVE_PLOT

                    if self.fsm_state[pid] == STATE_LIVE_PLOT:
                        self._finalize_window(pid)

                    if self.fsm_state[pid] == STATE_UPDATE_DATABASE:
                        self._save_to_database(pid)

            cmd = self.input_handler.poll()
            if cmd:
                self.commands.execute(cmd, latest_snapshots, self)

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
        if hasattr(self, 'etw_consumer'):
            self.etw_consumer.stop()
        self.db_integration.close()


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

    def handle_stop(s, f):
        monitor.stop()

    signal.signal(signal.SIGINT, handle_stop)

    try:
        monitor.loop(args.interval)
    except KeyboardInterrupt:
        log.info("Monitoring stopped by user")


if __name__ == "__main__":
    main()