"""Transactional, owner/run-fenced session plans (not network-replicated intent)."""
from __future__ import annotations

import json
from app.db.base import get_db, parse_jsonb
from fluid.tools.builtins.plan import apply_update, empty_plan, validate_input


def create_schema(cursor):
    cursor.execute("""CREATE TABLE IF NOT EXISTS session_plans (
        session_id TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
        id TEXT NOT NULL UNIQUE,
        document JSONB NOT NULL,
        receipts JSONB NOT NULL DEFAULT '[]'
    )""")


def read(session_id: str) -> dict:
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT document FROM session_plans WHERE session_id=%s", (session_id,))
        row = cur.fetchone()
        return {"plan": parse_jsonb(row["document"]) if row else empty_plan(), "status_source": "model_reported"}


def projection(session_id: str) -> dict:
    """Read-only UI projection; mirrored transcript events never authorize writes."""
    current = read(session_id)
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT payload FROM session_events WHERE session_id=%s AND event_type=%s ORDER BY seq DESC LIMIT 1",
                    (session_id, "plan.updated"))
        row = cur.fetchone()
    latest = (parse_jsonb(row["payload"]) or {}).get("plan") if row else None
    if isinstance(latest, dict) and type(latest.get("revision")) is int and latest["revision"] > current["plan"]["revision"]:
        current["plan"] = latest
    return current


def execute(session_id: str, args: dict, *, device_id: str, runner_id: str, proof: tuple | None) -> dict:
    validate_input(args)
    if not session_id:
        raise ValueError("Plan requires an active app session")
    with get_db() as conn:
        cur = conn.cursor()
        if args["action"] == "update":
            # Preserve the owner/unavailable error before run validation, without
            # taking a lock ahead of the event counter. Recheck under lock below.
            cur.execute("SELECT owner_device_id FROM sessions WHERE id=%s", (session_id,))
            session = cur.fetchone()
            if not session or session["owner_device_id"] not in (None, device_id):
                raise ValueError("Plan belongs to another worker or session is unavailable")
            if not proof or proof[0] != session_id:
                raise ValueError("Plan update requires the current session run")
            cur.execute("SELECT id FROM runs WHERE id=%s AND session_id=%s AND runner_id=%s "
                        "AND fence_token=%s AND state='active' AND lease_expires_at>=NOW() FOR SHARE",
                        (proof[1], session_id, runner_id, proof[2]))
            if not cur.fetchone():
                raise ValueError("Plan update refused: session run was superseded or expired")
            # Match append_events: run fence -> counter -> session row. Its
            # question projection locks the session after reserving a sequence;
            # taking the session first here deadlocks with concurrent events.
            # Lock without consuming a sequence; append_events still reserves
            # the actual event atomically below. SELECT avoids an absent-session
            # FK failure, leaving the normal ownership/unavailable check intact.
            cur.execute("INSERT INTO session_event_counters (session_id,next_seq) "
                        "SELECT id,0 FROM sessions WHERE id=%s "
                        "ON CONFLICT(session_id) DO UPDATE SET next_seq=session_event_counters.next_seq",
                        (session_id,))
        # The same row lock ownership publication takes: a handoff and mutation
        # cannot cross. Mirrors may never write, even with a stale runtime.
        cur.execute("SELECT owner_device_id FROM sessions WHERE id=%s FOR UPDATE", (session_id,))
        session = cur.fetchone()
        if not session or session["owner_device_id"] not in (None, device_id):
            raise ValueError("Plan belongs to another worker or session is unavailable")
        cur.execute("SELECT document,receipts FROM session_plans WHERE session_id=%s", (session_id,))
        row = cur.fetchone()
        document = parse_jsonb(row["document"]) if row else empty_plan()
        receipts = parse_jsonb(row["receipts"]) if row else []
        if args["action"] == "get":
            return {"plan": document, "status_source": "model_reported"}
        document, receipts, result, changed = apply_update(document, receipts, args)
        if changed:
            cur.execute("INSERT INTO session_plans (session_id,id,document,receipts) VALUES (%s,%s,%s,%s) "
                        "ON CONFLICT(session_id) DO UPDATE SET document=EXCLUDED.document,receipts=EXCLUDED.receipts",
                        (session_id, session_id, json.dumps(document), json.dumps(receipts)))
            from app.db.session_events import append_events
            append_events(session_id, [{"type": "plan.updated", "payload": {
                "plan": document, "status_source": "model_reported"}}],
                run_id=proof[1], fence_token=proof[2], _conn=conn)
        return result
