"""Task subagents that run on another worker of the same account.

Two halves joined by the worker session id, which the parent allocates and
the target uses as its idempotency key:

* **Parent** (:func:`dispatch_remote`, called by ``task_dispatch``): publish
  ``task/<worker id>`` to origin, ask the target to start the child with the
  ``task-start`` control action, and answer with a receipt. A refusal or an
  unreachable target refuses the Task call by name and releases the untouched
  branch.
* **Target** (:func:`handle_task_start`): re-check everything this worker owns
  (the asking device, the agent, its credentials and model, the depth, the
  checkout), cap the permission mode, start the child in its own checkout and
  record durably on the child's first turn which device and task its report is
  owed to (:data:`REPORT_DEVICE_KEY`, :func:`report_owed`).

Only identifiers travel: agent and provider ids, the model, Git identifiers
from ``worker_checkout`` and the parent's session ids. Paths, the parent's
cwd, worktree details, credentials and private-access grants never do.

When the child finishes, the target stores its report as owed and sends it
with ``task-report`` until the parent acknowledges it (:func:`sweep`, also at
boot). The parent accepts it once, only from the worker it sent the child to,
stores it with local Task reports and wakes the session that called Task
(:func:`handle_task_report`).

While it runs, the parent tracks the child like a local background Task
(:func:`track_dispatch`): a ``task_registry`` record carrying the worker
(TaskList, TaskGet, ListAgents, the running count, the park gate), a
supervisor record and a progress card fed from the child's mirrored session
events. The durable ``background_tasks`` row keeps ``remote_device_id``, so a
restart here neither kills the record nor clears the park; :func:`reattach`
rebuilds the rest. :func:`check_targets` asks each target with
``task-status`` (:func:`handle_task_status`) and records contact or since
when it has been unreachable (``app.db.remote_tasks``).

Stopping the child here (Stop, stop-all, Swarm cancel, deleting the parent)
reaches it on its worker with ``task-stop`` (:func:`stop_remote_child`,
answered by :func:`handle_task_stop` only for the worker that started it). The
stop is durable, so one that cannot be delivered now is sent again by
:func:`deliver_stops` when the target answers.

SendMessage works both ways with ``task-message`` (:func:`handle_task_message`):
the parent reaches the child (a finished one is resumed and reports again
under a new completion id) and the child reaches its ``main``. A stopped child
refuses a message on both workers, so it is never resumed.

No parent waits forever on a worker that went away (S12). When a target has
not answered ``task-status`` for :data:`OFFLINE_NOTICE_SECONDS`,
:func:`notice_offline` tells the caller once that it stopped waiting and that
the child may still be running there: the record settles as an error, its
approval prompts here are withdrawn, a Swarm item ends, and the caller wakes.
A report that still arrives later is accepted and wakes the caller once more
with the real outcome. Stop on the caller still reaches the child after the
notice (:func:`stop_noticed_children`): the stop is recorded durably and
delivered when its worker answers, and the child's report wakes nobody. A
target that restarted while a child ran reports it
failed at boot (the turn reap aborts it, :func:`sweep` sends the report), and
the child can be continued by message once its worker is back.

A remote child's permission prompts are shown in the parent conversation
(``remote_task_approvals``); its questions and private-access requests still
get Away handling there: deferred, never granted, and listed in its report.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional

from fluid.tools.hardening import defang_harness_markers

logger = logging.getLogger(__name__)

TASK_START_ACTION = "task-start"
#: The wire this release speaks; version 2 adds fields only.
WIRE_VERSION = 1
#: Receiving fetches a branch and provisions a checkout before it answers.
START_TIMEOUT = 90.0
#: A brief longer than this is refused, never cut.
MAX_PROMPT_CHARS = 50_000
MAX_LABEL_CHARS = 160
MAX_TOOLS = 200
#: Ends every parent-side refusal that provably started nothing.
NOT_DISPATCHED = "nothing was dispatched"
#: ``control.call``'s wording for a request that may have reached the peer.
MAY_HAVE_APPLIED = "may still have been applied"

#: Durable, on the child's first turn payload: the device the child owes its
#: report to. S6 reads it (with :data:`REPORT_KEY`) to deliver the report.
REPORT_DEVICE_KEY = "task_report_device"
#: Durable, beside it: which parent session and task the report belongs to.
REPORT_KEY = "task_report"
FINGERPRINT_KEY = "task_start_fingerprint"
CHECKOUT_MODE_KEY = "task_checkout_mode"
COMMAND_PREFIX = "task-start:"

_MODES = ("sync", "background")

_BODY_KEYS = frozenset({
    "version", "worker_session_id", "agent", "agent_name", "prompt", "label",
    "name", "provider_id", "model", "effort", "permission_mode",
    "tool_allowlist", "mode", "origin", "depth", "checkout", "parent",
    "context_window",
})
#: The ``remote_task`` capability that accepts ``context_window`` in the body.
#: An older target refuses unknown fields, so the parent sends it only to
#: targets at this version or later.
CONTEXT_WINDOW_VERSION = 2
#: Upper bound for a requested window; real choices are at most 1M.
MAX_CONTEXT_WINDOW = 10_000_000
_PARENT_KEYS = frozenset({
    "device_id", "session_id", "owner_session_id", "task_id", "tool_use_id",
})


# ---------------------------------------------------------------------------
# Shared
# ---------------------------------------------------------------------------

def report_owed(worker_session_id: Optional[str]) -> Optional[Dict[str, Any]]:
    """What a remote Task child owes, read from its first worker turn, or None.

    ``{"device_id", "parent_session_id", "owner_session_id", "task_id",
    "tool_use_id", "mode", "depth"}``. None for every local session. Never
    raises.
    """
    if not worker_session_id:
        return None
    try:
        from app.core.provider_agent import WORKER_TURN_SOURCE
        from app.db.base import get_db, parse_jsonb

        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT payload FROM turns WHERE session_id = %s AND source = %s "
                "ORDER BY created_at ASC LIMIT 1",
                (worker_session_id, WORKER_TURN_SOURCE),
            )
            row = cursor.fetchone()
        payload = parse_jsonb(row["payload"]) if row else None
    except Exception:  # noqa: BLE001 — unknown reads as a local session
        logger.debug("remote_task: report lookup failed for %s", worker_session_id, exc_info=True)
        return None
    if not isinstance(payload, dict) or not payload.get(REPORT_DEVICE_KEY):
        return None
    owed = payload.get(REPORT_KEY) if isinstance(payload.get(REPORT_KEY), dict) else {}
    return {"device_id": payload[REPORT_DEVICE_KEY], **owed}


def remote_child_in_chain(session_id: Optional[str], *, limit: int = 12) -> bool:
    """Whether ``session_id`` is, or works under, a remote Task child here.

    Such a chain belongs to another worker's dispatch, so it may run its own
    subagents only on this worker. Never raises.
    """
    try:
        from app.db import database

        current = session_id
        for _ in range(limit):
            if not current:
                return False
            if report_owed(current):
                return True
            row = database.get_session(current)
            if not row or not row.get("is_worker"):
                return False
            current = row.get("parent_session_id")
    except Exception:  # noqa: BLE001 — doubt answers "not remote"
        logger.debug("remote_task: chain check failed for %s", session_id, exc_info=True)
    return False


def remote_permission_mode(requested: Optional[str], agent_config: Optional[Mapping[str, Any]]) -> str:
    """The mode a remote child runs in on this worker: a ceiling, never a grant.

    The strictest of the mode the parent asked for (``default`` when it asked
    for none), the child agent's own configured mode (no limit when it has
    none) and this worker's ``remote_task_max_permission_mode``. Bypass is
    therefore reached only when the parent asked for it, the agent is not set
    lower and this worker allows it.
    """
    from app.core.config import settings
    from app.core.session_modes import strictest_permission_mode

    agent_mode = (agent_config or {}).get("permission_mode")
    if not isinstance(agent_mode, str) or not agent_mode.strip():
        agent_mode = None
    policy = getattr(settings, "remote_task_max_permission_mode", None) or "default"
    return strictest_permission_mode(requested or "default", agent_mode, policy)


def _fingerprint(body: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Parent side
# ---------------------------------------------------------------------------

def start_body(
    *,
    worker_session_id: str,
    agent: str,
    agent_name: str,
    prompt: str,
    label: str,
    roster_name: Optional[str],
    provider_id: Optional[str],
    model_id: Optional[str],
    effort: Optional[str],
    permission_mode: Optional[str],
    tool_allowlist: Optional[List[str]],
    mode: str,
    origin: Optional[str],
    depth: int,
    checkout: Mapping[str, Any],
    parent_device_id: str,
    parent_session_id: str,
    owner_session_id: str,
    tool_use_id: Optional[str],
    context_window: Optional[int] = None,
) -> Dict[str, Any]:
    """The ``task-start`` body. Identifiers only; never a path or credential.

    ``context_window`` (the caller's window choice) is sent only when set, so
    a request without one is byte-identical to the version-1 wire.
    """
    body = {
        "version": WIRE_VERSION,
        "worker_session_id": worker_session_id,
        "agent": agent,
        "agent_name": agent_name,
        "prompt": prompt,
        "label": label[:MAX_LABEL_CHARS],
        "name": roster_name,
        "provider_id": provider_id,
        "model": model_id,
        "effort": effort,
        "permission_mode": permission_mode,
        "tool_allowlist": list(tool_allowlist) if tool_allowlist else None,
        "mode": mode,
        "origin": origin,
        "depth": depth,
        "checkout": dict(checkout),
        "parent": {
            "device_id": parent_device_id,
            "session_id": parent_session_id,
            "owner_session_id": owner_session_id,
            "task_id": worker_session_id,
            "tool_use_id": tool_use_id,
        },
    }
    if context_window:
        body["context_window"] = int(context_window)
    return body


def _caller_bindings(caller_row: Mapping[str, Any], caller_session_id: str):
    """``(permission mode, loop-guard origin, context window)`` the child
    inherits. Blocking."""
    from app.core import loop_guard
    from app.core.provider_agent import _caller_context_window, _caller_permission_mode

    return (
        _caller_permission_mode(caller_row),
        loop_guard.session_origin(caller_session_id),
        _caller_context_window(caller_row),
    )


def _accepts_context_window(worker: Mapping[str, Any]) -> bool:
    """Whether the target's ``remote_task`` capability takes ``context_window``."""
    try:
        version = int((worker.get("capabilities") or {}).get("remote_task") or 0)
    except (TypeError, ValueError):
        return False
    return version >= CONTEXT_WINDOW_VERSION


def _link_job(caller_session_id: str, worker_id: str, device_id: str, label: str) -> None:
    """Count the child against the assistant's limits when the assistant
    (its home or one of its jobs) started it. Blocking; never raises."""
    try:
        from app.core import assistant_service

        assistant_service.link_remote_job(caller_session_id, worker_id, device_id, "task", label)
    except Exception:  # noqa: BLE001 — the child runs either way
        logger.warning("remote_task: could not link %s to the assistant's limits", worker_id, exc_info=True)


def _forget_job(worker_id: str) -> None:
    """Undo :func:`_link_job` for a child that provably never started. Blocking; never raises."""
    try:
        from app.core import assistant_service
        from app.db import assistant as assistant_store

        assistant_store.forget_remote_job(assistant_service.assistant_id(), worker_id)
    except Exception:  # noqa: BLE001
        logger.debug("remote_task: could not forget the link of %s", worker_id, exc_info=True)


async def dispatch_remote(
    *,
    worker: Mapping[str, Any],
    caller_row: Mapping[str, Any],
    caller_session_id: str,
    root_session_id: str,
    depth: int,
    profile_id: str,
    agent_name: str,
    result_agent: str,
    provider_id: Optional[str],
    model_id: Optional[str],
    effort: Optional[str],
    prompt: str,
    label: str,
    roster_name: Optional[str],
    mode: str,
    launch_tool_use_id: Optional[str] = None,
    tool_allowlist: Optional[List[str]] = None,
    stop_generation: Optional[int] = None,
) -> Dict[str, Any]:
    """Start a Task child on ``worker`` (a directory row) and return the receipt.

    Every failure before the target accepted is a refusal the model sees as
    the Task result; a branch this call pushed is released (compare-and-
    delete, so a branch carrying commits stays).

    A refusal that provably started nothing ends with :data:`NOT_DISPATCHED`
    (the assistant's default-worker fallback runs those here instead); one
    after which the child may exist there (a timeout once the request was
    sent, an invalid answer) never says it.
    """
    from app.core.task_registry import task_registry
    from app.core.worker_checkout import (
        REMOTE_BRANCH, WorkerCheckoutError, publish_worker_branch, release_worker_branch,
    )
    from app.network import control, engine

    name = str(worker.get("device_name") or worker["device_id"])
    if len(prompt) > MAX_PROMPT_CHARS:
        return {"ok": False, "error": (
            f"The prompt is {len(prompt)} characters; a subagent on another worker takes at most "
            f"{MAX_PROMPT_CHARS}. Shorten it, or omit worker to run it here; {NOT_DISPATCHED}.")}
    ctx = engine.context()
    if ctx is None:
        return {"ok": False, "error": (
            f"This worker is not part of a worker network; omit worker to run it here; {NOT_DISPATCHED}.")}
    from app.core import assistant_limits

    # The owner's daily spending limit, as for a local worker. Not a
    # NOT_DISPATCHED refusal: running it here instead is refused too.
    refusal = await asyncio.to_thread(assistant_limits.start_refusal, caller_session_id)
    if refusal:
        return {"ok": False, "error": refusal}

    worker_id = str(uuid.uuid4())
    permission_mode, origin, context_window = await asyncio.to_thread(
        _caller_bindings, caller_row, caller_session_id)
    if context_window and not _accepts_context_window(worker):
        # An older target would refuse the whole start over the unknown field;
        # its child runs at its model's default window instead.
        logger.info("remote_task: %s predates context_window; the child keeps its default window", name)
        context_window = None
    try:
        state = await asyncio.to_thread(
            publish_worker_branch, caller_row, worker_id, profile_id, tool_allowlist)
    except WorkerCheckoutError as exc:
        return {"ok": False, "error": f"{str(exc).rstrip('.')}; {NOT_DISPATCHED}."}
    if state.mode == REMOTE_BRANCH and not state.pushed:
        return {"ok": False, "error": (
            f"Could not publish the worker's branch to origin for {name}: "
            f"{str(state.reason or 'unknown reason').rstrip('.')}; {NOT_DISPATCHED}.")}

    async def release() -> None:
        try:
            result = await asyncio.to_thread(release_worker_branch, state)
            if not result.get("deleted") and state.pushed:
                logger.info("remote_task: kept %s on origin: %s", state.branch, result.get("reason"))
        except Exception:  # noqa: BLE001 — the refusal stands either way
            logger.warning("remote_task: could not release %s", state.branch, exc_info=True)

    body = start_body(
        worker_session_id=worker_id, agent=profile_id, agent_name=agent_name, prompt=prompt,
        label=label, roster_name=roster_name, provider_id=provider_id, model_id=model_id,
        effort=effort, permission_mode=permission_mode, tool_allowlist=tool_allowlist,
        mode=mode, origin=origin, depth=depth, checkout=state.to_wire(),
        parent_device_id=ctx.device_id, parent_session_id=root_session_id,
        owner_session_id=caller_session_id, tool_use_id=launch_tool_use_id,
        context_window=context_window,
    )
    if stop_generation is not None and task_registry.stop_invalidated(root_session_id, stop_generation):
        await release()
        return {"ok": False, "error": "The user stopped this Task dispatch.", "killed": True}

    # The record exists before the target can possibly report, so a fast
    # child's report is never refused as unknown. It is dropped again only
    # when the child provably never started.
    from app.db import remote_tasks

    await asyncio.to_thread(
        remote_tasks.record_dispatch, worker_session_id=worker_id,
        target_device_id=worker["device_id"], owner_session_id=caller_session_id,
        root_session_id=root_session_id, tool_use_id=launch_tool_use_id, mode=mode,
        label=label, agent=result_agent, target_name=name)
    # Its spend counts against the assistant's limits when the assistant started it.
    await asyncio.to_thread(_link_job, caller_session_id, worker_id, worker["device_id"], label)

    async def never_started() -> None:
        await release()
        await asyncio.to_thread(remote_tasks.forget_dispatch, worker_id)
        await asyncio.to_thread(_forget_job, worker_id)

    try:
        answer = await control.call(worker["device_id"], TASK_START_ACTION, body, timeout=START_TIMEOUT)
    except control.ControlRefused as exc:
        await never_started()
        reason = str(exc).rstrip(".")
        if "Unknown control action" in reason:
            reason = "it runs an older release; ask the owner to update it"
        return {"ok": False, "error": f"{name} refused the subagent: {reason}; {NOT_DISPATCHED}."}
    except control.RunnerUnreachable as exc:
        if MAY_HAVE_APPLIED in str(exc):
            # The request was sent: the child may be running there, and if it
            # is, its report is still accepted and wakes this session.
            await release()
            return {"ok": False, "error": (
                f"{name} did not confirm the subagent: {exc} Check that worker before "
                "dispatching it again; if it did start, its report still comes back here.")}
        await never_started()
        return {"ok": False, "error": f"{name} could not be reached: {str(exc).rstrip('.')}; {NOT_DISPATCHED}."}
    if not isinstance(answer, dict) or answer.get("worker_session_id") != worker_id:
        await release()
        return {"ok": False, "error": (
            f"{name} gave an invalid answer; the subagent may have started there. Check that "
            "worker before dispatching it again.")}
    await track_dispatch(
        worker_id=worker_id, target_device_id=worker["device_id"], worker_name=name,
        root_session_id=root_session_id, owner_session_id=caller_session_id, label=label,
        agent_name=agent_name, roster_name=roster_name, provider_id=provider_id,
        model_id=model_id, prompt=prompt, launch_tool_use_id=launch_tool_use_id)
    await _bind_swarm_item(worker_id, caller_session_id)
    return remote_receipt(answer, worker=worker, worker_id=worker_id, result_agent=result_agent,
                          model_id=model_id, label=label, mode=mode)


async def _bind_swarm_item(worker_id: str, caller_session_id: str) -> None:
    """Tie the child to the Swarm item that launched it, if any, so Swarm
    cancel can stop it and settle that item. Never raises."""
    try:
        from app.core.swarm import bind_worker

        await bind_worker(worker_id, caller_session_id)
    except Exception:  # noqa: BLE001 — the child runs either way
        logger.debug("remote_task: Swarm binding failed for %s", worker_id, exc_info=True)


def remote_receipt(answer: Mapping[str, Any], *, worker: Mapping[str, Any], worker_id: str,
                   result_agent: str, model_id: Optional[str], label: str, mode: str) -> Dict[str, Any]:
    """The Task result for a child another worker accepted."""
    name = str(worker.get("device_name") or worker["device_id"])
    checkout = answer.get("checkout") if isinstance(answer.get("checkout"), dict) else {}
    where = {
        "branch": f"in its own checkout of this project there, on {checkout.get('branch')}",
        "shared": "read-only in that worker's checkout of this project",
    }.get(checkout.get("mode"), "in that worker's workspace")
    permission_mode = answer.get("permission_mode")
    return {
        "ok": True,
        "mode": "remote",
        "requested_mode": mode,
        "agent": result_agent,
        "model": model_id,
        "task_id": worker_id,
        "worker_session_id": worker_id,
        "description": label,
        "worker": {"device_id": answer.get("owner_device_id") or worker["device_id"], "name": name},
        "permission_mode": permission_mode,
        "checkout": checkout,
        "note": (
            f"Worker started on {name}, {where}, with no private-access grants, at permission mode "
            f"{permission_mode}. "
            + ("It runs in the background even though sync was asked for: blocking on another "
               "worker is not supported yet. " if mode == "sync" else "")
            + "End the turn now unless you have genuinely disjoint work; you will be woken with "
            "its report when it finishes, even if that worker is briefly unreachable. "
            "TaskList/TaskGet show it with its worker. Approvals it needs are shown to the owner "
            "in this conversation; one nobody answers in time is denied and listed in its report."
        ),
    }


# ---------------------------------------------------------------------------
# Target side
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _Start:
    worker_session_id: str
    agent: str
    agent_name: str
    prompt: str
    label: str
    roster_name: Optional[str]
    provider_id: Optional[str]
    model: Optional[str]
    effort: Optional[str]
    permission_mode: Optional[str]
    tool_allowlist: Optional[List[str]]
    mode: str
    origin: Optional[str]
    depth: int
    checkout: Dict[str, Any]
    parent_session_id: str
    owner_session_id: str
    tool_use_id: Optional[str]
    fingerprint: str
    context_window: Optional[int] = None


def _text(body: Mapping[str, Any], key: str, limit: int) -> str:
    value = body.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{key} must be a non-empty string of at most {limit} characters")
    return value.strip()


def _optional_text(body: Mapping[str, Any], key: str, limit: int) -> Optional[str]:
    return None if body.get(key) is None else _text(body, key, limit)


def _require_worker_member(from_device: str):
    """The asking device's member row: an active worker of this account."""
    from app.network import engine, membership

    ctx = engine.context()
    member = ctx.member(from_device) if ctx else None
    if (ctx is None or from_device == ctx.device_id or member is None
            or member.kind != membership.KIND_WORKER or member.status != membership.STATUS_ACTIVE):
        raise PermissionError("Only active workers of this account may start subagents here.")
    return member


def _parse(body: Mapping[str, Any], from_device: str) -> _Start:
    """Validate the wire shape. Raises ``ValueError``; unknown keys are refused."""
    from app.core.model_effort import EFFORT_LEVELS
    from app.core.worker_checkout import RemoteWorkerBranch, WorkerCheckoutError
    from app.network import control

    if not isinstance(body, dict):
        raise ValueError("The subagent request must be an object")
    if body.get("version") != WIRE_VERSION:
        raise ValueError("Unsupported subagent request version; update this worker.")
    unknown = set(body) - _BODY_KEYS
    if unknown:
        raise ValueError(f"Unknown subagent request fields: {', '.join(sorted(unknown))}")
    worker_session_id = _text(body, "worker_session_id", 64)
    try:
        uuid.UUID(worker_session_id)
    except ValueError:
        raise ValueError("worker_session_id must be a UUID") from None
    prompt = body.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt must be a non-empty string")
    if len(prompt) > MAX_PROMPT_CHARS:
        raise ValueError(f"The prompt is longer than {MAX_PROMPT_CHARS} characters; nothing was started.")
    effort = _optional_text(body, "effort", 40)
    if effort is not None and effort not in EFFORT_LEVELS:
        raise ValueError(f"Unknown effort {effort!r}; nothing was started.")
    mode = body.get("mode")
    if mode not in _MODES:
        raise ValueError("mode must be sync or background")
    depth = body.get("depth")
    if type(depth) is not int or depth < 1:
        raise ValueError("depth must be a positive integer")
    context_window = body.get("context_window")
    if context_window is not None and (
            type(context_window) is not int or not 0 < context_window <= MAX_CONTEXT_WINDOW):
        raise ValueError("context_window must be a positive token count")
    tools = body.get("tool_allowlist")
    if tools is not None and (
            not isinstance(tools, list) or not tools or len(tools) > MAX_TOOLS
            or any(not isinstance(t, str) or not t.strip() or len(t) > 200 for t in tools)):
        raise ValueError("tool_allowlist must be a non-empty list of tool names")
    try:
        checkout = RemoteWorkerBranch.from_wire(body.get("checkout")).to_wire()
    except WorkerCheckoutError as exc:
        raise ValueError(str(exc)) from None
    parent = body.get("parent")
    if not isinstance(parent, dict) or set(parent) - _PARENT_KEYS:
        raise ValueError("parent must name the calling worker's session and task")
    if parent.get("device_id") != from_device:
        raise PermissionError("The subagent request names a different calling worker.")
    if parent.get("task_id") != worker_session_id:
        raise ValueError("parent.task_id must be the worker session id")
    return _Start(
        worker_session_id=worker_session_id,
        agent=_text(body, "agent", 200),
        agent_name=_text(body, "agent_name", 200),
        prompt=defang_harness_markers(prompt),
        label=defang_harness_markers(_text(body, "label", MAX_LABEL_CHARS)),
        roster_name=_optional_text(body, "name", 60),
        provider_id=_optional_text(body, "provider_id", 200),
        model=_optional_text(body, "model", 200),
        effort=effort,
        permission_mode=control._valid_permission_mode(body.get("permission_mode")),
        tool_allowlist=[t.strip() for t in tools] if tools else None,
        mode=mode,
        origin=_optional_text(body, "origin", 40),
        depth=depth,
        checkout=checkout,
        parent_session_id=_text(parent, "session_id", 200),
        owner_session_id=_text(parent, "owner_session_id", 200),
        tool_use_id=_optional_text(parent, "tool_use_id", 200),
        fingerprint=_fingerprint(body),
        context_window=context_window,
    )


def _answer(worker_session_id: str, turn: Mapping[str, Any], row: Mapping[str, Any], *,
            created: bool) -> Dict[str, Any]:
    from app.network import engine

    ctx = engine.context()
    payload = turn.get("payload") or {}
    mode = payload.get(CHECKOUT_MODE_KEY)
    return {
        "worker_session_id": worker_session_id,
        "turn_id": turn["id"],
        "created": created,
        "status": "started" if created else "already_started",
        "owner_device_id": ctx.device_id if ctx else None,
        "permission_mode": (turn.get("overrides") or {}).get("permission_mode") or row.get("permission_mode"),
        "checkout": {"mode": mode, "branch": row.get("branch") if mode == "branch" else None},
    }


def _existing(start: _Start, from_device: str) -> Optional[Dict[str, Any]]:
    """The answer for a start this worker already made, else None.

    A retry of the same request (a timeout on the parent) reads the same child
    back; anything else using the id is refused.
    """
    from app.core.provider_agent import WORKER_TURN_SOURCE
    from app.db import database, turns
    from app.db.base import get_db

    row = database.get_session(start.worker_session_id)
    if row is None:
        return None
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM turns WHERE session_id=%s AND command_id=%s AND source=%s",
                       (start.worker_session_id, COMMAND_PREFIX + start.worker_session_id,
                        WORKER_TURN_SOURCE))
        turn = turns._row(cursor.fetchone())
    payload = (turn or {}).get("payload") or {}
    if turn is None or payload.get(REPORT_DEVICE_KEY) != from_device:
        raise ValueError("A different session already uses this worker id; nothing was started.")
    if payload.get(FINGERPRINT_KEY) != start.fingerprint:
        raise ValueError("This worker id was already used with different arguments; nothing was started.")
    return _answer(start.worker_session_id, turn, row, created=False)


