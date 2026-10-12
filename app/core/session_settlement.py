"""Dismiss one fenced snapshot of owner waits; never sweep later work."""
import hashlib

from app.db.async_database import run_sync


_DENIAL = {"decision": "deny", "behavior": "deny", "message": "User denied permission"}


def _capture_and_dismiss(session_id, broker, expected_revision):
    """Linearize settlement under the membership and turn-admission locks.

    Capture the membership, turns and interaction identities without an await,
    then commit ONLY those identities under the same locks. Private waiters are
    captured while those DB rows are locked, and released on their owning loop.
    The returned turn IDs are signals, not selectors for another DB sweep.
    """
    from app.core import pending_reconcile
    from app.core.task_registry import task_registry
    from app.db import interactions, turns, session_events, network as store
    from app.db.base import get_db, parse_jsonb
    from app.network import control

    with get_db() as conn:
        cur = conn.cursor()
        store.lock_state(cur)
        # Transcript appends lock their counter before projecting the session.
        # Use that order too; the durable revision receipt is an ordinary event.
        cur.execute("INSERT INTO session_event_counters (session_id,next_seq) VALUES (%s,0) "
                    "ON CONFLICT (session_id) DO NOTHING", (session_id,))
        cur.execute("SELECT session_id FROM session_event_counters WHERE session_id=%s FOR UPDATE", (session_id,))
        cur.fetchone()
        cur.execute("SELECT * FROM sessions WHERE id=%s FOR UPDATE", (session_id,))
        row = cur.fetchone()
        if not row or row.get("is_active") or row.get("status") == "archived" or control.owner_of(dict(row)):
            return None
        revision = store.get_record(cur, "session_active", session_id)
        cur.execute("SELECT updated_at, local_pending FROM network_session_active WHERE session_id=%s", (session_id,))
        membership = cur.fetchone()
        if expected_revision is not None:
            if "membership_updated_at" in expected_revision:
                if not membership or membership["updated_at"] != expected_revision["membership_updated_at"]:
                    return None
            elif "session_updated_at" in expected_revision:
                if row["updated_at"] != expected_revision["session_updated_at"]:
                    return None
            elif (not revision or not membership or membership["local_pending"] or any(
                revision.get(key) != expected_revision.get(key) for key in ("clock", "origin_device_id")
            )):
                return None
        # Holding the parent's admission lock also fences new children. Lock
        # existing child counters before their rows, then take one wait snapshot.
        cur.execute("""WITH RECURSIVE family AS (
            SELECT id FROM sessions WHERE id=%s
            UNION ALL SELECT s.id FROM sessions s JOIN family f ON s.parent_session_id=f.id
        ) SELECT id FROM family ORDER BY id""", (session_id,))
        family = [r["id"] for r in cur.fetchall()]
        placeholders = ",".join(["%s"] * len(family))
        cur.execute("SELECT session_id FROM session_event_counters WHERE session_id IN (" + placeholders +
                    ") ORDER BY session_id FOR UPDATE", tuple(family))
        cur.fetchall()
        cur.execute("SELECT id FROM sessions WHERE id IN (" + placeholders + ") ORDER BY id FOR UPDATE", tuple(family))
        cur.fetchall()
        # Fast delivery and replication can race on the SAME revision. A
        # committed receipt prevents their second snapshot sweeping later waits.
        token = str(expected_revision["session_updated_at"] if expected_revision and "session_updated_at" in expected_revision
                    else membership["updated_at"] if membership else row["updated_at"])
        receipt = "settled-" + hashlib.sha256((session_id + "\0" + token).encode()).hexdigest()
        cur.execute("SELECT id FROM session_events WHERE id=%s", (receipt,))
        if cur.fetchone():
            return None
        cur.execute("SELECT * FROM turns WHERE session_id IN (" + ",".join(["%s"] * len(family)) +
                    ") AND state IN ('running','interrupting','draining','parked') ORDER BY position DESC FOR UPDATE",
                    tuple(family))
        captured_turns = [dict(t) for t in cur.fetchall()]
        cur.execute("SELECT * FROM pending_interactions WHERE session_id=%s AND state='pending' "
                    "AND (expires_at IS NULL OR expires_at >= NOW()) ORDER BY created_at FOR UPDATE", (session_id,))
        pending = [interactions._parse_row(dict(p)) for p in cur.fetchall()]
        private = broker.snapshot_pending(session_id)
        waiting = {p.session_id for group in private for p in group}
        for prompt in pending:
            if prompt["kind"] in ("permission", "question"):
                waiting.add((prompt["payload"].get("provider_agent") or {}).get("worker_session_id") or session_id)
        question_seq, user_seq = row.get("last_assistant_question_seq"), row.get("last_user_message_seq")
        prose = question_seq is not None and (user_seq is None or (
            row.get("last_user_message_epoch") == row.get("last_assistant_question_epoch") and question_seq > user_seq))
        if not (waiting or prose):
            # An empty snapshot is still a processed revision. Otherwise a
            # delayed duplicate could capture a prompt that arrived afterwards.
            # No boundary/attention update or runtime interrupt is warranted.
            session_events.append_events(session_id, [{
                "id": receipt, "type": "system.note", "payload": {
                    "subtype": "session_settled", "content": "Session settled.",
                    "through_seq": 0, "epoch": None, "empty_snapshot": True,
                },
            }], _conn=conn)
            return None
        cur.execute("SELECT COALESCE(MAX(seq),0) AS seq FROM session_events WHERE session_id=%s", (session_id,))
        through_seq = cur.fetchone()["seq"]
        permissions, questions, signals, aborted = [], [], [], []
        for prompt in pending:
            if prompt["kind"] == "permission" and interactions.answer_interaction(prompt["id"], _DENIAL, _conn=conn):
                permissions.append(prompt)
            elif prompt["kind"] == "question" and interactions.cancel_interaction(prompt["id"], _conn=conn):
                questions.append(prompt)
        for waiter in waiting:
            captured = next((t for t in captured_turns if t["session_id"] == waiter), None)
            if not captured:
                continue
            tid = captured["id"]
            if captured["state"] == "parked":
                # Retire only this captured park, and only if nothing else holds
                # it. Never revisit 'the latest park' after yielding to a wake.
                if (not task_registry.get_parking_background_work(waiter)
                        and not task_registry.has_unresolved_background_launches(waiter)
                        and not pending_reconcile.nonterminal_agent_task_ids(waiter)):
                    from app.core.provider_agent import PENDING_WAKE_LIMIT_SECONDS
                    payload = parse_jsonb(captured.get("payload")) or {}
                    wakes = task_registry.unpublished_monitor_wakes(waiter, payload.get("task_ids") or [])
                    if any(age < PENDING_WAKE_LIMIT_SECONDS for age in wakes.values()):
                        continue
                    if turns.settle_parked_turns(waiter, turn_ids=[tid], _conn=conn):
                        cur.execute("UPDATE sessions SET parked_task_kind=NULL WHERE id=%s", (waiter,))
                continue
            stopped = turns.stop_orphaned_turns(waiter, tid, _conn=conn, stop_background=False)
            if stopped:
                aborted.extend(stopped)
            elif turns.interrupt_turn(waiter, "stop", turn_id=tid, source="session_settled", _conn=conn):
                signals.append((waiter, tid))
        session_events.append_events(session_id, [{
            "id": receipt, "type": "system.note", "payload": {"subtype": "session_settled",
            "content": "Session settled; pending prompts dismissed.",
            "through_seq": through_seq, "epoch": row.get("last_assistant_question_epoch")},
        }], _conn=conn)
        return dict(row=row, revision=revision, permissions=permissions, questions=questions,
                    private=private, signals=signals, aborted=aborted, prose=prose, through_seq=through_seq)


