"""HarnessAdapter — concrete ProviderAdapter backed by the ``fluid`` harness.

This is the app's only provider backend, and the successor to
:class:`ClaudeAgentAdapter`. It wraps a fluid execution engine (``NativeLoop`` for direct-API providers, or
``DelegatedLoop`` for the Claude-subscription ``claude`` CLI) and translates the
fluid ``Event`` stream into the app's provider-agnostic ``ProviderEvent`` types,
so the rest of AI-Shuffle never sees a fluid type.

``factory.make_adapter()`` returns it unconditionally for every spawn path.

Design mirrors ClaudeAgentAdapter: connect/disconnect/health/reconnect lifecycle,
send_query/receive_events for streaming, and interrupt/respond/set_* controls.

Provider-kind decision (from SessionOptions):
  * ``extra["fluid_engine"] == "delegated"``  → DelegatedLoop (claude CLI)
  * ``extra["fluid_provider"]`` / env ``AIH_FLUID_PROVIDER`` in
    {anthropic, openai_responses, openrouter} → NativeLoop with that client
  * otherwise inferred from the model string:
      - "claude*" or any model-registry alias ("fable"/"opus"/"sonnet"/"haiku")
                             → DelegatedLoop (subscription default)
      - "gpt*"/"o1*"/"o3*"/"codex*" → NativeLoop + OpenAIResponsesClient
      - "vendor/model" slug  → NativeLoop + OpenRouterClient
      - else                 → NativeLoop + AnthropicClient (direct API key)

SessionOptions field support
----------------------------
HONORED:
  * ``model``                → engine model (native: request model; delegated: --model)
  * ``system_prompt`` (str)  → engine ``system`` (dict system prompts are stringified)
  * ``cwd``                  → ToolCtx.cwd (native) / DelegatedLoop cwd
  * ``resume_session_id``    → fluid session id (both engines). For the delegated
    engine the CLI's ``--resume`` handle is NOT this id — it is read off the
    stored session meta (``cli_session_id``), which DelegatedLoop writes back
    from the CLI's init frame. See ``_resolve_cli_session_id``.
  * ``session_id``           → explicit fluid session id
  * ``permission_mode``      → fluid PermissionEngine approval policy + plan_mode
  * ``env``                  → delegated child env / native provider (via os.environ).
    On native legs one variable is additionally consumed directly:
    ``CLAUDE_CODE_MAX_OUTPUT_TOKENS`` (positive int) becomes ``NativeLoop
    (max_tokens=...)``, the interim escape hatch from fluid's 8192 default
    until the reasoning/output config wave lands (plan wave W0a).
  * ``max_turns``            → native ``NativeLoop(max_model_calls_per_turn=...)``
    when positive; unlimited otherwise. The delegated leg's CLI enforces its
    own turn budget, so nothing is passed there.
  * ``mcp_servers``          → fluid ``registry.connect_mcp`` (external stdio/http).
    App configs are SDK-shaped (``{"type": "http"|"sse", "url", "headers"}``)
    while fluid's client dispatches on ``transport``, so configs are normalized
    at the seam (``_normalize_mcp_config_for_fluid``): ``http`` →
    ``streamable_http``; stdio passes through; ``sse`` is skipped with a loud
    warning and ``headers`` are dropped with one, both pending upstream fluid
    support (ruling PA5).
  * ``include_partial_messages`` → fluid always streams deltas (native); delegated
    passes ``--include-partial-messages`` (``extra["fluid_partial"]`` overrides)
  * session store: DERIVED, not configured — PgFluidStore when the app has a
    database (always, in real deployments), MemoryStore in DB-less unit tests
  * ``extra["fluid_inproc_servers"]`` → fluid in-process tool servers (see fluid_tool_bridge)
  * ``extra["fluid_registry"]``       → caller-supplied prebuilt ToolRegistry (escape hatch)
  * ``extra["provider_id"]``         → the session's bound ``model_providers`` row;
    its decrypted credentials authenticate the native client (API key, or a
    Codex OAuth auth object for a ChatGPT-subscription row). Threaded from
    ``query_options`` via ``ClaudeAgentOptions.aih_provider_id``. Absent → the
    fluid client's env-var fallback (see ``fluid_credentials``).

  * in-proc SDK servers in ``mcp_servers`` (type="sdk": builtins, app-config,
    ai-tools, computer-use, provider-agent) → auto-bridged onto the fluid
    ToolRegistry with per-tool risk maps (see fluid_tool_bridge.
    register_recorded_sdk_servers; tools recorded by sdk_compat.patch_sdk_server).

  * ``effort``               → delegated: CLI ``--effort``; native: fluid
    serializers map it per provider (Anthropic ``output_config.effort``
    verbatim, OpenAI-shaped ``reasoning.effort`` with xhigh/max clamped to
    high). Validated app-side (``_valid_effort``); changes force reconnect.

  * ``disallowed_tools``     → native ``NativeLoop(disallowed_tools=...)``, which
    filters the tool at the provider schema, the deferred/ToolSearch catalog and
    dispatch (ruling CE12) — the profile blocklist is now a wall, not just a
    permission-callback denial. Delegated: the CLI's ``--disallowed-tools``,
    unioned with the banned-tool set (Task/Agent/wake family, ruling A6).
  * code-mode + the continual harness → native ``NativeLoop(codemode=...)``.
    Both are ON by default for every native leg (the delegated CLI leg caps
    ``code_mode`` off and is not touched). Four app settings drive it, mirroring
    fluid's own ``config/loader.py`` wiring: ``fluid_code_mode`` builds the
    ``SkillStore`` + ``CodeModeSession``, ``fluid_continual`` attaches the
    ``Synthesizer`` that distills journaled cells into saved skills,
    ``fluid_continual_model`` pins synthesis to one model (None = the session's
    model), and ``fluid_skills_dir`` relocates the skill/journal root (None =
    ``<data_dir>/fluid/skills``, so nothing lands in fluid's ``~/.fluid``
    default). Construction is best-effort: any failure degrades to
    ``codemode=None`` rather than failing the turn. ``NativeLoop.aclose()``
    already closes the session, so there is no teardown here. When the session
    really came up (fail-open, so never on the setting alone) the compiled
    prompt gains a trailing ``# Code mode`` section and its skills/agents
    headers are rewritten to say that Skill and Task are imported inside
    run_python — fluid itself puts nothing about code mode in the prompt.
  * ``tool_allowlist``       → native ``NativeLoop(allowed_tools=...)``: the
    session's ENTIRE tool surface when set, None = unrestricted (CE12 + T3).
    ``query_options`` composes two sources into it — the AGENT's own ``allow``
    list (ruling T2/T3) and, for a worker dispatched onto a file/plugin agent
    whose definition carries a ``tools:`` list, that definition's list
    (``app.core.task_dispatch`` → ``overrides["tool_allowlist"]``). When both
    exist the surface is their INTERSECTION; an empty intersection becomes a
    ``["*"]`` deny instead of an empty list, which would mean "unrestricted".
    Both reach here as ``ClaudeAgentOptions.aih_tool_allowlist``. On the
    delegated leg it rides
    ``--allowed-tools``; because the claude CLI reads that flag as PERMISSION
    rules, this leg ALSO passes the complement as ``--disallowed-tools`` so the
    restriction is real there too (ruling T4 — see ``_delegated_disallowed``).

DEGRADED / NOT YET HONORED (documented gaps — mechanical follow-up):
  * ``thinking`` — fluid manages thinking via provider caps, not passed through.
  * ``hooks`` / ``include_hook_events`` — fluid has its own HookBus; app SDK hooks not wired.
  * ``extra_args`` — SDK/CLI-specific, ignored. (``setting_sources`` is now
    HONORED on native legs: it maps onto fluid's own scope filter — see
    ``_fluid_setting_sources`` — gating instruction files, skills, agents, and
    plugin discovery; None keeps every scope on, the historical behavior.)
  * ``task_budget`` / ``session_store`` / ``session_store_flush`` — SDK session-store
    concepts; fluid persistence is the Pg/Memory store chosen above.
  * ``enable_file_checkpointing`` / ``stderr`` / ``max_buffer_size`` / ``user`` — ignored.
  * MCP ``headers`` (remote servers) and the ``sse`` transport — fluid's MCP
    client supports neither yet; both are warned about at connect time.

HONORED SINCE THE P1 COMPILER LANDED (2026-08-13):
  * ``add_dirs`` — projected onto SessionOptions and passed to the delegated
    leg as repeated ``--add-dir`` flags (native-leg fluid tools are not
    path-sandboxed, so no equivalent is needed there).
  * system prompts on native legs (and the delegated leg under
    PROMPT_FULL_REPLACE) run through ``fluid.context.compiler`` — instruction
    files, skills index, and environment are fluid-gathered (ruling A1).

SUPPLIED BY FLUID ITSELF, WITH NO WIRING HERE (Claude-ecosystem compat, CE1):
  fluid's discovery is filesystem-native and defaults ON, so a native leg picks
  the user's existing Claude Code setup up for free. Nothing below is passed by
  this adapter — the only lever it would have is turning a piece OFF:
  * ``plugins`` — enabled plugins (``enabledPlugins`` in the settings stack) are
    resolved from ``~/.fluid/plugins``; their skills and commands join the
    compiled prompt namespaced ``<plugin>:<name>``, their agents join the agent
    registry, and their MCP servers join ``NativeLoop``'s lazy-connect set
    (``fluid.context.plugins``, ruling CE5). App-configured servers of the same
    name always win — see ``_prune_duplicate_plugin_mcp_servers``.
  * ``settings.json`` — the user/project/local stack is merged and applied:
    ``permissions.{allow,ask,deny,defaultMode}`` become PermissionEngine rules
    placed BEFORE the app's (last match wins, so the app keeps the final say),
    and ``env`` becomes a child-process overlay for the Bash builtin and stdio
    MCP spawns — never an ``os.environ`` mutation (``fluid.context.
    claude_settings``, ruling CE4).
  * instruction files — the ``AGENTS.md`` conventions (``AGENTS.md`` /
    ``AGENT.md`` / ``.agents/AGENTS.md`` per scope, ``AGENTS.override.md``
    replacing the root slot, ``~/.fluid/AGENTS.md`` globally) are collected
    with ``@path`` imports expanded by the instruction gatherer (rulings
    CE2/CE3) — so the compiled prompt this adapter builds already contains
    them. ``CLAUDE.md``/``CLAUDE.local.md`` are no longer read directly; an
    explicit settings import copies them over instead.
  * ``agents`` (subagents) — ``.claude/agents/**/*.md`` plus plugin agents are
    read into ``ctx.extra["agent_registry"]`` by ``NativeLoop`` (ruling CE6).
    The prompt's agents index is assembled by THIS module instead of fluid's
    compiler (``_agent_context``), because the app's own DB agents belong in the
    same list: the profile's context-declared agents (2026-08-17 ruling —
    ``enabled_agents``/``agent_invoke_mode`` decide what is DECLARED, never what
    may be dispatched) are merged with the discovered ones, name+description
    only. BOTH legs get the block: native legs through the compiled prompt, the
    delegated leg through whichever prompt part the CLI reads (the compiled
    prompt under PROMPT_FULL_REPLACE, the appended text otherwise — see
    ``_with_agents_index``), so a Claude-subscription caller can name a
    cross-provider agent too. Dispatch stays ours: the Task dispatcher injected
    below resolves app agents first and falls back to that registry
    (``app.core.task_dispatch``), with no allowlist between it and any agent
    that resolves.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, AsyncIterator, Dict, List, Optional, Tuple, cast

import httpx

from app.core.model_registry import (
    DEFAULT_MODEL,
    get_context_window,
    get_model,
    resolve_api_model_id,
    resolve_provider_context_window,
)
from app.core.provider.fluid_credentials import (
    ProviderCredentialError,
    resolve_provider_credentials,
)
from app.core.provider.protocol import ProviderAdapter
# Registers the app-owned SafetyWarning shape with fluid's event codec. The
# translation branch below matches on the class NAME, so it needs no symbol
# from here — but a session replayed through this adapter must be decodable,
# and only this import teaches the codec that class.
from app.core.provider import legacy_events as _legacy_events  # noqa: F401
from app.core.provider.types import (
    PermissionRequestEvent,
    ProviderEvent,
    SessionOptions,
    mcp_servers_equivalent,
)
# The streaming loops dispatch on the provider mirror vocabulary (identical
# names/shapes to the SDK message classes — see events.py). _translate
# synthesizes these from the fluid Event stream so a flag-on session flows
# through the exact same loop branches as the SDK path.
from app.core.provider.events import (
    AssistantMessage,
    RateLimitEvent,
    RateLimitInfo,
    ResultMessage,
    SteerDeliveredEvent,
    StreamEvent,
    SystemMessage,
    TaskNotificationMessage,
    TaskProgressMessage,
    TaskStartedMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

# fluid imports (the library we wrap). Its top-level __init__ is intentionally
# empty, so we import from submodules directly.
from fluid.codemode.continual import Synthesizer
from fluid.codemode.session import CodeModeSession
from fluid.codemode.skills import SkillStore
from fluid.codemode.subprocess_kernel import KernelConfig
from fluid.context.compaction import CompactionConfig
from fluid.context.trim import TrimConfig
from fluid.engine.native import NativeLoop
from fluid.engine.delegated import DelegatedLoop
from fluid.events.ops import (
    BackgroundTaskFinished,
    Interrupt,
    RespondPermission,
    RespondQuestion,
    Resume,
    SetModel,
    SetPermissionMode,
    Steer,
    UserTurn,
)
from fluid.permissions.engine import PermissionEngine
from fluid.providers.anthropic.client import AnthropicClient
from fluid.providers.openai_responses.client import (
    API_URL as OPENAI_RESPONSES_URL,
    CODEX_RESPONSES_URL,
    OpenAIResponsesClient,
)
from fluid.providers.openrouter.client import OpenRouterClient
from fluid.providers._retry import RetryPolicy
from fluid.store.base import MemoryStore
from fluid.tools.registry import ToolCtx, ToolRegistry

if TYPE_CHECKING:  # pragma: no cover — annotation-only; the DB import below is
    # deliberately lazy (inside ``_lookup_provider_row``), and this module has
    # ``from __future__ import annotations``, so naming the row type is free.
    from app.db.model_providers import ModelProviderRow

logger = logging.getLogger(__name__)

_ENGINE_CLOSE_GRACE_SECONDS = 5.0


def _extra_opt(options: SessionOptions, key: str, default: Any = None) -> Any:
    """Read ``extra["fluid_<key>"]``, falling back to the legacy ``omni_<key>``.

    The harness was renamed omni -> fluid (2026-08); pre-rename callers and
    scripts that still pass the old key keep working.
    """
    val = options.extra.get(f"fluid_{key}")
    if val is None:
        val = options.extra.get(f"omni_{key}", default)
    return val

# app permission_mode → fluid PermissionEngine approval policy
_PERMISSION_POLICY = {
    "bypassPermissions": "never_ask",
    "acceptEdits": "on_write",
    "default": "on_write",
    "plan": "always_ask",
}


class HarnessAdapter(ProviderAdapter):
    """ProviderAdapter implementation backed by the fluid harness."""

    def __init__(self) -> None:
        self._engine: Optional[Any] = None  # NativeLoop | DelegatedLoop
        self._store: Optional[Any] = None
        self._registry: Optional[ToolRegistry] = None
        self._connected: bool = False
        self._options: Optional[SessionOptions] = None
        self._sdk_session_id: Optional[str] = None
        # receive_events replay cursor: fluid's EventBus replays from ``since_seq``,
        # so we advance this per delivered event to avoid re-yielding prior turns.
        self._event_cursor: int = 0
        # app tool_use_id -> fluid permission request_id (respond_to_permission map)
        self._perm_requests: Dict[str, str] = {}
        # last pending fluid QuestionRequest id (AskUser / StopPlanning).
        self._pending_question_id: Optional[str] = None
        # Mode to restore when StopPlanning is approved. Set when PlanModeControl
        # first enters plan mode (or, if the session connected already in plan,
        # default).
        self._pre_plan_permission_mode: str = "default"
        # SystemMessage(status, permissionMode=...) queued by PlanModeControl so
        # receive_events can yield them on the next engine tick.
        self._pending_plan_mode_events: list = []
        self._is_delegated: bool = False
        self._provider_kind: Optional[str] = None
        # ─ translation state (see _translate) ─
        # Model reported by the current turn's TurnStarted (AssistantMessage.model).
        self._current_model: Optional[str] = None
        # Open streaming blocks: index -> block_type, and accumulated delta text.
        self._block_types: Dict[int, str] = {}
        self._block_buffers: Dict[int, list] = {}
        # App-side permission callback (ClaudeAgentOptions.can_use_tool). When
        # present, fluid PermissionRequests are bridged to it out-of-band (SDK
        # parity — the loops never see a permission event); when absent they
        # surface in-stream as PermissionRequestEvent.
        self._can_use_tool: Optional[Any] = None
        self._perm_bridge_tasks: set = set()
        # httpx client injected into the native provider client purely to
        # capture upstream usage/quota headers (see _usage_capture_http_client).
        # The fluid client does not own it, so disconnect() closes it.
        self._provider_http: Optional[httpx.AsyncClient] = None

    # ─── Transition helper ────────────────────────────────────────────

    @property
    def engine(self) -> Optional[Any]:
        """Direct access to the underlying fluid engine (parallels sdk_client)."""
        return self._engine

    # ─── Connection Lifecycle ─────────────────────────────────────────

    async def connect(self, options: SessionOptions) -> None:
        """Build the fluid engine from SessionOptions and prime its session."""
        self._options = options
        # Kept in a local as well as on ``self``: the engine constructors below
        # take a non-optional store, and the local is what carries that
        # non-optional fact to a reader (``self._store`` stays Optional because
        # it is None before connect and after disconnect).
        store = self._build_store(options)
        self._store = store

        # Decide the leg BEFORE building the registry: the native legs need
        # fluid's builtin file/shell tools registered, the delegated leg must not
        # get them (the claude CLI ships its own).
        # Off the loop: a bound provider id without its shape fields makes this
        # read the model_providers row synchronously.
        kind = await asyncio.to_thread(_decide_provider_kind, options)
        self._is_delegated = kind == "delegated"
        self._provider_kind = kind

        self._registry = await self._build_registry(options, native=not self._is_delegated)

        fluid_session_id = options.session_id or options.resume_session_id
        system, append_system = _system_parts(options.system_prompt)
        permissions, plan_mode = self._build_permissions(options)

        if self._is_delegated:
            cli_session_id = await self._resolve_cli_session_id(fluid_session_id, options)
            # Staged flip (ruling A3): append mode (default) keeps the CLI's
            # own preset + env/instruction gathering and passes our text
            # through untouched; full-replace hands the CLI the fluid-compiled
            # prompt instead. Compiling env/instructions in append mode would
            # duplicate what the CLI gathers itself, so the compiler is only
            # invoked under full-replace.
            from app.core.config import settings

            if settings.prompt_full_replace:
                # The compiled prompt already carries the merged agents index
                # (``_compile_prompt_sections`` slots it in), so nothing is
                # added here — that is what keeps the block un-doubled.
                delegated_system = await asyncio.to_thread(
                    self._compile_prompt, system, append_system, options
                )
                delegated_append = None
            else:
                # Append mode still owes the model the delegation catalog. The
                # CLI builds its own prompt here, so without this a
                # Claude-subscription caller would be the ONE leg that never
                # learns which agents exist — it could not name a target for
                # the Task tool at all. Same block as the native leg, attached
                # to whichever part the CLI actually reads.
                # discover_agents walks .claude/agents + plugin dirs (rglob)
                # — disk IO, so it runs in a worker thread.
                agents_index, _ = await asyncio.to_thread(_agent_context, options)
                delegated_system, delegated_append = _with_agents_index(
                    system, append_system, agents_index
                )
            # shutil.which + the bundled-CLI resolver stat the filesystem.
            claude_cmd = await asyncio.to_thread(_resolve_claude_cmd)
            self._engine = DelegatedLoop(
                store=store,
                model=options.model,
                system=delegated_system,
                append_system_prompt=delegated_append,
                permission_mode=options.permission_mode,
                registry=self._registry,
                ctx=ToolCtx(cwd=options.cwd or ".", session_id=fluid_session_id),
                session_id=fluid_session_id,
                cli_session_id=cli_session_id,
                # Honor the SDK-level streaming option (the app turns it on by
                # default). Without it the delegated CLI never gets
                # --include-partial-messages and the UI only sees whole messages
                # at turn end. extra["fluid_partial"] stays a manual override.
                partial=bool(
                    getattr(options, "include_partial_messages", False)
                    or _extra_opt(options, "partial", False)
                ),
                # ONLY external transports. The raw map also holds type='sdk'
                # entries wrapping live in-proc Server instances, and the
                # delegated leg JSON-serializes this map into its --mcp-config
                # file -> TypeError("Object of type Server is not JSON
                # serializable") and a failed spawn.
                mcp_servers=_external_mcp_servers(options.mcp_servers),
                cwd=options.cwd,
                env=options.env,
                add_dirs=list(options.add_dirs) if options.add_dirs else None,
                # Reasoning depth (profile config or chat override) → CLI
                # --effort. Validated app-side to the CLI's vocabulary.
                effort=_valid_effort(getattr(options, "effort", None)),
                # Per-session fast mode (v81) → the CLI's ``fastMode`` setting,
                # handed over as a --settings JSON blob. Only sent when the user
                # turned it on: an explicit ``{"fastMode": false}`` would be the
                # same as the default, and a missing --settings keeps the CLI on
                # whatever the account's own settings say.
                settings=(
                    json.dumps({"fastMode": True})
                    if getattr(options, "fast", None)
                    else None
                ),
                # Ruling A6's ban set, the agent's own blocklist, and (ruling
                # T4) the CLI-surface complement of an ``allow``-mode surface.
                # The bridged app builtin mcp__builtins__ScheduleWakeup
                # (wakeup_service-backed) is the one supported wake path on this
                # leg. See _delegated_disallowed for the full derivation.
                disallowed_tools=_delegated_disallowed(options),
                # The session's tool surface: the agent's ``allow`` list (T3)
                # intersected with a file-agent worker's definition list (CE12).
                # The CLI reads --allowed-tools as PERMISSION rules, so on this
                # leg the list grants rather than restricts — the restriction
                # comes from the complement passed as --disallowed-tools above.
                allowed_tools=_tool_allowlist(options),
                claude_cmd=claude_cmd,
            )
        else:
            provider_client, provider_http = await _build_provider_client(kind, options)
            self._provider_http = provider_http
            # fluid's bare Task builtin needs the app's dispatcher injected
            # (ruling A13: fluid owns the shape, the app owns the spawning).
            # UNCONDITIONAL + late-binding: this ctx is built once per connect
            # and then lives for the whole pooled life of the engine, while the
            # app session id may not be knowable here at all (a brand-new
            # native session has neither options.session_id nor
            # resume_session_id). Gating the injection on a connect-time id is
            # what made Task report itself "not available in this environment"
            # for every fresh native session. The supplier resolves the id at
            # dispatch time instead — see _resolve_app_session_id.
            native_ctx = ToolCtx(cwd=options.cwd or ".", session_id=fluid_session_id)
            try:
                from app.core.task_dispatch import make_fluid_dispatcher

                native_ctx.extra["task_dispatch"] = make_fluid_dispatcher(
                    self._resolve_app_session_id,
                    profile_id=(options.extra or {}).get("profile_id"),
                    provider_id=(options.extra or {}).get("provider_id"),
                )
            except Exception:  # noqa: BLE001 — Task degrades to "unavailable"
                logger.warning(
                    "[HarnessAdapter] task_dispatch injection FAILED (fluid "
                    "session=%s, cwd=%s) — the Task tool will report itself "
                    "unavailable on this native leg",
                    fluid_session_id, options.cwd, exc_info=True,
                )
            # Same A13 split for fluid's bare Monitor builtin: shape in fluid,
            # spawning here. The spawner reuses the app's detached-shell task
            # manager and its unpark wake path.
            try:
                from app.core.monitor_spawn import make_fluid_monitor_spawner

                native_ctx.extra["monitor_spawn"] = make_fluid_monitor_spawner(
                    self._resolve_app_session_id
                )
            except Exception:  # noqa: BLE001 — Monitor degrades to "unavailable"
                logger.warning(
                    "[HarnessAdapter] monitor_spawn injection FAILED (fluid "
                    "session=%s, cwd=%s) — the Monitor tool will report itself "
                    "unavailable on this native leg",
                    fluid_session_id, options.cwd, exc_info=True,
                )
            # Cross-session recall, wired exactly the way fluid's own
            # config/loader.py does it: the store's optional hooks back the
            # recall builtins (PgFluidStore sweeps fluid_session_nodes +
            # session_store_entries and reads titles off the sessions table).
            # search()/recent_sessions() serve SessionSearch (keyword search /
            # the no-query recent listing), session_slice() serves SessionView.
            # MemoryStore — the DB-less unit-test store — offers none, and the
            # tools' requires_extra gates then keep them invisible on every
            # surface. No try/except: getattr on a store object cannot throw.
            if callable(getattr(store, "search", None)):
                native_ctx.extra["session_search"] = store.search
            if callable(getattr(store, "recent_sessions", None)):
                native_ctx.extra["session_recent"] = store.recent_sessions
            if callable(getattr(store, "session_slice", None)):
                native_ctx.extra["session_view"] = store.session_slice
            # Only explicit user configuration may limit a native turn. There
            # is deliberately no install-wide model/token/cost/loop ceiling and
            # no automatic context trimming: provider usage remains telemetry,
            # never a reason for AI Shuffle to stop or rewrite a turn. Both are
            # now passed EXPLICITLY — fluid's own defaults are a 200-call
            # ceiling and compaction ON, so omitting either knob would quietly
            # reintroduce exactly what this paragraph rules out.
            native_caps: Dict[str, Any] = {}
            requested_calls = _positive_int(getattr(options, "max_turns", None))
            native_caps["max_model_calls_per_turn"] = requested_calls
            max_tokens = _native_max_output_tokens(options)
            if max_tokens is not None:
                native_caps["max_tokens"] = max_tokens
            # fluid's Anthropic client sends the model string verbatim to the
            # Messages API — a bare registry alias ("opus") 404s there and never
            # matches the pricing table. The other native kinds run provider-native
            # ids that must pass through untouched.
            native_model = (
                resolve_api_model_id(options.model) if kind == "anthropic" else options.model
            )
            # The delegation catalog (CE6 + the app's own DB agents) — one
            # name+description index for the prompt, plus the matching
            # descriptors for the "Custom agents" usage row.
            _, declared_agent_descriptors = await asyncio.to_thread(_agent_context, options)
            # Code-mode + the continual harness, mirroring fluid's own
            # config/loader.py wiring. ON by default for every native leg; the
            # skill store and its episodic journal live under the app's data
            # dir rather than fluid's ~/.fluid default. Best-effort throughout:
            # a broken skills dir or an unavailable kernel must never take down
            # a turn, so any failure degrades to plain JSON tool calling.
            # NativeLoop.aclose() closes the session, so there is no teardown.
            codemode = None
            try:
                from app.core.config import settings

                if settings.fluid_code_mode and self._registry is not None:
                    skills = SkillStore(_fluid_skills_root())
                    try:
                        # Opportunistic lifecycle pass, mirroring fluid's own
                        # loader (interval-gated, deterministic, archive-not-
                        # delete — see SkillStore.curate). Best-effort: curation
                        # must never block an engine build.
                        skills.curate()
                    except Exception:  # noqa: BLE001
                        pass
                    synthesizer = None
                    if settings.fluid_continual:
                        # Session provider; fluid_continual_model pins synthesis
                        # to one model so skill quality doesn't drift with the
                        # session's provider choice. Passes are background and
                        # best-effort (see fluid/codemode/continual.py).
                        synthesizer = Synthesizer(
                            provider_client,
                            settings.fluid_continual_model or cast(str, native_model),
                            skills,
                        )
                    codemode = CodeModeSession(
                        self._registry, config=_codemode_kernel_config(),
                        skills=skills, synthesizer=synthesizer,
                    )
            except Exception:  # noqa: BLE001 — degrade to JSON tool calling
                logger.warning(
                    "[HarnessAdapter] code-mode/continual setup FAILED (fluid "
                    "session=%s) — this native leg runs plain JSON tool calls",
                    fluid_session_id, exc_info=True,
                )
                codemode = None
            # Compiled ONCE, used twice below: the joined text is the prompt
            # the engine sends; the labeled sections feed get_context_usage().
            # Compiled AFTER the block above on purpose — the code-mode prompt
            # rewrites key on whether the session actually came up, never on
            # the setting: the build is fail-open, and a prompt that told the
            # model to import tools inside run_python when run_python is not in
            # its schema would be worse than saying nothing.
            # The compiler gathers instruction files, the skills index and the
            # git-backed environment block (subprocess + rglob) and runs the
            # agent discovery walk again — all synchronous disk/process IO,
            # so it runs off the loop.
            compiled_prompt = await asyncio.to_thread(
                self._compile_prompt_sections,
                system, append_system, options, code_mode=codemode is not None,
                codemode=codemode,
                # The session's real tool registry — the name set the T9 prompt
                # filter is computed from (MCP bridge names included).
                registry=self._registry,
            )
            self._engine = NativeLoop(
                provider=provider_client,
                store=store,
                # fluid's NativeLoop annotates ``model`` as ``str`` while
                # SessionOptions carries Optional[str]. The value is handed over
                # exactly as before — the cast bridges the annotations only and
                # deliberately does NOT substitute a default for None.
                model=cast(str, native_model),
                # Reasoning depth: fluid's serializers map this per provider
                # (Anthropic output_config.effort; OpenAI-shaped reasoning.effort).
                effort=_valid_effort(getattr(options, "effort", None)),
                # The model's INPUT window (distinct from native_caps'
                # max_tokens, which caps OUTPUT). Drives the maxTokens /
                # percentage / "Free space" fields of the leg's
                # get_context_usage() breakdown; None when unresolvable, which
                # fluid reports as an unknown window instead of guessing.
                context_window=_native_context_window(kind, options),
                # Per-session fast mode (v81). Only the OpenAI Responses lane
                # has a wire-level expedited service in v1 — ``service_tier:
                # priority``, merged into the request body by fluid's
                # ``request_extra``. The anthropic and openrouter native lanes
                # have no equivalent knob and ignore ``fast`` entirely.
                request_extra=(
                    {"service_tier": "priority"}
                    if kind == "openai_responses" and getattr(options, "fast", None)
                    else None
                ),
                **native_caps,
                # Context rewriting OFF, explicitly (see the ceiling note
                # above). fluid's default is compaction ON; AI Shuffle never
                # rewrites or digests a turn's history behind the user's back,
                # so both tiers are disabled at construction. ``fluid_trim``
                # (FLUID_TRIM, default off) turns tier-1 back on: that exact
                # spelling — compaction disabled, trim enabled — drops
                # superseded tool RESULTS and never writes a span digest, so
                # nothing the user or the agent said is ever rewritten.
                compaction=_compaction_config(),
                # Native legs have no Claude Code preset — they always run the
                # fluid-compiled prompt: the profile's configured prompt as the
                # base (a preset-shaped profile has none) with the append text
                # (security + profile instructions) riding as an extra block,
                # plus fluid's own instruction-file/skills/env gathering.
                system=(
                    compiled_prompt.text
                    if compiled_prompt is not None
                    else (system if system is not None else append_system)
                ),
                # The same compilation's labeled sections — what lets fluid's
                # get_context_usage() price Memory files / Skills as their own
                # category rows (with per-file memoryFiles detail) instead of
                # folding everything into "System prompt". None (compiler
                # failure) degrades to the opaque-blob pricing, matching the
                # string fallback above.
                system_sections=compiled_prompt,
                # Agent-definition descriptors behind get_context_usage()'s
                # "Custom agents" row: the profile's context-declared DB agents
                # (name+description, ruling of 2026-08-17) plus the file/plugin
                # definitions fluid discovered — passing this list at all
                # suppresses fluid's own auto-fill, so it has to carry both.
                agents=declared_agent_descriptors or None,
                # Code-mode session built above (None when disabled or when its
                # construction failed) — NativeLoop registers the four code-mode
                # tools onto the registry and swaps the model's tool schema for
                # them when this is present.
                codemode=codemode,
                registry=self._registry,
                permissions=permissions,
                ctx=native_ctx,
                plan_mode=plan_mode,
                session_id=fluid_session_id,
                # Same scope filter the prompt compiled under, so fluid's
                # native-agent discovery (the Task registry) sees the same
                # sources the agents index advertised.
                setting_sources=_fluid_setting_sources(options),
                # Tool restriction, ENFORCED (ruling CE12): fluid filters the
                # request schemas, the deferred/ToolSearch catalog AND dispatch,
                # so a filtered tool is invisible, unloadable and uncallable.
                # ``allowed_tools`` is the whole surface when set (None = every
                # registered tool); ``disallowed_tools`` — the profile's own
                # blocklist, previously honored only by the app's permission
                # callback — subtracts afterwards, deny beating allow. Both
                # accept fnmatch patterns (``mcp__chrome-devtools__*``).
                allowed_tools=_tool_allowlist(options),
                disallowed_tools=list(options.disallowed_tools or ()) or None,
                # fluid's folding of the host's ~/.claude settings stack is left
                # ON (its default) deliberately, per ruling CE4 — see the
                # settings.json bullet in the module docstring: the user's
                # permission rules are prepended (last match wins, so the app's
                # own rules keep the final say) and ``env`` rides as a
                # child-process overlay, never an os.environ mutation.
            )
            # Ruling T9 again, on the pricing side: when the compiled prompt
            # carries no agents index (``Task`` not permitted, or a compiler
            # failure that fell back to the raw prompt parts), the "Custom
            # agents" usage row must price ZERO — there is no section to pay
            # for. This cannot be expressed by passing ``agents=[]`` above:
            # NativeLoop's constructor treats an empty list and None
            # identically ("auto-fill from disk discovery"), so the priced list
            # is cleared AFTER construction instead — get_context_usage() reads
            # ``self.agents`` at call time. Dispatch is untouched: the agent
            # registry rides ``ctx.extra["agent_registry"]``, not this list.
            prompt_has_agents_index = compiled_prompt is not None and any(
                section.kind == "agents" for section in compiled_prompt.sections
            )
            if not prompt_has_agents_index:
                self._engine.agents = []
            # An enabled plugin may declare an MCP server under a name the app
            # also configures. fluid's own host-wins check only sees servers
            # passed as ``NativeLoop(mcp_servers=...)``, and the app connects
            # its servers itself (``_build_registry`` → ``registry.connect_mcp``,
            # already done above) — so without this the same name would be
            # connected twice onto the same registry: two child processes, the
            # second overwriting the first's catalog entries and orphaning it.
            _prune_duplicate_plugin_mcp_servers(
                self._engine, _external_mcp_servers(options.mcp_servers)
            )
            self._wire_plan_mode_control(options)

        # Prime the session so ``session_id`` and ``events()`` are usable
        # immediately (both engines lazily create it inside the first turn task,
        # which would otherwise race receive_events()).
        ensure = getattr(self._engine, "_ensure_session", None)
        if ensure is not None:
            meta = await ensure()
            self._sdk_session_id = meta.session_id
            # A brand-new native session has NO id when the ToolCtx above is
            # built (neither options.session_id nor resume_session_id exists
            # yet), so every session-scoped tool ran against ctx.session_id =
            # None — the Note tool wrote into a shared ``default.md`` while the
            # notes reminder read ``<session>.md``, and concurrent sessions on
            # one project overwrote each other's notes. The real id exists now:
            # write it back onto the ctx the engine dispatches with. Only when
            # unset — a resumed session's ctx already carries the right id, and
            # the delegated leg has no ``ctx`` at all.
            engine_ctx = getattr(self._engine, "ctx", None)
            if engine_ctx is not None and not getattr(engine_ctx, "session_id", None):
                try:
                    engine_ctx.session_id = meta.session_id
                except Exception:  # noqa: BLE001 — never fail a connect over this
                    logger.debug(
                        "[HarnessAdapter] could not stamp session_id onto ToolCtx",
                        exc_info=True,
                    )
            await self._seed_event_log(meta.session_id)

        self._connected = True

    def _resolve_app_session_id(self) -> Optional[str]:
        """The app's ``sessions.id`` for this adapter, resolved lazily.

        Task dispatch parents its worker onto an app session row, so it needs
        the app id — NOT the fluid session id (``ses_...``) the engine and
        ``ToolCtx`` carry. Two sources, in order:

        1. ``extra["app_session_id"]`` — stamped by ``query_options`` onto the
           options object (``aih_session_id``) and projected in
           ``connect_with_sdk_options``. Exact, and available before the
           session has ever streamed a turn.
        2. a reverse lookup of ``sessions.sdk_session_id`` against the fluid
           session id, for adapters built without the stamp.

        Returns None when neither resolves — the dispatcher turns that into a
        clear error instead of spawning a parentless worker.
        """
        opts = self._options
        stamped = (opts.extra or {}).get("app_session_id") if opts is not None else None
        if stamped:
            return str(stamped)
        fluid_id = self._sdk_session_id
        if not fluid_id and opts is not None:
            fluid_id = opts.session_id or opts.resume_session_id
        if not fluid_id:
            return None
        try:
            from app.db import database

            row = database.get_session_by_sdk_id(fluid_id)
        except Exception:  # noqa: BLE001
            logger.warning(
                "[HarnessAdapter] could not map fluid session %s back to an app "
                "session row for Task dispatch", fluid_id, exc_info=True,
            )
            return None
        return row.get("id") if row else None

    async def connect_with_sdk_options(self, sdk_options: Any) -> None:
        """Compat shim mirroring ClaudeAgentAdapter.connect_with_sdk_options.

        Callers that still build ``ClaudeAgentOptions`` directly (query_engine,
        query_background) can hand them here; the fields fluid can honor are
        projected onto a SessionOptions. This lets ``make_adapter()`` be a
        drop-in at those sites. Fields fluid cannot honor are ignored (see the
        module docstring's degraded list).
        """
        opts = SessionOptions(
            model=getattr(sdk_options, "model", None) or DEFAULT_MODEL,
            system_prompt=getattr(sdk_options, "system_prompt", None),
            cwd=getattr(sdk_options, "cwd", None),
            resume_session_id=getattr(sdk_options, "resume", None),
            permission_mode=getattr(sdk_options, "permission_mode", None),
            mcp_servers=getattr(sdk_options, "mcp_servers", None),
            env=getattr(sdk_options, "env", None),
            include_partial_messages=bool(
                getattr(sdk_options, "include_partial_messages", False)
            ),
            add_dirs=getattr(sdk_options, "add_dirs", None),
            disallowed_tools=getattr(sdk_options, "disallowed_tools", None),
            # CE12 tool-surface restriction, stamped by query_options as
            # ``aih_tool_allowlist``. NOT ``sdk_options.allowed_tools``: that is
            # the SDK's permission allowlist (MCP names + legacy config) and
            # reading it as a surface would strip the builtins from every native
            # session. See query_options for the full reasoning.
            tool_allowlist=getattr(sdk_options, "aih_tool_allowlist", None),
            # Profile ``max_turns`` (query_options puts it on the SDK options).
            # Native legs map it onto fluid's per-turn model-call budget.
            max_turns=getattr(sdk_options, "max_turns", None),
            # Reasoning depth (profile config or chat override, resolved by
            # query_options). Delegated leg → --effort; native legs → the
            # provider's effort/reasoning param via fluid's serializers.
            effort=getattr(sdk_options, "effort", None),
            # Per-session fast mode (v81), stamped by query_options as
            # ``aih_fast`` (override for this turn, else the session row).
            fast=getattr(sdk_options, "aih_fast", None),
            # Profile scope filter (user/project/local). Native legs map it
            # onto fluid's setting_sources (see _fluid_setting_sources); the
            # delegated leg already carries it to the CLI. None = every scope.
            setting_sources=getattr(sdk_options, "setting_sources", None),
        )
        # The session's bound model provider (``sessions.provider_id``) rides on
        # the SDK options object as ``aih_provider_id`` (stamped by
        # ``query_options.build_options_from_profile``; inert on the SDK path).
        # It is what lets _build_provider_client authenticate a native leg from
        # the app's encrypted ``model_providers`` row.
        provider_id = getattr(sdk_options, "aih_provider_id", None)
        if provider_id:
            opts.extra["provider_id"] = provider_id
            # The bound row's SHAPE, stamped alongside the id by query_options
            # so ``_decide_provider_kind`` can pick the leg from the provider
            # row instead of sniffing the model string (PA13). Only forwarded
            # when present — a missing type makes the adapter look the row up.
            provider_type = getattr(sdk_options, "aih_provider_type", None)
            if provider_type:
                opts.extra["provider_type"] = provider_type
            provider_auth_mode = getattr(sdk_options, "aih_provider_auth_mode", None)
            if provider_auth_mode:
                opts.extra["provider_auth_mode"] = provider_auth_mode
            provider_dialect = getattr(sdk_options, "aih_provider_dialect", None)
            if provider_dialect:
                opts.extra["provider_dialect"] = provider_dialect
        context_window = getattr(sdk_options, "aih_context_window", None)
        if context_window:
            opts.extra["context_window"] = int(context_window)
        # The app's own session id (``sessions.id``) rides alongside it as
        # ``aih_session_id``. Native legs need it for Task dispatch: the fluid
        # session id is a ``ses_...`` handle, not an app session row, and on a
        # fresh session neither id exists at connect time.
        app_session_id = getattr(sdk_options, "aih_session_id", None)
        if app_session_id:
            opts.extra["app_session_id"] = app_session_id
        profile_id = getattr(sdk_options, "aih_profile_id", None)
        if profile_id:
            opts.extra["profile_id"] = profile_id
        # The native leg's project-memory dir (rulings MEM1/MEM3), resolved by
        # query_options from the project's root path. Only the native prompt
        # builder reads it — the delegated leg's memory belongs to the CLI.
        memory_dir = getattr(sdk_options, "aih_memory_dir", None)
        if memory_dir:
            opts.extra["memory_dir"] = str(memory_dir)
        # Whether the install's gh CLI is present and signed in (stamped by
        # query_options as ``aih_github_connected``, behind a TTL cache). Only
        # forwarded when true — the prompt's "# GitHub" section exists solely to
        # say the tool is there, so "not connected" has nothing to carry.
        github_connected = getattr(sdk_options, "aih_github_connected", None)
        if github_connected:
            opts.extra["github_connected"] = True
        # The DB agents the profile declares into its model's context (2026-08-17
        # ruling, superseding A10) — ``[{"name", "description"}]`` resolved by
        # query_options from agent_invoke_mode/enabled_agents. Merged into the
        # compiled prompt's agents index; never a dispatch restriction.
        declared_agents = getattr(sdk_options, "aih_declared_agents", None)
        if declared_agents:
            opts.extra["declared_agents"] = list(declared_agents)
        # The app's permission prompt flow rides on this callback (see
        # _bridge_permission) — same wiring the SDK path uses.
        self._can_use_tool = getattr(sdk_options, "can_use_tool", None)
        await self.connect(opts)
        await self.prepare_background_completions(
            getattr(sdk_options, "aih_background_task_completions", None)
        )

    async def disconnect(
        self,
        grace_period: float = _ENGINE_CLOSE_GRACE_SECONDS,
        *,
        session_id: Optional[str] = None,
    ) -> None:
        """Close the fluid engine (cancels any running turn, flushes the bus)."""
        engine = self._engine
        if engine is None:
            return
        try:
            await asyncio.wait_for(engine.aclose(), timeout=grace_period)
        except asyncio.TimeoutError:
            logger.warning("[HarnessAdapter] engine.aclose() timed out after %.1fs", grace_period)
        except Exception as exc:  # never let teardown raise
            logger.warning("[HarnessAdapter] engine.aclose() failed: %r", exc)
        finally:
            # Unstick any in-flight permission bridges (they'd otherwise await
            # a user decision for an engine that no longer exists).
            for task in list(self._perm_bridge_tasks):
                task.cancel()
            self._perm_bridge_tasks.clear()
            if self._registry is not None:
                try:
                    await self._registry.aclose()
                except Exception:
                    pass
            if self._provider_http is not None:
                try:
                    await self._provider_http.aclose()
                except Exception:
                    pass
                self._provider_http = None
            self._connected = False

    def is_alive(self) -> bool:
        """Whether the engine exists and hasn't been closed.

        fluid engines are in-process (native) or supervise a subprocess
        (delegated). We treat "connected and not torn down" as alive; the
        delegated subprocess health is managed internally by the engine.
        """
        return self._engine is not None and self._connected

    def is_connected(self) -> bool:
        return self._connected and self._engine is not None

    # ─── Query Execution ──────────────────────────────────────────────

    async def prepare_background_completions(self, frames: Any) -> None:
        """Clear Fluid's pending Task receipts before the host-owned wake turn.

        AI Shuffle's durable turn queue owns wake admission, leases, batching,
        and user-message ordering. Fluid therefore records these completions with
        ``wake=False``: the app's already-claimed ``source=unpark`` turn is the
        continuation, so allowing Fluid to start another would double-run it.
        """
        if self._engine is None or self._is_delegated or not frames:
            return
        status_map = {
            "completed": "finished",
            "finished": "finished",
            "error": "failed",
            "failed": "failed",
            "killed": "cancelled",
            "cancelled": "cancelled",
            "stopped": "cancelled",
        }
        for frame in frames:
            if not isinstance(frame, dict):
                continue
            task_id = frame.get("task_id")
            if not task_id:
                continue
            status = status_map.get(str(frame.get("status") or "completed"), "failed")
            await self._engine.submit(BackgroundTaskFinished(
                task_id=str(task_id),
                status=status,
                summary=str(frame.get("summary") or "") or None,
                worker_session_id=str(task_id),
                wake=False,
            ))

    async def send_query(self, prompt: str) -> None:
        if self._engine is None:
            raise RuntimeError("Adapter not connected")
        # fluid annotates ``UserTurn.content`` as a list, but its own callers and
        # tests pass the prompt string straight through (the loops normalize it),
        # so the cast bridges the vendor annotation without touching the value.
        await self._engine.submit(UserTurn(content=cast(Any, prompt)))

    async def send_resume(self) -> None:
        if self._engine is None:
            raise RuntimeError("Adapter not connected")
        await self._engine.submit(Resume())

    async def steer(self, text: str, *, id: Optional[str] = None) -> None:
        """Hand ``text`` to the RUNNING turn as a fluid ``Steer``.

        Both legs accept it and both receipt it with ``SteerDelivered`` (mirrored
        to :class:`SteerDeliveredEvent`), but they deliver differently: the
        native leg folds the text into the next tool result inside fluid's
        steering marker, while the delegated leg writes a second ``user`` frame
        to the CLI's stdin. Either way the receipt — not this call — is what
        says the model has the words.

        fluid raises ``RuntimeError`` when there is nothing live to steer into
        (no running turn, an interrupt already requested, no CLI process yet).
        That is propagated verbatim: the caller leaves the message queued so the
        next turn picks it up.
        """
        if self._engine is None:
            raise RuntimeError("Adapter not connected")
        await self._engine.submit(Steer(text=text, id=id))

    async def cancel_steer(self, id: str) -> bool:
        """Ask fluid to drop the pending ``Steer`` submitted under ``id``.

        True only when the steer was still queued and is now gone: no delivery,
        no ``SteerDelivered`` receipt, nothing for the model to answer. The
        native leg decides this on the event loop, atomically against the tool
        boundary that would have delivered it, so there is no window where a
        steer is both cancelled and delivered.

        False whenever the text may already be in front of the model: an
        unknown id, a steer the native leg already folded into a tool result,
        and every delegated-leg call (writing to the CLI's stdin IS delivery,
        so there is nothing to recall). A disconnected or non-fluid engine
        answers False for the same reason — never claim an un-send that did
        not happen.
        """
        engine = self._engine
        if engine is None:
            return False
        cancel = getattr(engine, "cancel_steer", None)
        if cancel is None:
            return False
        return bool(await cancel(id))

    async def receive_events(self) -> AsyncIterator[ProviderEvent]:
        """Translate the fluid Event stream into ProviderEvents for one turn.

        Subscribes at the current replay cursor, yields translated mirror
        messages, and completes on the turn-terminal ResultMessage (synthesized
        from fluid's TurnComplete / Interrupted / ErrorEvent), mirroring the SDK
        adapter's iterator-completion contract.
        """
        if self._engine is None:
            raise RuntimeError("Adapter not connected")

        async for event in self._engine.events(since_seq=self._event_cursor):
            # Advance the cursor even for untranslated events so the next turn
            # never replays this one.
            if getattr(event, "seq", -1) >= 0:
                self._event_cursor = event.seq

            # Plan/StopPlanning flip the engine flag mid-dispatch; drain any
            # status notes they queued before translating this event so the
            # chip update lands next to the tool result that caused it.
            for pending in self._take_plan_mode_events():
                yield pending

            for translated in self._translate(event):
                if isinstance(translated, PermissionRequestEvent) and self._can_use_tool:
                    # SDK parity: permission prompts (and fluid QuestionRequest
                    # events, which we translate as risk="question") run
                    # out-of-band through the app's can_use_tool callback, not
                    # in the message stream. Spawned as a task — the callback
                    # blocks on the user while other stream events keep flowing.
                    if getattr(translated, "risk", None) == "question":
                        task = asyncio.ensure_future(self._bridge_question(translated))
                    else:
                        task = asyncio.ensure_future(self._bridge_permission(translated))
                    self._perm_bridge_tasks.add(task)
                    task.add_done_callback(self._perm_bridge_tasks.discard)
                    continue
                yield translated
                if isinstance(translated, ResultMessage):
                    return

    async def _resolve_cli_session_id(
        self, fluid_session_id: Optional[str], options: SessionOptions
    ) -> Optional[str]:
        """Resolve the delegated CLI's ``--resume`` handle from the store.

        ``resume_session_id`` is the app's *fluid* session id (``ses_...``); the
        CLI only recognises the uuid it minted itself and reported in its
        ``init`` frame. DelegatedLoop persists that uuid onto the session meta,
        so a resumed session reads it back from here.

        Returns None when there is nothing stored — a fresh CLI conversation is
        the correct degraded behavior. Passing the fluid id through instead
        (the old bug) makes the CLI reject the resume and the turn dies in ~1s.
        """
        if not options.resume_session_id or not fluid_session_id or self._store is None:
            return None
        try:
            meta = await self._store.get_session(fluid_session_id)
        except Exception:
            logger.warning(
                "[HarnessAdapter] could not load fluid session %s for the CLI resume "
                "handle — starting a fresh CLI conversation",
                fluid_session_id, exc_info=True,
            )
            return None
        return getattr(meta, "cli_session_id", None) if meta is not None else None

    async def _seed_event_log(self, session_id: str) -> None:
        """Seed the engine's in-memory event log from the durable store.

        WHY this exists: fluid's EventBus numbers events ``seq = len(log) + 1``
        off its *in-memory* log. A freshly built engine over a RESUMED session
        starts with an empty log, so its first new event is numbered 1 — even
        though the store already holds seq 1..N for that session. The durable
        append is idempotent per ``(session_id, seq)`` (PgFluidStore's
        ``ON CONFLICT DO NOTHING``), so those re-used numbers collide with the
        existing rows and the first N events of every post-resume turn are
        silently DROPPED from ``fluid_session_events``.

        Headless one-shot turns (ScheduleWakeup wakes, automations) build a
        fresh adapter per turn, so they hit this on EVERY turn and the wake's
        events never reach the durable log at all. Pooled sessions hit it once,
        on the first resume after a process restart.

        Seeding the bus log with the stored events restores the invariant the
        bus assumes (log length == highest seq), so new events continue at N+1
        and persist. Advancing ``_event_cursor`` past the seeded tail keeps
        ``receive_events()`` from replaying that history into the new turn.
        """
        if self._store is None:
            return
        try:
            stored = await self._store.events_since(session_id, 0)
        except Exception:
            # Seeding is best effort — it must never fail a connect.
            logger.exception(
                "[HarnessAdapter] could not load durable events for %s; new "
                "events may collide with stored seqs and be dropped",
                session_id,
            )
            return
        if not stored:
            return
        bus = getattr(self._engine, "bus", None)
        if bus is None:
            return  # engines/test fakes without an EventBus
        log = bus.log(session_id)
        if log:
            return  # this bus already carries the session's history
        if stored[-1].seq != len(stored):
            # A hole in the durable log can only come from prior drops under
            # this very bug. The bus indexes its log positionally, so only a
            # contiguous prefix may be seeded.
            logger.warning(
                "[HarnessAdapter] non-contiguous durable event log for %s "
                "(%d events, last seq %s) — seeding the contiguous prefix only",
                session_id, len(stored), stored[-1].seq,
            )
            prefix = []
            for index, event in enumerate(stored):
                if getattr(event, "seq", -1) != index + 1:
                    break
                prefix.append(event)
            stored = prefix
            if not stored:
                return
        log.extend(stored)
        self._event_cursor = max(self._event_cursor, stored[-1].seq)

    async def _bridge_permission(self, ev: PermissionRequestEvent) -> None:
        """Route one fluid permission request through the app's can_use_tool
        callback and submit the decision back to the engine.

        The callback duck-types its ``context`` argument via getattr (see
        can_use_tool_callback in query_engine), so a SimpleNamespace carrying
        tool_use_id is sufficient. The result is detected by class name to keep
        claude_agent_sdk types out of this module. Any failure denies —
        fluid blocks the tool until a decision arrives, so silence would hang
        the turn.
        """
        from types import SimpleNamespace

        allow = False
        message: Optional[str] = None
        updated_input: Optional[Dict[str, Any]] = None
        try:
            # Only reached from receive_events(), which spawns this bridge under
            # an ``and self._can_use_tool`` guard — non-None by construction.
            can_use_tool = cast(Any, self._can_use_tool)
            result = await can_use_tool(
                ev.name,
                ev.input,
                SimpleNamespace(tool_use_id=ev.tool_use_id, agent_id=None),
            )
            allow = type(result).__name__ == "PermissionResultAllow"
            if allow:
                # PermissionResultAllow.updated_input is how the callback hands
                # data back to the tool (AskUserQuestion answers, rewritten
                # commands). Dropping it silently ran the ORIGINAL input.
                candidate = getattr(result, "updated_input", None)
                if isinstance(candidate, dict):
                    updated_input = candidate
            else:
                # PermissionResultDeny.message is the text the model reads as the
                # denied call's tool_result. Forwarding it is what keeps a
                # question DISMISSAL from reading as a permission failure.
                candidate_msg = getattr(result, "message", None)
                if isinstance(candidate_msg, str) and candidate_msg:
                    message = candidate_msg
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "[HarnessAdapter] can_use_tool bridge failed for %s — denying",
                ev.name, exc_info=True,
            )
        try:
            await self.respond_to_permission(
                ev.tool_use_id, allow, message=message, updated_input=updated_input,
            )
        except Exception:
            logger.warning(
                "[HarnessAdapter] failed to submit permission decision for %s",
                ev.tool_use_id, exc_info=True,
            )

    async def _bridge_question(self, ev: PermissionRequestEvent) -> None:
        """Route one fluid QuestionRequest through the app's question modal.

        AskUser / StopPlanning park on RespondQuestion, not RespondPermission.
        ``can_use_tool`` drives the app interaction: AskUser uses UserQuestion;
        StopPlanning uses the plan-approval PermissionRequest panel. We take
        ``_fluid_answer`` off its allow result and submit that string as the
        question answer. Fluid has no deny-question op, so rejection is encoded
        as an allowed bridge result carrying ``keep planning``.
        """
        from types import SimpleNamespace

        from app.core.user_question_handler import FLUID_ANSWER_KEY

        answer = ""
        try:
            can_use_tool = cast(Any, self._can_use_tool)
            result = await can_use_tool(
                ev.name,
                ev.input,
                SimpleNamespace(tool_use_id=ev.tool_use_id, agent_id=None),
            )
            if type(result).__name__ == "PermissionResultAllow":
                candidate = getattr(result, "updated_input", None)
                if isinstance(candidate, dict):
                    raw = candidate.get(FLUID_ANSWER_KEY, "")
                    if isinstance(raw, list):
                        answer = ", ".join(str(item) for item in raw)
                    else:
                        answer = str(raw or "")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "[HarnessAdapter] can_use_tool question bridge failed for %s — empty answer",
                ev.name, exc_info=True,
            )
        await self._submit_question_answer(ev, answer)

    async def _submit_question_answer(
        self, ev: PermissionRequestEvent, answer: str
    ) -> None:
        try:
            request_id = ev.request_id or self._pending_question_id
            if self._engine is None or not request_id:
                return
            if self._pending_question_id == request_id:
                self._pending_question_id = None
            await self._engine.submit(
                RespondQuestion(request_id=request_id, answer=answer)
            )
        except Exception:
            logger.warning(
                "[HarnessAdapter] failed to submit question answer for %s",
                ev.tool_use_id, exc_info=True,
            )

    def _came_from_bypass(self) -> bool:
        """True when this session's human opted into bypassPermissions.

        Plan persists the chip as ``plan``, so the *current* binding is the
        wrong signal. Use the mode captured at connect / before Plan flipped
        it. A turn that still *is* bypass (Plan + StopPlanning in one turn)
        also counts.
        """
        pre = (self._pre_plan_permission_mode or "").strip()
        current = ""
        if self._options is not None:
            current = (self._options.permission_mode or "").strip()
        return "bypassPermissions" in (pre, current)

    async def interrupt(self) -> None:
        if self._engine is not None:
            await self._engine.submit(Interrupt())

    # ─── Interactive Responses ────────────────────────────────────────

    async def respond_to_permission(
        self,
        tool_use_id: str,
        allow: bool,
        *,
        remember: bool = False,
        message: Optional[str] = None,
        updated_input: Optional[Dict[str, Any]] = None,
    ) -> None:
        if self._engine is None:
            return
        request_id = self._perm_requests.pop(tool_use_id, None)
        if request_id is None:
            logger.warning(
                "[HarnessAdapter] no pending fluid permission for tool_use_id=%s", tool_use_id
            )
            return
        decision = ("allow_always" if remember else "allow") if allow else "deny"
        await self._engine.submit(
            RespondPermission(
                request_id=request_id,
                decision=decision,
                remember=remember,
                message=message,
                updated_input=updated_input,
            )
        )

    async def respond_to_user_question(self, answer: str) -> None:
        # Live AskUser / StopPlanning answers go through _bridge_question.
        # This remains as a fallback if a caller has the pending id.
        if self._engine is None or self._pending_question_id is None:
            return
        request_id = self._pending_question_id
        self._pending_question_id = None
        await self._engine.submit(RespondQuestion(request_id=request_id, answer=answer))

    # ─── Runtime Configuration ────────────────────────────────────────

    async def set_model(self, model: str) -> None:
        if self._engine is None:
            raise RuntimeError("Adapter not connected")
        if self._provider_kind == "anthropic":
            # Mid-session hot swap lands on NativeLoop's raw model attribute —
            # same verbatim-to-API path as connect, same canonicalization.
            model = resolve_api_model_id(model) or model
        try:
            await self._engine.submit(SetModel(model=model))
        except NotImplementedError:
            # NativeLoop reads self.model per request; hot-swap by attribute.
            self._engine.model = model

    async def set_permission_mode(self, mode: str) -> None:
        if self._engine is None:
            raise RuntimeError("Adapter not connected")
        # BEFORE the submit: fluid routes a plan-mode flip through the injected
        # PlanModeControl, so our own wrapper runs inside submit() and reads
        # this field to decide what mode to restore to. Setting it afterwards
        # made a chip switch out of plan restore the mode from BEFORE the one
        # the user just picked.
        if mode != "plan":
            self._pre_plan_permission_mode = mode
        try:
            await self._engine.submit(SetPermissionMode(mode=mode))
        except NotImplementedError:
            # NativeLoop: adjust the permission engine + plan_mode in place.
            policy = _PERMISSION_POLICY.get(mode, "on_write")
            perms = getattr(self._engine, "permissions", None)
            if perms is not None:
                perms.approval_policy = policy
            if hasattr(self._engine, "plan_mode"):
                self._engine.plan_mode = mode == "plan"
        if self._options is not None:
            self._options.permission_mode = mode

    async def get_context_usage(self) -> Optional[Dict[str, Any]]:
        """Context-window usage by category, or None when unavailable.

        SDK parity (see ClaudeAdapter.get_context_usage): every caller —
        ``shielded_get_context_usage`` in query_persistence, the
        ``/context-usage`` endpoint, and compact_continue's live check —
        tolerates None and falls back to stored usage. Returning None is the
        contract for "the backend can't answer", not an error.

        BOTH fluid legs answer this now (vendored a6957a1), in the same
        SDK-parity shape, so this forwards verbatim:

        * ``DelegatedLoop`` proxies the claude CLI's own ``/context``
          breakdown over the ``{"subtype": "get_context_usage"}`` control
          request (the leg gained request/response correlation for control
          frames, which is what used to make this impossible).
        * ``NativeLoop`` computes the same shape itself from the compiled
          prompt sections, the tool schemas and the trimmed history. Its
          ``maxTokens``/``percentage`` come from the ``context_window`` the
          adapter resolves at connect (see ``_native_context_window``); with
          no window known it reports ``maxTokens: None``.

        The ``getattr`` probe stays for engines/doubles without the method. It
        never estimates from stored usage — a fabricated breakdown would be
        worse than None.
        """
        engine = self._engine
        if engine is None:
            return None
        get_usage = getattr(engine, "get_context_usage", None)
        if get_usage is None:
            return None
        try:
            return await get_usage()
        except Exception as exc:  # pragma: no cover — defensive, mirrors the SDK adapter
            logger.warning(f"[HarnessAdapter] get_context_usage() failed: {exc}")
            return None

    # ─── Session Identity ─────────────────────────────────────────────

    @property
    def sdk_session_id(self) -> Optional[str]:
        # Prefer the engine's live session id once a turn has assigned it.
        engine = self._engine
        if engine is not None:
            try:
                return engine.session_id
            except AssertionError:
                pass
        return self._sdk_session_id

    @sdk_session_id.setter
    def sdk_session_id(self, value: Optional[str]) -> None:
        self._sdk_session_id = value

    # ─── Options Comparison ───────────────────────────────────────────

    def requires_reconnect(self, new_options: SessionOptions) -> bool:
        """Model / permission_mode hot-swap (False); structural changes → True.

        Mirrors ClaudeAgentAdapter.requires_reconnect: system_prompt, cwd,
        mcp_servers, plugins, and tools are fixed at engine-construction time.
        """
        old = self._options
        if old is None:
            return True
        # _system_parts, not a flattening stringify: the preset form loses its
        # append text under flattening, which would mask changes to the
        # appended profile instructions and skip a needed reconnect.
        if _system_parts(old.system_prompt) != _system_parts(
            new_options.system_prompt or old.system_prompt
        ):
            return True
        if old.cwd != (new_options.cwd or old.cwd):
            return True
        if not mcp_servers_equivalent(old.mcp_servers, new_options.mcp_servers):
            return True
        if (old.plugins or []) != (new_options.plugins or []):
            return True
        if (old.tools or []) != (new_options.tools or []):
            return True
        # Tool restriction is session-STATIC by design (ruling CE12): fluid
        # freezes allowed/disallowed at construction and never renegotiates them
        # mid-session, so a changed surface can only be honored by a rebuild.
        if _tool_allowlist(old) != _tool_allowlist(new_options):
            return True
        if sorted(old.disallowed_tools or []) != sorted(
            new_options.disallowed_tools or []
        ):
            return True
        # Effort is baked into the engine at construction (NativeLoop request
        # builder / DelegatedLoop argv), so a change needs a rebuild.
        if _valid_effort(old.effort) != _valid_effort(
            new_options.effort if new_options.effort is not None else old.effort
        ):
            return True
        # Fast mode is baked in the same way (DelegatedLoop --settings argv /
        # NativeLoop request_extra), so a flip is a rebuild, not a hot-swap.
        if bool(getattr(old, "fast", None)) != bool(
            getattr(new_options, "fast", None)
        ):
            return True
        # Resolved provider binding (PA13 / W-R4) — mirrors
        # ``session_pool.requires_reconnect``. The engine and its credentials are
        # built once in ``connect()`` from this id, so a cross-provider switch
        # can never be a ``set_model`` hot-swap. Compared directly (no
        # ``or old``-style defaulting): absent means NATIVE, which is a real,
        # distinct value here.
        if (old.extra or {}).get("provider_id") != (
            new_options.extra or {}
        ).get("provider_id"):
            return True
        return False

    # ─── Translation: fluid Event → ProviderEvent mirrors ──────────────

    def _uuid(self, event: Any) -> str:
        """Stable synthetic uuid for a fluid event (mirrors carry uuid fields)."""
        return f"fluid-{getattr(event, 'seq', -1)}"

    def _stream_event(self, event: Any, raw: Dict[str, Any]) -> StreamEvent:
        return StreamEvent(
            uuid=self._uuid(event),
            session_id=getattr(event, "session_id", "") or "",
            event=raw,
            parent_tool_use_id=None,
        )

    def _assistant(self, event: Any, blocks: list) -> AssistantMessage:
        """Synthesized content message; per-call usage has its own message."""
        return AssistantMessage(
            content=blocks,
            model=self._current_model
            or (self._options.model if self._options else ""),
            usage=None,
            session_id=getattr(event, "session_id", None),
            uuid=self._uuid(event),
        )

    def _translate(self, event: Any) -> list:
        """The core mapping table. Returns a list of mirror events (possibly
        empty) — one fluid event can fan out to a StreamEvent plus a complete
        message, exactly like the SDK's partial-message mode."""
        cls = type(event).__name__
        partial = bool(self._options and self._options.include_partial_messages)

        if cls == "TurnStarted":
            # Parallels the SDK's system-init message: the loops read
            # data["session_id"] (session link) and data.get("model")
            # (model_info seeding).
            self._sdk_session_id = event.session_id
            self._current_model = event.model
            self._block_types.clear()
            self._block_buffers.clear()
            return [
                SystemMessage(
                    subtype="init",
                    data={
                        "session_id": event.session_id,
                        "model": event.model,
                        "provider": event.provider,
                    },
                )
            ]

        if cls == "BlockStart":
            self._block_types[event.index] = event.block_type
            self._block_buffers[event.index] = []
            if not partial:
                return []
            content_block: Dict[str, Any] = {"type": event.block_type}
            if event.block_type == "tool_use":
                content_block["id"] = event.tool_use_id
                content_block["name"] = event.tool_name
                content_block["input"] = {}
            elif event.block_type == "thinking":
                content_block["thinking"] = ""
            else:
                content_block["text"] = ""
            return [
                self._stream_event(
                    event,
                    {
                        "type": "content_block_start",
                        "index": event.index,
                        "content_block": content_block,
                    },
                )
            ]

        if cls in ("TextDelta", "ThinkingDelta"):
            self._block_buffers.setdefault(event.index, []).append(event.text)
            self._block_types.setdefault(
                event.index, "text" if cls == "TextDelta" else "thinking"
            )
            if not partial:
                return []
            delta = (
                {"type": "text_delta", "text": event.text}
                if cls == "TextDelta"
                else {"type": "thinking_delta", "thinking": event.text}
            )
            return [
                self._stream_event(
                    event,
                    {
                        "type": "content_block_delta",
                        "index": event.index,
                        "delta": delta,
                    },
                )
            ]

        if cls == "ToolArgsDelta":
            # A tool call's arguments as the model writes them. The pipeline
            # from here on already exists for the delegated CLI leg — the pump
            # turns input_json_delta into a tool_input stream_delta and the
            # recorder coalesces those into ToolInputDelta contract events — so
            # the native leg only had to start speaking the same shape. Not
            # buffered here: BlockStop reads _block_buffers for the assistant
            # message, and the authoritative arguments arrive with ToolUse.
            if not partial:
                return []
            return [
                self._stream_event(
                    event,
                    {
                        "type": "content_block_delta",
                        "index": event.index,
                        "delta": {
                            "type": "input_json_delta",
                            "partial_json": event.partial_json,
                            "name": event.name,
                        },
                    },
                )
            ]

        if cls == "BlockStop":
            block_type = self._block_types.pop(event.index, "text")
            text = "".join(self._block_buffers.pop(event.index, []))
            out: list = []
            if partial:
                out.append(
                    self._stream_event(
                        event,
                        {"type": "content_block_stop", "index": event.index},
                    )
                )
            # Complete text/thinking blocks surface as an AssistantMessage
            # (SDK parity: complete messages follow the deltas). tool_use
            # blocks are covered by the discrete ToolUse fluid event.
            if block_type == "text" and text:
                out.append(self._assistant(event, [TextBlock(text=text)]))
            elif block_type == "thinking" and text:
                out.append(
                    self._assistant(event, [ThinkingBlock(thinking=text, signature="")])
                )
            return out

        if cls == "ToolUse":
            return [
                self._assistant(
                    event,
                    [
                        ToolUseBlock(
                            id=event.tool_use_id, name=event.name, input=event.input
                        )
                    ],
                )
            ]

        if cls == "ToolResult":
            output = event.output
            if output is not None and not isinstance(output, (str, list)):
                output = str(output)
            return [
                UserMessage(
                    content=[
                        ToolResultBlock(
                            tool_use_id=event.tool_use_id,
                            content=output,
                            is_error=bool(getattr(event, "is_error", False)),
                        )
                    ],
                    uuid=self._uuid(event),
                )
            ]

        if cls == "SteerDelivered":
            # A queued message just became part of immutable history — the model
            # reads it on its next call. The pump turns this into the user
            # message's transcript record and retires the queue row it came
            # from, so it must reach the stream rather than being swallowed.
            return [
                SteerDeliveredEvent(
                    steers=[
                        dict(steer) for steer in (event.steers or [])
                        if isinstance(steer, dict)
                    ],
                    session_id=getattr(event, "session_id", "") or "",
                    uuid=self._uuid(event),
                )
            ]

        if cls == "QuestionRequest":
            # MRTR input_required round-trip (AskUser / StopPlanning). Record
            # the id and surface as a permission-shaped event with
            # risk="question" so receive_events routes it through
            # _bridge_question → app interaction UI → RespondQuestion.
            self._pending_question_id = event.request_id
            question_input = {"question": event.message, "schema": event.schema}
            if event.name == "StopPlanning":
                # StopPlanning's QuestionRequest message contains the complete
                # plan followed by fluid's synthetic approval question. Carry
                # only the markdown plan so the permission panel does not have
                # to scrape transcript text or show the question twice.
                suffix = (
                    "\n\nApprove this plan and leave plan mode? "
                    "(1 = Approve, anything else = feedback)"
                )
                plan_text = event.message
                if plan_text.endswith(suffix):
                    plan_text = plan_text[: -len(suffix)]
                question_input["plan"] = plan_text
            return [
                PermissionRequestEvent(
                    tool_use_id=event.tool_use_id,
                    name=event.name,
                    input=question_input,
                    risk="question",
                    request_id=event.request_id,
                )
            ]

        if cls == "PermissionRequest":
            # Record the tool_use_id -> request_id link so respond_to_permission
            # can route the decision back to the right fluid future.
            self._perm_requests[event.tool_use_id] = event.request_id
            return [
                PermissionRequestEvent(
                    tool_use_id=event.tool_use_id,
                    name=event.name,
                    input=event.input,
                    risk=event.risk,
                    request_id=event.request_id,
                )
            ]

        if cls == "Usage":
            # Emit a contentless AssistantMessage NOW. Holding usage for the
            # next content block created a one-call lag and dropped the final
            # call's telemetry entirely. QueryEngine already treats an empty
            # AssistantMessage as non-visible while recording ``turn_usage``.
            usage: Dict[str, Any] = {
                "input_tokens": event.input_tokens,
                "output_tokens": event.output_tokens,
                "cache_read_input_tokens": event.cache_read_tokens,
                "cache_creation_input_tokens": event.cache_creation_tokens,
            }
            if event.raw:
                usage.update(event.raw)
            # Per-call context breakdown (fluid >= baf8be0). Set after ``raw``
            # so a stray raw key cannot shadow the real snapshot. Absent rather
            # than None when the engine could not take one, so downstream
            # ``.get("context_usage")`` checks stay a simple presence test.
            context_usage = getattr(event, "context_usage", None)
            if context_usage:
                usage["context_usage"] = context_usage
            return [AssistantMessage(
                content=[],
                model=self._current_model
                or (self._options.model if self._options else ""),
                usage=usage,
                session_id=getattr(event, "session_id", None),
                uuid=self._uuid(event),
            )]

        if cls == "DelegatedSurface":
            # fluid >= 5248b2e: the claude CLI announced its own tool surface in
            # its ``init`` frame and DelegatedLoop republished it. This is the
            # ingest point ruling T4 documented but nothing used to call — feed
            # the cache so the NEXT delegated connect can subtract the surface
            # in allow mode (see ``_delegated_disallowed``). ``getattr`` because
            # a stale vendor may carry a differently-shaped event; a surface we
            # cannot read is simply not recorded.
            from app.core.cli_tool_surface import record_cli_tools

            record_cli_tools(getattr(event, "tools", None))
            # Nothing user-visible: the CLI's startup inventory is plumbing,
            # not conversation.
            return []

        if cls == "SafetyWarning":
            # Legacy replay compatibility only; NativeLoop no longer emits
            # inferred safety warnings or stops.
            return [SystemMessage(
                subtype="safety_limit" if event.hard else "safety_warning",
                data={
                    "content": event.message,
                    "code": event.code,
                    "hard": event.hard,
                    "counters": dict(event.counters),
                },
            )]

        if cls == "HookEvent":
            # fluid >= 483e750: a configured hook denied a tool call ("deny")
            # or misbehaved and was ignored ("warning"). Never terminal; shown
            # the same way the legacy safety warnings were.
            return [SystemMessage(
                subtype="hook_deny" if event.level == "deny" else "hook_warning",
                data={
                    "content": event.message,
                    "hook_event": getattr(event, "hook_event", ""),
                    "tool_name": getattr(event, "tool_name", None),
                },
            )]

        if cls == "ProviderRetry":
            # The engine is about to re-send the same request after an empty
            # provider response. Never terminal; the UI folds consecutive
            # attempts into one card that counts up.
            return [SystemMessage(
                subtype="provider_retry",
                data={
                    "attempt": event.attempt,
                    "max_attempts": event.max_attempts,
                    "reason": event.reason,
                    "delay_seconds": event.delay_seconds,
                    "model": getattr(event, "model", None),
                },
            )]

        if cls == "RateLimit":
            return [
                RateLimitEvent(
                    rate_limit_info=RateLimitInfo(
                        status="allowed",
                        resets_at=event.resets_at,
                        rate_limit_type=event.kind,
                        utilization=event.utilization,
                    ),
                    uuid=self._uuid(event),
                    session_id=getattr(event, "session_id", "") or "",
                )
            ]

        if cls in ("SessionParked", "SessionWoken"):
            # AI Shuffle exposes its own durable turn.parked / queued-unpark
            # lifecycle. Fluid's matching engine-internal events are persisted for
            # rehydration but must not create a second app-level lifecycle frame.
            return []

        if cls == "SubagentEvent":
            if getattr(event, "worker_session_id", None):
                # Host-owned ProviderAgent workers already stream through
                # WorkerFeed/task_registry. Mirroring Fluid's receipt events would
                # duplicate the same task card and terminal wake.
                return []
            common = {
                "uuid": self._uuid(event),
                "session_id": getattr(event, "session_id", "") or "",
            }
            if event.kind == "started":
                # A receipt without a worker session is a host Monitor shell
                # (Task/ProviderAgent workers always carry one). Stamp the CLI's
                # local-bash marker so query_engine._record_task_message files
                # it under kind="shell" instead of clobbering the SHELL
                # registration the spawner made with kind="agent".
                return [
                    TaskStartedMessage(
                        subtype="task_started",
                        data={"task_id": event.task_id},
                        task_id=event.task_id,
                        description=event.description or "",
                        task_type=(
                            None if getattr(event, "worker_session_id", None)
                            else "local_bash"
                        ),
                        **common,
                    )
                ]
            if event.kind == "progress":
                return [
                    TaskProgressMessage(
                        subtype="task_progress",
                        data={"task_id": event.task_id},
                        task_id=event.task_id,
                        description=event.description or "",
                        **common,
                    )
                ]
            status = cast(str, {"finished": "completed"}.get(event.kind, event.kind))
            return [
                TaskNotificationMessage(
                    subtype="task_notification",
                    data={"task_id": event.task_id},
                    task_id=event.task_id,
                    status=status,
                    output_file="",
                    summary=event.summary or "",
                    **common,
                )
            ]

        if cls == "TurnComplete":
            if event.stop_reason.startswith("safety_limit:"):
                terminal_reason = "safety_limit"
            elif event.stop_reason == "max_calls":
                terminal_reason = "max_turns"
            else:
                terminal_reason = "completed"
            return [
                ResultMessage(
                    subtype="success",
                    is_error=False,
                    num_turns=event.num_model_calls,
                    session_id=event.session_id,
                    stop_reason=event.stop_reason,
                    # fluid can report cost_usd=None (e.g. the delegated-Claude
                    # leg on subscription auth); the sessions row and the
                    # response model both require a float, so coerce here.
                    total_cost_usd=event.cost_usd if event.cost_usd is not None else 0.0,
                    usage=dict(event.usage),
                    uuid=self._uuid(event),
                    terminal_reason=terminal_reason,
                )
            ]

        if cls == "Interrupted":
            # SDK parity: an interrupted turn still ends in a ResultMessage;
            # the loops detect the interrupt via state.interrupt_requested and
            # terminal_reason, not via a distinct message type.
            #
            # A turn-ending Interrupted carries the turn's accumulated partial
            # usage/cost, so the interrupted turn's tokens and spend reach the
            # session-row increments the same way TurnComplete's do. The cost can
            # still be None (no usage seen yet, or a leg that reports no cost);
            # the session-row update does `col + value`, so a None would NULL the
            # column — coerce it to 0.0.
            cost = getattr(event, "cost_usd", None)
            return [
                ResultMessage(
                    subtype="success",
                    is_error=False,
                    session_id=getattr(event, "session_id", "") or "",
                    result=event.message,
                    total_cost_usd=cost if cost is not None else 0.0,
                    usage=dict(getattr(event, "usage", None) or {}),
                    uuid=self._uuid(event),
                    terminal_reason="aborted_streaming",
                )
            ]

        if cls == "ErrorEvent":
            # Same partial-usage/cost carry as the Interrupted branch above
            # (including the None → 0.0 coercion rationale).
            cost = getattr(event, "cost_usd", None)
            return [
                ResultMessage(
                    subtype="error_during_execution",
                    is_error=True,
                    session_id=getattr(event, "session_id", "") or "",
                    result=event.message,
                    errors=[event.message],
                    total_cost_usd=cost if cost is not None else 0.0,
                    usage=dict(getattr(event, "usage", None) or {}),
                    uuid=self._uuid(event),
                )
            ]

        # Unknown / no-analog fluid event — skip.
        return []

    # ─── Internal builders ────────────────────────────────────────────

    @staticmethod
    def _compile_prompt_sections(
        system: Optional[str],
        append_system: Optional[str],
        options: SessionOptions,
        *,
        code_mode: bool = False,
        codemode: Any = None,
        native: bool = True,
        registry: Any = None,
    ) -> Any:
        """Run the fluid prompt compiler, keeping the labeled sections.

        Returns fluid's ``CompiledPrompt`` (joined text + one section per
        input: base, instruction files, skills index, agents index,
        environment, extras) or ``None`` on compiler failure. The sections are
        what let the native leg's ``get_context_usage()`` price "Memory
        files"/"Skills" as their own rows instead of one opaque "System prompt"
        blob — the same source-by-source split Claude Code's ``/context`` shows.

        The AGENTS section is the app's, not the compiler's: fluid's own
        ``agents_index`` can only see disk (``.claude/agents`` + plugin agents),
        and the profile's context-declared DB agents belong in the same index.
        ``include_agents=False`` turns the compiler's version off and
        :func:`_agent_context` supplies the merged one, slotted where the
        compiler would have put it (after skills, before environment) so the
        prompt prefix stays cache-stable.

        ``code_mode`` reflects whether this leg actually runs a
        ``CodeModeSession`` (never merely whether the setting is on — the
        build is fail-open). Under code mode the model's schema holds only
        run_python/list_tools/search_tools/read_tool_docs, so every "use the X
        tool" line in this prompt would otherwise name something the model
        cannot see. Two headers are rewritten to say how the tool is reached
        (skills, agents) and a trailing ``# Code mode`` section states the rule
        once for everything above it. ``codemode`` is that session itself, and
        it supplies the ``# Tools`` catalog that follows the rule — the full
        tool documentation the model would otherwise never see, since a
        code-mode schema carries four tools instead of forty.

        ``native`` says whether this compilation feeds a native leg. It is the
        gate on the ``# Project memory`` section (ruling MEM3): only a native
        leg has an app-owned memory dir to point at — under
        ``prompt_full_replace`` this same compiler also builds the DELEGATED
        leg's prompt, and there the claude CLI owns memory and gathers it
        itself, so naming the fluid dir would send it to the wrong place.
        """
        try:
            from fluid.context.compiler import (
                CompiledPrompt, PromptInputs, PromptSection, compile_prompt,
            )

            # A profile with ``inject_env_details`` on already carries the
            # app's <env> block (which knows worktree/execution mode fluid
            # doesn't) — skip fluid's env section rather than injecting twice.
            has_app_env = bool(system) and "<env>" in system
            # Ruling T9: the prompt never describes a tool the session cannot
            # call. ``tools`` is the permitted name set; fluid drops its skills
            # index when ``Skill`` is missing from it and its agents index when
            # ``Task`` is. None is fluid's own "no filter" value, so an
            # unrestricted agent compiles exactly as it did before.
            permitted = _permitted_tool_names(options, registry)
            memory_dir = (options.extra or {}).get("memory_dir") if native else None
            # The continual learner's ``# Memory`` snapshot (facts the
            # synthesizer distilled from past sessions' journals) — native legs
            # only, and only when continual learning is on. The delegated
            # claude CLI owns its own memory and would double up.
            from app.core.config import settings as _settings

            memories_dir = (
                str(_fluid_skills_root())
                if native and _settings.fluid_continual
                else None
            )
            compiled = compile_prompt(
                PromptInputs(
                    base=system,
                    extra_blocks=[append_system] if append_system else [],
                    cwd=options.cwd or ".",
                    include_env=not has_app_env,
                    include_agents=False,
                    memories_dir=memories_dir,
                    tools=permitted,
                    # Profile scope filter (user/project/local) — gates fluid's
                    # instruction files, skills, agents, and plugin discovery.
                    # None = every scope, the leg's historical behavior.
                    setting_sources=_fluid_setting_sources(options),
                )
            )
            permit = _tool_predicate(options)
            index, _ = _agent_context(options, code_mode=code_mode)
            if index and permitted is not None and "Task" not in permitted:
                # The app's merged agents index is the app's own section (see
                # the docstring), so fluid's ``tools`` filter cannot reach it —
                # gate it here on the same rule.
                index = None
            sections = list(compiled.sections)
            if code_mode:
                # The compiler's skills header points at "the Skill tool",
                # which is not in a code-mode schema. Keyed by section kind,
                # the same way the agents block is slotted below.
                sections = [
                    (
                        replace(section, text=_code_mode_skills_text(section.text))
                        if section.kind == "skills"
                        else section
                    )
                    for section in sections
                ]
            if index:
                insert_at = len(sections)
                for position, section in enumerate(sections):
                    if section.kind in ("environment", "extra"):
                        insert_at = position
                        break
                sections.insert(insert_at, PromptSection(kind="agents", text=index))
            if native:
                # Mid-turn steering, native leg only. A steer arrives INSIDE a
                # tool result — the one channel the base prompt teaches the
                # model to distrust — so fluid pairs the marker with a prompt
                # block that grants that exact marker user authority. fluid
                # carries the block in its own composed base prompt, which the
                # app replaces wholesale with the agent's configured prompt
                # (ruling A12), so without this the marker would read as
                # suspected injection and be ignored.
                #
                # The delegated leg needs nothing: its steers are real ``user``
                # frames on the CLI's stdin, indistinguishable from any other
                # thing the user typed.
                from fluid.context.steering import PROMPT_BLOCK as STEERING_BLOCK

                sections.append(
                    PromptSection(kind="extra", text=STEERING_BLOCK)
                )
                # fluid's model-family guidance ("Act, don't narrate",
                # "## Execution discipline"). fluid only ever emits these from
                # its own ``compose_base_prompt``, which the app replaces with
                # the profile's prompt (ruling A12) — so without this they
                # reach no session at all. Claude-family models get none; the
                # text is fixed per model, so the prefix stays cache-stable.
                for block in _model_guidance_blocks(options.model):
                    sections.append(PromptSection(kind="extra", text=block))
            if native and _recall_backend_available() and (
                permitted is None
                or {"SessionSearch", "SessionView"} <= permitted
            ):
                # Harness-owned "when to recall" guidance (see RECALL_BLOCK).
                # Gated the same way the tools themselves are: a DB-less build
                # has no recall backend, and a restricted surface without the
                # tools must not be told to call them (ruling T9).
                sections.append(
                    PromptSection(kind="extra", text=RECALL_BLOCK)
                )
            if (options.extra or {}).get("github_connected") and (
                permitted is None or "Bash" in permitted
            ):
                # The install's gh CLI is signed in, so say so once instead of
                # letting the model rebuild PR/CI state from git plumbing or ask
                # the user. Gated on Bash for the same reason the agents index is
                # gated on Task (ruling T9): a block about a command-line tool is
                # a description of something a Bash-less agent cannot call.
                # Before the code-mode section, which stays last.
                sections.append(
                    PromptSection(kind="extra", text=GITHUB_BLOCK)
                )
            if memory_dir:
                # Ruling MEM3. Before the code-mode section, which must stay
                # last: it is the operating rule for everything above it, this
                # section included ("write a file" means the file tools, which
                # under code mode are reached from run_python). Carried as a
                # ``memory`` section so get_context_usage() prices it under
                # "Memory files" with a per-file row naming MEMORY.md — the
                # section IS that file's contents plus its header, and the
                # AGENTS.md instruction stack (also memory-kind, gathered by
                # the compiler above) sits alongside it rather than clashing:
                # the memory dir lives outside the tree, so no file is counted
                # twice.
                sections.append(
                    PromptSection(
                        kind="memory",
                        text=_project_memory_section(str(memory_dir)),
                        path=str(Path(memory_dir) / "MEMORY.md"),
                        scope="project",
                    )
                )
            if code_mode:
                # LAST, after env and the app's extra blocks: it is the
                # operating rule for every "use the X tool" line above it.
                # Carried as a real section (not a text-only append) so the
                # leg's get_context_usage() prices it like the rest.
                sections.append(
                    PromptSection(
                        kind="extra",
                        text=_code_mode_section(),
                    )
                )
                # ... and the catalog it points at. ONE section per tool
                # (kind "tools", tool_name set) so the usage breakdown prices
                # each block under System/MCP tools like a JSON schema would
                # be; empty (no projection) simply omits it.
                for tool_text, tool_name in _code_mode_tool_sections(codemode, permit):
                    sections.append(
                        PromptSection(kind="tools", text=tool_text,
                                      tool_name=tool_name)
                    )
            if sections == list(compiled.sections):
                return compiled
            return CompiledPrompt(
                sections=tuple(sections),
                text="\n\n".join(s.text for s in sections) if sections else None,
            )
        except Exception:
            logger.exception("prompt compiler failed; falling back to raw parts")
            return None

    @staticmethod
    def _compile_prompt(
        system: Optional[str],
        append_system: Optional[str],
        options: SessionOptions,
    ) -> Optional[str]:
        """Run the fluid prompt compiler over the app's system-prompt parts.

        ``system`` (a custom/plain-string profile prompt) is the base — the
        agent's configured prompt, ruling A12. ``append_system`` (the preset
        shape's security+instructions text) rides as an extra block. The
        compiler adds fluid's own gathering: instruction files, skills index,
        environment. Never raises — a compiler failure falls back to the
        pre-compiler string so a session can always start.

        This is the DELEGATED full-replace path (the native leg calls
        ``_compile_prompt_sections`` directly for the labeled sections), hence
        ``native=False``: memory there is the claude CLI's own.
        """
        compiled = HarnessAdapter._compile_prompt_sections(
            system, append_system, options, native=False
        )
        if compiled is None:
            return system if system is not None else append_system
        return compiled.text

    @staticmethod
    def _build_store(options: SessionOptions) -> Any:
        # Derived, not configured (2026-08-13 ruling: no permanent flags — the
        # retired AIH_OMNI_STORE/AIH_OMNI_STORE_DIR env vars are ignored).
        # Whenever the app has a database — which every real deployment does,
        # a DB connection is required to boot — fluid sessions persist in
        # Postgres, exactly like the SDK path's PgSessionStore: cross-restart
        # resume, fork, and event replay from any app instance sharing the DB.
        # The only DB-less environments are unit tests, which get MemoryStore.
        # "Has a database" means either the POSTGRES_* pieces (postgres_password
        # is the one with no default) or a legacy pre-split DATABASE_URL.
        from app.core.config import settings

        if settings.database_url or settings.postgres_password:
            from app.core.provider.fluid_pg_store import PgFluidStore
            return PgFluidStore()
        return MemoryStore()

    async def _build_registry(
        self, options: SessionOptions, *, native: bool
    ) -> Optional[ToolRegistry]:
        # Escape hatch: a caller may hand a fully-built registry — its contents
        # are then entirely the caller's responsibility (builtins included).
        prebuilt = _extra_opt(options, "registry")
        if isinstance(prebuilt, ToolRegistry):
            return prebuilt

        inproc_servers = _extra_opt(options, "inproc_servers") or []
        external_mcp = _external_mcp_servers(options.mcp_servers)

        registry = ToolRegistry()
        # Constraint: native legs (anthropic / openai_responses / openrouter)
        # get fluid's builtin Read/Grep/Write/Edit/Bash/Skill/Note as first-class
        # tools — without them a native session can only reach the app's bridged
        # MCP servers and has no file/shell access at all. The delegated leg must
        # NOT get them: the claude CLI supplies its own copies. Conversely the
        # CLI must never be relied on for tools outside the Claude-subscription
        # case, which is exactly why the native leg registers its own here.
        builtins_registered = False
        if native:
            from fluid.tools.builtins import register_builtins

            register_builtins(registry)
            builtins_registered = True
            logger.info(
                "[HarnessAdapter] registered fluid builtin tools for native leg",
            )
            # Specialize builtin descriptions to this session's permitted
            # surface (fluid's cross-tool routing clauses collapse to the
            # tools that actually exist — ruling T9 for descriptions).
            try:
                from fluid.tools.builtins import refine_builtin_descriptions

                refine_builtin_descriptions(
                    registry,
                    allowed=_tool_allowlist(options),
                    disallowed=list(options.disallowed_tools or ()),
                )
            except ImportError:
                pass  # older vendored fluid without the refinement seam
            _amend_native_task_spec(registry, options)

        # The app's own in-proc SDK servers (builtins, app-config, ai-tools,
        # computer-use, provider-agent): their @tool objects were recorded per
        # instance when patch_sdk_server assembled them; re-register the same
        # tools on fluid's registry with the bridge's risk maps.
        from app.core.provider.fluid_tool_bridge import (
            DEFERRED_SDK_SERVERS,
            NATIVE_SHADOWED_BUILTINS,
            register_recorded_sdk_servers,
        )
        # Native legs run fluid's bare-named Task and Monitor builtins
        # (ruling A13) and its ToolSearch (the functional deferred-schema
        # loader for THIS registry) — the bridged mcp__builtins__ copies are
        # excluded so the model sees ONE of each. The delegated leg keeps the
        # MCP copies (the CLI can only receive external tools via
        # --mcp-config, and its own native ToolSearch serves the CLI-side
        # registry).
        #
        # Deferral is native-only too: fluid's ToolSearch is what makes a
        # deferred schema reachable, and the delegated leg's bundled socket
        # server must keep serving every spec to the CLI.
        skipped_builtins = set(NATIVE_SHADOWED_BUILTINS)
        recorded = register_recorded_sdk_servers(
            registry,
            options.mcp_servers,
            skip_tools={"builtins": skipped_builtins} if native else None,
            defer_servers=DEFERRED_SDK_SERVERS if native else None,
        )
        if recorded:
            logger.info(
                "[HarnessAdapter] bridged in-proc sdk servers onto fluid registry: %s",
                ", ".join(recorded),
            )

        for server in inproc_servers:
            registry.register_inproc(server)
        # External MCP servers are a NATIVE-leg concern only (ruling PA5). The
        # delegated leg hands the same server map to the claude CLI via
        # --mcp-config (DelegatedLoop.mcp_servers), and the CLI spawns/connects
        # them itself inside the child; nothing on that leg ever reads the
        # in-process connections (the bundled socket server only exposes
        # inproc/builtin specs, and permissions ride the CLI's stdio bridge).
        # Connecting here too would run every external server TWICE per session.
        if native:
            for name, config in external_mcp.items():
                # The app speaks the SDK's config dialect; fluid's MCP client
                # speaks ``transport``. Normalize (never mutate — the delegated
                # leg serializes the ORIGINAL map into its --mcp-config file).
                normalized = _normalize_mcp_config_for_fluid(name, config)
                if normalized is None:
                    continue  # unsupported transport; already warned about
                try:
                    await registry.connect_mcp(name, normalized)
                except Exception as exc:  # best-effort; a bad MCP server must not fail connect
                    logger.warning("[HarnessAdapter] connect_mcp(%s) failed: %r", name, exc)

        if not builtins_registered and not recorded and not inproc_servers and not external_mcp:
            return None
        return registry

    def _build_permissions(self, options: SessionOptions):
        mode = options.permission_mode or "default"
        policy = _PERMISSION_POLICY.get(mode, "on_write")
        plan_mode = mode == "plan"
        return PermissionEngine(approval_policy=policy), plan_mode

    def _wire_plan_mode_control(self, options: SessionOptions) -> None:
        """Wrap NativeLoop's PlanModeControl so Plan/StopPlanning persist + notify.

        NativeLoop already injects a controller that flips ``engine.plan_mode``.
        We wrap ``set_active`` so entering/leaving also:
          * remembers the pre-plan permission mode for restore
          * persists ``sessions.permission_mode`` (so the next send does not
            call ``set_permission_mode(default)`` and exit plan mode)
          * queues a SystemMessage the stream yields for the composer chip
        """
        engine = self._engine
        if engine is None:
            return
        ctx = getattr(engine, "ctx", None)
        extra = getattr(ctx, "extra", None)
        if not isinstance(extra, dict):
            return
        from fluid.tools.builtins.plan import PlanModeControl

        original = extra.get("plan_mode_control")
        if not isinstance(original, PlanModeControl):
            return
        current = options.permission_mode or "default"
        self._pre_plan_permission_mode = current if current != "plan" else "default"

        def set_active(active: bool) -> None:
            was = bool(original.is_active())
            original.set_active(active)
            if was == bool(active):
                return
            try:
                self._on_plan_mode_changed(bool(active))
            except Exception:  # noqa: BLE001
                # The engine flag is ALREADY flipped by the line above, so the
                # session's real mode is settled either way. Letting a
                # bookkeeping failure (a DB write, a chip event) escape would
                # surface inside fluid's tool dispatch, which turns the
                # approval the user just granted into a tool ERROR — the model
                # reads that as "the exit failed" and goes back to planning.
                logger.warning(
                    "[HarnessAdapter] plan-mode bookkeeping failed (active=%s); "
                    "the engine flag is authoritative and already set",
                    active, exc_info=True,
                )

        extra["plan_mode_control"] = PlanModeControl(
            is_active=original.is_active,
            set_active=set_active,
        )

    def _on_plan_mode_changed(self, active: bool) -> Optional[str]:
        """Keep engine policy, session binding, and the UI chip on the same mode.

        Returns the mode the session ended up bound to when a restore happened
        ("bypassPermissions" for the bypass-exit fall-through), else None.

        Linger bug: Plan flips ``engine.plan_mode`` but the composer chip still
        says ``default``. The next send therefore skips ``set_permission_mode``
        (old == new) and plan mode never ends — even after StopPlanning ran,
        if its QuestionRequest was never answered. Restoring the approval
        policy here means the rest of THIS turn is not stuck on always_ask
        after a chip-initiated plan, and persisting the binding makes the
        next send actually call set_permission_mode.
        """
        pre = self._pre_plan_permission_mode or "default"
        # Bypass users keep never_ask and their chip. Moving them to plan
        # would make the next send look like a chip-initiated plan and
        # StopPlanning would prompt — the opposite of bypass. Engine plan_mode
        # still drops write schemas; only the approval UI is skipped.
        #
        # EXCEPT on exit: if the session's own binding is ``plan`` (the user
        # picked Plan Only from the composer, then the model entered plan mode),
        # an approved exit must still restore the persisted binding + chip, or
        # the session stays bound to plan forever (the stickiness bug). The
        # fall-through below restores the binding to ``default``; the approval
        # policy is re-set to never_ask right after so bypass semantics hold.
        if pre == "bypassPermissions" and not (
            not active
            and self._options is not None
            and (self._options.permission_mode or "") == "plan"
        ):
            engine = self._engine
            if engine is not None and hasattr(engine, "plan_mode"):
                engine.plan_mode = bool(active)
            return None

        mode = "plan" if active else pre
        label = "plan" if active else mode
        engine = self._engine
        if engine is not None:
            perms = getattr(engine, "permissions", None)
            if perms is not None:
                # A bypass user exiting a chip-initiated plan restores their
                # binding to ``default`` above; the approval policy itself goes
                # straight back to never_ask — the whole point of bypass.
                if pre == "bypassPermissions" and not active:
                    perms.approval_policy = "never_ask"
                else:
                    perms.approval_policy = _PERMISSION_POLICY.get(mode, "on_write")
            if hasattr(engine, "plan_mode"):
                engine.plan_mode = bool(active)
        restored = "bypassPermissions" if (pre == "bypassPermissions" and not active) else None
        self._pending_plan_mode_events.append(
            SystemMessage(
                subtype="status",
                data={
                    "permissionMode": mode,
                    "content": f"Switched to {label} mode.",
                },
            )
        )
        restored = restored or (mode if not active else None)
        extras = (self._options.extra if self._options is not None else None) or {}
        app_session_id = extras.get("app_session_id")
        if not app_session_id:
            return restored
        try:
            from app.db.sessions import set_session_run_binding

            set_session_run_binding(str(app_session_id), permission_mode=mode)
        except Exception:
            logger.debug(
                "[HarnessAdapter] could not persist plan-mode binding",
                exc_info=True,
            )
        return restored

    def _take_plan_mode_events(self) -> list:
        events = list(self._pending_plan_mode_events)
        self._pending_plan_mode_events.clear()
        return events

    def notify_plan_mode_exited(self) -> Optional[str]:
        """Sync app state after an approved plan exit (ExitPlanMode / StopPlanning).

        The native leg flips its own flag through PlanModeControl; the delegated
        CLI leg has no such callback, so an approved ExitPlanMode panel left the
        session's persisted ``plan`` binding, the composer chip and the turn's
        permission refs pointing at plan forever — every later send re-resolved
        plan mode and the session looked stuck. Callers invoke this once the
        user APPROVES leaving plan mode.

        Returns the restored permission mode when a sync actually happened
        (``None`` when this adapter was never in plan — nothing to undo).
        """
        opts = self._options
        current = ((opts.permission_mode or "") if opts is not None else "") or ""
        # The session's OPTIONS mode is only half the signal. When the model
        # entered plan mode itself (the ``Plan`` builtin, chip still on
        # ``default``) the options never say ``plan`` — but the engine is read
        # only and ``sessions.permission_mode`` was persisted as ``plan`` on
        # the way in. Gating on the options alone skipped the sync for exactly
        # that session and left the persisted binding on ``plan``, so the NEXT
        # turn constructed the engine locked again.
        engine_in_plan = bool(getattr(self._engine, "plan_mode", False))
        if current != "plan" and not engine_in_plan:
            return None
        restored = self._on_plan_mode_changed(False)
        # Keep the options snapshot in step so requires_reconnect / hot-swap
        # comparisons see the post-exit mode, not the stale plan one. Mirrors
        # what set_permission_mode would have written.
        if opts is not None and restored and restored != "plan":
            opts.permission_mode = restored
            if restored != "bypassPermissions":
                self._pre_plan_permission_mode = restored
        # The delegated CLI leg has no PlanModeControl callback: drop its write
        # schemas right here (the native leg's set_active wrapper is a no-op —
        # the flag was already flipped before this call).
        engine = self._engine
        if engine is not None and hasattr(engine, "plan_mode"):
            engine.plan_mode = False
        # Never fall back to a blind "default": a bypassPermissions session
        # whose model entered plan mode restores nothing (bypass keeps its own
        # chip), and reporting "default" would re-pin the turn's permission
        # refs to default and start prompting a user who opted out of prompts.
        return restored or current or "default"


# ─── Module-level helpers ─────────────────────────────────────────────────────


def _resolve_claude_cmd() -> Optional[list]:
    """Command for the delegated ``claude`` CLI, or None for PATH default.

    In the Docker image the CLI is not on PATH — only the copy bundled inside
    the claude_agent_sdk package files exists — so fall back to the shared
    resolver in ``app.core.claude_cli_path`` (the same one the interrupt patcher
    uses to locate the binary it patches). Returning None lets DelegatedLoop try
    bare ``claude`` (dev machines with a global install).
    """
    import shutil

    found = shutil.which("claude")
    if found:
        return [found]
    try:
        from app.core.claude_cli_path import resolve_bundled_cli_path

        bundled = resolve_bundled_cli_path()
        if bundled is not None:
            return [str(bundled)]
    except Exception:  # pragma: no cover — resolver is best-effort
        logger.debug("bundled claude CLI resolution failed", exc_info=True)
    return None


def _system_parts(system_prompt: Any) -> tuple[Optional[str], Optional[str]]:
    """Split the SDK system-prompt shape into ``(system, append)``.

    The app's preset profiles send ``{"type": "preset", "preset":
    "claude_code", "append": <security + profile instructions>}`` — that
    append must ride the CLI's ``--append-system-prompt`` (keeping Claude
    Code's own prompt), NOT be flattened away. Plain strings are a full
    replacement (``--system-prompt``), matching legacy SDK semantics.
    """
    if system_prompt is None:
        return None, None
    if isinstance(system_prompt, str):
        return system_prompt, None
    if isinstance(system_prompt, dict):
        if system_prompt.get("type") == "preset" or "append" in system_prompt:
            return None, system_prompt.get("append") or ""
        return system_prompt.get("text") or system_prompt.get("prompt") or "", None
    return str(system_prompt), None


def _external_mcp_servers(mcp_servers: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Only external transports (stdio/sse/http) can be connected via fluid's
    McpManager. SDK in-process servers (type='sdk') are skipped — those port
    through ``fluid_tool_bridge`` into ``fluid_inproc_servers`` instead."""
    out: Dict[str, Any] = {}
    for name, cfg in (mcp_servers or {}).items():
        if isinstance(cfg, dict) and cfg.get("type") == "sdk":
            continue
        out[name] = cfg
    return out


def _prune_duplicate_plugin_mcp_servers(
    engine: Any, app_servers: Optional[Dict[str, Any]]
) -> list:
    """Drop plugin-discovered MCP servers the app already owns. App wins.

    fluid merges an enabled plugin's ``.mcp.json`` servers into ``NativeLoop``'s
    lazy-connect set (ruling CE5), skipping any name the HOST passed as
    ``mcp_servers=``. This adapter passes none — it connects the app's external
    servers eagerly onto the shared ``ToolRegistry`` inside ``_build_registry``
    — so fluid cannot see the collision and both would connect under one name.

    Name-keyed, app wins (the app's server is the one its profile/config
    deliberately configured, and its tools are what the prompt advertises).
    Returns the dropped names, for tests and logging.
    """
    if not app_servers:
        return []
    servers = getattr(engine, "_mcp_servers", None)
    if not isinstance(servers, dict) or not servers:
        return []
    dropped = [name for name in servers if name in app_servers]
    for name in dropped:
        servers.pop(name, None)
        logger.warning(
            "[HarnessAdapter] a Claude Code plugin declares MCP server %r, which "
            "this session already configures — keeping the app's server and "
            "skipping the plugin's",
            name,
        )
    return dropped


def _normalize_mcp_config_for_fluid(name: str, config: Any) -> Optional[Dict[str, Any]]:
    """Translate an SDK-shaped external MCP config into fluid's client dialect.

    The app emits SDK-shaped configs (``app/core/mcp_config.py``): stdio entries
    carry ``command`` (+ optional ``args``/``env``) and no type marker, remote
    entries carry ``{"type": "sse"|"http", "url", "headers"}``. fluid's client
    (``fluid/tools/mcp/client.py``) dispatches on ``config["transport"]``
    defaulting to ``"stdio"``, and implements only ``stdio`` and
    ``http``/``streamable_http``. So an SDK ``{"type": "http", ...}`` entry used
    to fall into the stdio branch and die on the missing ``command`` key, with
    the failure swallowed by the best-effort warning at the call site — remote
    MCP servers silently never connected on native legs.

    Returns the config to hand ``connect_mcp``, or ``None`` when the server
    cannot be connected on a fluid leg at all. The input dict is never mutated:
    the delegated leg JSON-serializes the same objects into its ``--mcp-config``
    file, which must keep the SDK shape the CLI expects.

    Gaps warned about here (upstream fluid work, ruling PA5 / plan wave W0a):
    SSE transport and per-server HTTP headers.
    """
    if not isinstance(config, dict):
        return config
    # An explicit transport means the caller already speaks fluid's dialect.
    if config.get("transport"):
        return config

    kind = str(config.get("type") or "stdio").lower()

    if kind in ("stdio", ""):
        return config

    if kind == "sse":
        logger.warning(
            "[HarnessAdapter] MCP server %r uses the SSE transport, which the fluid "
            "MCP client does not support yet — skipping it for this session. Its "
            "tools will be unavailable. Re-configure the server as streamable HTTP "
            "if it offers one.",
            name,
        )
        return None

    if kind in ("http", "streamable_http"):
        normalized = dict(config)
        normalized.pop("type", None)
        normalized["transport"] = "streamable_http"
        if config.get("headers"):
            logger.warning(
                "[HarnessAdapter] MCP server %r defines HTTP headers, which the fluid "
                "MCP client does not support yet — connecting WITHOUT them. Requests "
                "needing those headers (e.g. auth) will fail.",
                name,
            )
        return normalized

    logger.warning(
        "[HarnessAdapter] MCP server %r has unknown transport type %r — passing it "
        "through unchanged; fluid will reject it if it cannot be handled.",
        name,
        config.get("type"),
    )
    return config


_EFFORT_LEVELS = frozenset({"low", "medium", "high", "xhigh", "max"})


def _fluid_setting_sources(options: Any) -> Optional[tuple]:
    """Profile ``setting_sources`` → fluid's scope filter (user/project/local).

    ``None`` (unset) keeps fluid's default — every scope loads — which is this
    leg's historical behavior. An explicit list is filtered down to the scope
    names fluid knows; an explicit empty list runs the leg
    filesystem-isolated (no instruction files, skills, agents, or plugins from
    disk). Unknown names are dropped with a warning rather than forwarded —
    fluid raises on them, and a typo must not take the session build down.
    """
    raw = getattr(options, "setting_sources", None)
    if raw is None:
        return None
    valid = tuple(
        s for s in (str(v).strip() for v in raw) if s in ("user", "project", "local")
    )
    if len(valid) != len(list(raw)):
        logger.warning(
            "[HarnessAdapter] dropping unknown setting_sources entries from %r", raw
        )
    return valid


def _valid_effort(value: Any) -> Optional[str]:
    """``value`` when it is a recognized effort level, else None.

    Guards the engine constructors against junk config values — an
    unrecognized level would 400 the provider request (native) or fail the
    CLI spawn (delegated), so it is dropped rather than forwarded.
    """
    if isinstance(value, str) and value in _EFFORT_LEVELS:
        return value
    if value is not None:
        logger.warning("[HarnessAdapter] ignoring unrecognized effort=%r", value)
    return None


# Ruling A6: the CLI's native subagent tools are BANNED — the app's Task tool
# (mcp__builtins__Task) is the one orchestration surface. The CLI's native
# session-cron/wake tools are banned for the same reason: the CLI fires those
# wake turns between queries, so the delegated client never reads them and they
# vanish from app history and cost accounting.
DELEGATED_BANNED_TOOLS = frozenset({
    "Task",
    "Agent",
    "Workflow",
    "ScheduleWakeup",
    "CronCreate",
    "CronDelete",
    "CronList",
})


def _delegated_disallowed(options: SessionOptions) -> list:
    """``--disallowed-tools`` for the delegated (claude CLI) leg — ruling T4.

    Three sources, unioned:

    1. ``DELEGATED_BANNED_TOOLS`` (ruling A6), always.
    2. ``options.disallowed_tools`` — the agent's own blocklist. In ``none``
       mode this is ``["*"]``, which the pinned CLI honors: its deny rules are
       glob-matched, so one entry denies every tool. (Verified by reading the
       bundled binary rather than guessing — see ``app.core.cli_tool_surface``
       for the exact call chain.)
    3. For ``allow`` mode only: the CLI's own surface MINUS the allowed set.
       The CLI reads ``--allowed-tools`` as a permission GRANT, so without this
       subtraction "allow only selected" would merely stop prompting for the
       selected tools while leaving every other one callable.

    Source 3 needs the CLI's surface, which the CLI announces only in its
    ``init`` frame — i.e. after this connect already went out. (fluid >= 5248b2e
    republishes that frame as ``DelegatedSurface``; ``_translate`` feeds it to
    the cache.) Rather than reconnect, which would kill the CLI session and
    re-prime its prompt cache, the subtraction reads the last-seen surface from
    that module-level cache; on a cold cache it is skipped and only
    ``--allowed-tools`` goes out for that connect. The app's ``can_use_tool`` permission callback runs on this leg
    either way, so a cold cache degrades the surface wall, never the gate.
    """
    from app.core.cli_tool_surface import cached_cli_tools
    from app.core.tool_access import tool_permitted

    banned = set(DELEGATED_BANNED_TOOLS) | set(options.disallowed_tools or ())

    surface = _tool_allowlist(options)
    if surface:
        known = cached_cli_tools()
        if known:
            banned.update(
                name for name in known if not tool_permitted(name, surface, None)
            )
        else:
            logger.debug(
                "[HarnessAdapter] no cached claude CLI tool surface yet — "
                "allow-mode restriction rides --allowed-tools + can_use_tool "
                "for this connect"
            )
    return sorted(banned)


def _compaction_config() -> CompactionConfig:
    """This leg's context-rewriting policy.

    Tier 2 (span digests) is off unconditionally — a model-written summary must
    never replace what was actually said. Tier 1 (dropping superseded tool
    results) is behind ``settings.fluid_trim`` and defaults off.
    """
    from app.core.config import settings

    return CompactionConfig(
        enabled=False,
        trim=TrimConfig(enabled=bool(getattr(settings, "fluid_trim", False))),
    )


def _codemode_kernel_config() -> KernelConfig:
    """Kernel limits for this app's sessions — fluid's defaults, unchanged.

    ``stdout_cap`` (16,384 chars per cell) was briefly raised to fit a whole
    subagent report in one print. That bought one narrow case and taxed every
    other cell: audited sessions flooded 120KB of grep/read output per cell
    because the cap no longer corrected them. Sync Task results now carry a
    bounded head plus a ``report_path`` (see ``app.core.task_dispatch``), so
    nothing legitimate needs a cell to print more than the default allows.
    """
    return KernelConfig()


def _tool_allowlist(options: SessionOptions) -> Optional[list]:
    """This session's ENTIRE tool surface, or None for "unrestricted" (CE12).

    Reads ``SessionOptions.tool_allowlist`` — deliberately not
    ``allowed_tools``, which carries the SDK's permission vocabulary (see the
    field docs in ``provider/types.py``). Blank entries are dropped and an empty
    result degrades to None, so an accidental ``[]`` never blanks a session's
    toolset — CE12's "restriction is exactly the list" only applies to a list
    that actually names something.
    """
    raw = getattr(options, "tool_allowlist", None)
    if not raw:
        return None
    names = [str(t).strip() for t in raw if str(t).strip()]
    return names or None


def _positive_int(value: Any) -> Optional[int]:
    """``value`` as a positive int, or None (bools and junk included)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _native_max_output_tokens(options: SessionOptions) -> Optional[int]:
    """Interim per-response output cap for native legs, from profile env.

    ``NativeLoop`` otherwise runs at its ``max_tokens=8192`` default, silently
    truncating long responses with no app-side control. Until the reasoning /
    output-config wave lands, the profile env var the app already exposes
    (``CLAUDE_CODE_MAX_OUTPUT_TOKENS``, honored by the delegated CLI child) acts
    as the escape hatch here too.
    """
    env = getattr(options, "env", None)
    if not isinstance(env, dict):
        return None
    raw = env.get("CLAUDE_CODE_MAX_OUTPUT_TOKENS")
    if raw is None:
        return None
    parsed = _positive_int(raw)
    if parsed is None:
        logger.warning(
            "[HarnessAdapter] ignoring CLAUDE_CODE_MAX_OUTPUT_TOKENS=%r "
            "(not a positive integer) — keeping the engine default.",
            raw,
        )
    return parsed


# ``auth_mode`` values meaning "this provider row carries a subscription OAuth
# login, not an API key". Mirrors ``fluid_credentials._SUBSCRIPTION_AUTH_MODES``
# (imported rather than re-listed so the two can never drift): an anthropic row
# in one of these modes MUST land on the delegated CLI leg — Anthropic bans
# third-party use of subscription tokens, and ``resolve_provider_credentials``
# raises for kind="anthropic" on such a row.
def _provider_row_kind(
    provider_type: Optional[str],
    auth_mode: Optional[str],
    api_dialect: Optional[str] = None,
) -> Optional[str]:
    """Leg kind implied by a ``model_providers`` row's shape, or None.

    None means "this row shape says nothing" and the caller falls back to the
    model-string sniff.
    """
    from app.core.provider.fluid_credentials import _SUBSCRIPTION_AUTH_MODES

    ptype = (provider_type or "").strip().lower()
    mode = (auth_mode or "").strip().lower()
    dialect = (api_dialect or "").strip().lower()

    # A ChatGPT/Codex or Grok/xAI row drives OpenAI Responses (api.openai.com,
    # the Codex backend, or api.x.ai — _build_provider_client picks the URL).
    # Subscription rows can wear provider_type "chatgpt"/"grok" OR
    # "custom" + auth_mode "chatgpt"/"grok".
    if (
        ptype in ("openai", "chatgpt", "xai", "grok")
        or mode in ("chatgpt", "grok")
    ):
        return "openai_responses"
    if ptype == "openrouter":
        return "openrouter"
    if ptype == "custom":
        # A "custom" row always runs NATIVELY at the row's base_url; its
        # ``api_dialect`` picks the wire format. 'openai' → fluid's generic
        # chat/completions client (xAI-direct / Groq / vLLM endpoints).
        # Default (unset/'anthropic') → the Anthropic Messages client, the
        # historic custom-row contract (the delegated CLI pointed
        # ANTHROPIC_BASE_URL at these; the catalog reads {base_url}/v1/models).
        # Before this branch existed a custom row fell through to the
        # model-string sniff, and any non-prefixed model id ("grok-4.5")
        # landed on the REAL Anthropic API with the row's foreign key — an
        # instant, silent 401.
        return "openrouter" if dialect == "openai" else "anthropic"
    if ptype == "anthropic":
        # HARD CONSTRAINT: subscription auth stays on the delegated claude CLI.
        return "delegated" if mode in _SUBSCRIPTION_AUTH_MODES else "anthropic"
    return None


def _lookup_provider_row(provider_id: str) -> Optional[ModelProviderRow]:
    """The ``model_providers`` row for ``provider_id``, or ``None`` on any miss.

    Never raises: every caller treats a failed lookup as "no row", which each
    degrades from differently (the kind sniff falls back to the model string,
    the context-window resolution falls back to the Claude registry).
    """
    try:
        from app.db import database as db

        return db.get_model_provider(provider_id, include_token=False) or None
    except Exception:  # noqa: BLE001 — a lookup failure reads as "no row"
        logger.debug(
            "[HarnessAdapter] provider row lookup failed for %s.",
            provider_id,
            exc_info=True,
        )
        return None


def _lookup_provider_row_shape(provider_id: str) -> tuple:
    """(provider_type, auth_mode, api_dialect) for a provider row;
    (None, None, None) on any miss.

    Only reached when a session carries ``extra["provider_id"]`` WITHOUT the
    shape fields ``query_options`` normally stamps alongside it (a hand-built
    SessionOptions, a test). Never raises — a failed lookup just means the
    model-string sniff decides.
    """
    row = _lookup_provider_row(provider_id)
    if not row:
        return None, None, None
    return row.get("provider_type"), row.get("auth_mode"), row.get("api_dialect")


# fluid's discovery scope → the ``source`` label on a usage agent row (mirrors
# NativeLoop._AGENT_SOURCES; kept here so the app's combined descriptor list
# labels file/plugin agents exactly the way fluid's own auto-fill would).
_AGENT_SOURCES = {"project": "project", "global": "user", "plugin": "plugin"}


def _context_declared_agents(options: SessionOptions) -> List[Dict[str, str]]:
    """The DB agents this session declares, off ``extra["declared_agents"]``.

    ``[{"name", "description"}]`` — name and description and nothing else. The
    agent's own system prompt, tools and model are resolved by
    :mod:`app.core.task_dispatch` when the worker spawns; putting any of it in
    the CALLER's context is exactly what the index shape exists to avoid.
    """
    out: List[Dict[str, str]] = []
    for entry in (options.extra or {}).get("declared_agents") or ():
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or entry.get("agentType") or "").strip()
        if not name:
            continue
        out.append({"name": name, "description": str(entry.get("description") or "").strip()})
    return out


# ─── Code-mode prompt text ───────────────────────────────────────────────────
#
# fluid injects NOTHING about code mode into the system prompt: NativeLoop
# sends ``system`` verbatim and the compiler knows nothing about it, so the
# model's only hint is the run_python tool description. Everything else in the
# prompt — this app's security block, the agents catalog, the memory/app-config
# instructions, MCP server instructions — still says "call the X tool" about
# tools that are no longer in its schema. These strings close that gap.

# The compiler's skills header, rewritten for a schema that has no Skill tool.
CODE_MODE_SKILLS_HEADER = (
    "# Available skills\n"
    "Load a skill's full instructions before doing a task it covers: inside "
    "run_python, `from tools.Skill import Skill` and call it with the skill "
    "name."
)

# Appended to fluid's AGENTS_HEADER sentence about the Task tool.
_CODE_MODE_TASK_SUFFIX = (
    "delegated to with the Task tool, called from run_python "
    "(`from tools.Task import Task`)."
)

_CODE_MODE_SECTION_TEMPLATE = """# Code mode
Your tool schema contains only run_python, list_tools, search_tools and \
read_tool_docs. Every other tool named in these instructions — @REACHABLE@ \
— is called from Python inside run_python. The "# Tools" \
section below lists every tool with its Python signature and full \
documentation; it is the same documentation those tools carry as direct tool \
calls, so do not guess names or parameters.

## How calls work
- `from tools.<name> import <name>`, then call it as an ordinary synchronous \
function: `out = Read(file_path="/abs/path")`. Tools are not coroutines — \
never `await` them; `await` outside a function is a syntax error in the kernel.
- Pass arguments by keyword using exactly the parameter names in the \
signature. Unknown keywords raise TypeError.
- Every tool returns a string. Large results come back as a ResultRef — \
slice, search or iterate it rather than printing it whole.
- Image results (Read on an image file, screenshots) are attached to the \
cell's result as images you can see; inside the kernel the call returns only a \
`[image: ...]` caption. Read at most a few images per cell.
- Each call is permission-gated on the host exactly as a direct tool call \
would be; a denial raises HostCallDenied inside the cell.
- `mcp__server__name` is imported as `tools.name`. "Call the X tool" and "use \
the X tool" mean import and call X inside run_python. An instruction not to \
call a tool still applies.

## Batch work into one cell
Code mode exists so one run_python cell does a whole step of work. A cell that \
makes a single tool call is wrong unless the next call depends on that result \
and nothing else can be done meanwhile. Before each cell ask what else you will \
need afterwards and fetch it in the same cell: search then read every hit, read \
several files at once, run a command and inspect the related files, launch \
independent subagents together. Loop over results, filter in Python, and print \
only what you need to see:
```python
from tools.Grep import Grep
from tools.Read import Read
hits = Grep(pattern="usage", path="/repo/src").splitlines()
for path in hits[:10]:
    print(path, Read(file_path=path)[:400])
```
Wrap calls that may fail in try/except so one failure does not abort the \
batch. The kernel is persistent: variables, imports and ResultRefs survive \
across cells, so build on earlier results instead of re-fetching. Independent \
tool calls that you would otherwise issue one per turn belong in the same cell.

## Kernel limits
- Imports are limited to the allowlist: __future__, base64, bisect, \
collections, csv, dataclasses, datetime, decimal, enum, fractions, functools, \
hashlib, heapq, io, itertools, json, math, operator, pathlib, random, re, \
statistics, string, textwrap, typing, uuid, plus the `tools` and `skills` \
packages. There is no inspect, os, subprocess or asyncio — use read_tool_docs \
for signatures and the shell tool for processes.
- Network access and direct file I/O are disabled inside the kernel; go \
through the file tools.
- A cell's compute budget is `timeout` seconds (default 30); time spent \
waiting on host tool calls is not charged. A tool's own limits (for example \
the shell tool's command timeout) are set through that tool's parameters, not \
through run_python's timeout.
- Long text arguments (subagent prompts, file contents): build them in a \
variable first and never nest `\"\"\"` inside a triple-quoted string.

## Bind, then slice
A bare `print()` of a tool result forfeits its ResultRef and floods your \
context with text you did not choose. Bind every result to a name, check its \
size, and print only the slice you need:
```python
out = Read(file_path="/repo/app/server.py")
print(len(out))
print(out[:800])
```
The kernel keeps what you bound. A ref you dropped is still recoverable: \
`_refs` maps every live ResultRef id to its value, and `_` is the previous \
cell's result — recover from there instead of re-running the call.

## After you delegate, park
After `Task(mode="background")` or `Monitor`, either end the turn or work a \
file set disjoint from the child's. Never re-derive the child's brief \
yourself, and never poll for it — completion wakes you.

## One cell carries the whole step
Reads, greps, edits and the command that VERIFIES them belong in the same \
cell. Never spend a cell on a single Read. Never sleep-poll in a loop: park \
on a Monitor and let the wake bring you back.
A fixed setup->check->test recipe you already know end-to-end is ONE step, \
not three. Before ending a cell, ask whether the next cell touches the same \
file or working tree — if so it belongs in this one.
After binding a large result, do not print it in slices across cells; locate \
what you need in the same cell: `i = txt.find('needle'); print(txt[i-200:i+800])`.

## Recall budget
At most one SessionSearch per question, with `limit<=5`. Stop after two \
unproductive queries and work from the files instead. Call SessionView only \
with a specific offset that a search hit justifies.

## Discovery
list_tools returns the catalog, search_tools searches it by keyword, \
read_tool_docs returns a tool's full stub. The "# Tools" section below already \
contains all of this.

## Skills
Two different things are called skills. The "# Available skills" list above is \
instruction files, loaded through the Skill tool. The "Saved skills" that \
list_tools reports are Python modules from your continual skill store, \
imported with `from skills.<name> import <name>`. Once you have repeated a \
workflow, save it as a skill."""

# The list of reachable tool families in the opening sentence.
_CODE_MODE_REACHABLE = (
    "file, shell, search, Skill, Task, Ledger, Note, app-config, "
    "computer-use, media and MCP tools"
)

CODE_MODE_SECTION = _CODE_MODE_SECTION_TEMPLATE.replace(
    "@REACHABLE@", _CODE_MODE_REACHABLE
)


def _code_mode_section() -> str:
    """The code-mode block naming this session's reachable tool families."""
    return CODE_MODE_SECTION


# The four code-mode tools plus the skill-store pair describe themselves in the
# model's own schema, so re-documenting them in the catalog would be dead prompt.
_CODE_MODE_CATALOG_SKIP = frozenset({
    "run_python", "list_tools", "search_tools", "read_tool_docs",
    "save_skill", "delete_skill",
})


def _recall_backend_available() -> bool:
    """Whether this deployment's store can answer the recall builtins.

    Mirrors ``_build_store``: with a database the store is PgFluidStore, which
    offers every recall hook; without one (unit tests) it is MemoryStore and
    the tools are gated off — so no prompt block should describe them.
    """
    from app.core.config import settings

    return bool(settings.database_url or settings.postgres_password)


def _fluid_skills_root() -> Path:
    """The continual store's home: saved skills, the journal, memories.json."""
    from app.core.config import settings

    return (
        Path(settings.fluid_skills_dir)
        if settings.fluid_skills_dir
        else settings.effective_data_dir / "fluid" / "skills"
    )


def _tool_predicate(options: SessionOptions):
    """``(name) -> bool`` for this session's resolved tool access, or None.

    None means "no restriction" — callers skip the filtering work entirely.
    The two channels are the ones the engines already enforce: the surface
    (``tool_allowlist``, T3) and the blocklist (``disallowed_tools``, T2).
    """
    from app.core.tool_access import tool_permitted

    surface = _tool_allowlist(options)
    blocked = list(options.disallowed_tools or ())
    if not surface and not blocked:
        return None
    return lambda name: tool_permitted(name, surface, blocked)


def _permitted_tool_names(options: SessionOptions, registry: Any) -> Optional[frozenset]:
    """The names this session may actually call, or None when unrestricted.

    Ruling T9: the compiled prompt must not describe a tool the session cannot
    call, so fluid's ``compile_prompt`` takes the permitted set and drops the
    skills index when ``Skill`` is missing from it and the agents index when
    ``Task`` is. The set is the REGISTRY's names run through the predicate —
    MCP bridge names (``mcp__builtins__*`` …) included, since those are real
    entries on the same registry.

    Without a registry (the ``_compile_prompt`` helper, tests) the fluid
    builtins are registered into a throwaway one so ``Skill``/``Task`` still
    resolve honestly, rather than guessing.
    """
    permit = _tool_predicate(options)
    if permit is None:
        return None
    names: List[str] = []
    try:
        if registry is not None:
            names = [spec.name for spec in registry.list_tools(loaded_only=False)]
        else:
            from fluid.tools.builtins import register_builtins
            from fluid.tools.registry import ToolRegistry

            throwaway = ToolRegistry()
            register_builtins(throwaway)
            names = [spec.name for spec in throwaway.list_tools(loaded_only=False)]
    except Exception:  # noqa: BLE001 — an unreadable registry must not kill the turn
        logger.warning("[HarnessAdapter] could not enumerate tools for the prompt filter", exc_info=True)
        return None
    return frozenset(name for name in names if permit(name))


def _code_mode_tool_sections(codemode: Any, permit=None) -> List[Tuple[str, str]]:
    """Per-tool ``(section text, registry tool name)`` pairs for the catalog.

    One section per tool — not one blob — so fluid's context-usage breakdown
    can price each block under "System tools"/"MCP tools" by ``tool_name``,
    exactly where the equivalent JSON schema would sit on the direct-tool path.
    The FIRST section carries the ``# Tools`` heading; the joined text is
    byte-identical to the single-section form this used to build.

    Ruling of 2026-08-21. A code-mode schema carries four tools, so the model
    never sees the descriptions the JSON-tool path sends — a live run guessed
    parameter names because nothing in the prompt held the real ones. This is
    that missing text: same descriptions, same parameter docs, keyed to the
    Python call shape the kernel actually exposes.

    Built from the session's own projection (the snapshot behind ``list_tools``
    and the VFS stubs), so the catalog and the importable modules can never
    disagree. Best-effort: a codemode object without a projection (a test
    double, a future refactor) returns [] and the section is simply skipped
    rather than failing the prompt.

    ``permit`` is this session's tool predicate (ruling T9), and it is load
    bearing here rather than merely belt-and-braces: ``NativeLoop.__init__``
    installs the session's ``tool_filter`` onto the code-mode session, but the
    prompt is compiled BEFORE the engine is constructed (the code-mode rewrites
    depend on whether the session actually came up), so the projection this
    reads is still unfiltered at that moment.
    """
    try:
        from fluid.codemode.projection import _annotation, _param_doc, _py_ident
    except Exception:  # noqa: BLE001 — no projection helpers, no section
        return []
    build = getattr(codemode, "_projection", None)
    if not callable(build):
        return []
    try:
        projection = build()
        leaves = list(projection.leaves())
    except Exception:  # noqa: BLE001 — a broken registry must not kill the turn
        logger.warning("[HarnessAdapter] code-mode tool catalog FAILED", exc_info=True)
        return []

    blocks: list[str] = []
    for leaf in leaves:
        spec = projection.spec_for(leaf)
        if spec is None or leaf in _CODE_MODE_CATALOG_SKIP:
            continue
        if getattr(spec, "name", None) in _CODE_MODE_CATALOG_SKIP:
            continue
        if permit is not None and not permit(getattr(spec, "name", None) or leaf):
            continue
        schema = getattr(spec, "input_schema", None) or {}
        props: dict = schema.get("properties") or {}
        required = list(schema.get("required") or [])
        # Signature exactly as projection.stub_source renders it: required
        # params first, then optionals as ``| None = None``.
        ordered = [p for p in props if p in required]
        ordered += [p for p in props if p not in required]
        params: list[str] = []
        for jname in ordered:
            ann = _annotation(props.get(jname) or {})
            pyname = _py_ident(jname)
            params.append(
                f"{pyname}: {ann}" if jname in required
                else f"{pyname}: {ann} | None = None"
            )
        lines = [
            f"## {leaf}",
            f"`from tools.{leaf} import {leaf}`",
            f"`{leaf}({', '.join(params)})`",
        ]
        description = (getattr(spec, "description", "") or "").strip()
        if description:
            lines.append(description)
        if not getattr(spec, "code_mode_allowed", True):
            lines.append(
                "Not callable from code mode — use it as a direct tool call."
            )
        if ordered:
            lines.append("Parameters:")
            for jname in ordered:
                # Same renderer the VFS stub docstrings use, so a parameter's
                # enum/default text can never drift between prompt and stub.
                lines.append("- " + _param_doc(
                    _py_ident(jname),
                    props.get(jname) or {},
                    required=jname in required,
                ))
        blocks.append(("\n".join(lines), str(getattr(spec, "name", "") or leaf)))

    if not blocks:
        return []
    # The heading rides the first tool's section, so the joined text is
    # byte-identical to the old single-blob catalog.
    blocks[0] = ("# Tools\n" + blocks[0][0], blocks[0][1])
    return blocks



# Appended (native leg, Bash permitted) when the install's gh CLI is signed in,
# alongside fluid's own STEERING_BLOCK. Short by design: the model already knows
# how to drive gh, it just has no way to know the binary is there and
# authenticated, and without that it falls back to raw git or to asking.
# Harness-owned recall guidance. Agent profiles replace fluid's base prompt
# wholesale (ruling A12), so fluid's own "search past sessions" bullets never
# reach an AI Shuffle session — and recall is a harness capability the user
# should never have to explain in an agent prompt. Appended to every native
# compilation whose surface carries the recall tools; the tool descriptions
# teach the mechanics, this block teaches WHEN.
RECALL_BLOCK = """# Past sessions are your memory
Every session in this app is stored and searchable — earlier work is never \
lost, only out of context. SessionSearch with no query lists recent sessions \
(titles, dates, last reply): start there for "what were we working on?". \
SessionSearch with keywords finds older work by content. SessionView opens a \
session — by default its tail, where what was delivered and what was left \
open live. Reach for them whenever the user references work you cannot see \
("last time", "that script we wrote", "continue where we left off") — a \
couple of quick lookups beat asking the user to repeat themselves. Recalled \
content is history, not live state: verify against the current files before \
acting on it."""

GITHUB_BLOCK = """# GitHub
The gh CLI is installed and authenticated on this machine. Reach GitHub \
through it in Bash — pull requests (gh pr create/view/checks), issues (gh \
issue), CI runs (gh run list / gh run watch), and API reads (gh api) — rather \
than reconstructing that state from raw git or asking the user. Plain git \
remains the tool for local version control: status, diff, commit, branch, \
push.

CI takes minutes; never wait in Bash. Park on a Monitor whose loop ends on \
ANY terminal state, not just the green one:
Monitor(command="while true; do s=$(gh pr checks 123 --json bucket --jq \
'.[].bucket' || true); case $s in *pending*) sleep 30;; *) echo checks: $s; \
break;; esac; done")

Worktrees share the main checkout's stash stack, so a bare `git stash` \
followed by `git stash pop` can drop ANOTHER session's work into your tree. \
Never use either bare form: stash with `git stash push -u -m "<tag>"` and \
restore that entry by its SHA, or prefer a WIP commit you can reset.

Re-read `git worktree list` immediately before any bulk branch delete — \
another session may have created one since your listing, and `git branch \
--merged` will not tell you."""


def _model_guidance_blocks(model: Optional[str]) -> Tuple[str, ...]:
    """fluid's model-family guidance blocks for ``model``, or ``()``.

    fluid writes these for the families that narrate instead of calling tools
    and that skip read-back verification (gpt/grok/glm and friends); the
    Claude family is deliberately excluded upstream and resolves to no blocks
    here. Only ``compose_base_prompt`` consumes them upstream and this app
    never calls it, so this is the app's own injection point.

    Import-guarded: a vendored fluid without the symbol degrades to no blocks
    rather than taking down every native session's prompt compilation.
    """
    if not model:
        return ()
    try:
        from fluid.cli.prompt import model_guidance_blocks
    except Exception:  # noqa: BLE001 — vendored fluid may predate the symbol
        logger.debug("[HarnessAdapter] fluid model_guidance_blocks unavailable")
        return ()
    try:
        return tuple(model_guidance_blocks(model))
    except Exception:  # noqa: BLE001
        logger.warning(
            "[HarnessAdapter] model guidance resolution failed for %r", model,
            exc_info=True,
        )
        return ()


# Ruling MEM3: the native leg had no memory surface at all — no CLI behind it
# means nothing tells the model a memory dir exists, so nothing was ever
# written or read. This section is that surface. It ships as a ``memory``
# section pathed at the real MEMORY.md, so the context-usage breakdown lists
# it under "Memory files" next to the AGENTS.md stack.
#
# The inlined index is prompt prefix on every turn, so it is capped. 24k chars
# is roughly 6k tokens — far more than a healthy index needs, and small enough
# that a runaway MEMORY.md cannot eat the window.
_MEMORY_INDEX_CHAR_CAP = 24_000

_PROJECT_MEMORY_HEADER = """# Project memory
Your memory for this project lives in {memory_dir}. MEMORY.md in that \
directory is the index, and it is loaded at the start of every session — its \
current contents are below.

Save one fact per file: a short .md file with frontmatter (name, description, \
metadata.type = user | feedback | project | reference), then add a one-line \
pointer to MEMORY.md so the next session can find it:

- [Title](file.md) — the hook, in a few words

Update the existing file when a fact changes; do not write a second file on \
the same subject. Keep the index in step with the files.

Worth saving: the user's preferences and rulings, how this system actually \
behaves, decisions and the reasons behind them, traps that cost you time. Not \
worth saving: anything the repository already records — code, docs and commit \
history are there to be read."""

_MEMORY_INDEX_EMPTY = "(empty — nothing saved yet)"


def _project_memory_section(memory_dir: str) -> str:
    """The ``# Project memory`` prompt text, with MEMORY.md inlined.

    Reading the index at compile time is deliberate: the model gets the current
    memory as context instead of having to spend a tool call on it, exactly as
    the CLI leg does. A missing or unreadable file is stated as empty rather
    than hidden — "there is a dir, it holds nothing yet" is the useful answer.
    """
    index = _MEMORY_INDEX_EMPTY
    try:
        text = (Path(memory_dir) / "MEMORY.md").read_text(encoding="utf-8").strip()
        if text:
            index = (
                text
                if len(text) <= _MEMORY_INDEX_CHAR_CAP
                else text[:_MEMORY_INDEX_CHAR_CAP] + "\n\n[truncated]"
            )
    except (OSError, UnicodeDecodeError):
        pass
    header = _PROJECT_MEMORY_HEADER.format(memory_dir=memory_dir)
    return f"{header}\n\n## MEMORY.md\n{index}"


def _code_mode_skills_text(text: str) -> str:
    """Swap the compiler's skills header for its code-mode wording.

    Only the header line is touched; the index body (one line per skill) is
    left exactly as compiled. A header fluid has since reworded leaves the
    section untouched rather than producing a mangled one.
    """
    try:
        from fluid.context.compiler import SKILLS_HEADER
    except Exception:  # noqa: BLE001
        return text
    return text.replace(SKILLS_HEADER, CODE_MODE_SKILLS_HEADER, 1)


def _agents_header(code_mode: bool = False) -> str:
    """fluid's own "Available agent types" heading, with a literal fallback.

    Shared by the index builder and the delegated-leg idempotence check so the
    two can never drift apart (a header the check does not recognise would
    double the block on the CLI's prompt). ``code_mode`` only extends the
    sentence about the Task tool with how Task is reached — the FIRST line,
    which is all the idempotence check compares, is untouched either way.
    """
    try:
        from fluid.context.compiler import AGENTS_HEADER

        header = AGENTS_HEADER
    except Exception:  # noqa: BLE001
        header = "# Available agent types"
    if code_mode:
        header = header.replace(
            "delegated to with the Task tool.", _CODE_MODE_TASK_SUFFIX, 1
        )
    return header


def _with_agents_index(
    system: Optional[str], append: Optional[str], index: Optional[str]
) -> Tuple[Optional[str], Optional[str]]:
    """Attach the agents index to the delegated leg's prompt parts.

    ``DelegatedLoop`` spends exactly ONE of the two on argv —
    ``--append-system-prompt`` when it has append text, else
    ``--system-prompt`` — so the block must ride the part the CLI will actually
    read: the append text when there is one (a preset profile, the common
    shape), else a plain-string full replacement, else a bare append that
    leaves the CLI's own preset intact.

    Idempotent by header check: a part that already advertises agents is
    returned untouched, so neither a reconnect nor a profile prompt that
    already carries an index can produce two blocks.
    """
    if not index:
        return system, append
    header = _agents_header().split("\n", 1)[0]
    if append:
        return system, (append if header in append else f"{append}\n\n{index}")
    if system:
        return (system if header in system else f"{system}\n\n{index}"), append
    return system, index


def _agent_context(
    options: SessionOptions, *, code_mode: bool = False
) -> Tuple[Optional[str], List[Dict[str, Any]]]:
    """``(agents index text, host-declared descriptors)`` for a fluid leg.

    fluid's own ``compiler.agents_index`` only knows what it can find on disk
    (``.claude/agents`` + plugin agents, ruling CE6), and its ``agents=`` engine
    input feeds the context-usage breakdown, not the prompt. The app's DB agents
    have to join that index for the model to know they exist, so the index is
    assembled HERE from both sources and the compiler's own agents section is
    turned off (see ``_compile_prompt_sections``) — one "Available agent types"
    block, one line per agent, ``- name: description``, bodies left on disk.

    A DB agent WINS a name collision with a file agent, matching the dispatcher's
    host-first resolution (CE6) — the model must not read a description for a
    definition that is not the one it would get.

    The second element is the ``NativeLoop(agents=...)`` descriptor list: the
    declared DB agents (priced by their description, which is all that entered
    the context) plus the discovered file/plugin definitions with their bodies —
    the same records fluid would have auto-filled, which passing ``agents=`` at
    all would otherwise suppress.

    ``code_mode`` only reaches the header (see :func:`_agents_header`): the
    index body and the descriptors are identical either way.

    Never raises: discovery is best-effort context (CE11).
    """
    declared = _context_declared_agents(options)
    try:
        from fluid.context.agents import discover_agents

        discovered = list(discover_agents(options.cwd or "."))
    except Exception:  # noqa: BLE001 — never break session connect
        logger.warning("agent discovery failed; index falls back to DB agents", exc_info=True)
        discovered = []

    declared_names = {entry["name"] for entry in declared}
    lines: Dict[str, str] = {entry["name"]: entry["description"] for entry in declared}
    for definition in discovered:
        lines.setdefault(definition.name, definition.description or "")

    index: Optional[str] = None
    if lines:
        body = "\n".join(f"- {name}: {lines[name]}" for name in sorted(lines))
        index = f"{_agents_header(code_mode)}\n{body}"

    descriptors: List[Dict[str, Any]] = [
        {"agentType": entry["name"], "source": "app", "description": entry["description"]}
        for entry in declared
    ]
    for definition in discovered:
        if definition.name in declared_names:
            continue
        descriptors.append({
            "agentType": definition.name,
            "source": _AGENT_SOURCES.get(definition.scope, definition.scope or "app"),
            "text": definition.body,
        })
    return index, descriptors


def _native_context_window(kind: str, options: SessionOptions) -> Optional[int]:
    """Context window (tokens) for a NATIVE leg's model, or ``None`` if unknown.

    Feeds ``NativeLoop(context_window=...)``, which is what lets fluid's
    ``get_context_usage()`` report ``maxTokens``/``percentage`` and a
    "Free space" row. ``None`` is the honest answer for an unknown model:
    fluid then reports ``maxTokens: None`` rather than inventing a window.

    NOT a new source of truth — this reuses the app's existing resolution so
    the number the native leg prices against is the same one the UI shows and
    the delegated leg gets pinned to:

      * a bound ``model_providers`` row → ``resolve_provider_context_window``
        (the model's own ``models_json`` entry → the preset roster → the row's
        column), identical to ``environment.apply_provider_env``'s pin and the
        registry payload;
      * otherwise a registered Claude slug → ``model_registry.get_context_window``.

    Resolves against ``options.model`` — the REGISTRY value ("opus"), NOT the
    ``resolve_api_model_id`` output handed to the loop: both registries key on
    the slug, and the API id ("claude-opus-4-8") would miss every lookup.
    """
    selected_window = (options.extra or {}).get("context_window")
    if selected_window:
        try:
            window = int(selected_window)
        except (TypeError, ValueError):
            window = 0
        if window > 0:
            return window

    provider_id = (options.extra or {}).get("provider_id")
    if provider_id:
        window = resolve_provider_context_window(
            _lookup_provider_row(str(provider_id)), options.model
        )
        if window is not None:
            return window
    # Fall through for env-credentialed native Claude (no row) and for a row
    # that knows nothing about this model but still runs a Claude slug.
    if get_model(options.model) is not None:
        return get_context_window(options.model)
    return None


def _amend_native_task_spec(registry: ToolRegistry, options: SessionOptions) -> None:
    """Replace Fluid's static Task spec with the provider-scoped app contract.

    Fluid owns the bare tool and handler; AI Shuffle owns its profile registry
    and provider bindings. The native and delegated surfaces therefore receive
    the same eligible subagent roster and neither accepts model-authored
    provider or model selection.
    """
    try:
        provider_id = (options.extra or {}).get("provider_id")
        profile_id = (options.extra or {}).get("profile_id")
        from app.core.agent_provider_models import (
            build_task_contract,
            file_subagent_targets,
        )

        try:
            from fluid.context.agents import discover_agents

            discovered_registry = {
                definition.name: definition
                for definition in discover_agents(options.cwd or ".")
            }
        except Exception:  # noqa: BLE001 — DB roster still remains usable
            logger.debug("Could not discover file subagents for Task spec", exc_info=True)
            discovered_registry = {}
        contract = build_task_contract(
            profile_id,
            provider_id,
            file_subagent_targets(discovered_registry, options.model),
        )
        import dataclasses

        from fluid.tools.builtins.task import SPEC as _TASK_SPEC
        from fluid.tools.builtins.task import handler as _task_handler

        registry.register_builtin(
            dataclasses.replace(
                _TASK_SPEC,
                description=contract.description,
                input_schema=contract.input_schema,
            ),
            _task_handler,
        )
    except Exception:  # noqa: BLE001 — must never break session connect
        logger.warning(
            "[HarnessAdapter] failed to install provider-scoped native Task "
            "spec; leaving the vendor spec as-is.",
            exc_info=True,
        )


# NOTE: the former ``_amend_native_mutation_specs`` (which overwrote fluid's
# Bash/Write/Edit descriptions with one-line summaries) is gone: fluid's own
# tool contracts now carry richer guidance than the amendments did, including
# the invoke-immediately mutation rule and the environment-block Shell pointer,
# so amending here would only downgrade them.


def _decide_provider_kind(options: SessionOptions) -> str:
    """Return one of: 'delegated' | 'anthropic' | 'openai_responses' | 'openrouter'."""
    engine_hint = _extra_opt(options, "engine")
    if engine_hint == "delegated":
        return "delegated"
    if engine_hint == "native":
        # fall through to provider selection below
        pass

    provider_hint = (
        _extra_opt(options, "provider")
        or os.environ.get("AIH_FLUID_PROVIDER")
        # legacy env name from before the omni -> fluid harness rename
        or os.environ.get("AIH_OMNI_PROVIDER")
    )
    if provider_hint in ("anthropic", "openai_responses", "openrouter"):
        return provider_hint
    if provider_hint == "delegated":
        return "delegated"

    # The BOUND PROVIDER ROW outranks the model-string sniff below (PA13/W-R4).
    # The sniff reads a model id in isolation, so every cross-provider pick it
    # can't see the provider for lands on the wrong leg: "anthropic/claude-3.5"
    # on an OpenRouter row sniffs as an OpenRouter slug only by luck, a bare
    # "claude-3.5-sonnet" on that same row sniffs as the DELEGATED claude CLI,
    # and a slugged model on an OpenAI row sniffs as OpenRouter. The row's own
    # provider_type is authoritative when we have it; the sniff stays as the
    # fallback for native sessions and rows of unknown shape.
    bound_provider_id = options.extra.get("provider_id")
    if bound_provider_id:
        provider_type = options.extra.get("provider_type")
        auth_mode = options.extra.get("provider_auth_mode")
        api_dialect = options.extra.get("provider_dialect")
        if not provider_type:
            provider_type, auth_mode, api_dialect = _lookup_provider_row_shape(
                str(bound_provider_id)
            )
        row_kind = _provider_row_kind(provider_type, auth_mode, api_dialect)
        if row_kind:
            return row_kind

    model = (options.model or "").lower()
    # The app selects Claude models by SHORT ALIAS ("fable"/"opus"/"sonnet"/
    # "haiku"), which does not start with "claude" — those sessions used to fall
    # through to the plain `return "anthropic"` below, i.e. the raw Anthropic API
    # client with no API key, which 401s instantly and kills the turn. Resolve
    # the alias through the model registry first, then keep the raw "claude-*"
    # id check for full model ids.
    from app.core.model_registry import get_model_family

    if get_model_family(options.model) is not None or model.startswith("claude"):
        # Subscription default: the claude CLI (delegated). Set
        # extra["fluid_provider"]="anthropic" for the direct-API-key native path.
        return "delegated" if engine_hint != "native" else "anthropic"
    if model.startswith(("gpt", "o1", "o3", "o4", "codex")):
        return "openai_responses"
    if "/" in model:  # vendor/model OpenRouter slug, e.g. "meta-llama/llama-3.1-70b"
        return "openrouter"
    return "anthropic"


def provider_kind_for_sdk_options(sdk_options: Any) -> str:
    """Leg decision for a built ``ClaudeAgentOptions``, WITHOUT connecting.

    Callers outside the adapter (memory_sync, which has to pick the leg's
    project-memory dir before the turn starts — ruling MEM2) need the same
    answer ``connect()`` will reach. Rather than copy the decision table, this
    projects the same ``aih_*`` fields ``connect_with_sdk_options`` forwards
    onto a throwaway ``SessionOptions`` and defers to
    :func:`_decide_provider_kind`, which stays the single source of truth.
    """
    opts = SessionOptions(model=getattr(sdk_options, "model", None) or DEFAULT_MODEL)
    provider_id = getattr(sdk_options, "aih_provider_id", None)
    if provider_id:
        opts.extra["provider_id"] = provider_id
        for attr, key in (
            ("aih_provider_type", "provider_type"),
            ("aih_provider_auth_mode", "provider_auth_mode"),
            ("aih_provider_dialect", "provider_dialect"),
        ):
            value = getattr(sdk_options, attr, None)
            if value:
                opts.extra[key] = value
    return _decide_provider_kind(opts)


def _chat_completions_url(base: str) -> str:
    """Endpoint-root → full chat/completions URL.

    Accepts either a bare root ("https://api.x.ai") or a root that already
    carries the ``/v1`` segment ("https://api.x.ai/v1" — a common way users
    enter OpenAI-compatible bases); both compose to ``.../v1/chat/completions``.
    """
    trimmed = base.rstrip("/")
    if trimmed.endswith("/v1"):
        trimmed = trimmed[: -len("/v1")]
    return trimmed + "/v1/chat/completions"


def _responses_url(base: str) -> str:
    """Endpoint-root → full Responses API URL.

    Same composition as :func:`_chat_completions_url` but ending in
    ``/v1/responses``. ``https://api.x.ai`` and ``https://api.x.ai/v1`` both
    become ``https://api.x.ai/v1/responses``.
    """
    trimmed = base.rstrip("/")
    if trimmed.endswith("/v1"):
        trimmed = trimmed[: -len("/v1")]
    return trimmed + "/v1/responses"


def _require_env_credential(env_var: str, kind: str) -> None:
    """Dev-mode fallback guard: no provider row bound, so an env key is required.

    Raising here turns "no credentials anywhere" into a user-visible config
    error at connect time instead of a bare upstream 401 mid-turn.
    """
    if os.environ.get(env_var):
        return
    raise ProviderCredentialError(
        f"No model provider is bound to this session and {env_var} is not set, "
        f"so the {kind} provider has no credentials configured. Bind the session "
        f"to a model provider (Settings → Model Providers) or set {env_var}."
    )


def _usage_capture_http_client(provider_id: str, use_codex_backend: bool) -> httpx.AsyncClient:
    """An ``httpx.AsyncClient`` that passively captures usage/quota headers and
    records upstream rate limits.

    Native legs talk to the upstream provider directly (the fluid client owns the
    HTTP call), so injecting this client is what keeps
    ``provider_usage_snapshots`` fresh: httpx fires the ``response`` event hook
    with headers populated before the streamed body is consumed
    (``capture_codex_headers`` for the Codex backend, ``capture_openai_headers``
    for the OpenAI platform one).

    Deliberately no status-code guard on the capture: ``capture_*`` already
    no-ops when no allowlisted header is present, and error responses (notably
    429) carry the freshest quota headers of all. ``timeout=600.0`` matches the
    vendored fluid client's own default, which it bypasses whenever a client is
    injected.

    RATE-LIMIT BREAKER (ruling D2, 2026-08-12). This hook is also the app's
    replacement seam for the retired translator gateway's 429 detection: it sees
    every native-leg response, and ``provider_id`` — the session's bound
    ``model_providers`` row id, i.e. the exact key the breaker is scoped on — is
    already in scope here. An HTTP 429 trips
    :mod:`app.core.rate_limit_breaker` install-wide for that provider, honouring
    ``Retry-After``, exactly as the gateway's ``_note_rate_limit`` did. ONLY 429
    trips it (401 is a credential/rotation signal, 5xx a transient fault).
    ACCEPTED GAP: the delegated (claude CLI) leg makes its HTTP calls inside the
    CLI child process and is therefore not covered.

    Both side effects are best-effort and swallow every exception — neither
    capture nor breaker bookkeeping may ever break a turn. ``provider_usage`` is
    imported inside the hook (mirroring the legacy call sites) to keep the
    module import graph cycle-free.
    """

    async def _on_response(resp: httpx.Response) -> None:
        try:
            from app.core import provider_usage

            if use_codex_backend:
                provider_usage.capture_codex_headers(provider_id, resp.headers)
            else:
                provider_usage.capture_openai_headers(provider_id, resp.headers)
        except Exception:  # noqa: BLE001 — capture must never break a turn
            pass

        if resp.status_code == 429:
            try:
                from app.core import rate_limit_breaker

                rate_limit_breaker.record_rate_limited(
                    provider_id, resp.headers.get("retry-after")
                )
            except Exception:  # noqa: BLE001 — bookkeeping must never break a turn
                pass

    return httpx.AsyncClient(timeout=600.0, event_hooks={"response": [_on_response]})


async def _build_provider_client(
    kind: str, options: SessionOptions
) -> tuple[Any, Optional[httpx.AsyncClient]]:
    """Construct the fluid native client for ``kind``, authenticated.

    Returns ``(client, injected_http_client)``. The second element is any
    ``httpx.AsyncClient`` injected into the fluid client (the usage-header
    capture client on openai_responses rows; the Bearer-header client on
    custom/base_url anthropic rows), or ``None`` when no client was injected —
    the caller owns closing it (the fluid client does not, since it did not
    create it).

    Credential resolution order (see ``fluid_credentials``):
      1. the session's bound ``model_providers`` row (``extra["provider_id"]``,
         threaded from ``query_options`` through ``connect_with_sdk_options``) —
         decrypted API key, or a Codex OAuth auth object for a
         ChatGPT-subscription row;
      2. otherwise the fluid client's own env-var fallback (dev mode), which is
         required to be present.

    A bound-but-uncredentialed provider raises ``ProviderCredentialError``
    (a ``ValueError``) rather than yielding a client that 401s on first use.
    """
    provider_id = options.extra.get("provider_id")
    creds = None
    if provider_id:
        creds = await resolve_provider_credentials(kind, str(provider_id))

    if kind == "openai_responses":
        if creds is not None:
            http_client = _usage_capture_http_client(
                str(provider_id), creds.use_codex_backend
            )
            if creds.use_codex_backend:
                base_url = CODEX_RESPONSES_URL
            elif creds.base_url:
                base_url = _responses_url(creds.base_url)
            else:
                base_url = OPENAI_RESPONSES_URL
            client = OpenAIResponsesClient(
                api_key=creds.api_key,
                auth=creds.auth,
                base_url=base_url,
                http_client=http_client,
            )
            return client, http_client
        _require_env_credential("OPENAI_API_KEY", kind)
        return OpenAIResponsesClient(), None
    if kind == "openrouter":
        # fluid's OpenRouterClient is the generic OpenAI chat/completions
        # client — a row-level base_url redirects it (custom rows with
        # api_dialect='openai': xAI-direct / Groq / vLLM). For openrouter-type
        # rows the stored base ("https://openrouter.ai/api") composes to the
        # client's own default URL, so honoring it is behavior-preserving.
        #
        # The OpenRouter client ships with retries OFF (RetryPolicy(max_retries=0))
        # out of billing-idempotency caution; we opt in explicitly so this leg
        # rides out transient 429/5xx the way the anthropic and openai_responses
        # legs already do by default. fluid only retries before the first chunk,
        # so an opted-in retry can never double-stream a partial message.
        pricing = _openrouter_pricing(options)
        if creds is not None:
            if creds.base_url:
                return (
                    OpenRouterClient(
                        api_key=creds.api_key,
                        base_url=_chat_completions_url(creds.base_url),
                        pricing=pricing,
                        retry=RetryPolicy(),
                    ),
                    None,
                )
            return (
                OpenRouterClient(
                    api_key=creds.api_key, pricing=pricing, retry=RetryPolicy()
                ),
                None,
            )
        _require_env_credential("OPENROUTER_API_KEY", kind)
        return OpenRouterClient(pricing=pricing, retry=RetryPolicy()), None
    # anthropic — API-key rows only. A Claude Pro/Max SUBSCRIPTION OAuth token is
    # never routed here (that path stays delegated-CLI only); resolve_provider_
    # credentials refuses such a row outright. A row-level ``base_url`` (every
    # ``custom`` row, an anthropic row fronted by a proxy) redirects the client
    # to that endpoint using the same convention the delegated CLI applies to
    # ANTHROPIC_BASE_URL: append ``/v1/messages``.
    if creds is not None:
        if creds.base_url:
            # Gateways behind a custom base_url were authenticated by the CLI
            # via ``Authorization: Bearer`` (ANTHROPIC_AUTH_TOKEN); fluid's
            # client only sends ``x-api-key``. Inject an httpx client carrying
            # the Bearer header too so either scheme works. Returned as the
            # second element — the adapter owns closing an injected client.
            bearer_http = httpx.AsyncClient(
                timeout=600.0,
                headers={"Authorization": f"Bearer {creds.api_key}"},
            )
            return (
                AnthropicClient(
                    api_key=creds.api_key,
                    base_url=creds.base_url.rstrip("/") + "/v1/messages",
                    http_client=bearer_http,
                ),
                bearer_http,
            )
        return AnthropicClient(api_key=creds.api_key), None
    _require_env_credential("ANTHROPIC_API_KEY", kind)
    return AnthropicClient(), None


def _openrouter_pricing(options: SessionOptions) -> Dict[str, Dict[str, float]]:
    """Sanitized per-token pricing retained from the bound provider catalog."""
    provider_id = (options.extra or {}).get("provider_id")
    row = _lookup_provider_row(str(provider_id)) if provider_id else None
    out: Dict[str, Dict[str, float]] = {}
    for entry in (row or {}).get("models_json") or []:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
            continue
        raw = entry.get("pricing")
        if not isinstance(raw, dict):
            continue
        parsed: Dict[str, float] = {}
        for key in ("prompt", "completion", "input_cache_read", "input_cache_write"):
            try:
                value = float(raw[key])
            except (KeyError, TypeError, ValueError):
                continue
            if value >= 0 and math.isfinite(value):
                parsed[key] = value
        if parsed:
            out[entry["id"]] = parsed
    return out
