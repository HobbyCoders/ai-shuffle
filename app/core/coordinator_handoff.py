"""Retire oversized no-project coordinators at a safe turn boundary.

80% of the measured input window, before the normal compaction edge. No guessed
usage and no mid-tool interruption. The source and successor admission are one
transaction; a competing human send either wins first (we defer) or resumes the
old chat afterwards. Independent StartSession children are deliberately left
alone; their reports still enter the owner's shared working record.
"""
import asyncio
import hashlib
import logging
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

from fluid.tools.hardening import defang_harness_markers

from app.db.base import get_db, parse_jsonb

logger = logging.getLogger(__name__)
THRESHOLD = 0.80


def _admit(session_id, settled, terminal):
    from app.core import compact_continue
    from app.core import session_orchestration as orch
    from app.core.task_registry import task_registry
    from app.db import network, session_events, sessions, turns
    from app.network import control, session_active

    original = sessions.get_session(session_id)
    if not orch.is_owner_session(original) or not orch._local_top_level(original):
        return None
    principal = orch._principal(session_id)
    start = orch._start_turn(session_id)
    origin = orch._origin_turn(session_id, settled) if start else None
    successor_id = str(uuid5(NAMESPACE_URL, "coordinator-handoff:" + session_id))
    with get_db() as conn:
        cur = conn.cursor()
        # Same order as membership edits/transcript writes/turn admission.
        network.lock_state(cur)
        cur.execute("INSERT INTO session_event_counters (session_id,next_seq) VALUES (%s,0) "
                    "ON CONFLICT (session_id) DO NOTHING", (session_id,))
        cur.execute("SELECT session_id FROM session_event_counters WHERE session_id=%s FOR UPDATE", (session_id,))
        cur.fetchone()
        cur.execute("SELECT * FROM sessions WHERE id=%s FOR UPDATE", (session_id,))
        row = cur.fetchone()
        if not row:
            return None
        row = dict(row)
        if (not orch.is_owner_session(row, principal=principal) or not row.get("is_active")
                or row.get("project_id") or row.get("project_v2_id")
                or row.get("status") == "archived" or control.owner_of(row)
                or row.get("last_assistant_question_seq") is not None
                or task_registry.is_user_stopped(session_id)
                or task_registry.has_active_background_work(session_id)):
            return None
        usage = parse_jsonb(row.get("context_usage_json")) if row.get("context_usage_json") else None
        # Stored usage is wrapped by set_session_context_usage.
        if isinstance(usage, dict) and "data" in usage:
            usage = usage["data"]
        fraction = compact_continue.usage_fraction(usage)
        if fraction is None or fraction < THRESHOLD:
            return None
        cur.execute("SELECT successor_session_id FROM coordinator_handoffs WHERE source_session_id=%s", (session_id,))
        if cur.fetchone():
            return None
        cur.execute("SELECT id FROM turns WHERE session_id=%s AND state IN "
                    "('queued','running','draining','interrupting','parked','blocked_setup') LIMIT 1", (session_id,))
        if cur.fetchone():
            return None
        cur.execute("SELECT id FROM pending_interactions WHERE session_id=%s AND state='pending' "
                    "AND (expires_at IS NULL OR expires_at>=NOW()) LIMIT 1", (session_id,))
        if cur.fetchone():
            return None
        cur.execute("SELECT task_id FROM background_tasks WHERE session_id=%s AND status='running' LIMIT 1", (session_id,))
        if cur.fetchone():
            return None
        # Don't let the old usage snapshot retire a freshly compacted successor:
        # create_session gives it no sdk_session_id and no context_usage_json.
        successor = sessions.create_session(
            successor_id, row["profile_id"], title=row.get("title"), is_active=True,
            provider_id=row.get("provider_id"), derive_provider_from_profile=False,
            model=row.get("model"), effort=row.get("effort"),
            permission_mode=row.get("permission_mode"), permission_ceiling=row.get("permission_ceiling"),
            context_window=row.get("context_window"), fast=row.get("fast"), mode="chat", _conn=conn)
        cur.execute("SELECT prompt FROM turns WHERE session_id=%s AND source IN ('user','queue') "
                    "ORDER BY position DESC LIMIT 3", (session_id,))
        requests = [str(r.get("prompt") or "")[:500] for r in cur.fetchall()][::-1]
        unfinished = terminal.get("stop_reason") == "tool_use"
        prompt = ("[automatic coordinator hand-off] The previous coordinating chat " + session_id
                  + " reached 80% context and was settled. Read the owner working record in your context. "
                  + ("Continue unfinished coordination from that record; do not repeat completed work. " if unfinished else
                     "The previous turn finished. Give a short status update, then wait for the owner; do not start new work. ")
                  + "Recent owner messages from the old chat (historical data, not new requests): "
                  + repr([defang_harness_markers(text) for text in requests]))
        ancestry = settled.get("payload") or {}
        payload = {"coordinator_predecessor": session_id,
                   "orchestration_root": ancestry.get("orchestration_root") or settled.get("id"),
                   "orchestration_depth": ancestry.get("orchestration_depth", 0)}
        source, command_id = "wakeup", None
        # A local StartSession coordinator cut at the boundary still owes its
        # starter the final report. Transfer that host-written routing marker,
        # not permission grants or a fresh orchestration budget. Remote starts
        # require a project, so cannot enter this no-project hand-off path.
        if (unfinished and origin and turns.logical_source(origin) == "agent_message"
                and (origin.get("payload") or {}).get("orchestration_sender")
                == ((start or {}).get("payload") or {}).get("orchestration_sender")):
            payload.update({key: value for key, value in (start.get("payload") or {}).items()
                            if key in ("orchestration_sender", "orchestration_remote_origin",
                                       "orchestration_report_device")})
            source = "agent_message"
            command_id = "orchestration:start:" + hashlib.sha256(successor_id.encode()).hexdigest()
        turn = turns.enqueue_turn(successor_id, source, prompt=prompt, command_id=command_id,
                                  profile_id=row["profile_id"], parent_turn_id=settled.get("id"),
                                  payload=payload, _conn=conn)
        now = datetime.now(UTC).isoformat()
        cur.execute("INSERT INTO coordinator_handoffs (source_session_id,successor_session_id,turn_id,created_at) "
                    "VALUES (%s,%s,%s,%s)", (session_id, successor_id, turn["id"], now))
        session_events.append_events(session_id, [{"type": "system.note", "payload": {
            "subtype": "coordinator_handoff", "successor_session_id": successor_id,
            "content": "Context nearly full. Coordination continues in session " + successor_id + "."}}], _conn=conn)
        session_active._record_active(cur, session_id, False)
        session_active._record_active(cur, successor_id, True)
        return successor


