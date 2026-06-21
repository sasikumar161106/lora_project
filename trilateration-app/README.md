# RF Trilateration Dashboard

A live web dashboard for your LoRa-based trilateration project: 3+ fixed
"anchor" Raspberry Pis listen for a mobile "tourist" LoRa node, measure
its signal strength (RSSI), and a central server estimates the tourist's
position by combining all the anchors' readings.

## What was wrong with the original code

The original `sx126x.py` driver enabled RSSI mode (`rssi=True`) but its
`receive()` method never actually read the RSSI byte the LoRa module
sends — it sliced it off and threw it away:

```python
data_slice = r_buff[3:-1]   # last byte (RSSI) discarded, never used
```

So no RSSI was ever captured, and `distance_test.py` (the one file that
tried to do RSSI→distance math) imported modules (`src.drivers.sx126x`,
`config.settings`) that don't exist anywhere in the project. There was no
working trilateration pipeline — this project builds one from scratch on
top of your existing LoRa setup.

## How it works

```
Tourist node (mobile)  --ping-->  Anchor Pi 1 (known x,y) --HTTP-->  ┐
                        --ping-->  Anchor Pi 2 (known x,y) --HTTP-->  ├-> Server (your laptop) -> Web dashboard
                        --ping-->  Anchor Pi 3 (known x,y) --HTTP-->  ┘
```

1. **`anchor/tourist_node.py`** runs on the mobile device. It broadcasts a
   small ping packet once a second.
2. **`anchor/anchor_node.py`** runs on each fixed anchor Pi. It listens
   for those pings, measures real RSSI (using the fixed `sx126x.py`
   driver), and POSTs `{anchor_id, rssi, timestamp}` to your laptop.
3. **`server/server.py`** runs on your laptop. It converts each anchor's
   RSSI into an estimated distance, runs least-squares trilateration
   across all anchors with a recent reading, and serves a live web
   dashboard at `http://localhost:5000`.

## Setup

### 1. Calibrate RSSI → distance (do this first — it's the biggest accuracy lever)

Default calibration constants are placeholders and will NOT be accurate
for your specific hardware/environment. To fix this:

1. Copy `anchor/sx126x.py` and `anchor/calibrate.py` to one anchor Pi.
2. Put the tourist node at a known distance (e.g. exactly 1m), let it
   ping for ~15s, run `calibrate.py` on the anchor, note the average
   RSSI it reports.
3. Repeat at a few more known distances (e.g. 2m, 5m, 10m, 20m).
4. Put your (distance, rssi) pairs into `calibrate/fit_calibration.py`'s
   `MEASUREMENTS` list and run it on your laptop:
   ```
   cd calibrate
   pip install numpy
   python3 fit_calibration.py
   ```
5. Paste the printed `RSSI_AT_1M` and `PATH_LOSS_EXPONENT` values into
   `server/server.py`.

Recalibrate if you change anchor height, antenna orientation, or move to
a different environment (RSSI behaves very differently indoors vs
outdoors).

### 2. Configure anchor positions

In `server/server.py`, edit the `ANCHORS` dict with your real anchor
coordinates (meters on a local grid you define — e.g. anchor A1 = origin):

```python
ANCHORS = {
    "A1": {"x": 0.0,  "y": 0.0},
    "A2": {"x": 20.0, "y": 0.0},
    "A3": {"x": 10.0, "y": 17.0},
}
```

### 3. Configure each anchor Pi

Copy the `anchor/` folder to each anchor Pi. In `anchor_node.py` on each
one, edit:
- `ANCHOR_ID` — must match a key in `server.py`'s `ANCHORS` dict (e.g. `"A1"`)
- `SERVER_URL` — your laptop's IP address (find it with `ipconfig` on
  Windows, under your WiFi adapter's "IPv4 Address"), e.g.
  `http://192.168.1.50:5000/api/reading`
- `LORA_ADDR` — a unique number per anchor (1, 2, 3...)

### 4. Run everything

On your laptop:
```
cd server
pip install -r requirements.txt
python3 server.py
```
Open `http://localhost:5000` in a browser.

On each anchor Pi:
```
cd anchor
pip install -r requirements.txt
python3 anchor_node.py
```

On the mobile tourist device:
```
cd anchor
python3 tourist_node.py
```

## Deploying to Render

1. Push this project to a GitHub repository.
2. Go to https://dashboard.render.com, click "New +" → "Blueprint", connect the GitHub repo, and Render will read `render.yaml` automatically and provision both the web service and the database.
3. Wait for the first deploy to finish, then copy the live URL shown on the service page (looks like `https://trilateration-server.onrender.com`).
4. Paste that URL (with `/api/reading` appended) into `SERVER_URL` in `anchor_node.py` on every anchor Pi.
5. Open the live URL in a browser to view the dashboard. Note: if the service has been idle for 15+ minutes, the first load can take 30-50 seconds (free tier "cold start") while Render wakes the service back up. Subsequent loads are fast.

## Reading the dashboard

- **Position scope** (left): a top-down grid view. Anchors are shown as
  circles at their fixed positions; dashed rings show each anchor's
  current estimated distance to the tourist node. The amber dot is the
  trilaterated tourist position — if the dashed rings don't overlap
  cleanly near one point, that's a visual sign your calibration or anchor
  placement needs work.
- **Anchor list** (right): live RSSI, estimated distance, and signal age
  per anchor, so you can see exactly which anchor is giving a weak or
  stale reading instead of just an overall "wrong" position.

## Files

```
trilateration-app/
├── anchor/
│   ├── sx126x.py          # fixed LoRa driver (RSSI bug fixed)
│   ├── anchor_node.py     # runs on each fixed anchor Pi
│   ├── tourist_node.py    # runs on the mobile node
│   ├── calibrate.py       # run on an anchor to collect RSSI samples
│   └── requirements.txt
├── calibrate/
│   └── fit_calibration.py # run on laptop to fit calibration constants
├── server/
│   ├── server.py          # Flask backend: trilateration + API
│   ├── templates/
│   │   └── index.html     # live dashboard UI
│   └── requirements.txt
└── README.md
```

## Notes on accuracy

RSSI-based trilateration is inherently noisier than GPS — typical error
is 1-5 meters even with good calibration, more indoors or with
obstructions. The dashboard's rolling average (5 samples) smooths out
short-term jitter; if positions still feel jumpy, you can increase
`ROLLING_WINDOW` in `server.py` at the cost of slower response to real
movement. If one anchor is consistently the outlier, check its antenna
orientation and that nothing is blocking line-of-sight to where the
tourist node typically is.
