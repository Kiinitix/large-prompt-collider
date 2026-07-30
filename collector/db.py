"""
Storage backend abstraction for the collector.

Supports SQLite (default, zero setup, fine for testing / small fleets) and
Postgres (set DB_BACKEND=postgres) for when event volume grows past what
SQLite handles comfortably. store_event() is the single write path, and
the query helpers below are the only places that need backend-specific SQL
-- mostly just placeholder style ('?' vs '%s') and a couple of dialect
differences (INSERT OR REPLACE vs ON CONFLICT).
"""
import os
import json
import sqlite3
import threading
import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("genai-collector.db")

DB_BACKEND = os.getenv("DB_BACKEND", "sqlite").lower()  # "sqlite" | "postgres"
DB_PATH = os.getenv("DB_PATH", "/data/events.db")  # sqlite only

PG_HOST = os.getenv("PG_HOST", "postgres")
PG_PORT = int(os.getenv("PG_PORT", "5432"))
PG_DB = os.getenv("PG_DB", "genai_tracker")
PG_USER = os.getenv("PG_USER", "genai_tracker")
PG_PASSWORD = os.getenv("PG_PASSWORD", "")

COLUMNS = [
    "event_id", "correlation_id", "device_id", "direction", "timestamp",
    "provider", "model", "endpoint", "method", "status_code", "latency_ms",
    "request_bytes", "response_bytes", "prompt_tokens", "completion_tokens",
    "total_tokens", "body_preview", "error", "rate_limited",
    "estimated_cost_usd", "raw_json", "received_at",
]
_REAL_COLS = {
    "status_code", "latency_ms", "request_bytes", "response_bytes",
    "prompt_tokens", "completion_tokens", "total_tokens", "estimated_cost_usd",
}
_BOOL_COLS = {"rate_limited"}

_lock = threading.Lock()
_pg_pool = None  # lazily created psycopg2 connection pool, postgres only


def _placeholder() -> str:
    return "%s" if DB_BACKEND == "postgres" else "?"


def _pg_conn():
    global _pg_pool
    import psycopg2
    from psycopg2 import pool as pg_pool_mod
    if _pg_pool is None:
        _pg_pool = pg_pool_mod.SimpleConnectionPool(
            1, 10, host=PG_HOST, port=PG_PORT, dbname=PG_DB,
            user=PG_USER, password=PG_PASSWORD,
        )
    return _pg_pool.getconn()


def _pg_release(conn):
    if _pg_pool is not None:
        _pg_pool.putconn(conn)


def get_conn():
    if DB_BACKEND == "postgres":
        return _pg_conn()
    return sqlite3.connect(DB_PATH, check_same_thread=False)


def release_conn(conn):
    if DB_BACKEND == "postgres":
        _pg_release(conn)
    else:
        conn.close()


def _col_def(col: str) -> str:
    if col in _BOOL_COLS:
        return f"{col} {'BOOLEAN' if DB_BACKEND == 'postgres' else 'INTEGER'}"
    if col in _REAL_COLS:
        return f"{col} REAL"
    return f"{col} TEXT"


def _existing_columns(cur) -> set:
    if DB_BACKEND == "postgres":
        cur.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'events'"
        )
        return {r[0] for r in cur.fetchall()}
    cur.execute("PRAGMA table_info(events)")
    return {r[1] for r in cur.fetchall()}


def _migrate_missing_columns(cur) -> None:
    """Add any columns that COLUMNS defines but an existing `events` table
    (created by an older version of this code, e.g. before cost tracking
    or rate-limit tracking were added) doesn't have yet. CREATE TABLE IF
    NOT EXISTS only creates the table if it's missing entirely -- it won't
    retroactively add columns to a table that already exists, which is
    exactly what happens when you pull an update but keep an existing
    collector-data volume. Without this, endpoints referencing the new
    column (e.g. /stats referencing estimated_cost_usd) fail with
    'no such column' after an upgrade."""
    existing = _existing_columns(cur)
    missing = [c for c in COLUMNS if c not in existing]
    for col in missing:
        logger.info("Migrating events table: adding missing column '%s'", col)
        cur.execute(f"ALTER TABLE events ADD COLUMN {_col_def(col)}")


def init_db() -> None:
    if DB_BACKEND != "postgres":
        os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    with _lock:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS events (
                {", ".join(_col_def(c) for c in COLUMNS)},
                PRIMARY KEY (event_id)
            )
        """)
        _migrate_missing_columns(cur)
        # anomalies table -- populated by the anomaly-detection pass
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS anomalies (
                id {"SERIAL" if DB_BACKEND == "postgres" else "INTEGER PRIMARY KEY AUTOINCREMENT"}
                    {"PRIMARY KEY" if DB_BACKEND == "postgres" else ""},
                device_id TEXT, kind TEXT, detail TEXT, detected_at TEXT
            )
        """)
        conn.commit()
        cur.close()
        release_conn(conn)
    logger.info("DB initialized (backend=%s)", DB_BACKEND)


def store_event(payload: dict, estimated_cost_usd: Optional[float]) -> None:
    ph = _placeholder()
    values = [payload.get(c) for c in COLUMNS[:-2]]  # all but raw_json, received_at
    # rate_limited / estimated_cost_usd aren't in the raw payload lookup above
    # for sqlite bool storage, coerce to int
    row = dict(zip(COLUMNS[:-2], values))
    row["estimated_cost_usd"] = estimated_cost_usd
    if DB_BACKEND != "postgres":
        row["rate_limited"] = int(bool(row.get("rate_limited")))
    full_values = [row.get(c) for c in COLUMNS[:-2]] + [
        json.dumps(payload), datetime.now(timezone.utc).isoformat(),
    ]
    with _lock:
        conn = get_conn()
        cur = conn.cursor()
        if DB_BACKEND == "postgres":
            cur.execute(
                f"INSERT INTO events ({', '.join(COLUMNS)}) "
                f"VALUES ({', '.join([ph] * len(COLUMNS))}) "
                f"ON CONFLICT (event_id) DO NOTHING",
                full_values,
            )
        else:
            cur.execute(
                f"INSERT OR REPLACE INTO events ({', '.join(COLUMNS)}) "
                f"VALUES ({', '.join([ph] * len(COLUMNS))})",
                full_values,
            )
        conn.commit()
        cur.close()
        release_conn(conn)


def record_anomaly(device_id: str, kind: str, detail: str) -> None:
    ph = _placeholder()
    with _lock:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            f"INSERT INTO anomalies (device_id, kind, detail, detected_at) "
            f"VALUES ({ph}, {ph}, {ph}, {ph})",
            (device_id, kind, detail, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        cur.close()
        release_conn(conn)


def rate_limited_true() -> str:
    """SQL fragment for 'rate_limited is true', valid on both backends
    since they store the column differently (INTEGER 0/1 on SQLite,
    BOOLEAN on Postgres)."""
    return "rate_limited = TRUE" if DB_BACKEND == "postgres" else "rate_limited = 1"


def query(sql: str, params: tuple = ()) -> list[dict]:
    """Run a SELECT and return rows as a list of dicts. `sql` should use
    '?' placeholders regardless of backend -- they're translated here."""
    if DB_BACKEND == "postgres":
        sql = sql.replace("?", "%s")
    with _lock:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(sql, params)
        cols = [c[0] for c in cur.description] if cur.description else []
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        cur.close()
        release_conn(conn)
    return rows
