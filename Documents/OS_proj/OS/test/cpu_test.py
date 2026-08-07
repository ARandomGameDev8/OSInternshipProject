import time
import math

print("CPU test started")

x = 0

while True:
    for i in range(1000000):
        x += math.sqrt(i)

    time.sleep(0.1)