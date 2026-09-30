"""
Agent option building for queries.

Contains functions that construct AgentOptions, assemble system prompts,
configure MCP servers, build tool/permission configurations, and manage
subagent definitions.

Core types, thinking config, and the main build_options_from_profile function
live here. MCP config, agent config, and environment setup are split into
sibling modules and re-exported for backward compatibility.
"""

import asyncio
import hashlib
import logging
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from app.core.agent_types import AgentOptions

from app.db import database
from app.core.config import settings
from app.core.platform import detect_deployment_mode, DeploymentMode

from app.core.project_utils import get_project_working_dir
from app.core.model_registry import context_window_choices

# --- Re-exports for backward compatibility ---
# All symbols that were previously defined here are re-exported so that
# existing ``from app.core.query_options import X`` statements keep working.
from app.core.mcp_config import (  # noqa: F401
    _build_mcp_servers_dict,
    _build_mcp_servers_dict_with_ai_tools,
    _build_plugins_list,
)
from app.core.agent_config import (  # noqa: F401
    resolve_note_references,
)
from app.core.environment import (  # noqa: F401
    DEFAULT_SECURITY_INSTRUCTIONS,
    get_security_instructions,
    _is_git_repo,
    _get_os_version,
    _get_decrypted_api_key,
    _resolve_api_credential,
    _get_hook_tool_names,
    _build_extra_args,
    _build_env_with_ai_tools,
    apply_context_window_override,
    apply_provider_env,
    generate_environment_details,
)

logger = logging.getLogger(__name__)


def _ai_tools_credential_fingerprint(tool_env: Optional[Dict[str, str]]) -> Optional[str]:
    """Return a non-secret identity for the fal credential carried by tools.

    The persistent ``ai-tools`` Node worker captures its environment at spawn
    time. A fingerprint lets the session pool detect credential rotation or
    first configuration without storing or comparing the secret itself.
    """
    key = (tool_env or {}).get("FAL_KEY")
    if not isinstance(key, str) or not key:
        return None
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _ai_tools_credential_config(
    ai_tools_config: Optional[Dict[str, Any]], ai_tool_names: List[str]
) -> Dict[str, Any]:
    """The ``ai_tools`` toggle map the credential env is built from.

    The ai-tools server is attached from the permitted tool set, not the
    profile's retired ``ai_tools`` switches. The env that carries ``FAL_KEY`` and
    the per-category default models has to follow the same set: gated on the
    switches alone, the tools would attach to a Node worker with no key.
    Unlike ``ai_tools_config_from_names`` this keeps the catch-all tools, since
    ``runFalModel`` and ``searchModels`` run on the same key.
    """
    from app.mcp.ai_tools_sdk import TOOL_CONFIG_MAP

    config = dict(ai_tools_config or {})
    for name in {n.split("__")[-1] for n in ai_tool_names}:
        mapping = TOOL_CONFIG_MAP.get(name) or {}
        for key in mapping.get("config_keys") or [mapping.get("config_key")]:
            if key:
                config[key] = True
    return config


# The interactive question tools, blocked for worker sessions: a worker must
# not be able to ask the human anything. Approvals redirect to the parent;
# questions do not — they would stall on the question TTL. Fluid's AskUser has
# the same interaction kind as the CLI's AskUserQuestion.
WORKER_DISALLOWED_TOOLS = ("AskUserQuestion", "AskUser")


# The Claude Code CLI ships a ``claude-api`` skill (Anthropic SDK reference
# docs). Invoking it injects ~142K tokens of documentation in one shot, and its
# broad trigger ("the prompt names Claude in any form") makes non-Claude models
# fire it constantly (see the ``CLAUDE_CODE_BLOCKING_LIMIT_OVERRIDE`` note in
# ``environment.apply_provider_env``). Denied with a ``Skill(<name>)`` rule in
# ``--disallowed-tools``, which the CLI matches against the skill name (verified
# against the CLI once bundled with claude-agent-sdk 0.2.126; re-check on CLI
# upgrades).
NON_CLAUDE_DISALLOWED_SKILL = "Skill(claude-api)"


# ---------------------------------------------------------------------------
# Provider-model composite codec (`prov:<providerId>:<modelId>`)
# ---------------------------------------------------------------------------
#
# The frontend counterpart lives in
# ``frontend/src/lib/constants/models.svelte.ts``
# (``encodeProviderModel`` / ``decodeProviderModel``). Both the profile editor's
# ModelPicker and the chat composer's model override emit this shape; only the
# composer's value reaches here (as ``overrides["model"]``), where the decoded
# provider id REBINDS the turn's provider.

PROVIDER_MODEL_PREFIX = "prov:"


def decode_provider_model(value: Any) -> Optional[tuple]:
    """Decode ``prov:<providerId>:<modelId>`` → ``(provider_id, model_id)``.

    Returns ``None`` when ``value`` is not a well-formed composite (a plain
    model alias, a custom id, a non-string, an empty half, …).

    Splits on the FIRST colon after the prefix only: provider model ids
    routinely contain ``/`` and ``:`` themselves (e.g. OpenRouter's
    ``meta-llama/llama-3.1-8b-instruct:free``), which a naive ``split(":")``
    would corrupt.
    """
    if not isinstance(value, str) or not value.startswith(PROVIDER_MODEL_PREFIX):
        return None
    rest = value[len(PROVIDER_MODEL_PREFIX):]
    sep = rest.find(":")
    if sep <= 0:
        return None
    provider_id, model_id = rest[:sep], rest[sep + 1:]
    if not provider_id or not model_id:
        return None
    return provider_id, model_id


def is_native_claude_model(value: Any) -> bool:
    """Whether ``value`` names a model the NATIVE/delegated Claude leg serves.

    True for the registry's short aliases (``sonnet``/``opus``/``fable``/
    ``haiku``, including legacy ids that normalize onto them) and for raw
    ``claude-*`` ids. Mirrors
    the same test ``harness_adapter._decide_provider_kind`` applies to the model
    string, so "the composer picked a Claude model" and "the leg decision routes
    it to Claude" can never disagree.
    """
    if not isinstance(value, str) or not value:
        return False
    from app.core.model_registry import get_model_family

    return get_model_family(value) is not None or value.lower().startswith("claude")


def _append_system_block(prompt: Any, block: Optional[str]) -> Any:
    """Append app-owned guidance to either supported system-prompt shape."""
    if not block or not block.strip():
        return prompt
    if isinstance(prompt, dict):
        updated = dict(prompt)
        current = str(updated.get("append") or "").strip()
        updated["append"] = f"{current}\n\n{block}" if current else block
        return updated
    current = str(prompt or "").strip()
    return f"{current}\n\n{block}" if current else block


# The sidepanel mode a session belongs to, and the tool ceiling it imposes.
# Same import discipline as ``tool_access`` below — it is the only thing
# ``session_modes`` imports, so neither can close a cycle.
from app.core.session_modes import (
    ensure_chat_uploads_dir,
    narrow_tool_access_for_mode,
    permission_mode_for_session_mode,
    session_mode_of,
)

# Tool access. ``resolve_tool_access`` is the one seam both legs read; it lives
# in ``app.core.tool_access`` (no app imports, so migrations and the API layer
# can use it too) and is re-exported here for existing importers.
from app.core.tool_access import (  # noqa: F401  (re-exports, see above)
    DENY_ALL,
    LEGACY_TOOL_ALIASES as _LEGACY_TOOL_ALIASES,
    ToolAccess,
    apply_legacy_tool_aliases as _apply_legacy_tool_aliases,
    intersect_tool_surfaces,
    resolve_tool_access,
    tool_permitted,
    tool_selection_mode,
)

# This builder and both harness execution legs now enforce proactive provenance.
# Registration changes only process capability state, not assistant policy.
from app.core.assistant_service import register_proactive_guard
register_proactive_guard(1)


def _ai_tools_available() -> bool:
    """Whether the AI-generation tools have a credential to run on.

    The ai-tools server is attached when at least one AI tool is both
    AVAILABLE and permitted — every one of them runs on the same fal.ai key, so
    without it the picker's selection would put non-functional tools on the
    model's surface.
    """
    try:
        from app.mcp.ai_tools_sdk import _get_fal_api_key

        return bool(_get_fal_api_key())
    except Exception:  # noqa: BLE001 — an unreadable key is an unavailable one
        logger.debug("could not resolve the fal.ai key", exc_info=True)
        return False


# The background-work MANAGERS. Every one of them reports on, reads or stops
# work that Task or Monitor started — none of them can start anything — so a
# surface with neither producer leaves the model four tools that can only ever
# answer "no tasks". Bare names are checked alongside the bridged ones because
# a native leg runs fluid's bare Task/Monitor while keeping these MCP copies.
_TASK_MANAGER_TOOLS = (
    "mcp__builtins__TaskGet",
    "mcp__builtins__TaskList",
    "mcp__builtins__TaskOutput",
    "mcp__builtins__TaskStop",
)
_BACKGROUND_PRODUCERS = (
    "mcp__builtins__Task",
    "mcp__builtins__Monitor",
    "Task",
    "Monitor",
)


def _drop_orphan_task_managers(
    names: List[str],
    allowed: Optional[Iterable[str]],
    disallowed: Optional[Iterable[str]],
) -> List[str]:
    """Strip the task managers when nothing on the surface can start a task.

    Monitor counts as a producer, not just Task: TaskOutput/TaskStop are the
    Monitor's companions and a session with Monitor but no Task still needs
    them. Only when BOTH producers are off do the four managers become tools
    with nothing to manage.
    """
    if any(
        tool_permitted(name, allowed, disallowed)
        for name in _BACKGROUND_PRODUCERS
    ):
        return names
    return [name for name in names if name not in _TASK_MANAGER_TOOLS]


