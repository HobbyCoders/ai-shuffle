"""Approvals for a Task child that runs on another worker (slice S10).

A local Task worker's permission prompt is raised on its parent's session
(``provider_agent_approvals``). A remote child's parent lives on another
worker, so the prompt travels over the signed control channel:

* **Target** (the worker running the child): :func:`ask_parent` replaces the
  local prompt. It sends ``task-approval`` with a single-use approval id, the
  tool name and a bounded, redacted summary of its input to the worker that
  dispatched the child, then waits for the answer. The child's own tool input
  is what runs on an allow; nothing from the parent can change it.
* **Parent**: :func:`handle_task_approval` checks the request comes from the
  worker the child was sent to, then raises a proxy prompt on the root
  parent session exactly where a local child's prompt appears (the same card,
  labelled with the worker's name), or as an ``assistant_confirm`` question
  when the root is the assistant's home. The decision goes back with
  ``task-approval-answer``.
* **Target** again: :func:`handle_task_approval_answer` accepts the answer only
  from the dispatching worker, only once, and only while the tool call still
  waits. Anything else (a replay, a late answer, another worker) is refused.

What travels back is allow-once or deny. "Always allow" choices, saved rules,
the permission mode and private-access grants never travel: the proxy prompt
ignores the parent's saved rules (a person always decides), and a rule the
owner saves while answering stays on the parent.

Every failure denies, never allows: no answer within the prompt's time limit
(the parent's own TTL plus :data:`GRACE_SECONDS` for the trip), an
unreachable parent, a parent on an older release, a refusal, Stop on either
side. A denial for want of an answer is recorded as a deferred approval, so
the child's report lists it for the owner.

A parent restart while a prompt is open restores it (:func:`restore_proxies`,
run at boot): the durable prompt row stays on the root session and its answer
is still forwarded while the child waits. A target restart ends the tool call
with the child's turn; the parent's prompt is then withdrawn when the child's
report arrives, and an answer given before that is refused as late. A target
quiet long enough for the offline notice (``remote_task.notice_offline``) has
its open prompts withdrawn then; if it comes back with the child still
running, its next prompts are shown again.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Tuple

from fluid.tools.hardening import defang_harness_markers

logger = logging.getLogger(__name__)

APPROVAL_ACTION = "task-approval"
ANSWER_ACTION = "task-approval-answer"
WIRE_VERSION = 1
REQUEST_TIMEOUT = 20.0
ANSWER_TIMEOUT = 20.0
#: The target waits this much past the parent's prompt TTL for the answer to arrive.
GRACE_SECONDS = 30.0
#: Bounds on the TTL a parent may announce (seconds).
MIN_TTL_SECONDS = 10
MAX_TTL_SECONDS = 15 * 60

MAX_TOOL_NAME = 200
MAX_TITLE = 300
MAX_DESCRIPTION = 2000
MAX_REASON = 500
MAX_INPUT_KEYS = 20
MAX_KEY_CHARS = 80
MAX_VALUE_CHARS = 1000
MAX_INPUT_CHARS = 4000
MAX_MESSAGE = 300

_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_DECISIONS = ("allow", "deny")
_REQUEST_KEYS = frozenset({
    "version", "op", "approval_id", "worker_session_id", "asker", "tool_name", "title",
    "display_name", "description", "decision_reason", "input",
})
_ANSWER_KEYS = frozenset({"version", "approval_id", "worker_session_id", "decision", "message"})

NO_ANSWER_TAIL = (
    " The tool call was denied and did not run. Do not retry it or reach the same effect "
    "another way: continue with work that does not need it, and list it in your final reply "
    "as needing the owner's approval."
)


# ---------------------------------------------------------------------------
# Shared: what may travel
# ---------------------------------------------------------------------------

def _bounded(value: Any, limit: int) -> Optional[str]:
    if not isinstance(value, str) or not value.strip():
        return None
    text = defang_harness_markers(value.strip())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def summarize_input(tool_input: Any) -> Dict[str, Any]:
    """A bounded, redacted copy of a tool input for the proxy prompt.

    Recognized credentials are replaced (``export_redaction``), sensitive keys
    blanked, long values cut and deep values flattened to short JSON text.
    Never the whole input: at most :data:`MAX_INPUT_KEYS` keys and
    :data:`MAX_INPUT_CHARS` characters.
    """
    from app.core.export_redaction import redact_external_export

    data = tool_input if isinstance(tool_input, dict) else {"input": tool_input}
    redacted = redact_external_export(data)
    out: Dict[str, Any] = {}
    budget = MAX_INPUT_CHARS
    items = list(redacted.items())
    for key, value in items[:MAX_INPUT_KEYS]:
        name = str(key)[:MAX_KEY_CHARS]
        if value is None or isinstance(value, (bool, int, float)):
            out[name] = value
            continue
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
        limit = min(MAX_VALUE_CHARS, budget)
        if limit <= 1:
            out["…"] = "more fields not shown"
            break
        text = defang_harness_markers(text)
        if len(text) > limit:
            text = text[: limit - 1] + "…"
        out[name] = text
        budget -= len(text)
    if len(items) > MAX_INPUT_KEYS:
        out["…"] = f"{len(items) - MAX_INPUT_KEYS} more fields not shown"
    return out


def _ttl(value: Any) -> float:
    from app.core import permission_handler

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return float(permission_handler.PERMISSION_TTL_SECONDS)
    return float(min(max(value, MIN_TTL_SECONDS), MAX_TTL_SECONDS))


# ---------------------------------------------------------------------------
# Target side: ask the parent and wait
# ---------------------------------------------------------------------------

@dataclass
class _Waiter:
    approval_id: str
    worker_session_id: str
    parent_device_id: str
    future: "asyncio.Future[Dict[str, Any]]"
    loop: asyncio.AbstractEventLoop


#: approval id -> the tool call waiting on it. In memory on purpose: a
#: restart ends the child's turn, and with it every tool call that could use
#: an answer, so an answer for an id not here is late by definition.
_WAITING: Dict[str, _Waiter] = {}
_waiting_lock = threading.Lock()


def remote_child(session_id: Optional[str]) -> Optional[Dict[str, Any]]:
    """What ``session_id`` owes another worker when it is a remote Task child here.

    ``report_owed`` shape (``device_id``, ``parent_session_id``, ...), or None
    for every other session. Only sessions this process started as workers
    are looked up, so an ordinary chat costs nothing. Never raises.
    """
    try:
        from app.core.provider_agent import is_worker_session

        if not is_worker_session(session_id):
            return None
        from app.core.remote_task import report_owed

        return report_owed(session_id)
    except Exception:  # noqa: BLE001 — unknown reads as a local session
        logger.debug("remote approvals: child lookup failed for %s", session_id, exc_info=True)
        return None


def _request_body(approval_id: str, child: str, kwargs: Mapping[str, Any],
                  asker: Optional[str]) -> Dict[str, Any]:
    return {
        "version": WIRE_VERSION,
        "op": "request",
        "approval_id": approval_id,
        "worker_session_id": child,
        "asker": _bounded(asker, 160),
        "tool_name": _bounded(kwargs.get("tool_name"), MAX_TOOL_NAME) or "a tool",
        "title": _bounded(kwargs.get("title"), MAX_TITLE),
        "display_name": _bounded(kwargs.get("display_name"), MAX_TOOL_NAME),
        "description": _bounded(_redact_text(kwargs.get("description")), MAX_DESCRIPTION),
        "decision_reason": _bounded(_redact_text(kwargs.get("decision_reason")), MAX_REASON),
        "input": summarize_input(kwargs.get("tool_input") or {}),
    }


def _redact_text(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    from app.core.export_redaction import redact_export_text

    return redact_export_text(value)


async def _no_answer(child: str, tool_name: str, reason: str):
    """Deny for want of an answer and list it for the owner. Never raises."""
    from app.core.agent_types import PermissionResultDeny
    from app.core.away_mode import record_decision

    action = f"permission for {tool_name}"
    await record_decision(child, {"kind": "deferred", "action": action},
                          f"Not approved ({reason.rstrip('.')}): {action}")
    return PermissionResultDeny(message=f"Not approved: {reason.rstrip('.')}." + NO_ANSWER_TAIL)


def _withdraw_later(parent: str, approval_id: str, child: str) -> None:
    """Take the proxy prompt back on the parent. Best effort, detached."""
    from app.core.detached_tasks import spawn_detached

    async def _send() -> None:
        from app.network import control

        try:
            await control.call(parent, APPROVAL_ACTION, {
                "version": WIRE_VERSION, "op": "withdraw", "approval_id": approval_id,
                "worker_session_id": child}, timeout=REQUEST_TIMEOUT)
        except Exception as exc:  # noqa: BLE001 — its own TTL clears it otherwise
            logger.info("remote approvals: could not withdraw %s on %s: %s", approval_id, parent, exc)

    spawn_detached(_send(), name=f"task-approval-withdraw-{approval_id[:8]}")


async def ask_parent(kwargs: Mapping[str, Any], owed: Mapping[str, Any], *,
                     child_session_id: str, asker: Optional[str] = None):
    """Raise a remote child's permission prompt on the worker that dispatched it.

    Returns the engine's permission result: allow (with the child's own,
    unchanged input) only on the dispatching worker's allow; deny on its deny
    and on every failure. ``asker`` labels a local worker under the remote
    child whose prompt is passed up. Cancellation (Stop here) withdraws the
    prompt and propagates.
    """
    from app.core.agent_types import PermissionResultAllow, PermissionResultDeny
    from app.core.remote_task import worker_display_name
    from app.network import control

    parent = str(owed.get("device_id") or "")
    tool_name = str(kwargs.get("tool_name") or "a tool")
    name = worker_display_name(parent) or "the worker that started this subagent"
    approval_id = uuid.uuid4().hex
    loop = asyncio.get_running_loop()
    waiter = _Waiter(approval_id, child_session_id, parent, loop.create_future(), loop)
    with _waiting_lock:
        _WAITING[approval_id] = waiter
    body = _request_body(approval_id, child_session_id, kwargs, asker)
    try:
        try:
            ack = await control.call(parent, APPROVAL_ACTION, body, timeout=REQUEST_TIMEOUT)
        except control.ControlRefused as exc:
            reason = str(exc).rstrip(".")
            if "Unknown control action" in reason:
                reason = (f"{name} runs an older release that cannot show approvals for subagents "
                          "on other workers")
            else:
                reason = f"{name} refused the approval request: {reason}"
            return await _no_answer(child_session_id, tool_name, reason)
        except Exception as exc:  # noqa: BLE001 — unreachable, off-network
            from app.core.remote_task import MAY_HAVE_APPLIED

            if MAY_HAVE_APPLIED in str(exc):
                _withdraw_later(parent, approval_id, child_session_id)
            return await _no_answer(child_session_id, tool_name,
                                    f"{name} could not be reached to ask the owner ({exc})")
        ttl = _ttl(ack.get("ttl_seconds") if isinstance(ack, dict) else None)
        try:
            answer = await asyncio.wait_for(asyncio.shield(waiter.future), ttl + GRACE_SECONDS)
        except asyncio.TimeoutError:
            _withdraw_later(parent, approval_id, child_session_id)
            return await _no_answer(child_session_id, tool_name,
                                    f"nobody answered on {name} within {int(ttl // 60) or 1} min")
        except asyncio.CancelledError:
            if not waiter.future.done():
                _withdraw_later(parent, approval_id, child_session_id)
            raise
    finally:
        with _waiting_lock:
            _WAITING.pop(approval_id, None)
    if answer.get("decision") == "allow":
        return PermissionResultAllow(updated_input=kwargs.get("tool_input"))
    message = _bounded(answer.get("message"), MAX_MESSAGE) or "The owner denied permission"
    if message in NOT_ANSWERED_MESSAGES:
        # Nobody decided on the parent (its prompt lapsed, or the owner is
        # away there): list it for the owner like any other missing answer.
        return await _no_answer(child_session_id, tool_name, f"{message} on {name}")
    return PermissionResultDeny(message=f"{message.rstrip('.')} (asked on {name}).")


def _require_member(from_device: str) -> None:
    from app.core.remote_task import _require_worker_member

    _require_worker_member(from_device)


def accept_answer(body: Mapping[str, Any], from_device: str) -> Tuple[_Waiter, Dict[str, Any]]:
    """Validate and claim one answer. Raises ``PermissionError`` / ``LookupError`` / ``ValueError``.

    Single-use: a claimed approval is gone, so a replay is refused as no longer
    waiting. Only the worker the child owes its report to may answer.
    """
    _require_member(from_device)
    if not isinstance(body, dict) or body.get("version") != WIRE_VERSION:
        raise ValueError("Unsupported approval answer version")
    if set(body) - _ANSWER_KEYS:
        raise ValueError("Unknown approval answer fields")
    approval_id = body.get("approval_id")
    if not isinstance(approval_id, str) or not _ID_RE.match(approval_id):
        raise ValueError("Invalid approval_id")
    decision = body.get("decision")
    if decision not in _DECISIONS:
        raise ValueError("decision must be allow or deny")
    with _waiting_lock:
        waiter = _WAITING.get(approval_id)
        if waiter is None or waiter.future.done():
            raise LookupError("This approval is no longer waiting: it was answered, withdrawn or "
                              "timed out, so the answer was not used.")
        if waiter.parent_device_id != from_device:
            raise PermissionError("Only the worker that started this subagent may answer its approvals.")
        if body.get("worker_session_id") != waiter.worker_session_id:
            raise PermissionError("The answer names a different subagent.")
        del _WAITING[approval_id]
    answer = {"decision": decision, "message": _bounded(body.get("message"), MAX_MESSAGE)}
    return waiter, answer


def _settle(future: "asyncio.Future[Dict[str, Any]]", answer: Dict[str, Any]) -> None:
    if not future.done():
        future.set_result(answer)


async def handle_task_approval_answer(body: Dict[str, Any], from_device: str) -> Dict[str, Any]:
    """``task-approval-answer`` control action (target side)."""
    from app.db.async_database import run_sync

    waiter, answer = await run_sync(accept_answer, body, from_device)
    # The waiting tool call may live on another loop than this request.
    waiter.loop.call_soon_threadsafe(_settle, waiter.future, answer)
    return {"accepted": True}


# ---------------------------------------------------------------------------
# Parent side: show the proxy prompt and send the decision back
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _Request:
    approval_id: str
    worker_session_id: str
    target_device_id: str
    worker_name: str
    root_session_id: str
    label: str
    asker: Optional[str]
    tool_name: str
    title: Optional[str]
    display_name: Optional[str]
    description: Optional[str]
    decision_reason: Optional[str]
    tool_input: Dict[str, Any]


#: approval ids this process has shown, so a replayed request raises nothing.
_SEEN: "OrderedDict[str, None]" = OrderedDict()
_SEEN_LIMIT = 10_000
#: approval ids the target took back; their proxy wakes without answering.
_WITHDRAWN: "OrderedDict[str, None]" = OrderedDict()


def _note(store: "OrderedDict[str, None]", key: str) -> None:
    store[key] = None
    while len(store) > _SEEN_LIMIT:
        store.popitem(last=False)


def accept_request(body: Mapping[str, Any], from_device: str) -> _Request:
    """Validate a request: only from the worker the child was sent to, only
    while this worker still tracks the child. Raises ``PermissionError`` /
    ``ValueError``. Blocking."""
    from app.core.remote_task import worker_display_name
    from app.db import remote_tasks

    _require_member(from_device)
    if not isinstance(body, dict) or body.get("version") != WIRE_VERSION:
        raise ValueError("Unsupported approval request version")
    if set(body) - _REQUEST_KEYS:
        raise ValueError("Unknown approval request fields")
    if body.get("op") not in ("request", "withdraw"):
        raise ValueError("op must be request or withdraw")
    approval_id = body.get("approval_id")
    if not isinstance(approval_id, str) or not _ID_RE.match(approval_id):
        raise ValueError("Invalid approval_id")
    child = body.get("worker_session_id")
    if not isinstance(child, str) or not child or len(child) > 64:
        raise ValueError("Invalid worker_session_id")
    dispatch = remote_tasks.get_dispatch(child)
    if not dispatch or dispatch["target_device_id"] != from_device:
        raise PermissionError("No subagent with this id was sent to this worker.")
    raw_input = body.get("input")
    if body.get("op") == "request" and not isinstance(raw_input, dict):
        raise ValueError("input must be an object")
    name = dispatch.get("target_name") or worker_display_name(from_device)
    return _Request(
        approval_id=approval_id, worker_session_id=child, target_device_id=from_device,
        worker_name=str(name), root_session_id=dispatch["root_session_id"],
        label=_bounded(dispatch.get("label"), 160) or "Task worker",
        asker=_bounded(body.get("asker"), 160),
        tool_name=_bounded(body.get("tool_name"), MAX_TOOL_NAME) or "a tool",
        title=_bounded(body.get("title"), MAX_TITLE),
        display_name=_bounded(body.get("display_name"), MAX_TOOL_NAME),
        description=_bounded(body.get("description"), MAX_DESCRIPTION),
        decision_reason=_bounded(body.get("decision_reason"), MAX_REASON),
        tool_input=summarize_input(raw_input or {}),
    )


def _still_running(request: _Request) -> bool:
    """Whether this worker still tracks the child as running. Blocking-safe."""
    from app.core.task_registry import task_registry
    from app.db import remote_tasks

    dispatch = remote_tasks.get_dispatch(request.worker_session_id)
    # A stop asked for here (S8) is durable: its approvals are not shown even
    # before the target answers it or when no record here settled.
    if not dispatch or dispatch.get("completion_id") or dispatch.get("stop_requested_at"):
        return False
    if dispatch.get("offline_notice_at"):
        # Its worker was quiet long enough that the caller stopped waiting
        # (S12) and its record here settled; the worker is back and the child
        # still runs, so a person decides its tool calls as before.
        return True
    record = task_registry.get_task(request.root_session_id, request.worker_session_id)
    return record is None or record.get("status") == "running"


def _route(request: _Request):
    from app.core.provider_agent_approvals import WorkerApprovalRoute

    label = f"{request.asker} under {request.label}" if request.asker else request.label
    return WorkerApprovalRoute(
        worker_session_id=request.worker_session_id, parent_session_id=request.root_session_id,
        label=f"{label} on {request.worker_name}",
        requester="a subagent on another worker",
    )


def _proxy_tag(request: _Request, route) -> Dict[str, Any]:
    return {**route.as_payload(), "remote_device_id": request.target_device_id,
            "worker_name": request.worker_name, "remote_approval_id": request.approval_id}


def _confirm_marker(request: _Request, route) -> Optional[Dict[str, Any]]:
    """The ``assistant_confirm`` marker when the root is the assistant's home. Never raises."""
    try:
        from app.core import assistant_confirm
        from app.core.assistant_service import is_primary_session

        if not is_primary_session(request.root_session_id):
            return None
        return assistant_confirm.marker(request.tool_name, request.tool_input,
                                        chat_title=route.label,
                                        chat_session_id=request.worker_session_id)
    except Exception:  # noqa: BLE001 — an ordinary card is the fallback
        logger.debug("remote approvals: confirm marker failed", exc_info=True)
        return None


