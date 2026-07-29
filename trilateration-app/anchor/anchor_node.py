#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
anchor_node.py — Tourist Safety System (Relay / Anchor)
=========================================================
Runs on EVERY fixed anchor Raspberry Pi (A1, A2, A3 …).

How it fits into the OFFLINE GPS system:
  1. Listens on LoRa (865 MHz) for tourist packets.
  2. Receives "PING:DEV001" or "SOS:DEV001" messages.
  3. Measures the RSSI of each received packet (hardware reports it).
  4. POSTs {anchor_id, rssi, tourist_id, sos_flag, timestamp} to the
     central server via HTTP.
  5. The server trilaterates X,Y and converts to GPS (lat/lng) from the
     anchor GPS reference points — ENTIRELY OFFLINE (no internet GPS).

Stagger delay:
  Each anchor waits a slightly different time before posting to avoid
  all three hitting the server simultaneously and causing race conditions.

Message filtering:
  The anchor ignores REPORT messages (from other anchors in a mesh setup)
  and only processes direct PING / SOS packets from tourist devices.

Setup (edit the section below for each anchor Pi):
  - ANCHOR_ID  : unique label per Pi (A1, A2, A3)
  - SERVER_URL : IP of the laptop/server running server.py
  - LORA_ADDR  : unique LoRa address per Pi (1, 2, 3)
