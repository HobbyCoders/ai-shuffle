"""Task-tool dispatch — the unified agent-orchestration surface (rulings A6-A10).

The ``Task`` tool (omni builtin on native legs, ``mcp__builtins__Task`` on the
delegated leg) resolves its target against the unified ``agents`` table and
runs it as a worker session via :func:`app.core.provider_agent.dispatch_worker`
— the proven ProviderAgent machinery (real session rows, approval routing,
supervisor teardown, progress feed, bounded report).

What this module adds on top of raw ``dispatch_worker``:

* target resolution — subagent id first, then unambiguous name, inside the
  current provider's eligible roster;
* invoke grants (A10) — open by default; a caller agent with a non-empty
  ``enabled_agents`` list may only invoke subagents on that list;
* provider binding — the caller provider and target's configured model on that
  provider are authoritative; Task cannot switch providers or models;
* nesting depth (A9) — max ``settings.task_tool_max_depth`` (5) levels of
  agent-invokes-agent, counted by walking worker-created ancestor sessions;
* cycle detection (A9) — an agent already active in the ancestor chain cannot
  be dispatched again beneath itself;
* worker budget (A9) — at most ``settings.task_tool_max_workers`` live
  workers per root session tree;
* root routing (A9) — approvals, progress cards, and supervisor registration
  land on the ROOT session (the one with a human watching), while
  project/worktree/cwd inheritance still reads the direct caller;
* modes (A8) — ``sync`` blocks and returns the worker's report;
  ``background`` returns the worker session id immediately and announces
  completion to the parent — background workers survive the caller's turn end.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

#: Hard ceiling on parent-chain walks — above max depth anyway.
_CHAIN_LIMIT = 12

#: Background dispatch tasks, kept referenced so asyncio never GCs them.
_BACKGROUND_TASKS: set = set()


# ---------------------------------------------------------------------------
# Chain / depth helpers
# ---------------------------------------------------------------------------

def _session_row(session_id: str) -> Optional[Dict[str, Any]]:
    try:
        from app.db import database

        return database.get_session(session_id)
    except Exception:  # noqa: BLE001
        logger.debug("task_dispatch: session read failed for %s", session_id, exc_info=True)
        return None


def _first_turn_source(session_id: str) -> Optional[str]:
    """The ``source`` of a session's first turn — worker sessions are born
    with a ``provider_agent`` turn; forks/user sessions are not."""
    try:
        from app.db.base import get_db

        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT source FROM turns WHERE session_id = %s "
                "ORDER BY created_at ASC LIMIT 1",
                (session_id,),
            )
            row = cursor.fetchone()
            return row["source"] if row else None
    except Exception:  # noqa: BLE001
        return None


def _is_worker_session(session_id: str) -> bool:
    from app.core import provider_agent

    if provider_agent.is_worker_session(session_id):
        return True
    return _first_turn_source(session_id) == provider_agent.WORKER_TURN_SOURCE


def caller_chain(session_id: str) -> Tuple[List[Dict[str, Any]], str, int]:
    """Walk the parent chain upward from ``session_id``.

    Returns ``(worker_ancestor_rows, root_session_id, depth)`` where ``depth``
    is the number of worker sessions in the chain INCLUDING ``session_id``
    itself (0 for a top-level human session), and ``root_session_id`` is the
    top non-worker session (falls back to the highest reachable ancestor).
    """
    rows: List[Dict[str, Any]] = []
    depth = 0
    current_id = session_id
    root_id = session_id
    for _ in range(_CHAIN_LIMIT):
        row = _session_row(current_id)
        if row is None:
            break
        if current_id != session_id:
            rows.append(row)
        if _is_worker_session(current_id):
            depth += 1
            parent = row.get("parent_session_id")
            if parent:
                root_id = parent
                current_id = parent
                continue
            break
        # First non-worker session is the root — stop walking (a fork's
        # parent link above a human session is lineage, not nesting).
        root_id = current_id
        break
    return rows, root_id, depth


def _chain_agent_ids(session_id: str) -> List[str]:
    """Profile (agent) ids active in the caller chain, caller first."""
    ids: List[str] = []
    current_id: Optional[str] = session_id
    for _ in range(_CHAIN_LIMIT):
        if not current_id:
            break
        row = _session_row(current_id)
        if row is None:
            break
        pid = row.get("profile_id")
        if pid:
            ids.append(pid)
        if not _is_worker_session(current_id):
            break
        current_id = row.get("parent_session_id")
    return ids


def _live_worker_count_for_root(root_session_id: str) -> int:
    """Live Task/ProviderAgent workers anywhere under ``root_session_id``."""
    from app.core import provider_agent_supervisor as sup

    count = 0
    live = getattr(sup, "_LIVE", {})
    for parent_id, bucket in list(live.items()):
        _, root, _ = caller_chain(parent_id)
        if root == root_session_id or parent_id == root_session_id:
            count += len(bucket)
    return count


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

async def dispatch_task_tool(
    args: Dict[str, Any],
    *,
    caller_session_id: str,
    caller_profile_id: Optional[str] = None,
    caller_provider_id: Optional[str] = None,
    launch_tool_use_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Handle one ``Task`` tool call. Never raises — returns a payload dict
    with ``ok`` plus either a report/worker id or an ``error``."""
    from app.core import provider_agent
    from app.core.config import settings

    prompt = (args.get("prompt") or "").strip()
    if not prompt:
        return {"ok": False, "error": "Task requires a prompt."}
    mode = (args.get("mode") or "sync").strip().lower()
    if mode not in ("sync", "background"):
        return {"ok": False, "error": f"Unknown mode '{mode}' (sync|background)."}
    if args.get("model") not in (None, ""):
        return {
            "ok": False,
            "error": (
                "Task does not accept a model override. The selected subagent's "
                "model on the current provider is configured in the Agents UI."
            ),
        }

    # Resolve only inside the provider-scoped roster. This applies invoke
    # grants and provider/model admission before the model-authored target is
    # accepted, so a forged call cannot cross providers even if the agent row
    # exists in the database.
    caller_row = _session_row(caller_session_id) or {}
    caller_pid = caller_profile_id or caller_row.get("profile_id")
    if not caller_pid:
        return {
            "ok": False,
            "error": "Task could not resolve the calling agent profile.",
        }
    from app.core.agent_provider_models import (
        available_subagents,
        resolve_subagent,
    )

    roster = available_subagents(caller_pid, caller_provider_id)
    target, err = resolve_subagent(args.get("agent") or "", roster)
    if err:
        return {"ok": False, "error": err}
    assert target is not None

    # Nesting depth + cycles (A9).
    max_depth = int(getattr(settings, "task_tool_max_depth", 5) or 5)
    _, root_session_id, caller_depth = caller_chain(caller_session_id)
    if caller_depth + 1 > max_depth:
        return {
            "ok": False,
            "error": f"Nesting limit reached ({max_depth} levels of agent-invokes-agent).",
        }
    active_agents = _chain_agent_ids(caller_session_id)
    if target.id in active_agents:
        return {
            "ok": False,
            "error": (
                f"Cycle refused: subagent '{target.id}' is already active in "
                "this dispatch chain."
            ),
        }

    # Worker budget (A9), counted across the whole root tree.
    max_workers = int(getattr(settings, "task_tool_max_workers", 8) or 8)
    if _live_worker_count_for_root(root_session_id) >= max_workers:
        return {
            "ok": False,
            "error": (
                f"Worker budget reached ({max_workers} live workers for this "
                "session). Wait for running workers or stop them."
            ),
        }

    label = (args.get("label") or "").strip() or f"Task · {target.name}"

    kwargs: Dict[str, Any] = dict(
        parent_session_id=root_session_id,
        context_session_id=caller_session_id,
        profile_id=target.id,
        provider_id=caller_provider_id,
        model_id=target.model,
        prompt=prompt,
        label=label,
        # The model's Task tool_use_id (omni native leg) — lets the worker
        # card absorb the launch tool_use block instead of rendering both.
        launch_tool_use_id=launch_tool_use_id,
    )

    if mode == "background":
        task = asyncio.create_task(
            provider_agent.dispatch_worker(background=True, **kwargs)
        )
        _BACKGROUND_TASKS.add(task)
        task.add_done_callback(_BACKGROUND_TASKS.discard)
        # The worker session id is minted inside dispatch_worker; wait briefly
        # for registration so the caller gets an inspectable id.
        worker_id = await _await_worker_registration(root_session_id, label)
        return {
            "ok": True,
            "mode": "background",
            "agent": target.id,
            "model": target.model,
            "worker_session_id": worker_id,
            "note": (
                "Worker dispatched in the background. Completion will be "
                "announced to the parent automatically; continue useful work. "
                "Use TaskList/TaskOutput only for explicit inspection, not a "
                "polling loop."
            ),
        }

    outcome = await provider_agent.dispatch_worker(**kwargs)
    payload: Dict[str, Any] = {
        "ok": outcome.ok,
        "mode": "sync",
        "agent": target.id,
        "model": target.model,
        "worker_session_id": outcome.session_id,
    }
    if outcome.ok:
        payload["report"] = outcome.report or "(no report)"
        if outcome.truncated:
            payload["truncated"] = True
    else:
        payload["error"] = outcome.error or "Worker failed."
    return payload


