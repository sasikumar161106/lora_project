#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tourist_node.py — Tourist Safety System
========================================
Runs on the MOBILE (wearable) device carried by the tourist.

Core concept — OFFLINE GPS via LoRa:
  • No internet, no GPS chip required.
  • This device just broadcasts short LoRa packets every few seconds.
  • Three fixed anchor Raspberry Pis hear the packet and measure RSSI.
  • The server trilaterates the tourist's X,Y position from those RSSI
    readings, then converts X,Y → GPS (lat/lng) using the anchors'
    known GPS coordinates — 100% offline.

SOS:
  • A physical push-button on GPIO 17 triggers an SOS packet.
  • SOS packets contain the same device ID so the server can correlate.

Message format (broadcast over LoRa at 865 MHz):
  Normal : "PING:DEV001"
  SOS    : "SOS:DEV001"
"""

import sys
import time
import os
import argparse

# ============================================================
#  CONFIGURATION  (override via env-vars or CLI args)
# ============================================================
LORA_FREQ      = 865           # MHz — India ISM band (850-930 MHz, E22-900T22S)
LORA_PORT      = "/dev/ttyS0"  # Raspberry Pi hardware UART
LORA_ADDR      = 0             # Tourist node address
BROADCAST_ADDR = 65535         # 0xFFFF = all nearby LoRa nodes
PING_INTERVAL  = 2.0           # seconds between normal pings
SOS_PIN        = 17            # BCM GPIO pin for the SOS push-button

DEFAULT_DEVICE_ID = os.environ.get("DEVICE_ID", "DEV001")

# ============================================================
#  PLATFORM DETECTION
# ============================================================
import platform
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
#  TOURIST NODE CLASS
# ============================================================
class TouristNode:
    """
    Wearable LoRa device for a tourist.
    Broadcasts PING / SOS packets so fixed anchor nodes can
    measure RSSI and compute an offline GPS position.
    """

    def __init__(self, device_id: str, test_sos: bool = False,
                 interval: float = PING_INTERVAL):
        self.device_id = device_id.upper()
        self.test_sos  = test_sos
        self.interval  = interval
        self.ping_count = 0
        self.lora       = None
        self.sos_available = False

        self._init_hardware()

    # ----------------------------------------------------------
    def _init_hardware(self):
        """Initialise LoRa radio and SOS button (Raspberry Pi only)."""
        if IS_RASPBERRY_PI:
            try:
                from sx126x import sx126x
                import RPi.GPIO as GPIO

                self.lora = sx126x(
                    serial_num=LORA_PORT,
                    freq=LORA_FREQ,
                    addr=LORA_ADDR,
                    power=22,
                    rssi=False,       # tourist doesn't need to read RSSI
                    air_speed=2400,
                    relay=False,
                )
                print(f"{Colors.GREEN}✓ LoRa initialised at {LORA_FREQ} MHz{Colors.RESET}")

                # SOS push-button (active HIGH with pull-down)
                GPIO.setmode(GPIO.BCM)
                GPIO.setwarnings(False)
                GPIO.setup(SOS_PIN, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)
                self.sos_available = True
                print(f"{Colors.GREEN}✓ SOS button on GPIO {SOS_PIN}{Colors.RESET}")

            except Exception as exc:
                print(f"{Colors.YELLOW}⚠  Hardware init failed: {exc}{Colors.RESET}")
        else:
            print(f"{Colors.YELLOW}⚠  Not on Raspberry Pi — running in SIMULATION mode{Colors.RESET}")

    # ----------------------------------------------------------
    def _is_sos(self) -> bool:
        """Return True if SOS should be sent this cycle."""
        if self.test_sos:
            # Simulate SOS on pings 5, 6, 7 for easy testing
            return 5 <= self.ping_count <= 7

        if self.sos_available:
            try:
                import RPi.GPIO as GPIO
                return GPIO.input(SOS_PIN) == GPIO.HIGH
            except Exception:
                pass
        return False

    # ----------------------------------------------------------
    def _send(self, is_sos: bool):
        """
        Build and transmit a PING or SOS packet.

        NOTE: sx126x.send() already prepends the 3-byte broadcast header
        [0xFF, 0xFF, channel] internally, so we ONLY pass the payload string.
        The receiver strips that header automatically via the E22 hardware.
        """
        msg_type = "SOS" if is_sos else "PING"
        payload  = f"{msg_type}:{self.device_id}"

        if self.lora:
            # sx126x.send() adds [0xFF, 0xFF, offset_freq] header automatically
            self.lora.send(payload.encode("utf-8"))
        # (in simulation mode we just print)

        # Terminal output
        ts = time.strftime("%H:%M:%S")
        if is_sos:
            print(
                f"\r{Colors.RED}{Colors.BOLD}🚨 [{ts}] SOS #{self.ping_count}: {payload}{Colors.RESET}  ",
                end="\n" if self.test_sos else "",
                flush=True,
            )
        else:
            print(
                f"\r{Colors.GREEN}📡 [{ts}] Ping #{self.ping_count}: {payload}{Colors.RESET}  ",
                end="\n" if self.test_sos else "",
                flush=True,
            )

    # ----------------------------------------------------------
    def run(self):
        """Main broadcast loop."""
        os.system("cls" if os.name == "nt" else "clear")
        print(f"\n{Colors.CYAN}{Colors.BOLD}")
        print("╔══════════════════════════════════════════════╗")
        print("║   🚶  TOURIST SAFETY DEVICE — INDIA          ║")
        print("║   Offline GPS via LoRa RSSI Trilateration    ║")
        print("╚══════════════════════════════════════════════╝")
        print(f"{Colors.RESET}")
        print(f"  Device ID : {Colors.GREEN}{Colors.BOLD}{self.device_id}{Colors.RESET}")
        print(f"  Frequency : {Colors.CYAN}{LORA_FREQ} MHz (India ISM){Colors.RESET}")
        print(f"  Interval  : {self.interval}s")
        if self.test_sos:
            print(f"  {Colors.YELLOW}⚠  SOS TEST MODE — pings 5-7 will be SOS{Colors.RESET}")
        print(f"\n{Colors.GREEN}{Colors.BOLD}✓ Broadcasting — press Ctrl+C to stop{Colors.RESET}\n")

        try:
            while True:
                self.ping_count += 1
                sos = self._is_sos()
                self._send(sos)
                time.sleep(self.interval)

        except KeyboardInterrupt:
            print(f"\n\n{Colors.YELLOW}Stopped after {self.ping_count} pings.{Colors.RESET}")
        finally:
            self._cleanup()

    # ----------------------------------------------------------
    def _cleanup(self):
        if IS_RASPBERRY_PI:
            try:
                import RPi.GPIO as GPIO
                GPIO.cleanup()
            except Exception:
                pass


# ============================================================
#  ENTRY POINT
# ============================================================
def main():
    parser = argparse.ArgumentParser(
        description="Tourist Safety Device — LoRa Offline GPS System"
    )
    parser.add_argument(
        "--device-id", type=str, default=DEFAULT_DEVICE_ID,
        help="Unique device identifier (default: DEV001 or $DEVICE_ID env var)"
    )
    parser.add_argument(
        "--test-sos", action="store_true",
        help="Simulate SOS on pings 5-7 (for testing without hardware button)"
    )
    parser.add_argument(
        "--interval", type=float, default=PING_INTERVAL,
        help=f"Ping interval in seconds (default: {PING_INTERVAL})"
    )
    args = parser.parse_args()

    node = TouristNode(
        device_id=args.device_id,
        test_sos=args.test_sos,
        interval=args.interval,
    )
    node.run()


if __name__ == "__main__":
    main()