"""

import sys
import time
import requests
import argparse
import platform
import os

# ============================================================
#  EDIT THESE FOR EACH ANCHOR PI
# ============================================================
ANCHOR_ID  = os.environ.get("ANCHOR_ID", "A1")   # A1, A2, A3 …
SERVER_URL = os.environ.get(
    "SERVER_URL", "http://192.168.0.69:5000/api/reading"
)
LORA_ADDR  = int(os.environ.get("LORA_ADDR", "1"))  # 1, 2, 3 …
# ============================================================

LORA_FREQ    = 865           # MHz — India ISM band
LORA_PORT    = "/dev/ttyS0"
POST_TIMEOUT = 3             # seconds before giving up on HTTP post

# Stagger delay so anchors don't all POST at the same moment
# A1=0.0s  A2=0.5s  A3=1.0s  (add more as needed)
STAGGER_DELAYS = {
    "A1": 0.0,
    "A2": 0.5,
    "A3": 1.0,
}

# ============================================================
#  PLATFORM DETECTION
# ============================================================
IS_RASPBERRY_PI = (
    platform.system() == "Linux"
    and ("aarch" in platform.machine() or "arm" in platform.machine())
)

# ============================================================
#  ANSI COLOUR CODES
# ============================================================
class Colors:
    CYAN    = "\033[96m"
    GREEN   = "\033[92m"
    YELLOW  = "\033[93m"
    RED     = "\033[91m"
    BOLD    = "\033[1m"
    DIM     = "\033[2m"
    RESET   = "\033[0m"


# ============================================================
#  HELPERS
# ============================================================
def parse_tourist_message(raw: str):
    """
    Parse a tourist broadcast message.

    Returns (tourist_id, msg_type) or (None, None) if not a tourist packet.

    Valid tourist formats:
        "PING:DEV001"
        "SOS:DEV001"

    Ignored (relay packets from other anchors):
        "REPORT:A2:DEV001:-65:PING"
    """
    msg = raw.strip().upper()

    # Reject relay REPORT messages
    if msg.startswith("REPORT"):
        return None, None

    for prefix in ("SOS", "PING"):
        if msg.startswith(prefix):
            parts = msg.split(":")
            tourist_id = parts[1].strip() if len(parts) >= 2 else "UNKNOWN"
            return tourist_id, prefix

    return None, None


def post_to_server(anchor_id: str, rssi: int, tourist_id: str,
                   sos_flag: bool, timestamp: float, server_url: str):
    """Send RSSI reading + safety metadata to the trilateration server."""
    payload = {
        "anchor_id"  : anchor_id,
        "rssi"       : rssi,
        "tourist_id" : tourist_id,
        "sos_flag"   : sos_flag,
        "timestamp"  : timestamp,
    }
    try:
        resp = requests.post(server_url, json=payload, timeout=POST_TIMEOUT)
        return resp.status_code == 200
    except requests.exceptions.RequestException as exc:
        print(f"  {Colors.YELLOW}[WARN] Server unreachable: {exc}{Colors.RESET}")
        return False


# ============================================================
#  MAIN
# ============================================================
def main(anchor_id: str, server_url: str, lora_addr: int):
    os.system("cls" if os.name == "nt" else "clear")
    print(f"\n{Colors.CYAN}{Colors.BOLD}")
    print("╔══════════════════════════════════════════════╗")
    print(f"║   📡  ANCHOR NODE : {anchor_id:<24} ║")
    print("║   Tourist Safety System — Offline GPS        ║")
    print("╚══════════════════════════════════════════════╝")
    print(f"{Colors.RESET}")
    print(f"  Anchor ID  : {Colors.GREEN}{Colors.BOLD}{anchor_id}{Colors.RESET}")
    print(f"  Frequency  : {Colors.CYAN}{LORA_FREQ} MHz (India ISM){Colors.RESET}")
    print(f"  Reporting  : {server_url}")
    print(f"  Stagger    : {STAGGER_DELAYS.get(anchor_id, 0.0)}s")
    print()

    # ----- Initialise LoRa hardware -----
    lora = None
    if IS_RASPBERRY_PI:
        try:
            from sx126x import sx126x
            lora = sx126x(
                serial_num=LORA_PORT,
                freq=LORA_FREQ,
                addr=lora_addr,
                power=22,
                rssi=True,           # REQUIRED — RSSI byte appended by module
                air_speed=2400,
                relay=False,
            )
            print(f"{Colors.GREEN}✓ LoRa hardware ready (RSSI enabled){Colors.RESET}\n")
        except Exception as exc:
            print(f"{Colors.RED}✗ LoRa init failed: {exc}{Colors.RESET}\n")
    else:
        print(f"{Colors.YELLOW}⚠  Simulation mode (not on Raspberry Pi){Colors.RESET}\n")

    # ----- Stats -----
    total_received = 0
    total_sent     = 0
    total_sos      = 0
    stagger        = STAGGER_DELAYS.get(anchor_id, 0.0)

    print(f"{Colors.DIM}Listening for tourist packets…{Colors.RESET}\n")

    try:
        while True:
            message, rssi = (lora.receive() if lora else (None, None))

            if message:
                tourist_id, msg_type = parse_tourist_message(message)

                if tourist_id is None:
                    # Not a tourist packet (e.g. a REPORT from another anchor)
                    time.sleep(0.05)
                    continue

                is_sos    = (msg_type == "SOS")
                timestamp = time.time()
                total_received += 1
                if is_sos:
                    total_sos += 1

                ts_str = time.strftime("%H:%M:%S", time.localtime(timestamp))

                if is_sos:
                    print(
                        f"{Colors.RED}{Colors.BOLD}"
                        f"🚨 [{ts_str}] SOS  | tourist={tourist_id} | RSSI={rssi} dBm"
                        f"{Colors.RESET}"
                    )
                else:
                    print(
                        f"{Colors.GREEN}"
                        f"📥 [{ts_str}] PING | tourist={tourist_id} | RSSI={rssi} dBm"
                        f"{Colors.RESET}"
                    )

                # Stagger delay to avoid simultaneous server hits from all anchors
                if stagger > 0:
                    time.sleep(stagger)

                ok = post_to_server(
                    anchor_id=anchor_id,
                    rssi=rssi,
                    tourist_id=tourist_id,
                    sos_flag=is_sos,
                    timestamp=timestamp,
                    server_url=server_url,
                )

                if ok:
                    total_sent += 1
                    status_sym = (
                        f"{Colors.RED}⬆ SOS posted{Colors.RESET}"
                        if is_sos
                        else f"{Colors.DIM}⬆ posted{Colors.RESET}"
                    )
                else:
                    status_sym = f"{Colors.YELLOW}⬆ FAILED (server down?){Colors.RESET}"

                print(
                    f"  {status_sym}  "
                    f"{Colors.DIM}[rx={total_received} tx={total_sent} sos={total_sos}]{Colors.RESET}"
                )
                print(f"{Colors.DIM}{'─' * 55}{Colors.RESET}")

            time.sleep(0.05)

    except KeyboardInterrupt:
        print(f"\n\n{Colors.YELLOW}[{anchor_id}] Shutting down.{Colors.RESET}")
        print(
            f"{Colors.DIM}Final: received={total_received} "
            f"sent={total_sent} sos={total_sos}{Colors.RESET}"
        )


# ============================================================
#  ENTRY POINT
# ============================================================
if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Anchor Node — Tourist Safety System (Offline GPS)"
    )
    parser.add_argument(
        "--anchor-id", type=str, default=ANCHOR_ID,
        help="Unique anchor label (A1, A2, A3 …)"
    )
    parser.add_argument(
        "--server", type=str, default=SERVER_URL,
        help="URL of the trilateration server /api/reading endpoint"
    )
    parser.add_argument(
        "--lora-addr", type=int, default=LORA_ADDR,
        help="LoRa module address for this anchor (1, 2, 3 …)"
    )
    args = parser.parse_args()

    main(
        anchor_id=args.anchor_id,
        server_url=args.server,
        lora_addr=args.lora_addr,
    )
