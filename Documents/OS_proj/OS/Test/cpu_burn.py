import time
import math

x = 0

while True:
    for i in range(1000000):
        x += math.sqrt(i) * math.sin(i)

    time.sleep(0.1)