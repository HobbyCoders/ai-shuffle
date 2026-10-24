"""The owner's spending and run-time limits for coordination work.

One worker-local row per runner (the same id as ``assistant_state``): a daily
spending limit for coordination chats and everything they start, and a
run-time limit for one background job. Only the owner-only limits API writes
it (``/api/v1/owner/limits``); no model-facing tool does.

``assistant_limit_stops`` remembers each turn the limits stopped, so a turn is
stopped once.
"""
from __future__ import annotations

from datetime import timedelta

from app.db.base import get_db, row_to_dict

DEFAULTS = dict(spend_limit_enabled=True, daily_spend_limit_usd=20.0,
                job_limit_enabled=True, max_job_minutes=120)
# Inclusive bounds the API and this module enforce.
SPEND_BOUNDS = (0.5, 10000.0)
MINUTE_BOUNDS = (5, 24 * 60)
STOP_RETENTION = timedelta(days=30)


def create_schema(cursor):
    cursor.execute("""CREATE TABLE IF NOT EXISTS assistant_limits (
        id TEXT PRIMARY KEY,
        spend_limit_enabled BOOLEAN NOT NULL DEFAULT TRUE,
        daily_spend_limit_usd DOUBLE PRECISION NOT NULL DEFAULT 20,
        job_limit_enabled BOOLEAN NOT NULL DEFAULT TRUE,
        max_job_minutes INTEGER NOT NULL DEFAULT 120,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""")
    cursor.execute("""CREATE TABLE IF NOT EXISTS assistant_limit_stops (
        turn_id TEXT PRIMARY KEY, assistant_id TEXT NOT NULL,
        session_id TEXT NOT NULL, reason TEXT NOT NULL, label TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""")


def get(assistant_id):
    """The limits in force: the stored row, or the defaults when none was saved."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM assistant_limits WHERE id=%s", (assistant_id,))
        row = row_to_dict(cursor.fetchone()) or {}
    limits: dict = dict(DEFAULTS)
    for key in DEFAULTS:
        if row.get(key) is not None:
            limits[key] = row[key]
    limits["spend_limit_enabled"] = bool(limits["spend_limit_enabled"])
    limits["job_limit_enabled"] = bool(limits["job_limit_enabled"])
    limits["daily_spend_limit_usd"] = float(limits["daily_spend_limit_usd"])
    limits["max_job_minutes"] = int(limits["max_job_minutes"])
    return limits


def validate(changes):
    if set(changes) - set(DEFAULTS):
        raise ValueError("Unknown assistant limit")
    spend = changes.get("daily_spend_limit_usd")
    if spend is not None and not SPEND_BOUNDS[0] <= float(spend) <= SPEND_BOUNDS[1]:
        raise ValueError(f"Daily spending limit must be between ${SPEND_BOUNDS[0]:g} and ${SPEND_BOUNDS[1]:g}")
    minutes = changes.get("max_job_minutes")
    if minutes is not None and not MINUTE_BOUNDS[0] <= int(minutes) <= MINUTE_BOUNDS[1]:
        raise ValueError(f"Job time limit must be between {MINUTE_BOUNDS[0]} and {MINUTE_BOUNDS[1]} minutes")


def update(assistant_id, changes):
    """Apply the owner's changes and return the limits in force."""
    changes = {key: value for key, value in changes.items() if value is not None}
    validate(changes)
    if changes:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("INSERT INTO assistant_limits (id) VALUES (%s) ON CONFLICT DO NOTHING", (assistant_id,))
            cursor.execute("UPDATE assistant_limits SET " + ", ".join(key + "=%s" for key in changes) +
                           ", updated_at=NOW() WHERE id=%s", (*changes.values(), assistant_id))
    return get(assistant_id)


def stopped(turn_id):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM assistant_limit_stops WHERE turn_id=%s", (turn_id,))
        return cursor.fetchone() is not None


def record_stop(assistant_id, session_id, turn_id, reason, label):
    """Remember a stopped turn; False when it was already recorded."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""INSERT INTO assistant_limit_stops (turn_id, assistant_id, session_id, reason, label)
            VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING turn_id""",
                       (turn_id, assistant_id, session_id, reason, label))
        return cursor.fetchone() is not None
