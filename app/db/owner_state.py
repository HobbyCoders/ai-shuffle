"""Owner introduction, legacy account-wire compatibility and remote job spend.

Research source/run/event tables are migrated to watches and removed. The
remaining historical table names are still used by identity, default git author
and the owner's cross-worker spending limit; they are not a research loop.
"""

from __future__ import annotations
from typing import Any

import json
import uuid
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone

from app.db.base import get_db, row_to_dict, column_exists

AVATAR_PRESETS = ("robot", "spark", "orbit", "sprout", "pixel",
                  "owl", "fox", "cat", "bear", "ghost", "cloud",
                  "moon", "sun", "bolt", "gem", "drop",
                  "king", "queen", "jack", "joker")
ACCOUNT_ID = "owner"
from app.core.owner_compat import RETIRED_AGENT_ID

AGENT_ID = RETIRED_AGENT_ID  # Mixed-version tombstone, never a live agent.
ACCOUNT_DEFAULTS = dict(agent_id=None, enabled=False, paused=False, sources={}, introduction={},
                        home_session_id=None, home_device_id=None, home_created_at=None,
                        assistant_name=None, avatar_id=None)
DEFAULTS = dict(agent_id=None, enabled=False, paused=False, introduction={})

def now():
    return datetime.now(timezone.utc)


def create_schema(cursor: Any) -> None:
    cursor.execute("""CREATE TABLE IF NOT EXISTS assistant_state (
        id TEXT PRIMARY KEY, runner_id TEXT NOT NULL,
        agent_id TEXT REFERENCES agents(id) ON DELETE SET NULL,
        home_session_id TEXT, enabled BOOLEAN NOT NULL DEFAULT FALSE,
        paused BOOLEAN NOT NULL DEFAULT FALSE,
        introduction JSONB NOT NULL DEFAULT '{}',
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""")
    # Synced (network collection "assistant"). No foreign keys: a peer's row
    # can name an agent or a home session that has not reached this worker yet.
    cursor.execute("""CREATE TABLE IF NOT EXISTS assistant_account (
        id TEXT PRIMARY KEY, agent_id TEXT,
        enabled BOOLEAN NOT NULL DEFAULT FALSE, paused BOOLEAN NOT NULL DEFAULT FALSE,
        sources JSONB NOT NULL DEFAULT '{}', introduction JSONB NOT NULL DEFAULT '{}',
        home_session_id TEXT, home_device_id TEXT, home_created_at TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""")
    # Historical presentation columns retained for old-peer account rows.
    # They never create a dedicated agent or a hidden home.
    for column in ("assistant_name", "avatar_id"):
        if not column_exists(cursor, "assistant_account", column):
            cursor.execute(f"ALTER TABLE assistant_account ADD COLUMN {column} TEXT")
    cursor.execute("""CREATE TABLE IF NOT EXISTS assistant_remote_jobs (
        assistant_id TEXT NOT NULL REFERENCES assistant_state(id) ON DELETE CASCADE,
        session_id TEXT NOT NULL, device_id TEXT NOT NULL, kind TEXT NOT NULL,
        home_session_id TEXT, label TEXT,
        reported_usd DOUBLE PRECISION NOT NULL DEFAULT 0,
        answered_at TIMESTAMPTZ, idle_since TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL,
        PRIMARY KEY (assistant_id, session_id)
    )""")
    cursor.execute("""CREATE TABLE IF NOT EXISTS assistant_remote_costs (
        id TEXT PRIMARY KEY, assistant_id TEXT NOT NULL, session_id TEXT NOT NULL,
        cost_usd DOUBLE PRECISION NOT NULL, booked_at TIMESTAMPTZ NOT NULL
    )""")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_assistant_remote_costs_booked ON assistant_remote_costs(assistant_id, booked_at)")


def _parse(row):
    row = row_to_dict(row)
    if row:
        for key in ("watermark", "payload", "event_ids", "source_ids", "introduction", "limits", "sources"):
            if isinstance(row.get(key), str):
                row[key] = json.loads(row[key])
    return row


def lock_state(conn, assistant_id):
    cursor = conn.cursor()
    cursor.execute("UPDATE assistant_state SET updated_at=updated_at WHERE id=%s RETURNING *", (assistant_id,))
    return _parse(cursor.fetchone())


def get_state(assistant_id):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM assistant_state WHERE id=%s", (assistant_id,))
        return _parse(cursor.fetchone())


