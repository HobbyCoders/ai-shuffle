"""Durable watches, atomic source cursors and an idempotent delivery outbox.

Source I/O never holds a database transaction. A compare-and-swap cursor admits
one polling generation; unique event keys deduplicate callbacks and polling.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone

from app.db.base import get_db, row_to_dict


def now():
    return datetime.now(timezone.utc)


def at(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def encode(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def parse(row):
    row = row_to_dict(row)
    if row:
        for key in ("source", "filter", "permissions", "cursor", "payload"):
            if isinstance(row.get(key), str):
                row[key] = json.loads(row[key])
    return row


def create_schema(cursor):
    cursor.execute("""CREATE TABLE IF NOT EXISTS watches (
        id TEXT PRIMARY KEY, owner TEXT NOT NULL, creating_session_id TEXT,
        worker_id TEXT NOT NULL, agent_id TEXT NOT NULL, description TEXT NOT NULL,
        source JSONB NOT NULL, filter JSONB NOT NULL, action TEXT NOT NULL,
        permissions JSONB NOT NULL, cursor JSONB NOT NULL DEFAULT '{}',
        daily_budget_usd DOUBLE PRECISION NOT NULL, status TEXT NOT NULL DEFAULT 'active',
        reason TEXT, last_fired_at TIMESTAMPTZ, cost_usd DOUBLE PRECISION NOT NULL DEFAULT 0,
        ends_at TIMESTAMPTZ, failures INTEGER NOT NULL DEFAULT 0,
        revision INTEGER NOT NULL DEFAULT 0, creator_notified BOOLEAN NOT NULL DEFAULT FALSE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""")
    cursor.execute("""CREATE TABLE IF NOT EXISTS watch_events (
        id TEXT PRIMARY KEY, watch_id TEXT NOT NULL REFERENCES watches(id) ON DELETE CASCADE,
        event_key TEXT NOT NULL, payload JSONB NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
        session_id TEXT, turn_id TEXT, command_id TEXT, route TEXT,
        reserved_usd DOUBLE PRECISION NOT NULL DEFAULT 0, cost_usd DOUBLE PRECISION,
        summary TEXT, summary_attempted BOOLEAN NOT NULL DEFAULT FALSE,
        notification_sent BOOLEAN NOT NULL DEFAULT FALSE,
        deadline_at TIMESTAMPTZ, admitted_at TIMESTAMPTZ,
        lease_until TIMESTAMPTZ, lease_owner TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        UNIQUE(watch_id, event_key)
    )""")
    from app.db.base import column_exists
    for column, ddl in (("command_id","TEXT"),("admitted_at","TIMESTAMPTZ"),("lease_until","TIMESTAMPTZ"),("lease_owner","TEXT")):
        if not column_exists(cursor, "watch_events", column):
            cursor.execute(f"ALTER TABLE watch_events ADD COLUMN {column} {ddl}")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_watch_events_pending ON watch_events(watch_id,status)")


def create(*, owner, creating_session_id, worker_id, agent_id, description,
           source, filter, action, permissions, daily_budget_usd, ends_at=None,
           watch_id=None, cursor=None, status="active", conn=None):
    from contextlib import nullcontext
    wid = watch_id or str(uuid.uuid4())
    with (nullcontext(conn) if conn is not None else get_db()) as connection:
        c = connection.cursor()
        c.execute("""INSERT INTO watches
            (id,owner,creating_session_id,worker_id,agent_id,description,source,filter,action,
             permissions,daily_budget_usd,ends_at,cursor,status)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(id) DO NOTHING""",
            (wid,owner,creating_session_id,worker_id,agent_id,description,encode(source),encode(filter),
             action,encode(permissions),daily_budget_usd,ends_at,encode(cursor or {}),status))
    return wid


def get(wid):
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM watches WHERE id=%s", (wid,))
        return parse(c.fetchone())


def list_watches(owner="admin", worker_id=None):
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM watches WHERE owner=%s" + (" AND worker_id=%s" if worker_id else "") +
                  " ORDER BY created_at,id", (owner,worker_id) if worker_id else (owner,))
        return [parse(row) for row in c.fetchall()]


def set_status(wid, owner, status, reason=None):
    if status not in ("active", "paused", "stopped"):
        raise ValueError("Unknown watch status")
    with get_db() as conn:
        c = conn.cursor()
        # Stop is terminal: a new confirmation is needed to create another watch.
        c.execute("UPDATE watches SET status=%s,reason=%s,revision=revision+1,updated_at=NOW() "
                  "WHERE id=%s AND owner=%s AND status <> 'stopped'", (status,reason,wid,owner))
        changed = c.rowcount
        if status != "active":
            c.execute("UPDATE watch_events SET status='cancelled' WHERE watch_id=%s AND status='pending'", (wid,))
    row = get(wid)
    if not row or row["owner"] != owner:
        raise ValueError("Watch does not exist")
    return row


def event_id(wid, key):
    return hashlib.sha256((wid + "\0" + key).encode()).hexdigest()


def _insert(c, watch, events):
    for key, payload in events:
        c.execute("""INSERT INTO watch_events (id,watch_id,event_key,payload)
            VALUES (%s,%s,%s,%s) ON CONFLICT(watch_id,event_key) DO NOTHING""",
            (event_id(watch["id"],key),watch["id"],key,encode(payload)))


def append_events(watch, events):
    with get_db() as conn:
        c = conn.cursor()
        c.execute("UPDATE watches SET revision=revision+1 WHERE id=%s AND status='active'", (watch['id'],))
        if c.rowcount:
            _insert(c, watch, events)


def checkpoint(watch, events, cursor):
    """Inbox and bookmark commit together; a raced/paused poll cannot advance."""
    with get_db() as conn:
        c = conn.cursor()
        c.execute("UPDATE watches SET cursor=%s,revision=revision+1,updated_at=NOW() "
                  "WHERE id=%s AND revision=%s AND status='active'",
                  (encode(cursor),watch["id"],watch["revision"]))
        if not c.rowcount:
            return False
        _insert(c, watch, events)
    return True


def events(wid, statuses=("pending", "admitted", "running")):
    with get_db() as conn:
        c = conn.cursor()
        marks = ','.join(['%s'] * len(statuses))
        c.execute(f"SELECT * FROM watch_events WHERE watch_id=%s AND status IN ({marks}) ORDER BY created_at,id",
                  (wid,*statuses))
        return [parse(row) for row in c.fetchall()]


def update_event(eid, **values):
    allowed = {"status", "session_id", "turn_id", "route", "cost_usd", "summary", "summary_attempted", "notification_sent"}
    if not values or not values.keys() <= allowed:
        raise ValueError("Invalid event update")
    with get_db() as conn:
        c = conn.cursor()
        c.execute("UPDATE watch_events SET " + ','.join(f'{k}=%s' for k in values) + " WHERE id=%s",
                  (*values.values(),eid))


def reserve(wid, eid, reserve_usd, *, max_per_hour=20):
    """Serialize budget/flood admission on the watch row, including in-flight cost."""
    with get_db() as conn:
        c = conn.cursor()
        c.execute("UPDATE watches SET revision=revision+1 WHERE id=%s AND status='active'", (wid,))
        if not c.rowcount:
            return False
        c.execute("SELECT * FROM watches WHERE id=%s", (wid,))
        watch = parse(c.fetchone())
        c.execute("SELECT status FROM watch_events WHERE id=%s AND watch_id=%s", (eid,wid))
        candidate = c.fetchone()
        if not candidate or candidate['status'] != 'pending':
            return False
        c.execute("SELECT * FROM watch_events WHERE watch_id=%s AND status IN ('admitted','running','delivered')", (wid,))
        rows = [parse(r) for r in c.fetchall()]
        stamp = now()
        spent = sum(float(r['cost_usd'] if r['cost_usd'] is not None else r['reserved_usd']) for r in rows
                    if r['status'] != 'delivered' or at(r.get('admitted_at') or r['created_at']) > stamp-timedelta(days=1))
        count = sum(at(r.get('admitted_at') or r['created_at']) > stamp-timedelta(hours=1) for r in rows)
        reason = None
        if watch.get('ends_at') and at(watch['ends_at']) <= stamp:
            reason = 'End date reached'
        elif count >= max_per_hour:
            reason = 'Event flood: hourly firing limit reached'
        elif spent + reserve_usd > watch['daily_budget_usd']:
            reason = 'Daily budget reached (including reserved in-flight cost)'
        if reason:
            c.execute("UPDATE watches SET status='paused',reason=%s WHERE id=%s", (reason,wid))
            return False
        c.execute("UPDATE watch_events SET status='admitted',reserved_usd=%s,deadline_at=%s,admitted_at=%s "
                  "WHERE id=%s AND watch_id=%s AND status='pending'",
                  (reserve_usd,stamp+timedelta(seconds=180),stamp,eid,wid))
        if not c.rowcount:
            return False
        c.execute("UPDATE watches SET last_fired_at=%s WHERE id=%s", (stamp,wid))
        return True


def settle(eid, cost):
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM watch_events WHERE id=%s", (eid,))
        event = parse(c.fetchone())
        if not event or event['status'] == 'delivered':
            return
        c.execute("UPDATE watch_events SET status='delivered',cost_usd=%s WHERE id=%s AND status <> 'delivered'", (cost,eid))
        if c.rowcount:
            c.execute("UPDATE watches SET cost_usd=cost_usd+%s WHERE id=%s", (cost,event['watch_id']))


def claim_delivery(eid, token):
    with get_db() as conn:
        c = conn.cursor()
        c.execute("UPDATE watch_events SET lease_until=%s,lease_owner=%s WHERE id=%s AND status IN ('admitted','running') "
                  "AND (lease_until IS NULL OR lease_until<%s OR lease_owner=%s)",
                  (now()+timedelta(seconds=60),token,eid,now(),token))
        return bool(c.rowcount)


def release_delivery(eid, token):
    with get_db() as conn:
        conn.cursor().execute("UPDATE watch_events SET lease_until=NULL,lease_owner=NULL WHERE id=%s AND lease_owner=%s",(eid,token))


def assign_route(eid, route, sid):
    with get_db() as conn:
        c = conn.cursor()
        c.execute("UPDATE watch_events SET route=%s,session_id=%s WHERE id=%s AND route IS NULL",(route,sid,eid))
        c.execute("SELECT * FROM watch_events WHERE id=%s",(eid,))
        return parse(c.fetchone())


def mark_creator_notified(wid):
    with get_db() as conn:
        conn.cursor().execute("UPDATE watches SET creator_notified=TRUE WHERE id=%s",(wid,))


def failure(wid, message):
    with get_db() as conn:
        c = conn.cursor()
        c.execute("UPDATE watches SET failures=failures+1,reason=%s WHERE id=%s", (str(message)[:500],wid))
        c.execute("UPDATE watches SET status='paused',reason=%s WHERE id=%s AND failures>=3 AND status='active'",
                  ('Repeated source/delivery failures: '+str(message)[:400],wid))
