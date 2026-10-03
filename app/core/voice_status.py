"""Read-only work status for a live call on a conversation.

A caller asks "what are you working on?" or "is it done yet?". The answer comes
from the app's own task state: the conversation's live or queued chat turns,
whether it is parked on background work, its running background jobs with
their plain labels, finished jobs whose report the chat has not taken in yet,
and decisions waiting for the user. No model is called and no chat turn starts.

Both voice transports use it: the Realtime sideband declares it as the
``check_work`` tool, and GPT Live (which has no function tools, only
delegation) answers a pure status question from it instead of handing the
question to the chat agent.

Labels are short public titles. Tool input, output and ids never appear.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

MAX_LISTED = 6
LABEL_MAX_CHARS = 80
SUMMARY_MAX_CHARS = 160

CHECK_WORK_TOOL = {
    "type": "function", "name": "check_work",
    "description": (
        "Read what this conversation is doing right now: whether the chat agent "
        "is working or waiting on background jobs, the running jobs by name, "
        "finished jobs with a report waiting, and decisions waiting for the "
        "user. Read-only and instant; it never starts work. Use it when the "
        "caller asks what you are working on, how it is going or whether "
        "something is done."
    ),
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
}


def _label(value: Any, limit: int = LABEL_MAX_CHARS) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[:limit - 1].rsplit(" ", 1)[0] + "…"


def _task_label(task: Dict[str, Any]) -> str:
    return _label(task.get("subject") or task.get("title") or task.get("agent")
                  or task.get("name") or "Background job")


def work_status(session_id: str) -> Dict[str, Any]:
    """Snapshot of the conversation's work. Synchronous: call via ``run_sync``.

    Every source is read independently, so one failing read leaves the rest
    of the answer intact rather than reporting nothing.
    """
    status: Dict[str, Any] = {
        "chat": "idle", "queued_requests": 0, "running": [],
        "finished_waiting": [], "approvals_waiting": 0, "on_screen_waiting": 0,
    }
    try:
        from app.db import turns
        live = turns.get_live_turn(session_id)
        if live:
            status["chat"] = "working"
        else:
            latest = turns.get_latest_turn(session_id) or {}
            if latest.get("state") == "parked":
                status["chat"] = "waiting_on_background"
        status["queued_requests"] = len(turns.get_queued_turns(session_id))
    except Exception:  # noqa: BLE001 - a partial answer beats none
        logger.debug("Voice status: turn read failed for %s", session_id, exc_info=True)

    running: Dict[str, Dict[str, Any]] = {}
    finished: List[Dict[str, Any]] = []
    try:
        from app.core.task_registry import task_registry
        for task in task_registry.get_active(session_id):
            running[str(task.get("id"))] = {"label": _task_label(task), "kind": task.get("kind") or ""}
        for task in task_registry.finished_awaiting_report(session_id):
            finished.append({
                "label": _task_label(task),
                "status": "failed" if task.get("status") in ("error", "killed") else "finished",
                "summary": _label(task.get("summary"), SUMMARY_MAX_CHARS),
            })
    except Exception:  # noqa: BLE001
        logger.debug("Voice status: registry read failed for %s", session_id, exc_info=True)
    try:
        # The durable mirror covers work the in-memory registry has not seen
        # in this process (it is rebuilt empty on restart).
        from app.db import background_tasks
        for row in background_tasks.list_session_nonterminal_background_tasks(session_id):
            running.setdefault(str(row.get("task_id")), {"label": _task_label(row), "kind": row.get("kind") or ""})
    except Exception:  # noqa: BLE001
        logger.debug("Voice status: task rows unavailable for %s", session_id, exc_info=True)
    status["running"] = list(running.values())[:MAX_LISTED]
    status["running_count"] = len(running)
    status["finished_waiting"] = finished[:MAX_LISTED]
    if status["chat"] == "idle" and running:
        status["chat"] = "waiting_on_background"

    try:
        from app.db import interactions
        for row in interactions.get_pending_interactions(session_id):
            if row.get("kind") == "permission":
                status["approvals_waiting"] += 1
            else:
                status["on_screen_waiting"] += 1
    except Exception:  # noqa: BLE001
        logger.debug("Voice status: interaction read failed for %s", session_id, exc_info=True)
    try:
        from app.core.chat_access import broker
        status["on_screen_waiting"] += sum(
            1 for action in list(broker.actions.values())
            if action.session_id == session_id and not action.decision.done()
        ) + sum(
            1 for request in list(broker.requests.values())
            if request.session_id == session_id and not request.result.done()
        )
    except Exception:  # noqa: BLE001
        logger.debug("Voice status: private-access read failed for %s", session_id, exc_info=True)
    return status


def _plural(count: int, one: str, many: str) -> str:
    return f"{count} {one if count == 1 else many}"


def status_sentence(status: Dict[str, Any]) -> str:
    """The snapshot as a few plain sentences the voice can say or keep."""
    parts: List[str] = []
    chat = status.get("chat")
    if chat == "working":
        parts.append("The chat agent is working on a request right now.")
    elif chat == "waiting_on_background":
        parts.append("The chat agent is waiting for background jobs to finish.")
    queued = int(status.get("queued_requests") or 0)
    if queued:
        parts.append(_plural(queued, "more request is", "more requests are") + " queued after it.")
    running = status.get("running") or []
    count = int(status.get("running_count") or len(running))
    if count:
        names = "; ".join(task["label"] for task in running)
        more = count - len(running)
        tail = f"; and {more} more" if more > 0 else ""
        parts.append(f"{_plural(count, 'background job is', 'background jobs are')} running: {names}{tail}.")
    finished = status.get("finished_waiting") or []
    if finished:
        items = "; ".join(
            f"{task['label']} ({task['status']}{': ' + task['summary'] if task.get('summary') else ''})"
            for task in finished
        )
        parts.append(f"{_plural(len(finished), 'job has', 'jobs have')} finished with a report waiting: {items}.")
    approvals = int(status.get("approvals_waiting") or 0)
    if approvals:
        parts.append(_plural(approvals, "approval is", "approvals are") + " waiting for the user.")
    on_screen = int(status.get("on_screen_waiting") or 0)
    if on_screen:
        parts.append(_plural(on_screen, "request needs", "requests need") + " the user on screen.")
    if not parts:
        return "Nothing is running. No background jobs, approvals or reports are waiting."
    return " ".join(parts)


def has_news(status: Dict[str, Any]) -> bool:
    """Whether the snapshot says anything beyond "nothing is running"."""
    return bool(status.get("chat") != "idle" or status.get("queued_requests")
                or status.get("running") or status.get("finished_waiting")
                or status.get("approvals_waiting") or status.get("on_screen_waiting"))


# A pure status question, said on its own. Anything longer, or joined to
# another request ("and also…"), goes to the chat agent as before.
_LEADING_FILLER = re.compile(r"^(?:(?:hey|so|okay|ok|um|uh|well|and|alright|quick question)\b[\s,]*)+")
_STATUS_PATTERNS = tuple(re.compile(p) for p in (
    r"^what(?:'s| is| are)? (?:are )?you (?:working on|doing|up to|busy with)(?: right now| now| at the moment)?$",
    r"^what(?:'s| is) (?:going on|happening|running|the status|the progress|left)(?: right now| now)?$",
    r"^(?:is|are) (?:it|that|they|those|this|these|everything|the (?:job|jobs|task|tasks|work|agent|agents))(?: all)? "
    r"(?:done|finished|ready|complete|completed|still running|still going)(?: yet| now)?$",
    r"^(?:are you|is the agent|is the chat agent) (?:done|finished|still working|still busy)(?: yet| now)?$",
    r"^how(?:'s| is| are) (?:it|that|things|the work|everything|the job|the jobs|the task|the tasks|progress)"
    r"(?: going| coming along| looking)?(?: so far)?$",
    r"^(?:any|got any) (?:news|update|updates|progress)(?: yet| so far)?$",
    r"^(?:what's the |give me a |quick )?status(?: update| check)?$",
    r"^(?:is anything|is something) (?:running|still running|happening)(?: right now| now)?$",
))


def is_status_question(text: str) -> bool:
    """True when ``text`` is only a question about current work."""
    normal = " ".join(re.sub(r"[^\w\s']", " ", str(text or "").lower().replace("’", "'")).split())
    normal = _LEADING_FILLER.sub("", normal).strip()
    if not normal or len(normal.split()) > 12:
        return False
    return any(pattern.match(normal) for pattern in _STATUS_PATTERNS)