def _check_owner_chat(start: _Start, from_device: str) -> None:
    """Refuse a parent that is not an owner chat of the calling worker.

    The parent applies the full check (API key, principal, background
    observation); here the mirrored parent rows are checked when present.
    """
    from app.db import database

    for session_id in {start.owner_session_id, start.parent_session_id}:
        row = database.get_session(session_id)
        if row is None:
            continue
        if row.get("api_user_id"):
            raise PermissionError("Only the owner's chats can run a subagent on another worker.")
        owner = row.get("owner_device_id")
        if owner and owner != from_device:
            raise PermissionError("The calling session does not belong to the calling worker.")


def _check_model(start: _Start) -> None:
    """Refuse a provider, credential or model this worker cannot serve.

    No swap: a missing provider or model is a refusal, never another one.
    """
    from app.core import provider_agent
    from app.core.task_dispatch import _credential_gap, _native_task_model
    from app.db import database

    if start.provider_id:
        provider = database.get_model_provider(start.provider_id, include_token=False)
        if not provider:
            raise ValueError(f"This worker has no model provider {start.provider_id!r}; nothing was started.")
        gap = _credential_gap(start.provider_id)
        if gap:
            raise ValueError(f"{gap} (on this worker) Nothing was started.")
        if not start.model:
            raise ValueError("A provider-bound subagent needs an explicit model; nothing was started.")
        validation = provider_agent.validate_provider_model(
            start.provider_id, start.model, require_servable=False)
        if not validation.ok:
            raise ValueError(f"{validation.reason} (on this worker) Nothing was started.")
        return
    if start.model and _native_task_model(start.model) is None:
        raise ValueError(f"Model {start.model!r} is not a Claude model on this worker; nothing was started.")
    try:
        from app.core import claude_cli_status

        status = claude_cli_status.peek()
    except Exception:  # noqa: BLE001 — unknown sign-in is left to the runtime
        status = None
    if status is not None and status.signed_in is False:
        raise ValueError("This worker has no Claude login, so it cannot run a Claude subagent. "
                         "Sign in to Claude there, or pick a model another worker can run. "
                         "Nothing was started.")


