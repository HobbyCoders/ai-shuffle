"""Stopping one background task of a chat that runs on another worker.

A chat owned by another worker is a read-only mirror here: its shells and
subagents run there, and this worker has nothing of theirs to stop. The "x"
on a running task is forwarded to the owner, as the chat's Stop already is.
"""
import logging
from typing import Optional

from app.db.async_database import run_sync
from app.network import control, engine

logger = logging.getLogger(__name__)

ACTION = "session-task-stop"


async def forward_task_stop(session: dict, task_id: str) -> Optional[dict]:
    """The owner's answer to stopping ``task_id``, or None when the chat is ours.

    Never raises for a network failure: the answer is ``ok: false`` with a
    ``detail`` that says why, so the UI can show it.
    """
    owner = await run_sync(control.owner_of, session)
    if owner is None:
        return None
    try:
        result = await control.call(owner, ACTION, {"session_id": session["id"], "task_id": task_id})
    except control.ControlRefused as exc:
        if "Unknown control action" in str(exc):
            name, _ = await run_sync(control._names, engine.require_context(), owner)
            detail = (f"{name} needs an update to stop a single task. "
                      "Update it, or use Stop to stop everything in this chat.")
        else:
            detail = str(exc) or "The worker that runs this chat refused the stop."
        return _failed(task_id, detail)
    except control.RunnerUnreachable as exc:
        # MaybeApplied included: the stop may have landed, and the row will
        # settle from the owner's events if it did.
        logger.info("Task stop for %s/%s did not reach %s: %s", session.get("id"), task_id, owner, exc)
        return _failed(task_id, str(exc))
    if not isinstance(result, dict) or not isinstance(result.get("ok"), bool):
        return _failed(task_id, "The worker that runs this chat sent an answer that could not be read.")
    return result


def _failed(task_id: str, detail: str) -> dict:
    # ``message`` is for the person: the UI shows it in place of its own guess.
    return {"ok": False, "kind": "agent", "task_id": task_id, "detail": detail,
            "message": f"Couldn't stop the task. {detail}"}


async def handle_task_stop(body: dict, from_device: str) -> dict:
    """Owner side: stop one background task of a chat this worker runs.

    Device-token authentication is enforced by /peer/control. Ownership is
    checked again here so a peer cannot turn this worker into a relay or make
    it act on a mirror of its own.
    """
    from app.core.query_engine import stop_session_task

    session_id = str(body.get("session_id") or "")
    task_id = body.get("task_id")
    if not isinstance(task_id, str) or not task_id or len(task_id) > 200:
        raise ValueError("Invalid task id")
    await run_sync(control._require_owned, session_id)
    logger.info("Worker %s stopped task %s of %s", from_device, task_id, session_id)
    return await stop_session_task(session_id, task_id)
