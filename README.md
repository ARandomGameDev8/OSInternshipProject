# Process Intelligence Monitor

A CLI-based process monitoring and behavioral analysis tool that answers a core question:

> **How can we know a program might crash, be dangerous, or be wasteful — before it actually happens?**

The answer is statistical behavioral analysis. Rather than waiting for a crash or a resource alert, this tool tracks how a process *behaves over time* and surfaces patterns that indicate trouble early.

---

## The Problem

Traditional task managers show you a snapshot — CPU and RAM usage right now. That's reactive. By the time RAM is critically high, the damage is often done.

This tool is proactive. It watches how memory consumption evolves across time, models its behavior statistically, and lets you read the story before the ending.

---

## How It Works

### Stage 1 — Live RAM Utilization Monitoring

The master program queries the OS directly for process information, collecting RAM samples continuously.

For each monitored process, the tool:

1. Collects RAM readings over a **30-second sampling window**
2. Extracts the **mode** (most frequent RAM value) across that window — a stable representation of typical usage within that period
3. Normalizes it against the process's maximum allowed RAM, producing a metric **rᵢ ∈ [0, 1]**
4. Plots **rᵢ over time** as a live updating graph

This gives you a first-hand view of intermediate RAM consumption as it evolves.

#### Reading the live graph

| Pattern | Interpretation |
|---|---|
| Periodic oscillation with regular downspikes | ✅ Healthy — allocating and freeing normally |
| Peak rᵢ staying around ≤ 0.8 | ✅ Healthy headroom |
| Monotonically increasing with infrequent downspikes | ⚠️ Likely memory leak |
| Sudden spike to rᵢ > 0.9 | ⚠️ Burst allocation — investigate |
| rᵢ approaching 1.0 | 🚨 Critical — near memory limit |

---

### Stage 2 — Behavioral Trend Analysis (Database Layer)

The second stage draws from a persistent database of historical snapshots to analyze how a process behaves **across different time periods**, not just right now.

From the database, the tool computes and plots:

#### Velocity of RAM (Δrᵢ / Δt)
Rate of change of RAM utilization across snapshots.

| Velocity pattern | Interpretation |
|---|---|
| Near-zero, fluctuating | ✅ Healthy — stable allocation |
| Constant positive | ⚠️ Steady leak — linear RAM growth over time |
| Linearly increasing | 🚨 Worsening leak — polynomial RAM growth |
| Exponentially increasing | 🚨🚨 Dangerous — possible intentional attack (e.g. zip bomb) |

#### Acceleration of RAM (Δ²rᵢ / Δt²)
Rate of change of velocity. Amplifies early warning signals — a rising acceleration precedes a visible RAM spike.

#### Standard Deviation
Measures **stability** of memory allocation rather than absolute consumption. A process with low mean RAM but high standard deviation has erratic, unpredictable allocation behavior — a different class of problem from a steady leak, and often harder to diagnose without this metric.

---

## Architecture

```
┌─────────────────────────────────────────┐
│               Master Program            │
│                                         │
│   ┌─────────────┐   ┌───────────────┐  │
│   │ Input Thread│   │  Main Thread  │  │
│   │  (CLI cmds) │   │  (sampling +  │  │
│   │             │   │   plotting)   │  │
│   └─────────────┘   └───────┬───────┘  │
│                             │           │
│                    ┌────────▼────────┐  │
│                    │   OS Queries    │  │
│                    │  (proc info)    │  │
│                    └────────┬────────┘  │
│                             │           │
│                    ┌────────▼────────┐  │
│                    │    Database     │  │
│                    │ (snapshot store │  │
│                    │  + trend data)  │  │
│                    └─────────────────┘  │
└─────────────────────────────────────────┘
```

- **Main thread** — handles process sampling and live matplotlib plotting
- **Input thread** — reads CLI commands without blocking the sampling loop
- **OS queries** — the master talks directly to the OS for process metrics (RAM, burst time, waiting time, turnaround time). No instrumentation of target processes required — they don't need to cooperate.
- **Database** — persists behavioral snapshots for trend analysis across sessions

