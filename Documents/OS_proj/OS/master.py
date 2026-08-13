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
import queue
import signal
import argparse
import logging
import threading
import warnings
import ctypes
from collections import deque
from ctypes import wintypes
from datetime import datetime, timedelta
from mysql.connector import Error

import psutil
import matplotlib.dates as mdates

from Documents.OS_proj.STATS.ContiniousRandomVariable import ContiniousRandomVariable
from Documents.OS_proj.STATS.Univariate.Distributions import ContiniousDistribution
from Documents.OS_proj.DataBaseConnector.mySQL_Backend import Database


logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(message)s",
)

log = logging.getLogger("master")

warnings.filterwarnings("ignore", category=UserWarning, module="matplotlib")

# ---------------------------------------------------------
# Window / refresh constants
# ---------------------------------------------------------

SAMPLING_INTERVAL     = 1.0
WINDOW_DURATION       = 300
SAMPLES_PER_WINDOW    = int(WINDOW_DURATION / SAMPLING_INTERVAL)
LIVE_REFRESH_INTERVAL = 1.0
DATABASE_REFRESH      = 300.0
MAX_GRAPH_POINTS      = 10_000

# Microsoft-Windows-Heap Provider GUID
HEAP_PROVIDER_GUID = "{E61C8C90-1C48-4E58-B78A-0611367E442B}"

# ---------------------------------------------------------
# Ctypes Native Windows ETW Consumer Engine
# ---------------------------------------------------------

PROCESS_TRACE_MODE_REAL_TIME    = 0x00000100
PROCESS_TRACE_MODE_EVENT_RECORD = 0x10000000
WNODE_FLAG_TRACED_GUID          = 0x00020000
EVENT_TRACE_REAL_TIME_MODE      = 0x00000100
EVENT_TRACE_CONTROL_STOP        = 1
INVALID_HANDLE_VALUE            = ctypes.c_uint64(-1).value


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    def __init__(self, guid_str=""):
        super().__init__()
        if guid_str:
            import uuid
            u = uuid.UUID(guid_str)
            self.Data1 = u.time_low
            self.Data2 = u.time_mid
            self.Data3 = u.time_hi_version
            for i, b in enumerate(u.bytes[8:]):
                self.Data4[i] = b


class WNODE_HEADER(ctypes.Structure):
    _fields_ = [
        ("BufferSize",    ctypes.c_ulong),
        ("ProviderId",    ctypes.c_ulong),
        ("HistoricalContext", ctypes.c_uint64),
        ("TimeStamp",     ctypes.c_int64),
        ("Guid",          GUID),
        ("ClientContext", ctypes.c_ulong),
        ("Flags",         ctypes.c_ulong),
    ]


class EVENT_TRACE_PROPERTIES(ctypes.Structure):
    _fields_ = [
        ("Wnode",                WNODE_HEADER),
        ("BufferSize",           ctypes.c_ulong),
        ("MinimumBuffers",       ctypes.c_ulong),
        ("MaximumBuffers",       ctypes.c_ulong),
        ("MaximumFileSize",      ctypes.c_ulong),
        ("LogFileMode",          ctypes.c_ulong),
        ("FlushTimer",           ctypes.c_ulong),
        ("EnableFlags",          ctypes.c_ulong),
        ("AgeLimit",             ctypes.c_long),
        ("NumberOfBuffers",      ctypes.c_ulong),
        ("FreeBuffers",          ctypes.c_ulong),
        ("EventsLost",           ctypes.c_ulong),
        ("BuffersWritten",       ctypes.c_ulong),
        ("LogBuffersLost",       ctypes.c_ulong),
        ("RealTimeBuffersLost",  ctypes.c_ulong),
        ("LoggerThreadId",       ctypes.c_void_p),
        ("LogFileNameOffset",    ctypes.c_ulong),
        ("LoggerNameOffset",     ctypes.c_ulong),
    ]


class EVENT_DESCRIPTOR(ctypes.Structure):
    _fields_ = [
        ("Id",      ctypes.c_ushort),
        ("Version", ctypes.c_ubyte),
        ("Channel", ctypes.c_ubyte),
        ("Level",   ctypes.c_ubyte),
        ("Opcode",  ctypes.c_ubyte),
        ("Task",    ctypes.c_ushort),
        ("Keyword", ctypes.c_uint64),
    ]


