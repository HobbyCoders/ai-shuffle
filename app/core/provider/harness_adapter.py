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
    when positive; fluid's default (50) otherwise. The delegated leg's CLI
    enforces its own turn budget, so nothing is passed there.
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
  * ``tool_allowlist``       → native ``NativeLoop(allowed_tools=...)``: the
    session's ENTIRE tool surface when set, None = unrestricted (CE12). Set for
    a worker dispatched onto a file/plugin agent whose definition carries a
    ``tools:`` list (``app.core.task_dispatch`` → ``overrides["tool_allowlist"]``
    → ``ClaudeAgentOptions.aih_tool_allowlist``). On the delegated leg it rides
    ``--allowed-tools``, which the claude CLI reads as PERMISSION rules — that
    leg therefore grants rather than restricts, because the CLI owns its own
    tool surface and fluid cannot filter it.

DEGRADED / NOT YET HONORED (documented gaps — mechanical follow-up):
  * ``allowed_tools`` — the SDK's PERMISSION allowlist (every MCP tool name plus
    the profile's legacy list) is still enforced only by the app's own
    ``can_use_tool`` callback. It is NOT projected onto the engines: fluid's
    ``allowed_tools`` is a tool-SURFACE restriction, and this list is empty of
    builtins whenever a tools preset is active, so projecting it would delete
    Read/Bash/Edit from every native session. The surface channel is
    ``tool_allowlist`` above.
  * ``thinking`` — fluid manages thinking via provider caps, not passed through.
  * ``hooks`` / ``include_hook_events`` — fluid has its own HookBus; app SDK hooks not wired.
  * ``extra_args`` / ``setting_sources`` — SDK/CLI-specific, ignored.
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
    resolved from ``~/.claude/plugins``; their skills and commands join the
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
  * ``CLAUDE.md`` ``@path`` imports, ``CLAUDE.local.md`` and codex
    ``AGENTS.override.md`` are expanded/collected by the instruction gatherer
    (rulings CE2/CE3) — so the compiled prompt this adapter builds already
    contains them.
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
import logging
import os
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
from fluid.engine.native import NativeLoop
from fluid.engine.delegated import DelegatedLoop
from fluid.events.ops import (
    Interrupt,
    RespondPermission,
    RespondQuestion,
    SetModel,
    SetPermissionMode,
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
        # last pending user-question request_id (fluid core does not yet emit a
        # question event; kept for forward-compat with RespondQuestion).
        self._pending_question_id: Optional[str] = None
        self._is_delegated: bool = False
        self._provider_kind: Optional[str] = None
        # ─ translation state (see _translate) ─
        # Model reported by the current turn's TurnStarted (AssistantMessage.model).
        self._current_model: Optional[str] = None
        # Open streaming blocks: index -> block_type, and accumulated delta text.
        self._block_types: Dict[int, str] = {}
        self._block_buffers: Dict[int, list] = {}
        # Last fluid Usage reading, attached to the next synthesized
        # AssistantMessage (the loops read per-call usage off that message).
        self._pending_usage: Optional[Dict[str, Any]] = None
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
        kind = _decide_provider_kind(options)
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
                delegated_system = self._compile_prompt(system, append_system, options)
                delegated_append = None
            else:
                # Append mode still owes the model the delegation catalog. The
                # CLI builds its own prompt here, so without this a
                # Claude-subscription caller would be the ONE leg that never
                # learns which agents exist — it could not name a target for
                # the Task tool at all. Same block as the native leg, attached
                # to whichever part the CLI actually reads.
                agents_index, _ = _agent_context(options)
                delegated_system, delegated_append = _with_agents_index(
                    system, append_system, agents_index
                )
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
                # Ruling A6: the CLI's native subagent tools are BANNED — the
                # app's Task tool (mcp__builtins__Task here) is the one
                # orchestration surface. Profile-level disallows ride along.
                # The CLI's native session-cron/wake tools (ScheduleWakeup and
                # the CronCreate family) are banned for the same reason: the CLI
                # fires those wake turns between queries, so the delegated client
                # never reads them and they vanish from app history and cost
                # accounting. The bridged app builtin
                # mcp__builtins__ScheduleWakeup (wakeup_service-backed) is the
                # one supported wake path on this leg.
                disallowed_tools=sorted(
                    {
                        "Task",
                        "Agent",
                        "Workflow",
                        "ScheduleWakeup",
                        "CronCreate",
                        "CronDelete",
                        "CronList",
                    }
                    | set(options.disallowed_tools or [])
                ),
                # CE12: the definition's tool list for a file-agent worker. The
                # CLI reads --allowed-tools as PERMISSION rules, so on this leg
                # the list grants (no prompt for those tools) rather than
                # restricting — the enforced surface wall exists only on the
                # native leg, where fluid owns the registry. Passed anyway so
                # the two legs agree on intent and the CLI stops prompting for
                # exactly the tools the definition sanctioned.
                allowed_tools=_tool_allowlist(options),
                claude_cmd=_resolve_claude_cmd(),
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
                    self._resolve_app_session_id
                )
            except Exception:  # noqa: BLE001 — Task degrades to "unavailable"
                logger.warning(
                    "[HarnessAdapter] task_dispatch injection FAILED (fluid "
                    "session=%s, cwd=%s) — the Task tool will report itself "
                    "unavailable on this native leg",
                    fluid_session_id, options.cwd, exc_info=True,
                )
            # Engine caps the app can express today (plan wave W0a). Both are
            # omitted unless configured so fluid keeps its own defaults
            # (max_model_calls_per_turn=50, max_tokens=8192).
            native_caps: Dict[str, Any] = {}
            max_calls = _positive_int(getattr(options, "max_turns", None))
            if max_calls is not None:
                native_caps["max_model_calls_per_turn"] = max_calls
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
            _, declared_agent_descriptors = _agent_context(options)
            # Compiled ONCE, used twice below: the joined text is the prompt
            # the engine sends; the labeled sections feed get_context_usage().
            compiled_prompt = self._compile_prompt_sections(
                system, append_system, options
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
                **native_caps,
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
                registry=self._registry,
                permissions=permissions,
                ctx=native_ctx,
                plan_mode=plan_mode,
                session_id=fluid_session_id,
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

        # Prime the session so ``session_id`` and ``events()`` are usable
        # immediately (both engines lazily create it inside the first turn task,
        # which would otherwise race receive_events()).
        ensure = getattr(self._engine, "_ensure_session", None)
        if ensure is not None:
            meta = await ensure()
            self._sdk_session_id = meta.session_id
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
        # The app's own session id (``sessions.id``) rides alongside it as
        # ``aih_session_id``. Native legs need it for Task dispatch: the fluid
        # session id is a ``ses_...`` handle, not an app session row, and on a
        # fresh session neither id exists at connect time.
        app_session_id = getattr(sdk_options, "aih_session_id", None)
        if app_session_id:
            opts.extra["app_session_id"] = app_session_id
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

    async def send_query(self, prompt: str) -> None:
        if self._engine is None:
            raise RuntimeError("Adapter not connected")
        # fluid annotates ``UserTurn.content`` as a list, but its own callers and
        # tests pass the prompt string straight through (the loops normalize it),
        # so the cast bridges the vendor annotation without touching the value.
        await self._engine.submit(UserTurn(content=cast(Any, prompt)))

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

            for translated in self._translate(event):
                if isinstance(translated, PermissionRequestEvent) and self._can_use_tool:
                    # SDK parity: permission prompts run out-of-band through the
                    # app's can_use_tool callback, not in the message stream.
                    # Spawned as a task — the callback blocks on the user while
                    # other stream events keep flowing.
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
        # fluid's core Event model does not yet emit a user-question event, so
        # there is normally no request_id to answer. This is wired for
        # forward-compat: if a question id was recorded, respond to it.
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
        """Synthesized AssistantMessage; attaches the last Usage reading so the
        loops' per-call token accounting sees it (SDK parity)."""
        usage, self._pending_usage = self._pending_usage, None
        return AssistantMessage(
            content=blocks,
            model=self._current_model
            or (self._options.model if self._options else ""),
            usage=usage,
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

        if cls == "QuestionRequest":
            # MRTR input_required round-trip (fluid engine). Record the id so
            # respond_to_user_question routes the answer back; surface the
            # prompt through the permission-event channel (name/input carry the
            # question text + schema) until a dedicated question UI lands.
            self._pending_question_id = event.request_id
            return [
                PermissionRequestEvent(
                    tool_use_id=event.tool_use_id,
                    name=event.name,
                    input={"question": event.message, "schema": event.schema},
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
            # Held and attached to the next synthesized AssistantMessage — the
            # loops read per-call usage off AssistantMessage.usage (SDK names).
            usage: Dict[str, Any] = {
                "input_tokens": event.input_tokens,
                "output_tokens": event.output_tokens,
                "cache_read_input_tokens": event.cache_read_tokens,
                "cache_creation_input_tokens": event.cache_creation_tokens,
            }
            if event.raw:
                usage.update(event.raw)
            self._pending_usage = usage
            return []

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

        if cls == "SubagentEvent":
            common = {
                "uuid": self._uuid(event),
                "session_id": getattr(event, "session_id", "") or "",
            }
            if event.kind == "started":
                return [
                    TaskStartedMessage(
                        subtype="task_started",
                        data={"task_id": event.task_id},
                        task_id=event.task_id,
                        description=event.description or "",
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
                    terminal_reason="completed",
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
        """
        try:
            from fluid.context.compiler import (
                CompiledPrompt, PromptInputs, PromptSection, compile_prompt,
            )

            # A profile with ``inject_env_details`` on already carries the
            # app's <env> block (which knows worktree/execution mode fluid
            # doesn't) — skip fluid's env section rather than injecting twice.
            has_app_env = bool(system) and "<env>" in system
            compiled = compile_prompt(
                PromptInputs(
                    base=system,
                    extra_blocks=[append_system] if append_system else [],
                    cwd=options.cwd or ".",
                    include_env=not has_app_env,
                    include_agents=False,
                )
            )
            index, _ = _agent_context(options)
            if not index:
                return compiled
            sections = list(compiled.sections)
            insert_at = len(sections)
            for position, section in enumerate(sections):
                if section.kind in ("environment", "extra"):
                    insert_at = position
                    break
            sections.insert(insert_at, PromptSection(kind="agents", text=index))
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
        """
        compiled = HarnessAdapter._compile_prompt_sections(
            system, append_system, options
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
            logger.info("[HarnessAdapter] registered fluid builtin tools for native leg")
            _amend_native_task_spec(registry, options)

        # The app's own in-proc SDK servers (builtins, app-config, ai-tools,
        # computer-use, provider-agent): their @tool objects were recorded per
        # instance when patch_sdk_server assembled them; re-register the same
        # tools on fluid's registry with the bridge's risk maps.
        from app.core.provider.fluid_tool_bridge import register_recorded_sdk_servers
        # Native legs run fluid's bare-named Task builtin (ruling A13) and its
        # ToolSearch (the functional deferred-schema loader for THIS registry)
        # — the bridged mcp__builtins__ copies are excluded so the model sees
        # ONE of each. The delegated leg keeps the MCP copies (the CLI can
        # only receive external tools via --mcp-config, and its own native
        # ToolSearch serves the CLI-side registry).
        recorded = register_recorded_sdk_servers(
            registry,
            options.mcp_servers,
            skip_tools={"builtins": {"Task", "ToolSearch"}} if native else None,
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


def _agents_header() -> str:
    """fluid's own "Available agent types" heading, with a literal fallback.

    Shared by the index builder and the delegated-leg idempotence check so the
    two can never drift apart (a header the check does not recognise would
    double the block on the CLI's prompt).
    """
    try:
        from fluid.context.compiler import AGENTS_HEADER

        return AGENTS_HEADER
    except Exception:  # noqa: BLE001
        return "# Available agent types"


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


def _agent_context(options: SessionOptions) -> Tuple[Optional[str], List[Dict[str, Any]]]:
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
        index = f"{_agents_header()}\n{body}"

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
    """Re-register fluid's bare Task builtin with a policy-aware ``model`` hint.

    The vendor spec's ``model`` property only says "Optional model override for
    this run" — on a native (non-Claude) leg the model calling Task has no idea
    what that means for ITS provider and reliably guesses a Claude shorthand
    alias ("sonnet"/"opus"/"haiku"), which the dispatcher then drops. Swap in
    :data:`TASK_MODEL_OVERRIDE_DESCRIPTION`, which states the actual policy.
    Unbound sessions (delegated/Claude default) are left alone — that leg gets
    the same wording on its bridged ``mcp__builtins__Task`` copy instead (see
    :mod:`app.mcp.builtin_tools`).

    Best-effort only: any failure here must never break session connect, so the
    vendor spec simply stays registered as-is.
    """
    try:
        if not (options.extra or {}).get("provider_id"):
            return

        import copy
        import dataclasses

        from fluid.tools.builtins.task import SPEC as _TASK_SPEC
        from fluid.tools.builtins.task import handler as _task_handler

        from app.core.task_dispatch import TASK_MODEL_OVERRIDE_DESCRIPTION

        schema = copy.deepcopy(_TASK_SPEC.input_schema)
        schema["properties"]["model"]["description"] = (
            TASK_MODEL_OVERRIDE_DESCRIPTION
        )
        registry.register_builtin(
            dataclasses.replace(_TASK_SPEC, input_schema=schema), _task_handler
        )
    except Exception:  # noqa: BLE001 — must never break session connect
        logger.warning(
            "[HarnessAdapter] failed to amend native Task spec's model "
            "description; leaving the vendor spec as-is.",
            exc_info=True,
        )


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
        if creds is not None:
            if creds.base_url:
                return (
                    OpenRouterClient(
                        api_key=creds.api_key,
                        base_url=_chat_completions_url(creds.base_url),
                    ),
                    None,
                )
            return OpenRouterClient(api_key=creds.api_key), None
        _require_env_credential("OPENROUTER_API_KEY", kind)
        return OpenRouterClient(), None
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
