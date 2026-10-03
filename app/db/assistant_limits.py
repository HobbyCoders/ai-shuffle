"""The owner's spending and run-time limits for the persistent assistant.

One worker-local row per assistant runner (the same id as ``assistant_state``):
a daily spending limit for the home conversation and everything it starts,
and a run-time limit for one background job. Only the owner-only assistant
API writes it (see ``app.api.assistant``); no model-facing tool does.

``warned`` and ``reached`` hold ``"<day>|<limit>"`` for the last day the 80%
and limit-reached notices were posted, so each fires once per day and a new
limit set later that day arms them again.

``assistant_limit_stops`` remembers each turn the limits stopped, so a turn is
stopped and reported once.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.db.base import get_db, row_to_dict

DEFAULTS = dict(spend_limit_enabled=True, daily_spend_limit_usd=20.0,
                job_limit_enabled=True, max_job_minutes=120)
# Inclusive bounds the API and this module enforce.
SPEND_BOUNDS = (0.5, 10000.0)
MINUTE_BOUNDS = (5, 24 * 60)
NOTICE_COLUMNS = ("warned", "reached")
STOP_RETENTION = timedelta(days=30)


def create_schema(cursor):
    cursor.execute("""CREATE TABLE IF NOT EXISTS assistant_limits (
        id TEXT PRIMARY KEY,
        spend_limit_enabled BOOLEAN NOT NULL DEFAULT TRUE,
        daily_spend_limit_usd DOUBLE PRECISION NOT NULL DEFAULT 20,
        job_limit_enabled BOOLEAN NOT NULL DEFAULT TRUE,
        max_job_minutes INTEGER NOT NULL DEFAULT 120,
        warned TEXT, reached TEXT,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""")
    cursor.execute("""CREATE TABLE IF NOT EXISTS assistant_limit_stops (
        turn_id TEXT PRIMARY KEY, assistant_id TEXT NOT NULL,
        session_id TEXT NOT NULL, reason TEXT NOT NULL, label TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), reported_at TIMESTAMPTZ
    )""")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_assistant_limit_stops_unreported "
                   "ON assistant_limit_stops(assistant_id, reported_at)")


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
    limits["warned"] = row.get("warned")
    limits["reached"] = row.get("reached")
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


def claim_notice(assistant_id, column, key):
    """Mark notice ``column`` as posted for ``key``; False when it already was."""
    if column not in NOTICE_COLUMNS:
        raise ValueError("Unknown notice")
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT INTO assistant_limits (id) VALUES (%s) ON CONFLICT DO NOTHING", (assistant_id,))
        cursor.execute(f"UPDATE assistant_limits SET {column}=%s WHERE id=%s AND ({column} IS NULL OR {column}<>%s) "
                       "RETURNING id", (key, assistant_id, key))
        return cursor.fetchone() is not None


def release_notice(assistant_id, column, key):
    """Undo a claim whose post failed, so the next pass retries it."""
    if column not in NOTICE_COLUMNS:
        raise ValueError("Unknown notice")
    with get_db() as conn:
        conn.cursor().execute(f"UPDATE assistant_limits SET {column}=NULL WHERE id=%s AND {column}=%s",
                              (assistant_id, key))


def stopped(turn_id):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM assistant_limit_stops WHERE turn_id=%s", (turn_id,))
        return cursor.fetchone() is not None


def record_stop(assistant_id, session_id, turn_id, reason, label):
    """Remember a stopped turn; False when it was already recorded."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM assistant_limit_stops WHERE assistant_id=%s AND created_at < %s",
                       (assistant_id, datetime.now(timezone.utc) - STOP_RETENTION))
        cursor.execute("""INSERT INTO assistant_limit_stops (turn_id, assistant_id, session_id, reason, label)
            VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING turn_id""",
                       (turn_id, assistant_id, session_id, reason, label))
        return cursor.fetchone() is not None


def unreported_stops(assistant_id):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM assistant_limit_stops WHERE assistant_id=%s AND reported_at IS NULL "
                       "ORDER BY created_at, turn_id", (assistant_id,))
        return [row_to_dict(row) for row in cursor.fetchall()]


def mark_reported(turn_ids):
    if not turn_ids:
        return
    marks = ", ".join(["%s"] * len(turn_ids))
    with get_db() as conn:
        conn.cursor().execute(f"UPDATE assistant_limit_stops SET reported_at=NOW() WHERE turn_id IN ({marks})",
                              tuple(turn_ids))
