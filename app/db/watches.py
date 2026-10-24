"""Durable watches, atomic source cursors and an idempotent delivery outbox.

Source I/O never holds a database transaction. A compare-and-swap cursor admits
one polling generation; unique event keys deduplicate callbacks and polling.
"""

from __future__ import annotations
from typing import Any
from datetime import datetime

import hashlib
import json
import uuid
from datetime import timedelta

from app.core.watch_values import now, at, encode
from app.db.base import get_db, row_to_dict


def parse(row: dict[str, Any]) -> dict[str, Any] | None:
    row = row_to_dict(row)
    if row:
        for key in ("source", "filter", "permissions", "cursor", "payload"):
            if isinstance(row.get(key), str):
                row[key] = json.loads(row[key])
    return row


def create_schema(cursor: Any) -> None:
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
    # Watches first ship with all columns above. Do not probe the hard-coded
    # public schema and ALTER this new table: migrations also create private
    # search_path schemas for atomic worker snapshots.
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_watch_events_pending ON watch_events(watch_id,status)"
    )


def create(
    *,
    owner: str,
    creating_session_id: Any,
    worker_id: str,
    agent_id: str,
    description: str,
    source: dict[str, Any],
    filter: dict[str, Any],
    action: str,
    permissions: dict[str, Any],
    daily_budget_usd: float,
    ends_at: datetime | None = None,
    watch_id: str | None = None,
    cursor: Any = None,
    status: str = "active",
    conn: Any = None,
) -> str:
    from contextlib import nullcontext

    wid = watch_id or str(uuid.uuid4())
    with nullcontext(conn) if conn is not None else get_db() as connection:
        c = connection.cursor()
        c.execute(
            """INSERT INTO watches
            (id,owner,creating_session_id,worker_id,agent_id,description,source,filter,action,
             permissions,daily_budget_usd,ends_at,cursor,status)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(id) DO NOTHING""",
            (
                wid,
                owner,
                creating_session_id,
                worker_id,
                agent_id,
                description,
                encode(source),
                encode(filter),
                action,
                encode(permissions),
                daily_budget_usd,
                ends_at,
                encode(cursor or {}),
                status,
            ),
        )
    return wid


def get(wid: str) -> dict[str, Any] | None:
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM watches WHERE id=%s", (wid,))
        return parse(c.fetchone())


def list_watches(owner: str = "admin", worker_id: str | None = None) -> list[dict[str, Any]]:
    with get_db() as conn:
        c = conn.cursor()
        c.execute(
            "SELECT * FROM watches WHERE owner=%s"
            + (" AND worker_id=%s" if worker_id else "")
            + " ORDER BY created_at,id",
            (owner, worker_id) if worker_id else (owner,),
        )
        return [parse(row) for row in c.fetchall()]


def set_status(wid: str, owner: str, status: str, reason: str | None = None) -> dict[str, Any]:
    if status not in ("active", "paused", "stopped"):
        raise ValueError("Unknown watch status")
    with get_db() as conn:
        c = conn.cursor()
        # Stop is terminal: a new confirmation is needed to create another watch.
        c.execute(
            "UPDATE watches SET status=%s,reason=%s,revision=revision+1,updated_at=NOW() "
            "WHERE id=%s AND owner=%s AND status <> 'stopped'",
            (status, reason, wid, owner),
        )
        changed = c.rowcount
        if status != "active":
            c.execute(
                "UPDATE watch_events SET status='cancelled' WHERE watch_id=%s AND status='pending'",
                (wid,),
            )
    row = get(wid)
    if not row or row["owner"] != owner:
        raise ValueError("Watch does not exist")
    return row


def event_id(wid: str, key: str) -> str:
    return hashlib.sha256((wid + "\0" + key).encode()).hexdigest()


def _insert(c: Any, watch: dict[str, Any], events: list[tuple[str, dict[str, Any]]]) -> None:
    for key, payload in events:
        c.execute(
            """INSERT INTO watch_events (id,watch_id,event_key,payload)
            VALUES (%s,%s,%s,%s) ON CONFLICT(watch_id,event_key) DO NOTHING""",
            (event_id(watch["id"], key), watch["id"], key, encode(payload)),
        )


def append_events(watch: dict[str, Any], events: list[tuple[str, dict[str, Any]]]) -> None:
    with get_db() as conn:
        c = conn.cursor()
        c.execute(
            "UPDATE watches SET revision=revision+1 WHERE id=%s AND status='active'", (watch["id"],)
        )
        if c.rowcount:
            _insert(c, watch, events)


