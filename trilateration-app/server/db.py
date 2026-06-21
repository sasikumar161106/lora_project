import os
import psycopg2
from psycopg2.extras import RealDictCursor
from contextlib import contextmanager

# Read DATABASE_URL from environment variable
# e.g., postgres://user:password@hostname:port/dbname
DATABASE_URL = os.environ.get("DATABASE_URL")

@contextmanager
def get_db_connection():
    # If DATABASE_URL is not set, we cannot connect. The caller should handle this or fail early.
    conn = psycopg2.connect(DATABASE_URL)
    try:
        yield conn
    finally:
        conn.close()

def init_db():
    if not DATABASE_URL:
        print("[WARN] DATABASE_URL not set. Database persistence will not work.")
        return
        
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            # Create anchor_readings table
            cur.execute("""
                CREATE TABLE IF NOT EXISTS anchor_readings (
                    anchor_id TEXT PRIMARY KEY,
                    rssi FLOAT,
                    distance FLOAT,
                    last_seen DOUBLE PRECISION
                )
            """)
            
            # Create position_trail table
            cur.execute("""
                CREATE TABLE IF NOT EXISTS position_trail (
                    id SERIAL PRIMARY KEY,
                    x FLOAT,
                    y FLOAT,
                    t DOUBLE PRECISION
                )
            """)
            conn.commit()
            print("Database initialized successfully.")

def upsert_anchor_reading(anchor_id, rssi, distance, timestamp):
    if not DATABASE_URL:
        return
        
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO anchor_readings (anchor_id, rssi, distance, last_seen)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (anchor_id) DO UPDATE 
                SET rssi = EXCLUDED.rssi,
                    distance = EXCLUDED.distance,
                    last_seen = EXCLUDED.last_seen
            """, (anchor_id, rssi, distance, timestamp))
            conn.commit()

def get_all_anchor_readings():
    if not DATABASE_URL:
        return []
        
    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT anchor_id, rssi, distance, last_seen FROM anchor_readings")
            return cur.fetchall()

def insert_position(x, y, timestamp):
    if not DATABASE_URL:
        return
        
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            # Insert the new position
            cur.execute("""
                INSERT INTO position_trail (x, y, t)
                VALUES (%s, %s, %s)
            """, (x, y, timestamp))
            
            # Keep only the most recent 200 rows to avoid unbounded growth
            cur.execute("""
                DELETE FROM position_trail
                WHERE id NOT IN (
                    SELECT id FROM position_trail
                    ORDER BY id DESC
                    LIMIT 200
                )
            """)
            conn.commit()

def get_recent_trail(limit=50):
    if not DATABASE_URL:
        return []
        
    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Order by DESC to get newest, but then we want them in chronological order
            cur.execute("""
                SELECT x, y, t FROM (
                    SELECT x, y, t, id FROM position_trail
                    ORDER BY id DESC
                    LIMIT %s
                ) AS recent
                ORDER BY id ASC
            """, (limit,))
            return cur.fetchall()