def _narrowed_tools(start: _Start, config: Mapping[str, Any]) -> Optional[List[str]]:
    """The requested allowlist within the agent's own surface here (narrow only)."""
    from app.core.tool_access import resolve_tool_access, tool_permitted

    if not start.tool_allowlist:
        return None
    access = resolve_tool_access(dict(config))
    kept = [t for t in start.tool_allowlist if tool_permitted(t, access.allowed, access.disallowed)]
    if not kept:
        raise ValueError(f"None of the requested tools are available to agent {start.agent!r} on this "
                         "worker; nothing was started.")
    return kept


def prepare_task_start(body: Mapping[str, Any], from_device: str) -> Dict[str, Any]:
    """Validate, deduplicate, provision and record one remote child. Blocking.

    Returns the answer (``created`` False for a retry). Refusals raise
    ``ValueError`` / ``PermissionError``, which the control endpoint returns
    to the parent as a named refusal.
    """
    from app.core import provider_agent
    from app.core.config import settings
    from app.core.worker_checkout import (
        REMOTE_BRANCH, REMOTE_SHARED, WorkerCheckoutError, discard_unused_worker_checkout,
        receive_worker_branch,
    )
    from app.db import database, remote_tasks, turns
    from app.db.base import transaction

    _require_worker_member(from_device)
    start = _parse(body, from_device)
    existing = _existing(start, from_device)
    if existing is not None:
        return existing
    profile = database.get_profile(start.agent)
    if profile is None:
        raise ValueError(f"The agent {start.agent!r} does not exist on this worker; nothing was started.")
    max_depth = int(getattr(settings, "task_tool_max_depth", 5) or 5)
    if start.depth > max_depth:
        raise ValueError(f"Nesting limit reached ({max_depth} levels of agent-invokes-agent) on this "
                         "worker; nothing was started.")
    _check_owner_chat(start, from_device)
    _check_model(start)
    config = profile.get("config") or {}
    tools = _narrowed_tools(start, config)
    permission_mode = remote_permission_mode(start.permission_mode, config)

    try:
        received = receive_worker_branch(start.checkout, start.worker_session_id)
    except WorkerCheckoutError as exc:
        raise ValueError(str(exc)) from None
    checkout = received.checkout
    project_v2_id = start.checkout.get("project_v2_id") if received.mode in (REMOTE_BRANCH, REMOTE_SHARED) else None
    # Fitted to the child's model on THIS worker's provider rows.
    context_window = provider_agent.worker_context_window(
        start.context_window, start.provider_id, start.model)
    fields = provider_agent.worker_session_fields(
        worker_session_id=start.worker_session_id, profile_id=start.agent, title=start.label,
        parent_session_id=start.parent_session_id, provider_id=start.provider_id,
        model_id=start.model, effort=start.effort, project_v2_id=project_v2_id,
        worktree_branch=checkout.branch if checkout else None,
        worktree_path=checkout.path if checkout else None,
        permission_mode=permission_mode, context_window=context_window,
    )
    overrides = provider_agent.worker_turn_overrides(
        provider_id=start.provider_id, model_id=start.model, effort=start.effort,
        cwd=received.cwd, permission_mode=permission_mode, checkout=checkout,
        tool_allowlist=tools, context_window=context_window,
    )
    payload: Dict[str, Any] = {
        REPORT_DEVICE_KEY: from_device,
        REPORT_KEY: {
            "parent_session_id": start.parent_session_id,
            "owner_session_id": start.owner_session_id,
            "task_id": start.worker_session_id,
            "tool_use_id": start.tool_use_id,
            "mode": start.mode,
            "depth": start.depth,
        },
        FINGERPRINT_KEY: start.fingerprint,
        CHECKOUT_MODE_KEY: received.mode,
        # A chain begun by another worker never starts chats here.
        "orchestration_remote_origin": from_device,
    }
    if start.origin:
        from app.core import loop_guard

        payload[loop_guard.ORIGIN_KEY] = start.origin
    # Known as a worker before its first turn can start (depth walk,
    # AskUserQuestion withheld), as for a local worker.
    provider_agent.register_worker_session(start.worker_session_id)
    try:
        with transaction() as conn:
            database.create_session(**fields, _conn=conn)
            # Permission prompts travel to the parent (remote_task_approvals);
            # Away handling still defers what cannot (private-access requests).
            conn.cursor().execute(
                "UPDATE sessions SET away_mode=TRUE, branch=%s WHERE id=%s",
                (checkout.branch if checkout else None, start.worker_session_id))
            turn = turns.enqueue_turn(
                start.worker_session_id, provider_agent.WORKER_TURN_SOURCE, prompt=start.prompt,
                profile_id=start.agent, overrides=overrides, payload=payload,
                command_id=COMMAND_PREFIX + start.worker_session_id, _conn=conn)
            # The report it owes, durable from birth: a restart at any point
            # after this commit still ends in a report (``sweep``).
            remote_tasks.record_child(start.worker_session_id, from_device, conn=conn)
    except BaseException:
        if checkout is not None:
            discard_unused_worker_checkout(checkout)
        raise
    logger.info("remote_task: worker %s started child %s for %s", from_device,
                start.worker_session_id, start.owner_session_id)
    return _answer(start.worker_session_id, turn, database.get_session(start.worker_session_id) or {},
                   created=True)


_start_locks: Dict[str, List[Any]] = {}


@contextlib.asynccontextmanager
async def _start_lock(key: str):
    """One start per worker id at a time, so a retry waits for the first."""
    entry = _start_locks.setdefault(key, [asyncio.Lock(), 0])
    entry[1] += 1
    try:
        async with entry[0]:
            yield
    finally:
        entry[1] -= 1
        if entry[1] <= 0:
            _start_locks.pop(key, None)


async def handle_task_start(body: Dict[str, Any], from_device: str) -> Dict[str, Any]:
    """``task-start`` control action: start a Task child another worker asked for."""
    from app.core import turn_scheduler
    from app.db.async_database import run_sync

    key = str((body or {}).get("worker_session_id") or "")[:64]
    async with _start_lock(key):
        answer = await run_sync(prepare_task_start, body, from_device)
    if answer["created"]:
        turn_scheduler.kick(answer["worker_session_id"])
        try:
            from app.network import catalog

            await run_sync(catalog.publish_owned)
        except Exception:  # noqa: BLE001 — the child runs; the mirror catches up
            logger.exception("remote_task: child %s started, but publishing it failed",
                             answer["worker_session_id"])
    return answer


# ---------------------------------------------------------------------------
# Target side: the report a finished child owes its parent (task-report)
# ---------------------------------------------------------------------------

TASK_REPORT_ACTION = "task-report"
#: The parent fetches the child's branch before it answers (S13), within
#: ``checkout_transfer.RESULT_FETCH_TIMEOUT``.
REPORT_TIMEOUT = 60.0
REPORT_STATUSES = ("completed", "failed", "stopped")
#: Delivery retries back off to this ceiling and never give up on their own.
RETRY_DELAYS = (10, 30, 60, 120, 300, 600)
SWEEP_SECONDS = 30.0
MAX_NEEDS_OWNER = 10

_delivery_lock: Optional[tuple] = None
_delivery_tasks: set = set()


def _lock_for_loop() -> asyncio.Lock:
    """One delivery at a time per event loop (tests run several loops)."""
    global _delivery_lock
    loop = asyncio.get_running_loop()
    if _delivery_lock is None or _delivery_lock[0] is not loop:
        _delivery_lock = (loop, asyncio.Lock())
    return _delivery_lock[1]


def _settled_outcome(session_id: str) -> Optional[Dict[str, Any]]:
    """The child's last turn when its work is over, else None. Blocking.

    Over means nothing queued or running, the last turn completed, aborted or
    parked, and none of the child's own background work still running (#1812).

    * A turn ends ``completed`` while a Monitor it started still runs: the
      Monitor's completion wakes the child, so that turn's text is a parking
      remark, not the report.
    * A message steered into the running turn settles its own queue row
      ``completed`` at once; that row is not the child's last turn.
    * A park with no background work left is stranded: nothing will wake it,
      so what it has is the report.

    The owner's Stop on this worker ends the child's work too, so a
    user-stopped child is over as soon as its turn is.
    """
    from app.core.provider_agent import (
        _active_worker_background_work, _lost_monitor_wakes, _worker_still_working,
    )
    from app.core.task_registry import task_registry
    from app.db import turns

    latest = turns.get_latest_run_turn(session_id)
    if not latest:
        return None
    state = latest.get("state")
    if state not in ("parked", "completed", "aborted"):
        return None
    if turns.session_has_unsettled_turns(session_id):
        return None
    if not task_registry.is_user_stopped(session_id) and (
        _active_worker_background_work(session_id) or _worker_still_working(session_id)
    ):
        return None
    settled = {**latest, "_outcome": "completed" if state == "parked" else state}
    if state == "parked":
        # A finished Monitor whose wake never arrived: the park's text is a
        # parking remark, not a report (``build_child_report`` fails it).
        lost = _lost_monitor_wakes(session_id)
        if lost:
            settled["_lost_wakes"] = lost
    return settled


def build_child_report(session_id: str) -> Optional[Dict[str, Any]]:
    """The ``task-report`` body for a finished remote child, or None. Blocking.

    Status is completed, failed (an error, or interrupted by a restart) or
    stopped (stopped by the owner on this worker). The text is the child's
    final reply, bounded like a local worker report; approvals Away handling
    deferred are listed as the child met them.
    """
    from app.core.session_orchestration import _turn_report
    from app.db import database, remote_tasks
    from app.db.base import get_db

    if database.get_session(session_id) is None:
        return {"version": WIRE_VERSION, "worker_session_id": session_id,
                "completion_id": f"missing:{session_id}", "status": "failed",
                "report": "The subagent's session no longer exists on the worker that ran it.",
                "truncated": False, "needs_owner": []}
    settled = _settled_outcome(session_id)
    if settled is None:
        return None
    # Approvals held back since this run began: the newest message-resume,
    # else the original start, so a resumed run does not repeat old ones.
    origin_id = remote_tasks.latest_resume_turn(session_id)
    if origin_id is None:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id FROM turns WHERE session_id=%s AND command_id=%s",
                           (session_id, COMMAND_PREFIX + session_id))
            origin = cursor.fetchone()
        origin_id = origin["id"] if origin else settled["id"]
    status, report, needs = _turn_report(session_id, settled["id"], settled["_outcome"], origin_id)
    reason = None
    if status == "completed" and settled.get("_lost_wakes"):
        from app.core.provider_agent import bound_report, lost_wake_error

        status = "failed"
        text = lost_wake_error(session_id, settled["_lost_wakes"])
        if report and report != _NO_REPORT:
            text += "\n\nIts last message (not a report):\n" + report
        report = bound_report(text)[0]
    elif status == "failed" and settled.get("stop_reason") == LEASE_EXPIRED:
        report = _cut_off_report(report)
        reason = WORKER_RESTART_REASON
    elif status == "failed" and _shut_down(settled):
        report = _cut_off_report(report, shutdown=True)
        reason = WORKER_SHUTDOWN_REASON
    from app.core.provider_agent import MAX_REPORT_CHARS
    body = {
        "version": WIRE_VERSION,
        "worker_session_id": session_id,
        "completion_id": settled["id"],
        "status": status,
        "report": report,
        "truncated": len(report) >= MAX_REPORT_CHARS,
        "needs_owner": needs,
    }
    if reason:
        # Additive: an older parent ignores it and records a plain failure.
        body["reason"] = reason
    return body


#: ``turns.stop_reason`` of a turn the boot reap (or a lapsed run lease) aborted.
LEASE_EXPIRED = "lease_expired"
_NO_REPORT = "The chat ended without a written report."
#: ``reason`` on a report, and on the parent's record, when the worker that
#: ran the child shut down (docker stop, quitting the desktop app) or
#: restarted mid-run. Neither is a stop: a message resumes the child (#1808).
WORKER_SHUTDOWN_REASON = "worker_shutdown"
WORKER_RESTART_REASON = "worker_restarted"
_REPORT_REASONS = frozenset({WORKER_SHUTDOWN_REASON, WORKER_RESTART_REASON})