class EVENT_HEADER(ctypes.Structure):
    _fields_ = [
        ("Size",            ctypes.c_ushort),
        ("HeaderType",      ctypes.c_ushort),
        ("Flags",           ctypes.c_ushort),
        ("EventProperty",   ctypes.c_ushort),
        ("ThreadId",        ctypes.c_ulong),
        ("ProcessId",       ctypes.c_ulong),
        ("TimeStamp",       ctypes.c_int64),
        ("ProviderId",      GUID),
        ("EventDescriptor", EVENT_DESCRIPTOR),
        ("KernelTime",      ctypes.c_ulong),
        ("UserTime",        ctypes.c_ulong),
        ("ActivityId",      GUID),
    ]


class ETW_BUFFER_CONTEXT(ctypes.Structure):
    _fields_ = [
        ("ProcessorNumber", ctypes.c_ubyte),
        ("Alignment",       ctypes.c_ubyte),
        ("LoggerId",        ctypes.c_ushort),
    ]


class EVENT_RECORD(ctypes.Structure):
    _fields_ = [
        ("EventHeader",       EVENT_HEADER),
        ("BufferContext",     ETW_BUFFER_CONTEXT),
        ("ExtendedDataCount", ctypes.c_ushort),
        ("UserDataLength",    ctypes.c_ushort),
        ("ExtendedData",      ctypes.c_void_p),
        ("UserData",          ctypes.c_void_p),
        ("UserContext",       ctypes.c_void_p),
    ]


EVENT_RECORD_CALLBACK = ctypes.WINFUNCTYPE(None, ctypes.POINTER(EVENT_RECORD))


class TRACE_LOGFILE_HEADER(ctypes.Structure):
    _fields_ = [
        ("BufferSize",         ctypes.c_ulong),
        ("MajorVersion",       ctypes.c_ubyte),
        ("MinorVersion",       ctypes.c_ubyte),
        ("SubVersion",         ctypes.c_ubyte),
        ("SubMinorVersion",    ctypes.c_ubyte),
        ("ProviderVersion",    ctypes.c_ulong),
        ("NumberOfProcessors", ctypes.c_ulong),
        ("EndTime",            ctypes.c_int64),
        ("TimerResolution",    ctypes.c_ulong),
        ("MaximumFileSize",    ctypes.c_ulong),
        ("LogFileMode",        ctypes.c_ulong),
        ("BuffersWritten",     ctypes.c_ulong),
        ("StartBuffers",       ctypes.c_ulong),
        ("PointerSize",        ctypes.c_ulong),
        ("EventsLost",         ctypes.c_ulong),
        ("CpuSpeedInMHz",      ctypes.c_ulong),
        ("LoggerName",         ctypes.c_void_p),
        ("LogFileName",        ctypes.c_void_p),
        ("TimeZone",           ctypes.c_byte * 176),
        ("BootTime",           ctypes.c_int64),
        ("PerfFreq",           ctypes.c_int64),
        ("StartTime",          ctypes.c_int64),
        ("ReservedFlags",      ctypes.c_ulong),
        ("BuffersLost",        ctypes.c_ulong),
    ]


class EVENT_TRACE_LOGFILE(ctypes.Structure):
    _fields_ = [
        ("LogFileName",      ctypes.c_wchar_p),
        ("LoggerName",       ctypes.c_wchar_p),
        ("CurrentTime",      ctypes.c_int64),
        ("BuffersRead",      ctypes.c_ulong),
        ("ProcessTraceMode", ctypes.c_ulong),
        ("CurrentEvent",     EVENT_RECORD),
        ("LogfileHeader",    TRACE_LOGFILE_HEADER),
        ("BufferCallback",   ctypes.c_void_p),
        ("BufferSize",       ctypes.c_ulong),
        ("Filled",           ctypes.c_ulong),
        ("EventsLost",       ctypes.c_ulong),
        ("EventCallback",    EVENT_RECORD_CALLBACK),
        ("IsKernelTrace",    ctypes.c_ulong),
        ("Context",          ctypes.c_void_p),
    ]


def _make_props_buffer(session_name: str):
    name_bytes      = (session_name + "\0").encode("utf-16-le")
    props_size      = ctypes.sizeof(EVENT_TRACE_PROPERTIES) + len(name_bytes) + 512
    buf             = ctypes.create_string_buffer(props_size)
    ptr             = ctypes.cast(buf, ctypes.POINTER(EVENT_TRACE_PROPERTIES))

    ptr[0].Wnode.BufferSize  = props_size
    ptr[0].Wnode.Flags       = WNODE_FLAG_TRACED_GUID
    ptr[0].LogFileMode       = EVENT_TRACE_REAL_TIME_MODE
    ptr[0].LoggerNameOffset  = ctypes.sizeof(EVENT_TRACE_PROPERTIES)

    name_offset = ctypes.sizeof(EVENT_TRACE_PROPERTIES)
    ctypes.memmove(
        ctypes.addressof(buf) + name_offset,
        name_bytes,
        len(name_bytes)
    )

    return buf, ptr