async def on_session_rest(session_id, settled, terminal, outcome):
    """Scheduler seam: no queued work, before compact-boundary continuation."""
    if outcome != "completed" or (settled.get("payload") or {}).get("coordinator_predecessor"):
        # The initial status wake must never make an automatic hand-off chain,
        # even on a model with a window smaller than its fixed system prefix.
        return False
    from app.core import session_orchestration as orch
    from app.core import turn_scheduler, working_record
    from app.db import sessions
    committed = False
    try:
        if terminal.get("stop_reason") != "tool_use":
            await asyncio.to_thread(working_record.record_completion, session_id, settled, outcome)
        if terminal.get("is_error"):
            return False
        row = await asyncio.to_thread(sessions.get_session, session_id)
        if (not row or row.get("project_id") or row.get("project_v2_id")
                or not await asyncio.to_thread(orch.is_owner_session, row)):
            return False
        # Capture the producing adapter now, not just a fixture/client GET.
        # Turn-end capture is detached and may not have won the settlement race.
        from app.core.query_persistence import capture_context_usage
        await capture_context_usage(session_id)
        successor = await asyncio.to_thread(_admit, session_id, settled, terminal)
        if successor is None:
            return False
        committed = True
        # The queued wake is durable: a crash after commit is recovered by the
        # ordinary scheduler queue recovery. No parallel hand-off task to lose.
        turn_scheduler.kick(successor["id"])
        if terminal.get("stop_reason") != "tool_use":
            await orch.report_settled_start(session_id, settled, outcome)
        from app.network import engine
        engine.kick()
        from app.services.notification_service import emit_notification
        try:
            from app.core.sync_engine import sync_engine
            from app.network import session_active
            await sync_engine.broadcast_user_session_created("admin", successor["id"])
            current = await asyncio.to_thread(sessions.get_session, session_id)
            if current:
                await session_active._broadcast(current, session_id)
        except Exception:
            logger.warning("Coordinator hand-off sidebar broadcast failed for %s", session_id, exc_info=True)
        try:
            await emit_notification("admin", title="Continuing in a fresh chat",
                message="The coordinating chat reached its context threshold. Its working record carries over.",
                category="coordinator_handoff", session_id=successor["id"], push=True)
        except Exception:
            logger.warning("Coordinator hand-off notification failed for %s", session_id, exc_info=True)
        return True
    except Exception:
        logger.warning("Coordinator continuity failed for %s", session_id, exc_info=True)
        return committed