def _profile_of(session_id: str) -> str:
    from app.db import database

    row = database.get_session(session_id) or {}
    return str(row.get("profile_id") or "default")


def _ttl_for(confirm: Optional[Dict[str, Any]]) -> int:
    from app.core import permission_handler

    if confirm is not None:
        from app.core.assistant_confirm import TTL_SECONDS

        return int(TTL_SECONDS)
    return int(permission_handler.PERMISSION_TTL_SECONDS)


CANCELLED_MESSAGE = "The approval was withdrawn on the parent worker (the subagent was stopped there)"
TIMED_OUT_MESSAGE = "Nobody answered the approval in time"
AWAY_MESSAGE = "The owner is away, so the approval was deferred and not granted"
#: Denials that mean no person decided: the target lists them for the owner.
NOT_ANSWERED_MESSAGES = frozenset({TIMED_OUT_MESSAGE, AWAY_MESSAGE})


def _decision_of(result: Any) -> Tuple[str, Optional[str]]:
    from app.core.agent_types import PermissionResultAllow

    if isinstance(result, PermissionResultAllow) or getattr(result, "behavior", None) == "allow":
        return "allow", None
    message = getattr(result, "message", None)
    if message == "Permission request timed out":
        return "deny", TIMED_OUT_MESSAGE
    if message == "Request cancelled":
        return "deny", CANCELLED_MESSAGE
    if isinstance(message, str) and message.startswith("Owner away"):
        return "deny", AWAY_MESSAGE
    if message == "User denied permission":
        return "deny", "The owner denied permission"
    return "deny", (message or "Not approved")