class NativeETWConsumer:

    def __init__(self, target_pids, event_callback):
        self.target_pids    = set(target_pids)
        self.user_callback  = event_callback
        self.session_name   = f"HeapTracerSession_{os.getpid()}"
        self.running        = False
        self.trace_handle   = None
        self.session_handle = None

        self._advapi32   = ctypes.windll.advapi32
        self._kernel32   = ctypes.windll.kernel32

        TRACEHANDLE = ctypes.c_uint64

        self._advapi32.StartTraceW.argtypes = [
            ctypes.POINTER(TRACEHANDLE),
            ctypes.c_wchar_p,
            ctypes.POINTER(EVENT_TRACE_PROPERTIES),
        ]
        self._advapi32.StartTraceW.restype = ctypes.c_ulong

        self._advapi32.ControlTraceW.argtypes = [
            TRACEHANDLE,
            ctypes.c_wchar_p,
            ctypes.POINTER(EVENT_TRACE_PROPERTIES),
            ctypes.c_ulong,
        ]
        self._advapi32.ControlTraceW.restype = ctypes.c_ulong

        self._advapi32.EnableTraceEx2.argtypes = [
            TRACEHANDLE,
            ctypes.POINTER(GUID),
            ctypes.c_ulong,
            ctypes.c_ubyte,
            ctypes.c_uint64,
            ctypes.c_uint64,
            ctypes.c_ulong,
            ctypes.c_void_p,
        ]
        self._advapi32.EnableTraceEx2.restype = ctypes.c_ulong

        self._advapi32.OpenTraceW.argtypes = [
            ctypes.POINTER(EVENT_TRACE_LOGFILE)
        ]
        self._advapi32.OpenTraceW.restype = TRACEHANDLE

        self._advapi32.ProcessTrace.argtypes = [
            ctypes.POINTER(TRACEHANDLE),
            ctypes.c_ulong,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        self._advapi32.ProcessTrace.restype = ctypes.c_ulong

        self._advapi32.CloseTrace.argtypes = [
            TRACEHANDLE
        ]
        self._advapi32.CloseTrace.restype = ctypes.c_ulong

        self._c_callback = EVENT_RECORD_CALLBACK(self._internal_callback)

    def add_target_pid(self, pid: int):
        self.target_pids.add(pid)

    def _internal_callback(self, record_ptr):
        if not record_ptr:
            return
        try:
            rec      = record_ptr[0]
            pid      = rec.EventHeader.ProcessId
            event_id = rec.EventHeader.EventDescriptor.Id
            data_len = rec.UserDataLength
            userdata = rec.UserData

            if pid not in self.target_pids:
                return
            if not userdata or data_len < 8:
                return

            val = ctypes.cast(userdata, ctypes.POINTER(ctypes.c_uint64))[0]

            # Heap Alloc: 1, 33 | Heap Free: 2, 34
            if event_id in (1, 33):
                self.user_callback(pid, "malloc", val)
            elif event_id in (2, 34):
                self.user_callback(pid, "free", val)

        except Exception as e:
            log.error("ETW callback error: %s", e)

    def _stop_existing_session(self):
        buf, ptr = _make_props_buffer(self.session_name)
        self._advapi32.ControlTraceW(
            0,
            self.session_name,
            ptr,
            EVENT_TRACE_CONTROL_STOP
        )

    def start(self):
        self.running = True
        self._stop_existing_session()

        buf, ptr = _make_props_buffer(self.session_name)

        session_guid = GUID("{11223344-5566-7788-9900-AABBCCDDEEFF}")
        ptr[0].Wnode.Guid = session_guid

        sess_handle = ctypes.c_uint64(0)
        res = self._advapi32.StartTraceW(
            ctypes.byref(sess_handle),
            self.session_name,
            ptr
        )

        if res != 0:
            log.error(
                "ETW StartTrace failed — error code %d. "
                "Run as Administrator.", res
            )
            return

        self.session_handle = sess_handle
        log.info("ETW session started successfully.")

        heap_guid = GUID(HEAP_PROVIDER_GUID)
        res = self._advapi32.EnableTraceEx2(
            sess_handle,
            ctypes.byref(heap_guid),
            1,
            5,
            0xFFFFFFFFFFFFFFFF,
            0,
            0,
            None
        )
        if res != 0:
            log.error("ETW EnableTraceEx2 failed — error code %d.", res)
            return

        logfile = EVENT_TRACE_LOGFILE()
        logfile.LoggerName       = self.session_name
        logfile.ProcessTraceMode = (
            PROCESS_TRACE_MODE_REAL_TIME | PROCESS_TRACE_MODE_EVENT_RECORD
        )
        logfile.EventCallback = self._c_callback

        t_handle = self._advapi32.OpenTraceW(ctypes.byref(logfile))

        if t_handle == INVALID_HANDLE_VALUE:
            err = self._kernel32.GetLastError()
            log.error("ETW OpenTraceW failed — error code %d.", err)
            return

        self.trace_handle = t_handle
        log.info("ETW trace handle opened. Starting ProcessTrace thread.")

        def _process_thread():
            handles = (ctypes.c_uint64 * 1)(t_handle)
            res = self._advapi32.ProcessTrace(handles, 1, None, None)
            if res != 0:
                log.error("ETW ProcessTrace exited with code %d.", res)

        threading.Thread(target=_process_thread, daemon=True, name="ETWProcessTrace").start()
        log.info("ETW session active — Microsoft-Windows-Heap provider running.")

    def stop(self):
        if self.session_handle:
            buf, ptr = _make_props_buffer(self.session_name)
            self._advapi32.ControlTraceW(
                self.session_handle,
                self.session_name,
                ptr,
                EVENT_TRACE_CONTROL_STOP
            )
            log.info("ETW session stopped.")
        if self.trace_handle:
            self._advapi32.CloseTrace(self.trace_handle)


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
                clean = proc_name.lower().replace(".exe", "")
                if clean in names_clean:
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
    pid: int, current_val_gb: float, max_system_ram_gb: float
):
    dynamic_upper_gb = min(
        max_system_ram_gb, max(0.128, current_val_gb * 1.5)
    )

    if dynamic_upper_gb <= 0.128:
        num_bins = 64
    elif dynamic_upper_gb <= 0.512:
        num_bins = 100
    elif dynamic_upper_gb <= 4.0:
        num_bins = 64
    elif dynamic_upper_gb <= 32.0:
        num_bins = 32
    else:
        num_bins = 16

    lower_gb = 0.0
    step     = (dynamic_upper_gb - lower_gb) / num_bins
    edges    = [lower_gb + i * step for i in range(num_bins + 1)]

    return (
        [ContiniousRandomVariable(f"bin_{i}", edges[i], edges[i + 1]) for i in range(num_bins)],
        num_bins,
    )


