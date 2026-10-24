"""Durable watches, atomic source cursors and an idempotent delivery outbox.

Source I/O never holds a database transaction. A compare-and-swap cursor admits
one polling generation; unique event keys deduplicate callbacks and polling.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta
from typing import Any

from app.core.watch_values import at, encode, now
from app.db.base import get_db, row_to_dict


def parse(row: dict[str, Any]) -> dict[str, Any] | None:
    row = row_to_dict(row)
    if row:
        for key in ("source", "filter", "permissions", "cursor", "payload"):
            if isinstance(row.get(key), str):
                row[key] = json.loads(row[key])
    return row


def create_schema(cursor: Any) -> None:
    from app.db.watch_delivery import create_schema as delivery_schema

    delivery_schema(cursor)
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
    cursor.execute("""CREATE TABLE IF NOT EXISTS watch_connector_inbox (
        seq BIGSERIAL PRIMARY KEY, connector_id TEXT NOT NULL, event_key TEXT NOT NULL,
        payload JSONB NOT NULL, UNIQUE(connector_id,event_key)
    )""")
    from app.db import watch_journal

    watch_journal.schema(cursor)
    cursor.execute("""CREATE TABLE IF NOT EXISTS watch_transfers (
        watch_id TEXT PRIMARY KEY, token TEXT NOT NULL, target TEXT NOT NULL,
        snapshot JSONB NOT NULL, completed BOOLEAN NOT NULL DEFAULT FALSE
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
    creating_device_id: str | None = None,
    conn: Any = None,
) -> str:
    from contextlib import nullcontext

    wid = watch_id or str(uuid.uuid4())
    with nullcontext(conn) if conn is not None else get_db() as connection:
        c = connection.cursor()
        if source.get("connector_id"):
            from app.db import watch_journal

            state = watch_journal.lock(c, source["connector_id"])
            if state["fence"]:
                raise ValueError("Telegram receiver is fenced for transfer")
            if cursor and "seq" in cursor and int(cursor["seq"]) < state["floor_seq"]:
                raise ValueError(watch_journal.EXPIRED)
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
        if creating_device_id:
            from app.db.watch_delivery import set_origin

            set_origin(c, wid, creating_device_id)
    return wid


def export_watch(
    wid: str, target: str, telegram_identity: str | None = None,
) -> dict[str, Any]:
    """Fence polling/admission under reserve's row lock before taking a snapshot."""
    with get_db() as conn:
        c = conn.cursor()
        before = get(wid)
        if before and before["source"].get("connector_id"):
            lock_connector(c, before["source"]["connector_id"])
        c.execute("SELECT * FROM watches WHERE id=%s FOR UPDATE", (wid,))
        watch = parse(c.fetchone())
        if not watch or watch["owner"] != "admin":
            raise ValueError("Watch does not exist")
        c.execute("SELECT * FROM watch_transfers WHERE watch_id=%s", (wid,))
        previous = c.fetchone()
        if previous and str(previous["target"]).startswith("import:"):
            c.execute("DELETE FROM watch_transfers WHERE watch_id=%s", (wid,))
            previous = None
        if previous:
            if previous["target"] != target or previous["completed"]:
                raise ValueError("Watch has already moved or is moving to another worker")
            snapshot = previous["snapshot"]
            snapshot = json.loads(snapshot) if isinstance(snapshot, str) else snapshot
            if not snapshot:
                raise ValueError("Watch transfer was invalidated by connector deletion")
            return snapshot
        if watch["status"] == "stopped":
            raise ValueError("Stopped watches cannot move")
        c.execute("SELECT * FROM watch_events WHERE watch_id=%s ORDER BY created_at,id", (wid,))
        rows = [parse(row) for row in c.fetchall()]
        if any(row["status"] in ("admitted", "running") for row in rows):
            raise ValueError("Wait for in-flight deliveries to finish before moving this watch")
        if len(rows) > 10000:
            raise ValueError("Watch history is too large for a single transfer")
        from app.db.watch_delivery import origin

        c.execute(
            "SELECT o.* FROM watch_notification_outbox o JOIN watch_events e "
            "ON e.id=o.event_id WHERE e.watch_id=%s", (wid,),
        )
        reports = [dict(row) for row in c.fetchall()]
        snapshot = dict(watch=watch, events=rows, token=str(uuid.uuid4()), target=target,
                        creating_device_id=origin(wid), reports=reports)
        if telegram_identity is not None:
            from app.db import watch_journal

            snapshot["journal"] = dict(watch_journal.tail(c, watch), identity=telegram_identity)
        if len(encode(snapshot).encode()) > 3 * 1024 * 1024:
            raise ValueError("Watch history exceeds the transfer payload limit")
        if telegram_identity is not None:
            cid = watch["source"]["connector_id"]
            state = watch_journal.lock(c, cid)
            fence = "export:" + wid + ":" + target
            if state["fence"] and state["fence"] != fence:
                raise ValueError("Telegram receiver is fenced by another transfer")
            c.execute("UPDATE watch_connector_journals SET fence=%s WHERE connector_id=%s",
                      (fence, cid))
        c.execute(
            "INSERT INTO watch_transfers (watch_id,token,target,snapshot) VALUES (%s,%s,%s,%s)",
            (wid, snapshot["token"], target, encode(snapshot)),
        )
        c.execute(
            "UPDATE watches SET status='paused',reason='Transfer pending',"
            "worker_id=%s,revision=revision+1 WHERE id=%s", ("transfer:" + target, wid),
        )
    return snapshot


