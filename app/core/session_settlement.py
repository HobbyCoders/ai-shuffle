"""Dismiss owner waits on settlement, without the archive/Stop work sweep."""
from app.db.async_database import run_sync


async def dismiss_owner_waits(session_id: str) -> bool:
    from app.core import streaming_status, turn_manager
    from app.core.chat_access import broker, publish_change
    from app.core.history_backfill import ensure_backfilled
    from app.core.permission_handler import permission_handler
    from app.core.sync_engine import sync_engine
    from app.core.user_question_handler import user_question_handler, publish_session_attention
    from app.db import sessions, turns, session_events
    from app.network import catalog, control, engine

    row = await run_sync(sessions.get_session, session_id)
    if not row or row.get("is_active") or row.get("status") == "archived" or control.owner_of(row):
        return False
    permissions = await run_sync(permission_handler.get_pending_requests, session_id)
    questions = await run_sync(user_question_handler.get_pending_questions, session_id)
    private_wait = broker.pending_count(session_id)
    question_seq = row.get("last_assistant_question_seq")
    user_seq = row.get("last_user_message_seq")
    prose = question_seq is not None and (user_seq is None or (
        row.get("last_user_message_epoch") == row.get("last_assistant_question_epoch")
        and question_seq > user_seq
    ))
    if not (permissions or questions or private_wait or prose):
        return False

    # Bind the interrupt to the waiting turn, never a successor that starts
    # while a prompt is being dismissed. Prose-only parks need no interrupt.
    waiting_sessions = broker.pending_sessions(session_id)
    waiting_sessions.update((request.get("provider_agent") or {}).get("worker_session_id") or session_id
                            for request in permissions)
    if questions:
        waiting_sessions.add(session_id)
    live_turns = {}
    for waiter in waiting_sessions:
        live = await run_sync(turns.get_live_turn, waiter)
        if live:
            live_turns[waiter] = live["id"]
    user_id = row.get("api_user_id") or "admin"
    resolved = []
    for request in permissions:
        result = await permission_handler.respond(request["request_id"], session_id, "deny")
        if result.get("resolved"):
            resolved.append(request["request_id"])
    if resolved:
        await sync_engine.broadcast_user_permission(user_id, {
            "type": "permission_queue_update", "session_id": session_id,
            "resolved_ids": resolved,
            "remaining_count": await run_sync(permission_handler.get_queue_count, session_id),
        })
    await user_question_handler.cancel_all(session_id)
    for question in questions:
        await sync_engine.broadcast_user_question(user_id, {
            "type": "user_question_timeout", "session_id": session_id,
            "request_id": question["request_id"],
        })
    broker.dismiss_pending(session_id)
    if private_wait:
        await publish_change(session_id, row.get("api_user_id"), pending=broker.pending_count(session_id))
    for waiter, turn_id in live_turns.items():
        await turn_manager.interrupt_async(waiter, "stop", stop_background=False,
                                           turn_id=turn_id, source="session_settled")
    if session_id not in live_turns and (permissions or questions or private_wait):
        # Same no-live-background-work guards as a suppressed Monitor wake:
        # retire a prompt-only park, never a park still held by real work.
        from app.core.turn_finalize import settle_stopped_monitor_parks
        await settle_stopped_monitor_parks(session_id)
    if prose:
        await ensure_backfilled(session_id)
        await run_sync(session_events.append_events, session_id, [{
            "type": "system.note", "payload": {"subtype": "session_settled",
            "content": "Session settled; pending prompts dismissed."},
        }])
    await publish_session_attention(session_id)
    streaming_status.notify(session_id, force=True)
    ctx = engine.context()
    if ctx is not None:
        # Publish prompt resolutions AND the cleared prose projection along
        # with runtime state, rather than waiting for the next catalog scan.
        await run_sync(lambda: catalog.publish_owned(ctx=ctx, session_ids=[session_id]))
        engine.kick()
    return True
