import time
import random

chunks = []

while True:

    action = random.randint(0, 2)

    if action == 0:
        chunks.append(bytearray(random.randint(1, 20) * 1024 * 1024))
        print("Allocated memory")

    elif action == 1 and chunks:
        chunks.pop(random.randint(0, len(chunks)-1))
        print("Released memory")

    else:
        print("Idle")

    time.sleep(2)