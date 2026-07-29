#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
master_node.py — Tourist Safety System (Master Node / Gateway)
===============================================================
Runs on MASTER anchor Raspberry Pi (A1) — sits at the BOUNDARY between
the no-internet zone (LoRa) and the internet zone (Wi-Fi → server).

Role in the offline GPS system:
  ┌────────────────────────────────────────────────────────────┐
  │   NO INTERNET ZONE (forest / mountain / remote area)       │
  │                                                            │
  │  Tourist  ──[PING:DEV001]──► Master A1 (this Pi)           │
  │           ──[PING:DEV001]──► Relay A2 ──[REPORT]──►┐      │
  │           ──[PING:DEV001]──► Relay A3 ──[REPORT]──►┤      │
  │                                                    ↓       │
  │                                            Master A1       │
  │                                         collects all 3     │
  │                              RSSI readings via LoRa        │
  └────────────────────────────────────────────────────────────┘
                            │ Wi-Fi (only this Pi needs it)
                            ▼
              ┌─────────────────────────┐
              │   SERVER (internet zone)│
              │   Trilaterates X,Y      │
              │   Computes offline GPS  │
              │   Runs dashboard        │
              └─────────────────────────┘

What this node does:
  1. Receives "PING:DEV001" / "SOS:DEV001" directly from tourist → MASTER RSSI
  2. Receives "REPORT:A2:DEV001:-72:PING" from Relay A2 → A2 RSSI
  3. Receives "REPORT:A3:DEV001:-68:PING" from Relay A3 → A3 RSSI
  4. Groups readings from the same tourist ping into a SESSION
  5. When session completes (all 3 received) OR times out (3 s):
       → POSTs each anchor's reading to the server as separate HTTP calls
  6. Server receives all 3, trilaterates, computes offline GPS, updates dashboard
  7. If server unreachable → buffer readings in memory, retry on next cycle

Offline buffering:
  Up to 100 sessions are buffered in memory. When the server becomes
  reachable again, buffered data is flushed automatically.

Setup — edit the section below:
  SERVER_URL = URL of your server's /api/reading endpoint
  RELAY_IDS  = IDs of relay nodes that will send REPORTs to this master
