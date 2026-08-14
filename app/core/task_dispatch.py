"""Task-tool dispatch — the unified agent-orchestration surface (rulings A6-A10).

The ``Task`` tool (omni builtin on native legs, ``mcp__builtins__Task`` on the
delegated leg) resolves its target against the unified ``agents`` table and
runs it as a worker session via :func:`app.core.provider_agent.dispatch_worker`
— the proven ProviderAgent machinery (real session rows, approval routing,
supervisor teardown, progress feed, bounded report).

What this module adds on top of raw ``dispatch_worker``:

* target resolution — agent id first, then unambiguous case-insensitive name;
* invoke grants (A10) — open by default; a caller agent with a non-empty
  ``enabled_agents`` list may only invoke agents on that list;
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
  ``background`` returns the worker session id immediately (collect later via
  TaskList/TaskOutput — background workers survive the caller's turn end).
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
# Target resolution
# ---------------------------------------------------------------------------

def resolve_target_agent(ref: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Resolve ``ref`` (agent id, else unambiguous name) to an agent row.

    Returns ``(agent_row, error_message)`` — exactly one is non-None.
    """
    from app.db import database

    ref = (ref or "").strip()
    if not ref:
        return None, "Task requires a target agent (id or name)."
    row = database.get_profile(ref)
    if row:
        return row, None
    matches = [
        a for a in database.get_all_profiles()
        if (a.get("name") or "").strip().lower() == ref.lower()
    ]
    if len(matches) == 1:
        return matches[0], None
    if len(matches) > 1:
        ids = ", ".join(sorted(a["id"] for a in matches))
        return None, f"Agent name '{ref}' is ambiguous — use an id: {ids}"
    return None, f"Unknown agent: '{ref}'. List agents via the agents API/UI."


def _validate_target(agent: Dict[str, Any], model_override: Optional[str]) -> Optional[str]:
    """Provider/model admission for the target agent. None = OK."""
    from app.core import provider_agent
    from app.core.environment import is_provider_servable
    from app.db import database

    config = agent.get("config") or {}
    provider_id = config.get("provider")
    if not provider_id:
        # Delegated Claude-subscription leg — always a valid worker target.
        return None
    try:
        provider = database.get_model_provider(provider_id)
    except Exception:  # noqa: BLE001
        provider = None
    if not provider:
        return f"Agent '{agent['id']}' is bound to unknown provider '{provider_id}'."
    if not provider.get("enabled"):
        return f"Agent '{agent['id']}' is bound to disabled provider '{provider_id}'."
    if not is_provider_servable(provider):
        return (
            f"Agent '{agent['id']}' is bound to provider '{provider_id}' "
            "which cannot serve worker sessions."
        )
    if model_override:
        validation = provider_agent.validate_provider_model(provider_id, model_override)
        if not validation.ok:
            return validation.reason or (
                f"Model '{model_override}' is not curated for provider '{provider_id}'."
            )
    return None


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

async def dispatch_task_tool(
    args: Dict[str, Any],
    *,
    caller_session_id: str,
    caller_profile_id: Optional[str] = None,
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

    target, err = resolve_target_agent(args.get("agent") or "")
    if err:
        return {"ok": False, "error": err}
    assert target is not None

    # Invoke grants (A10): open by default, optional caller allowlist.
    caller_row = _session_row(caller_session_id) or {}
    caller_pid = caller_profile_id or caller_row.get("profile_id")
    if caller_pid:
        from app.db import database

        caller_agent = database.get_profile(caller_pid)
        allowlist = ((caller_agent or {}).get("config") or {}).get("enabled_agents")
        if isinstance(allowlist, list) and allowlist and target["id"] not in allowlist:
            return {
                "ok": False,
                "error": (
                    f"Agent '{caller_pid}' may only invoke: "
                    f"{', '.join(allowlist)} (enabled_agents allowlist)."
                ),
            }

    # Nesting depth + cycles (A9).
    max_depth = int(getattr(settings, "task_tool_max_depth", 5) or 5)
    _, root_session_id, caller_depth = caller_chain(caller_session_id)
    if caller_depth + 1 > max_depth:
        return {
            "ok": False,
            "error": f"Nesting limit reached ({max_depth} levels of agent-invokes-agent).",
        }
    active_agents = _chain_agent_ids(caller_session_id)
    if target["id"] in active_agents:
        return {
            "ok": False,
            "error": (
                f"Cycle refused: agent '{target['id']}' is already active in "
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

    model_override = (args.get("model") or "").strip() or None
    admission_error = _validate_target(target, model_override)
    if admission_error:
        return {"ok": False, "error": admission_error}

    target_config = target.get("config") or {}
    provider_id = target_config.get("provider") or None
    label = (args.get("label") or "").strip() or f"Task · {target.get('name') or target['id']}"

    kwargs: Dict[str, Any] = dict(
        parent_session_id=root_session_id,
        context_session_id=caller_session_id,
        profile_id=target["id"],
        provider_id=provider_id,
        model_id=model_override,
        prompt=prompt,
        label=label,
    )

    if mode == "background":
        task = asyncio.create_task(
            provider_agent.dispatch_worker(background=True, **kwargs)
        )
        _BACKGROUND_TASKS.add(task)
        task.add_done_callback(_BACKGROUND_TASKS.discard)
        # The worker session id is minted inside dispatch_worker; wait briefly
        # for registration so the caller gets a pollable id.
        worker_id = await _await_worker_registration(root_session_id, label)
        return {
            "ok": True,
            "mode": "background",
            "agent": target["id"],
            "worker_session_id": worker_id,
            "note": (
                "Worker dispatched in the background. Poll with TaskList/"
                "TaskOutput (task id = worker session id)."
            ),
        }

    outcome = await provider_agent.dispatch_worker(**kwargs)
    payload: Dict[str, Any] = {
        "ok": outcome.ok,
        "mode": "sync",
        "agent": target["id"],
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

    async def _dispatch(input: Dict[str, Any], ctx: Any) -> str:
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
            return json.dumps({
                "ok": False,
                "error": (
                    "Task dispatch could not resolve the calling session — the "
                    "worker would have no parent session to report back to. "
                    "This is a host wiring problem, not a problem with the "
                    "request; retry once the session is fully registered."
                ),
            })
        payload = await dispatch_task_tool(
            input,
            caller_session_id=str(caller_session_id),
            caller_profile_id=profile_id,
        )
        return json.dumps(payload, default=str)

    return _dispatch