async def _send_answer(request: _Request, decision: str, message: Optional[str]) -> None:
    """Send the decision to the target. One attempt; its own deadline covers a loss."""
    from app.network import control

    if request.approval_id in _WITHDRAWN:
        return
    try:
        await control.call(request.target_device_id, ANSWER_ACTION, {
            "version": WIRE_VERSION, "approval_id": request.approval_id,
            "worker_session_id": request.worker_session_id, "decision": decision,
            "message": message}, timeout=ANSWER_TIMEOUT)
    except Exception as exc:  # noqa: BLE001 — the target denies on its own deadline
        logger.info("remote approvals: answer for %s not delivered to %s: %s",
                    request.approval_id, request.worker_name, exc)


async def _proxy(request: _Request, confirm: Optional[Dict[str, Any]]) -> None:
    """Show the proxy prompt, wait for the owner, send the decision back."""
    from app.core import provider_agent_approvals as approvals
    from app.core.permission_handler import permission_handler

    route = _route(request)
    try:
        profile_id = await asyncio.to_thread(_profile_of, request.root_session_id)
        kwargs = approvals.redirect_kwargs(route, {
            "request_id": f"perm-{request.worker_session_id}-{request.approval_id[:12]}",
            "session_id": request.worker_session_id,
            "profile_id": profile_id,
            "tool_name": request.tool_name,
            "tool_input": request.tool_input,
            "broadcast_func": None,
            "title": request.title,
            "display_name": request.display_name,
            "description": request.description,
            "decision_reason": request.decision_reason,
        })
        kwargs["provider_agent"] = _proxy_tag(request, route)
        # A person always decides: the parent's saved rules do not reach
        # another worker's tool calls.
        kwargs["skip_saved_rules"] = True
        if confirm is not None:
            kwargs["assistant_confirm"] = confirm
        result = await permission_handler.request_permission(**kwargs)
        decision, message = _decision_of(result)
    # Cancellation (this worker shutting down) sends nothing: the durable row
    # stays pending and restore_proxies picks it up at the next boot, while the
    # child keeps waiting out its own deadline.
    except Exception:  # noqa: BLE001 — the tool call must resolve as a deny
        logger.exception("remote approvals: proxy prompt for %s failed", request.approval_id)
        decision, message = "deny", "The approval could not be shown on the parent worker"
    await _send_answer(request, decision, message)