def _shut_down(turn: Mapping[str, Any]) -> bool:
    """Whether this worker's shutdown interrupted ``turn`` (``desktop_lifecycle``)."""
    from app.core.task_registry import SERVER_SHUTDOWN_REASON

    payload = turn.get("payload") if isinstance(turn.get("payload"), dict) else {}
    return payload.get("interrupt_source") == SERVER_SHUTDOWN_REASON


def _cut_off_report(partial: str, *, shutdown: bool = False) -> str:
    """The failed report of a child whose run this worker lost (a restart).

    ``shutdown`` is a graceful stop of this worker (docker stop, quitting the
    desktop app) rather than a crash. Its last streamed words are kept,
    marked as unfinished; they are not a report.
    """
    from app.core.provider_agent import bound_report

    if shutdown:
        text = ("The worker that ran this subagent shut down before the subagent finished, so its "
                "work was cut off. Nobody stopped it. It can be continued with SendMessage once "
                "that worker is back.")
    else:
        text = ("The worker that ran this subagent restarted (or lost its run) before the subagent "
                "finished, so its work was cut off. It can be continued with SendMessage now that "
                "the worker is back.")
    if partial and partial != _NO_REPORT:
        text += "\n\nWhat it had written before it was cut off (unfinished):\n" + partial
    return bound_report(text)[0]


def _child_stopped(session_id: str, child: Mapping[str, Any]) -> bool:
    """Whether the parent or the owner stopped this child. Blocking."""
    from app.core.task_registry import task_registry

    report = child.get("report") if isinstance(child.get("report"), dict) else {}
    return bool(child.get("stop_requested_at") or report.get("status") == "stopped"
                or task_registry.is_user_stopped(session_id))


def owe_if_finished(session_id: str) -> bool:
    """Store the report of a finished child. True when one is now owed.

    A child that ran again after its report was stored (a wake, a held
    message, the owner typing in its chat) owes the newer report instead:
    it replaces one not sent yet, or reopens the row under a new completion
    id, which the parent takes as a resumed run's report (#1812). A stopped
    child never reopens.
    """
    from app.db import remote_tasks

    child = remote_tasks.get_child(session_id)
    if not child:
        return False
    state = child["state"]
    if state != remote_tasks.RUNNING:
        if state not in (remote_tasks.OWED, remote_tasks.DELIVERED, remote_tasks.REFUSED):
            return False
        if _child_stopped(session_id, child):
            return state == remote_tasks.OWED
        settled = _settled_outcome(session_id)
        if settled is None or settled["id"] == child.get("completion_id"):
            return state == remote_tasks.OWED
    report = build_child_report(session_id)
    if report is None or (state != remote_tasks.RUNNING
                          and report["completion_id"] == child.get("completion_id")):
        return state == remote_tasks.OWED
    report["git"] = git_summary(session_id)
    if state == remote_tasks.RUNNING:
        remote_tasks.owe_report(session_id, report["completion_id"], report)
        return True
    if state == remote_tasks.OWED:
        return remote_tasks.replace_owed_report(session_id, child.get("completion_id"),
                                                report["completion_id"], report)
    logger.info("remote_task: child %s ran again after its report; a new one is owed", session_id)
    return remote_tasks.reowe_report(session_id, state, report["completion_id"], report)


def _retry_at(attempts: int) -> str:
    from datetime import datetime, timedelta, timezone

    delay = RETRY_DELAYS[min(attempts, len(RETRY_DELAYS) - 1)]
    return (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()


async def deliver_due() -> int:
    """Send every owed report that is due. Returns how many were acknowledged.

    A refusal (4xx) is final and kept as ``refused``; an unreachable parent,
    or this worker being off the network, is retried with backoff.
    """
    from app.db import remote_tasks
    from app.network import control

    delivered = 0
    async with _lock_for_loop():
        for row in await asyncio.to_thread(remote_tasks.due_reports):
            worker_id = row["worker_session_id"]
            # A newer report may replace this one while it is on the wire; only
            # the one sent is settled (#1812).
            sent = row.get("completion_id")
            try:
                answer = await control.call(row["parent_device_id"], TASK_REPORT_ACTION,
                                            row["report"], timeout=REPORT_TIMEOUT)
            except control.ControlRefused as exc:
                logger.warning("remote_task: parent refused the report of %s: %s", worker_id, exc)
                await asyncio.to_thread(remote_tasks.settle_report, worker_id, remote_tasks.REFUSED,
                                        error=str(exc)[:500], completion_id=sent)
                continue
            except Exception as exc:  # noqa: BLE001 — unreachable, off-network: try again later
                logger.info("remote_task: report of %s not delivered yet: %s", worker_id, exc)
                await asyncio.to_thread(remote_tasks.retry_report_later, worker_id,
                                        next_attempt_at=_retry_at(int(row.get("attempts") or 0)),
                                        error=str(exc) or type(exc).__name__)
                continue
            if isinstance(answer, dict) and answer.get("delivered") is True:
                await asyncio.to_thread(remote_tasks.settle_report, worker_id, remote_tasks.DELIVERED,
                                        completion_id=sent)
                delivered += 1
            else:
                await asyncio.to_thread(remote_tasks.retry_report_later, worker_id,
                                        next_attempt_at=_retry_at(int(row.get("attempts") or 0)),
                                        error="The parent gave an invalid answer.")
    return delivered


async def sweep() -> int:
    """Owe the report of every finished child, then deliver what is due.

    Run at boot and every :data:`SWEEP_SECONDS`, so a report survives a
    restart at any point after the child was created.
    """
    from app.db import remote_tasks

    def _reconcile() -> None:
        for worker_id in remote_tasks.running_children():
            try:
                owe_if_finished(worker_id)
            except Exception:  # noqa: BLE001 — one child never blocks the others
                logger.warning("remote_task: could not settle the report of %s", worker_id,
                               exc_info=True)

    await asyncio.to_thread(_reconcile)
    return await deliver_due()


async def on_child_settled(session_id: str) -> None:
    """Settlement hook: a remote child at rest owes its report now. Never raises.

    Only children this process registered are checked here; after a restart
    the periodic :func:`sweep` finds the rest.
    """
    try:
        from app.core.provider_agent import is_worker_session

        if not is_worker_session(session_id):
            return
        if not await asyncio.to_thread(owe_if_finished, session_id):
            return
        task = asyncio.create_task(deliver_due())
        _delivery_tasks.add(task)
        task.add_done_callback(_delivery_tasks.discard)
    except Exception:  # noqa: BLE001 — settlement must never break
        logger.warning("remote_task: settle hook failed for %s", session_id, exc_info=True)


async def delivery_loop() -> None:
    """Boot-time and periodic :func:`sweep`. Runs until cancelled."""
    await asyncio.sleep(5.0)
    while True:
        try:
            await sweep()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — try again next round
            logger.warning("remote_task: report sweep failed", exc_info=True)
        await asyncio.sleep(SWEEP_SECONDS)


# ---------------------------------------------------------------------------
# Commits back: the child's branch, pushed there and fetched here
# ---------------------------------------------------------------------------

def _checkout_mode(session_id: str) -> Optional[str]:
    """How the child's checkout was set up (:data:`CHECKOUT_MODE_KEY`). Blocking."""
    from app.db.base import get_db, parse_jsonb

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT payload FROM turns WHERE session_id=%s AND command_id=%s",
                       (session_id, COMMAND_PREFIX + session_id))
        row = cursor.fetchone()
    payload = parse_jsonb(row["payload"]) if row else None
    mode = payload.get(CHECKOUT_MODE_KEY) if isinstance(payload, dict) else None
    return mode if isinstance(mode, str) else None


def git_summary(session_id: str) -> Dict[str, Any]:
    """Target side: push the finished child's commits; the report's git block.

    Runs once, when the report is first owed. Never raises: a failure here
    becomes the block's reason, and the report goes out either way.
    """
    from app.core.worker_checkout import REMOTE_NONE, bound_git_block, empty_git_block, push_worker_commits

    mode = None
    try:
        mode = _checkout_mode(session_id)
        block = push_worker_commits(session_id, mode=mode)
    except Exception as exc:  # noqa: BLE001 — the report must still go
        from app.core.scrub import redact_url_credentials

        logger.warning("remote_task: could not check the branch of %s: %s", session_id,
                       redact_url_credentials(str(exc))[:300])
        block = empty_git_block(mode or REMOTE_NONE, (
            "This worker could not check the subagent's branch; anything it committed stays in its "
            "checkout there."))
    return bound_git_block(block, session_id) or empty_git_block(REMOTE_NONE)


async def bring_commits_here(owner_row: Mapping[str, Any], worker_session_id: str, worker_name: str,
                             raw: Any) -> str:
    """Parent side: fetch the child's pushed branch; the wake's git paragraph.

    ``raw`` is the report's git block; an older worker sends none and this
    returns "". A failed fetch never fails the delivery: the paragraph says
    so. Never raises.
    """
    from app.core.scrub import redact_url_credentials
    from app.core.worker_checkout import bound_git_block, fetch_worker_commits, git_note

    block = bound_git_block(raw, worker_session_id)
    if block is None:
        return ""
    try:
        outcome = await asyncio.to_thread(fetch_worker_commits, owner_row, worker_session_id, block)
    except Exception as exc:  # noqa: BLE001 — the report is delivered either way
        logger.warning("remote_task: could not fetch the branch of %s: %s", worker_session_id,
                       redact_url_credentials(str(exc))[:300])
        outcome = {"fetched": False, "released": False,
                   "reason": "Fetching the branch on this worker failed unexpectedly."}
    if outcome.get("reason"):
        logger.info("remote_task: %s not fetched here: %s", worker_session_id,
                    redact_url_credentials(str(outcome["reason"])))
    return "\n\n" + git_note(block, outcome, worker_name)


# ---------------------------------------------------------------------------
# Parent side: accept a report and wake the session that called Task
# ---------------------------------------------------------------------------

_report_locks: Dict[str, List[Any]] = {}

_WAKE_STATUS = {"completed": "completed", "failed": "error", "stopped": "killed"}


@dataclass(frozen=True)
class _Report:
    worker_session_id: str
    completion_id: str
    status: str
    report: str
    truncated: bool
    needs_owner: List[str]
    dispatch: Dict[str, Any]
    worker_name: str
    #: Why a failed run ended, when its worker shut down or restarted.
    reason: Optional[str] = None


def accept_task_report(body: Mapping[str, Any], from_device: str) -> _Report:
    """Validate a report: only from the worker the task went to, only for a
    task this worker dispatched. Raises ``PermissionError`` / ``ValueError``."""
    from app.core.provider_agent import bound_report
    from app.db import remote_tasks

    member = _require_worker_member(from_device)
    if not isinstance(body, dict) or body.get("version") != WIRE_VERSION:
        raise ValueError("Unsupported report version")
    worker_id = _text(body, "worker_session_id", 64)
    dispatch = remote_tasks.get_dispatch(worker_id)
    if not dispatch or dispatch["target_device_id"] != from_device:
        raise PermissionError("No subagent with this id was sent to this worker.")
    completion_id = _text(body, "completion_id", 200)
    status = body.get("status")
    if status not in REPORT_STATUSES:
        raise ValueError("Invalid report status")
    text = body.get("report") if isinstance(body.get("report"), str) else ""
    report, cut = bound_report(defang_harness_markers(text) or "The subagent ended without a written report.")
    needs = body.get("needs_owner") or []
    if not isinstance(needs, list):
        raise ValueError("Invalid needs_owner")
    needs = [defang_harness_markers(str(item)[:300]) for item in needs[:MAX_NEEDS_OWNER] if str(item).strip()]
    reason = body.get("reason") if body.get("reason") in _REPORT_REASONS and status == "failed" else None
    return _Report(worker_id, completion_id, status, report, bool(cut or body.get("truncated") is True),
                   needs, dispatch, member.name or from_device, reason)


def _wake_summary(report: _Report, report_path: Optional[str]) -> str:
    from app.core.task_dispatch import SYNC_REPORT_HEAD_CHARS

    head = report.report[:SYNC_REPORT_HEAD_CHARS]
    parts = [f"Ran on {report.worker_name}. {head}"]
    if report_path and len(report.report) > SYNC_REPORT_HEAD_CHARS:
        parts.append(f"[This is the first {SYNC_REPORT_HEAD_CHARS} of {len(report.report)} characters; "
                     f"the full report is at {report_path}. Read more of it only if this does not "
                     "answer your question.]")
    elif report_path:
        parts.append(f"[The same report is at {report_path}.]")
    elif len(report.report) > SYNC_REPORT_HEAD_CHARS:
        parts.append(report.report[SYNC_REPORT_HEAD_CHARS:])
    if report.truncated:
        parts.append("[The subagent's reply was longer and was cut at the report limit.]")
    if report.needs_owner:
        parts.append("Approvals it needed and did not get (held back for the owner): "
                     + "; ".join(report.needs_owner))
    return "\n\n".join(parts)


