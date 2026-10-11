"""Transactionally repair an owned task projection, never a peer's replica."""

from datetime import datetime, timezone
import uuid

from app.db.base import get_db
from app.db.background_tasks import TERMINAL_STATUSES
from app.db.session_events import _parse_payload, append_events, notify_session_write


def _timestamp(value):
    try:
        stamp = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
        return stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp
    except (TypeError, ValueError):
        return None


def repair_task_projection(session_id, task_id, *, instance_id, device_id):
    """Re-read ownership, projection and durable assignment in one transaction.

    The session row fences ownership transfers; the event-counter row serializes
    this read/check/append with *all* event writers, across processes. A stable
    event id is an additional durable dedup fence for this particular start.
    """
    if not device_id or not instance_id:
        return False
    with get_db() as conn:
        cursor = conn.cursor()
        # Match fenced append_events (run -> counter -> session) and handoff's
        # _lease -> session UPDATE. Taking the run lock after the session can
        # deadlock with a handoff before we even see its reservation and decline.
        cursor.execute(
            "SELECT runner_id FROM runs WHERE session_id=%s AND state='active' FOR SHARE",
            (session_id,),
        )
        if any(str(row["runner_id"]).startswith(("handoff:", "handoff-hold:"))
               for row in cursor.fetchall()):
            return False
        cursor.execute(
            "SELECT next_seq FROM session_event_counters WHERE session_id=%s FOR UPDATE",
            (session_id,),
        )
        if cursor.fetchone() is None:
            return False
        cursor.execute(
            "SELECT instance_id, owner_device_id FROM sessions WHERE id=%s FOR UPDATE",
            (session_id,),
        )
        owner = cursor.fetchone()
        if (not owner or not owner["owner_device_id"]
                or owner["instance_id"] != instance_id
                or owner["owner_device_id"] != device_id):
            return False
        cursor.execute(
            "SELECT id, seq, payload, created_at FROM session_events "
            "WHERE session_id=%s AND event_type='task.updated' "
            "AND payload->>'task_id'=%s ORDER BY seq DESC LIMIT 1",
            (session_id, task_id),
        )
        latest = cursor.fetchone()
        if not latest:
            return False
        update = _parse_payload(latest["payload"])
        if update.get("state") not in ("running", "started"):
            return False
        cursor.execute(
            "SELECT status, started_at, updated_at, summary, tool_use_id, kind "
            "FROM background_tasks WHERE session_id=%s AND task_id=%s FOR SHARE",
            (session_id, task_id),
        )
        task = cursor.fetchone()
        if not task or task["status"] not in TERMINAL_STATUSES:
            return False
        started, ended = _timestamp(task["started_at"]), _timestamp(task["updated_at"])
        observed = _timestamp(latest["created_at"])
        if not started or not ended or not observed or ended < observed or ended < started:
            return False
        if update.get("started_at"):
            # New logs identify the assignment exactly, even when its preceding
            # terminal frame was lost. Never apply an older generation's stop.
            if _timestamp(update["started_at"]) != started:
                return False
        else:
            # Legacy starts have no generation field. Their event must fall
            # within this snapshot's lifetime, after the previous assignment's
            # terminal boundary. Ambiguous or missing timestamps fail closed.
            if started > observed:
                return False
            cursor.execute(
                "SELECT created_at FROM session_events WHERE session_id=%s "
                "AND event_type='task.updated' AND payload->>'task_id'=%s "
                "AND payload->>'state' IN ('completed','error','killed','failed','stopped') "
                "AND seq<%s ORDER BY seq DESC LIMIT 1",
                (session_id, task_id, latest["seq"]),
            )
            previous = cursor.fetchone()
            if previous:
                boundary = _timestamp(previous["created_at"])
                if boundary is None or started <= boundary:
                    return False
        payload = dict(update, state=task["status"], summary=task["summary"],
                       tool_use_id=task["tool_use_id"] or task_id,
                       kind=task["kind"], started_at=started.isoformat())
        append_events(session_id, [{
            "id": str(uuid.uuid5(uuid.NAMESPACE_URL,
                                 f"task-replay:{session_id}:{latest['id']}")),
            "type": "task.updated", "payload": payload,
        }], _conn=conn)
    notify_session_write(session_id, "session_events")
    return True
