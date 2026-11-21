import socket
import time
import random

# drone controller destination
CONTROLLER_IP = "10.60.1.20"
CONTROLLER_PORT = 5000

# create a UDP socket
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

print(f"Sending simulated drone traffic to "
      f"{CONTROLLER_IP}:{CONTROLLER_PORT}")

try:
    while True:
        # generate simulated drone telemetry
        altitude = round(random.uniform(80, 120), 1)
        speed = round(random.uniform(10, 20), 1)
        battery = random.randint(60, 100)
        heading = random.randint(0, 359)

        message = (
            f"altitude={altitude} "
            f"speed={speed} "
            f"battery={battery} "
            f"heading={heading} "
            f"status=OK"
        )

        # send telemetry to the drone controller
        sock.sendto(
            message.encode(),
            (CONTROLLER_IP, CONTROLLER_PORT)
        )

        print(f"Sent: {message}")

        # add variation so normal traffic is not perfectly periodic
        time.sleep(random.uniform(1.5, 4.0))

except KeyboardInterrupt:
    print("\nDrone traffic simulation stopped.")

finally:
    sock.close()