def checkpoint(
    watch: dict[str, Any], events: list[tuple[str, dict[str, Any]]], cursor: dict[str, Any]
) -> bool:
    """Inbox and bookmark commit together; a raced/paused poll cannot advance."""
    with get_db() as conn:
        c = conn.cursor()
        c.execute(
            "UPDATE watches SET cursor=%s,revision=revision+1,updated_at=NOW() "
            "WHERE id=%s AND revision=%s AND status='active'",
            (encode(cursor), watch["id"], watch["revision"]),
        )
        if not c.rowcount:
            return False
        _insert(c, watch, events)
    return True


def events(
    wid: str, statuses: tuple[str, ...] = ("pending", "admitted", "running")
) -> list[dict[str, Any]]:
    with get_db() as conn:
        c = conn.cursor()
        marks = ",".join(["%s"] * len(statuses))
        c.execute(
            f"SELECT * FROM watch_events WHERE watch_id=%s AND status IN ({marks}) ORDER BY"
            " created_at,id",
            (wid, *statuses),
        )
        return [parse(row) for row in c.fetchall()]


def update_event(eid: str, **values: Any) -> None:
    allowed = {
        "status",
        "session_id",
        "turn_id",
        "route",
        "cost_usd",
        "summary",
        "summary_attempted",
        "notification_sent",
    }
    if not values or not values.keys() <= allowed:
        raise ValueError("Invalid event update")
    with get_db() as conn:
        c = conn.cursor()
        c.execute(
            "UPDATE watch_events SET " + ",".join(f"{k}=%s" for k in values) + " WHERE id=%s",
            (*values.values(), eid),
        )


def reserve(wid: str, eid: str, reserve_usd: float, *, max_per_hour: int = 20) -> bool:
    """Serialize budget/flood admission on the watch row, including in-flight cost."""
    with get_db() as conn:
        c = conn.cursor()
        c.execute("UPDATE watches SET revision=revision+1 WHERE id=%s AND status='active'", (wid,))
        if not c.rowcount:
            return False
        c.execute("SELECT * FROM watches WHERE id=%s", (wid,))
        watch = parse(c.fetchone())
        c.execute("SELECT status FROM watch_events WHERE id=%s AND watch_id=%s", (eid, wid))
        candidate = c.fetchone()
        if not candidate or candidate["status"] != "pending":
            return False
        c.execute(
            "SELECT * FROM watch_events WHERE watch_id=%s AND status IN"
            " ('admitted','running','delivered')",
            (wid,),
        )
        rows = [parse(r) for r in c.fetchall()]
        stamp = now()
        spent = sum(
            float(r["cost_usd"] if r["cost_usd"] is not None else r["reserved_usd"])
            for r in rows
            if r["status"] != "delivered"
            or at(r.get("admitted_at") or r["created_at"]) > stamp - timedelta(days=1)
        )
        count = sum(
            at(r.get("admitted_at") or r["created_at"]) > stamp - timedelta(hours=1) for r in rows
        )
        reason = None
        if watch.get("ends_at") and at(watch["ends_at"]) <= stamp:
            reason = "End date reached"
        elif count >= max_per_hour:
            reason = "Event flood: hourly firing limit reached"
        elif spent + reserve_usd > watch["daily_budget_usd"]:
            reason = "Daily budget reached (including reserved in-flight cost)"
        if reason:
            c.execute("UPDATE watches SET status='paused',reason=%s WHERE id=%s", (reason, wid))
            return False
        c.execute(
            "UPDATE watch_events SET"
            " status='admitted',reserved_usd=%s,deadline_at=%s,admitted_at=%s WHERE id=%s AND"
            " watch_id=%s AND status='pending'",
            (reserve_usd, stamp + timedelta(seconds=180), stamp, eid, wid),
        )
        if not c.rowcount:
            return False
        c.execute("UPDATE watches SET last_fired_at=%s WHERE id=%s", (stamp, wid))
        return True