def _seconds_left(expires: Any) -> float:
    """Seconds until a row's ``expires_at`` (a datetime or ISO text); 0 when unknown."""
    from datetime import datetime, timezone

    moment: Optional[datetime] = None
    if isinstance(expires, datetime):
        moment = expires
    elif isinstance(expires, str):
        try:
            moment = datetime.fromisoformat(expires)
        except ValueError:
            return 0.0
    if moment is None:
        return 0.0
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp() - datetime.now(timezone.utc).timestamp()


async def _follow_existing(request: _Request, row: Mapping[str, Any]) -> None:
    """Wait on a proxy prompt a previous process raised, then answer (restore)."""
    from app.core import interaction_wait

    final = await interaction_wait.await_interaction(row["id"], request.root_session_id,
                                                     max(1.0, _seconds_left(row.get("expires_at"))))
    state = (final or {}).get("state")
    answer = (final or {}).get("answer") or {}
    if state == "answered" and answer.get("decision") == "allow":
        decision, message = "allow", None
    elif state == "answered":
        decision, message = "deny", ("The owner denied permission" if answer.get("decision") == "deny"
                                     else "Not approved")
    elif state == "cancelled":
        decision, message = "deny", CANCELLED_MESSAGE
    else:
        decision, message = "deny", TIMED_OUT_MESSAGE
    await _send_answer(request, decision, message)


