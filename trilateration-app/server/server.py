#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
server.py
=========
Central server. Run this on your laptop (or any one machine on the same
WiFi as the anchor Pis).

What it does:
  1. Exposes POST /api/reading — each anchor Pi calls this every time it
     hears the tourist node, sending {anchor_id, rssi, timestamp}.
  2. Converts each anchor's RSSI into an estimated distance (log-distance
     path loss model), with a rolling average to smooth out jitter.
  3. Runs least-squares trilateration using all anchors with a recent
     reading, to estimate the tourist node's (x, y) position.
  4. Serves a live web dashboard (GET /) that polls /api/state every
     second and shows: anchor positions, per-anchor RSSI/distance, the
     computed tourist position, and a trail of recent positions.

Setup:
  1. pip install flask requests numpy
  2. Edit ANCHORS below with each anchor's real ID and (x, y) position
     in meters (or lat/lon — see USE_LATLON below).
  3. Edit RSSI_AT_1M and PATH_LOSS_EXPONENT after calibrating
     (see calibrate.py in this folder).
  4. Run: python3 server.py
  5. Open http://localhost:5000 in a browser (or http://<laptop-ip>:5000
     from another device on the same network).
"""

import time
import math
import threading

import os
from flask import Flask, request, jsonify, render_template
from flask_socketio import SocketIO

import numpy as np
from scipy.optimize import least_squares

import db

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")

# ===================== CONFIGURE THIS =====================

# Fixed anchor positions, in meters, on a local flat grid you define
# (e.g. anchor A1 = origin (0,0), measure the others relative to it with
# a tape measure / GPS-derived local coords). Add/remove anchors freely.
ANCHORS = {
    "A1": {"x": 0.0,  "y": 0.0},
    "A2": {"x": 20.0, "y": 0.0},
    "A3": {"x": 10.0, "y": 17.0},
}

# RSSI-to-distance calibration (log-distance path loss model):
#   distance = 10 ^ ((RSSI_AT_1M - measured_rssi) / (10 * PATH_LOSS_EXPONENT))
# RSSI_AT_1M: the RSSI you measure at exactly 1 meter from an anchor.
# PATH_LOSS_EXPONENT: how fast signal decays with distance.
#   ~2.0 = free space / open field
#   ~2.5-3.0 = indoors, some obstacles
#   ~3.5-4.0 = indoors, many walls/obstacles
# These defaults are PLACEHOLDERS. Run calibrate.py to measure real values
# for your hardware and environment, then update these two numbers.
RSSI_AT_1M = -40.0
PATH_LOSS_EXPONENT = 2.5

# How many recent RSSI samples to average per anchor (smooths jitter)
ROLLING_WINDOW = 5

# An anchor's reading is ignored if it's older than this (seconds) —
# stops trilateration from using stale data if an anchor goes quiet
READING_MAX_AGE = 10.0

DB_FLUSH_INTERVAL = 2.0

# ============================================================


# In-memory state. anchor_id -> {"history": [...rssi...], "avg_rssi": ..., "distance": ..., "last_seen": ...}
state_lock = threading.Lock()
anchor_state = {}

position_trail = []
MAX_TRAIL = 200
last_flushed_trail_index = 0

def load_state_from_db():
    global position_trail, last_flushed_trail_index
    try:
        readings = db.get_all_anchor_readings()
        with state_lock:
            for r in readings:
                anchor_state[r["anchor_id"]] = {
                    "history": [r["rssi"]] if r.get("rssi") is not None else [],
                    "avg_rssi": r["rssi"],
                    "distance": r["distance"],
                    "last_seen": r["last_seen"]
                }
            
            trail_records = db.get_recent_trail(limit=MAX_TRAIL)
            position_trail = [{"x": r["x"], "y": r["y"], "t": r["t"]} for r in trail_records]
            last_flushed_trail_index = len(position_trail)
        print("Loaded initial state from DB.")
    except Exception as e:
        print(f"[WARN] Failed to load initial state from DB: {e}")


def rssi_to_distance(rssi):
    """Log-distance path loss model. Returns distance in meters."""
    if rssi is None:
        return None
    if rssi > -10:
        rssi = -10  # cap unrealistic values (e.g. anchor right next to tourist)
    exponent = (RSSI_AT_1M - rssi) / (10 * PATH_LOSS_EXPONENT)
    return 10 ** exponent


def trilaterate(points):
    """
    points: list of (x, y, distance) tuples, one per anchor with a recent
    reading. Requires at least 3.

    Uses least-squares to find the (x, y) that best fits all the
    distance constraints simultaneously — this is more robust to noisy
    RSSI than solving 3 circle equations exactly, and works cleanly with
    4+ anchors too (over-determined system).
    """
    if len(points) < 3:
        return None

    xs = np.array([p[0] for p in points])
    ys = np.array([p[1] for p in points])
    ds = np.array([p[2] for p in points])

    # initial guess: centroid of the anchors used
    x0 = np.array([xs.mean(), ys.mean()])

    def residuals(point):
        px, py = point
        return np.sqrt((xs - px) ** 2 + (ys - py) ** 2) - ds

    result = least_squares(residuals, x0)
    if not result.success:
        return None

    return float(result.x[0]), float(result.x[1])


@app.route("/api/reading", methods=["POST"])
def post_reading():
    data = request.get_json(force=True)
    anchor_id = data.get("anchor_id")
    rssi = data.get("rssi")
    timestamp = data.get("timestamp", time.time())

    if anchor_id not in ANCHORS:
        return jsonify({"error": f"unknown anchor_id '{anchor_id}'"}), 400
    if rssi is None:
        return jsonify({"error": "missing rssi (is rssi=True set on the anchor's LoRa node?)"}), 400

    global last_flushed_trail_index
    with state_lock:
        s = anchor_state.setdefault(anchor_id, {"history": []})
        s["history"].append(rssi)
        if len(s["history"]) > ROLLING_WINDOW:
            s["history"].pop(0)
        avg_rssi = sum(s["history"]) / len(s["history"])
        distance = rssi_to_distance(avg_rssi)

        s["avg_rssi"] = avg_rssi
        s["distance"] = distance
        s["last_seen"] = timestamp

        # Compute usable points for trilateration
        usable_points = []
        for a_id, pos in ANCHORS.items():
            a_s = anchor_state.get(a_id)
            if a_s and a_s.get("last_seen"):
                if timestamp - a_s["last_seen"] <= READING_MAX_AGE and a_s.get("distance") is not None:
                    usable_points.append((pos["x"], pos["y"], a_s["distance"]))
        
        if len(usable_points) >= 3:
            result = trilaterate(usable_points)
            if result:
                position_trail.append({"x": result[0], "y": result[1], "t": timestamp})
                if len(position_trail) > MAX_TRAIL:
                    position_trail.pop(0)
                    last_flushed_trail_index = max(0, last_flushed_trail_index - 1)

    # Compute latest state and push to clients
    state_payload = _compute_state_payload(timestamp)
    socketio.emit("state_update", state_payload)

    return jsonify({"ok": True})


def _compute_state_payload(now):
    anchors_out = []
    usable_points = []
    
    with state_lock:
        for anchor_id, pos in ANCHORS.items():
            r = anchor_state.get(anchor_id)
            entry = {
                "id": anchor_id,
                "x": pos["x"],
                "y": pos["y"],
                "rssi": None,
                "distance": None,
                "age": None,
                "stale": True,
            }
            if r and r.get("last_seen"):
                age = now - r["last_seen"]
                entry["rssi"] = r.get("avg_rssi")
                entry["distance"] = r.get("distance")
                entry["age"] = round(age, 1)
                entry["stale"] = age > READING_MAX_AGE

                if not entry["stale"] and entry["distance"] is not None:
                    usable_points.append((pos["x"], pos["y"], entry["distance"]))

            anchors_out.append(entry)

        position = None
        if len(usable_points) >= 3:
            result = trilaterate(usable_points)
            if result:
                position = {"x": result[0], "y": result[1]}

        trail = list(position_trail)

    return {
        "anchors": anchors_out,
        "position": position,
        "trail": trail,
        "usable_anchor_count": len(usable_points),
        "server_time": now,
    }

def periodic_db_flush():
    global last_flushed_trail_index
    while True:
        time.sleep(DB_FLUSH_INTERVAL)
        
        with state_lock:
            # Copy state to flush
            anchors_to_flush = []
            for a_id, s in anchor_state.items():
                if "avg_rssi" in s:
                    anchors_to_flush.append((a_id, s["avg_rssi"], s["distance"], s["last_seen"]))
            
            trail_to_flush = position_trail[last_flushed_trail_index:]
            num_flushed = len(trail_to_flush)
        
        if not anchors_to_flush and not trail_to_flush:
            continue
            
        try:
            # Perform DB I/O outside the lock
            for a in anchors_to_flush:
                db.upsert_anchor_reading(a[0], a[1], a[2], a[3])
            
            for t in trail_to_flush:
                db.insert_position(t["x"], t["y"], t["t"])
            
            with state_lock:
                # Only advance the index on success. 
                # If pop(0) happened during I/O, last_flushed_trail_index was already shifted back.
                # So we just advance it by what we successfully wrote.
                last_flushed_trail_index += num_flushed
                
        except Exception as e:
            print(f"[WARN] DB flush failed: {e}")

@app.route("/api/state")
def get_state():
    return jsonify(_compute_state_payload(time.time()))


@app.route("/")
def dashboard():
    return render_template("index.html")


if __name__ == "__main__":
    print("=" * 60)
    print("  Trilateration Server")
    print(f"  Anchors configured: {list(ANCHORS.keys())}")
    print(f"  RSSI_AT_1M={RSSI_AT_1M}  PATH_LOSS_EXPONENT={PATH_LOSS_EXPONENT}")
    print("  Dashboard: http://localhost:5000")
    print("=" * 60)
    
    db.init_db()
    load_state_from_db()
    
    threading.Thread(target=periodic_db_flush, daemon=True).start()
    
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host="0.0.0.0", port=port, debug=False)