async def _await_worker_registration(
    root_session_id: str, label: str, timeout: float = 5.0
) -> Optional[str]:
    """Best-effort: return the just-dispatched background worker's session id
    once the supervisor sees it (labels match)."""
    from app.core import provider_agent_supervisor as sup

    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        for record in reversed(sup.live_workers(root_session_id)):
            if (record.label or "").strip() == label.strip():
                return record.worker_session_id
        await asyncio.sleep(0.05)
    return None


# ---------------------------------------------------------------------------
# Tool-surface adapters
# ---------------------------------------------------------------------------

def make_omni_dispatcher(
    session_id: "str | Callable[[], Optional[str]]",
    profile_id: Optional[str] = None,
    provider_id: Optional[str] = None,
):
    """The ``ctx.extra['task_dispatch']`` callable for omni's bare Task builtin
    (native legs).

    ``session_id`` is the caller's **app** session id (``sessions.id``) — the
    row Task dispatch walks for lineage, budget, and worker parenting. It may
    be a plain string or a zero-argument callable resolved at CALL time.

    Late binding is the point: the native leg builds its ``ToolCtx`` inside
    ``HarnessAdapter.connect()``, and a brand-new session has no id to bind
    there yet. The ctx (and therefore this closure) then lives for the whole
    pooled life of the engine, so a connect-time snapshot would stay empty
    forever and every Task call would report the tool unavailable.

    ``ctx.session_id`` is deliberately NOT used as a fallback: on a native leg
    it holds the *omni* session id (``ses_...``), which is not a
    ``sessions.id`` — dispatching against it would parent the worker onto a
    session row that does not exist.
    """

    async def _dispatch(input: Dict[str, Any], ctx: Any) -> Any:
        from omni.tools.registry import ToolOutcome

        caller_session_id: Any = session_id
        if callable(caller_session_id):
            try:
                caller_session_id = caller_session_id()
            except Exception:  # noqa: BLE001
                logger.warning(
                    "task_dispatch: caller session-id supplier raised; Task "
                    "cannot be dispatched for this call",
                    exc_info=True,
                )
                caller_session_id = None
        if not caller_session_id:
            logger.warning(
                "task_dispatch: no app session id resolved at dispatch time — "
                "refusing to dispatch a parentless worker"
            )
            payload = {
                "ok": False,
                "error": (
                    "Task dispatch could not resolve the calling session — the "
                    "worker would have no parent session to report back to. "
                    "This is a host wiring problem, not a problem with the "
                    "request; retry once the session is fully registered."
                ),
            }
            return ToolOutcome(
                text=json.dumps(payload), ui=payload, is_error=True
            )
        # Stamped by the engine for the duration of this dispatch (NativeLoop
        # sets ctx.extra["tool_use_id"] per tool call; dispatches are
        # sequential). Absent on the delegated leg's bridged mcp tool.
        launch_tool_use_id = None
        try:
            candidate = (getattr(ctx, "extra", None) or {}).get("tool_use_id")
            if isinstance(candidate, str) and candidate:
                launch_tool_use_id = candidate
        except Exception:  # noqa: BLE001 — display-only nicety
            launch_tool_use_id = None
        payload = await dispatch_task_tool(
            input,
            caller_session_id=str(caller_session_id),
            caller_profile_id=profile_id,
            caller_provider_id=provider_id,
            launch_tool_use_id=launch_tool_use_id,
        )
        return ToolOutcome(
            text=json.dumps(payload, default=str),
            ui=payload,
            is_error=not bool(payload.get("ok")),
        )

    return _dispatch


def subagent_system_prompt(session_id: Optional[str]) -> Optional[str]:
    """Return the model-facing role block for a Task-created worker."""
    if not session_id or not _is_worker_session(session_id):
        return None
    from app.core.config import settings

    _, _, depth = caller_chain(session_id)
    max_depth = int(getattr(settings, "task_tool_max_depth", 5) or 5)
    if depth < max_depth:
        nesting = (
            f"You are at depth {depth} of {max_depth}. You may delegate only "
            "through your own provider-scoped Task roster."
        )
    else:
        nesting = (
            f"You are at the maximum subagent depth ({max_depth}) and cannot "
            "delegate further."
        )
    return (
        "# Subagent context\n"
        "You are a subagent, not the main agent. The first user message is your "
        "complete assigned task. You do not have the parent's conversation.\n"
        "Focus on the assigned scope, use the available tools, and verify the "
        "result. Do not start a conversation with the user or take unrelated "
        "side work. Your final response is returned to the parent, so report "
        "concise accomplishments, findings, changed files, verification, and "
        f"blockers. {nesting}"
    )
