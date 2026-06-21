#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
calibrate.py
============
Run this on ONE anchor Pi to measure real values for RSSI_AT_1M and
PATH_LOSS_EXPONENT — the two numbers that determine how accurate your
RSSI-to-distance conversion is. Using made-up/default values for these
is the #1 cause of inaccurate trilateration.

How to use:
  1. Place this anchor at a fixed spot.
  2. Place the tourist node (running tourist_node.py) at a KNOWN distance
     away, e.g. exactly 1 meter. Let it ping for ~15 seconds, then note
     the average RSSI this script reports.
  3. Move the tourist node to a few more known distances: 2m, 5m, 10m,
     20m (further is better — more spread gives a more reliable fit).
     Record the average RSSI at each distance.
  4. Enter all your (distance, rssi) pairs into the MEASUREMENTS list in
     fit_calibration.py and run it — it will print the RSSI_AT_1M and
     PATH_LOSS_EXPONENT values to paste into server.py.

Run: python3 calibrate.py
"""

import time
from sx126x import sx126x

LORA_FREQ = 868
LORA_PORT = "/dev/ttyS0"
LORA_ADDR = 1  # match whichever anchor you're calibrating with


def main():
    print("=" * 60)
    print("  RSSI Calibration — place tourist node at a KNOWN distance")
    print("  and let it ping for ~15s before moving it.")
    print("=" * 60)

    node = sx126x(
        serial_num=LORA_PORT,
        freq=LORA_FREQ,
        addr=LORA_ADDR,
        power=22,
        rssi=True,
        air_speed=2400,
        relay=False,
    )

    readings = []
    print("\nListening... Ctrl+C to stop and see summary.\n")

    try:
        while True:
            message, rssi = node.receive()
            if message is not None and rssi is not None:
                readings.append(rssi)
                avg = sum(readings) / len(readings)
                print(f"\rsamples={len(readings):4d}  latest={rssi:5.1f} dBm  "
                      f"running avg={avg:6.2f} dBm", end="", flush=True)
            time.sleep(0.05)
    except KeyboardInterrupt:
        print("\n\nSTOPPED.")
        if readings:
            avg = sum(readings) / len(readings)
            print(f"Samples collected: {len(readings)}")
            print(f"Average RSSI at this distance: {avg:.2f} dBm")
            print("\n>>> Record this as (distance_in_meters, {:.2f}) <<<".format(avg))
            print("    Then move the tourist node to the next known distance")
            print("    and run this script again.")
        else:
            print("No readings captured — check the tourist node is pinging "
                  "and the anchor address/frequency match.")


if __name__ == "__main__":
    main()
