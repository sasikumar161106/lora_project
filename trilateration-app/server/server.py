#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
server.py — Tourist Safety System: Trilateration + Offline GPS Server
======================================================================
Central server. Run on a laptop or single-board computer connected to the
same Wi-Fi as the anchor Raspberry Pis.

KEY CONCEPT — OFFLINE GPS via LoRa:
  • No internet, no satellite GPS chip on the tourist device.
  • Three fixed anchor nodes are placed at known real-world GPS locations.
  • When a tourist LoRa device broadcasts, each anchor hears it and
    measures the received signal strength (RSSI).
  • This server converts each anchor's RSSI → distance (log-distance
    path-loss model), then trilaterates the tourist's local X,Y position.
  • X,Y is converted to GPS lat/lng using the anchor GPS reference points
    via a flat-Earth Haversine approximation.
  • Result: real-time GPS tracking with ZERO internet dependency.

Safety features:
  • SOS detection: if any anchor reports an SOS packet, an alert is
    broadcast via WebSocket to all connected dashboards instantly.
  • Per-tourist tracking: multiple tourist devices can be tracked
    simultaneously (identified by their device IDs).
  • Offline buffering: positions computed while the DB is unavailable
    are stored in memory and flushed when the DB reconnects.

Setup:
  1. pip install flask flask-socketio eventlet numpy scipy requests
  2. Edit GPS_REFERENCE below with your actual anchor A1 GPS coordinates.
  3. Edit ANCHORS with the real X,Y of each anchor (A1=origin, measure
     others relative to it in meters).
  4. Run: python3 server.py
  5. Open http://localhost:5000 or http://<your-ip>:5000
