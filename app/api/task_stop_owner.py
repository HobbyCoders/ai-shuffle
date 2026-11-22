"""Stopping one background task (or swarm) of a chat that runs on another worker.

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


# A swarm row's "x" (cancel the whole workflow) has the same problem: its
# coordinator runs on the worker that runs the chat.
SWARM_ACTION = "session-swarm-cancel"


async def forward_swarm_cancel(session: dict, swarm_id: str) -> Optional[dict]:
    """The owner's answer to cancelling ``swarm_id``, or None when the chat is ours.

    The answer is ``{"swarm": ...}`` on success, else ``{"error": "unknown" |
    "unavailable", "detail": ...}`` so the caller keeps its 404/409 split.
    """
    owner = await run_sync(control.owner_of, session)
    if owner is None:
        return None
    try:
        result = await control.call(owner, SWARM_ACTION, {"session_id": session["id"], "swarm_id": swarm_id})
    except control.ControlRefused as exc:
        if "Unknown control action" in str(exc):
            name, _ = await run_sync(control._names, engine.require_context(), owner)
            return {"error": "unavailable", "detail": f"{name} needs an update to cancel a swarm from here. "
                    "Update it, or use Stop to stop everything in this chat."}
        return {"error": "unavailable", "detail": str(exc) or "The worker that runs this chat refused."}
    except control.RunnerUnreachable as exc:
        logger.info("Swarm cancel for %s/%s did not reach %s: %s", session.get("id"), swarm_id, owner, exc)
        return {"error": "unavailable", "detail": f"Couldn't cancel the swarm. {exc}"}
    if not isinstance(result, dict) or not ("swarm" in result or result.get("error") in ("unknown", "unavailable")):
        return {"error": "unavailable", "detail": "The worker that runs this chat sent an answer that could not be read."}
    return result


async def handle_swarm_cancel(body: dict, from_device: str) -> dict:
    """Owner side: cancel one swarm of a chat this worker runs."""
    from app.core import swarm

    session_id = str(body.get("session_id") or "")
    swarm_id = body.get("swarm_id")
    if not isinstance(swarm_id, str) or not swarm_id or len(swarm_id) > 200:
        raise ValueError("Invalid swarm id")
    await run_sync(control._require_owned, session_id)
    logger.info("Worker %s cancelled swarm %s of %s", from_device, swarm_id, session_id)
    try:
        return {"swarm": await swarm.cancel(session_id, swarm_id)}
    except ValueError as exc:
        return {"error": "unknown", "detail": str(exc)}
    except swarm.SwarmUnavailableError as exc:
        return {"error": "unavailable", "detail": str(exc)}


# A row paused by a usage limit is stopped by cancelling its scheduled resume,
# and that wakeup is armed on the worker that runs the chat.
RESUME_ACTION = "session-paused-task-cancel"


async def forward_paused_task_cancel(session: dict, task_id: str) -> Optional[dict]:
    """The owner's ``{"cancelled": bool}``, ``{"error": "unknown" | "unavailable",
    "detail": ...}``, or None when the chat is ours."""
    owner = await run_sync(control.owner_of, session)
    if owner is None:
        return None
    try:
        result = await control.call(owner, RESUME_ACTION, {"session_id": session["id"], "task_id": task_id})
    except control.ControlRefused as exc:
        if "Unknown control action" in str(exc):
            name, _ = await run_sync(control._names, engine.require_context(), owner)
            return {"error": "unavailable", "detail": f"{name} needs an update to stop a paused task from here. "
                    "Update it, or use Stop to stop everything in this chat."}
        return {"error": "unavailable", "detail": str(exc) or "The worker that runs this chat refused."}
    except control.RunnerUnreachable as exc:
        logger.info("Paused task cancel for %s/%s did not reach %s: %s", session.get("id"), task_id, owner, exc)
        return {"error": "unavailable", "detail": str(exc)}
    if not isinstance(result, dict) or not (isinstance(result.get("cancelled"), bool)
                                            or result.get("error") == "unknown"):
        return {"error": "unavailable", "detail": "The worker that runs this chat sent an answer that could not be read."}
    return result


async def handle_paused_task_cancel(body: dict, from_device: str) -> dict:
    """Owner side: cancel the scheduled resume of one paused worker of a chat
    this worker runs. Only a worker of that chat: never another chat's."""
    from app.core import limit_resume

    session_id = str(body.get("session_id") or "")
    task_id = body.get("task_id")
    if not isinstance(task_id, str) or not task_id or len(task_id) > 200:
        raise ValueError("Invalid task id")
    await run_sync(control._require_owned, session_id)
    if task_id not in await run_sync(limit_resume.paused_workers_of, session_id):
        return {"error": "unknown", "detail": "No paused worker with that id"}
    logger.info("Worker %s cancelled the paused task %s of %s", from_device, task_id, session_id)
    return {"cancelled": await limit_resume.cancel(task_id)}