async def handle_task_report(body: Dict[str, Any], from_device: str) -> Dict[str, Any]:
    """``task-report`` control action: store the report, wake the caller once.

    The tracked record settles first (registry rows, card, supervisor), as a
    local background worker's do before its wake. A record this worker already
    settled (the user stopped it here) keeps its status, and the report is
    stored without waking anyone.

    A child a message resumed (``task-message``) reports again under a new
    completion id. That report reopens the record first when it beat the
    message's answer, and wakes the caller once more; a retry of any report
    already delivered is a duplicate.
    """
    from app.core import turn_scheduler
    from app.core.task_dispatch import write_task_report
    from app.db import database, remote_tasks
    from app.db.async_database import run_sync

    report = await run_sync(accept_task_report, body, from_device)
    key = report.worker_session_id
    async with _tracking_lock(key):
        current = await run_sync(remote_tasks.get_dispatch, key)
        past = await run_sync(remote_tasks.past_completions, key)
        known = current.get("completion_id") if current else None
        if report.completion_id == known or report.completion_id in past:
            return {"delivered": True, "duplicate": True}
        if known:
            # A resumed run's report that arrived before the message's answer
            # reopened the record (lock already held here).
            await _reopen_locked(current, known)
            past = [*past, known]
        owner = report.dispatch["owner_session_id"]
        owner_row = await run_sync(database.get_session, owner)
        path = None
        # Stopped here: durable, so this holds even if the record was lost.
        stopped_here = bool(current and current.get("stop_requested_at"))
        # The caller was already told this worker went quiet (S12): the
        # report is still taken, and wakes it once more with the outcome.
        noticed = bool(current and current.get("offline_notice_at")) and not stopped_here
        if owner_row is not None:
            git_text = await bring_commits_here(owner_row, key, report.worker_name, body.get("git"))
            path = await run_sync(write_task_report, owner_row, key, report.report)
            if (await _settle_tracked(report.dispatch, report, path, noticed=noticed,
                                      reason=report.reason)
                    and not stopped_here):
                summary = _wake_summary(report, path) + git_text
                if noticed:
                    summary = (f"{report.worker_name} answered again after the notice that it was "
                               "unreachable, and this is the subagent's report.\n\n" + summary)
                frame = {
                    "task_id": key,
                    "status": _WAKE_STATUS[report.status],
                    "tool_use_id": report.dispatch.get("tool_use_id") or key,
                    "summary": summary,
                }
                if past or noticed:
                    # A resumed run's report, or one after the offline
                    # notice: its own wake, past the one-wake-per-task latch
                    # the earlier report or notice set.
                    frame["agent_message_turn_id"] = report.completion_id
                await turn_scheduler.on_remote_task_terminal(owner, frame)
            else:
                logger.info("remote_task: report for %s stored; it was already settled here",
                            key)
        else:
            logger.info("remote_task: report for %s arrived after its caller %s was deleted",
                        key, owner)
        await run_sync(remote_tasks.mark_delivered, key, completion_id=report.completion_id,
                       status=report.status, report_path=path)
        _finish_swarm_item(key, _SWARM_STATUS[report.status], report.report)
        from app.core import remote_task_approvals

        await run_sync(remote_task_approvals.drop_proxies, report.dispatch["root_session_id"], key)
        return {"delivered": True, "duplicate": False}


#: A report's status as the Swarm item that launched the child records it.
_SWARM_STATUS = {"completed": "completed", "failed": "failed", "stopped": "cancelled"}


def _finish_swarm_item(worker_id: str, status: str, result: str) -> None:
    """End the Swarm item that launched this child, if any. Never raises.

    The Swarm waits on the item's launch; a child on another worker has no
    local job to end it, so its report (or the offline notice) does. Ending
    an item twice keeps the first outcome.
    """
    try:
        from app.core.swarm import workers as swarm_workers

        launch = swarm_workers.get(worker_id)
        if launch is not None:
            launch.finish(status, result)
    except Exception:  # noqa: BLE001 — the report is delivered either way
        logger.debug("remote_task: could not end the Swarm item of %s", worker_id, exc_info=True)


@contextlib.asynccontextmanager
async def _tracking_lock(key: str):
    """One tracking change per remote child at a time: its start, re-attach and report."""
    entry = _report_locks.setdefault(key, [asyncio.Lock(), 0])
    entry[1] += 1
    try:
        async with entry[0]:
            yield
    finally:
        entry[1] -= 1
        if entry[1] <= 0:
            _report_locks.pop(key, None)


# ---------------------------------------------------------------------------
# Parent side: track the child like a local background Task
# ---------------------------------------------------------------------------

TASK_STATUS_ACTION = "task-status"
STATUS_TIMEOUT = 15.0
#: How often the parent asks each target about the children it runs.
TRACK_SECONDS = 60.0
MAX_STATUS_IDS = 200
#: What a target says about a child: its ``remote_task_children`` state, or
#: ``unknown`` when it has no such child for the asking worker.
CHILD_UNKNOWN = "unknown"
#: How long a target may go without answering before the caller is told it
#: stopped waiting (:func:`notice_offline`).
OFFLINE_NOTICE_SECONDS = 30 * 60
#: ``reason`` on a record the offline notice settled.
OFFLINE_REASON = "worker_unreachable"

#: When this process first checked its targets. Silence before it (this
#: worker was down, not the target) never counts towards the notice.
_tracking_started: Optional[Any] = None


def worker_display_name(device_id: Optional[str]) -> str:
    """The worker's name as this account knows it, else its device id. Never raises."""
    if not device_id:
        return ""
    try:
        from app.network import engine

        ctx = engine.context()
        member = ctx.member(device_id) if ctx else None
        if member is not None and member.name:
            return str(member.name)
    except Exception:  # noqa: BLE001 — a name is cosmetic
        logger.debug("remote_task: name lookup failed for %s", device_id, exc_info=True)
    return str(device_id)


def _model_label(provider_id: Optional[str], model_id: Optional[str]) -> str:
    """The registry's "<provider> · <model>" label, as for a local worker. Blocking."""
    from app.core.provider_agent import _provider_display_name

    return f"{_provider_display_name(provider_id)} · {model_id or 'inherit'}"


def _register(*, worker_id: str, root_session_id: str, owner_session_id: str, label: str,
              agent: Optional[str], roster_name: Optional[str], model_label: Optional[str],
              target_device_id: str, worker_name: str) -> None:
    """The registry records a local background dispatch would have.

    One on the root (what the UI renders and the park gate reads) and, for a
    nested caller, one on the caller; the launch key admits the completion to
    the caller's own wake lane. The task id, launch key and card join key are
    all the worker session id, as for a local worker.
    """
    from app.core.task_registry import task_registry

    for session_id in dict.fromkeys((root_session_id, owner_session_id)):
        task_registry.start_task(
            session_id, worker_id, "agent", label, summary=label, model=model_label,
            tool_use_id=worker_id, agent=agent, name=roster_name,
            remote_device_id=target_device_id, worker_name=worker_name)
    task_registry.mark_background_launch(owner_session_id, worker_id)


async def _follow(*, worker_id: str, target_device_id: str, root_session_id: str, label: str,
                  provider_id: Optional[str], model_id: Optional[str], agent_name: Optional[str],
                  prompt: Optional[str], launch_tool_use_id: Optional[str],
                  resume_cursor: Optional[int]) -> None:
    """Supervisor record plus a progress card fed from the child's mirrored events.

    ``resume_cursor`` None starts a new card; a number re-attaches the card a
    previous process started, past that event (see ``WorkerFeed.resume_at``).
    """
    from app.core import provider_agent_supervisor as supervisor
    from app.core.provider_agent_feed import WorkerFeed

    if supervisor.get_worker(root_session_id, worker_id) is not None:
        return
    feed = WorkerFeed(
        parent_session_id=root_session_id, worker_session_id=worker_id, task_id=worker_id,
        label=label, provider_id=provider_id, model_id=model_id, agent_name=agent_name,
        prompt=prompt, launch_tool_use_id=launch_tool_use_id)
    # Always background: the turn-end teardown spares it, as it spares a local
    # background worker; Stop and stop-all reach it through the supervisor.
    supervisor.register_worker(supervisor.WorkerRecord(
        parent_session_id=root_session_id, worker_session_id=worker_id, task_id=worker_id,
        tool_use_id=worker_id, provider_id=provider_id or "", model_id=model_id or "",
        label=label, feed=feed, background=True, remote_device_id=target_device_id))
    if resume_cursor is None:
        await feed.started()
    else:
        feed.resume_at(resume_cursor)
    # The child's events reach this worker by catalog sync; the feed reads
    # them from the local mirror exactly as it reads a local worker's log.
    feed.start_following()


async def track_dispatch(*, worker_id: str, target_device_id: str, worker_name: str,
                         root_session_id: str, owner_session_id: str, label: str,
                         agent_name: Optional[str], roster_name: Optional[str],
                         provider_id: Optional[str], model_id: Optional[str],
                         prompt: Optional[str], launch_tool_use_id: Optional[str]) -> None:
    """Make a child another worker accepted show up like a local background Task.

    Never raises: the child is running either way, and its report still wakes
    the caller. A report that already came back (a very fast child) wins.
    """
    from app.db import remote_tasks

    try:
        model_label = await asyncio.to_thread(_model_label, provider_id, model_id)
        async with _tracking_lock(worker_id):
            dispatch = await asyncio.to_thread(remote_tasks.get_dispatch, worker_id)
            if dispatch is None or dispatch.get("completion_id"):
                return
            _register(worker_id=worker_id, root_session_id=root_session_id,
                      owner_session_id=owner_session_id, label=label, agent=agent_name,
                      roster_name=roster_name, model_label=model_label,
                      target_device_id=target_device_id, worker_name=worker_name)
            await _follow(worker_id=worker_id, target_device_id=target_device_id,
                          root_session_id=root_session_id, label=label, provider_id=provider_id,
                          model_id=model_id, agent_name=agent_name, prompt=prompt,
                          launch_tool_use_id=launch_tool_use_id, resume_cursor=None)
    except Exception:  # noqa: BLE001 — tracking is display; the report still arrives
        logger.exception("remote_task: could not track child %s on %s", worker_id, target_device_id)


def _child_cursor(worker_session_id: str) -> int:
    """The newest mirrored event of a child here (0 when none). Blocking."""
    from app.db import session_events

    try:
        return int(session_events.get_max_seq(worker_session_id) or 0)
    except Exception:  # noqa: BLE001 — start from the beginning of the mirror
        return 0


async def reattach(dispatch: Mapping[str, Any]) -> bool:
    """Rebuild one undelivered child's tracking after this worker restarted.

    The durable row comes back running (``ensure_hydrated``); this adds the
    caller's launch key, the supervisor record and the card's feed, resumed
    at the newest mirrored event. A record this worker already settled, a
    deleted caller or a child already attached is left alone. True when it
    attached.
    """
    from app.core import provider_agent_supervisor as supervisor
    from app.core.task_registry import task_registry
    from app.db import database, remote_tasks

    worker_id = dispatch["worker_session_id"]
    root = dispatch["root_session_id"]
    owner = dispatch["owner_session_id"]
    async with _tracking_lock(worker_id):
        if supervisor.get_worker(root, worker_id) is not None:
            return False
        current = await asyncio.to_thread(remote_tasks.get_dispatch, worker_id)
        if not current or current.get("completion_id"):
            return False
        if await asyncio.to_thread(database.get_session, owner) is None:
            return False
        for session_id in dict.fromkeys((root, owner)):
            await task_registry.ensure_hydrated(session_id)
        record = task_registry.get_task(root, worker_id)
        if record is not None and record.get("status") != "running":
            return False
        target = current["target_device_id"]
        name = worker_display_name(target)
        _register(worker_id=worker_id, root_session_id=root, owner_session_id=owner,
                  label=current.get("label") or "Task worker",
                  agent=(record or {}).get("agent") or current.get("agent"),
                  roster_name=(record or {}).get("name"), model_label=(record or {}).get("model"),
                  target_device_id=target, worker_name=name)
        cursor = await asyncio.to_thread(_child_cursor, worker_id)
        # Provider and model as the child's mirrored session row records them.
        child = await asyncio.to_thread(database.get_session, worker_id) or {}
        await _follow(worker_id=worker_id, target_device_id=target, root_session_id=root,
                      label=current.get("label") or "Task worker", provider_id=child.get("provider_id"),
                      model_id=child.get("model"),
                      agent_name=(record or {}).get("agent") or current.get("agent"), prompt=None,
                      launch_tool_use_id=current.get("tool_use_id"), resume_cursor=cursor)
        return True


async def reattach_all() -> int:
    """:func:`reattach` every child still owed a report here. Returns how many attached."""
    from app.db import remote_tasks

    attached = 0
    for dispatch in await asyncio.to_thread(remote_tasks.tracked_dispatches):
        try:
            attached += bool(await reattach(dispatch))
        except Exception:  # noqa: BLE001 — one child never blocks the others
            logger.warning("remote_task: could not re-attach %s", dispatch.get("worker_session_id"),
                           exc_info=True)
    return attached


async def _settle_tracked(dispatch: Mapping[str, Any], report: "_Report",
                          report_path: Optional[str], *, noticed: bool = False,
                          reason: Optional[str] = None) -> bool:
    """Settle the child's registry records, card and supervisor record.

    Returns False when this worker had already settled the record (a Stop
    here, or a watchdog): its status stands and the report must not wake
    anyone. A record that is missing (the report beat its tracking) is created
    first, so TaskList still lists the finished child.

    ``noticed`` is a report arriving after the offline notice settled the
    record as an error: the real outcome replaces it (a record stopped here
    since keeps its status). ``reason`` is recorded on the terminal record.
    """
    from app.core import provider_agent_supervisor as supervisor
    from app.core.provider_agent import _completion_summary
    from app.core.provider_agent_feed import WorkerFeed
    from app.core.task_registry import USER_STOP_REASON, task_registry

    worker_id = dispatch["worker_session_id"]
    root = dispatch["root_session_id"]
    owner = dispatch["owner_session_id"]
    for session_id in dict.fromkeys((root, owner)):
        await task_registry.ensure_hydrated(session_id)
    record = task_registry.get_task(root, worker_id)
    tracked = supervisor.get_worker(root, worker_id)
    reopened = noticed and record is not None and record.get("status") == "error"
    if record is not None and record.get("status") != "running" and not reopened:
        if tracked is not None:
            supervisor.unregister_worker(root, worker_id)
        return False
    if record is None:
        _register(worker_id=worker_id, root_session_id=root, owner_session_id=owner,
                  label=dispatch.get("label") or "Task worker", agent=dispatch.get("agent"),
                  roster_name=None, model_label=None,
                  target_device_id=dispatch["target_device_id"], worker_name=report.worker_name)
        record = task_registry.get_task(root, worker_id) or {}
    status = _WAKE_STATUS[report.status]
    if tracked is not None and tracked.feed is not None:
        feed = tracked.feed
        await feed.stop_following(final_drain=True)
    else:
        # No live feed (the report beat its tracking): settle the card only.
        feed = WorkerFeed(parent_session_id=root, worker_session_id=worker_id, task_id=worker_id,
                          label=dispatch.get("label"))
    await feed.completed(status, result_preview=report.report,
                         summary=record.get("model") or dispatch.get("label"))
    summary = _completion_summary(report.report)
    for session_id in dict.fromkeys((root, owner)):
        task_registry.complete_task(
            session_id, worker_id, status, summary=summary,
            reason=USER_STOP_REASON if report.status == "stopped" else reason,
            report_path=report_path, report_chars=len(report.report))
    supervisor.unregister_worker(root, worker_id)
    return True


