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

# ============================================================


# In-memory state. anchor_id -> {"history": [...rssi...]}
state_lock = threading.Lock()
anchor_state = {}

# Trail limit to fetch from database
MAX_TRAIL = 50


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

    with state_lock:
        s = anchor_state.setdefault(anchor_id, {"history": []})
        s["history"].append(rssi)
        if len(s["history"]) > ROLLING_WINDOW:
            s["history"].pop(0)
        avg_rssi = sum(s["history"]) / len(s["history"])
        distance = rssi_to_distance(avg_rssi)

    db.upsert_anchor_reading(anchor_id, avg_rssi, distance, timestamp)

    # Compute latest state and push to clients
    state_payload = _compute_state_payload(timestamp)
    socketio.emit("state_update", state_payload)

    return jsonify({"ok": True})


def _compute_state_payload(now):
    anchors_out = []
    usable_points = []
    
    db_readings = db.get_all_anchor_readings()
    readings_map = {r["anchor_id"]: r for r in db_readings}

    for anchor_id, pos in ANCHORS.items():
        r = readings_map.get(anchor_id)
        entry = {
            "id": anchor_id,
            "x": pos["x"],
            "y": pos["y"],
            "rssi": None,
            "distance": None,
            "age": None,
            "stale": True,
        }
        if r:
            age = now - r["last_seen"]
            entry["rssi"] = r.get("rssi")
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
            db.insert_position(result[0], result[1], now)

    trail = db.get_recent_trail(limit=MAX_TRAIL)

    return {
        "anchors": anchors_out,
        "position": position,
        "trail": trail,
        "usable_anchor_count": len(usable_points),
        "server_time": now,
    }

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
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host="0.0.0.0", port=port, debug=False)