"""

import os
import time
import math
import threading

from flask import Flask, request, jsonify, render_template
from flask_socketio import SocketIO
import numpy as np
from scipy.optimize import least_squares

import db

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")


# =====================================================================
#  CONFIGURATION — EDIT THESE FOR YOUR DEPLOYMENT SITE
# =====================================================================

# GPS coordinates of your MASTER/first anchor (A1) — this is the origin
# of the local X,Y coordinate system.
# Default: Tamil Nadu, India (example — replace with your real location)
GPS_REFERENCE = {
    "lat": float(os.environ.get("GPS_REF_LAT", "13.082680")),
    "lng": float(os.environ.get("GPS_REF_LNG", "80.270721")),
}

# Fixed anchor positions in LOCAL meters (A1 = origin 0,0)
# Measure the other anchors' distances from A1 with a tape measure.
ANCHORS = {
    "A1": {"x": 0.0,  "y": 0.0},
    "A2": {"x": 5.0,  "y": 0.0},
    "A3": {"x": 1.76, "y": 3.81},
}

# RSSI-to-distance calibration (log-distance path-loss model):
#   distance = 10 ^ ((RSSI_AT_1M - measured_rssi) / (10 * PATH_LOSS_EXPONENT))
# Run calibrate.py to measure real values for your environment.
RSSI_AT_1M         = float(os.environ.get("RSSI_AT_1M", "-24.0"))
PATH_LOSS_EXPONENT = float(os.environ.get("PATH_LOSS_EXP", "1.79"))

# Rolling RSSI average window (smooths out multipath fading jitter)
ROLLING_WINDOW = 5

# Ignore anchor readings older than this (seconds)
READING_MAX_AGE = 10.0

# How often to flush in-memory data to the database (seconds)
DB_FLUSH_INTERVAL = 2.0

# SOS alert expiry — how long to keep an SOS active after the last packet
SOS_EXPIRY_SECONDS = 30.0

# =====================================================================


# ── In-memory state ──────────────────────────────────────────────────
state_lock   = threading.Lock()
anchor_state = {}   # anchor_id → {history, avg_rssi, distance, last_seen,
                    #               tourist_id, sos_flag}

position_trail       = []   # list of {x, y, t, tourist_id, sos_flag}
MAX_TRAIL            = 200
last_flushed_trail_index = 0

# SOS tracking: tourist_id → last SOS timestamp
sos_alerts = {}     # {tourist_id: last_sos_time}

# Current tracked tourist (most recently active)
current_tourist_id = None
# ─────────────────────────────────────────────────────────────────────


# =====================================================================
#  DATABASE BOOTSTRAP
# =====================================================================
def load_state_from_db():
    """Restore last-known state from the database on startup."""
    global position_trail, last_flushed_trail_index
    try:
        readings = db.get_all_anchor_readings()
        with state_lock:
            for r in readings:
                anchor_state[r["anchor_id"]] = {
                    "history"   : [r["rssi"]] if r.get("rssi") is not None else [],
                    "avg_rssi"  : r["rssi"],
                    "distance"  : r["distance"],
                    "last_seen" : r["last_seen"],
                    "tourist_id": None,
                    "sos_flag"  : False,
                }
            trail_records  = db.get_recent_trail(limit=MAX_TRAIL)
            position_trail = [
                {"x": r["x"], "y": r["y"], "t": r["t"],
                 "tourist_id": r.get("tourist_id"), "sos_flag": r.get("sos_flag", False)}
                for r in trail_records
            ]
            last_flushed_trail_index = len(position_trail)
        print("[DB] State restored from database.")
    except Exception as exc:
        print(f"[WARN] Could not load state from DB: {exc}")


# =====================================================================
#  KALMAN FILTER (2-D constant-velocity, smooths jitter)
# =====================================================================
class KalmanFilter2D:
    """
    Smooths noisy RSSI-trilaterated X,Y positions.
    State vector: [x, y, vx, vy]
    """
    def __init__(self, process_noise: float = 0.1,
                 measurement_noise: float = 0.8):
        self.x = None
        self.P = np.eye(4) * 10.0
        self.q  = process_noise
        self.R  = np.eye(2) * measurement_noise
        self.last_t = None

    def update(self, z_x: float, z_y: float, t: float):
        if self.x is None:
            self.x      = np.array([z_x, z_y, 0.0, 0.0])
            self.last_t = t
            return z_x, z_y

        dt = max(0.05, min(2.0, t - self.last_t))
        self.last_t = t

        F = np.array([
            [1.0, 0.0, dt,  0.0],
            [0.0, 1.0, 0.0, dt ],
            [0.0, 0.0, 1.0, 0.0],
            [0.0, 0.0, 0.0, 1.0],
        ])
        q = self.q
        Q = np.array([
            [0.25*dt**4*q, 0,            0.5*dt**3*q, 0],
            [0,            0.25*dt**4*q, 0,           0.5*dt**3*q],
            [0.5*dt**3*q,  0,            dt**2*q,     0],
            [0,            0.5*dt**3*q,  0,           dt**2*q],
        ])
        H = np.array([[1, 0, 0, 0], [0, 1, 0, 0]], dtype=float)
        z = np.array([z_x, z_y])

        x_pred = F @ self.x
        P_pred = F @ self.P @ F.T + Q
        y_k    = z - H @ x_pred
        S      = H @ P_pred @ H.T + self.R
        K      = P_pred @ H.T @ np.linalg.inv(S)

        self.x = x_pred + K @ y_k
        self.P = (np.eye(4) - K @ H) @ P_pred
        return float(self.x[0]), float(self.x[1])


kalman_tracker = KalmanFilter2D()


# =====================================================================
#  MATH HELPERS
# =====================================================================
def compute_filtered_rssi(history: list):
    """Trimmed mean: drop min/max then average."""
    if not history:
        return None
    if len(history) < 3:
        return sum(history) / len(history)
    sorted_h = sorted(history)
    return sum(sorted_h[1:-1]) / len(sorted_h[1:-1])


def rssi_to_distance(rssi) -> float:
    """Log-distance path-loss model → distance in metres."""
    if rssi is None:
        return None
    if rssi > -10:
        rssi = -10   # cap unrealistically strong values
    exponent = (RSSI_AT_1M - rssi) / (10.0 * PATH_LOSS_EXPONENT)
    return 10.0 ** exponent


def trilaterate(points: list):
    """
    Weighted Least Squares trilateration.
    points: [(x, y, distance), …]  — minimum 3 required.
    Returns (x, y) or None on failure.
    """
    if len(points) < 3:
        return None

    xs = np.array([p[0] for p in points])
    ys = np.array([p[1] for p in points])
    ds = np.array([p[2] for p in points])

    # Inverse-distance-squared weights (closer = more reliable)
    weights = 1.0 / (ds ** 2 + 0.1)
    weights /= weights.sum()
    x0 = np.array([np.sum(xs * weights), np.sum(ys * weights)])

    def residuals(pt):
        return (np.sqrt((xs - pt[0])**2 + (ys - pt[1])**2) - ds) * np.sqrt(weights)

    result = least_squares(residuals, x0)
    if not result.success:
        return None

    # Spatial clamp to anchor bounding box + 3 m margin
    rx, ry   = float(result.x[0]), float(result.x[1])
    rx = max(min(xs) - 3.0, min(max(xs) + 3.0, rx))
    ry = max(min(ys) - 3.0, min(max(ys) + 3.0, ry))
    return rx, ry


def xy_to_gps(x: float, y: float) -> tuple:
    """
    Convert local X,Y (metres from anchor A1) to GPS lat/lng.
    Uses flat-Earth approximation — accurate for short distances (< 10 km).
    This is the core of the OFFLINE GPS feature.

    Parameters
    ----------
    x : East-West offset in metres from A1
    y : North-South offset in metres from A1

    Returns
    -------
    (latitude, longitude) in decimal degrees
    """
    R       = 6_378_137.0                      # Earth radius metres
    ref_lat = GPS_REFERENCE["lat"]
    ref_lng = GPS_REFERENCE["lng"]

    d_lat = y / R
    d_lng = x / (R * math.cos(math.radians(ref_lat)))

    lat = ref_lat + math.degrees(d_lat)
    lng = ref_lng + math.degrees(d_lng)
    return round(lat, 7), round(lng, 7)


# =====================================================================
#  SOS HELPERS
# =====================================================================
def record_sos(tourist_id: str, t: float):
    """Mark a tourist as SOS-active and emit WebSocket alert."""
    sos_alerts[tourist_id] = t
    socketio.emit("sos_alert", {
        "tourist_id": tourist_id,
        "timestamp" : t,
        "message"   : f"🚨 SOS from {tourist_id}",
    })


def get_active_sos(now: float) -> list:
    """Return list of tourist IDs with an active (non-expired) SOS."""
    return [
        tid for tid, ts in sos_alerts.items()
        if now - ts <= SOS_EXPIRY_SECONDS
    ]


# =====================================================================
#  API ROUTES
# =====================================================================
@app.route("/api/reading", methods=["POST"])
def post_reading():
    """
    Called by each anchor Pi every time it hears the tourist node.
    Expected JSON body:
    {
        "anchor_id"  : "A1",
        "rssi"       : -65,
        "tourist_id" : "DEV001",      ← NEW (tourist safety system)
        "sos_flag"   : false,          ← NEW (tourist safety system)
        "timestamp"  : 1700000000.0   ← optional
    }
    """
    global current_tourist_id

    data       = request.get_json(force=True)
    anchor_id  = data.get("anchor_id")
    rssi       = data.get("rssi")
    tourist_id = data.get("tourist_id", "UNKNOWN").upper()
    sos_flag   = bool(data.get("sos_flag", False))
    timestamp  = float(data.get("timestamp", time.time()))

    # Validate anchor
    if anchor_id not in ANCHORS:
        return jsonify({"error": f"Unknown anchor_id '{anchor_id}'"}), 400
    if rssi is None:
        return jsonify({"error": "Missing rssi field"}), 400

    # Update SOS registry
    if sos_flag:
        record_sos(tourist_id, timestamp)

    with state_lock:
        current_tourist_id = tourist_id

        s = anchor_state.setdefault(anchor_id, {"history": []})
        s["history"].append(rssi)
        if len(s["history"]) > ROLLING_WINDOW:
            s["history"].pop(0)

        avg_rssi = compute_filtered_rssi(s["history"])
        distance = rssi_to_distance(avg_rssi)

        s.update({
            "avg_rssi"  : avg_rssi,
            "distance"  : distance,
            "last_seen" : timestamp,
            "tourist_id": tourist_id,
            "sos_flag"  : sos_flag,
        })

        # Try trilateration
        usable = []
        for a_id, pos in ANCHORS.items():
            a_s = anchor_state.get(a_id)
            if (a_s and a_s.get("last_seen") and
                    timestamp - a_s["last_seen"] <= READING_MAX_AGE and
                    a_s.get("distance") is not None):
                usable.append((pos["x"], pos["y"], a_s["distance"]))

        if len(usable) >= 3:
            raw = trilaterate(usable)
            if raw:
                sx, sy = kalman_tracker.update(raw[0], raw[1], timestamp)
                position_trail.append({
                    "x"          : sx,
                    "y"          : sy,
                    "t"          : timestamp,
                    "tourist_id" : tourist_id,
                    "sos_flag"   : sos_flag,
                })

    # Push live state to dashboard
    state_payload = _build_state_payload(time.time())
    socketio.emit("state_update", state_payload)

    return jsonify({"ok": True})


@app.route("/api/sos", methods=["GET"])
def get_sos():
    """Return currently active SOS alerts."""
    now    = time.time()
    active = get_active_sos(now)
    return jsonify({
        "active_sos": active,
        "alerts": [
            {"tourist_id": tid, "timestamp": sos_alerts[tid],
             "age_seconds": round(now - sos_alerts[tid], 1)}
            for tid in active
        ],
    })


@app.route("/api/state")
def get_state():
    return jsonify(_build_state_payload(time.time()))


# Master node heartbeat registry
master_heartbeats = {}   # anchor_id → {timestamp, stats}


@app.route("/api/heartbeat", methods=["POST"])
def post_heartbeat():
    """
    Called by the master_node.py every 60 seconds.
    Lets the dashboard know the field gateway is still alive.
    """
    data      = request.get_json(force=True)
    anchor_id = data.get("anchor_id", "MASTER")
    now       = time.time()
    master_heartbeats[anchor_id] = {
        "timestamp": now,
        "stats"    : data,
    }
    print(
        f"[HB] Master '{anchor_id}' heartbeat — "
        f"sessions={data.get('sessions','?')} "
        f"sent={data.get('sent','?')} "
        f"buffered={data.get('buffered','?')}"
    )
    return jsonify({"ok": True, "server_time": now})


@app.route("/api/master-status")
def master_status():
    """Return connected master node heartbeats and their status."""
    now = time.time()
    result = {}
    for anchor_id, hb in master_heartbeats.items():
        age = now - hb["timestamp"]
        result[anchor_id] = {
            "last_seen_seconds_ago": round(age, 1),
            "online": age < 120,    # consider online if heartbeat within 2 min
            "stats" : hb["stats"],
        }
    return jsonify(result)


@app.route("/")
def dashboard():
    return render_template("index.html")



# =====================================================================
#  STATE PAYLOAD BUILDER
# =====================================================================
def _build_state_payload(now: float) -> dict:
    """
    Compile the full state snapshot pushed to the dashboard.
    Includes: anchor readings, tourist position (X/Y + GPS), SOS status.
    """
    anchors_out  = []
    usable       = []
    active_sos   = get_active_sos(now)

    with state_lock:
        for a_id, pos in ANCHORS.items():
            s     = anchor_state.get(a_id)
            entry = {
                "id"        : a_id,
                "x"         : pos["x"],
                "y"         : pos["y"],
                "rssi"      : None,
                "distance"  : None,
                "age"       : None,
                "stale"     : True,
                "tourist_id": None,
                "sos_flag"  : False,
            }
            if s and s.get("last_seen"):
                age = now - s["last_seen"]
                entry.update({
                    "rssi"      : s.get("avg_rssi"),
                    "distance"  : s.get("distance"),
                    "age"       : round(age, 1),
                    "stale"     : age > READING_MAX_AGE,
                    "tourist_id": s.get("tourist_id"),
                    "sos_flag"  : s.get("sos_flag", False),
                })
                if not entry["stale"] and entry["distance"] is not None:
                    usable.append((pos["x"], pos["y"], entry["distance"]))
            anchors_out.append(entry)

        # Position
        position = None
        gps      = None
        if len(usable) >= 3:
            result = trilaterate(usable)
            if result:
                position = {"x": result[0], "y": result[1]}
                lat, lng = xy_to_gps(result[0], result[1])
                gps = {"lat": lat, "lng": lng}

        trail = list(position_trail[-MAX_TRAIL:])

    return {
        "anchors"            : anchors_out,
        "position"           : position,
        "gps"                : gps,             # ← offline GPS coordinates
        "trail"              : trail,
        "usable_anchor_count": len(usable),
        "tourist_id"         : current_tourist_id,
        "sos_active"         : len(active_sos) > 0,
        "sos_tourists"       : active_sos,
        "server_time"        : now,
    }


# =====================================================================
#  PERIODIC DATABASE FLUSH
# =====================================================================
def periodic_db_flush():
    global last_flushed_trail_index
    while True:
        time.sleep(DB_FLUSH_INTERVAL)
        with state_lock:
            anchors_snap = [
                (a_id, s.get("avg_rssi"), s.get("distance"), s.get("last_seen"))
                for a_id, s in anchor_state.items()
                if "avg_rssi" in s
            ]
            trail_snap = position_trail[last_flushed_trail_index:]
            num_new    = len(trail_snap)

        if not anchors_snap and not trail_snap:
            continue
        try:
            for row in anchors_snap:
                db.upsert_anchor_reading(*row)
            for pt in trail_snap:
                db.insert_position(
                    pt["x"], pt["y"], pt["t"],
                    tourist_id=pt.get("tourist_id"),
                    sos_flag=pt.get("sos_flag", False),
                )
            with state_lock:
                last_flushed_trail_index += num_new
                if len(position_trail) > MAX_TRAIL:
                    overflow = len(position_trail) - MAX_TRAIL
                    del position_trail[:overflow]
                    last_flushed_trail_index = max(
                        0, last_flushed_trail_index - overflow
                    )
        except Exception as exc:
            print(f"[WARN] DB flush error: {exc}")


# =====================================================================
#  STARTUP
# =====================================================================
if __name__ == "__main__":
    print("=" * 62)
    print("  Tourist Safety System — Offline GPS Trilateration Server")
    print(f"  Anchors      : {list(ANCHORS.keys())}")
    print(f"  GPS Reference: {GPS_REFERENCE['lat']}°N, {GPS_REFERENCE['lng']}°E")
    print(f"  Frequency    : 865 MHz (India ISM band)")
    print(f"  RSSI@1m={RSSI_AT_1M}  N={PATH_LOSS_EXPONENT}")
    print("  Dashboard    : http://0.0.0.0:5000")
    print("=" * 62)

    db.init_db()
    load_state_from_db()

    threading.Thread(target=periodic_db_flush, daemon=True).start()

    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host="0.0.0.0", port=port, debug=False)