async def _ask_status(target_device_id: str, ids: List[str]) -> Optional[Dict[str, Optional[str]]]:
    """``{id: state}`` from the target, or None when it could not be reached.

    A refusal is an answer: the target is reachable (an older release refuses
    the action by name), so its children keep their last known state.
    """
    from app.network import control

    try:
        answer = await control.call(target_device_id, TASK_STATUS_ACTION,
                                    {"version": WIRE_VERSION, "worker_session_ids": ids},
                                    timeout=STATUS_TIMEOUT)
    except control.ControlRefused:
        return {worker_id: None for worker_id in ids}
    except Exception as exc:  # noqa: BLE001 — unreachable, off-network: quiet
        logger.info("remote_task: %s did not answer task-status: %s", target_device_id, exc)
        return None
    children = answer.get("children") if isinstance(answer, dict) else None
    states: Dict[str, Optional[str]] = {}
    for worker_id in ids:
        entry = children.get(worker_id) if isinstance(children, dict) else None
        state = entry.get("state") if isinstance(entry, dict) else None
        states[worker_id] = state if isinstance(state, str) and len(state) <= 40 else None
    return states


def _mirrored_states(worker_ids: List[str]) -> Dict[str, Optional[str]]:
    """Each child's runtime state from the local mirror of its session. Blocking."""
    try:
        from app.network import session_runtime

        rows = session_runtime.for_sessions(worker_ids)
    except Exception:  # noqa: BLE001 — the state is display only
        logger.debug("remote_task: mirrored runtime read failed", exc_info=True)
        return {}
    return {worker_id: (rows.get(worker_id) or {}).get("state") for worker_id in worker_ids}


def _quiet_seconds(since: Optional[str], now: Any) -> float:
    """How long a target has been quiet as this process saw it (0 when unknown)."""
    from datetime import datetime, timezone

    try:
        moment = datetime.fromisoformat(str(since))
    except ValueError:
        return 0.0
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    floor = _tracking_started or now
    return (now - max(moment, floor)).total_seconds()


async def check_targets(now: Optional[Any] = None) -> int:
    """Ask each target about the children it runs for this worker.

    A target that answers clears ``unreachable_since`` and records each
    child's state as it reports it; one that cannot be reached keeps the first
    time it went quiet. The registry records show the worker's state and that
    time; their status stays ``running`` until the report arrives, or until
    the target has been quiet for :data:`OFFLINE_NOTICE_SECONDS`
    (:func:`notice_offline`). ``now`` (an aware datetime) is for tests.
    Returns how many children are unreachable.
    """
    from datetime import datetime, timezone

    from app.core.task_registry import task_registry
    from app.db import remote_tasks

    global _tracking_started
    now = now or datetime.now(timezone.utc)
    if _tracking_started is None:
        _tracking_started = now
    rows = await asyncio.to_thread(remote_tasks.tracked_dispatches)
    by_target: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        by_target.setdefault(row["target_device_id"], []).append(row)
    quiet = 0
    for target, group in by_target.items():
        for start in range(0, len(group), MAX_STATUS_IDS):
            chunk = group[start:start + MAX_STATUS_IDS]
            ids = [row["worker_session_id"] for row in chunk]
            states = await _ask_status(target, ids)
            if states is None:
                since = await asyncio.to_thread(remote_tasks.note_unreachable, ids)
                quiet += len(since)
            else:
                await asyncio.to_thread(remote_tasks.note_contact, states)
                since = {}
            mirrored = await asyncio.to_thread(_mirrored_states, ids)
            for row in chunk:
                worker_id = row["worker_session_id"]
                for session_id in dict.fromkeys((row["root_session_id"], row["owner_session_id"])):
                    task_registry.update_remote_status(
                        session_id, worker_id, worker_state=mirrored.get(worker_id),
                        unreachable_since=since.get(worker_id))
                if (worker_id in since and not row.get("offline_notice_at")
                        and _quiet_seconds(since[worker_id], now) >= OFFLINE_NOTICE_SECONDS):
                    try:
                        await notice_offline(row, since[worker_id])
                    except Exception:  # noqa: BLE001 — one child never blocks the others
                        logger.warning("remote_task: offline notice for %s failed", worker_id,
                                       exc_info=True)
    return quiet


def _offline_text(worker_name: str, since: str) -> str:
    """The caller's notice that it stopped waiting for a quiet worker."""
    from datetime import datetime

    try:
        when = datetime.fromisoformat(since).strftime("%Y-%m-%d %H:%M UTC")
    except ValueError:
        when = since
    minutes = OFFLINE_NOTICE_SECONDS // 60
    return (
        f"{worker_name} has not answered for {minutes} minutes (since {when}), so this stopped "
        "waiting for the subagent. It may still be running there.\n\n"
        f"If {worker_name} comes back and the subagent finishes, its report still arrives here as "
        f"its own notification. Until then a message to it cannot be delivered, and TaskGet shows "
        f"when {worker_name} answers again. Carry on without its result, or redo that work here "
        "if you need it now.")


async def notice_offline(dispatch: Mapping[str, Any], since: str) -> bool:
    """Tell the caller once that a child's worker has been quiet too long.

    The caller stops waiting: the record settles as an error (reason
    :data:`OFFLINE_REASON`), the card ends, open approval prompts for the
    child are withdrawn here, a Swarm item that launched it ends as failed,
    and the caller wakes with the notice. Nothing is sent to the target, which
    may still be running the child: its report is still accepted later and
    wakes the caller again (``handle_task_report``), a message to it is
    refused with a plain reason while the worker stays away, and its spend is
    still collected when it is back (``assistant_limits.refresh_remote``).
    True when this call sent the notice.
    """
    from app.core import remote_task_approvals, turn_scheduler
    from app.db import database, remote_tasks

    worker_id = dispatch["worker_session_id"]
    async with _tracking_lock(worker_id):
        claimed = await asyncio.to_thread(remote_tasks.claim_offline_notice, worker_id)
        if claimed is None:
            return False
        root, owner = claimed["root_session_id"], claimed["owner_session_id"]
        name = worker_display_name(claimed["target_device_id"])
        text = _offline_text(name, since)
        logger.warning("remote_task: %s quiet since %s; told %s to stop waiting for %s",
                       name, since, owner, worker_id)
        await asyncio.to_thread(remote_task_approvals.drop_proxies, root, worker_id)
        notice = _Report(worker_id, "", "failed", text, False, [], dict(claimed), name)
        settled = await _settle_tracked(claimed, notice, None, reason=OFFLINE_REASON)
        _finish_swarm_item(worker_id, "failed", text)
        if not settled or await asyncio.to_thread(database.get_session, owner) is None:
            return True
        frame = {"task_id": worker_id, "status": _WAKE_STATUS["failed"],
                 "tool_use_id": claimed.get("tool_use_id") or worker_id, "summary": text}
        if await asyncio.to_thread(remote_tasks.past_completions, worker_id):
            # A resumed run: the first report already used the task's latch.
            frame["agent_message_turn_id"] = f"offline:{since}"
        await turn_scheduler.on_remote_task_terminal(owner, frame)
        return True


async def tracking_loop() -> None:
    """Re-attach children still running elsewhere after a boot, then check
    their targets every :data:`TRACK_SECONDS`. Runs until cancelled."""
    await asyncio.sleep(5.0)
    reattached = False
    while True:
        try:
            if not reattached:
                await reattach_all()
                from app.core import remote_task_approvals

                await remote_task_approvals.restore_proxies()
                reattached = True
            await deliver_stops()
            await check_targets()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — try again next round
            logger.warning("remote_task: tracking pass failed", exc_info=True)
        await asyncio.sleep(TRACK_SECONDS)


# ---------------------------------------------------------------------------
# Target side: answer task-status for the children a worker started here
# ---------------------------------------------------------------------------

def answer_task_status(body: Mapping[str, Any], from_device: str) -> Dict[str, Any]:
    """Each asked child's state, only for children ``from_device`` started. Blocking.

    Raises ``PermissionError`` / ``ValueError`` (a named refusal).
    """
    from app.db import remote_tasks

    _require_worker_member(from_device)
    if not isinstance(body, dict) or body.get("version") != WIRE_VERSION:
        raise ValueError("Unsupported status request version")
    ids = body.get("worker_session_ids")
    if (not isinstance(ids, list) or not ids or len(ids) > MAX_STATUS_IDS
            or any(not isinstance(i, str) or not i.strip() or len(i) > 64 for i in ids)):
        raise ValueError(f"worker_session_ids must list 1 to {MAX_STATUS_IDS} ids")
    ids = list(dict.fromkeys(ids))
    rows = remote_tasks.children_for(ids, from_device)
    return {"children": {
        worker_id: {"state": rows[worker_id]["state"] if worker_id in rows else CHILD_UNKNOWN}
        for worker_id in ids
    }}


async def handle_task_status(body: Dict[str, Any], from_device: str) -> Dict[str, Any]:
    """``task-status`` control action."""
    from app.db.async_database import run_sync

    return await run_sync(answer_task_status, body, from_device)


# ---------------------------------------------------------------------------
# Stop: the parent asks the target to stop a child (task-stop)
# ---------------------------------------------------------------------------

TASK_STOP_ACTION = "task-stop"
STOP_TIMEOUT = 20.0
#: How long a stop path waits for the target's answer. A slower answer is
#: left to the send already under way and, failing that, to the retry from
#: :func:`tracking_loop`, so a Stop here never hangs on the network.
STOP_WAIT = 2.0
MAX_STOP_IDS = MAX_STATUS_IDS
#: What a target answers per child: stopped now, or already at rest. A child
#: it does not know for the asking worker is :data:`CHILD_UNKNOWN`.
CHILD_STOPPED = "stopped"
CHILD_FINISHED = "finished"
_STOP_ANSWERS = (CHILD_STOPPED, CHILD_FINISHED, CHILD_UNKNOWN)
#: Stop reasons a target is told (for its log); anything else travels as ``stopped``.
_STOP_REASONS = frozenset({
    "user_stop", "user_stopped", "user_stopped_all", "agent_stopped", "swarm_cancelled", "session_closed",
    "parent_turn_ended", "provider_rate_limited", "host_cleanup",
    "assistant_daily_limit", "assistant_job_time",
})

_stop_sends: set = set()


def _unreached_detail(name: str) -> str:
    return (f"{name} could not be reached, so the subagent may still be running there. "
            "The stop is sent again when it answers.")


async def _send_stops(target_device_id: str, worker_ids: List[str],
                      reason: str) -> Dict[str, Dict[str, Any]]:
    """Send one ``task-stop`` and record the answer. Never raises.

    Returns ``{id: {"outcome", "detail"?}}``. The target's answer settles each
    stop (``sent``); a refusal is final (``refused``), with a plain message for
    an older release that does not know the action; anything else leaves the
    stops pending for :func:`deliver_stops`.
    """
    from app.db import remote_tasks
    from app.network import control

    name = worker_display_name(target_device_id)
    body = {"version": WIRE_VERSION, "worker_session_ids": worker_ids,
            "reason": reason if reason in _STOP_REASONS else "stopped"}
    try:
        answer = await control.call(target_device_id, TASK_STOP_ACTION, body, timeout=STOP_TIMEOUT)
    except control.ControlRefused as exc:
        why = str(exc).rstrip(".")
        if "Unknown control action" in why:
            detail = (f"{name} runs an older release that cannot stop a subagent from another "
                      f"worker, so it keeps running there until it finishes. Update {name}, or "
                      "stop the subagent in its chat there.")
        else:
            detail = f"{name} refused to stop the subagent: {why}."
        logger.warning("remote_task: stop of %s refused: %s", ", ".join(worker_ids), detail)
        with contextlib.suppress(Exception):
            await asyncio.to_thread(remote_tasks.settle_stop, worker_ids, remote_tasks.STOP_REFUSED,
                                    error=detail)
        return {i: {"outcome": "remote_stop_refused", "detail": detail} for i in worker_ids}
    except Exception as exc:  # noqa: BLE001 — unreachable, off-network: sent again later
        logger.info("remote_task: stop of %s not delivered to %s yet: %s",
                    ", ".join(worker_ids), target_device_id, exc)
        with contextlib.suppress(Exception):
            await asyncio.to_thread(remote_tasks.note_stop_failed, worker_ids,
                                    str(exc) or type(exc).__name__)
        return {i: {"outcome": "remote_stop_pending", "detail": _unreached_detail(name)}
                for i in worker_ids}
    children = answer.get("children") if isinstance(answer, dict) else None
    results: Dict[str, Dict[str, Any]] = {}
    answered: List[str] = []
    for worker_id in worker_ids:
        entry = children.get(worker_id) if isinstance(children, dict) else None
        state = entry.get("state") if isinstance(entry, dict) else None
        if state in _STOP_ANSWERS:
            answered.append(worker_id)
            results[worker_id] = {"outcome": f"remote_{state}"}
        else:
            results[worker_id] = {"outcome": "remote_stop_pending", "detail": _unreached_detail(name)}
    if answered:
        with contextlib.suppress(Exception):
            await asyncio.to_thread(remote_tasks.settle_stop, answered, remote_tasks.STOP_SENT)
    return results


