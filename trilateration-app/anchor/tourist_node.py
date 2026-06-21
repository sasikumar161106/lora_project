#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tourist_node.py
================
Runs on the MOBILE node — the device whose location you want to track.

It just broadcasts a small ping packet repeatedly. Every anchor Pi within
range hears it, measures the RSSI of that packet, and reports it to the
central server. The server then trilaterates the tourist's position from
all the anchors' RSSI readings.

This is a cleaned-up version of the original tourist.py.
"""

import sys
import time

from sx126x import sx126x

LORA_FREQ = 868
LORA_PORT = "/dev/ttyS0"
LORA_ADDR = 0          # tourist node's own address
BROADCAST_ADDR = 65535  # special address = "all nearby LoRa modules"
PING_INTERVAL = 1.0     # seconds between pings


def get_broadcast_header(node):
    offset_frequence = LORA_FREQ - (850 if LORA_FREQ > 850 else 410)
    header = (
        bytes([BROADCAST_ADDR >> 8]) + bytes([BROADCAST_ADDR & 0xff]) +
        bytes([offset_frequence]) +
        bytes([node.addr >> 8]) + bytes([node.addr & 0xff]) +
        bytes([node.offset_freq])
    )
    return header


def main():
    print("=" * 60)
    print("  TOURIST NODE: Broadcast Mode")
    print("=" * 60)

    try:
        node = sx126x(
            serial_num=LORA_PORT,
            freq=LORA_FREQ,
            addr=LORA_ADDR,
            power=22,
            rssi=True,
            air_speed=2400,
            relay=False,
        )
    except Exception as e:
        print(f"ERROR: could not initialize LoRa module: {e}")
        sys.exit(1)

    header = get_broadcast_header(node)
    print("Broadcasting PING every", PING_INTERVAL, "seconds. Ctrl+C to stop.\n")

    try:
        while True:
            packet = header + b"PING"
            node.send(packet)
            print(f"[{time.strftime('%H:%M:%S')}] PING sent")
            time.sleep(PING_INTERVAL)
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
