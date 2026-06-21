#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fit_calibration.py
===================
Run this on your LAPTOP (not the Pi) after collecting (distance, rssi)
pairs with calibrate.py on an anchor.

Fits the log-distance path loss model:
    rssi = RSSI_AT_1M - 10 * PATH_LOSS_EXPONENT * log10(distance)

to your real measurements, and prints the two values to paste into
server.py.

Usage:
  1. pip install numpy
  2. Edit MEASUREMENTS below with your real (distance_meters, rssi_dbm)
     pairs from calibrate.py. Use at least 3 distances, ideally 5+.
  3. python3 fit_calibration.py
"""

import math
import numpy as np

# ============ PASTE YOUR REAL MEASUREMENTS HERE ============
# Format: (distance_in_meters, average_rssi_in_dbm)
MEASUREMENTS = [
    (1.0,  -42.0),
    (2.0,  -48.0),
    (5.0,  -58.0),
    (10.0, -65.0),
    (20.0, -72.0),
]
# =============================================================


def fit(measurements):
    # rssi = RSSI_AT_1M - 10*n*log10(d)
    # This is linear in (RSSI_AT_1M, n) if we treat log10(d) as the x-variable:
    #   rssi = RSSI_AT_1M - (10*n) * log10(d)
    # Standard linear regression: y = a + b*x, where
    #   y = rssi, x = log10(d), a = RSSI_AT_1M, b = -10*n
    xs = np.array([math.log10(d) for d, _ in measurements])
    ys = np.array([r for _, r in measurements])

    A = np.vstack([np.ones_like(xs), xs]).T
    result, _, _, _ = np.linalg.lstsq(A, ys, rcond=None)
    rssi_at_1m, slope = result
    path_loss_exponent = -slope / 10

    return rssi_at_1m, path_loss_exponent


def main():
    if len(MEASUREMENTS) < 3:
        print("Need at least 3 (distance, rssi) measurements for a reliable fit.")
        return

    rssi_at_1m, n = fit(MEASUREMENTS)

    print("=" * 60)
    print("  Calibration Fit Results")
    print("=" * 60)
    print(f"\n  RSSI_AT_1M = {rssi_at_1m:.2f}")
    print(f"  PATH_LOSS_EXPONENT = {n:.3f}")

    print("\n  Paste these into server.py:")
    print(f"  RSSI_AT_1M = {rssi_at_1m:.2f}")
    print(f"  PATH_LOSS_EXPONENT = {n:.3f}")

    print("\n  Fit quality check (predicted vs actual):")
    for d, actual_rssi in MEASUREMENTS:
        predicted = rssi_at_1m - 10 * n * math.log10(d)
        print(f"    d={d:5.1f}m   actual={actual_rssi:6.1f} dBm   "
              f"predicted={predicted:6.1f} dBm   diff={predicted-actual_rssi:+.1f}")

    print("\n  If 'diff' is consistently large (>5 dBm) at some distances,")
    print("  collect more measurement points there, or note that RSSI may")
    print("  be unreliable in that range (e.g. too close, or obstructed).")


if __name__ == "__main__":
    main()
