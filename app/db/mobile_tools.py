"""Persistent, device-scoped native command queue (PostgreSQL).

Call init_schema() from database startup after mobile_devices exists. No payload
is logged. Expired payloads are scrubbed by a periodic sweep (at most once per
_PURGE_INTERVAL_S per process) plus a targeted scrub whenever an expired command
is touched. Every read decides expiry from ``expires_at`` rather than from the
stored status, so a command that the sweep has not reached yet is still never
claimed, counted or reported as live.
"""
import json
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

from app.db.base import get_db


def now():
    return datetime.now(timezone.utc)


def _ts(value):
    """Coerce a stored timestamp to an aware datetime."""
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if value is not None and value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value


def init_schema():
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""CREATE TABLE IF NOT EXISTS mobile_tool_devices (
            device_id TEXT PRIMARY KEY REFERENCES mobile_devices(device_id) ON DELETE CASCADE,
            enabled BOOLEAN NOT NULL DEFAULT FALSE,
            tools TEXT NOT NULL DEFAULT '[]', heartbeat_at TIMESTAMPTZ,
            updated_at TIMESTAMPTZ NOT NULL)""")
        cur.execute("""CREATE TABLE IF NOT EXISTS mobile_tool_commands (
            id TEXT PRIMARY KEY, device_id TEXT NOT NULL REFERENCES mobile_devices(device_id) ON DELETE CASCADE,
            session_id TEXT NOT NULL, name TEXT NOT NULL, arguments TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('pending','claimed','completed','cancelled','expired')),
            result TEXT, created_at TIMESTAMPTZ NOT NULL, expires_at TIMESTAMPTZ NOT NULL,
            claimed_at TIMESTAMPTZ, completed_at TIMESTAMPTZ)""")
        cur.execute("""CREATE INDEX IF NOT EXISTS mobile_tool_pending_idx
            ON mobile_tool_commands(device_id, status, expires_at)""")


# The sweep below is table-wide, so running it on every queue access (including
# each device's claim() poll) made every poll write the whole table. It carries
# no correctness weight — the queries below all filter on expires_at — so it is
# throttled to one run per process per interval.
_PURGE_INTERVAL_S = 60
_last_purge_monotonic = None
_purge_lock = threading.Lock()


def _reset_purge_throttle():
    """Test hook: make the next _purge() call run instead of being skipped."""
    global _last_purge_monotonic
    _last_purge_monotonic = None


def _purge(cur, at, force=False):
    global _last_purge_monotonic
    with _purge_lock:
        clock = time.monotonic()
        if (not force and _last_purge_monotonic is not None
                and clock - _last_purge_monotonic < _PURGE_INTERVAL_S):
            return
        cur.execute("""UPDATE mobile_tool_commands SET status='expired', arguments='{}', result=NULL
            WHERE expires_at <= %s AND status != 'expired'""", (at,))
        cur.execute("DELETE FROM mobile_tool_commands WHERE expires_at < %s", (at - timedelta(days=1),))
        _last_purge_monotonic = clock


def _scrub_expired(cur, command_id):
    """Apply the sweep's effect to one row the caller just found expired."""
    cur.execute("""UPDATE mobile_tool_commands SET status='expired', arguments='{}', result=NULL
        WHERE id=%s AND status != 'expired'""", (command_id,))


def capabilities(device_id, enabled, tools):
    at = now()
    with get_db() as conn:
        cur = conn.cursor()
        _purge(cur, at)
        cur.execute("""INSERT INTO mobile_tool_devices(device_id, enabled, tools, updated_at)
            VALUES (%s,%s,%s,%s) ON CONFLICT(device_id) DO UPDATE SET
            enabled=EXCLUDED.enabled, tools=EXCLUDED.tools, updated_at=EXCLUDED.updated_at,
            heartbeat_at=NULL
            WHERE mobile_tool_devices.enabled IS DISTINCT FROM EXCLUDED.enabled
               OR mobile_tool_devices.tools IS DISTINCT FROM EXCLUDED.tools
            RETURNING device_id""", (device_id, enabled, json.dumps(tools), at))
        if cur.fetchone() is None:
            return
        # Capability changes invalidate outstanding deliveries, including already claimed work.
        cur.execute("""UPDATE mobile_tool_commands SET status='cancelled', arguments='{}', result=NULL
            WHERE device_id=%s AND status IN ('pending','claimed') AND expires_at>%s""", (device_id, at))


