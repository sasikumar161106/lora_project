#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
anchor_node.py
==============
Runs on EVERY fixed anchor Raspberry Pi.

What it does:
  1. Listens continuously on the LoRa module for packets from the mobile
     "tourist" node (see tourist_node.py).
  2. Captures the real RSSI of each received packet (using the fixed
     sx126x.py driver in this folder).
  3. Sends {anchor_id, rssi, timestamp} to the central server over HTTP,
     so the server can run trilateration using all anchors' latest readings.

Setup before running:
  1. Edit ANCHOR_ID below to a unique name for this anchor (e.g. "A1").
  2. Edit SERVER_URL to the Render URL (e.g. https://trilateration-server.onrender.com/api/reading)
     (find it on your Render dashboard once deployed).
  3. Make sure this anchor's LoRa addr (below) is different from the
     other anchors' addr and from the tourist node's addr.
  4. Run: python3 anchor_node.py
"""

import sys
import time
import requests

from sx126x import sx126x

# ============== EDIT THESE FOR EACH ANCHOR ==============
ANCHOR_ID = "A1"                                  # unique per anchor: A1, A2, A3...
# SERVER_URL should point to the deployed Render URL (e.g. https://trilateration-server.onrender.com/api/reading)
# You can find the live URL on your Render dashboard service page.
SERVER_URL = "http://192.168.1.50:5000/api/reading"  # Replace with actual Render URL once deployed
LORA_ADDR = 1                                     # unique per anchor: 1, 2, 3...
# ==========================================================

LORA_FREQ = 868
LORA_PORT = "/dev/ttyS0"
POST_TIMEOUT = 2  # seconds, don't let a slow/offline server block listening


def main():
    print("=" * 60)
    print(f"  ANCHOR NODE: {ANCHOR_ID}")
    print(f"  Reporting to: {SERVER_URL}")
    print("=" * 60)

    try:
        node = sx126x(
            serial_num=LORA_PORT,
            freq=LORA_FREQ,
            addr=LORA_ADDR,
            power=22,
            rssi=True,          # REQUIRED so the trailing RSSI byte is sent
            air_speed=2400,
            relay=False,
        )
    except Exception as e:
        print(f"ERROR: could not initialize LoRa module: {e}")
        sys.exit(1)

    print("LoRa module ready. Listening for tourist node packets...\n")

    while True:
        try:
            message, rssi = node.receive()

            if message is not None:
                timestamp = time.time()
                print(f"[{time.strftime('%H:%M:%S')}] msg='{message.strip()}'  rssi={rssi} dBm")

                payload = {
                    "anchor_id": ANCHOR_ID,
                    "rssi": rssi,
                    "message": message.strip(),
                    "timestamp": timestamp,
                }

                try:
                    requests.post(SERVER_URL, json=payload, timeout=POST_TIMEOUT)
                except requests.exceptions.RequestException as e:
                    print(f"  [WARN] could not reach server: {e}")

        except KeyboardInterrupt:
            print("\nStopped.")
            break
        except Exception as e:
            print(f"  [ERROR] {e}")

        time.sleep(0.05)


if __name__ == "__main__":
    main()
