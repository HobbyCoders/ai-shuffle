"""HarnessAdapter — concrete ProviderAdapter backed by the ``omni`` harness.

This is the app's only provider backend, and the successor to
:class:`ClaudeAgentAdapter`. It wraps a omni execution engine (``NativeLoop`` for direct-API providers, or
``DelegatedLoop`` for the Claude-subscription ``claude`` CLI) and translates the
omni ``Event`` stream into the app's provider-agnostic ``ProviderEvent`` types,
so the rest of AI-Shuffle never sees a omni type.

``factory.make_adapter()`` returns it unconditionally for every spawn path.

Design mirrors ClaudeAgentAdapter: connect/disconnect/health/reconnect lifecycle,
send_query/receive_events for streaming, and interrupt/respond/set_* controls.

Provider-kind decision (from SessionOptions):
  * ``extra["omni_engine"] == "delegated"``  → DelegatedLoop (claude CLI)
  * ``extra["omni_provider"]`` / env ``AIH_OMNI_PROVIDER`` in
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
  * ``resume_session_id``    → omni session id (both engines). For the delegated
    engine the CLI's ``--resume`` handle is NOT this id — it is read off the
    stored session meta (``cli_session_id``), which DelegatedLoop writes back
    from the CLI's init frame. See ``_resolve_cli_session_id``.
  * ``session_id``           → explicit omni session id
  * ``permission_mode``      → omni PermissionEngine approval policy + plan_mode
  * ``env``                  → delegated child env / native provider (via os.environ)
  * ``mcp_servers``          → omni ``registry.connect_mcp`` (external stdio/sse/http)
  * ``include_partial_messages`` → omni always streams deltas (native); delegated
    passes ``--include-partial-messages`` (``extra["omni_partial"]`` overrides)
  * ``extra["omni_store_dir"]`` / env ``AIH_OMNI_STORE_DIR`` → JsonlStore root
  * ``extra["omni_inproc_servers"]`` → omni in-process tool servers (see omni_tool_bridge)
  * ``extra["omni_registry"]``       → caller-supplied prebuilt ToolRegistry (escape hatch)
  * ``extra["provider_id"]``         → the session's bound ``model_providers`` row;
    its decrypted credentials authenticate the native client (API key, or a
    Codex OAuth auth object for a ChatGPT-subscription row). Threaded from
    ``query_options`` via ``ClaudeAgentOptions.aih_provider_id``. Absent → the
    omni client's env-var fallback (see ``omni_credentials``).

  * in-proc SDK servers in ``mcp_servers`` (type="sdk": builtins, app-config,
    ai-tools, computer-use, provider-agent) → auto-bridged onto the omni
    ToolRegistry with per-tool risk maps (see omni_tool_bridge.
    register_recorded_sdk_servers; tools recorded by sdk_compat.patch_sdk_server).

DEGRADED / NOT YET HONORED (documented gaps — mechanical follow-up):
  * ``allowed_tools`` / ``disallowed_tools`` — the app's SDK allow/deny lists
    are not auto-translated to registry filtering yet.
  * ``plugins`` — no omni equivalent.
  * ``thinking`` / ``effort`` — omni manages thinking via provider caps, not passed through.
  * ``hooks`` / ``include_hook_events`` — omni has its own HookBus; app SDK hooks not wired.
  * ``agents`` (subagents) — not configured on the omni engine here.
  * ``betas`` / ``extra_args`` / ``setting_sources`` — SDK/CLI-specific, ignored.
  * ``task_budget`` / ``session_store`` / ``session_store_flush`` — SDK session-store
    concepts; omni persistence is the JsonlStore/MemoryStore chosen above.
  * ``enable_file_checkpointing`` / ``stderr`` / ``max_buffer_size`` / ``user`` — ignored.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, AsyncIterator, Dict, Optional

import httpx

from app.core.provider.omni_credentials import (
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
# synthesizes these from the omni Event stream so a flag-on session flows
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

# omni imports (the library we wrap). Its top-level __init__ is intentionally
# empty, so we import from submodules directly.
from omni.engine.native import NativeLoop
from omni.engine.delegated import DelegatedLoop
from omni.events.ops import (
    Interrupt,
    RespondPermission,
    RespondQuestion,
    SetModel,
    SetPermissionMode,
    UserTurn,
)
from omni.permissions.engine import PermissionEngine
from omni.providers.anthropic.client import AnthropicClient
from omni.providers.openai_responses.client import (
    API_URL as OPENAI_RESPONSES_URL,
    CODEX_RESPONSES_URL,
    OpenAIResponsesClient,
)
from omni.providers.openrouter.client import OpenRouterClient
from omni.store.base import MemoryStore
from omni.store.jsonl import JsonlStore
from omni.tools.registry import ToolCtx, ToolRegistry

logger = logging.getLogger(__name__)

_ENGINE_CLOSE_GRACE_SECONDS = 5.0

# app permission_mode → omni PermissionEngine approval policy
_PERMISSION_POLICY = {
    "bypassPermissions": "never_ask",
    "acceptEdits": "on_write",
    "default": "on_write",
    "plan": "always_ask",
}


class HarnessAdapter(ProviderAdapter):
    """ProviderAdapter implementation backed by the omni harness."""

    def __init__(self) -> None:
        self._engine: Optional[Any] = None  # NativeLoop | DelegatedLoop
        self._store: Optional[Any] = None
        self._registry: Optional[ToolRegistry] = None
        self._connected: bool = False
        self._options: Optional[SessionOptions] = None
        self._sdk_session_id: Optional[str] = None
        # receive_events replay cursor: omni's EventBus replays from ``since_seq``,
        # so we advance this per delivered event to avoid re-yielding prior turns.
        self._event_cursor: int = 0
        # app tool_use_id -> omni permission request_id (respond_to_permission map)
        self._perm_requests: Dict[str, str] = {}
        # last pending user-question request_id (omni core does not yet emit a
        # question event; kept for forward-compat with RespondQuestion).
        self._pending_question_id: Optional[str] = None
        self._is_delegated: bool = False
        # ─ translation state (see _translate) ─
        # Model reported by the current turn's TurnStarted (AssistantMessage.model).
        self._current_model: Optional[str] = None
        # Open streaming blocks: index -> block_type, and accumulated delta text.
        self._block_types: Dict[int, str] = {}
        self._block_buffers: Dict[int, list] = {}
        # Last omni Usage reading, attached to the next synthesized
        # AssistantMessage (the loops read per-call usage off that message).
        self._pending_usage: Optional[Dict[str, Any]] = None
        # App-side permission callback (ClaudeAgentOptions.can_use_tool). When
        # present, omni PermissionRequests are bridged to it out-of-band (SDK
        # parity — the loops never see a permission event); when absent they
        # surface in-stream as PermissionRequestEvent.
        self._can_use_tool: Optional[Any] = None
        self._perm_bridge_tasks: set = set()
        # httpx client injected into the native provider client purely to
        # capture upstream usage/quota headers (see _usage_capture_http_client).
        # The omni client does not own it, so disconnect() closes it.
        self._provider_http: Optional[httpx.AsyncClient] = None

    # ─── Transition helper ────────────────────────────────────────────

    @property
    def engine(self) -> Optional[Any]:
        """Direct access to the underlying omni engine (parallels sdk_client)."""
        return self._engine

    # ─── Connection Lifecycle ─────────────────────────────────────────

    async def connect(self, options: SessionOptions) -> None:
        """Build the omni engine from SessionOptions and prime its session."""
        self._options = options
        self._store = self._build_store(options)

        # Decide the leg BEFORE building the registry: the native legs need
        # omni's builtin file/shell tools registered, the delegated leg must not
        # get them (the claude CLI ships its own).
        kind = _decide_provider_kind(options)
        self._is_delegated = kind == "delegated"

        self._registry = await self._build_registry(options, native=not self._is_delegated)

        omni_session_id = options.session_id or options.resume_session_id
        system, append_system = _system_parts(options.system_prompt)
        permissions, plan_mode = self._build_permissions(options)

        if self._is_delegated:
            cli_session_id = await self._resolve_cli_session_id(omni_session_id, options)
            self._engine = DelegatedLoop(
                store=self._store,
                model=options.model,
                system=system,
                append_system_prompt=append_system,
                permission_mode=options.permission_mode,
                registry=self._registry,
                ctx=ToolCtx(cwd=options.cwd or ".", session_id=omni_session_id),
                session_id=omni_session_id,
                cli_session_id=cli_session_id,
                # Honor the SDK-level streaming option (the app turns it on by
                # default). Without it the delegated CLI never gets
                # --include-partial-messages and the UI only sees whole messages
                # at turn end. extra["omni_partial"] stays a manual override.
                partial=bool(
                    getattr(options, "include_partial_messages", False)
                    or options.extra.get("omni_partial", False)
                ),
                # ONLY external transports. The raw map also holds type='sdk'
                # entries wrapping live in-proc Server instances, and the
                # delegated leg JSON-serializes this map into its --mcp-config
                # file -> TypeError("Object of type Server is not JSON
                # serializable") and a failed spawn.
                mcp_servers=_external_mcp_servers(options.mcp_servers),
                cwd=options.cwd,
                env=options.env,
                claude_cmd=_resolve_claude_cmd(),
            )
        else:
            provider_client, provider_http = await _build_provider_client(kind, options)
            self._provider_http = provider_http
            self._engine = NativeLoop(
                provider=provider_client,
                store=self._store,
                model=options.model,
                # Native legs have no Claude Code preset to append to — the
                # preset form's append text (security + profile instructions)
                # becomes the system prompt itself.
                system=system if system is not None else append_system,
                registry=self._registry,
                permissions=permissions,
                ctx=ToolCtx(cwd=options.cwd or ".", session_id=omni_session_id),
                plan_mode=plan_mode,
                session_id=omni_session_id,
            )

        # Prime the session so ``session_id`` and ``events()`` are usable
        # immediately (both engines lazily create it inside the first turn task,
        # which would otherwise race receive_events()).
        ensure = getattr(self._engine, "_ensure_session", None)
        if ensure is not None:
            meta = await ensure()
            self._sdk_session_id = meta.session_id

        self._connected = True

    async def connect_with_sdk_options(self, sdk_options: Any) -> None:
        """Compat shim mirroring ClaudeAgentAdapter.connect_with_sdk_options.

        Callers that still build ``ClaudeAgentOptions`` directly (query_engine,
        query_background) can hand them here; the fields omni can honor are
        projected onto a SessionOptions. This lets ``make_adapter()`` be a
        drop-in at those sites. Fields omni cannot honor are ignored (see the
        module docstring's degraded list).
        """
        opts = SessionOptions(
            model=getattr(sdk_options, "model", None) or "claude-sonnet-4-5",
            system_prompt=getattr(sdk_options, "system_prompt", None),
            cwd=getattr(sdk_options, "cwd", None),
            resume_session_id=getattr(sdk_options, "resume", None),
            permission_mode=getattr(sdk_options, "permission_mode", None),
            mcp_servers=getattr(sdk_options, "mcp_servers", None),
            env=getattr(sdk_options, "env", None),
            include_partial_messages=bool(
                getattr(sdk_options, "include_partial_messages", False)
            ),
        )
        # The session's bound model provider (``sessions.provider_id``) rides on
        # the SDK options object as ``aih_provider_id`` (stamped by
        # ``query_options.build_options_from_profile``; inert on the SDK path).
        # It is what lets _build_provider_client authenticate a native leg from
        # the app's encrypted ``model_providers`` row.
        provider_id = getattr(sdk_options, "aih_provider_id", None)
        if provider_id:
            opts.extra["provider_id"] = provider_id
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
        """Close the omni engine (cancels any running turn, flushes the bus)."""
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

        omni engines are in-process (native) or supervise a subprocess
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
        await self._engine.submit(UserTurn(content=prompt))

    async def receive_events(self) -> AsyncIterator[ProviderEvent]:
        """Translate the omni Event stream into ProviderEvents for one turn.

        Subscribes at the current replay cursor, yields translated mirror
        messages, and completes on the turn-terminal ResultMessage (synthesized
        from omni's TurnComplete / Interrupted / ErrorEvent), mirroring the SDK
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
        self, omni_session_id: Optional[str], options: SessionOptions
    ) -> Optional[str]:
        """Resolve the delegated CLI's ``--resume`` handle from the store.

        ``resume_session_id`` is the app's *omni* session id (``ses_...``); the
        CLI only recognises the uuid it minted itself and reported in its
        ``init`` frame. DelegatedLoop persists that uuid onto the session meta,
        so a resumed session reads it back from here.

        Returns None when there is nothing stored — a fresh CLI conversation is
        the correct degraded behavior. Passing the omni id through instead
        (the old bug) makes the CLI reject the resume and the turn dies in ~1s.
        """
        if not options.resume_session_id or not omni_session_id or self._store is None:
            return None
        try:
            meta = await self._store.get_session(omni_session_id)
        except Exception:
            logger.warning(
                "[HarnessAdapter] could not load omni session %s for the CLI resume "
                "handle — starting a fresh CLI conversation",
                omni_session_id, exc_info=True,
            )
            return None
        return getattr(meta, "cli_session_id", None) if meta is not None else None

    async def _bridge_permission(self, ev: PermissionRequestEvent) -> None:
        """Route one omni permission request through the app's can_use_tool
        callback and submit the decision back to the engine.

        The callback duck-types its ``context`` argument via getattr (see
        can_use_tool_callback in query_engine), so a SimpleNamespace carrying
        tool_use_id is sufficient. The result is detected by class name to keep
        claude_agent_sdk types out of this module. Any failure denies —
        omni blocks the tool until a decision arrives, so silence would hang
        the turn.
        """
        from types import SimpleNamespace

        allow = False
        message: Optional[str] = None
        updated_input: Optional[Dict[str, Any]] = None
        try:
            result = await self._can_use_tool(
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
                "[HarnessAdapter] no pending omni permission for tool_use_id=%s", tool_use_id
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
        # omni's core Event model does not yet emit a user-question event, so
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

        No omni engine answers this today:

        * ``NativeLoop`` owns the conversation itself and has no equivalent of
          the CLI's per-category ``/context`` breakdown.
        * ``DelegatedLoop`` delegates to the ``claude`` CLI, which *does*
          understand the ``{"subtype": "get_context_usage"}`` control_request
          the SDK uses — but the engine has no request/response correlation for
          control frames (``_send_control`` is fire-and-forget and
          ``StreamJsonMapper`` drops ``control_response``), so there is nothing
          to forward the call through. Wiring that up is an omni-side change.

        The method probes the engine anyway so that it starts forwarding for
        free once an engine grows the capability. It never estimates from
        stored usage — a fabricated breakdown would be worse than None.
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
        return False

    # ─── Translation: omni Event → ProviderEvent mirrors ──────────────

    def _uuid(self, event: Any) -> str:
        """Stable synthetic uuid for a omni event (mirrors carry uuid fields)."""
        return f"omni-{getattr(event, 'seq', -1)}"

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
        empty) — one omni event can fan out to a StreamEvent plus a complete
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
            # blocks are covered by the discrete ToolUse omni event.
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
            # MRTR input_required round-trip (omni engine). Record the id so
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
            # can route the decision back to the right omni future.
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
            status = {"finished": "completed"}.get(event.kind, event.kind)
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
                    # omni can report cost_usd=None (e.g. the delegated-Claude
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
            return [
                ResultMessage(
                    subtype="success",
                    is_error=False,
                    session_id=getattr(event, "session_id", "") or "",
                    result=event.message,
                    # ResultMessage.total_cost_usd defaults to None; the SDK path
                    # always carries a float here (the CLI's result frame makes
                    # total_cost_usd mandatory), and the session-row update does
                    # `col + value`, so a None would NULL the column.
                    total_cost_usd=0.0,
                    uuid=self._uuid(event),
                    terminal_reason="aborted_streaming",
                )
            ]

        if cls == "ErrorEvent":
            return [
                ResultMessage(
                    subtype="error_during_execution",
                    is_error=True,
                    session_id=getattr(event, "session_id", "") or "",
                    result=event.message,
                    errors=[event.message],
                    total_cost_usd=0.0,  # see the Interrupted branch above
                    uuid=self._uuid(event),
                )
            ]

        # Unknown / no-analog omni event — skip.
        return []

    # ─── Internal builders ────────────────────────────────────────────

    @staticmethod
    def _build_store(options: SessionOptions) -> Any:
        # Postgres store (deployment default target): survives restarts and is
        # shared across app instances, like the SDK path's PgSessionStore.
        # Opt-in via AIH_OMNI_STORE=pg / extra["omni_store"]="pg" so unit tests
        # and DB-less environments keep the file/memory stores.
        store_kind = (
            options.extra.get("omni_store") or os.environ.get("AIH_OMNI_STORE") or ""
        ).lower()
        if store_kind == "pg":
            from app.core.provider.omni_pg_store import PgOmniStore
            return PgOmniStore()
        store_dir = options.extra.get("omni_store_dir") or os.environ.get("AIH_OMNI_STORE_DIR")
        if store_dir:
            return JsonlStore(store_dir)
        return MemoryStore()

    async def _build_registry(
        self, options: SessionOptions, *, native: bool
    ) -> Optional[ToolRegistry]:
        # Escape hatch: a caller may hand a fully-built registry — its contents
        # are then entirely the caller's responsibility (builtins included).
        prebuilt = options.extra.get("omni_registry")
        if isinstance(prebuilt, ToolRegistry):
            return prebuilt

        inproc_servers = options.extra.get("omni_inproc_servers") or []
        external_mcp = _external_mcp_servers(options.mcp_servers)

        registry = ToolRegistry()
        # Constraint: native legs (anthropic / openai_responses / openrouter)
        # get omni's builtin Read/Grep/Write/Edit/Bash/Skill/Note as first-class
        # tools — without them a native session can only reach the app's bridged
        # MCP servers and has no file/shell access at all. The delegated leg must
        # NOT get them: the claude CLI supplies its own copies. Conversely the
        # CLI must never be relied on for tools outside the Claude-subscription
        # case, which is exactly why the native leg registers its own here.
        builtins_registered = False
        if native:
            from omni.tools.builtins import register_builtins

            register_builtins(registry)
            builtins_registered = True
            logger.info("[HarnessAdapter] registered omni builtin tools for native leg")

        # The app's own in-proc SDK servers (builtins, app-config, ai-tools,
        # computer-use, provider-agent): their @tool objects were recorded per
        # instance when patch_sdk_server assembled them; re-register the same
        # tools on omni's registry with the bridge's risk maps.
        from app.core.provider.omni_tool_bridge import register_recorded_sdk_servers
        recorded = register_recorded_sdk_servers(registry, options.mcp_servers)
        if recorded:
            logger.info(
                "[HarnessAdapter] bridged in-proc sdk servers onto omni registry: %s",
                ", ".join(recorded),
            )

        for server in inproc_servers:
            registry.register_inproc(server)
        for name, config in external_mcp.items():
            try:
                await registry.connect_mcp(name, config)
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
    """Only external transports (stdio/sse/http) can be connected via omni's
    McpManager. SDK in-process servers (type='sdk') are skipped — those port
    through ``omni_tool_bridge`` into ``omni_inproc_servers`` instead."""
    out: Dict[str, Any] = {}
    for name, cfg in (mcp_servers or {}).items():
        if isinstance(cfg, dict) and cfg.get("type") == "sdk":
            continue
        out[name] = cfg
    return out


def _decide_provider_kind(options: SessionOptions) -> str:
    """Return one of: 'delegated' | 'anthropic' | 'openai_responses' | 'openrouter'."""
    engine_hint = options.extra.get("omni_engine")
    if engine_hint == "delegated":
        return "delegated"
    if engine_hint == "native":
        # fall through to provider selection below
        pass

    provider_hint = options.extra.get("omni_provider") or os.environ.get("AIH_OMNI_PROVIDER")
    if provider_hint in ("anthropic", "openai_responses", "openrouter"):
        return provider_hint
    if provider_hint == "delegated":
        return "delegated"

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
        # extra["omni_provider"]="anthropic" for the direct-API-key native path.
        return "delegated" if engine_hint != "native" else "anthropic"
    if model.startswith(("gpt", "o1", "o3", "o4", "codex")):
        return "openai_responses"
    if "/" in model:  # vendor/model OpenRouter slug, e.g. "meta-llama/llama-3.1-70b"
        return "openrouter"
    return "anthropic"


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
    """An ``httpx.AsyncClient`` that passively captures usage/quota headers.

    Flag-on native legs talk to the upstream provider directly (the omni client
    owns the HTTP call), so the translator gateway's header capture in
    ``app/core/translator_gateway/upstream.py`` never runs and
    ``provider_usage_snapshots`` goes stale. Injecting this client into the omni
    client restores the same capture: httpx fires the ``response`` event hook
    with headers populated before the streamed body is consumed, so the hook
    mirrors the gateway call sites exactly (``capture_codex_headers`` for the
    Codex backend, ``capture_openai_headers`` for the OpenAI platform one).

    Deliberately no status-code guard: ``capture_*`` already no-ops when no
    allowlisted header is present, and error responses (notably 429) carry the
    freshest quota headers of all. ``timeout=600.0`` matches the vendored omni
    client's own default, which it bypasses whenever a client is injected.

    ``provider_usage`` is imported inside the hook (mirroring the legacy call
    sites) to keep the module import graph cycle-free.
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

    return httpx.AsyncClient(timeout=600.0, event_hooks={"response": [_on_response]})


async def _build_provider_client(
    kind: str, options: SessionOptions
) -> tuple[Any, Optional[httpx.AsyncClient]]:
    """Construct the omni native client for ``kind``, authenticated.

    Returns ``(client, capture_http_client)``. The second element is the
    usage-header-capturing ``httpx.AsyncClient`` injected into the omni client
    (openai_responses with a bound provider row only), or ``None`` when no
    client was injected — the caller owns closing it (the omni client does not,
    since it did not create it).

    Credential resolution order (see ``omni_credentials``):
      1. the session's bound ``model_providers`` row (``extra["provider_id"]``,
         threaded from ``query_options`` through ``connect_with_sdk_options``) —
         decrypted API key, or a Codex OAuth auth object for a
         ChatGPT-subscription row;
      2. otherwise the omni client's own env-var fallback (dev mode), which is
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
            client = OpenAIResponsesClient(
                api_key=creds.api_key,
                auth=creds.auth,
                base_url=CODEX_RESPONSES_URL if creds.use_codex_backend else OPENAI_RESPONSES_URL,
                http_client=http_client,
            )
            return client, http_client
        _require_env_credential("OPENAI_API_KEY", kind)
        return OpenAIResponsesClient(), None
    if kind == "openrouter":
        if creds is not None:
            return OpenRouterClient(api_key=creds.api_key), None
        _require_env_credential("OPENROUTER_API_KEY", kind)
        return OpenRouterClient(), None
    # anthropic — API-key rows only. A Claude Pro/Max SUBSCRIPTION OAuth token is
    # never routed here (that path stays delegated-CLI only); resolve_provider_
    # credentials refuses such a row outright.
    if creds is not None:
        return AnthropicClient(api_key=creds.api_key), None
    _require_env_credential("ANTHROPIC_API_KEY", kind)
    return AnthropicClient(), None