def _app_config_permitted(config: Optional[Dict[str, Any]]) -> bool:
    """Whether this agent's tool access permits ANY ``mcp__app-config__*`` tool.

    There is no standalone ``app_config_tools`` switch: the group is
    permitted or not by the same picker every other tool goes through. Admin-ness
    is a separate, unchanged gate the callers apply.
    """
    from app.mcp.app_config_sdk import get_app_config_tool_names

    access = resolve_tool_access(config)
    return any(
        tool_permitted(name, access.allowed, access.disallowed)
        for name in get_app_config_tool_names()
    )


class WorktreeUnavailableError(Exception):
    """Raised when a worktree-flagged session's working directory no longer exists
    on disk and we refuse to silently fall back to the main checkout.

    Running a worktree session in the project's main directory is never correct —
    it lands the user's work on the wrong branch. A worktree session only needs
    its initial working directory to exist (exactly like a local session); we do
    NOT recreate anything on the resume path. When the directory is genuinely
    gone, callers (the query engines) convert this into a user-visible error
    event instead of executing in main.
    """

    def __init__(
        self,
        session_id: Optional[str],
        branch_name: Optional[str] = None,
        reason: str = "",
        path: Optional[str] = None,
    ):
        self.session_id = session_id
        self.branch_name = branch_name
        self.reason = reason
        self.path = path
        branch = f" (branch '{branch_name}')" if branch_name else ""
        where = f" at {path}" if path else ""
        detail = f" ({reason})" if reason else ""
        hint = (
            f" — you can ask the agent there to recreate the worktree from "
            f"branch '{branch_name}'" if branch_name else ""
        )
        super().__init__(
            f"The working directory for this session{branch} no longer exists"
            f"{where}{detail}. Start a new session{hint}. Refusing to run in the "
            f"main checkout."
        )


def _claim_and_run_pending_checkout(
    project_v2: Dict[str, Any], branch: Optional[str]
) -> bool:
    """Spawn-path inline claim of a queued ``ensure_worktree`` git op.

    Returns True when an op for (this runner, this project, this branch) was
    found, won, and executed successfully — meaning a retry of ``resolve_cwd``
    is worth attempting. Returns False for "nothing queued", "someone else won
    the claim", or "the op failed"; every one of those leaves the caller on its
    normal refusal path.

    Never raises: a bug here must degrade to the honest
    ``WorktreeUnavailableError``, never to an opaque spawn crash.
    """
    if not branch:
        return False
    try:
        from app.core import git_ops_runner
        from app.core.config import settings as _settings
        from app.db import git_ops as git_ops_db

        me = _settings.effective_instance_id
        pending = git_ops_db.find_queued(
            me, project_v2.get("id"), "ensure_worktree", branch=branch
        )
        if pending is None:
            return False
        claimed = git_ops_db.claim_op(pending["id"], me)
        if claimed is None:
            # Lost the race. Must not wait here: this runs inside the sync
            # option builder and would occupy a run_sync worker for 30s.
            # The async preflight (ensure_pending_checkout) is the waiter.
            return False
        # This is the EXPECTED path for a session created from another device,
        # so it is announced as ordinary work (INFO) before it runs — not as the
        # aftermath of a failure.
        logger.info(
            "Materializing worktree via queued ensure_worktree op %s (branch %s)",
            claimed["id"], branch,
        )
        try:
            result = git_ops_runner.execute_ensure_worktree(claimed)
        except Exception as exc:  # noqa: BLE001 — the failure belongs on the row
            logger.warning(
                "Inline ensure_worktree claim failed for branch %s: %s", branch, exc
            )
            git_ops_db.fail_op(claimed["id"], str(exc))
            return False
        git_ops_db.complete_op(claimed["id"], result)
        logger.info(
            "Queued ensure_worktree op %s materialized branch %s -> %s",
            claimed["id"], branch, result.get("path"),
        )
        return True
    except Exception:  # noqa: BLE001 — degrade to the normal refusal path
        logger.warning(
            "Inline git-op claim errored for branch %s", branch, exc_info=True
        )
        return False


def _rescue_pending_checkout(session_row: Dict[str, Any]) -> bool:
    """Pre-flight half of the branch-session race guard: materialize this
    session's checkout from a queued ``ensure_worktree`` op BEFORE the cwd
    resolver runs.

    Running first matters for the logs, not the outcome: letting
    ``resolve_cwd`` try and fail first records the checkout as "branch … not
    materializable on this runner" — a WARNING for the case that is about to
    succeed. The probe is one small indexed read, and a spawn with nothing
    queued (the overwhelming majority) pays only that, resolving the project
    only once an op is actually waiting.

    Never raises: any trouble here simply leaves the post-resolve guard —
    unchanged, still the authority on refusing — to run as before.
    """
    try:
        branch = session_row.get("branch") or session_row.get("worktree_branch")
        if not branch:
            return False
        from app.core import project_resolver
        from app.db import git_ops as git_ops_db

        probe = git_ops_db.find_queued(
            settings.effective_instance_id, None, "ensure_worktree", branch=branch
        )
        if probe is None:
            return False
        project_v2 = project_resolver.resolve_project_v2(session_row)
        if project_v2 is None:
            return False
        return _claim_and_run_pending_checkout(project_v2, branch)
    except Exception:  # noqa: BLE001 — degrade to the post-resolve guard
        logger.warning("Pending-checkout pre-flight errored", exc_info=True)
        return False


async def ensure_pending_checkout(
    session_row: Optional[Dict[str, Any]],
    timeout_s: float = 30.0,
) -> bool:
    """Wait for (or claim-run) a matching ``ensure_worktree`` on the event loop.

    One timeout budget for the whole preflight. Never calls ``ensure_checkout``
    unless this task won ``claim_op`` — the wait path must not double-add.
    Returns False on timeout/error/nothing-to-do; the caller still runs
    ``build_session_options`` because cwd may already exist.
    """
    if not session_row:
        return False
    branch = session_row.get("branch") or session_row.get("worktree_branch")
    if not branch:
        return False
    try:
        from app.core import git_ops_runner
        from app.core.config import settings as _settings
        from app.db import git_ops as git_ops_db
        from app.db.async_database import run_sync

        me = _settings.effective_instance_id
        pv2_id = session_row.get("project_v2_id")
        deadline = time.monotonic() + timeout_s

        def remaining() -> float:
            return max(0.0, deadline - time.monotonic())

        pending = await run_sync(
            git_ops_db.find_matching, me, pv2_id, "ensure_worktree", branch
        )
        zombies = await run_sync(
            git_ops_db.list_stale_running, me, pv2_id, "ensure_worktree", branch
        )
        pending_id = pending["id"] if pending else None
        for zombie in zombies:
            if zombie.get("runner_id") != me:
                continue
            if pending_id is not None and zombie["id"] == pending_id:
                continue
            logger.warning(
                "git_ops: failing stale running op=%s", zombie["id"]
            )
            await run_sync(git_ops_db.fail_op, zombie["id"], "stale running")
        if pending is None:
            return False
        if pending["status"] == "queued":
            claimed = await run_sync(git_ops_db.claim_op, pending["id"], me)
            if claimed is not None:
                logger.info(
                    "Materializing worktree via queued ensure_worktree op %s "
                    "(branch %s)",
                    claimed["id"], branch,
                )
                try:
                    result = await run_sync(
                        git_ops_runner.execute_ensure_worktree, claimed
                    )
                except Exception as exc:  # noqa: BLE001 — failure belongs on the row
                    logger.warning(
                        "Inline ensure_worktree claim failed for branch %s: %s",
                        branch, exc,
                    )
                    await run_sync(git_ops_db.fail_op, claimed["id"], str(exc))
                    return False
                await run_sync(git_ops_db.complete_op, claimed["id"], result)
                logger.info(
                    "Queued ensure_worktree op %s materialized branch %s -> %s",
                    claimed["id"], branch, result.get("path"),
                )
                return True
            # Lost the race; wait on the same id rather than double-add.
        wait_started = time.monotonic()
        while remaining() > 0:
            row = await run_sync(git_ops_db.get_op, pending["id"])
            if row is None or row["status"] in ("done", "error"):
                status = (row or {}).get("status")
                logger.info(
                    "git_ops: spawn wait finished op=%s status=%s duration=%.2fs",
                    pending["id"], status, time.monotonic() - wait_started,
                )
                return bool(row) and row["status"] == "done"
            await asyncio.sleep(min(0.2, remaining()))
        logger.warning("git_ops: spawn wait timed out op=%s", pending["id"])
        return False
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — degrade to the normal refusal path
        logger.warning(
            "ensure_pending_checkout errored for branch %s", branch, exc_info=True
        )
        return False


# Operating guidance appended to the system prompt when computer-use is active.
COMPUTER_USE_GUIDANCE = (
    "## Computer use\n"
    "You can control this **Windows** computer with the `mcp__computer-use__*` tools "
    "(screenshot, left_click, type, key, scroll, open_application, zoom, request_access, "
    "computer_batch, …) — the standard computer-use interface, with the usual conventions "
    "(coordinates are [x, y] pixels in the most recent screenshot).\n"
    "- Call `request_access` FIRST with the apps you'll need (Start-menu names, e.g. "
    "'Notepad', 'File Explorer') and a one-line reason; wait for approval, then take a "
    "`screenshot` to begin.\n"
    "- To launch or focus an app, use `open_application` (e.g. open_application app='Notepad') "
    "rather than hunting for its window — this app (AI Shuffle) runs in a browser that is "
    "usually in front and won't move itself.\n"
    "- Take a fresh `screenshot` after actions; use `zoom` to read small text. Use "
    "`computer_batch` for predictable sequences (click → type → Return).\n"
    "- For multiple monitors, read the display note on screenshots and use `switch_display`.\n"
    "- You share the desktop mouse and keyboard with the user. Prefer the browser tools "
    "for web-only work to avoid moving their mouse. The purple agent marker shows your "
    "last position, not a separate input device. Call `release_control` when done.\n"
    "- One chat owns the shared desktop at a time, including between screenshots and "
    "actions. A busy result means another action/chat owns it: do other work and retry "
    "later. Never run desktop actions in parallel; use `computer_batch` for sequences.\n"
    "- The user can press physical Escape anywhere or Stop computer use in the app to "
    "halt desktop tools for this turn. Respect the stop; do not work around it. The chat "
    "Stop button also stops the agent. Tool-generated Escape still closes menus normally.\n"
    "- Synthetic input cannot operate the Windows UAC / secure desktop or the lock screen. "
    "If you hit an elevation prompt or a black screenshot, stop and ask the user to handle it."
)

