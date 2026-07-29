#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
relay_node.py — Tourist Safety System (Relay Node)
====================================================
Runs on RELAY anchor Raspberry Pis (A2, A3, …) in the NO-INTERNET zone.

Role in the offline GPS system:
  • Listens on LoRa 865 MHz for tourist PING / SOS packets.
  • Measures the RSSI of each received packet.
  • Re-broadcasts a REPORT packet on LoRa so the MASTER node (A1)
    can collect this anchor's RSSI reading without any Wi-Fi.
  • Does NOT talk to the internet at all. 100% LoRa-only.

Message flow:
  Tourist  ──[PING:DEV001]──►  Relay A2
  Relay A2 ──[REPORT:A2:DEV001:-72:PING]──►  Master A1 (via LoRa)

Stagger delay (prevents RF collision between relays):
  A2 → waits 0.5 s before broadcasting REPORT
  A3 → waits 1.0 s before broadcasting REPORT
  (The master always listens and collects what it gets.)

Setup — edit the section below for each relay Pi:
  RELAY_ID  = "A2"  or  "A3"
  LORA_ADDR = 2     or   3
"""

import sys
import os
import time
import argparse
import platform

# ============================================================
#  EDIT THESE FOR EACH RELAY PI
# ============================================================
RELAY_ID  = os.environ.get("RELAY_ID",  "A2")   # "A2" or "A3"
LORA_ADDR = int(os.environ.get("LORA_ADDR", "2"))  # 2 or 3
# ============================================================

LORA_FREQ  = 865           # MHz — India ISM band
LORA_PORT  = "/dev/ttyS0"

# Stagger delays per relay ID so REPORTs don't collide on the air
STAGGER = {
    "A2": 0.5,
    "A3": 1.0,
    "A4": 1.5,   # room for a 4th relay if needed
    "A5": 2.0,
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
def parse_tourist_packet(raw: str):
    """
    Parse an incoming LoRa packet.

    Tourist formats we accept:
      "PING:DEV001"   →  returns ("DEV001", "PING")
      "SOS:DEV001"    →  returns ("DEV001", "SOS")

    Ignored (relay REPORT from another node):
      "REPORT:A2:DEV001:-72:PING"  →  returns (None, None)
    """
    msg = raw.strip().upper()

    # Ignore REPORT packets — those come from other relays, not tourists
    if msg.startswith("REPORT"):
        return None, None

    for prefix in ("SOS", "PING"):
        if msg.startswith(prefix):
            parts = msg.split(":")
            tourist_id = parts[1].strip() if len(parts) >= 2 else "UNKNOWN"
            return tourist_id, prefix

    return None, None


def build_report(relay_id: str, tourist_id: str,
                  rssi: int, msg_type: str) -> str:
    """
    Build the REPORT string this relay broadcasts to the master.
    Format: "REPORT:A2:DEV001:-72:PING"
    """
    return f"REPORT:{relay_id}:{tourist_id}:{rssi}:{msg_type}"


# ============================================================
#  MAIN
# ============================================================
def main(relay_id: str, lora_addr: int):
    os.system("cls" if os.name == "nt" else "clear")
    print(f"\n{Colors.CYAN}{Colors.BOLD}")
    print("╔══════════════════════════════════════════════╗")
    print(f"║   📡  RELAY NODE : {relay_id:<25} ║")
    print("║   Tourist Safety System — No Internet Zone   ║")
    print("╚══════════════════════════════════════════════╝")
    print(f"{Colors.RESET}")
    print(f"  Relay ID  : {Colors.GREEN}{Colors.BOLD}{relay_id}{Colors.RESET}")
    print(f"  Frequency : {Colors.CYAN}{LORA_FREQ} MHz (India ISM){Colors.RESET}")
    print(f"  LoRa Addr : {lora_addr}")
    print(f"  Stagger   : {STAGGER.get(relay_id, 0.5)}s")
    print(f"\n  {Colors.YELLOW}⚠  No internet — LoRa REPORT only{Colors.RESET}")
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
                rssi=True,    # REQUIRED — trailing RSSI byte from E22 module
                air_speed=2400,
                relay=False,
            )
            print(f"{Colors.GREEN}✓ LoRa hardware ready{Colors.RESET}\n")
        except Exception as exc:
            print(f"{Colors.RED}✗ LoRa init failed: {exc}{Colors.RESET}\n")
    else:
        print(f"{Colors.YELLOW}⚠  Simulation mode (not on Raspberry Pi){Colors.RESET}\n")

    print(f"{Colors.DIM}Listening for tourist packets…{Colors.RESET}\n")

    # ----- Stats -----
    total_rx      = 0
    total_reports = 0
    total_sos     = 0
    stagger_delay = STAGGER.get(relay_id, 0.5)

    try:
        while True:
            # ─── Receive ───────────────────────────────────────────
            message, rssi = (lora.receive() if lora else (None, None))

            if not message:
                time.sleep(0.05)
                continue

            tourist_id, msg_type = parse_tourist_packet(message)

            if tourist_id is None:
                # Not a tourist packet (likely another relay's REPORT)
                time.sleep(0.05)
                continue

            is_sos = (msg_type == "SOS")
            total_rx += 1
            if is_sos:
                total_sos += 1

            ts = time.strftime("%H:%M:%S")

            if is_sos:
                print(
                    f"{Colors.RED}{Colors.BOLD}"
                    f"🚨 [{ts}] SOS  rx | tourist={tourist_id} | RSSI={rssi} dBm"
                    f"{Colors.RESET}"
                )
            else:
                print(
                    f"{Colors.GREEN}"
                    f"📥 [{ts}] PING rx | tourist={tourist_id} | RSSI={rssi} dBm"
                    f"{Colors.RESET}"
                )

            # ─── Stagger delay (avoid RF collision with other relays) ──
            print(
                f"  {Colors.DIM}Waiting {stagger_delay}s before REPORT…{Colors.RESET}"
            )
            time.sleep(stagger_delay)

            # ─── Build and broadcast REPORT via LoRa ──────────────────
            report = build_report(relay_id, tourist_id, rssi, msg_type)

            if lora:
                # sx126x.send() adds [0xFF,0xFF,channel] broadcast header
                lora.send(report.encode("utf-8"))
                total_reports += 1
                sos_tag = f" {Colors.RED}[SOS]{Colors.RESET}" if is_sos else ""
                print(
                    f"  {Colors.CYAN}📤 REPORT sent → {report}{sos_tag}{Colors.RESET}"
                )
            else:
                print(
                    f"  {Colors.YELLOW}📤 [SIM] Would send → {report}{Colors.RESET}"
                )

            print(
                f"  {Colors.DIM}Stats: rx={total_rx} "
                f"reports={total_reports} sos={total_sos}{Colors.RESET}"
            )
            print(f"{Colors.DIM}{'─' * 52}{Colors.RESET}")

    except KeyboardInterrupt:
        print(f"\n\n{Colors.YELLOW}[{relay_id}] Shutting down…{Colors.RESET}")
        print(
            f"{Colors.DIM}Final: rx={total_rx} "
            f"reports={total_reports} sos={total_sos}{Colors.RESET}"
        )


# ============================================================
#  ENTRY POINT
# ============================================================
if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Relay Node — Tourist Safety System (No-Internet LoRa Relay)"
    )
    parser.add_argument(
        "--relay-id", type=str, default=RELAY_ID,
        help="Relay anchor ID (A2, A3 …) — must be unique per relay Pi"
    )
    parser.add_argument(
        "--lora-addr", type=int, default=LORA_ADDR,
        help="LoRa module address for this relay (2, 3 …)"
    )
    args = parser.parse_args()

    main(relay_id=args.relay_id, lora_addr=args.lora_addr)
