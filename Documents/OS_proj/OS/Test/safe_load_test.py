#!/usr/bin/env python3

"""
safe_load_test.py — Safe, continuous 1-hour load generator for process monitoring.
Executes memory allocations, sorting algorithms, text processing, and math tasks 
in a controlled loop. Memory is explicitly freed after every task cycle.
"""

import os
import sys
import time
import math
import random
import gc

# System limits
RUN_DURATION_HOURS = 1.0
RUN_DURATION_SECONDS = RUN_DURATION_HOURS * 3600

# Cap max allocation per task safely (e.g., ~250 MB max)
MAX_ALLOCATION_MB = 250


# =========================================================
# TASK 1: Dynamic List Buffer & String Operations
# =========================================================
class StringProcessingTask:
    """Creates a temporary list of structured strings, processes them, and releases them."""

    def __init__(self, target_mb: int = 150):
        self.target_mb = min(target_mb, MAX_ALLOCATION_MB)

    def run(self):
        print(f"[{time.strftime('%H:%M:%S')}] [Task 1] String Allocation (~{self.target_mb} MB)...")
        
        # ~20,000 strings = ~1 MB of memory
        element_count = self.target_mb * 20_000
        
        # 1. Allocate
        data_buffer = [
            f"Process_Data_Record_ID_{i:08d}_Value_{i * 1.618:012.4f}"
            for i in range(element_count)
        ]
        
        # 2. Process data
        sample_chars = "".join(data_buffer[i][0] for i in range(0, min(1000, len(data_buffer)), 10))
        _ = sample_chars.upper()
        
        time.sleep(1.5)  # Hold memory briefly so monitor captures the peak
        
        # 3. Explicit Cleanup
        del data_buffer
        gc.collect()
        print(f"[{time.strftime('%H:%M:%S')}] [Task 1] String Buffer Freed.")


# =========================================================
# TASK 2: Numerical Matrix & Vector Operations
# =========================================================
class VectorMathTask:
    """Performs element-wise operations on numeric lists."""

    def __init__(self, size_elements: int = 3_000_000):
        self.size_elements = size_elements

    def run(self):
        print(f"[{time.strftime('%H:%M:%S')}] [Task 2] Vector Mathematics (Allocating float array)...")
        
        # 1. Allocate floats (~24 MB per 1M floats in standard Python list)
        vec_a = [random.uniform(1.0, 100.0) for _ in range(self.size_elements)]
        vec_b = [random.uniform(1.0, 100.0) for _ in range(self.size_elements)]
        
        # 2. Transform & Compute
        vec_c = [math.sin(a) * math.cos(b) for a, b in zip(vec_a[:100_000], vec_b[:100_000])]
        _ = sum(vec_c)
        
        time.sleep(2.0)
        
        # 3. Explicit Cleanup
        del vec_a, vec_b, vec_c
        gc.collect()
        print(f"[{time.strftime('%H:%M:%S')}] [Task 2] Vector Arrays Freed.")


# =========================================================
# TASK 3: Algorithmic Sorting & Dictionary Mapping
# =========================================================
class SortingAndLookupTask:
    """Tests CPU and transient allocations via sorting and hash table lookups."""

    def __init__(self, item_count: int = 400_000):
        self.item_count = item_count

    def run(self):
        print(f"[{time.strftime('%H:%M:%S')}] [Task 3] Sorting & Hash Lookup (Items: {self.item_count:,})...")
        
        # 1. Allocate random integer list
        raw_items = [random.randint(1, 10_000_000) for _ in range(self.item_count)]
        
        # 2. Algorithmic Sort (creates internal Timsort working memory)
        raw_items.sort()
        
        # 3. Build Dictionary Lookup Map
        lookup_dict = {item: f"Mapped_Value_{item}" for item in raw_items[::100]}
        _ = lookup_dict.get(raw_items[0], None)
        
        time.sleep(1.5)
        
        # 4. Explicit Cleanup
        del raw_items, lookup_dict
        gc.collect()
        print(f"[{time.strftime('%H:%M:%S')}] [Task 3] Sorting Data Freed.")


# =========================================================
# TASK 4: Safe Byte Buffer Burst Allocation
# =========================================================
class ByteBufferBurstTask:
    """Allocates a raw byte array to simulate sharp memory spikes."""

    def __init__(self, burst_mb: int = 200):
        self.burst_mb = min(burst_mb, MAX_ALLOCATION_MB)

    def run(self):
        print(f"[{time.strftime('%H:%M:%S')}] [Task 4] Byte Array Burst ({self.burst_mb} MB)...")
        
        bytes_to_allocate = self.burst_mb * 1024 * 1024
        
        # 1. Direct bytearray allocation
        raw_bytes = bytearray(bytes_to_allocate)
        
        # Touch pages sequentially to force OS physical commit
        step = 4096  # 4 KB page size
        for i in range(0, min(len(raw_bytes), 20_000_000), step):
            raw_bytes[i] = i % 256
            
        time.sleep(2.0)
        
        # 2. Explicit Cleanup
        del raw_bytes
        gc.collect()
        print(f"[{time.strftime('%H:%M:%S')}] [Task 4] Byte Array Freed.")


# =========================================================
# MAIN EXECUTION CONTROLLER
# =========================================================
def main():
    start_time = time.time()
    end_time = start_time + RUN_DURATION_SECONDS
    
    print("=" * 65)
    print(f" SAFE LOAD GENERATOR STARTED")
    print(f" Target Duration : {RUN_DURATION_HOURS} Hour ({RUN_DURATION_SECONDS:.0f} seconds)")
    print(f" Process PID      : {os.getpid()}")
    print("=" * 65)
    print(" Press CTRL+C at any time to safely terminate prematurely.\n")

    # Instantiate available safe tasks
    tasks = [
        StringProcessingTask(target_mb=120),
        VectorMathTask(size_elements=2_500_000),
        SortingAndLookupTask(item_count=350_000),
        ByteBufferBurstTask(burst_mb=180),
    ]

    cycle_count = 0

    try:
        while time.time() < end_time:
            cycle_count += 1
            remaining_seconds = end_time - time.time()
            remaining_minutes = remaining_seconds / 60.0
            
            print(f"\n--- [Cycle {cycle_count}] Time Remaining: {remaining_minutes:.1f} mins ---")
            
            # Shuffle tasks every cycle for dynamic load variety
            random.shuffle(tasks)
            
            for task in tasks:
                if time.time() >= end_time:
                    break
                
                # Execute task
                task.run()
                
                # Sleep briefly between tasks to allow memory velocity/acceleration
                # to settle back to zero baseline on the monitor
                time.sleep(2.0)

    except KeyboardInterrupt:
        print("\n[!] Execution manually stopped by user.")

    total_run_mins = (time.time() - start_time) / 60.0
    print("\n" + "=" * 65)
    print(f" LOAD GENERATOR COMPLETED")
    print(f" Total Runtime    : {total_run_mins:.2f} minutes")
    print(f" Completed Cycles : {cycle_count}")
    print("=" * 65)


if __name__ == "__main__":
    main()