# Token budgets for thinking modes (token-based thinking)
THINKING_TOKEN_BUDGETS = {
    'think': 8000,
    'think_harder': 24000,
    'ultra_think': 64000
}


def _build_thinking_config(
    config: Dict[str, Any],
    model: Optional[str],
    overrides: Optional[Dict[str, Any]] = None,
    session_effort: Optional[str] = None,
    allow_profile_effort: bool = True,
) -> Dict[str, Any]:
    """
    Build thinking/effort configuration for AgentOptions.

    Thinking and effort are independent fields:
    - thinking: ThinkingConfigAdaptive|ThinkingConfigEnabled|ThinkingConfigDisabled
      - 'adaptive': model decides when/how much to think
      - 'enabled' + budget_tokens: token-budget based thinking
      - 'disabled': suppress extended thinking
    - effort: 'low'|'medium'|'high'|'max' - controls reasoning depth
      Works independently of (and alongside) thinking config.

    Chat-level overrides (thinking_mode, effort) take priority over profile config.

    Args:
        config: Profile configuration dict
        model: Model name (e.g., 'opus', 'sonnet', 'haiku')
        overrides: Optional query-level overrides dict (may contain 'thinking_mode' and/or 'effort')
        session_effort: The effort the session itself is bound to (picked at
            session creation, not pinned by the agent). Beaten by a
            query-level override, beats the agent's legacy config.effort.
        allow_profile_effort: False for worker sessions, which use the model
            default when neither spawn nor caller selected an effort.

    Returns:
        Dict with thinking/effort configuration to spread into AgentOptions
    """
    result: Dict[str, Any] = {}
    overrides = overrides or {}

    # Resolve thinking config: override takes priority over profile config
    thinking_mode = overrides.get('thinking_mode') or config.get('thinking_mode', 'default')

    if thinking_mode == 'disabled':
        result['thinking'] = {'type': 'disabled'}
        logger.debug("Thinking disabled by config")
    elif thinking_mode == 'adaptive':
        result['thinking'] = {'type': 'adaptive'}
        logger.debug("Using adaptive thinking")
    elif thinking_mode in THINKING_TOKEN_BUDGETS:
        budget_tokens = THINKING_TOKEN_BUDGETS[thinking_mode]
        result['thinking'] = {'type': 'enabled', 'budget_tokens': budget_tokens}
        logger.debug(f"Using token-based thinking with budget={budget_tokens}")
    # 'default' or unrecognized: omit thinking field, so the leg uses its default

    # Resolve effort: override takes priority over profile config (independent of thinking)
    # Preserve recognized values here; the adapter validates the selected route and model.
    effort = overrides.get('effort') or session_effort
    if not effort and allow_profile_effort:
        effort = config.get('effort')
    if effort and effort in ('none', 'minimal', 'low', 'medium', 'high', 'max', 'xhigh', 'ultra'):
        result['effort'] = effort
        logger.debug(f"Using effort={effort}")

    return result