def import_watch(
    snapshot: dict[str, Any], row: dict[str, Any], from_device: str
) -> dict[str, Any]:
    """Import once, including past reports for dedupe, caps and accounting continuity."""
    original = snapshot["watch"]
    wid = original["id"]
    token = snapshot["token"]
    with get_db() as conn:
        c = conn.cursor()
        if row["source"].get("connector_id"):
            lock_connector(c, row["source"]["connector_id"])
        # Serialize even two first imports of a previously absent id.
        lock_id = int(hashlib.sha256(("watch-import:" + wid).encode()).hexdigest()[:15], 16)
        c.execute("SELECT pg_advisory_xact_lock(%s)", (lock_id,))
        c.execute("SELECT * FROM watches WHERE id=%s", (wid,))
        existing = parse(c.fetchone())
        if existing:
            c.execute("SELECT token FROM watch_transfers WHERE watch_id=%s", (wid,))
            receipt = c.fetchone()
            if receipt and receipt["token"] == token:
                return existing
            c.execute("SELECT * FROM watch_transfers WHERE watch_id=%s", (wid,))
            previous = c.fetchone()
            if not (
                existing["worker_id"].startswith("transfer:") and previous
                and previous["completed"] and previous["target"] == from_device
            ):
                raise ValueError("Watch id is already owned here")
            # Returning to a former executor replaces only its fenced tombstone.
            c.execute("DELETE FROM watch_events WHERE watch_id=%s", (wid,))
            c.execute("DELETE FROM watches WHERE id=%s", (wid,))
            c.execute("DELETE FROM watch_transfers WHERE watch_id=%s", (wid,))
        cursor = original["cursor"]
        if "journal" in snapshot:
            from app.db import watch_journal

            cursor = watch_journal.restore(c, row, snapshot["journal"])
        create(
            **row, watch_id=wid, cursor=cursor, status=original["status"], conn=conn,
            creating_device_id=snapshot.get("creating_device_id"),
        )
        c.execute(
            "UPDATE watches SET cost_usd=%s,last_fired_at=%s,created_at=%s,reason=%s "
            "WHERE id=%s",
            (original["cost_usd"], original["last_fired_at"], original["created_at"],
             original.get("reason"), wid),
        )
        for event in snapshot["events"]:
            if event["status"] in ("admitted", "running") or event["watch_id"] != wid:
                raise ValueError("Invalid transfer event")
            fields = tuple(event)
            if not set(fields) <= {
                "id", "watch_id", "event_key", "payload", "status", "session_id", "turn_id",
                "command_id", "route", "reserved_usd", "cost_usd", "summary",
                "summary_attempted", "notification_sent", "deadline_at", "admitted_at",
                "lease_until", "lease_owner", "created_at",
            }:
                raise ValueError("Invalid transfer fields")
            c.execute(
                "INSERT INTO watch_events (" + ",".join(fields) + ") VALUES ("
                + ",".join(["%s"] * len(fields)) + ")",
                tuple(encode(event[f]) if f == "payload" else event[f] for f in fields),
            )
        event_ids = {event["id"] for event in snapshot["events"]}
        for report in snapshot.get("reports", []):
            if report["event_id"] not in event_ids:
                raise ValueError("Invalid transfer report")
            c.execute(
                "INSERT INTO watch_notification_outbox(event_id,target,body,acknowledged) "
                "VALUES (%s,%s,%s,%s) ON CONFLICT(event_id,target) DO NOTHING",
                (report["event_id"], report["target"], encode(report["body"]),
                 report["acknowledged"]),
            )
        c.execute(
            "INSERT INTO watch_transfers (watch_id,token,target,snapshot,completed) "
            "VALUES (%s,%s,%s,%s,TRUE)", (wid, token, "import:" + from_device, "{}"),
        )
    return get(wid)


