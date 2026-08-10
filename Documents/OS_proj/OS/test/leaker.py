import time
import sys

print("=== LEAKER PROCESS ACTIVE ===", flush=True)

# List to hold references so memory is not freed by Garbage Collection
memory_store = []

iteration = 0
while True:
    iteration += 1
    
    # Allocate 10 MB incrementally using bytearrays (10 blocks of 1 MB)
    for _ in range(10):
        memory_store.append(bytearray(1024 * 1024))  # 1 MB chunk
        
    current_mb = len(memory_store)
    print(f"[{iteration}s] Total Allocated RAM: {current_mb} MB", flush=True)
    
    time.sleep(1)