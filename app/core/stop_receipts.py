"""Replay an owning installation's captured Stop cleanup, never a newer turn."""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)
_running: dict[str, asyncio.Task] = {}


async def consume(row: dict, *, scope=None) -> list[dict]:
    receipt_id = row["id"]
    current = _running.get(receipt_id)
    if current is not None and current is not asyncio.current_task():
        return await asyncio.shield(current)
    task = asyncio.current_task()
    _running[receipt_id] = task
    try:
        return await _consume(row, scope=scope)
    finally:
        if _running.get(receipt_id) is task:
            _running.pop(receipt_id, None)


async def _consume(row: dict, *, scope=None) -> list[dict]:
    from app.core.query_session_lifecycle import capture_stop_scope, stop_all_background_tasks
    from app.core.task_registry import task_registry
    from app.db import stop_receipts

    current = await asyncio.to_thread(stop_receipts.get, row["id"])
    if current is None:
        return []
    if current["consumed_at"] is not None:
        return (current["result"] or {}).get("results") or []
    row = current
    sid, receipt = row["session_id"], row["receipt"]
    await task_registry.ensure_hydrated(sid)
    # A deletion may already have removed the mutable mirrors. Keep the
    # captured identities available, but never overwrite a newer local run.
    task_registry.hydrate_rows(sid, receipt.get("tasks") or [])
    scope = scope or capture_stop_scope(sid, before_turn=int(row["input_cutoff"]) + 1)
    try:
        results = await asyncio.wait_for(stop_all_background_tasks(
            sid, user_stop=True, scope=scope, receipt=receipt, _receipt_consumer=True,
        ), timeout=30)
    except Exception as exc:
        logger.warning("Stop receipt %s cleanup failed", row["id"], exc_info=True)
        results = [{"ok": False, "incomplete": True, "reason": type(exc).__name__}]
    missing = [task["task_id"] for task in receipt.get("tasks") or []
               if task_registry.get_task(sid, task["task_id"]) is None]
    results.extend({"ok": False, "task_id": tid, "incomplete": True,
                    "reason": "captured task identity unavailable"} for tid in missing)
    incomplete = any(r.get("incomplete") or r.get("ok") is False for r in results)
    if incomplete:
        try:
            from app.core.turn_recorder import record_drained_event
            from app.core.scoped_remote_stop import incomplete_text
            await record_drained_event(sid, {
                "type": "system", "subtype": "stop_incomplete",
                "data": {"interrupted_turn_id": row["turn_id"], "stop_receipt_id": row["id"],
                         "incomplete": True,
                         "content": incomplete_text(results),
                         "tasks": [r["task_id"] for r in results if r.get("task_id") and r.get("incomplete")]},
            })
        except Exception:
            logger.warning("Stop receipt %s incomplete note failed", row["id"], exc_info=True)
    # The result and consumed marker commit together, only AFTER the bounded
    # captured-scope attempt. A crash before this commit safely replays it.
    await asyncio.to_thread(stop_receipts.finish, row["id"],
                            {"incomplete": incomplete, "results": results})
    return results


async def observe(instance_id: str) -> None:
    from app.db import stop_receipts

    rows = await asyncio.to_thread(stop_receipts.pending, instance_id)
    for row in rows:
        if row["id"] in _running:
            continue
        task = asyncio.create_task(consume(row))
        _running[row["id"]] = task
        task.add_done_callback(lambda t: t.cancelled() or t.exception())