def configure(assistant_id, changes, _conn=None):
    if set(changes) - set(DEFAULTS):
        raise ValueError("Unknown assistant setting")
    with (nullcontext(_conn) if _conn is not None else get_db()) as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT INTO assistant_state (id, runner_id) VALUES (%s,%s) ON CONFLICT DO NOTHING", (assistant_id, assistant_id))
        state = lock_state(conn, assistant_id)
        if changes:
            changes = dict(changes)
            if "introduction" in changes:
                changes["introduction"] = json.dumps(changes["introduction"])
            cursor.execute("UPDATE assistant_state SET " + ", ".join(key + "=%s" for key in changes) +
                           ", updated_at=NOW() WHERE id=%s RETURNING *", (*changes.values(), assistant_id))
            state = _parse(cursor.fetchone())
        return state


def get_account(_conn=None):
    with (nullcontext(_conn) if _conn is not None else get_db()) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM assistant_account WHERE id=%s", (ACCOUNT_ID,))
        return _parse(cursor.fetchone())


def lock_account(conn):
    """Lock and return the account row, or None when nothing was configured."""
    cursor = conn.cursor()
    cursor.execute("UPDATE assistant_account SET updated_at=updated_at WHERE id=%s RETURNING *", (ACCOUNT_ID,))
    return _parse(cursor.fetchone())


def write_account(conn, changes):
    """Create the account row if needed, apply ``changes`` and return the row."""
    if set(changes) - set(ACCOUNT_DEFAULTS):
        raise ValueError("Unknown assistant account setting")
    cursor = conn.cursor()
    cursor.execute("INSERT INTO assistant_account (id) VALUES (%s) ON CONFLICT DO NOTHING", (ACCOUNT_ID,))
    if not changes:
        return lock_account(conn)
    changes = {key: json.dumps(value) if key in ("sources", "introduction") else value
               for key, value in changes.items()}
    cursor.execute("UPDATE assistant_account SET " + ", ".join(key + "=%s" for key in changes) +
                   ", updated_at=NOW() WHERE id=%s RETURNING *", (*changes.values(), ACCOUNT_ID))
    return _parse(cursor.fetchone())


def project(conn: Any, assistant_id: str, values: Any, sources: Any = None) -> Any:
    """Project introduction/identity only. Retired source opt-ins are ignored."""
    return configure(assistant_id, {k: v for k, v in values.items() if k in DEFAULTS}, _conn=conn)


def owner_connector(conn, connector):
    """Normal owner connectors use auto-created integration API credentials.

    A connector API user is not its owner: the owner-only connector API creates
    a dedicated, non-login user with this exact durable provenance. Existing
    regular API-user connections stay outside the owner assistant scope.
    """
    user_id = connector.get("api_user_id")
    if user_id is None:
        return True
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM api_users WHERE id=%s", (user_id,))
    user = cursor.fetchone()
    return bool(user and not user["web_login_allowed"] and
                user["description"] == "Auto-created API user for connector " + connector["id"])


def connector_grant_available(conn, connector, agent_id):
    if not owner_connector(conn, connector):
        return False
    user_id = connector.get("api_user_id")
    if user_id is None:
        return True
    cursor = conn.cursor()
    cursor.execute("SELECT is_active,profile_id FROM api_users WHERE id=%s", (user_id,))
    user = cursor.fetchone()
    if not user or not user["is_active"]:
        return False
    cursor.execute("SELECT profile_id FROM api_user_profiles WHERE api_user_id=%s", (user_id,))
    allowed = {row["profile_id"] for row in cursor.fetchall()}
    if user["profile_id"]:
        allowed.add(user["profile_id"])
    return not allowed or agent_id in allowed


def link_remote_job(assistant_id, session_id, device_id, kind, label=None, *, starter_session_id=None):
    """Link a job a coordination chat started on worker ``device_id``. Idempotent.

    ``home_session_id`` on the row records ``starter_session_id``, the chat
    that started it.
    """
    if not starter_session_id:
        raise ValueError("A remote job needs the chat that started it")
    with get_db() as conn:
        # The runner row anchors the job (its foreign key); create it if needed.
        lock_state(conn, assistant_id) or configure(assistant_id, {}, _conn=conn)
        report_to = starter_session_id
        cursor = conn.cursor()
        cursor.execute("INSERT INTO assistant_remote_jobs (assistant_id,session_id,device_id,kind,home_session_id,label,"
                       "created_at) VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (assistant_id,session_id) DO NOTHING",
                       (assistant_id, session_id, device_id, kind, report_to,
                        (label or None) and str(label)[:160], now()))
        cursor.execute("SELECT * FROM assistant_remote_jobs WHERE assistant_id=%s AND session_id=%s",
                       (assistant_id, session_id))
        return row_to_dict(cursor.fetchone())


