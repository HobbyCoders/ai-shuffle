"""Durable owner assistant configuration, observation inbox and linked activity.

The configuration row is also the per-install admission mutex. A guarded UPDATE
locks it on PostgreSQL (and serializes writers in the SQLite test shim). No
configuration or source is seeded by a migration.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone

from app.db.base import get_db, row_to_dict, column_exists

DEFAULTS = dict(agent_id=None, home_session_id=None, enabled=False, paused=False,
                max_runs_per_day=12, max_concurrent_runs=1, max_turns=3,
                timeout_seconds=180, max_cost_per_run=1.0, max_cost_per_day=5.0, introduction={})
AVATAR_PRESETS = ("robot", "spark", "orbit", "sprout", "pixel")
ACTIVE = ("admitted", "submitting", "running")
IN_FLIGHT = ACTIVE + ("cancelling",)


def now():
    return datetime.now(timezone.utc)


def create_schema(cursor):
    cursor.execute("""CREATE TABLE IF NOT EXISTS assistant_state (
        id TEXT PRIMARY KEY, runner_id TEXT NOT NULL,
        agent_id TEXT REFERENCES agents(id) ON DELETE SET NULL,
        home_session_id TEXT REFERENCES sessions(id) ON DELETE SET NULL,
        enabled BOOLEAN NOT NULL DEFAULT FALSE, paused BOOLEAN NOT NULL DEFAULT FALSE,
        max_runs_per_day INTEGER NOT NULL DEFAULT 12 CHECK (max_runs_per_day BETWEEN 1 AND 100),
        max_concurrent_runs INTEGER NOT NULL DEFAULT 1 CHECK (max_concurrent_runs BETWEEN 1 AND 3),
        max_turns INTEGER NOT NULL DEFAULT 3 CHECK (max_turns BETWEEN 1 AND 20),
        timeout_seconds INTEGER NOT NULL DEFAULT 180 CHECK (timeout_seconds BETWEEN 30 AND 900),
        max_cost_per_run DOUBLE PRECISION NOT NULL DEFAULT 1 CHECK (max_cost_per_run > 0 AND max_cost_per_run <= 100),
        max_cost_per_day DOUBLE PRECISION NOT NULL DEFAULT 5 CHECK (max_cost_per_day > 0 AND max_cost_per_day <= 1000),
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""")
    if not column_exists(cursor, "assistant_state", "introduction"):
        cursor.execute("ALTER TABLE assistant_state ADD COLUMN introduction JSONB NOT NULL DEFAULT '{}'")
    cursor.execute("""CREATE TABLE IF NOT EXISTS assistant_sources (
        assistant_id TEXT NOT NULL REFERENCES assistant_state(id) ON DELETE CASCADE,
        connector_id TEXT NOT NULL REFERENCES connectors(id) ON DELETE CASCADE,
        enabled BOOLEAN NOT NULL DEFAULT FALSE, watermark JSONB,
        last_observed_at TIMESTAMPTZ, last_error TEXT,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        PRIMARY KEY (assistant_id, connector_id)
    )""")
    cursor.execute("""CREATE TABLE IF NOT EXISTS assistant_runs (
        id TEXT PRIMARY KEY, assistant_id TEXT NOT NULL REFERENCES assistant_state(id) ON DELETE CASCADE,
        home_session_id TEXT REFERENCES sessions(id) ON DELETE SET NULL,
        session_id TEXT REFERENCES sessions(id) ON DELETE SET NULL,
        turn_id TEXT, kind TEXT NOT NULL DEFAULT 'proactive', status TEXT NOT NULL,
        event_ids JSONB NOT NULL DEFAULT '[]', source_ids JSONB NOT NULL DEFAULT '[]',
        deadline_at TIMESTAMPTZ, cost_usd DOUBLE PRECISION NOT NULL DEFAULT 0,
        error TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), ended_at TIMESTAMPTZ
    )""")
    for column, ddl in (("agent_id", "TEXT REFERENCES agents(id) ON DELETE SET NULL"),
                        ("limits", "JSONB NOT NULL DEFAULT '{}'")):
        if not column_exists(cursor, "assistant_runs", column):
            cursor.execute(f"ALTER TABLE assistant_runs ADD COLUMN {column} {ddl}")
    cursor.execute("""CREATE TABLE IF NOT EXISTS assistant_events (
        id TEXT PRIMARY KEY, assistant_id TEXT NOT NULL REFERENCES assistant_state(id) ON DELETE CASCADE,
        source_id TEXT NOT NULL, event_type TEXT NOT NULL, fingerprint TEXT NOT NULL,
        payload JSONB NOT NULL, trust TEXT NOT NULL DEFAULT 'external',
        status TEXT NOT NULL DEFAULT 'pending', run_id TEXT REFERENCES assistant_runs(id) ON DELETE SET NULL,
        observed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        UNIQUE (assistant_id, source_id, fingerprint)
    )""")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_assistant_events_pending ON assistant_events(assistant_id, status, observed_at)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_assistant_runs_status ON assistant_runs(assistant_id, status, created_at)")


def _parse(row):
    row = row_to_dict(row)
    if row:
        for key in ("watermark", "payload", "event_ids", "source_ids", "introduction", "limits"):
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


def get_sources(assistant_id):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM assistant_sources WHERE assistant_id=%s ORDER BY connector_id", (assistant_id,))
        return [_parse(row) for row in cursor.fetchall()]


def set_source(assistant_id, connector_id, enabled):
    with get_db() as conn:
        state = lock_state(conn, assistant_id)
        if not state:
            raise ValueError("Choose an assistant agent first")
        cursor = conn.cursor()
        cursor.execute("""INSERT INTO assistant_sources (assistant_id,connector_id,enabled)
            VALUES (%s,%s,%s) ON CONFLICT (assistant_id,connector_id) DO UPDATE SET
            enabled=EXCLUDED.enabled, updated_at=NOW() RETURNING *""", (assistant_id, connector_id, enabled))
        row = _parse(cursor.fetchone())
        if not enabled:
            cursor.execute("UPDATE assistant_events SET status='revoked' WHERE assistant_id=%s AND source_id=%s AND status='pending'", (assistant_id, connector_id))
        return row


def trusted_source(conn, state, connector_id):
    """Recheck the live connector grant, owner and runner inside admission/write."""
    cursor = conn.cursor()
    cursor.execute("""SELECT c.* FROM connectors c JOIN assistant_sources s ON s.connector_id=c.id
        WHERE s.assistant_id=%s AND c.id=%s AND s.enabled=TRUE AND c.is_active=TRUE
        AND c.api_user_id IS NULL AND c.runner_id=%s AND c.status='connected'""",
        (state["id"], connector_id, state["runner_id"]))
    return row_to_dict(cursor.fetchone())


def record_observations(assistant_id, connector_id, observations, watermark=None):
    """Atomic provenance/fingerprint insertion and reconciliation watermark.

    An observation is (event_type, external ID or None, bounded JSON payload).
    At most 50 are processed, and the pending inbox is capped at 100.
    """
    with get_db() as conn:
        state = lock_state(conn, assistant_id)
        if not state or not state["enabled"] or state["paused"] or not trusted_source(conn, state, connector_id):
            return []
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) AS n FROM assistant_events WHERE assistant_id=%s AND status='pending'", (assistant_id,))
        remaining = max(0, 100 - cursor.fetchone()["n"])
        inserted = []
        for event_type, external_id, payload in observations[:50]:
            encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False)
            if len(encoded) > 16000:
                raise ValueError("Observation payload too large")
            identity = str(external_id) if external_id else encoded
            fingerprint = hashlib.sha256((event_type + ":" + identity).encode()).hexdigest()
            if remaining == 0:
                break
            cursor.execute("""INSERT INTO assistant_events (id,assistant_id,source_id,event_type,fingerprint,payload)
                VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING *""",
                (str(uuid.uuid4()), assistant_id, connector_id, event_type, fingerprint, encoded))
            event = _parse(cursor.fetchone())
            if event:
                inserted.append(event)
                remaining -= 1
        cursor.execute("UPDATE assistant_sources SET last_observed_at=NOW(),last_error=%s WHERE assistant_id=%s AND connector_id=%s",
                       ("Observation inbox full; overflow not queued" if remaining == 0 else None, assistant_id, connector_id))
        if watermark is not None:
            cursor.execute("UPDATE assistant_sources SET watermark=%s WHERE assistant_id=%s AND connector_id=%s", (json.dumps(watermark), assistant_id, connector_id))
        return inserted


def source_error(assistant_id, connector_id):
    with get_db() as conn:
        conn.cursor().execute("UPDATE assistant_sources SET last_error=%s WHERE assistant_id=%s AND connector_id=%s",
                              ("Observation failed; check this connector's connection", assistant_id, connector_id))


def get_usage(assistant_id):
    day = now().replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) AS runs_today,COALESCE(SUM(cost_usd),0) AS reported_cost_today FROM assistant_runs WHERE assistant_id=%s AND kind='proactive' AND created_at >= %s", (assistant_id, day))
        usage = row_to_dict(cursor.fetchone())
        cursor.execute("SELECT COUNT(*) AS n FROM assistant_runs WHERE assistant_id=%s AND kind='proactive' AND status IN ('admitted','submitting','running','cancelling')", (assistant_id,))
        usage["in_flight_runs"] = cursor.fetchone()["n"]
        return usage


def get_activity(assistant_id, limit=50, active_only=False):
    with get_db() as conn:
        cursor = conn.cursor()
        sql = ("SELECT r.*,s.title AS session_title,s.status AS session_status,s.profile_id AS session_agent_id,"
               "a.name AS agent_name,a.config AS agent_config FROM assistant_runs r "
               "LEFT JOIN sessions s ON s.id=r.session_id LEFT JOIN agents a ON a.id=s.profile_id WHERE r.assistant_id=%s")
        if active_only:
            sql += " AND r.kind='proactive' AND r.status IN ('admitted','submitting','running','cancelling')"
        cursor.execute(sql + " ORDER BY r.created_at DESC LIMIT %s", (assistant_id, limit))
        result = []
        for raw in cursor.fetchall():
            row = _parse(raw)
            config = row.pop("agent_config") or {}
            if isinstance(config, str):
                config = json.loads(config)
            avatar = config.get("avatar_id", "robot")
            row["avatar_id"] = avatar if avatar in AVATAR_PRESETS else "robot"
            result.append(row)
        return result


def get_session_job(assistant_id, session_id):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM assistant_runs WHERE assistant_id=%s AND session_id=%s ORDER BY created_at DESC LIMIT 1", (assistant_id, session_id))
        return _parse(cursor.fetchone())


def get_run(run_id):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM assistant_runs WHERE id=%s", (run_id,))
        return _parse(cursor.fetchone())


def admit_run(assistant_id):
    """Coalesce <=10 events under the install mutex and reserve finite budgets."""
    with get_db() as conn:
        state = lock_state(conn, assistant_id)
        if not state or not state["enabled"] or state["paused"] or not state["agent_id"] or not state["home_session_id"]:
            return None
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) AS n FROM assistant_runs WHERE assistant_id=%s AND kind='proactive' AND status IN ('admitted','submitting','running','cancelling')", (assistant_id,))
        if cursor.fetchone()["n"] >= state["max_concurrent_runs"]:
            return None
        day = now().replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        cursor.execute("SELECT COUNT(*) AS n, COALESCE(SUM(cost_usd),0) AS cost FROM assistant_runs WHERE assistant_id=%s AND kind='proactive' AND created_at >= %s", (assistant_id, day))
        usage = cursor.fetchone()
        # Reserve the configured per-run amount for *every* live run. Provider
        # billing is reported after a run, so this is not an exact dollar cap.
        cursor.execute("SELECT limits FROM assistant_runs WHERE assistant_id=%s AND kind='proactive' AND status IN ('admitted','submitting','running','cancelling')", (assistant_id,))
        reserved = sum((_parse(row).get("limits") or {}).get("max_cost_per_run", state["max_cost_per_run"]) for row in cursor.fetchall())
        if usage["n"] >= state["max_runs_per_day"] or usage["cost"] + reserved + state["max_cost_per_run"] > state["max_cost_per_day"]:
            return None
        cursor.execute("SELECT * FROM assistant_events WHERE assistant_id=%s AND status='pending' ORDER BY observed_at,id LIMIT 10", (assistant_id,))
        events = [_parse(row) for row in cursor.fetchall()]
        eligible = []
        for event in events:
            if trusted_source(conn, state, event["source_id"]):
                eligible.append(event)
            else:
                cursor.execute("UPDATE assistant_events SET status='revoked' WHERE id=%s", (event["id"],))
        if not eligible:
            return None
        # Reserve admission BEFORE minting a normal session: create_session
        # may read the runner directory through a separate DB transaction.
        # Keeping that outside the mutex works on PostgreSQL and the shim.
        run_id = str(uuid.uuid4())
        event_ids = [e["id"] for e in eligible]
        source_ids = sorted({e["source_id"] for e in eligible})
        deadline = (now() + timedelta(seconds=state["timeout_seconds"])).isoformat()
        cursor.execute("""INSERT INTO assistant_runs (id,assistant_id,home_session_id,status,event_ids,source_ids,deadline_at,created_at,agent_id,limits)
            VALUES (%s,%s,%s,'admitted',%s,%s,%s,%s,%s,%s) RETURNING *""",
            (run_id, assistant_id, state["home_session_id"], json.dumps(event_ids), json.dumps(source_ids), deadline, now().isoformat(), state["agent_id"],
             json.dumps({k: state[k] for k in ("max_turns", "max_cost_per_run", "timeout_seconds")})))
        run = _parse(cursor.fetchone())
        for event in eligible:
            cursor.execute("UPDATE assistant_events SET status='admitted',run_id=%s WHERE id=%s AND status='pending'", (run_id, event["id"]))
        run["events"] = eligible
        return run


def bind_run_session(run_id, session_id):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE assistant_runs SET session_id=%s WHERE id=%s AND status='submitting' AND session_id IS NULL RETURNING *", (session_id, run_id))
        return _parse(cursor.fetchone())


def update_run(run_id, *, status=None, turn_id=None):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE assistant_runs SET status=COALESCE(%s,status),turn_id=COALESCE(%s,turn_id) WHERE id=%s AND status IN ('admitted','submitting','running','cancelling') RETURNING *", (status, turn_id, run_id))
        return _parse(cursor.fetchone())


def mark_cancelling(run_id, error):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE assistant_runs SET status='cancelling',error=%s WHERE id=%s AND status IN ('admitted','submitting','running') RETURNING *", (error, run_id))
        return _parse(cursor.fetchone())


def begin_dispatch(run_id):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE assistant_runs SET status='submitting' WHERE id=%s AND status='admitted' RETURNING *", (run_id,))
        return _parse(cursor.fetchone())


def settle_run(run_id, status, error=None, cost_usd=0):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE assistant_runs SET status=%s,error=%s,cost_usd=%s,ended_at=NOW() WHERE id=%s AND status IN ('admitted','submitting','running','cancelling') RETURNING *", (status, error, max(0, cost_usd), run_id))
        row = _parse(cursor.fetchone())
        if row:
            cursor.execute("UPDATE assistant_events SET status=%s WHERE run_id=%s", (status, run_id))
        return row


def link_job(assistant_id, session_id, kind="delegated"):
    with get_db() as conn:
        state = lock_state(conn, assistant_id)
        if not state or not state["home_session_id"]:
            raise ValueError("Assistant home is not configured")
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM assistant_runs WHERE assistant_id=%s AND session_id=%s AND kind=%s", (assistant_id, session_id, kind))
        existing = _parse(cursor.fetchone())
        if existing:
            return existing
        cursor.execute("INSERT INTO assistant_runs (id,assistant_id,home_session_id,session_id,kind,status) VALUES (%s,%s,%s,%s,%s,'linked') RETURNING *", (str(uuid.uuid4()), assistant_id, state["home_session_id"], session_id, kind))
        return _parse(cursor.fetchone())
