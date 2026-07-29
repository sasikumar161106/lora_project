"""
db.py — Tourist Safety System Database Layer
=============================================
Persistent storage for anchor RSSI readings, position trail,
and SOS alert events using PostgreSQL.

If DATABASE_URL is not set, all DB calls are silently skipped
(the server runs fully in-memory — fine for local testing).

Schema changes vs original:
  position_trail: added tourist_id (TEXT), sos_flag (BOOLEAN)
  NEW TABLE: sos_events (tourist_id, x, y, timestamp)
"""

import os
import psycopg2
from psycopg2.extras import RealDictCursor
from contextlib import contextmanager

DATABASE_URL = os.environ.get("DATABASE_URL")


@contextmanager
def get_db_connection():
    conn = psycopg2.connect(DATABASE_URL)
    try:
        yield conn
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────
#  SCHEMA INIT
# ─────────────────────────────────────────────────────────────────────
def init_db():
    if not DATABASE_URL:
        print("[WARN] DATABASE_URL not set — DB persistence disabled.")
        return

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            # Anchor latest readings (one row per anchor)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS anchor_readings (
                    anchor_id   TEXT PRIMARY KEY,
                    rssi        FLOAT,
                    distance    FLOAT,
                    last_seen   DOUBLE PRECISION
                )
            """)

            # Tourist position trail (rolling window)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS position_trail (
                    id          SERIAL PRIMARY KEY,
                    x           FLOAT,
                    y           FLOAT,
                    t           DOUBLE PRECISION,
                    tourist_id  TEXT,
                    sos_flag    BOOLEAN DEFAULT FALSE
                )
            """)

            # Attempt to add new columns to existing tables (idempotent)
            for col, col_type, default in [
                ("tourist_id", "TEXT",    "NULL"),
                ("sos_flag",   "BOOLEAN", "FALSE"),
            ]:
                try:
                    cur.execute(f"""
                        ALTER TABLE position_trail
                        ADD COLUMN IF NOT EXISTS {col} {col_type} DEFAULT {default}
                    """)
                except Exception:
                    conn.rollback()

            # SOS events — one row per SOS activation
            cur.execute("""
                CREATE TABLE IF NOT EXISTS sos_events (
                    id          SERIAL PRIMARY KEY,
                    tourist_id  TEXT,
                    x           FLOAT,
                    y           FLOAT,
                    gps_lat     FLOAT,
                    gps_lng     FLOAT,
                    timestamp   DOUBLE PRECISION
                )
            """)

            conn.commit()
            print("[DB] Schema initialised.")


# ─────────────────────────────────────────────────────────────────────
#  ANCHOR READINGS
# ─────────────────────────────────────────────────────────────────────
def upsert_anchor_reading(anchor_id: str, rssi: float,
                           distance: float, timestamp: float):
    if not DATABASE_URL:
        return
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO anchor_readings (anchor_id, rssi, distance, last_seen)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (anchor_id) DO UPDATE
                SET rssi      = EXCLUDED.rssi,
                    distance  = EXCLUDED.distance,
                    last_seen = EXCLUDED.last_seen
            """, (anchor_id, rssi, distance, timestamp))
            conn.commit()


def get_all_anchor_readings() -> list:
    if not DATABASE_URL:
        return []
    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT anchor_id, rssi, distance, last_seen FROM anchor_readings"
            )
            return cur.fetchall()


# ─────────────────────────────────────────────────────────────────────
#  POSITION TRAIL
# ─────────────────────────────────────────────────────────────────────
def insert_position(x: float, y: float, timestamp: float,
                    tourist_id: str = None, sos_flag: bool = False):
    if not DATABASE_URL:
        return
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO position_trail (x, y, t, tourist_id, sos_flag)
                VALUES (%s, %s, %s, %s, %s)
            """, (x, y, timestamp, tourist_id, sos_flag))

            # Keep only the most recent 200 rows
            cur.execute("""
                DELETE FROM position_trail
                WHERE id NOT IN (
                    SELECT id FROM position_trail
                    ORDER BY id DESC
                    LIMIT 200
                )
            """)
            conn.commit()


def get_recent_trail(limit: int = 50) -> list:
    if not DATABASE_URL:
        return []
    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT x, y, t, tourist_id, sos_flag
                FROM (
                    SELECT x, y, t, tourist_id, sos_flag, id
                    FROM position_trail
                    ORDER BY id DESC
                    LIMIT %s
                ) AS recent
                ORDER BY id ASC
            """, (limit,))
            return cur.fetchall()


# ─────────────────────────────────────────────────────────────────────
#  SOS EVENTS
# ─────────────────────────────────────────────────────────────────────
def insert_sos_event(tourist_id: str, x: float, y: float,
                     gps_lat: float, gps_lng: float, timestamp: float):
    """
    Persist an SOS activation event with both local X,Y and computed GPS.
    Called by the server whenever a new SOS is first detected.
    """
    if not DATABASE_URL:
        return
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO sos_events
                    (tourist_id, x, y, gps_lat, gps_lng, timestamp)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (tourist_id, x, y, gps_lat, gps_lng, timestamp))
            conn.commit()


def get_recent_sos_events(limit: int = 20) -> list:
    """Return the most recent SOS events in reverse-chronological order."""
    if not DATABASE_URL:
        return []
    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT tourist_id, x, y, gps_lat, gps_lng, timestamp
                FROM sos_events
                ORDER BY timestamp DESC
                LIMIT %s
            """, (limit,))
            return cur.fetchall()