def renew_admission(eid: str, token: str) -> dict | None:
    """Renew only a provably undispatched event, retaining its command and reservation."""
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT watch_id FROM watch_events WHERE id=%s", (eid,))
        identity = c.fetchone()
        if not identity:
            return None
        # Match reserve's lock order: watch before event.
        c.execute("SELECT * FROM watches WHERE id=%s FOR UPDATE", (identity["watch_id"],))
        watch = parse(c.fetchone())
        c.execute("SELECT * FROM watch_events WHERE id=%s FOR UPDATE", (eid,))
        event = parse(c.fetchone())
        if (
            not watch
            or watch["status"] != "active"
            or event["status"] != "admitted"
            or event["lease_owner"] != token
            or event.get("turn_id")
            or event.get("summary_attempted")
            or event.get("summary")
            or (event.get("notification_sent") and event.get("route") != "investigate")
            or event.get("cost_usd") is not None
        ):
            return None
        if event.get("session_id"):
            c.execute(
                "SELECT id FROM turns WHERE session_id=%s AND command_id=%s",
                (event["session_id"], event.get("command_id") or event["id"]),
            )
            if c.fetchone():
                return None
        stamp = now()
        c.execute(
            "SELECT * FROM watch_events WHERE watch_id=%s AND id<>%s "
            "AND status IN ('admitted','running','delivered')",
            (watch["id"], eid),
        )
        rows = [parse(row) for row in c.fetchall()]
        spent = sum(
            float(row["cost_usd"] if row["cost_usd"] is not None else row["reserved_usd"])
            for row in rows
            if row["status"] != "delivered"
            or at(row.get("admitted_at") or row["created_at"]) > stamp - timedelta(days=1)
        )
        if watch.get("ends_at") and at(watch["ends_at"]) <= stamp:
            reason = "End date reached"
        elif spent + event["reserved_usd"] > watch["daily_budget_usd"]:
            reason = "Daily budget reached (including reserved in-flight cost)"
        else:
            reason = None
        if reason:
            c.execute(
                "UPDATE watches SET status='paused',reason=%s WHERE id=%s", (reason, watch["id"])
            )
            return None
        c.execute(
            "UPDATE watch_events SET deadline_at=%s WHERE id=%s",
            (stamp + timedelta(seconds=180), eid),
        )
        c.execute("SELECT * FROM watch_events WHERE id=%s", (eid,))
        return parse(c.fetchone())


def settle(eid: str, cost: float) -> None:
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM watch_events WHERE id=%s", (eid,))
        event = parse(c.fetchone())
        if not event or event["status"] == "delivered":
            return
        c.execute(
            "UPDATE watch_events SET status='delivered',cost_usd=%s WHERE id=%s AND status <>"
            " 'delivered'",
            (cost, eid),
        )
        if c.rowcount:
            c.execute(
                "UPDATE watches SET cost_usd=cost_usd+%s WHERE id=%s", (cost, event["watch_id"])
            )


def claim_delivery(eid: str, token: str) -> bool:
    with get_db() as conn:
        c = conn.cursor()
        c.execute(
            "UPDATE watch_events SET lease_until=%s,lease_owner=%s WHERE id=%s AND status IN"
            " ('admitted','running') AND (lease_until IS NULL OR lease_until<%s OR lease_owner=%s)",
            (now() + timedelta(seconds=60), token, eid, now(), token),
        )
        return bool(c.rowcount)


def release_delivery(eid: str, token: str) -> None:
    with get_db() as conn:
        conn.cursor().execute(
            "UPDATE watch_events SET lease_until=NULL,lease_owner=NULL WHERE id=%s AND"
            " lease_owner=%s",
            (eid, token),
        )


def assign_route(eid: str, route: str, sid: str) -> dict[str, Any]:
    with get_db() as conn:
        c = conn.cursor()
        c.execute(
            "UPDATE watch_events SET route=%s,session_id=%s WHERE id=%s AND route IS NULL",
            (route, sid, eid),
        )
        c.execute("SELECT * FROM watch_events WHERE id=%s", (eid,))
        return parse(c.fetchone())


def mark_creator_notified(wid: str) -> None:
    with get_db() as conn:
        conn.cursor().execute("UPDATE watches SET creator_notified=TRUE WHERE id=%s", (wid,))


def failure(wid: str, message: str) -> None:
    with get_db() as conn:
        c = conn.cursor()
        c.execute(
            "UPDATE watches SET failures=failures+1,reason=%s WHERE id=%s",
            (str(message)[:500], wid),
        )
        c.execute(
            "UPDATE watches SET status='paused',reason=%s WHERE id=%s AND failures>=3 AND"
            " status='active'",
            ("Repeated source/delivery failures: " + str(message)[:400], wid),
        )