async def dismiss_owner_waits(session_id: str, *, expected_revision=None) -> bool:
    from app.core import interaction_events, interaction_wait, streaming_status, turn_manager
    from app.core.chat_access import broker, publish_change
    from app.core.permission_handler import permission_handler
    from app.core.sync_engine import sync_engine
    from app.core.user_question_handler import publish_session_attention
    from app.db import session_events
    from app.network import catalog, engine

    from app.core.history_backfill import ensure_backfilled
    await ensure_backfilled(session_id)
    snapshot = await run_sync(_capture_and_dismiss, session_id, broker, expected_revision)
    if snapshot is None:
        return False
    row = snapshot["row"]
    user_id = row.get("api_user_id") or "admin"
    # Futures belong to THIS loop. A request created since the DB snapshot is
    # deliberately absent, even if it has the same display/request identifier.
    broker.dismiss_pending(session_id, snapshot=snapshot["private"])
    interaction_wait.notify_changed(session_id)
    for waiter, tid in snapshot["signals"]:
        turn_manager._fire_interrupt_executor(waiter, "stop", tid)
    for turn in snapshot["aborted"]:
        await run_sync(session_events.append_events, turn["session_id"], [{
            "type": "turn.aborted", "turn_id": turn["id"],
            "payload": {"reason": "interrupt", "message": "Session settled"},
        }])
    permissions, questions = snapshot["permissions"], snapshot["questions"]
    for prompt in permissions:
        await interaction_events.emit_interaction_answered(
            session_id, prompt["id"], prompt["payload"].get("request_id"), _DENIAL)
    if permissions:
        await sync_engine.broadcast_user_permission(user_id, {
            "type": "permission_queue_update", "session_id": session_id,
            "resolved_ids": [p["payload"].get("request_id") for p in permissions],
            "remaining_count": await run_sync(permission_handler.get_queue_count, session_id),
        })
    for prompt in questions:
        rid = prompt["payload"].get("request_id")
        await interaction_events.emit_interaction_expired(session_id, prompt["id"], rid, "cancelled")
        await sync_engine.broadcast_user_question(user_id, {
            "type": "user_question_timeout", "session_id": session_id, "request_id": rid,
        })
    if any(snapshot["private"]):
        await publish_change(session_id, row.get("api_user_id"), pending=broker.pending_count(session_id))
    await publish_session_attention(session_id)
    streaming_status.notify(session_id, force=True)
    ctx = engine.context()
    if ctx is not None:
        await run_sync(lambda: catalog.publish_owned(ctx=ctx, session_ids=[session_id]))
        engine.kick()
    return True