def import_receipt(wid: str) -> dict[str, Any]:
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT token,target FROM watch_transfers WHERE watch_id=%s", (wid,))
        receipt = c.fetchone()
        if not receipt or not receipt["target"].startswith("import:"):
            return {}
        return {"source": receipt["target"][len("import:"):], "token": receipt["token"]}


def complete_transfer(wid: str, target: str, token: str) -> None:
    with get_db() as conn:
        c = conn.cursor()
        watch = get(wid)
        cid = watch["source"].get("connector_id") if watch else None
        if cid:
            lock_connector(c, cid)
        c.execute(
            "UPDATE watch_transfers SET completed=TRUE,snapshot='{}' "
            "WHERE watch_id=%s AND target=%s AND token=%s",
            (wid, target, token),
        )
        if not c.rowcount:
            raise ValueError("Transfer does not exist")
        c.execute("UPDATE watches SET reason='Transferred' WHERE id=%s", (wid,))
        if cid:
            from app.db.watch_journal import trim

            trim(c, cid)


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
        before = get(wid)
        if before and before["source"].get("connector_id"):
            lock_connector(c, before["source"]["connector_id"])
            before = get(wid)
        if status == "active" and before and before["cursor"].get("expired"):
            from app.db.watch_journal import EXPIRED

            raise ValueError(EXPIRED)
        # Stop is terminal: a new confirmation is needed to create another watch.
        c.execute(
            "UPDATE watches SET status=%s,reason=%s,revision=revision+1,updated_at=NOW() "
            "WHERE id=%s AND owner=%s AND status <> 'stopped' AND worker_id NOT LIKE 'transfer:%%'",
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


def lock_connector(cursor: Any, connector_id: str) -> None:
    """Keep sequence allocation, commit and cursor reads ordered per connector."""
    key = int(hashlib.sha256(("watch-ingress:" + connector_id).encode()).hexdigest()[:15], 16)
    cursor.execute("SELECT pg_advisory_xact_lock(%s)", (key,))


def record_connector_event(connector_id: str, key: str, payload: dict[str, Any]) -> None:
    """Commit before the connector acknowledges the incoming update."""
    from app.db.watch_journal import append

    append(connector_id, key, payload)


def connector_page(
    connector_id: str, channel_id: str, cursor: dict[str, Any]
) -> tuple[list[tuple[str, dict[str, Any]]], dict[str, Any]]:
    from app.db.watch_journal import page

    return page(connector_id, channel_id, cursor)


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
        cid = watch["source"].get("connector_id")
        if cid:
            lock_connector(c, cid)
        c.execute(
            "UPDATE watches SET cursor=%s,revision=revision+1,updated_at=NOW() "
            "WHERE id=%s AND revision=%s AND status='active'",
            (encode(cursor), watch["id"], watch["revision"]),
        )
        if not c.rowcount:
            return False
        _insert(c, watch, events)
        if cid:
            from app.db.watch_journal import trim

            trim(c, cid)
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


def renew_admission(eid: str, token: str) -> dict[str, Any] | None:
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
            or (
                event.get("notification_sent")
                and event.get("route") not in ("investigate", "remote")
            )
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
