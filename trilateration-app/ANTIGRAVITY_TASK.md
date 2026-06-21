# Task Brief: Deploy Trilateration Dashboard to Render with Real-Time Updates

## Context

This project is a LoRa-based RF trilateration system. Fixed "anchor"
Raspberry Pis listen for a mobile "tourist" LoRa node, measure its
signal strength (RSSI), and a central Flask server estimates the
tourist's live position using least-squares trilateration. A web
dashboard shows the result.

**Current state:** the server (`server/server.py`) runs locally on a
laptop, uses HTTP polling (browser asks for updates every 1s) and
keeps all state in memory (lost on restart).

**Goal of this task:** make the dashboard reachable from anywhere on
the internet, with near-instant updates instead of 1s polling lag, and
make state survive server restarts. Multiple people (friends/judges)
will view it simultaneously from outside the local network.

## Why each change is needed (read before editing)

1. **Hosting on Render, not the laptop** — the laptop has no stable
   public address. Render gives a permanent HTTPS URL and runs the
   server continuously.
2. **WebSockets instead of polling** — polling means up to 1s of lag
   per update and every viewer independently re-requests the full
   state every second. WebSockets let the server push a new position
   the instant it's computed, to all connected viewers at once.
3. **PostgreSQL instead of in-memory state** — Render's free tier
   restarts the service periodically (cold starts after inactivity).
   In-memory Python dicts are wiped on restart; a database persists
   across restarts.

## Step-by-step tasks

### 1. Add dependencies

In `server/requirements.txt`, replace contents with:

```
flask
flask-socketio
eventlet
numpy
scipy
requests
psycopg2-binary
```

(`eventlet` is required for Flask-SocketIO to handle concurrent
WebSocket connections properly in production. `psycopg2-binary` is the
PostgreSQL driver.)

### 2. Add PostgreSQL persistence

Create a new file `server/db.py`:

- Read the database connection string from an environment variable
  called `DATABASE_URL` (Render provides this automatically when you
  attach a PostgreSQL database to the service — do not hardcode any
  connection string).
- On startup, create two tables if they don't exist:
  - `anchor_readings`: columns `anchor_id (text)`, `rssi (float)`,
    `distance (float)`, `last_seen (double precision)` — one row per
    anchor, upserted (INSERT ... ON CONFLICT UPDATE) on every new
    reading, so it always holds the latest state per anchor.
  - `position_trail`: columns `id (serial primary key)`, `x (float)`,
    `y (float)`, `t (double precision)` — append-only log of computed
    positions. Keep only the most recent 200 rows (delete older rows
    after each insert, or run a periodic cleanup) to avoid unbounded
    growth.
- Expose simple functions: `upsert_anchor_reading(anchor_id, rssi,
  distance, timestamp)`, `get_all_anchor_readings()`,
  `insert_position(x, y, timestamp)`, `get_recent_trail(limit=50)`.

### 3. Modify `server/server.py`

- Import and use the new `db.py` functions instead of the in-memory
  `anchor_state` dict and `position_trail` list. Keep the rolling
  average smoothing logic (it can still keep a short in-memory window
  per anchor for smoothing — only the *latest smoothed value* and the
  *trail* need to persist to the database, not every raw sample).
- Replace `Flask` route-only setup with `Flask-SocketIO`:
  ```python
  from flask_socketio import SocketIO
  socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")
  ```
- In the `/api/reading` POST handler (called by each anchor Pi), after
  saving the new reading and recomputing the trilaterated position,
  **emit** the updated state to all connected browsers instead of
  waiting for them to poll:
  ```python
  socketio.emit("state_update", state_payload)
  ```
  where `state_payload` is the same JSON structure currently returned
  by `GET /api/state` (anchors list, position, trail, usable_anchor_count).
- Keep the existing `GET /api/state` REST endpoint too — it's still
  useful as a fallback for the initial page load (so the dashboard has
  something to show before the first WebSocket push arrives).
- At the bottom of the file, change the run command from
  `app.run(...)` to `socketio.run(app, host="0.0.0.0", port=...)`. Read
  the port from the `PORT` environment variable (Render sets this
  automatically — do not hardcode port 5000 for production, but keep
  5000 as a local-dev fallback default).

