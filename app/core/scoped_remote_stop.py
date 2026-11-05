"""Fail-closed automatic remote Stop; legacy explicit TaskStop stays separate.

The captured token comes from actual task-start/message producers, not a late
status read. Both the peer advertisement and the distinct enforcing action are
required. There is deliberately no fallback to the legacy stable-id stop.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

logger = logging.getLogger(__name__)
CAPABILITY = "scoped_task_stop"
ACTION = "task-stop-scoped"


def valid_scope(scope: Any) -> bool:
    return (isinstance(scope, dict) and isinstance(scope.get("generation"), str)
            and 0 < len(scope["generation"]) <= 100
            and type(scope.get("epoch")) is int and scope["epoch"] > 0)


def advertised(device: str) -> bool:
    from app.network import engine
    # Do not use directory's member fallback when the current publication is
    # missing/empty/unreadable. That could resurrect an obsolete advertisement.
    try:
        presence = engine.all_presence().get(device)
        caps = presence.get("capabilities") if isinstance(presence, dict) else None
        value = caps.get(CAPABILITY) if isinstance(caps, dict) else None
        return type(value) is int and value == 1
    except Exception:
        return False


def incomplete(task_id: str, device: str, name: str, reason: str) -> dict:
    return {"ok": False, "incomplete": True, "task_id": task_id, "worker_id": device,
            "worker_name": name, "reason": reason,
            "detail": f"Work on {name} may STILL BE RUNNING. Update {name}, or open that worker "
                      "and stop the work there. Stop has not confirmed that this work ended."}


def incomplete_text(results: list[dict]) -> str:
    details = list(dict.fromkeys(r["detail"] for r in results
                               if r.get("incomplete") and isinstance(r.get("detail"), str)))
    return "Stop was incomplete. " + (" ".join(details) if details else
        "Some background work may STILL BE RUNNING. Open the worker running that work and stop it there.")


NOTICE_WAIT = 1.0
_notice_writes: dict[str, asyncio.Task] = {}


async def _write_notice(notice_id: str, sid: str | None = None, data: dict | None = None) -> bool:
    from app.db import stop_notices
    from app.db.async_database import run_event_write

    if sid is not None:
        await asyncio.to_thread(stop_notices.save, notice_id, sid, data)
    return await run_event_write(stop_notices.deliver, notice_id)


async def _notice_ack(notice_id: str, sid: str | None = None, data: dict | None = None) -> bool:
    from app.core.turn_recorder import start_detached

    task = _notice_writes.get(notice_id)
    if task is None:
        task = start_detached(_write_notice(notice_id, sid, data))
        _notice_writes[notice_id] = task
        task.add_done_callback(lambda done: _notice_writes.pop(notice_id, None)
                               if _notice_writes.get(notice_id) is done else None)
    try:
        # Timeout/cancellation is NOT an acknowledgement. The in-flight write
        # may still commit; the durable outbox and stable event id make replay safe.
        return await asyncio.wait_for(asyncio.shield(task), timeout=NOTICE_WAIT)
    except Exception:
        logger.warning("Stop incomplete note not confirmed for %s", notice_id, exc_info=True)
        return False


async def record_incomplete_note(sid: str, results: list[dict], *,
                                 notice_key: str | None = None, data: dict | None = None) -> bool:
    # Do not use record_drained_event: its never-raising/queued-write contract is
    # intentionally not a durable acknowledgement. Keep warning-only authority
    # even for idle sessions or dispatches deleted before their next retry.
    notice_id = str(uuid.uuid5(uuid.NAMESPACE_URL,
                              f"stop-incomplete:{sid}:{notice_key or uuid.uuid4().hex}"))
    return await _notice_ack(notice_id, sid, {
        **(data or {}), "incomplete": True, "content": incomplete_text(results)})


async def retry_incomplete_notes(*, wait: bool = True) -> None:
    from app.db import stop_notices
    from app.core.turn_recorder import start_detached

    acknowledgements = [_notice_ack(notice_id)
                        for notice_id in await asyncio.to_thread(stop_notices.pending)]
    if wait:
        await asyncio.gather(*acknowledgements)
    else:
        # The claim observer must not wait behind warning commits before it can
        # signal interrupts or admit a new turn. Per-id writers are coalesced.
        for acknowledgement in acknowledgements:
            start_detached(acknowledgement)


async def request(task_id: str, worker: dict, scope: Any, *, guard, suppress) -> dict:
    from app.core.remote_task import STOP_WAIT
    from app.network import control
    device, name = str(worker.get("deviceId") or ""), str(worker.get("name") or worker.get("deviceId") or "the other worker")
    if not valid_scope(scope) or not await asyncio.to_thread(advertised, device):
        return incomplete(task_id, device, name, "owner boundary enforcement is unknown")
    if not guard():
        return {"ok": True, "task_id": task_id, "skipped": "resumed"}
    suppress()  # an admitted new generation clears its predecessor's latch
    try:
        answer = await asyncio.wait_for(control.call(device, ACTION, {
            "version": 1, "worker_session_id": task_id, "scope": scope,
        }), timeout=STOP_WAIT)
    except Exception:
        logger.warning("Scoped Stop could not be confirmed by %s", device, exc_info=True)
        return incomplete(task_id, device, name, "owner could not confirm the scoped Stop")
    if (not isinstance(answer, dict) or answer.get("scope_enforced") is not True
            or answer.get("ok") is not True):
        result = incomplete(task_id, device, name, "owner did not confirm the captured boundary")
        if isinstance(answer, dict) and answer.get("incomplete") is True and isinstance(answer.get("message"), str):
            result["detail"] += " " + answer["message"][:4000]
        return result
    return {"ok": True, "task_id": task_id, "scope_enforced": True}


def claim(worker_id: str, scope: dict, from_device: str) -> dict:
    """Atomically validate the owner boundary and publish only THAT Stop.

    A repeatable snapshot keeps a concurrent direct human input out of both
    the cutoff and cleanup owners/tasks. The later interrupt UPDATE has its
    own accepted-input guard; it cannot mark a resumed logical row instead.
    """
    from app.core.remote_task import _require_worker_member
    from app.db import remote_tasks, turns
    from app.db.base import get_db, utc_now_iso
    _require_worker_member(from_device)
    remote_tasks.init_schema()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        cursor.execute("SET LOCAL lock_timeout = '100ms'")
        cursor.execute("SET LOCAL statement_timeout = '1s'")
        cursor.execute("SELECT * FROM remote_task_children WHERE worker_session_id=%s", (worker_id,))
        row = cursor.fetchone()
        if not row or row["parent_device_id"] != from_device:
            raise PermissionError("Only the worker that started this child may stop it.")
        if (row.get("scope_generation") != scope["generation"] or
                str(row.get("scope_epoch")) != str(scope["epoch"]) or row.get("scope_input_order") is None):
            return {"accepted": False}
        cursor.execute("SELECT id FROM turns WHERE session_id=%s AND insert_order>%s "
                       "AND payload ->> 'person_input' IS NOT NULL LIMIT 1",
                       (worker_id, int(row["scope_input_order"])))
        if cursor.fetchone() is not None:
            return {"accepted": False}
        receipt = turns._stop_receipt(cursor, worker_id)
        cursor.execute("UPDATE remote_task_children SET stop_requested_at=COALESCE(stop_requested_at,%s) "
                       "WHERE worker_session_id=%s", (utc_now_iso(), worker_id))
        cursor.execute("SELECT id FROM turns WHERE session_id=%s AND state IN ('running','interrupting') "
                       "AND insert_order<=%s", (worker_id, receipt["input_cutoff"]))
        live = cursor.fetchone()
        return {"accepted": True, "receipt": receipt, "turn_id": live["id"] if live else None}


async def handle(body: dict, from_device: str) -> dict:
    from app.core import remote_task, session_runtime, stop_receipts
    from app.core.query_session_lifecycle import capture_stop_scope, stop_all_background_tasks
    from app.core.turn_recorder import start_detached
    from app.db import turns
    if (not isinstance(body, dict) or body.get("version") != 1 or
            not isinstance(body.get("worker_session_id"), str) or
            not 0 < len(body["worker_session_id"]) <= 64 or not valid_scope(body.get("scope"))):
        raise ValueError("A scoped Stop requires a captured generation and epoch.")
    sid = body["worker_session_id"]
    async with remote_task._start_lock("scoped:" + sid):
        scope, runtime = capture_stop_scope(sid), session_runtime.get_live_runtime(sid)
        claimed = await asyncio.to_thread(claim, sid, body["scope"], from_device)
        if not claimed["accepted"]:
            return {"scope_enforced": True, "ok": False, "incomplete": True}
        receipt, tid = claimed["receipt"], claimed["turn_id"]
        # Signal the immutable runtime concurrently with the bounded row write.
        # Its own replacement checks protect a newer runtime after every await.
        drive = None
        # A queued original may have started while the boundary was recorded.
        # Only take that runtime if its accepted input still predates the Stop.
        runtime = runtime or session_runtime.get_live_runtime(sid)
        if runtime is not None and runtime.admission_order <= receipt["input_cutoff"]:
            drive = start_detached(runtime.interrupt("stop", source="user_stop"))
        if tid:
            await asyncio.to_thread(turns.interrupt_turn, sid, "stop", True, tid,
                                    source="user_stop", stop_receipt=receipt)
        results = []
        from app.db import stop_receipts as receipts_db
        for rid in receipt.get("cleanup_receipts") or []:
            row = await asyncio.to_thread(receipts_db.get, rid)
            if row:
                results.extend(await stop_receipts.consume(row, scope=scope))
        if not receipt.get("cleanup_receipts"):
            results = await stop_all_background_tasks(sid, user_stop=True, scope=scope, receipt=receipt)
        if drive is not None:
            state = await asyncio.shield(drive)
            if state not in turns.TERMINAL_STATES:
                return {"scope_enforced": True, "ok": False, "incomplete": True}
        if drive is None:
            # A different process can own the provider. A durable interrupt is
            # not evidence that it has already ended; never forge a killed chip.
            for turn_id in dict.fromkeys(([tid] if tid else []) + (receipt.get("queued") or [])):
                row = await asyncio.to_thread(turns.get_turn, turn_id)
                accepted = ((row or {}).get("payload") or {}).get("accepted_input_order", (row or {}).get("insert_order", 0))
                if row and accepted <= receipt["input_cutoff"] and row["state"] not in turns.TERMINAL_STATES:
                    return {"scope_enforced": True, "ok": False, "incomplete": True}
        failed = any(r.get("ok") is False or r.get("incomplete") for r in results)
        return {"scope_enforced": True, "ok": not failed,
                **({"incomplete": True, "message": incomplete_text(results)} if failed else {})}