def state(device_id):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM mobile_tool_devices WHERE device_id=%s", (device_id,))
        row = cur.fetchone()
        return dict(row) if row else None


def enqueue(device_id, session_id, name, arguments, ttl):
    at = now()
    with get_db() as conn:
        cur = conn.cursor()
        _purge(cur, at)
        # Serialize enqueue/capability updates/claims per device, and bound queue depth.
        cur.execute("SELECT * FROM mobile_tool_devices WHERE device_id=%s FOR UPDATE", (device_id,))
        row = cur.fetchone()
        if not row or not row['enabled']:
            return {'error': 'device_disabled'}
        heartbeat = row['heartbeat_at']
        if isinstance(heartbeat, str):
            heartbeat = datetime.fromisoformat(heartbeat)
        if not heartbeat or (at - heartbeat).total_seconds() > 30:
            return {'error': 'device_offline'}
        if name not in json.loads(row['tools']):
            return {'error': 'unsupported_tool'}
        cur.execute("""SELECT COUNT(*) AS n FROM mobile_tool_commands
            WHERE device_id=%s AND status IN ('pending','claimed') AND expires_at>%s""", (device_id, at))
        if cur.fetchone()['n'] >= 8:
            return {'error': 'device_busy'}
        command_id = str(uuid.uuid4())
        cur.execute("""INSERT INTO mobile_tool_commands
            (id,device_id,session_id,name,arguments,status,created_at,expires_at)
            VALUES (%s,%s,%s,%s,%s,'pending',%s,%s)""",
            (command_id, device_id, session_id, name, arguments, at, at + timedelta(seconds=ttl)))
        return {'id': command_id}


def claim(device_id):
    at = now()
    with get_db() as conn:
        cur = conn.cursor()
        _purge(cur, at)
        cur.execute("""UPDATE mobile_tool_devices SET heartbeat_at=%s
            WHERE device_id=%s AND enabled=TRUE RETURNING tools""", (at, device_id))
        row = cur.fetchone()
        if not row:
            return []
        cur.execute("""UPDATE mobile_tool_commands SET status='claimed', claimed_at=%s
            WHERE device_id=%s AND status='pending' AND expires_at>%s
            RETURNING id,name,arguments,expires_at,session_id""", (at, device_id, at))
        return [dict(r, arguments=json.loads(r['arguments'])) for r in cur.fetchall()]


def submit_result(device_id, command_id, result):
    at = now()
    with get_db() as conn:
        cur = conn.cursor()
        _purge(cur, at)
        cur.execute("SELECT * FROM mobile_tool_commands WHERE id=%s AND device_id=%s FOR UPDATE", (command_id, device_id))
        row = cur.fetchone()
        if not row:
            return 'not_found'
        # Expiry is decided from expires_at, not from the swept status, and the
        # row is scrubbed here so an expired payload never survives a touch.
        if row['status'] == 'expired' or _ts(row['expires_at']) <= at:
            _scrub_expired(cur, command_id)
            return 'expired'
        if row['status'] == 'completed':
            return None if row['result'] == result else 'result_conflict'
        if row['status'] != 'claimed':
            return 'not_claimed'
        cur.execute("""UPDATE mobile_tool_commands SET status='completed', result=%s,
            completed_at=%s, arguments='{}' WHERE id=%s""", (result, at, command_id))
        return None


def outcome(command_id, session_id):
    at = now()
    with get_db() as conn:
        cur = conn.cursor()
        _purge(cur, at)
        cur.execute("SELECT status,result,expires_at FROM mobile_tool_commands WHERE id=%s AND session_id=%s", (command_id, session_id))
        row = cur.fetchone()
        if not row:
            return None
        if _ts(row['expires_at']) <= at:
            # Same answer the sweep would have produced; the payload scrub is
            # left to the sweep so this poll stays a read.
            return {'status': 'expired', 'result': None}
        return {'status': row['status'], 'result': row['result']}


def cancel(command_id, session_id):
    with get_db() as conn:
        conn.cursor().execute("""UPDATE mobile_tool_commands SET status='cancelled', arguments='{}'
            WHERE id=%s AND session_id=%s AND status IN ('pending','claimed')""", (command_id, session_id))