async def stop_remote_child(worker_id: str, target_device_id: str, *, reason: str,
                            summary: Optional[str] = None) -> Dict[str, Any]:
    """Stop a Task child on the worker that runs it. Never raises.

    The stop is recorded durably first, so a target that cannot be reached
    now is asked again when it answers (:func:`deliver_stops`). A nested
    caller's own record settles here too, as a local worker's does when it
    ends. Returns ``{"outcome", "detail"?}``: ``remote_stopped``,
    ``remote_finished``, ``remote_unknown``, ``remote_stop_refused`` (the
    detail says why) or ``remote_stop_pending`` (not answered yet; it will be
    asked again).
    """
    from app.core.task_registry import task_registry
    from app.db import remote_tasks

    try:
        row = await asyncio.to_thread(remote_tasks.request_stop, worker_id, reason)
    except Exception:  # noqa: BLE001 — still try to reach it once
        logger.warning("remote_task: could not record the stop of %s", worker_id, exc_info=True)
        row = None
    if row is not None:
        # Every stop path comes here (Swarm cancel may have no supervisor
        # record): its open approval prompts on this worker are withdrawn, and
        # their tool calls on the target are denied.
        from app.core import remote_task_approvals

        await asyncio.to_thread(remote_task_approvals.drop_proxies, row["root_session_id"], worker_id)
    owner = (row or {}).get("owner_session_id")
    if owner and owner != (row or {}).get("root_session_id"):
        with contextlib.suppress(Exception):
            current = task_registry.get_task(owner, worker_id)
            if current is not None and current.get("status") == "running":
                task_registry.complete_task(owner, worker_id, "killed", summary=summary,
                                            reason=reason, notify=False)
    send = asyncio.create_task(_send_stops(target_device_id, [worker_id], reason))
    _stop_sends.add(send)
    send.add_done_callback(_stop_sends.discard)
    pending = {"outcome": "remote_stop_pending",
               "detail": _unreached_detail(worker_display_name(target_device_id))}
    if row is not None and row.get("unreachable_since"):
        # Known to be away: do not hold the stop here for an answer.
        return pending
    try:
        results = await asyncio.wait_for(asyncio.shield(send), STOP_WAIT)
    except Exception:  # noqa: BLE001 — too slow: the send goes on in the background
        return pending
    return dict(results.get(worker_id) or pending)


async def stop_noticed_children(session_id: str, *, reason: str) -> List[Dict[str, Any]]:
    """Stop this session's children whose worker went quiet past the notice. Never raises.

    The offline notice (:func:`notice_offline`) settled their records and
    dropped their supervisor records, so Stop's usual walk no longer finds
    them; they may still be running on their worker. Each goes through
    :func:`stop_remote_child`, so the stop is durable and delivered when the
    worker answers again, and the child's later report wakes nobody. Returns
    one result per child, as the Stop path reports a task.
    """
    from app.db import remote_tasks

    try:
        rows = await asyncio.to_thread(remote_tasks.noticed_unstopped, session_id)
    except Exception:  # noqa: BLE001 — never fail a Stop
        logger.warning("remote_task: could not read noticed children of %s", session_id,
                       exc_info=True)
        return []
    results: List[Dict[str, Any]] = []
    for row in rows:
        worker_id = row["worker_session_id"]
        try:
            stopped = await stop_remote_child(worker_id, row["target_device_id"], reason=reason)
        except Exception:  # noqa: BLE001 — the next child still gets its stop
            logger.warning("remote_task: could not stop noticed child %s", worker_id, exc_info=True)
            continue
        detail = stopped.get("detail")
        results.append({"ok": True, "kind": "agent", "task_id": worker_id,
                         **({"detail": detail} if detail else {})})
    return results


async def drain_stops() -> None:
    """Wait for the stops being sent right now (tests, shutdown)."""
    while _stop_sends:
        await asyncio.gather(*list(_stop_sends), return_exceptions=True)


async def deliver_stops() -> int:
    """Send every stop a target has not answered yet. Returns how many were answered.

    Run by :func:`tracking_loop` every :data:`TRACK_SECONDS`, so a child does
    not keep running after its parent stopped it just because its worker was
    away at that moment. Never raises.
    """
    from app.db import remote_tasks

    try:
        rows = await asyncio.to_thread(remote_tasks.pending_stops)
    except Exception:  # noqa: BLE001 — try again next round
        logger.warning("remote_task: could not read pending stops", exc_info=True)
        return 0
    groups: Dict[tuple, List[str]] = {}
    for row in rows:
        key = (row["target_device_id"], row.get("stop_reason") or "stopped")
        groups.setdefault(key, []).append(row["worker_session_id"])
    answered = 0
    for (target, reason), ids in groups.items():
        for start in range(0, len(ids), MAX_STOP_IDS):
            results = await _send_stops(target, ids[start:start + MAX_STOP_IDS], reason)
            answered += sum(1 for r in results.values() if r["outcome"] != "remote_stop_pending")
    return answered


async def stop_child_here(worker_session_id: str) -> None:
    """Stop a child another worker started here, as Stop on its chat would.

    The durable interrupt reaches its turn, then the user-stop sweep latches
    the session, aborts queued turns and ends its own background work and
    subagents. Its report then says ``stopped``. Never raises.
    """
    from app.core import turn_manager
    from app.core.query_session_lifecycle import stop_all_background_tasks

    try:
        await turn_manager.interrupt_async(worker_session_id, "stop", stop_background=True)
    except Exception:  # noqa: BLE001 — the sweep below still runs
        logger.warning("remote_task: could not interrupt child %s", worker_session_id, exc_info=True)
    try:
        await stop_all_background_tasks(worker_session_id, user_stop=True)
    except Exception:  # noqa: BLE001
        logger.warning("remote_task: could not stop the work of child %s", worker_session_id,
                       exc_info=True)


def _stop_targets(body: Mapping[str, Any], from_device: str) -> Dict[str, Optional[Dict[str, Any]]]:
    """Validate a ``task-stop`` and read the asked children. Blocking.

    Raises ``PermissionError`` when the asker is not an active worker of this
    account, or when any asked child was started here by another worker (the
    whole request is refused and nothing is stopped). Returns ``{id: row}``,
    None for a child this worker does not know.
    """
    from app.db import remote_tasks

    _require_worker_member(from_device)
    if not isinstance(body, dict) or body.get("version") != WIRE_VERSION:
        raise ValueError("Unsupported stop request version")
    ids = body.get("worker_session_ids")
    if (not isinstance(ids, list) or not ids or len(ids) > MAX_STOP_IDS
            or any(not isinstance(i, str) or not i.strip() or len(i) > 64 for i in ids)):
        raise ValueError(f"worker_session_ids must list 1 to {MAX_STOP_IDS} ids")
    ids = list(dict.fromkeys(ids))
    rows = remote_tasks.children_by_id(ids)
    if any(row["parent_device_id"] != from_device for row in rows.values()):
        raise PermissionError("Only the worker that started a subagent here may stop it.")
    for worker_id in rows:
        remote_tasks.mark_child_stopped(worker_id)
    return {worker_id: rows.get(worker_id) for worker_id in ids}


async def handle_task_stop(body: Dict[str, Any], from_device: str) -> Dict[str, Any]:
    """``task-stop`` control action: stop children the asking worker started here.

    Idempotent: a running child is stopped (again, harmlessly), one already at
    rest answers ``finished`` and keeps its report, and an unknown one answers
    ``unknown``. A finished child is latched stopped too, so a later message
    cannot resume it.
    """
    from app.core.task_registry import task_registry
    from app.db import remote_tasks
    from app.db.async_database import run_sync

    rows = await run_sync(_stop_targets, body, from_device)
    children: Dict[str, Dict[str, str]] = {}
    for worker_id, row in rows.items():
        if row is None:
            children[worker_id] = {"state": CHILD_UNKNOWN}
        # A child at rest whose report the sweep has not stored yet: store it
        # now, so it keeps the report it finished with and answers finished.
        elif row["state"] == remote_tasks.RUNNING and not await run_sync(owe_if_finished, worker_id):
            logger.info("remote_task: worker %s stopped child %s (%s)", from_device, worker_id,
                        body.get("reason"))
            await stop_child_here(worker_id)
            children[worker_id] = {"state": CHILD_STOPPED}
        else:
            task_registry.mark_user_stopped(worker_id)
            children[worker_id] = {"state": CHILD_FINISHED}
    return {"children": children}


# ---------------------------------------------------------------------------
# Messages both ways (task-message): resume a finished child, reach `main`
# ---------------------------------------------------------------------------

TASK_MESSAGE_ACTION = "task-message"
#: Resuming may restore the child's reclaimed checkout before it answers.
MESSAGE_TIMEOUT = 30.0
MAX_MESSAGE_CHARS = MAX_PROMPT_CHARS
#: ``to`` on the wire: the child on the worker that runs it, or ``main``,
#: the conversation that started it, on the worker that dispatched it.
TO_CHILD = "child"
TO_MAIN = "main"
#: The answer for a finished child the message started again. The other
#: answers are ``agent_messaging``'s delivery statuses.
RESUMED = "resumed"

_MESSAGE_KEYS = frozenset({
    "version", "to", "worker_session_id", "message_id", "sender_session_id", "text",
    "urgent", "known_completion_id",
})
_RESUMABLE = {"completed": "completed", "failed": "failed"}

_message_locks: Dict[str, List[Any]] = {}
_parents: Dict[tuple, Optional[Dict[str, Any]]] = {}
_PARENT_CACHE = 2048


def remote_parent(session_id: Optional[str]) -> Optional[Dict[str, Any]]:
    """Where a Task child another worker started here reaches its ``main``.

    ``{"device_id", "owner_session_id", "parent_session_id", "worker_name"}``,
    or None for every session that is not such a child. Fixed for a session's
    life, so it is remembered. Never raises.
    """
    if not session_id:
        return None
    try:
        from app.network import engine

        ctx = engine.context()
        if ctx is None:
            return None
        key = (ctx.device_id, session_id)
        if key not in _parents:
            owed = report_owed(session_id)
            parent = None
            if owed and owed.get("owner_session_id"):
                parent = {"device_id": owed["device_id"], "owner_session_id": owed["owner_session_id"],
                          "parent_session_id": owed.get("parent_session_id")}
            while len(_parents) >= _PARENT_CACHE:
                _parents.pop(next(iter(_parents)))
            _parents[key] = parent
        parent = _parents[key]
    except Exception:  # noqa: BLE001 — doubt answers "a local session"
        logger.debug("remote_task: parent lookup failed for %s", session_id, exc_info=True)
        return None
    if parent is None:
        return None
    return {**parent, "worker_name": worker_display_name(parent["device_id"])}


@contextlib.asynccontextmanager
async def _message_lock(key: str):
    """One application of a message id at a time, so a retry waits for the first."""
    entry = _message_locks.setdefault(key, [asyncio.Lock(), 0])
    entry[1] += 1
    try:
        async with entry[0]:
            yield
    finally:
        entry[1] -= 1
        if entry[1] <= 0:
            _message_locks.pop(key, None)