---

## Key Design Decisions

**No IPC with target processes.** The tool queries the OS directly for process information. This means any process can be monitored without modification, injection, or cooperation — making it universally applicable.

**Mode over mean for rᵢ.** Using the mode of the 30-second sampling window rather than the mean produces a more stable, noise-resistant representation of typical usage within that cycle.

**Velocity and acceleration as early warning.** By the time raw RAM is dangerously high, reaction time is short. Velocity and acceleration surface the problem earlier — a constant positive velocity means a leak is already happening even if rᵢ looks acceptable.

**The graph is the warning system.** Rather than threshold-based alerts (which are noisy and context-blind), the statistical plots encode the full behavioral story. A developer reading the velocity graph can immediately distinguish a steady leak from an erratic allocation pattern from a burst spike — no alert system captures that nuance.

---

## Running the Tool

Monitor all processes on the system:
```bash
python -m Documents.OS_proj.OS.master
```

Monitor specific processes by PID:
```bash
python -m Documents.OS_proj.OS.master --pid <pid1> <pid2> <pid3> ...
```

Monitor specific processes by name:
```bash
python -m Documents.OS_proj.OS.master --name <name1> <name2> <name3> ...
```

---

## CLI Commands

### 1. PID Inspection & Filtering

Inspect specific process metrics for one PID or across all monitored processes.

| Command | Description |
|---|---|
| `pids <pid>` | Show all recorded snapshot metrics for a given PID |
| `pids <pid> --l` / `pids --l` | Display PID(s) |
| `pids <pid> --lr` / `--lrmb` / `--lrgb` | RAM usage in KB, MB, or GB |
| `pids <pid> --io` / `--iomb` / `--iogb` | Total read + write I/O in KB, MB, or GB |
| `pids <pid> --read` / `--readmb` / `--readgb` | Read I/O only in KB, MB, or GB |
| `pids <pid> --write` / `--writemb` / `--writegb` | Write I/O only in KB, MB, or GB |
| `pids <pid> --lt` | Active thread count for the process |
| `pids <pid> --lfd` | Open file descriptors (Linux) or handles (Windows) |

---

### 2. Live Visualizations & Monitoring

Control live GUI plots and real-time terminal streaming.

| Command | Description |
|---|---|
| `monitor --live figshow [pid\|name]` | Opens live Matplotlib GUI plotting rᵢ vs time_ns for up to 3 processes. Omit argument to plot all active processes. |
| `<command> --watch` | Enters continuous terminal streaming mode — clears and refreshes the console every cycle. Example: `pids 1234 --lrmb --watch` |
| `watch --stop` | Exits watch mode and returns to standard command prompt |

---

### 3. Database History Plotting

Query stored historical snapshots from MySQL and render time-series behavioral plots.

| Command | Description |
|---|---|
| `db plot --vel <pid>` | Plot RAM Velocity over time |
| `db plot --acc <pid>` | Plot RAM Acceleration over time |
| `db plot --var <pid>` | Plot RAM Variance over time (GB²) |
| `db plot --mean <pid>` | Plot Mean RAM over time (GB) |

---

### 4. System & Execution Controls

| Command | Description |
|---|---|
| `curr --tick` | Print CPU tick counts for all monitored processes |
| `curr --tns` | Print current timestamps in nanoseconds for all monitored processes |
| `monitor --stop` | Safely stop monitoring, close Matplotlib windows, close MySQL connection, and exit |

---

## Dependencies

```
matplotlib
mysql-connector-python
psutil
```

Install with:

```bash
pip install -r requirements.txt
```

---

## Project Context

Built as a two-week independent software engineering internship project at gotocme, a software engineering company, Lebanon branch. The core research question was exploring OS-level process observation and statistical behavioral modeling as a proactive alternative to reactive monitoring.
