"""Replay an owning installation's captured Stop cleanup, never a newer turn."""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)
_running: dict[str, asyncio.Task] = {}
# One local incarnation snapshot per unfinished receipt. A task mirror may not
# have flushed at publication, so the first owner observation can add proven
# older local work, but retries must never recapture a resumed incarnation.
_scopes: dict[str, object] = {}


def _start_consume(row: dict, *, scope=None) -> asyncio.Task:
    from app.core.turn_recorder import start_detached

    receipt_id = row["id"]
    task = _running.get(receipt_id)
    if task is None or task.done():
        # Own just the cleanup result, never an endpoint/runtime/observer's
        # entire caller task or its unrelated tail. Cancellation of a waiter
        # must not cancel or duplicate the captured cleanup.
        task = start_detached(_consume(row, scope=scope))
        _running[receipt_id] = task
        task.add_done_callback(lambda done: _running.pop(receipt_id, None)
                               if _running.get(receipt_id) is done else None)
    return task


async def consume(row: dict, *, scope=None) -> list[dict]:
    return await asyncio.shield(_start_consume(row, scope=scope))


async def _consume(row: dict, *, scope=None) -> list[dict]:
    from app.core.query_session_lifecycle import capture_stop_scope, stop_all_background_tasks
    from app.core.task_registry import task_registry
    from app.db import stop_receipts

    current = await asyncio.to_thread(stop_receipts.get, row["id"])
    if current is None:
        _scopes.pop(row["id"], None)
        return []
    if current["consumed_at"] is not None:
        _scopes.pop(row["id"], None)
        return (current["result"] or {}).get("results") or []
    row = current
    sid, receipt = row["session_id"], row["receipt"]
    result = row.get("result")
    if result is None:
        await task_registry.ensure_hydrated(sid)
        # A deletion may already have removed the mutable mirrors. Keep the
        # captured identities available, but never overwrite a newer local run.
        task_registry.hydrate_rows(sid, receipt.get("tasks") or [])
        scope = _scopes.get(row["id"], scope)
        if scope is None:
            scope = capture_stop_scope(sid, before_turn=int(row["input_cutoff"]) + 1)
        _scopes.setdefault(row["id"], scope)
        try:
            results = await asyncio.wait_for(stop_all_background_tasks(
                sid, user_stop=True, scope=scope, receipt=receipt, _receipt_consumer=True,
            ), timeout=30)
        except Exception as exc:
            logger.warning("Stop receipt %s cleanup failed", row["id"], exc_info=True)
            results = [{"ok": False, "incomplete": True, "retryable": True,
                        "reason": type(exc).__name__}]
        missing = [task["task_id"] for task in receipt.get("tasks") or []
                   if task_registry.get_task(sid, task["task_id"]) is None]
        results.extend({"ok": False, "task_id": tid, "incomplete": True,
                        "reason": "captured task identity unavailable"} for tid in missing)
        result = {"incomplete": any(r.get("incomplete") or r.get("ok") is False for r in results),
                  "results": results}
        # A warning is not completion. Leave failed validation/unfinished
        # cleanup uncheckpointed so the owner observer replays its same receipt.
        if not any(r.get("retryable") for r in results):
            await asyncio.to_thread(stop_receipts.record_result, row["id"], result)
    results = result["results"]
    if result["incomplete"]:
        from app.core.scoped_remote_stop import record_incomplete_note
        noted = await record_incomplete_note(sid, results, notice_key=f"receipt:{row['id']}",
            data={"interrupted_turn_id": row["turn_id"], "stop_receipt_id": row["id"],
                  "tasks": [r["task_id"] for r in results if r.get("task_id") and r.get("incomplete")]})
        if not noted:
            return results
    if any(r.get("retryable") for r in results):
        return results
    # Consume only after a committed warning (if needed), never a queued write.
    await asyncio.to_thread(stop_receipts.finish, row["id"], result)
    _scopes.pop(row["id"], None)
    return results


async def observe(instance_id: str) -> None:
    from app.db import stop_receipts
    from app.core.scoped_remote_stop import retry_incomplete_notes

    await retry_incomplete_notes(wait=False)
    rows = await asyncio.to_thread(stop_receipts.pending, instance_id)
    for row in rows:
        _start_consume(row)
