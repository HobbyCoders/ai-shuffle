"""Best-effort external lifecycle delivery from authoritative runtime transitions.

The winning turn owner enqueues start/settlement events; interaction creation
enqueues permission/question events. Delivery runs outside the model pump, in
order, with bounded memory and time. This is not a durable webhook outbox: a
process crash can lose queued deliveries. Event IDs let receivers deduplicate.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from app.db import database, turns
from app.db.async_database import run_sync
from app.db.base import get_db

logger = logging.getLogger(__name__)
_queue: asyncio.Queue | None = None
_worker: asyncio.Task | None = None


def start() -> None:
    global _queue, _worker
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    if _worker is not None and not _worker.done():
        return
    _queue = asyncio.Queue(maxsize=256)
    _worker = loop.create_task(_deliver(), name="lifecycle-events")


async def stop() -> None:
    global _queue, _worker
    worker, _worker = _worker, None
    _queue = None
    if worker is not None:
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass


def enqueue(kind: str, session_id: str, identity: str, **extra: Any) -> None:
    """Never block a turn or permission waiter on an external recipient."""
    if _queue is None:
        return
    try:
        _queue.put_nowait((kind, session_id, identity, extra))
    except asyncio.QueueFull:
        logger.warning("Lifecycle delivery queue full; dropped %s event", kind)


def _payload(kind: str, session_id: str, identity: str, extra: dict) -> tuple[str, dict] | None:
    session = database.get_session(session_id)
    if session is None:
        return None
    data = {
        "session_id": session_id, "profile_id": session.get("profile_id"),
        "title": session.get("title"),
        "automation_id": session.get("automation_id"),
        "connector_ids": list({s["connector_id"] for s in
                               database.get_connector_sessions_for_ai_session(session_id)}),
        **extra,
    }
    if kind in ("started", "completed", "aborted"):
        data["turn_id"] = identity
        turn = turns.get_turn(identity)
        if turn is None:
            return None
        data["source"] = turn.get("source")
        if kind == "started":
            event_type = "session.started"
        else:
            # Scope the terminal lookup to this turn, never the latest session
            # event: a queued follow-up may already have started by delivery.
            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT payload FROM session_events WHERE session_id=%s AND turn_id=%s "
                    "AND event_type IN ('turn.completed','turn.aborted') ORDER BY seq DESC LIMIT 1",
                    (session_id, identity),
                )
                row = cursor.fetchone()
            payload = row["payload"] if row else {}
            if isinstance(payload, str):
                payload = json.loads(payload)
            payload = payload or {}
            if kind == "aborted":
                reason = turn.get("stop_reason") or ""
                if reason.startswith("interrupt") or reason in ("removed_from_queue", "drain_stale_result", "steered"):
                    return None
                data["error_message"] = turn.get("error") or "Turn failed"
                event_type = "session.error"
            elif payload.get("api_error_status") or payload.get("is_error") or payload.get("errors"):
                # A completed pump can carry an unsuccessful provider result.
                # Do not expose arbitrary provider error details to webhooks.
                data["error_message"] = (
                    f"Provider returned status {payload['api_error_status']}"
                    if payload.get("api_error_status") else "Provider execution failed"
                )
                event_type = "session.error"
            else:
                usage = payload.get("usage") or {}
                data.update(total_cost=usage.get("total_cost_usd", 0),
                            input_tokens=usage.get("tokens_in", 0),
                            output_tokens=usage.get("tokens_out", 0),
                            duration_seconds=(usage.get("duration_ms") or 0) / 1000)
                event_type = "session.complete"
    else:
        event_type = kind
        data["interaction_id"] = identity
    data["event_id"] = f"{identity}:{event_type}"
    return event_type, data


async def _deliver() -> None:
    from app.core.connectors.manager import connector_manager
    queue = _queue
    while True:
        item = await queue.get()
        try:
            async with asyncio.timeout(30):
                event = await run_sync(_payload, *item)
                if event is not None:
                    await connector_manager.dispatch_event(*event)
        except Exception:
            logger.warning("Lifecycle event delivery failed", exc_info=True)
        finally:
            queue.task_done()
