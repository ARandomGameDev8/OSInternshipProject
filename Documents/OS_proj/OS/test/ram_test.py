import time

print("RAM test started")

# Allocate ~500 MB
data = []

for i in range(500):
    data.append(bytearray(1024 * 1024))


print("Allocated 500 MB")

while True:
    time.sleep(1)