"""

import os
import sys
import time
import argparse
import platform
import threading
import requests
from collections import defaultdict

# ============================================================
#  CONFIGURATION
# ============================================================
SERVER_URL = os.environ.get(
    "SERVER_URL", "http://192.168.0.69:5000/api/reading"
)
HEARTBEAT_URL = SERVER_URL.replace("/api/reading", "/api/heartbeat")

LORA_FREQ  = 865           # MHz — India ISM band
LORA_PORT  = "/dev/ttyS0"
LORA_ADDR  = 1             # Master node address (fixed)

MASTER_ID  = "A1"          # Master's own anchor ID

# IDs of relay nodes we expect REPORT packets from
RELAY_IDS  = ["A2", "A3"]

# A session collects {MASTER, A2, A3} readings for one tourist ping.
# If we don't receive all within this window, send what we have.
SESSION_TIMEOUT = 4.0      # seconds

# HTTP
POST_TIMEOUT  = 3          # seconds per request
HEARTBEAT_INT = 60         # seconds between heartbeats

# Offline buffer
MAX_BUFFER = 100           # maximum buffered sessions

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
#  PARSING
# ============================================================
def parse_message(raw: str):
    """
    Parse any incoming LoRa packet.

    Returns (source, tourist_id, rssi_or_none, msg_type, is_report)

    Direct tourist packet:
      "PING:DEV001"  → (MASTER_ID, "DEV001", None, "PING", False)
      "SOS:DEV001"   → (MASTER_ID, "DEV001", None, "SOS",  False)

    Relay REPORT packet (RSSI embedded in message):
      "REPORT:A2:DEV001:-72:PING" → ("A2", "DEV001", -72, "PING", True)
      "REPORT:A3:DEV001:-68:SOS"  → ("A3", "DEV001", -68, "SOS",  True)

    Unknown / garbage → (None, None, None, None, False)
    """
    msg = raw.strip().upper()

    # ─── REPORT from a relay ─────────────────────────────────────────
    if msg.startswith("REPORT"):
        try:
            # Format: REPORT:ANCHOR_ID:TOURIST_ID:RSSI:MSG_TYPE
            parts = msg.split(":")
            if len(parts) >= 5:
                anchor_id  = parts[1].strip()
                tourist_id = parts[2].strip()
                rssi_val   = int(parts[3].strip())
                msg_type   = parts[4].strip()
                return anchor_id, tourist_id, rssi_val, msg_type, True
        except Exception:
            pass
        return None, None, None, None, False

    # ─── Direct tourist PING / SOS ───────────────────────────────────
    for prefix in ("SOS", "PING"):
        if msg.startswith(prefix):
            parts = msg.split(":")
            tourist_id = parts[1].strip() if len(parts) >= 2 else "UNKNOWN"
            return MASTER_ID, tourist_id, None, prefix, False

    return None, None, None, None, False


# ============================================================
#  SESSION — one round of readings for a single tourist ping
# ============================================================
class Session:
    """
    Groups {A1, A2, A3} RSSI readings from a single tourist broadcast.
    """
    REQUIRED = {MASTER_ID} | set(RELAY_IDS)   # {"A1", "A2", "A3"}

    def __init__(self, tourist_id: str, sos_flag: bool):
        self.tourist_id = tourist_id
        self.sos_flag   = sos_flag
        self.readings   = {}          # anchor_id → rssi
        self.start_time = time.time()

    def add(self, anchor_id: str, rssi: int, sos_flag: bool):
        self.readings[anchor_id] = rssi
        if sos_flag:
            self.sos_flag = True

    @property
    def is_complete(self) -> bool:
        return self.REQUIRED.issubset(self.readings.keys())

    @property
    def is_timed_out(self) -> bool:
        return time.time() - self.start_time > SESSION_TIMEOUT

    @property
    def has_any(self) -> bool:
        return len(self.readings) > 0


# ============================================================
#  MASTER NODE CLASS
# ============================================================
class MasterNode:
    def __init__(self, server_url: str):
        self.server_url  = server_url
        self.lora        = None
        self.session     = None        # current active session
        self.offline_buf = []          # list of {anchor_id, rssi, tourist_id, sos_flag, ts}
        self._buf_lock   = threading.Lock()

        # Stats
        self.total_sessions = 0
        self.total_sent     = 0
        self.total_sos      = 0
        self.total_buffered = 0

        self._init_lora()

    # ------------------------------------------------------------------
    def _init_lora(self):
        if IS_RASPBERRY_PI:
            try:
                from sx126x import sx126x
                self.lora = sx126x(
                    serial_num=LORA_PORT,
                    freq=LORA_FREQ,
                    addr=LORA_ADDR,
                    power=22,
                    rssi=True,     # REQUIRED — E22 appends RSSI byte
                    air_speed=2400,
                    relay=False,
                )
                print(f"{Colors.GREEN}✓ LoRa hardware ready (RSSI enabled){Colors.RESET}")
            except Exception as exc:
                print(f"{Colors.RED}✗ LoRa init failed: {exc}{Colors.RESET}")
        else:
            print(f"{Colors.YELLOW}⚠  Simulation mode (not on Raspberry Pi){Colors.RESET}")

    # ------------------------------------------------------------------
    def _post(self, anchor_id: str, rssi: int, tourist_id: str,
               sos_flag: bool, timestamp: float) -> bool:
        """HTTP POST a single anchor reading to the server."""
        payload = {
            "anchor_id"  : anchor_id,
            "rssi"       : rssi,
            "tourist_id" : tourist_id,
            "sos_flag"   : sos_flag,
            "timestamp"  : timestamp,
        }
        try:
            resp = requests.post(
                self.server_url, json=payload, timeout=POST_TIMEOUT
            )
            return resp.status_code == 200
        except requests.exceptions.RequestException:
            return False

    # ------------------------------------------------------------------
    def _send_session(self, session: Session):
        """
        Send all readings in a completed / timed-out session to the server.
        Buffers any that fail.
        """
        ts = time.time()
        for anchor_id, rssi in session.readings.items():
            ok = self._post(
                anchor_id=anchor_id,
                rssi=rssi,
                tourist_id=session.tourist_id,
                sos_flag=session.sos_flag,
                timestamp=ts,
            )
            if ok:
                self.total_sent += 1
                print(
                    f"  {Colors.GREEN}✓ Sent [{anchor_id}] "
                    f"rssi={rssi} tourist={session.tourist_id}{Colors.RESET}"
                )
            else:
                print(
                    f"  {Colors.YELLOW}⚠ Buffer [{anchor_id}] "
                    f"(server unreachable){Colors.RESET}"
                )
                with self._buf_lock:
                    if len(self.offline_buf) < MAX_BUFFER:
                        self.offline_buf.append({
                            "anchor_id"  : anchor_id,
                            "rssi"       : rssi,
                            "tourist_id" : session.tourist_id,
                            "sos_flag"   : session.sos_flag,
                            "timestamp"  : ts,
                        })
                        self.total_buffered += 1

    # ------------------------------------------------------------------
    def _flush_buffer(self):
        """Try to send buffered readings when server is reachable again."""
        with self._buf_lock:
            if not self.offline_buf:
                return
            buf_copy = list(self.offline_buf)

        print(
            f"\n{Colors.DIM}Flushing {len(buf_copy)} buffered "
            f"readings…{Colors.RESET}"
        )
        sent = []
        for item in buf_copy:
            ok = self._post(**item)
            if ok:
                sent.append(item)
                self.total_sent += 1

        with self._buf_lock:
            for item in sent:
                try:
                    self.offline_buf.remove(item)
                except ValueError:
                    pass

        if sent:
            print(
                f"{Colors.GREEN}  ✓ Flushed {len(sent)} / "
                f"{len(buf_copy)} records{Colors.RESET}"
            )

    # ------------------------------------------------------------------
    def _heartbeat_loop(self):
        """Background thread: send periodic heartbeat to server."""
        while True:
            time.sleep(HEARTBEAT_INT)
            try:
                requests.post(
                    HEARTBEAT_URL,
                    json={
                        "anchor_id"   : MASTER_ID,
                        "sessions"    : self.total_sessions,
                        "sent"        : self.total_sent,
                        "sos"         : self.total_sos,
                        "buffered"    : len(self.offline_buf),
                    },
                    timeout=POST_TIMEOUT,
                )
            except Exception:
                pass  # heartbeat failure is non-critical

    # ------------------------------------------------------------------
    def _finalize_session(self):
        """Dispatch and reset the current session."""
        if self.session is None or not self.session.has_any:
            return

        sess = self.session
        self.total_sessions += 1
        if sess.sos_flag:
            self.total_sos += 1

        complete = sess.is_complete
        ts_str   = time.strftime("%H:%M:%S")

        print(f"\n{Colors.CYAN}{'═'*55}{Colors.RESET}")
        if sess.sos_flag:
            print(
                f"{Colors.RED}{Colors.BOLD}"
                f"🚨 [{ts_str}] SOS SESSION #{self.total_sessions} "
                f"— {sess.tourist_id}{Colors.RESET}"
            )
        else:
            print(
                f"{Colors.GREEN}{Colors.BOLD}"
                f"📍 [{ts_str}] SESSION #{self.total_sessions} "
                f"— {sess.tourist_id}{Colors.RESET}"
            )

        received  = set(sess.readings.keys())
        missing   = Session.REQUIRED - received
        print(
            f"  Anchors: {Colors.GREEN}{sorted(received)}{Colors.RESET} "
            + (f"{Colors.YELLOW}missing={sorted(missing)}{Colors.RESET}" if missing else "")
        )
        print(
            f"  {'Complete ✓' if complete else 'Timeout (partial)'} — "
            f"sending {len(sess.readings)} reading(s)"
        )
        self._send_session(sess)

        # Opportunistic buffer flush
        self._flush_buffer()

        print(
            f"  {Colors.DIM}Stats: sessions={self.total_sessions} "
            f"sent={self.total_sent} sos={self.total_sos} "
            f"buffered={len(self.offline_buf)}{Colors.RESET}"
        )
        print(f"{Colors.DIM}{'─'*55}{Colors.RESET}\n")

        self.session = None

    # ------------------------------------------------------------------
    def run(self):
        os.system("cls" if os.name == "nt" else "clear")
        print(f"\n{Colors.CYAN}{Colors.BOLD}")
        print("╔══════════════════════════════════════════════════════╗")
        print("║   🛰   MASTER NODE A1 — Tourist Safety System        ║")
        print("║   LoRa (no-internet zone)  ↔  Wi-Fi (server)        ║")
        print("╚══════════════════════════════════════════════════════╝")
        print(f"{Colors.RESET}")
        print(f"  Frequency  : {Colors.CYAN}{LORA_FREQ} MHz (India ISM){Colors.RESET}")
        print(f"  Relay IDs  : {Colors.DIM}{RELAY_IDS}{Colors.RESET}")
        print(f"  Server     : {self.server_url}")
        print(f"  Session TO : {SESSION_TIMEOUT}s")
        print(f"  Buffer max : {MAX_BUFFER} sessions")
        print()

        # Check server reachability
        try:
            r = requests.get(
                self.server_url.replace("/api/reading", "/api/state"),
                timeout=2
            )
            if r.status_code == 200:
                print(f"{Colors.GREEN}✓ Server reachable{Colors.RESET}")
            else:
                print(f"{Colors.YELLOW}⚠ Server responded {r.status_code}{Colors.RESET}")
        except Exception:
            print(f"{Colors.YELLOW}⚠ Server unreachable — buffering enabled{Colors.RESET}")

        print(f"\n{Colors.GREEN}{Colors.BOLD}✓ Ready — listening for tourist signals…{Colors.RESET}\n")

        # Start heartbeat thread
        threading.Thread(target=self._heartbeat_loop, daemon=True).start()

        try:
            while True:
                self._loop()
                time.sleep(0.02)

        except KeyboardInterrupt:
            print(f"\n\n{Colors.YELLOW}Shutting down…{Colors.RESET}")
            # Finalize any open session
            self._finalize_session()
            print(
                f"\n{Colors.DIM}Final stats: sessions={self.total_sessions} "
                f"sent={self.total_sent} sos={self.total_sos} "
                f"buffered={len(self.offline_buf)}{Colors.RESET}"
            )

    # ------------------------------------------------------------------
    def _loop(self):
        """Main event loop: receive → parse → session management → dispatch."""

        # ── 1. Check for session timeout ──────────────────────────────
        if self.session and self.session.is_timed_out:
            print(
                f"\n{Colors.YELLOW}⏱ Session timeout — "
                f"dispatching with {len(self.session.readings)} / "
                f"{len(Session.REQUIRED)} readings{Colors.RESET}"
            )
            self._finalize_session()

        # ── 2. Check for session completion ───────────────────────────
        if self.session and self.session.is_complete:
            self._finalize_session()

        # ── 3. Receive from LoRa ──────────────────────────────────────
        message, hw_rssi = (self.lora.receive() if self.lora else (None, None))
        if not message:
            return

        anchor_id, tourist_id, report_rssi, msg_type, is_report = \
            parse_message(message)

        if anchor_id is None:
            return   # garbage packet

        is_sos  = (msg_type == "SOS")
        # RSSI value to record:
        #   Direct tourist ping → hardware RSSI (measured by this Pi)
        #   Relay REPORT        → RSSI embedded in the REPORT message
        rssi_val = report_rssi if is_report else hw_rssi

        ts = time.strftime("%H:%M:%S")

        if is_report:
            src_label = f"relay  [{anchor_id}]"
        else:
            src_label = f"direct [tourist]"

        if is_sos:
            print(
                f"{Colors.RED}{Colors.BOLD}"
                f"🚨 [{ts}] SOS  | {src_label} | "
                f"tourist={tourist_id} | RSSI={rssi_val} dBm{Colors.RESET}"
            )
        else:
            print(
                f"{Colors.GREEN}"
                f"📥 [{ts}] PING | {src_label} | "
                f"tourist={tourist_id} | RSSI={rssi_val} dBm{Colors.RESET}"
            )

        # ── 4. Session management ─────────────────────────────────────
        if self.session is None:
            # Start a new session
            self.session = Session(tourist_id, is_sos)

        elif self.session.tourist_id != tourist_id:
            # Different tourist — finalize old session, start fresh
            print(
                f"{Colors.YELLOW}⚠ New tourist detected "
                f"({tourist_id}) — finalising previous session{Colors.RESET}"
            )
            self._finalize_session()
            self.session = Session(tourist_id, is_sos)

        # Add this reading to the current session
        self.session.add(anchor_id, rssi_val, is_sos)

        collected = set(self.session.readings.keys())
        remaining = Session.REQUIRED - collected
        print(
            f"  {Colors.DIM}Session: {sorted(collected)} "
            f"| still waiting: {sorted(remaining) if remaining else 'none'}{Colors.RESET}"
        )


# ============================================================
#  ENTRY POINT
# ============================================================
def main():
    parser = argparse.ArgumentParser(
        description="Master Node — Tourist Safety System (LoRa Gateway)"
    )
    parser.add_argument(
        "--server", type=str, default=SERVER_URL,
        help="Server /api/reading URL (must have Wi-Fi access to this)"
    )
    args = parser.parse_args()

    node = MasterNode(server_url=args.server)
    node.run()


if __name__ == "__main__":
    main()