def forget_remote_job(assistant_id, session_id):
    """Drop a link whose job provably never started (nothing booked for it)."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM assistant_remote_jobs WHERE assistant_id=%s AND session_id=%s AND reported_usd=0 "
                       "AND NOT EXISTS (SELECT 1 FROM assistant_remote_costs c WHERE c.assistant_id=%s "
                       "AND c.session_id=%s)", (assistant_id, session_id, assistant_id, session_id))


def get_remote_job(assistant_id, session_id):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM assistant_remote_jobs WHERE assistant_id=%s AND session_id=%s",
                       (assistant_id, session_id))
        return row_to_dict(cursor.fetchone())


def _at(value):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def remote_jobs_to_check(assistant_id, idle_for, at=None):
    """Linked remote jobs still worth asking about, oldest first.

    A job drops out once its worker has said it was idle, with nothing new
    spent, for ``idle_for``. A worker that cannot be reached never ages its
    jobs out, so its late usage is still collected when it comes back.
    """
    cutoff = (at or now()) - idle_for
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM assistant_remote_jobs WHERE assistant_id=%s ORDER BY created_at",
                       (assistant_id,))
        rows = [row_to_dict(row) for row in cursor.fetchall()]
    return [row for row in rows if _at(row["idle_since"]) is None or _at(row["idle_since"]) > cutoff]


def record_remote_usage(assistant_id, session_id, total_usd, *, active, at=None):
    """Book what a remote job spent since it was last heard from. Returns the new cost.

    ``total_usd`` is the job's cumulative cost as its worker reports it. Only
    the rise over the highest total seen so far is booked, once, at ``at``;
    a total that went down (an in-flight estimate settled lower) books
    nothing. ``active`` (running, or spent something) clears ``idle_since``.
    """
    at = at or now()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT reported_usd, idle_since FROM assistant_remote_jobs WHERE assistant_id=%s "
                       "AND session_id=%s FOR UPDATE", (assistant_id, session_id))
        row = row_to_dict(cursor.fetchone())
        if row is None:
            return 0.0
        previous = max(0.0, float(row["reported_usd"] or 0))
        total = max(0.0, float(total_usd or 0))
        added = total - previous if total > previous + 1e-9 else 0.0
        idle_since = None if (active or added) else (row["idle_since"] or at)
        cursor.execute("UPDATE assistant_remote_jobs SET reported_usd=%s, answered_at=%s, idle_since=%s "
                       "WHERE assistant_id=%s AND session_id=%s",
                       (max(previous, total), at, idle_since, assistant_id, session_id))
        if added:
            cursor.execute("INSERT INTO assistant_remote_costs (id,assistant_id,session_id,cost_usd,booked_at) "
                           "VALUES (%s,%s,%s,%s,%s)", (str(uuid.uuid4()), assistant_id, session_id, added, at))
        return added


def note_remote_unknown(assistant_id, session_id, at=None):
    """The worker answered but has no such job for us: it starts ageing out."""
    at = at or now()
    with get_db() as conn:
        conn.cursor().execute("UPDATE assistant_remote_jobs SET answered_at=%s, idle_since=COALESCE(idle_since,%s) "
                              "WHERE assistant_id=%s AND session_id=%s", (at, at, assistant_id, session_id))


def wake_remote_job(assistant_id, session_id):
    """The job runs again (a message resumed it): it is asked about again."""
    with get_db() as conn:
        conn.cursor().execute("UPDATE assistant_remote_jobs SET idle_since=NULL WHERE assistant_id=%s "
                              "AND session_id=%s", (assistant_id, session_id))

RETENTION_BATCH = 500


def retention_sweep(
    assistant_id: str, at: datetime | None = None, *, stop: Any = None
) -> dict[str, Any]:
    """Bounded cleanup still required by remote spending and job stops."""
    from app.db.retention import mutate_batches
    from app.db.spending_limits import STOP_RETENTION
    at = at or now()
    common = dict(stop=stop, batch_size=RETENTION_BATCH)
    return {
        "remote_costs_deleted": mutate_batches(
            "assistant_remote_costs",
            "assistant_id=%s AND booked_at < %s",
            (assistant_id, at - timedelta(days=35)),
            **common,
        ),
        "limit_stops_deleted": mutate_batches(
            "assistant_limit_stops",
            "assistant_id=%s AND created_at < %s",
            (assistant_id, at - STOP_RETENTION),
            keys=("turn_id",),
            **common,
        ),
    }