# ---------------------------------------------------------
# Single Metric Process Histogram Container
# ---------------------------------------------------------

class ProcessHistogram:

    def __init__(self, pid: int, metric_name: str, max_system_ram_kb: float):
        self.pid               = pid
        self.metric_name       = metric_name
        self.max_system_ram_gb = max_system_ram_kb / (1024.0 * 1024.0)
        self.samples           = []
        self.sample_count      = 0
        self.last_distribution = None

    def record(self, size_gb: float):
        self.samples.append(size_gb)

    def increment_sample(self):
        self.sample_count += 1

    def is_window_complete(self) -> bool:
        return self.sample_count >= SAMPLES_PER_WINDOW

    def build_distribution(self):
        if not self.samples:
            return None

        peak_val = max(self.samples)
        bins, num_bins = partition_to_continious_rv_custom_dynamic(
            self.pid, peak_val, self.max_system_ram_gb
        )

        frequencies = [0.0] * num_bins
        for val in self.samples:
            placed = False
            for i, crv in enumerate(bins):
                if crv.getLower() <= val < crv.getUpper():
                    frequencies[i] += 1.0
                    placed = True
                    break
            if not placed:
                frequencies[-1] += 1.0

        self.last_distribution = ContiniousDistribution(
            f"PID {self.pid} {self.metric_name} Distribution",
            bins,
            frequencies,
        )
        return self.last_distribution

    def reset_counts(self):
        self.samples      = []
        self.sample_count = 0


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
            mode       = "size"
            sub_tokens = tokens[3:]

            if sub_tokens and sub_tokens[0] == "--vel":
                mode       = "velocity"
                sub_tokens = sub_tokens[1:]
            elif sub_tokens and sub_tokens[0] == "--acc":
                mode       = "acceleration"
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

    BYTES_TO_GB = 1024.0 * 1024.0 * 1024.0

    def __init__(self, filt):
        self.filter        = filt
        self.previous_cpu  = {}
        self.data          = {}
        self.running       = True
        self.commands      = Commands()
        self.input_handler = AsyncConsoleInput()

        self.target_pids = filt.get_target_pids()

        self.fsm_state = {}
        self.ram_max   = {}

        # Dictionaries mapping PID -> {"x": deque(), "y": deque()}
        self.RAM_graph     = {}
        self.ALLOC_graph   = {}
        self.DEALLOC_graph = {}

        # Dictionaries mapping PID -> ProcessHistogram
        self.RAM_hist     = {}
        self.ALLOC_hist   = {}
        self.DEALLOC_hist = {}

        self._pid_lock = threading.Lock()

        self.live_plot_active = False
        self.live_plot_mode   = "size"
        self.live_target_pids = []
        self.fig              = None
        self.axes             = None
        self.active_db_fig    = None

        self.db_plot_requests   = queue.Queue()
        self.db_integration     = DATABASE_INTEGRATION(time_period_save_to_memory=DATABASE_REFRESH)
        self.last_db_flush_time = {}

        self.etw_consumer = NativeETWConsumer(
            target_pids=self.target_pids,
            event_callback=self._handle_heap_call_event
        )
        self.etw_consumer.start()

    def _init_pid(self, pid: int, name: str) -> bool:
        ram_max_kb = get_max_ram(pid)

        self.ram_max[pid] = ram_max_kb

        self.RAM_graph[pid]     = {"x": deque(maxlen=MAX_GRAPH_POINTS), "y": deque(maxlen=MAX_GRAPH_POINTS)}
        self.ALLOC_graph[pid]   = {"x": deque(maxlen=MAX_GRAPH_POINTS), "y": deque(maxlen=MAX_GRAPH_POINTS)}
        self.DEALLOC_graph[pid] = {"x": deque(maxlen=MAX_GRAPH_POINTS), "y": deque(maxlen=MAX_GRAPH_POINTS)}

        self.RAM_hist[pid]     = ProcessHistogram(pid, "RAM RSS", ram_max_kb)
        self.ALLOC_hist[pid]   = ProcessHistogram(pid, "RAM Allocation", ram_max_kb)
        self.DEALLOC_hist[pid] = ProcessHistogram(pid, "RAM Deallocation", ram_max_kb)

        self.fsm_state[pid] = STATE_HISTOGRAM
        self.last_db_flush_time[pid] = time.time()
        self.db_integration.ensure_process_exists(pid, burst_time=0.0)

        # Ensure ETW consumer includes this PID
        if hasattr(self, "etw_consumer"):
            self.etw_consumer.add_target_pid(pid)

        return True

    def _handle_heap_call_event(self, pid: int, event_type: str, size_bytes: int):
        now_ns  = time.time_ns()
        size_gb = size_bytes / self.BYTES_TO_GB

        with self._pid_lock:
            if pid not in self.ALLOC_graph:
                self._init_pid(pid, get_process_name(pid) or "Process")

            # STRICT ETW Heap Callback Ingestion
            if event_type == "malloc":
                self.ALLOC_graph[pid]["x"].append(float(now_ns))
                self.ALLOC_graph[pid]["y"].append(size_gb)
                self.ALLOC_hist[pid].record(size_gb)

            elif event_type == "free":
                self.DEALLOC_graph[pid]["x"].append(float(now_ns))
                self.DEALLOC_graph[pid]["y"].append(size_gb)
                self.DEALLOC_hist[pid].record(size_gb)

    def _on_close(self, event):
        self.live_plot_active = False
        self.fig              = None
        self.axes             = None
        self.active_db_fig    = None

        import matplotlib.pyplot as plt
        plt.close("all")
        gc.collect()

    def _finalize_window(self, pid: int):
        dist_ram     = self.RAM_hist[pid].build_distribution()
        dist_alloc   = self.ALLOC_hist[pid].build_distribution()
        dist_dealloc = self.DEALLOC_hist[pid].build_distribution()

        if dist_ram is None and dist_alloc is None and dist_dealloc is None:
            self.RAM_hist[pid].reset_counts()
            self.ALLOC_hist[pid].reset_counts()
            self.DEALLOC_hist[pid].reset_counts()
            self.fsm_state[pid] = STATE_HISTOGRAM
            return

        elapsed_since_flush = time.time() - self.last_db_flush_time.get(pid, 0.0)
        if elapsed_since_flush >= DATABASE_REFRESH:
            self.fsm_state[pid] = STATE_UPDATE_DATABASE
        else:
            self.RAM_hist[pid].reset_counts()
            self.ALLOC_hist[pid].reset_counts()
            self.DEALLOC_hist[pid].reset_counts()
            self.fsm_state[pid] = STATE_HISTOGRAM

    def _save_to_database(self, pid: int):
        dist = (
            self.RAM_hist[pid].last_distribution
            or self.ALLOC_hist[pid].last_distribution
            or self.DEALLOC_hist[pid].last_distribution
        )

        if dist is None:
            self.RAM_hist[pid].reset_counts()
            self.ALLOC_hist[pid].reset_counts()
            self.DEALLOC_hist[pid].reset_counts()
            self.last_db_flush_time[pid] = time.time()
            self.fsm_state[pid]          = STATE_HISTOGRAM
            return

        mean_gb     = dist.getMean()
        variance_gb = dist.getVariance()
        std_dev_gb  = dist.getStandardDeviation()
        mode_gb     = dist.getMode()

        self.db_integration.add_process_statistics(
            pid                = pid,
            snapshot_time      = datetime.now(),
            mean               = mean_gb,
            variance           = variance_gb,
            standard_deviation = std_dev_gb,
            mode               = mode_gb,
        )

        self.RAM_hist[pid].reset_counts()
        self.ALLOC_hist[pid].reset_counts()
        self.DEALLOC_hist[pid].reset_counts()
        self.last_db_flush_time[pid] = time.time()
        self.fsm_state[pid]          = STATE_HISTOGRAM

    def show_live_figures(self, target_pids: list, mode: str = "size"):
        import matplotlib.pyplot as plt

        self._on_close(None)
        plt.ion()
        self.live_target_pids = target_pids
        self.live_plot_mode   = mode

        with self._pid_lock:
            for pid in target_pids:
                if pid not in self.ALLOC_graph:
                    self._init_pid(pid, get_process_name(pid) or "Process")

        num_pids = len(target_pids)
        if num_pids == 0:
            return

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
                pids = self.live_target_pids

                for i, pid in enumerate(pids):
                    if i * 2 + 1 >= len(self.axes):
                        break

                    ax_alloc   = self.axes[i * 2]
                    ax_dealloc = self.axes[i * 2 + 1]

                    ax_alloc.clear()
                    ax_dealloc.clear()

                    proc_name = get_process_name(pid) or "Process"
                    label     = f"{proc_name} (PID {pid})"

                    def _plot_line(ax, graph_dict, title, color):
                        if not graph_dict or not graph_dict["x"]:
                            ax.set_title(f"{title} — (0 ETW Events)")
                            ax.grid(True)
                            return

                        xs = list(graph_dict["x"])
                        ys = list(graph_dict["y"])
                        t0 = xs[0]
                        times = [(t - t0) / 1e9 for t in xs]

                        ax.plot(times, ys, color=color, linewidth=1.5, marker=".", markersize=4)
                        ax.set_title(f"{title} ({len(xs)} ETW Events)")
                        ax.set_ylabel("Size (GB)")
                        ax.set_xlabel("Time (s)")
                        ax.grid(True)

                    _plot_line(
                        ax_alloc, 
                        self.ALLOC_graph.get(pid), 
                        f"{label} — ETW Heap Allocations (Malloc)", 
                        "royalblue"
                    )
                    _plot_line(
                        ax_dealloc, 
                        self.DEALLOC_graph.get(pid), 
                        f"{label} — ETW Heap Deallocations (Free)", 
                        "crimson"
                    )

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

                    with self._pid_lock:
                        if pid not in self.fsm_state:
                            self._init_pid(pid, snapshot["name"])

                    # Process RSS strictly mapped to total RAM
                    rss_gb = snapshot["rss_kb"] / (1024.0 * 1024.0)

                    self.RAM_graph[pid]["x"].append(float(current_time))
                    self.RAM_graph[pid]["y"].append(rss_gb)
                    self.RAM_hist[pid].record(rss_gb)

                    if self.fsm_state[pid] == STATE_HISTOGRAM:
                        self.RAM_hist[pid].increment_sample()
                        self.ALLOC_hist[pid].increment_sample()
                        self.DEALLOC_hist[pid].increment_sample()

                        if self.RAM_hist[pid].is_window_complete():
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
        if hasattr(self, "etw_consumer"):
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
        log.info("Monitoring stopped by user.")


if __name__ == "__main__":
    main()