async def _send_message(device_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
    """``task-message`` with one retry of the same message id after a timeout.

    The receiver applies a message id once, so the retry is safe; it turns
    "may have arrived" into a definite answer whenever the link recovers.
    """
    from app.network import control

    try:
        return await control.call(device_id, TASK_MESSAGE_ACTION, body, timeout=MESSAGE_TIMEOUT)
    except control.RunnerUnreachable as exc:
        if MAY_HAVE_APPLIED not in str(exc):
            raise
        logger.info("remote_task: message %s unconfirmed, retrying once: %s", body["message_id"], exc)
    return await control.call(device_id, TASK_MESSAGE_ACTION, body, timeout=MESSAGE_TIMEOUT)


def _message_refusal(exc: Exception, *, who: str, where: str) -> str:
    """The sender's text for a message that did not arrive."""
    from app.network import control

    reason = str(exc).rstrip(".")
    if isinstance(exc, control.ControlRefused):
        if "Unknown control action" in reason:
            return (f"Could not deliver to {who}: {where} runs an older release that cannot take "
                    "messages for subagents; ask the owner to update it. The message was not delivered.")
        return f"Could not deliver to {who}: {where} refused it: {reason}. The message was not delivered."
    if MAY_HAVE_APPLIED in reason:
        return (f"Could not confirm delivery to {who}: {reason}. It may have arrived; do not assume "
                "it did, and do not send it again until you have checked.")
    return (f"Could not deliver to {who}: {where} cannot be reached right now ({reason}). The message "
            "was not delivered; send it again when that worker is back.")


def _offline_message_refusal(who: str, where: str, dispatch: Mapping[str, Any]) -> str:
    """A message to a child whose worker is still away after the offline notice."""
    from datetime import datetime

    since = dispatch.get("unreachable_since")
    try:
        when = f" since {datetime.fromisoformat(since).strftime('%Y-%m-%d %H:%M UTC')}" if since else ""
    except ValueError:
        when = ""
    return (f"Could not deliver to {who}: {where} has not answered{when}, and you were already told "
            "this conversation stopped waiting for it. The message was not delivered. The subagent "
            "may still be running there; if it finishes, its report still comes back here. Send the "
            f"message again when {where} is back (TaskGet shows when it answers).")


def _invalid_answer(who: str, where: str) -> Dict[str, Any]:
    return {"text": (f"Could not confirm delivery to {who}: {where} gave an invalid answer. It may "
                     "have arrived; do not send it again until you have checked."), "is_error": True}


async def send_to_child(sender_session_id: str, row: Mapping[str, Any], text: str, *,
                        urgent: bool = False) -> Dict[str, Any]:
    """SendMessage from this worker to a Task child it sent to another worker.

    Same meaning as for a local child: a running child takes it at its next
    tool result, a parked one when it next wakes (``urgent`` wakes it), and a
    finished one is resumed over its own transcript, its new report coming
    back through ``task-report``. A stopped child refuses. A child whose
    worker the caller was told is away (:func:`notice_offline`) is still
    tried, since the worker may be back; while it stays away the answer says
    so in plain words. Returns ``{"text", "is_error"}`` for the tool result;
    "queued" is never reported as delivered.
    """
    from app.core import agent_messaging
    from app.core.task_registry import task_registry
    from app.db import remote_tasks

    worker_id = row["worker_session_id"]
    where = str(row.get("worker") or "the other worker")
    who = f"{row['name']} on {where}"
    dispatch = await asyncio.to_thread(remote_tasks.get_dispatch, worker_id)
    if dispatch is None:
        return {"text": f"Could not deliver to {who}: this worker has no record of sending it there.",
                "is_error": True}
    # A stop asked for here is durable (S8): refuse even before the target
    # answered it, so a message never resumes what this worker stopped.
    if (task_registry.is_user_stopped(worker_id) or row.get("status") == "killed"
            or dispatch.get("stop_requested_at")):
        return {"text": (f"Could not deliver to {who}: it was stopped, and a message cannot restart it. "
                         "Ask the owner to continue it in its chat."), "is_error": True}
    if len(text) > MAX_MESSAGE_CHARS:
        return {"text": (f"Could not deliver to {who}: the message is {len(text)} characters; another "
                         f"worker takes at most {MAX_MESSAGE_CHARS}."), "is_error": True}
    from app.core import assistant_limits

    # The assistant's daily spending limit (S11): that worker cannot see it.
    refusal = await asyncio.to_thread(assistant_limits.message_refusal, worker_id)
    if refusal:
        return {"text": f"Could not deliver to {who}: {refusal}", "is_error": True}
    known = dispatch.get("completion_id")
    body = {
        "version": WIRE_VERSION, "to": TO_CHILD, "worker_session_id": worker_id,
        "message_id": str(uuid.uuid4()), "sender_session_id": sender_session_id,
        "text": text, "urgent": bool(urgent), "known_completion_id": known,
    }
    try:
        answer = await _send_message(dispatch["target_device_id"], body)
    except Exception as exc:  # noqa: BLE001 — refused or unreachable: say which
        from app.network import control

        if (dispatch.get("offline_notice_at") and isinstance(exc, control.RunnerUnreachable)
                and MAY_HAVE_APPLIED not in str(exc)):
            return {"text": _offline_message_refusal(who, where, dispatch), "is_error": True}
        return {"text": _message_refusal(exc, who=who, where=where), "is_error": True}
    status = answer.get("status") if isinstance(answer, dict) else None
    if status == RESUMED:
        if known:
            await reopen(dispatch, known)
        return {"text": (f"Resume queued for {who} (turn {answer.get('turn_id')}). You will be woken "
                         "with its new report when it finishes."), "is_error": False}
    if status not in (agent_messaging.DELIVERED, agent_messaging.QUEUED, agent_messaging.HELD):
        return _invalid_answer(who, where)
    return {"text": agent_messaging.delivery_receipt(who, status)["text"], "is_error": False}


async def send_to_parent(sender_session_id: str, row: Mapping[str, Any], text: str, *,
                         urgent: bool = False) -> Dict[str, Any]:
    """SendMessage ``main`` from a Task child that another worker started here.

    The message reaches the conversation that started it on that worker as a
    peer message from this child, with local semantics there. Returns
    ``{"text", "is_error"}``.
    """
    from app.core import agent_messaging

    parent = remote_parent(sender_session_id)
    if parent is None:
        return {"text": "Could not deliver to main: this session has no parent on another worker.",
                "is_error": True}
    where = parent["worker_name"]
    who = f"main on {where}"
    if len(text) > MAX_MESSAGE_CHARS:
        return {"text": (f"Could not deliver to {who}: the message is {len(text)} characters; another "
                         f"worker takes at most {MAX_MESSAGE_CHARS}."), "is_error": True}
    body = {
        "version": WIRE_VERSION, "to": TO_MAIN, "worker_session_id": sender_session_id,
        "message_id": str(uuid.uuid4()), "sender_session_id": sender_session_id,
        "text": text, "urgent": bool(urgent), "known_completion_id": None,
    }
    try:
        answer = await _send_message(parent["device_id"], body)
    except Exception as exc:  # noqa: BLE001 — refused or unreachable: say which
        return {"text": _message_refusal(exc, who=who, where=where), "is_error": True}
    status = answer.get("status") if isinstance(answer, dict) else None
    if status not in (agent_messaging.DELIVERED, agent_messaging.QUEUED, agent_messaging.HELD):
        return _invalid_answer(who, where)
    return {"text": agent_messaging.delivery_receipt(who, status)["text"], "is_error": False}


async def reopen(dispatch: Mapping[str, Any], completion_id: str) -> bool:
    """A message resumed a child whose report ``completion_id`` came back.

    Tracked again like a running background Task (registry records, launch
    key, supervisor, card back to running) until its next report. False when
    that report is no longer the current one (already reopened, or the next
    report beat the message's answer).
    """
    async with _tracking_lock(dispatch["worker_session_id"]):
        return await _reopen_locked(dispatch, completion_id)


async def _reopen_locked(dispatch: Mapping[str, Any], completion_id: str) -> bool:
    from app.core.task_registry import task_registry
    from app.core.turn_recorder import record_task_start_frame
    from app.db import remote_tasks

    worker_id = dispatch["worker_session_id"]
    root = dispatch["root_session_id"]
    owner = dispatch["owner_session_id"]
    if not await asyncio.to_thread(remote_tasks.reopen_dispatch, worker_id, completion_id):
        return False
    from app.core import assistant_limits

    # Running again: its spend is collected again even if it had aged out.
    await asyncio.to_thread(assistant_limits.wake_remote_job, worker_id)
    for session_id in dict.fromkeys((root, owner)):
        await task_registry.ensure_hydrated(session_id)
    record = task_registry.get_task(root, worker_id, durable=True)
    if record is not None and record.get("status") == "killed":
        # Stopped here: it stays stopped; a report it still sends wakes nobody.
        return True
    target = dispatch["target_device_id"]
    name = worker_display_name(target)
    label = dispatch.get("label") or "Task worker"
    for session_id in dict.fromkeys((root, owner)):
        task_registry.resume_task(session_id, worker_id)
        task_registry.start_task(session_id, worker_id, "agent", label, remote_device_id=target,
                                 worker_name=name)
    task_registry.mark_background_launch(owner, worker_id)
    cursor = await asyncio.to_thread(_child_cursor, worker_id)
    await _follow(worker_id=worker_id, target_device_id=target, root_session_id=root, label=label,
                  provider_id=None, model_id=None,
                  agent_name=(record or {}).get("agent") or dispatch.get("agent"), prompt=None,
                  launch_tool_use_id=dispatch.get("tool_use_id"), resume_cursor=cursor)
    # The card's chip goes back to running, as for a local continuation.
    await record_task_start_frame(root, worker_id, description=label, summary=label,
                                  tool_use_id=worker_id)
    return True


def _parse_message(body: Mapping[str, Any]) -> Dict[str, Any]:
    if not isinstance(body, dict) or body.get("version") != WIRE_VERSION:
        raise ValueError("Unsupported message version; update this worker.")
    unknown = set(body) - _MESSAGE_KEYS
    if unknown:
        raise ValueError(f"Unknown message fields: {', '.join(sorted(unknown))}")
    if body.get("to") not in (TO_CHILD, TO_MAIN):
        raise ValueError("to must be child or main")
    text = body.get("text")
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_MESSAGE_CHARS:
        raise ValueError(f"text must be a non-empty string of at most {MAX_MESSAGE_CHARS} characters")
    if not isinstance(body.get("urgent", False), bool):
        raise ValueError("urgent must be true or false")
    known = body.get("known_completion_id")
    if known is not None and (not isinstance(known, str) or len(known) > 200):
        raise ValueError("known_completion_id must be a string")
    return {
        "to": body["to"],
        "worker_session_id": _text(body, "worker_session_id", 64),
        "message_id": _text(body, "message_id", 100),
        "sender_session_id": _text(body, "sender_session_id", 200),
        "text": defang_harness_markers(text),
        "urgent": bool(body.get("urgent")),
        "known_completion_id": known,
    }


_STOPPED_CHILD = "it was stopped on this worker, and a message cannot restart it"


async def _message_child(msg: Mapping[str, Any], from_device: str) -> Dict[str, Any]:
    """Target side: a message from the worker that started this child."""
    from app.core import agent_messaging, provider_agent, turn_scheduler
    from app.core.task_registry import task_registry
    from app.db import remote_tasks

    worker_id = msg["worker_session_id"]
    child = await asyncio.to_thread(remote_tasks.get_child, worker_id)
    if child is None or child["parent_device_id"] != from_device:
        raise PermissionError("No subagent with this id was started here by the asking worker.")
    owed = await asyncio.to_thread(report_owed, worker_id) or {}
    if msg["sender_session_id"] not in {owed.get("parent_session_id"), owed.get("owner_session_id")}:
        raise PermissionError("Only the conversation that started this subagent can message it.")
    report = child.get("report") if isinstance(child.get("report"), dict) else {}
    # ``stop_requested_at`` is the parent's durable stop (S8): it outlives the
    # in-memory latch, so a restart here does not let a message resume it.
    if (task_registry.is_user_stopped(worker_id) or report.get("status") == "stopped"
            or child.get("stop_requested_at")):
        raise ValueError(_STOPPED_CHILD)
    if child["state"] == remote_tasks.RUNNING:
        status, turn_id = await agent_messaging.deliver_message(
            worker_id, msg["sender_session_id"], msg["text"], urgent=msg["urgent"])
        if status == agent_messaging.STOPPED:
            raise ValueError(_STOPPED_CHILD)
        return {"status": status, "turn_id": turn_id}
    if child["state"] == remote_tasks.OWED and child.get("completion_id") != msg["known_completion_id"]:
        raise ValueError("it just finished and its report is still on its way to you; send the "
                         "message again after that report arrives")
    error = await agent_messaging.resume_unavailable(
        worker_id, {"status": _RESUMABLE.get(report.get("status"), "completed")})
    if error:
        raise ValueError(f"it cannot be resumed: {error}")
    # Known as a worker again, so its settle hook owes the new report at once.
    provider_agent.register_worker_session(worker_id)
    turn_id = await turn_scheduler.on_agent_message(
        worker_id, msg["sender_session_id"], msg["text"], urgent=True)
    if not turn_id or turn_id == turn_scheduler.HELD:
        raise ValueError(_STOPPED_CHILD)
    # After the turn exists: a report owed from here on is the resumed run's.
    if not await asyncio.to_thread(remote_tasks.resume_child, worker_id, from_device,
                                   msg["known_completion_id"]):
        logger.warning("remote_task: resumed %s, but its report row did not reopen", worker_id)
    return {"status": RESUMED, "turn_id": turn_id}


async def _message_main(msg: Mapping[str, Any], from_device: str) -> Dict[str, Any]:
    """Parent side: a child this worker sent elsewhere messages ``main``."""
    from app.core import agent_messaging
    from app.db import database, remote_tasks

    worker_id = msg["worker_session_id"]
    dispatch = await asyncio.to_thread(remote_tasks.get_dispatch, worker_id)
    if (dispatch is None or dispatch["target_device_id"] != from_device
            or msg["sender_session_id"] != worker_id):
        raise PermissionError("No subagent with this id was sent to the asking worker.")
    owner = dispatch["owner_session_id"]
    if await asyncio.to_thread(database.get_session, owner) is None:
        raise ValueError("the conversation that started it no longer exists")
    status, turn_id = await agent_messaging.deliver_message(
        owner, worker_id, msg["text"], urgent=msg["urgent"])
    if status == agent_messaging.STOPPED:
        raise ValueError("main was stopped by the owner")
    return {"status": status, "turn_id": turn_id}


async def handle_task_message(body: Dict[str, Any], from_device: str) -> Dict[str, Any]:
    """``task-message`` control action, both directions.

    Only the worker at the other end of this child may send: the one that
    started it (``to: child``) or the one running it (``to: main``). A message
    id is applied once; a retry gets the first answer back with
    ``duplicate: true``.
    """
    from app.db import remote_tasks
    from app.db.async_database import run_sync

    await run_sync(_require_worker_member, from_device)
    msg = _parse_message(body)
    async with _message_lock(msg["message_id"]):
        seen = await run_sync(remote_tasks.message_answer, msg["message_id"])
        if seen is not None:
            if (seen["from_device_id"] != from_device
                    or seen["worker_session_id"] != msg["worker_session_id"]):
                raise ValueError("This message id was already used for another message.")
            return {**seen["answer"], "duplicate": True}
        if msg["to"] == TO_CHILD:
            answer = await _message_child(msg, from_device)
        else:
            answer = await _message_main(msg, from_device)
        await run_sync(
            remote_tasks.record_message, msg["message_id"], worker_session_id=msg["worker_session_id"],
            from_device_id=from_device, direction=msg["to"], answer=answer,
            resume_turn_id=answer.get("turn_id") if answer.get("status") == RESUMED else None)
        return {**answer, "duplicate": False}
