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

Not here yet (later slices of docs/reviews/cross-worker-subagents-2026-10-03.md):
report delivery back to the parent (S6), the parent's registry row, supervisor
record and mirrored progress card (S7), stop (S8), resume by message (S9) and
approval routing (S10). Until S10 a remote child runs with Away handling: an
approval it needs is deferred, never granted, and listed in its final reply.
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
})
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
) -> Dict[str, Any]:
    """The ``task-start`` body. Identifiers only; never a path or credential."""
    return {
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


def _caller_bindings(caller_row: Mapping[str, Any], caller_session_id: str):
    """``(permission mode, loop-guard origin)`` the child inherits. Blocking."""
    from app.core import loop_guard
    from app.core.provider_agent import _caller_permission_mode

    return _caller_permission_mode(caller_row), loop_guard.session_origin(caller_session_id)


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

    worker_id = str(uuid.uuid4())
    permission_mode, origin = await asyncio.to_thread(_caller_bindings, caller_row, caller_session_id)
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
    )
    if stop_generation is not None and task_registry.stop_invalidated(root_session_id, stop_generation):
        await release()
        return {"ok": False, "error": "The user stopped this Task dispatch.", "killed": True}
    try:
        answer = await control.call(worker["device_id"], TASK_START_ACTION, body, timeout=START_TIMEOUT)
    except control.ControlRefused as exc:
        await release()
        reason = str(exc).rstrip(".")
        if "Unknown control action" in reason:
            reason = "it runs an older release; ask the owner to update it"
        return {"ok": False, "error": f"{name} refused the subagent: {reason}; {NOT_DISPATCHED}."}
    except control.RunnerUnreachable as exc:
        await release()
        if MAY_HAVE_APPLIED in str(exc):
            # The request was sent: the child may be running there.
            return {"ok": False, "error": (
                f"{name} did not confirm the subagent: {exc} Check that worker before "
                "dispatching it again.")}
        return {"ok": False, "error": f"{name} could not be reached: {str(exc).rstrip('.')}; {NOT_DISPATCHED}."}
    if not isinstance(answer, dict) or answer.get("worker_session_id") != worker_id:
        await release()
        return {"ok": False, "error": (
            f"{name} gave an invalid answer; the subagent may have started there. Check that "
            "worker before dispatching it again.")}
    # S7 attaches here: the registry row, supervisor record and mirrored feed
    # for this child, keyed by ``worker_id`` on the owner ``caller_session_id``.
    return remote_receipt(answer, worker=worker, worker_id=worker_id, result_agent=result_agent,
                          model_id=model_id, label=label, mode=mode)


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
            f"{permission_mode}. Its report does not come back to you automatically yet, and "
            "TaskList/TaskGet do not track it; do not wait or park on it. The owner can follow it on "
            f"{name}. Approvals it needs are held back there (never granted) and listed in its "
            "final reply."
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
    from app.db import database, turns
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
    fields = provider_agent.worker_session_fields(
        worker_session_id=start.worker_session_id, profile_id=start.agent, title=start.label,
        parent_session_id=start.parent_session_id, provider_id=start.provider_id,
        model_id=start.model, effort=start.effort, project_v2_id=project_v2_id,
        worktree_branch=checkout.branch if checkout else None,
        worktree_path=checkout.path if checkout else None,
        permission_mode=permission_mode,
    )
    overrides = provider_agent.worker_turn_overrides(
        provider_id=start.provider_id, model_id=start.model, effort=start.effort,
        cwd=received.cwd, permission_mode=permission_mode, checkout=checkout,
        tool_allowlist=tools,
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
            # Approvals cannot travel back yet (S10): Away handling defers
            # them, never grants them, and the child lists them in its reply.
            conn.cursor().execute(
                "UPDATE sessions SET away_mode=TRUE, branch=%s WHERE id=%s",
                (checkout.branch if checkout else None, start.worker_session_id))
            turn = turns.enqueue_turn(
                start.worker_session_id, provider_agent.WORKER_TURN_SOURCE, prompt=start.prompt,
                profile_id=start.agent, overrides=overrides, payload=payload,
                command_id=COMMAND_PREFIX + start.worker_session_id, _conn=conn)
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