def _resolve_thinking_display(
    config: Dict[str, Any],
    overrides: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """Resolve the thinking-display CLI flag value ("summarized" | "omitted").

    Opus 4.7 silently defaults ``display`` to ``"omitted"`` (signature-only, no
    thinking text). Callers that want summarized thinking text must pass
    ``summarized``.

    Forwarded via ``extra_args={"thinking-display": ...}``, the one
    ``extra_args`` key the harness adapter honors (it rides to the CLI as a raw
    flag and to the native anthropic lane via ``SessionOptions.extra``).
    """
    overrides = overrides or {}
    value = overrides.get('thinking_display') or config.get('thinking_display')
    if value in ('summarized', 'omitted'):
        return value
    return None


def build_options_from_profile(
    profile: Dict[str, Any],
    project: Optional[Dict[str, Any]] = None,
    overrides: Optional[Dict[str, Any]] = None,
    resume_session_id: Optional[str] = None,
    can_use_tool: Optional[callable] = None,
    hooks: Optional[Dict[str, list]] = None,
    api_user_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> tuple[AgentOptions, None]:
    """
    Convert a profile to AgentOptions with all available options.

    Returns:
        Tuple of (AgentOptions, agents_dict)
        - AgentOptions: SDK options with agents included for Docker/Linux, excluded for Windows
        - agents_dict: Always None; agents are dispatched through the harness

    Args:
        profile: Profile configuration dict
        project: Optional project configuration
        overrides: Optional override settings
        resume_session_id: Optional SDK session ID to resume (from Claude SDK)
        can_use_tool: Optional permission callback
        hooks: Optional hooks configuration
        api_user_id: Optional API user ID for user-specific credential resolution
        session_id: Optional our session ID (for worktree lookup)

    Note: Agents are always passed programmatically via options.agents for tool
    enforcement.
    """
    config = profile["config"]
    overrides = overrides or {}

    # Determine working directory and execution mode (needed for env details injection)
    # Priority order:
    # 1. Override cwd (explicit worktree path from agent engine)
    # 2. Session worktree (for resumed sessions)
    # 3. Project path
    # 4. Profile config cwd
    # 5. Default workspace
    execution_mode = "local"
    worktree_info = None
    working_dir_source = "workspace"

    # The session row, fetched ONCE below and reused by both the v2 resolver seam
    # and the fail-fast rebind guard (so neither re-fetches). None outside the
    # session_id branch (e.g. override / project-only spawns).
    session_row = None
    if session_id:
        try:
            session_row = database.get_session(session_id)
        except Exception:
            session_row = None

    if session_mode_of(session_row) == "chat":
        # Do not resolve/materialize a stale project binding before choosing the
        # chat cwd. Even spawning a Chat session must not perform repository work.
        project = None
        working_dir = str(ensure_chat_uploads_dir())
        working_dir_source = "chat uploads"
    elif overrides.get("cwd"):
        # Override takes highest priority - used by agent engine for worktrees
        working_dir = overrides.get("cwd")
        working_dir_source = "override"
        # Check if override includes worktree info
        if overrides.get("worktree_info"):
            execution_mode = "worktree"
            worktree_info = overrides.get("worktree_info")
    elif session_id:
        # V2 projects: the origin-keyed resolver is the SINGLE cwd authority for
        # every session (resolver-FIRST, no project_v2_id gate). This is the SPAWN
        # path, so resolve_cwd materializes the checkout (git worktree add) and
        # lazily self-heals project_v2_id + sessions.branch off the legacy
        # project_id / worktree_branch. Fetch the session row ONCE.
        from app.core import project_resolver

        # RACE GUARD, first half. A branch session created FROM ANOTHER
        # DEVICE arrives with a queued ``ensure_worktree`` op instead of a
        # materialized checkout, and its first turn can be claimed before the
        # git-ops loop's next 5s tick. Run that op HERE, so the expected remote
        # path materializes the checkout instead of tripping the resolver's
        # not-materializable warning on the way to the same result. Nothing
        # queued → no-op; the post-resolve guard below still covers ops that
        # appear after this point.
        if session_row:
            _rescue_pending_checkout(session_row)

        v2_cwd = project_resolver.resolve_cwd(session_row) if session_row else None
        if v2_cwd is not None:
            working_dir = v2_cwd
            working_dir_source = "project-v2"
            branch = session_row.get("branch") if session_row else None
            if branch:
                # A branch-bound v2 checkout is a worktree; record minimal info so
                # the environment-details prompt reflects the branch. base_branch
                # comes from the legacy worktree row when the session has one, else
                # the conventional default.
                execution_mode = "worktree"
                base_branch = "main"
                try:
                    wt_row = database.get_worktree_by_session(session_id)
                    if wt_row and wt_row.get("base_branch"):
                        base_branch = wt_row["base_branch"]
                except Exception:  # noqa: BLE001 — best-effort base_branch enrichment
                    pass
                worktree_info = {"branch": branch, "base_branch": base_branch}
        else:
            # resolve_cwd returned None. Two cases, distinguished by whether the
            # session resolves to a project_v2 at all:
            #  (i) it HAS a project_v2 but that project is not materialized on THIS
            #      runner (its checkout lives only on another install) → refuse
            #      rather than silently running in the wrong place. A worktree-
            #      flagged session raises WorktreeUnavailableError (carrying the
            #      branch); the query engines convert it to a user-visible event. A
            #      plain session raises an actionable "not checked out here" error.
            #  (ii) it has NO project_v2 and no live legacy project row (orphan /
            #      project-less session) → legacy parity: the profile-config cwd,
            #      else the default workspace.
            project_v2 = (
                project_resolver.resolve_project_v2(session_row)
                if session_row
                else None
            )
            if project_v2 is not None:
                row = session_row or {}
                branch = row.get("branch") or row.get("worktree_branch")
                if branch or row.get("worktree_id") or row.get("worktree_path"):
                    # RACE GUARD. A branch session created FROM ANOTHER
                    # DEVICE leaves a queued ``ensure_worktree`` git op for us
                    # instead of a materialized checkout, and its first turn can
                    # be claimed before the git-ops loop's next 5s tick. That op
                    # row is the DURABLE CARRIER of the caller's
                    # ``create_new_branch`` intent — a brand-new branch exists
                    # nowhere yet, so failing here would strand the session
                    # permanently rather than merely delay it. Claim and execute
                    # the matching op INLINE (the claim is the same conditional
                    # UPDATE the loop uses, so exactly one of us ever runs it),
                    # then retry cwd resolution ONCE.
                    retry_cwd = None
                    if session_row and _claim_and_run_pending_checkout(
                        project_v2, branch
                    ):
                        retry_cwd = project_resolver.resolve_cwd(session_row)
                    if retry_cwd is None:
                        raise WorktreeUnavailableError(
                            session_id,
                            branch_name=branch,
                            reason=(
                                "its checkout is not materialized on this device "
                                "(or could not be prepared — see server logs)"
                            ),
                        )
                    working_dir = retry_cwd
                    working_dir_source = "project-v2"
                    execution_mode = "worktree"
                    base_branch = "main"
                    try:
                        wt_row = database.get_worktree_by_session(session_id)
                        if wt_row and wt_row.get("base_branch"):
                            base_branch = wt_row["base_branch"]
                    except Exception:  # noqa: BLE001 — best-effort enrichment
                        pass
                    worktree_info = {"branch": branch, "base_branch": base_branch}
                else:
                    raise ValueError(
                        f"This session's project is not checked out on this device "
                        f"(project '{project_v2.get('name') or project_v2.get('origin')}'). "
                        f"Clone the repository here and open the project, then try again."
                    )
            elif config.get("cwd"):
                working_dir = config.get("cwd")
                working_dir_source = "profile config"
            else:
                working_dir = str(settings.effective_workspace_dir)
    elif project:
        working_dir = get_project_working_dir(project)
        working_dir_source = "project"
    elif config.get("cwd"):
        working_dir = config.get("cwd")
        working_dir_source = "profile config"
    else:
        working_dir = str(settings.effective_workspace_dir)

    # ── The session's sidepanel lane, resolved BEFORE the cwd is final ───────
    #
    # The chat lane repoints the working directory below, and the cwd is already
    # baked into the environment-details prompt block a few dozen lines down —
    # so the lane has to be known here, not at the run-binding block further on.
    # Read off the session row (the same snapshot the binding block reuses);
    # ``session_row`` is only populated inside the session_id branch above, so a
    # cwd-override or project-only spawn re-fetches or resolves to 'code'.
    _binding_row = session_row
    if _binding_row is None and session_id:
        try:
            _binding_row = database.get_session(session_id)
        except Exception:  # noqa: BLE001 — a lookup failure falls back to config
            _binding_row = None
    session_mode = session_mode_of(_binding_row)

    # ── Where this session EXECUTES, for the <env> block below ───────────────
    #
    # ``sessions.placement["where"]`` — ``runner:<id>`` for a pinned session.
    # Without it, a session on a shared runner can start the app inside that
    # runner and point the browser tool at its loopback address. Read
    # off the same row snapshot as the lane above; only an EXPLICIT placement
    # counts (the implicit ``instance_id`` pin is every session, including
    # the local lane, so surfacing it would say "shared runner" everywhere).
    placement_where: Optional[str] = None
    try:
        from app.core.placement import _normalize_placement

        _placement = _normalize_placement((_binding_row or {}).get("placement"))
        _where = str((_placement or {}).get("where") or "").strip()
        placement_where = _where or None
    except Exception:  # noqa: BLE001 — placement is a prompt nicety, never a gate
        placement_where = None

    if session_mode == "chat":
        # A chat session RUNS INSIDE its own uploads folder. This is not
        # cosmetic and it is not about tidiness: it is half of the read fence.
        # The delegated CLI auto-approves any read whose path resolves inside the
        # session's working directory — before the permission-prompt tool, and so
        # before anything the app could say about it. Left on the workspace root
        # (the parent of every project checkout on this machine) that auto-
        # approval would hand a projectless chat every .env and credential file
        # under it. Pointed at the uploads folder, the CLI's own shortcut lands
        # on exactly the files the fence was going to allow anyway, and every
        # other path falls through to the callback that denies it.
        working_dir = str(ensure_chat_uploads_dir())
        working_dir_source = "chat uploads"

    # Fail fast when the resolved cwd doesn't exist: an invalid cwd otherwise
    # surfaces as an opaque OS-level spawn failure (WinError 267 "directory name
    # is invalid") deep inside the adapter's connect. Shared-DB installs can
    # carry project rows whose paths only exist on another machine — name the
    # path so the failure is actionable. The default workspace is ours to create.
    if not Path(working_dir).is_dir():
        if working_dir_source == "workspace":
            Path(working_dir).mkdir(parents=True, exist_ok=True)
        else:
            # No cross-install project rebind here anymore: the origin resolver
            # (resolve_cwd, above) owns cwd for every session and already
            # materializes / self-heals onto THIS runner's checkout, so a session
            # cwd never reaches this point stale. The remaining sources — a
            # project-only spawn, an override, or a profile-config cwd — name a
            # concrete path we do NOT own, so surface it honestly.
            detail = (
                f"project '{project['name']}'"
                if working_dir_source == "project" and project and project.get("name")
                else working_dir_source
            )
            raise ValueError(
                f"Working directory does not exist: {working_dir} (from {detail}). "
                f"The path may belong to a different install or have been moved — "
                f"update or remove the project/profile path, or pick another project."
            )

    # Get AI tools config (used for MCP tool registration + env vars, not system prompt)
    ai_tools_config = config.get("ai_tools")

    # Build system prompt
    system_prompt = config.get("system_prompt")
    override_append = overrides.get("system_prompt_append", "")
    # Set only by the one prompt shape that carries the app's <env> block: the
    # inputs its volatile half needs, re-rendered per turn onto the user message
    # (see generate_turn_environment_note). None on every other shape, so a
    # profile that never had the app env block does not start getting one.
    turn_env: Optional[Dict[str, Any]] = None

    # Get the active security instructions (custom or default)
    security_instructions = get_security_instructions()

    if system_prompt is None:
        # No preset - just security instructions as plain text
        final_system_prompt = security_instructions

        if override_append:
            final_system_prompt += "\n\n" + override_append
    elif isinstance(system_prompt, dict):
        prompt_type = system_prompt.get("type", "preset")

        if prompt_type == "custom":
            # Custom system prompt - use content directly with security instructions
            # Content can be empty/blank
            custom_content = system_prompt.get("content", "") or ""
            inject_env = system_prompt.get("inject_env_details", False)

            # Start building the prompt
            prompt_parts = [security_instructions]

            # Add environment details if enabled (includes AI tools based on ai_tools_config)
            #
            # STABLE HALF ONLY. The date and the branch pair change under a
            # session nobody touched, and everything in this prompt is the
            # provider's cached prefix — one changed character there re-bills
            # system + tools + the whole history on the next turn. They ride
            # the turn's user message instead, rendered per turn by the
            # harness adapter from ``aih_turn_env`` below.
            if inject_env:
                prompt_parts.append(generate_environment_details(
                    working_dir,
                    execution_mode=execution_mode,
                    worktree_info=worktree_info,
                    include_volatile=False,
                    placement_where=placement_where,
                ))
                turn_env = {
                    "execution_mode": execution_mode,
                    "worktree_info": worktree_info,
                }

            # Add custom content if provided
            if custom_content:
                prompt_parts.append(custom_content)

            # Add override append if provided
            if override_append:
                prompt_parts.append(override_append)

            final_system_prompt = "\n\n".join(prompt_parts)
        else:
            # Preset system prompt - pass the preset with append
            existing_append = system_prompt.get("append", "")
            full_append = security_instructions
            if existing_append:
                full_append += "\n\n" + existing_append
            if override_append:
                full_append += "\n\n" + override_append

            final_system_prompt = {
                "type": "preset",
                "preset": system_prompt.get("preset", "claude_code"),
                "append": full_append
            }
    else:
        # String system prompt - use it directly with security instructions
        final_system_prompt = security_instructions + "\n\n" + str(system_prompt)

        if override_append:
            final_system_prompt += "\n\n" + override_append

    # SDK AgentDefinitions are retired: the CLI's native Task/Agent/Workflow
    # tools are banned on the delegated leg (harness_adapter passes
    # --disallowed-tools), so ``agents_dict`` is permanently None.
    #
    # ``enabled_agents``/``agent_invoke_mode`` are a CONTEXT-DECLARATION
    # concern — see
    # app.core.agent_provider_models.available_subagents.
    # The resolved name+description list is stamped onto the options object as
    # ``aih_declared_agents`` further down and projected by the fluid
    # HarnessAdapter onto the compiled prompt's agents index. Dispatch itself is
    # never gated by it.
    agents_dict = None

    # No built-in-subagent prompt instructions: the Task tool's own description
    # tells the model how to dispatch agents, and the agents carry their prompts.

    # Inject app-config system prompt instructions when the picker permits the
    # app_config group and the session is an admin one.
    if session_mode != "chat" and _app_config_permitted(config) and not api_user_id:
        from app.mcp.app_config_sdk import APP_CONFIG_SYSTEM_PROMPT
        if isinstance(final_system_prompt, dict):
            current_append = final_system_prompt.get("append", "")
            final_system_prompt["append"] = current_append + "\n\n" + APP_CONFIG_SYSTEM_PROMPT
        else:
            final_system_prompt += "\n\n" + APP_CONFIG_SYSTEM_PROMPT

    # ── The session's own run binding ───────────────────────────────────────
    #
    # The user picks the model, effort level and permission mode when the
    # session is created and they live on the session row. The row is read
    # ONCE, up with the working directory — the chat lane's cwd is derived from
    # it and had to be settled before the environment-details block rendered
    # the path into the prompt — and every binding below reads that same
    # snapshot. A session-LESS build (tests,
    # project-only spawns) has no row and falls through to the agent's config.
    _session_effort = (_binding_row or {}).get("effort")
    _session_fast = (_binding_row or {}).get("fast")
    _session_permission_mode = (_binding_row or {}).get("permission_mode")
    _context_window_value = overrides.get("context_window") or (
        _binding_row or {}
    ).get("context_window")
    try:
        context_window = int(_context_window_value) if _context_window_value else None
    except (TypeError, ValueError):
        context_window = None
    if context_window is not None and context_window <= 0:
        context_window = None
    # Fast mode. A BOOLEAN, so the usual ``or`` chain would swallow an
    # explicit False: test the override for presence, not truthiness, and only
    # fall back to the session row when the turn said nothing. There is no
    # agent-config fallback — fast mode is a per-session user choice.
    _fast_value = (
        overrides["fast"]
        if overrides.get("fast") is not None
        else _session_fast
    )
    fast = None if _fast_value is None else bool(_fast_value)

    # Determine permission mode. Like the model and effort, this is the
    # session's own binding with the agent's legacy config as the fallback for
    # older sessions; a query-level override still wins for this turn.
    permission_mode = (
        overrides.get("permission_mode")
        or _session_permission_mode
        or config.get("permission_mode")
        or "default"
    )
    # The session's sidepanel mode overrules that chain for chat sessions, which
    # always run on 'default' — see ``permission_mode_for_session_mode``.
    # ``session_mode`` was resolved off ``_binding_row`` with the cwd above:
    # ``session_row`` is None on a cwd-override spawn, and the mode must not
    # depend on which branch happened to fetch the row.
    permission_mode = permission_mode_for_session_mode(permission_mode, session_mode)

    # Stderr callback for AgentOptions.stderr (SDK-shaped; the harness adapter
    # ignores it). Lazy %-formatting avoids f-string overhead when debug is off.
    def stderr_callback(line: str):
        logger.debug("[Claude CLI stderr] %s", line)

    # Process model selection.
    # The preset model values (haiku, sonnet, opus, fable) are Claude Code CLI
    # shorthand aliases; custom model IDs pass through as-is too.
    # Model provider. Resolution:
    # a query-level override wins; otherwise the SESSION's bound provider
    # (sessions.provider_id, stamped at creation from the model the user
    # picked) is authoritative for a real session spawn — editing the agent
    # later never reroutes a live session, and a NULL binding stays native even
    # if the agent now carries a provider. Only session-LESS builds (e.g. tests
    # / project-only spawns) fall back to the agent's config.provider. None =
    # native Anthropic; when set, the env is overlaid with the provider's vars
    # (apply_provider_env).
    if _binding_row is not None:
        provider_id = overrides.get("provider") or _binding_row.get("provider_id")
    else:
        provider_id = overrides.get("provider") or config.get("provider")

    # The session's model. Agents do not pin one: the user picks a
    # model when the session is created and it lands on the row, so the
    # SELECTION is this turn's override or the row's own binding. The legacy
    # chain below only carries older sessions, whose column is NULL —
    # ``launched_model`` (what that session actually last ran on) first, then
    # the agent's old config.model — so upgrading never reroutes an existing
    # conversation.
    _selected_model = overrides.get("model") or (_binding_row or {}).get("model")
    _legacy_model = (_binding_row or {}).get("launched_model") or config.get("model")
    raw_model = _selected_model or _legacy_model
    model_to_use = raw_model

    # ── The composer's model override may REBIND the provider ───────────────
    #
    # The ``or``-chain above can only SET a provider, never clear one, so two
    # selection shapes re-resolve the binding for THIS turn. The selection is
    # durable: the turn intake writes it back to ``sessions.model`` /
    # ``sessions.provider_id`` (``set_session_run_binding``), so the rebind
    # below and the stored row always describe the same leg.
    #
    #   1. a ``prov:<pid>:<mid>`` composite → provider_id = pid (beats the
    #      session binding), model = mid. Validated against the provider row so
    #      an unknown/disabled provider or an uncurated model fails LOUDLY here
    #      (ValueError → a user-visible error frame in both query engines)
    #      instead of shipping garbage upstream and 4xx-ing mid-turn.
    #   2. a plain native-Claude pick (registry alias or ``claude-*``) on a
    #      provider-bound session, where the bound provider does NOT curate that
    #      id → provider_id = None, i.e. back to the native/delegated Claude leg.
    #      The curated-id exemption keeps an Anthropic-API provider row that
    #      serves real ``claude-*`` ids on its own provider (case 3).
    #
    #   3. anything else (a bare model id belonging to the bound provider, a
    #      custom id, no override at all) keeps the binding as resolved above.
    #
    # An explicit ``overrides["provider"]`` (internal callers: Task workers,
    # generation) still pins the provider and is never cleared by a
    # model pick.
    # Only an EXPLICIT selection (this turn's override or the session's own
    # binding) may rebind the provider. The legacy fallbacks above must not:
    # an older provider session whose launched_model happens to read as a
    # native Claude id would otherwise silently drop back to the native leg.
    _override_model = _selected_model
    _decoded_model = decode_provider_model(_override_model)
    _bound_provider_row = None
    if _decoded_model is not None:
        _decoded_pid, _decoded_mid = _decoded_model
        from app.core.provider_agent import validate_provider_model

        _validation = validate_provider_model(
            _decoded_pid, _decoded_mid, require_servable=False
        )
        if not _validation.ok:
            raise ValueError(
                _validation.reason
                or f"Model '{_decoded_mid}' is not available on provider "
                   f"'{_decoded_pid}'."
            )
        provider_id = _decoded_pid
        model_to_use = _decoded_mid
        _bound_provider_row = _validation.provider
    elif _override_model and provider_id and not overrides.get("provider"):
        _bound_provider_row = database.get_model_provider(
            provider_id, include_token=False
        )
        if is_native_claude_model(_override_model):
            from app.core.provider_agent import curated_model_ids

            if _override_model not in curated_model_ids(_bound_provider_row):
                logger.info(
                    "[Provider] Model override %r clears the session's provider "
                    "binding %r for this turn (native Claude pick).",
                    _override_model,
                    provider_id,
                )
                provider_id = None
                _bound_provider_row = None

    # Last-resort floor: a native session with nothing bound anywhere (a fresh
    # row whose first turn carried no selection, an agent with no legacy model)
    # still needs a model. A PROVIDER-bound session deliberately gets no floor —
    # a native alias would be meaningless on a provider leg, and resolving to an
    # arbitrary curated id would silently pick a model the user never chose.
    if not model_to_use and not provider_id:
        from app.core.model_registry import DEFAULT_MODEL

        model_to_use = DEFAULT_MODEL

    # Subagent models are not resolved here: the Task tool resolves the target
    # agent's own binding at dispatch time.

    # True only for a provider-bound session whose MAIN model is NOT a Claude
    # model, read off the session's bound provider row
    # (``provider_type != "anthropic"``). Deliberately a provider-SHAPE signal,
    # not a model-id prefix sniff: a native session (no binding) and an
    # anthropic-type provider row both serve real ``claude-*`` ids and
    # stay False. Consumed by the ``claude-api`` skill exclusion below.
    is_non_claude_provider_session = False
    main_provider_row = None
    if provider_id:
        # Reuse the row the rebind resolution above already fetched (validated
        # composite / binding-clear check) rather than hitting the DB twice.
        main_provider_row = _bound_provider_row
        if main_provider_row is None:
            main_provider_row = database.get_model_provider(
                provider_id, include_token=False
            )
        is_non_claude_provider_session = (
            (main_provider_row or {}).get("provider_type") != "anthropic"
        )

    # Use the same capabilities exposed by the registry to the picker. A stale
    # choice is discarded after switching to a model that cannot support it.
    if context_window not in context_window_choices(model_to_use, main_provider_row):
        context_window = None

    # Model-facing orchestration contract. The same provider-scoped resolver
    # builds Task's schema and enforces dispatch, so prompt prose cannot promise
    # a subagent that the tool will later reject.
    from app.core.agent_provider_models import parent_subagent_guidance
    from app.core.task_dispatch import subagent_system_prompt

    final_system_prompt = _append_system_block(
        final_system_prompt,
        parent_subagent_guidance(),
    )
    final_system_prompt = _append_system_block(
        final_system_prompt,
        subagent_system_prompt(session_id),
    )
    from app.core.assistant_service import build_role_context, assert_proactive_allowed
    from app.core.session_modes import narrow_tool_access_for_proactive
    assistant_proactive = overrides.get("_assistant_proactive")
    if assistant_proactive is not None:
        assert_proactive_allowed(assistant_proactive, session_id)
    final_system_prompt = _append_system_block(
        final_system_prompt, build_role_context(session_id),
    )

    # SDK-shaped tool fields. ``tools`` / ``tools_preset`` feed
    # ``AgentOptions.tools``, which the harness adapter ignores.
    tools_config = config.get("tools")  # Explicit list of tools
    tools_preset = config.get("tools_preset")  # "claude_code" preset

    # ``tool_selection_mode`` decides, not list emptiness. ``access`` carries the
    # SURFACE (``allowed``) and the blocklist (``disallowed``); "Disable all
    # tools" is ``disallowed=["*"]`` on both legs, not an empty allow list.
    access = resolve_tool_access(config)
    # The session's sidepanel mode is a CEILING over whatever the agent allows
    # (see ``app.core.session_modes``): a chat session gets the general web
    # tools plus a ``Read`` fenced to its own uploads directory, and nothing
    # else, whichever agent it happens to be running. The fence on that Read is
    # a path test no allow list can express, so it is enforced where the
    # runtimes actually look — a fluid permission rule, the delegated CLI's
    # working directory and its withheld bare grant — rather than here; the
    # narrowing below is the NAME half only. This
    # sits before every consumer of ``access`` below, so the AI-tools server and
    # the app-config server drop out on their own — both are already filtered
    # through ``tool_permitted(name, access.allowed, access.disallowed)``.
    # (``session_mode`` was resolved with the run binding above, off the same
    # row — the cwd-override spawn has no ``session_row`` but does have a
    # binding row, and both legs must agree on the lane.)
    access = narrow_tool_access_for_mode(access, session_mode)
    access = narrow_tool_access_for_proactive(access, assistant_proactive)
    allowed_tools = list(access.allowed or [])
    disallowed_tools = list(access.disallowed or [])

    # Block the CLI's ``claude-api`` skill on every non-Claude provider session
    # (see NON_CLAUDE_DISALLOWED_SKILL). This sits here — on the shared
    # ``disallowed_tools`` list built from the profile — rather than in the
    # worker branch below, so it covers BOTH a non-Claude main session and
    # a non-Claude worker: a worker is an ordinary session with its
    # own provider binding, so it flows through this same build. Claude sessions
    # (native, or an anthropic-type provider row) are untouched.
    if (
        is_non_claude_provider_session
        and NON_CLAUDE_DISALLOWED_SKILL not in disallowed_tools
    ):
        disallowed_tools = [*disallowed_tools, NON_CLAUDE_DISALLOWED_SKILL]

    sdk_tools = None
    if tools_config:
        # Explicit tool list provided
        sdk_tools = tools_config
    elif tools_preset == "claude_code":
        sdk_tools = {"type": "preset", "preset": "claude_code"}

    # Collect MCP tool names that always need to be in allowed_tools
    # (MCP tools must be explicitly allowed regardless of the tools preset)
    mcp_allowed_tools: list = []

    # ``ai_tools`` and ``app_config_tools`` are not independent switches. The
    # picker decides, through the same access lists every other tool goes
    # through; these two flags stay on the model for import compatibility and
    # are ignored here. The ai-tools server is attached whenever at least one AI
    # tool is permitted, and the app-config server whenever its group is
    # permitted AND the caller is an admin (an API-key session never gets it).
    from app.core.tool_catalog import (
        ai_tool_names_for_config,
        ai_tools_config_from_names,
    )

    ai_tool_names = [
        name
        for name in ai_tool_names_for_config(None, every=True)
        if tool_permitted(name, access.allowed, access.disallowed)
    ] if _ai_tools_available() else []
    if ai_tool_names:
        mcp_allowed_tools.extend(ai_tool_names)
        logger.debug(f"AI MCP tools to allow: {ai_tool_names}")

    from app.mcp.app_config_sdk import get_app_config_tool_names
    app_config_names = [
        name
        for name in get_app_config_tool_names()
        if tool_permitted(name, access.allowed, access.disallowed)
    ]
    app_config_enabled = bool(app_config_names) and not api_user_id
    if app_config_enabled:
        mcp_allowed_tools.extend(app_config_names)
        logger.debug("App-config MCP tools added to allowed_tools")

    # Always expose Claude Code builtin bridge tools (ScheduleWakeup, Monitor,
    # CronCreate, NotifyDevices, ToolSearch, etc). These are
    # harness-level tools that the CLI ships natively; the app provides its own
    # implementations bridged to AI Shuffle infrastructure.
    from app.mcp.builtin_tools import get_builtin_tool_names
    mcp_allowed_tools.extend(
        _drop_orphan_task_managers(
            get_builtin_tool_names(), access.allowed, access.disallowed
        )
    )

    # Workers keep the Task tool (mcp__builtins__Task / fluid's bare Task
    # builtin); nesting is depth-gated in app.core.task_dispatch, so workers may
    # dispatch sub-workers up to settings.task_tool_max_depth.
    if settings.provider_agent_enabled and session_id:
        try:
            from app.core.provider_agent import is_provider_agent_worker
            if is_provider_agent_worker(session_id):
                # A worker must not be able to ask the
                # human anything. Worker tool APPROVALS are redirected to the
                # parent, but ``AskUserQuestion`` is a different interaction
                # kind with its own handler/TTL and is NOT redirected — a
                # worker that asked would simply stall until
                # ``user_question_timeout_seconds`` expired. The tool stays
                # removed for workers at every nesting depth.
                missing = [n for n in WORKER_DISALLOWED_TOOLS if n not in disallowed_tools]
                if missing:
                    disallowed_tools = [*disallowed_tools, *missing]
        except Exception:
            logger.exception("Failed to apply worker tool restrictions")

    # Built-in/managed MCP servers (e.g. the headless browser server) are
    # passed via mcp_servers (loaded through McpService inside
    # _build_mcp_servers_dict). Their tool names must be explicitly allowed —
    # like other MCP tools — or they would be blocked when a tools preset is
    # active (final_allowed_tools = mcp_allowed_tools in that case). We add a
    # server-level wildcard (mcp__<server>) so every tool from each enabled
    # built-in server is permitted, and skip disabled ones so the
    # disabledMcpServers toggle is honoured here too.
    try:
        from app.core.builtin_mcp_servers import get_builtin_servers
        from app.core.mcp_service import get_mcp_service
        # get_disabled_servers() reads the GLOBAL disable list (settings.json ->
        # disabledMcpServers), which is independent of project_dir, so the
        # unscoped service instance is equivalent here. Built-in servers are
        # toggled globally (the project-level toggle routes to this same path).
        _disabled = get_mcp_service().get_disabled_servers()
        for _srv in get_builtin_servers():
            if _srv.name not in _disabled:
                mcp_allowed_tools.append(f"mcp__{_srv.name}")
    except Exception:
        logger.exception("Failed to add built-in MCP server tools to allowed_tools")

    # Computer-use (host desktop control). Only allow-list its tools when the
    # GLOBAL toggle (Settings → Chat & Input, persisted as a system setting)
    # is on AND the host can actually run it (native / non-Docker with a usable
    # display). Off by default; tools never reach the model otherwise. The
    # legacy per-profile ``computer_use.enabled`` config key is no longer
    # consulted — the toggle is app-wide, not agent configuration.
    computer_use_cfg = config.get("computer_use") or {}
    try:
        from app.api.computer_use import get_computer_use_enabled
        computer_use_enabled = get_computer_use_enabled()
    except Exception:
        computer_use_enabled = False
        logger.exception("Failed to read the computer-use global toggle")
    if session_mode == "chat":
        computer_use_enabled = False
    if computer_use_enabled:
        try:
            from app.mcp.computer_use.scaling import computer_use_available
            _cu_ok, _cu_reason = computer_use_available()
            if _cu_ok:
                from app.mcp.computer_use_sdk import get_computer_use_tool_names
                mcp_allowed_tools.extend(get_computer_use_tool_names())
            else:
                computer_use_enabled = False
                logger.info("computer-use requested but unavailable (%s)", _cu_reason)
        except Exception:
            computer_use_enabled = False
            logger.exception("Failed to evaluate computer-use availability")

    from app.core.session_modes import CHAT_MODE_RESOURCE_TOOLS
    private_access_names = [
        name for name in CHAT_MODE_RESOURCE_TOOLS
        if tool_permitted(name, access.allowed, access.disallowed)
    ] if session_id else []
    mcp_allowed_tools.extend(private_access_names)

    # Build final allowed_tools list:
    # - When sdk_tools is set (preset), built-in tools are handled by the preset.
    #   Only MCP tool names need to go in allowed_tools.
    # - When sdk_tools is not set (legacy mode), pass all configured tools + MCP names.
    if sdk_tools:
        final_allowed_tools = mcp_allowed_tools
    else:
        final_allowed_tools = list(allowed_tools) + mcp_allowed_tools

    # Belt and braces for chat mode. Everything appended to ``mcp_allowed_tools``
    # above — the builtin bridge tools, the ``mcp__<server>`` wildcards for every
    # enabled built-in server, the computer-use names — is added UNCONDITIONALLY,
    # without consulting ``access``; and a ``tools_preset`` would hand the leg the
    # whole claude_code toolset while bypassing ``allowed_tools`` entirely. So the
    # ceiling is re-applied to the final list rather than trusted to the narrowing
    # above.
    if session_mode == "chat":
        # ``sdk_tools`` goes first: a preset hands the leg its whole built-in
        # toolset without ever consulting ``allowed_tools``. Dropping it also
        # means the preset branch's "MCP names only" list no longer applies, so
        # the list is rebuilt the legacy way (surface + MCP names) before the
        # ceiling filters it — otherwise a preset agent would land in chat mode
        # with no tools at all, web ones included.
        sdk_tools = None
        final_allowed_tools = [
            t
            for t in (list(allowed_tools) + mcp_allowed_tools)
            if tool_permitted(t, access.allowed, access.disallowed)
        ]
        if not final_allowed_tools and DENY_ALL not in disallowed_tools:
            # An empty allow list reads as "unrestricted" on the runtime legs,
            # so say the denial out loud instead.
            disallowed_tools = [*disallowed_tools, DENY_ALL]

    # Build AI tool environment (API keys + provider settings) ONCE, then pass to both:
    # 1. MCP server creation (so Node.js tool subprocesses get the keys)
    # 2. AgentOptions env (so the Claude CLI subprocess also gets them)
    full_env = _build_env_with_ai_tools(
        config.get("env"),
        _ai_tools_credential_config(ai_tools_config, ai_tool_names),
        api_user_id,
        provider_id,
    )

    if provider_id:
        # Overlay the model-provider env (base URL, bearer token, mitigation
        # flags, small-fast model, optional window/output caps). Raises
        # ProviderConfigError (a ValueError) for a missing/disabled provider or an
        # undecryptable token — the query engines convert it into a user-visible
        # error rather than silently running native Anthropic.
        apply_provider_env(
            full_env,
            provider_id,
            model_to_use,
            session_id=session_id,
        )

    if context_window is not None:
        apply_context_window_override(full_env, context_window)

    ai_tool_env = full_env if full_env else None

    # Build MCP servers dict (pre-compute so we can add app-config server).
    # The ai-tools server rides the PERMITTED set, not ``config.ai_tools``.
    # ``create_ai_tools_server`` still gates on the per-category booleans, so the
    # picker's selection is projected back onto that shape. (The credential ENV
    # above follows the raw config plus this permitted set, so every attached
    # tool can reach its key.)
    ai_tools_server_config = ai_tools_config_from_names(ai_tool_names)
    mcp_servers_dict = _build_mcp_servers_dict_with_ai_tools(
        project_dir=Path(working_dir) if working_dir else None,
        ai_tools_config=ai_tools_server_config,
        tool_env=ai_tool_env,
        session_id=session_id,
    ) or {}

    if private_access_names:
        from app.mcp.chat_access_sdk import create_chat_access_server, GUIDANCE, CHAT_GUIDANCE
        mcp_servers_dict["chat-access"] = create_chat_access_server(session_id)
        guidance = GUIDANCE
        if session_mode == "chat":
            guidance = CHAT_GUIDANCE + guidance
            # Chat must not inherit the repository-development preset.
            if isinstance(final_system_prompt, dict):
                final_system_prompt = final_system_prompt.get("append", "")
        if isinstance(final_system_prompt, dict):
            final_system_prompt = {
                **final_system_prompt,
                "append": f"{final_system_prompt.get('append', '')}\n\n{guidance}",
            }
        else:
            final_system_prompt = f"{final_system_prompt or ''}\n\n{guidance}"

    # Add app-config MCP server for admin sessions whose picker permits it
    if app_config_enabled:
        try:
            from app.mcp.app_config_sdk import create_app_config_server
            app_config_srv = create_app_config_server(
                is_admin=True,
                project_id=project.get("id") if project else None,
                project_dir=working_dir,
            )
            if app_config_srv:
                mcp_servers_dict["app-config"] = app_config_srv
                logger.debug("App-config MCP server registered for admin session")
        except Exception:
            logger.exception("Failed to create app-config MCP server")

    # Register Claude Code builtin bridge tools for every session.
    # Session context (session_id, profile_id, project_id) is baked in so
    # ScheduleWakeup can re-fire prompts back at this session. The effective
    # model is baked in too, so a scheduled wake replays THIS turn's model
    # instead of firing on the profile default (which re-primes the prompt
    # cache on the wrong model).
    try:
        from app.mcp.builtin_tools import create_builtin_tools_server
        builtins_srv = create_builtin_tools_server(
            session_id=session_id,
            profile_id=profile.get("id"),
            project_id=project.get("id") if project else None,
            is_admin=not api_user_id,
            model=model_to_use,
            provider_id=provider_id,
            # The resolved cwd, so the bridged Task tool can discover the same
            # file/plugin agent definitions a native leg gets from fluid — the
            # delegated (CLI) leg has no engine to publish them (ruling CE6).
            cwd=working_dir,
        )
        if builtins_srv:
            mcp_servers_dict["builtins"] = builtins_srv
            logger.debug("Builtin bridge MCP server registered")
    except Exception:
        logger.exception("Failed to create builtin bridge MCP server")

    # The Task tool rides the builtins server above (mcp__builtins__Task) and,
    # on native legs, fluid's bare-named Task builtin. Workers get it too —
    # nesting is depth-gated in app.core.task_dispatch
    # (settings.task_tool_max_depth) instead of denied at registration.

    # Register the computer-use server (host desktop control) when enabled and
    # the host supports it. computer_use_enabled was already validated against
    # computer_use_available() above, so this just builds the server.
    if computer_use_enabled:
        try:
            from app.mcp.computer_use_sdk import create_computer_use_server
            cu_srv = create_computer_use_server(
                enabled=True,
                target_display=computer_use_cfg.get("target_display"),
                virtual_size=(
                    int(computer_use_cfg.get("virtual_width", 1280)),
                    int(computer_use_cfg.get("virtual_height", 800)),
                ),
                permission_mode=permission_mode,
                session_id=session_id,
                profile_id=profile.get("id"),
            )
            if cu_srv:
                mcp_servers_dict["computer-use"] = cu_srv
                logger.info("computer-use MCP server registered for session %s", session_id)
            else:
                computer_use_enabled = False
        except Exception:
            computer_use_enabled = False
            logger.exception("Failed to create computer-use MCP server")

    # When computer-use is active, append operating guidance to the system prompt
    # so the model uses the tools correctly (coordinate space, screenshot-first,
    # shared cursor, UAC limits, Stop-button halts). Handle both string and preset
    # (dict with "append") prompt shapes.
    if computer_use_enabled and "computer-use" in mcp_servers_dict:
        if isinstance(final_system_prompt, dict):
            _existing = final_system_prompt.get("append", "")
            final_system_prompt["append"] = (
                f"{_existing}\n\n{COMPUTER_USE_GUIDANCE}" if _existing else COMPUTER_USE_GUIDANCE
            )
        else:
            _base = str(final_system_prompt) if final_system_prompt else ""
            final_system_prompt = f"{_base}\n\n{COMPUTER_USE_GUIDANCE}" if _base else COMPUTER_USE_GUIDANCE

    if session_mode == "chat":
        # Do not spawn project/user MCP processes just to hide their tools.
        mcp_servers_dict = {name: server for name, server in mcp_servers_dict.items()
                            if name in {"builtins", "chat-access"}}

    # Build options with all AgentOptions fields
    options = AgentOptions(
        # Core settings
        model=model_to_use,
        permission_mode=permission_mode,
        max_turns=overrides.get("max_turns") or config.get("max_turns"),

        # Tool configuration. The harness adapter ignores 'tools'. The delegated
        # leg reads 'allowed_tools' as CLI permission grants (its in-proc MCP entries,
        # via harness_adapter._delegated_permission_grants); native legs read
        # the tool_allowlist stamped below instead. 'disallowed_tools' is the
        # blocklist on both legs.
        tools=sdk_tools,
        allowed_tools=final_allowed_tools,
        disallowed_tools=disallowed_tools,

        # System prompt
        system_prompt=final_system_prompt,

        # Streaming behavior - enable partial messages for real-time streaming
        include_partial_messages=config.get("include_partial_messages", True),

        # Settings loading
        setting_sources=[] if session_mode == "chat" else config.get("setting_sources"),

        # MCP servers - pass all registered MCP servers directly to the adapter
        # This ensures tools from MCP servers are available regardless of
        # whether the CLI discovers them from .mcp.json files
        # Note: ai_tool_env is passed so Node.js tool subprocesses get API keys
        mcp_servers=mcp_servers_dict,

        # Environment and arguments - inject AI tool credentials if enabled
        # Pass api_user_id for policy-based credential resolution
        env=full_env,
        extra_args=_build_extra_args(config.get("extra_args") or {}, hooks, detect_deployment_mode()),

        # User identification
        user=config.get("user"),

        # Token budget (SDK-shaped; the harness adapter ignores it)
        **({"task_budget": config["task_budget"]} if config.get("task_budget") else {}),

        # The harness owns subagent dispatch; SDK-native agent definitions
        # remain disabled on every platform.
        agents=agents_dict,

        # Permission callback - set if callback provided (handles AskUserQuestion in all modes)
        can_use_tool=can_use_tool if can_use_tool else None,

        # Hooks - for interactive tool handling like AskUserQuestion
        hooks=hooks,

        # Stderr callback (SDK-shaped; the harness adapter ignores it)
        stderr=stderr_callback,

        # Plugins - load from enabled_plugins in profile config
        # Each plugin path is resolved relative to workspace_dir if relative
        # Pass setting_sources to avoid duplicating plugins that Claude Code auto-loads
        plugins=[] if session_mode == "chat" else _build_plugins_list(
            config.get("enabled_plugins"),
            config.get("setting_sources")
        ),

        # File checkpointing (SDK-shaped; the harness adapter ignores it)
        enable_file_checkpointing=config.get("enable_file_checkpointing", False),

        # Extended thinking configuration
        # For Opus 4.6+: use effort-based adaptive thinking
        # For Sonnet/Haiku: use token budget-based thinking
        **_build_thinking_config(
            config, model_to_use, overrides, session_effort=_session_effort,
            # A model-bound session owns its effort choice, including an
            # explicit model default (NULL). Do not resurrect a legacy agent's
            # effort after a live reset. Unbound pre-migration sessions retain
            # their old fallback; workers never inherit a saved role default.
            allow_profile_effort=not bool((_binding_row or {}).get("is_worker"))
            and not bool((_binding_row or {}).get("model")),
        ),
    )

    deployment_mode = detect_deployment_mode()
    logger.debug(f"Built options with model={model_to_use}, permission_mode={permission_mode}, can_use_tool={options.can_use_tool is not None}, hooks={hooks is not None}, deployment_mode={deployment_mode}")
    logger.debug(f"Agents passed via SDK: {list(agents_dict.keys()) if agents_dict else 'None'}")
    if deployment_mode == DeploymentMode.LOCAL:
        logger.debug("Windows local mode: agents also written to filesystem for discovery")

    # Apply working directory (already computed above for env details)
    options.cwd = working_dir

    # Forward thinking-display as a raw CLI flag (see _resolve_thinking_display).
    thinking_display = _resolve_thinking_display(config, overrides)
    if thinking_display is not None:
        if options.extra_args is None:
            options.extra_args = {}
        options.extra_args["thinking-display"] = thinking_display

    # Enable any CLI-flag built-in integrations (generic mechanism: a built-in
    # whose McpServerConfig sets ``cli_flag`` is turned on by adding ``--<flag>``
    # to the CLI rather than being spawned as a server). No built-in currently
    # uses this path — the browser built-in is now a spawned STDIO server (added
    # via mcp_servers with a ``mcp__browser`` wildcard above), so this loop
    # no-ops and never sets ``--chrome``. Kept as generic infra for future flags.
    try:
        from app.core.builtin_mcp_servers import get_builtin_servers
        from app.core.mcp_service import get_mcp_service
        _disabled = get_mcp_service().get_disabled_servers()
        for _srv in get_builtin_servers():
            _flag = getattr(_srv, "cli_flag", None)
            if _flag and _srv.name not in _disabled:
                if options.extra_args is None:
                    options.extra_args = {}
                options.extra_args[_flag] = None
    except Exception:
        logger.exception("Failed to apply CLI-flag built-in MCP servers")

    # Additional directories.
    # Notes live in the data dir (e.g. /data/notes in Docker), which is OUTSIDE
    # every possible working directory (workspace / project / worktree all live
    # under the workspace root). The CLI's file tools can only touch the cwd plus
    # add_dirs, so an @-referenced note path like /data/notes/foo.md would be
    # unreachable. Register the notes dir so referenced notes (and their
    # attachments, stored beneath it) resolve from any owner session.
    # Not for an API key's session: the folder is shared (one flat directory of
    # every note, the owner's included) and an add_dir is an auto-approved read
    # root on the delegated leg, so the key's agent could Glob/Read every note
    # the notes API hides from it. A key's own note is still readable through
    # the notes API and the scoped ``@Notes/`` expansion.
    merged_add_dirs: list = list(config.get("add_dirs") or [])
    if session_mode != "chat":
        from app.core.workspace_folders import workspace_directories
        workspace_ids = overrides.get("workspace_project_ids")
        if workspace_ids is None:
            workspace_ids = (_binding_row or {}).get("workspace_project_ids")
        for directory in workspace_directories(workspace_ids, (project or {}).get("id"), api_user_id):
            if directory not in (str(d) for d in merged_add_dirs):
                merged_add_dirs.append(directory)
    try:
        from app.core.note_files import get_notes_dir
        notes_dir = str(get_notes_dir())
        if api_user_id:
            merged_add_dirs = [d for d in merged_add_dirs if str(d) != notes_dir]
        elif notes_dir not in (str(d) for d in merged_add_dirs):
            merged_add_dirs.append(notes_dir)
    except Exception:
        logger.exception("Failed to register notes dir as an accessible directory")
    if session_mode == "chat":
        # Not for the chat lane. ``add_dirs`` widens the set of directories the
        # delegated CLI treats as "the working directory" for its read
        # auto-approval, so every entry here is another folder the read fence
        # would silently stop covering. The lane's whole file surface is its
        # uploads folder — which is already the cwd — and nothing in it can
        # @-reference a note.
        merged_add_dirs = []
    if merged_add_dirs:
        options.add_dirs = merged_add_dirs

    # Resume existing session
    if resume_session_id:
        options.resume = resume_session_id

    # Carry the resolved model-provider binding on the options object. The fluid
    # HarnessAdapter reads it in connect_with_sdk_options and
    # projects it onto SessionOptions.extra["provider_id"], which is how a native
    # (non-delegated) fluid leg gets the provider row's decrypted credentials
    # instead of failing with a missing-credential 401. Always stamped (None for
    # a native Anthropic session) so the attribute's presence is not a signal.
    options.aih_provider_id = provider_id
    # A user-selected input window is fixed when the provider loop connects.
    # The harness adapter uses this for native-loop compaction/usage accounting;
    # delegated Claude also receives the authoritative env pins above.
    options.aih_context_window = context_window
    # Per-session fast mode, same ride-along pattern: the harness adapter
    # reads it off the options object and projects it onto SessionOptions.fast,
    # where the delegated lane turns it into the CLI's ``fastMode`` setting and
    # the OpenAI Responses lane into ``service_tier: priority``. None/False both
    # mean standard; stamped unconditionally so presence is not a signal.
    options.aih_fast = fast
    # ``ai-tools`` owns a persistent Node worker whose FAL_KEY is fixed at
    # spawn. Force a fresh pooled session when the configured credential moves
    # absent → set, rotates, or is removed; the fingerprint contains no secret.
    options.aih_ai_tools_credential_fingerprint = (
        _ai_tools_credential_fingerprint(ai_tool_env)
        if ai_tools_server_config and any(ai_tools_server_config.values())
        else None
    )

    # The bound row's SHAPE, carried alongside the id so the fluid
    # HarnessAdapter can decide the leg from the provider row instead of
    # sniffing the model string: a cross-provider pick like
    # "claude-3.5-sonnet on an OpenRouter row" reads as delegated-Claude to the
    # sniff, which is the wrong leg entirely. Both are None for a native
    # session; the adapter falls back to a lookup (then the sniff) when a
    # provider id arrives without them.
    options.aih_provider_type = (main_provider_row or {}).get("provider_type")
    options.aih_provider_auth_mode = (main_provider_row or {}).get("auth_mode")
    # A custom row's wire format ('anthropic' default | 'openai') — decides
    # which native client serves it. None everywhere else.
    options.aih_provider_dialect = (main_provider_row or {}).get("api_dialect")

    # Same trick for the app's own session id. The fluid HarnessAdapter projects
    # it onto SessionOptions.extra["app_session_id"], which is how a native leg
    # knows which `sessions` row to parent a Task worker onto — the fluid engine
    # only ever knows its own `ses_...` handle, and a brand-new session has no
    # fluid id at connect time at all.
    options.aih_session_id = session_id
    options.aih_profile_id = profile.get("id")
    options.aih_assistant_proactive = assistant_proactive
    # Durable turn provenance must also survive SDK option rebuilding.

    # The volatile half of the app's <env> block — execution mode and the
    # branch pair — carried as INPUTS, not as rendered text. The adapter
    # re-renders the note (with the date) on every send_query, so a pooled
    # client that is reused for twenty turns still tells the model today's
    # date and the branch it is actually on. None when the profile's prompt
    # shape carries no app env block at all.
    options.aih_turn_env = turn_env

    # No per-project memory dir is stamped here and the prompt has no
    # "# Project memory" section: nothing writes one (see
    # docs/MEMORY_ARCHITECTURE.md). Durable facts flow through the continual
    # learner (Remember → journal → synthesizer → the # Memory snapshot).
    # memory_sync still resolves its own dir for the DB mirror and the
    # explicit per-project import.

    # Whether THIS machine can reach GitHub through the gh CLI (installed AND
    # signed in). The adapter projects it onto SessionOptions.extra
    # ["github_connected"], where a native/fluid leg turns it into the prompt's
    # "# GitHub" section — without it the model reconstructs PR/CI state from
    # raw git plumbing or asks the user for something gh would just answer.
    # Cached behind a TTL: the underlying check runs `gh auth status`, and a
    # subprocess per turn is not a price the prompt builder should pay. Inert
    # on the delegated leg, whose CLI already knows its own environment.
    try:
        from app.core.auth import is_github_authenticated_cached

        options.aih_github_connected = is_github_authenticated_cached()
    except Exception:  # noqa: BLE001 — a prompt section is never a blocker
        logger.warning("could not resolve gh connectivity", exc_info=True)
        options.aih_github_connected = False
    # The app turn source (user, unpark, wakeup, ...). The fluid adapter
    # records it on the turn's TurnStarted event; inert elsewhere.
    options.aih_turn_source = overrides.get("turn_source")
    _completion_frames = overrides.get("background_task_completions")
    options.aih_background_task_completions = (
        [dict(frame) for frame in _completion_frames if isinstance(frame, dict)]
        if isinstance(_completion_frames, (list, tuple))
        else []
    )

    # Per-turn TOOL-SURFACE restriction (ruling CE12), set today by Task
    # dispatch when the target is a file/plugin agent whose definition carries a
    # ``tools:`` list. Deliberately NOT ``allowed_tools`` above: that field is a
    # PERMISSION grant list (it holds every MCP tool name so MCP calls do not
    # prompt, and the delegated leg passes its MCP entries as ``--allowed-tools``
    # grants), so reading it as a surface restriction would strip Read/Bash/Edit
    # from every native session. This one means exactly what it says — a
    # non-empty list is the whole tool surface.
    _tool_allowlist = overrides.get("tool_allowlist")
    _surface = (
        [str(t).strip() for t in _tool_allowlist if str(t).strip()]
        if isinstance(_tool_allowlist, (list, tuple))
        else []
    )
    # An all-blank list degrades to None rather than to an empty allowlist: [] on
    # the engine would mean "no tools at all", which is never what a malformed
    # ``tools:`` line asked for.
    _dispatch_surface = _surface or None

    # The AGENT's own ``allow`` list is a surface too, and the two
    # compose — when both exist the session gets the INTERSECTION. This is what
    # makes "Allow only selected" real on the native leg: fluid filters the
    # request schemas, the ToolSearch catalog and dispatch, so an unselected
    # tool is invisible, unloadable and uncallable, instead of merely being
    # denied by ``can_use_tool`` after the model has already seen and called it.

    # The session's sidepanel lane, carried to the adapter. The tool LISTS
    # cannot express the chat lane's read fence — it is a path test — so the
    # adapter needs the lane itself to build the fluid permission rule and to
    # decide what the delegated CLI may be handed as a bare grant. Always
    # stamped (not just for chat) so a pooled client comparing old/new options
    # sees a lane CHANGE rather than an absent field on one side.
    options.aih_session_mode = session_mode

    options.aih_tool_allowlist = intersect_tool_surfaces(
        access.allowed, _dispatch_surface
    )
    if options.aih_tool_allowlist == []:
        # An empty intersection is a real answer ("the agent and the dispatch
        # definition have no tool in common"), but an empty list on the engine
        # degrades to unrestricted. Express it as a total deny instead — deny
        # beats allow on both legs.
        options.aih_tool_allowlist = None
        if DENY_ALL not in (options.disallowed_tools or []):
            options.disallowed_tools = [*(options.disallowed_tools or []), DENY_ALL]

    # DB agents in the caller's context use the exact same provider-scoped
    # roster as Task's schema and runtime admission. File/plugin definitions are
    # added separately by Fluid and run on the caller's provider.
    try:
        from app.core.agent_provider_models import available_subagents

        options.aih_declared_agents = [
            {
                "name": target.name,
                "description": target.description,
            }
            for target in available_subagents(profile.get("id"), provider_id)
        ] or None
    except Exception:  # noqa: BLE001 — context declaration is best-effort
        logger.warning("could not resolve declared agents for context", exc_info=True)
        options.aih_declared_agents = None

    return options, agents_dict