### 4. Modify `server/templates/index.html`

- Add the Socket.IO client library via CDN in the `<head>`:
  ```html
  <script src="https://cdn.socket.io/4.7.5/socket.io.min.js"></script>
  ```
- Replace the existing `poll()` / `setInterval(poll, 1000)` polling
  loop with a WebSocket connection:
  ```js
  const socket = io();
  socket.on("connect", () => { /* update status pill to "live" */ });
  socket.on("disconnect", () => { /* update status pill to "server unreachable" */ });
  socket.on("state_update", (state) => {
    render(state);
    renderAnchorList(state);
  });
  ```
- Keep one initial `fetch("/api/state")` call on page load (before the
  socket connects) so the dashboard isn't blank while waiting for the
  first push.
- Do not change the SVG rendering logic (`render()`,
  `renderAnchorList()`, `computeBounds()`, etc.) — only the data
  delivery mechanism changes, not how the visuals are drawn.

### 5. Add Render deployment config

Create `render.yaml` at the project root (`trilateration-app/render.yaml`):

```yaml
services:
  - type: web
    name: trilateration-server
    runtime: python
    rootDir: server
    buildCommand: pip install -r requirements.txt
    startCommand: python server.py
    envVars:
      - key: DATABASE_URL
        fromDatabase:
          name: trilateration-db
          property: connectionString
      - key: PYTHON_VERSION
        value: 3.11.0

databases:
  - name: trilateration-db
    plan: free
```

### 6. Update `anchor/anchor_node.py`

Change the `SERVER_URL` constant's comment to clarify it should now
point at the Render URL (e.g.
`https://trilateration-server.onrender.com/api/reading`) once deployed,
not a local laptop IP. Do not hardcode the actual Render URL since it
doesn't exist until after first deploy — leave it as a placeholder with
a clear comment showing where to find the real one (Render dashboard,
top of the service page).

### 7. Update `README.md`

Add a new section "Deploying to Render" with these steps in plain
language:
1. Push this project to a GitHub repository.
2. Go to https://dashboard.render.com, click "New +" → "Blueprint",
   connect the GitHub repo, and Render will read `render.yaml`
   automatically and provision both the web service and the database.
3. Wait for the first deploy to finish, then copy the live URL shown
   on the service page (looks like
   `https://trilateration-server.onrender.com`).
4. Paste that URL (with `/api/reading` appended) into `SERVER_URL` in
   `anchor_node.py` on every anchor Pi.
5. Open the live URL in a browser to view the dashboard. Note: if the
   service has been idle for 15+ minutes, the first load can take
   30-50 seconds (free tier "cold start") while Render wakes the
   service back up. Subsequent loads are fast.

## Acceptance criteria (verify before considering this done)

- [ ] `server/server.py` runs locally without errors:
      `cd server && pip install -r requirements.txt && python server.py`
- [ ] Posting a simulated reading to `/api/reading` (3+ anchors) still
      produces a correct trilaterated position in `/api/state` (same
      math as before — do not change `rssi_to_distance` or
      `trilaterate`)
- [ ] The dashboard HTML loads and the browser console shows a
      successful Socket.IO connection (no JS errors)
- [ ] `render.yaml` is valid YAML and references match actual file
      paths in the repo
- [ ] README's new deployment section is accurate and matches the
      actual `render.yaml` service/database names
- [ ] No secrets (database URLs, API keys) are hardcoded anywhere —
      everything sensitive comes from environment variables

## Do NOT change

- The RSSI-to-distance formula or the trilateration math in
  `rssi_to_distance()` / `trilaterate()` — these are already tested
  and correct.
- `anchor/sx126x.py`, `anchor/tourist_node.py`, `anchor/calibrate.py`,
  `calibrate/fit_calibration.py` — these only run on hardware and are
  unaffected by this hosting/transport change.
- The visual design / SVG rendering in `index.html` — only swap the
  data delivery (polling → WebSocket), not the look of the dashboard.
