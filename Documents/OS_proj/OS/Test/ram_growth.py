import time
import random

data = []

while True:
    # Allocate ~1 MB every second
    data.append(bytearray(1024 * 1024))

    print(f"Allocated: {len(data)} MB")

    time.sleep(1)