def _spawn(coro, name: str) -> None:
    """Run a proxy detached from the request (a seam for tests)."""
    from app.core.detached_tasks import spawn_detached

    spawn_detached(coro, name=name)


def _proxy_rows(root_session_id: str, worker_session_id: Optional[str] = None,
                approval_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Pending proxy prompts on a root session. Blocking."""
    from app.core.permission_handler import PERMISSION_KIND
    from app.db import interactions

    rows = []
    for row in interactions.get_pending_interactions(root_session_id):
        tag = (row.get("payload") or {}).get("provider_agent")
        if row.get("kind") != PERMISSION_KIND or not isinstance(tag, dict):
            continue
        if not tag.get("remote_approval_id"):
            continue
        if worker_session_id and tag.get("worker_session_id") != worker_session_id:
            continue
        if approval_id and tag.get("remote_approval_id") != approval_id:
            continue
        rows.append(row)
    return rows


def _cancel_rows(root_session_id: str, rows: List[Dict[str, Any]]) -> int:
    from app.core import interaction_wait
    from app.db import interactions

    cancelled = sum(1 for row in rows if interactions.cancel_interaction(row["id"]))
    if cancelled:
        interaction_wait.notify_changed(root_session_id)
    return cancelled


async def handle_task_approval(body: Dict[str, Any], from_device: str) -> Dict[str, Any]:
    """``task-approval`` control action (parent side): show or withdraw a proxy prompt."""
    from app.core.task_registry import task_registry
    from app.db.async_database import run_sync

    request = await run_sync(accept_request, body, from_device)
    if body.get("op") == "withdraw":
        _note(_WITHDRAWN, request.approval_id)
        rows = await run_sync(_proxy_rows, request.root_session_id, request.worker_session_id,
                              request.approval_id)
        return {"withdrawn": await run_sync(_cancel_rows, request.root_session_id, rows)}
    await task_registry.ensure_hydrated(request.root_session_id)
    if not await run_sync(_still_running, request):
        raise PermissionError("This subagent is no longer running here (stopped or finished), "
                              "so its approval was not shown.")
    route = _route(request)
    confirm = await run_sync(_confirm_marker, request, route)
    ttl = _ttl_for(confirm)
    if request.approval_id in _SEEN:
        return {"accepted": True, "duplicate": True, "ttl_seconds": ttl}
    _note(_SEEN, request.approval_id)
    _spawn(_proxy(request, confirm), f"task-approval-{request.approval_id[:8]}")
    logger.info("remote approvals: %s asks for %s for subagent %s", request.worker_name,
                request.tool_name, request.worker_session_id)
    return {"accepted": True, "ttl_seconds": ttl}


async def restore_proxies() -> int:
    """Re-attach proxy prompts still open after this worker restarted.

    The rows are durable on the root sessions; their waiters died with the old
    process. Each gets a waiter again, so an answer still reaches the child
    while it waits. Returns how many were restored. Never raises.
    """
    from app.db import remote_tasks

    restored = 0
    try:
        dispatches = await asyncio.to_thread(remote_tasks.tracked_dispatches)
    except Exception:  # noqa: BLE001 — nothing to restore
        logger.debug("remote approvals: restore read failed", exc_info=True)
        return 0
    for dispatch in dispatches:
        try:
            rows = await asyncio.to_thread(_proxy_rows, dispatch["root_session_id"],
                                           dispatch["worker_session_id"])
            for row in rows:
                tag = (row.get("payload") or {}).get("provider_agent") or {}
                approval_id = tag.get("remote_approval_id")
                if not approval_id or approval_id in _SEEN:
                    continue
                request = _Request(
                    approval_id=approval_id, worker_session_id=dispatch["worker_session_id"],
                    target_device_id=dispatch["target_device_id"],
                    worker_name=str(dispatch.get("target_name") or dispatch["target_device_id"]),
                    root_session_id=dispatch["root_session_id"], label="", asker=None,
                    tool_name="", title=None, display_name=None, description=None,
                    decision_reason=None, tool_input={})
                _note(_SEEN, approval_id)
                _spawn(_follow_existing(request, row), f"task-approval-restore-{approval_id[:8]}")
                restored += 1
        except Exception:  # noqa: BLE001 — one child never blocks the others
            logger.warning("remote approvals: could not restore prompts of %s",
                           dispatch.get("worker_session_id"), exc_info=True)
    return restored


def drop_proxies(root_session_id: str, worker_session_id: str) -> int:
    """Withdraw a finished child's leftover proxy prompts. Blocking; never raises."""
    try:
        return _cancel_rows(root_session_id, _proxy_rows(root_session_id, worker_session_id))
    except Exception:  # noqa: BLE001 — their own TTL clears them otherwise
        logger.debug("remote approvals: could not drop prompts of %s", worker_session_id, exc_info=True)
        return 0


def reset() -> None:
    """Forget in-process state (tests only)."""
    with _waiting_lock:
        _WAITING.clear()
    _SEEN.clear()
    _WITHDRAWN.clear()
