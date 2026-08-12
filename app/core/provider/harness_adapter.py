"""HarnessAdapter — concrete ProviderAdapter backed by the ``loom`` harness.

This is the additive, feature-flagged sibling of :class:`ClaudeAgentAdapter`.
It wraps a loom execution engine (``NativeLoop`` for direct-API providers, or
``DelegatedLoop`` for the Claude-subscription ``claude`` CLI) and translates the
loom ``Event`` stream into the app's provider-agnostic ``ProviderEvent`` types,
so the rest of AI-Shuffle never sees a loom type.

It is selected ONLY when ``AIH_USE_LOOM_HARNESS=1`` (see ``factory.make_adapter``);
the default path remains the Claude Agent SDK adapter, unchanged.

Design mirrors ClaudeAgentAdapter: connect/disconnect/health/reconnect lifecycle,
send_query/receive_events for streaming, and interrupt/respond/set_* controls.

Provider-kind decision (from SessionOptions):
  * ``extra["loom_engine"] == "delegated"``  → DelegatedLoop (claude CLI)
  * ``extra["loom_provider"]`` / env ``AIH_LOOM_PROVIDER`` in
    {anthropic, openai_responses, openrouter} → NativeLoop with that client
  * otherwise inferred from the model string:
      - "claude*"            → DelegatedLoop (subscription default)
      - "gpt*"/"o1*"/"o3*"/"codex*" → NativeLoop + OpenAIResponsesClient
      - "vendor/model" slug  → NativeLoop + OpenRouterClient
      - else                 → NativeLoop + AnthropicClient (direct API key)

SessionOptions field support
----------------------------
HONORED:
  * ``model``                → engine model (native: request model; delegated: --model)
  * ``system_prompt`` (str)  → engine ``system`` (dict system prompts are stringified)
  * ``cwd``                  → ToolCtx.cwd (native) / DelegatedLoop cwd
  * ``resume_session_id``    → loom session id (native) / ``--resume`` handle (delegated)
  * ``session_id``           → explicit loom session id
  * ``permission_mode``      → loom PermissionEngine approval policy + plan_mode
  * ``env``                  → delegated child env / native provider (via os.environ)
  * ``mcp_servers``          → loom ``registry.connect_mcp`` (external stdio/sse/http)
  * ``extra["loom_store_dir"]`` / env ``AIH_LOOM_STORE_DIR`` → JsonlStore root
  * ``extra["loom_inproc_servers"]`` → loom in-process tool servers (see loom_tool_bridge)
  * ``extra["loom_registry"]``       → caller-supplied prebuilt ToolRegistry (escape hatch)

DEGRADED / NOT YET HONORED (documented gaps — mechanical follow-up):
  * ``tools`` / ``allowed_tools`` / ``disallowed_tools`` — loom uses a ToolRegistry;
    the app's SDK tool/allow lists are not auto-translated (use loom_inproc_servers).
  * ``plugins`` — no loom equivalent.
  * ``thinking`` / ``effort`` — loom manages thinking via provider caps, not passed through.
  * ``hooks`` / ``include_hook_events`` — loom has its own HookBus; app SDK hooks not wired.
  * ``agents`` (subagents) — not configured on the loom engine here.
  * ``betas`` / ``extra_args`` / ``setting_sources`` — SDK/CLI-specific, ignored.
  * ``task_budget`` / ``session_store`` / ``session_store_flush`` — SDK session-store
    concepts; loom persistence is the JsonlStore/MemoryStore chosen above.
  * ``include_partial_messages`` — loom always streams deltas (native); delegated
    ``partial`` defaults off (set ``extra["loom_partial"]=True`` to enable).
  * ``enable_file_checkpointing`` / ``stderr`` / ``max_buffer_size`` / ``user`` — ignored.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, AsyncIterator, Dict, Optional

from app.core.provider.protocol import ProviderAdapter
from app.core.provider.types import (
    PermissionRequestEvent,
    ProviderEvent,
    SessionOptions,
    mcp_servers_equivalent,
)
# The streaming loops dispatch on the provider mirror vocabulary (identical
# names/shapes to the SDK message classes — see events.py). _translate
# synthesizes these from the loom Event stream so a flag-on session flows
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

# loom imports (the library we wrap). Its top-level __init__ is intentionally
# empty, so we import from submodules directly.
from loom.engine.native import NativeLoop
from loom.engine.delegated import DelegatedLoop
from loom.events.ops import (
    Interrupt,
    RespondPermission,
    RespondQuestion,
    SetModel,
    SetPermissionMode,
    UserTurn,
)
from loom.permissions.engine import PermissionEngine
from loom.providers.anthropic.client import AnthropicClient
from loom.providers.openai_responses.client import OpenAIResponsesClient
from loom.providers.openrouter.client import OpenRouterClient
from loom.store.base import MemoryStore
from loom.store.jsonl import JsonlStore
from loom.tools.registry import ToolCtx, ToolRegistry

logger = logging.getLogger(__name__)

_ENGINE_CLOSE_GRACE_SECONDS = 5.0

# app permission_mode → loom PermissionEngine approval policy
_PERMISSION_POLICY = {
    "bypassPermissions": "never_ask",
    "acceptEdits": "on_write",
    "default": "on_write",
    "plan": "always_ask",
}


class HarnessAdapter(ProviderAdapter):
    """ProviderAdapter implementation backed by the loom harness."""

    def __init__(self) -> None:
        self._engine: Optional[Any] = None  # NativeLoop | DelegatedLoop
        self._store: Optional[Any] = None
        self._registry: Optional[ToolRegistry] = None
        self._connected: bool = False
        self._options: Optional[SessionOptions] = None
        self._sdk_session_id: Optional[str] = None
        # receive_events replay cursor: loom's EventBus replays from ``since_seq``,
        # so we advance this per delivered event to avoid re-yielding prior turns.
        self._event_cursor: int = 0
        # app tool_use_id -> loom permission request_id (respond_to_permission map)
        self._perm_requests: Dict[str, str] = {}
        # last pending user-question request_id (loom core does not yet emit a
        # question event; kept for forward-compat with RespondQuestion).
        self._pending_question_id: Optional[str] = None
        self._is_delegated: bool = False
        # ─ translation state (see _translate) ─
        # Model reported by the current turn's TurnStarted (AssistantMessage.model).
        self._current_model: Optional[str] = None
        # Open streaming blocks: index -> block_type, and accumulated delta text.
        self._block_types: Dict[int, str] = {}
        self._block_buffers: Dict[int, list] = {}
        # Last loom Usage reading, attached to the next synthesized
        # AssistantMessage (the loops read per-call usage off that message).
        self._pending_usage: Optional[Dict[str, Any]] = None
        # App-side permission callback (ClaudeAgentOptions.can_use_tool). When
        # present, loom PermissionRequests are bridged to it out-of-band (SDK
        # parity — the loops never see a permission event); when absent they
        # surface in-stream as PermissionRequestEvent.
        self._can_use_tool: Optional[Any] = None
        self._perm_bridge_tasks: set = set()

    # ─── Transition helper ────────────────────────────────────────────

    @property
    def engine(self) -> Optional[Any]:
        """Direct access to the underlying loom engine (parallels sdk_client)."""
        return self._engine

    # ─── Connection Lifecycle ─────────────────────────────────────────

    async def connect(self, options: SessionOptions) -> None:
        """Build the loom engine from SessionOptions and prime its session."""
        self._options = options
        self._store = self._build_store(options)
        self._registry = await self._build_registry(options)

        kind = _decide_provider_kind(options)
        self._is_delegated = kind == "delegated"

        loom_session_id = options.session_id or options.resume_session_id
        system = _stringify_system(options.system_prompt)
        permissions, plan_mode = self._build_permissions(options)

        if self._is_delegated:
            self._engine = DelegatedLoop(
                store=self._store,
                model=options.model,
                system=system,
                permission_mode=options.permission_mode,
                registry=self._registry,
                ctx=ToolCtx(cwd=options.cwd or ".", session_id=loom_session_id),
                session_id=loom_session_id,
                cli_session_id=options.resume_session_id,
                partial=bool(options.extra.get("loom_partial", False)),
                mcp_servers=options.mcp_servers,
                cwd=options.cwd,
                env=options.env,
            )
        else:
            provider_client = _build_provider_client(kind, options)
            self._engine = NativeLoop(
                provider=provider_client,
                store=self._store,
                model=options.model,
                system=system,
                registry=self._registry,
                permissions=permissions,
                ctx=ToolCtx(cwd=options.cwd or ".", session_id=loom_session_id),
                plan_mode=plan_mode,
                session_id=loom_session_id,
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
        query_background) can hand them here; the fields loom can honor are
        projected onto a SessionOptions. This lets ``make_adapter()`` be a
        drop-in at those sites. Fields loom cannot honor are ignored (see the
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
        """Close the loom engine (cancels any running turn, flushes the bus)."""
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
            self._connected = False

    def is_alive(self) -> bool:
        """Whether the engine exists and hasn't been closed.

        loom engines are in-process (native) or supervise a subprocess
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
        """Translate the loom Event stream into ProviderEvents for one turn.

        Subscribes at the current replay cursor, yields translated mirror
        messages, and completes on the turn-terminal ResultMessage (synthesized
        from loom's TurnComplete / Interrupted / ErrorEvent), mirroring the SDK
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

    async def _bridge_permission(self, ev: PermissionRequestEvent) -> None:
        """Route one loom permission request through the app's can_use_tool
        callback and submit the decision back to the engine.

        The callback duck-types its ``context`` argument via getattr (see
        can_use_tool_callback in query_engine), so a SimpleNamespace carrying
        tool_use_id is sufficient. The result is detected by class name to keep
        claude_agent_sdk types out of this module. Any failure denies —
        loom blocks the tool until a decision arrives, so silence would hang
        the turn.
        """
        from types import SimpleNamespace

        allow = False
        try:
            result = await self._can_use_tool(
                ev.name,
                ev.input,
                SimpleNamespace(tool_use_id=ev.tool_use_id, agent_id=None),
            )
            allow = type(result).__name__ == "PermissionResultAllow"
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "[HarnessAdapter] can_use_tool bridge failed for %s — denying",
                ev.name, exc_info=True,
            )
        try:
            await self.respond_to_permission(ev.tool_use_id, allow)
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
    ) -> None:
        if self._engine is None:
            return
        request_id = self._perm_requests.pop(tool_use_id, None)
        if request_id is None:
            logger.warning(
                "[HarnessAdapter] no pending loom permission for tool_use_id=%s", tool_use_id
            )
            return
        decision = ("allow_always" if remember else "allow") if allow else "deny"
        await self._engine.submit(
            RespondPermission(request_id=request_id, decision=decision, remember=remember)
        )

    async def respond_to_user_question(self, answer: str) -> None:
        # loom's core Event model does not yet emit a user-question event, so
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
        if _stringify_system(old.system_prompt) != _stringify_system(
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

    # ─── Translation: loom Event → ProviderEvent mirrors ──────────────

    def _uuid(self, event: Any) -> str:
        """Stable synthetic uuid for a loom event (mirrors carry uuid fields)."""
        return f"loom-{getattr(event, 'seq', -1)}"

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
        empty) — one loom event can fan out to a StreamEvent plus a complete
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
            # blocks are covered by the discrete ToolUse loom event.
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

        if cls == "PermissionRequest":
            # Record the tool_use_id -> request_id link so respond_to_permission
            # can route the decision back to the right loom future.
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
                    total_cost_usd=event.cost_usd,
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
                    uuid=self._uuid(event),
                )
            ]

        # Unknown / no-analog loom event — skip.
        return []

    # ─── Internal builders ────────────────────────────────────────────

    @staticmethod
    def _build_store(options: SessionOptions) -> Any:
        store_dir = options.extra.get("loom_store_dir") or os.environ.get("AIH_LOOM_STORE_DIR")
        if store_dir:
            return JsonlStore(store_dir)
        return MemoryStore()

    async def _build_registry(self, options: SessionOptions) -> Optional[ToolRegistry]:
        # Escape hatch: a caller may hand a fully-built registry.
        prebuilt = options.extra.get("loom_registry")
        if isinstance(prebuilt, ToolRegistry):
            return prebuilt

        inproc_servers = options.extra.get("loom_inproc_servers") or []
        external_mcp = _external_mcp_servers(options.mcp_servers)
        if not inproc_servers and not external_mcp:
            return None

        registry = ToolRegistry()
        for server in inproc_servers:
            registry.register_inproc(server)
        for name, config in external_mcp.items():
            try:
                await registry.connect_mcp(name, config)
            except Exception as exc:  # best-effort; a bad MCP server must not fail connect
                logger.warning("[HarnessAdapter] connect_mcp(%s) failed: %r", name, exc)
        return registry

    def _build_permissions(self, options: SessionOptions):
        mode = options.permission_mode or "default"
        policy = _PERMISSION_POLICY.get(mode, "on_write")
        plan_mode = mode == "plan"
        return PermissionEngine(approval_policy=policy), plan_mode


# ─── Module-level helpers ─────────────────────────────────────────────────────


def _stringify_system(system_prompt: Any) -> Optional[str]:
    """loom takes a plain ``system`` string; coerce dict/None forms."""
    if system_prompt is None:
        return None
    if isinstance(system_prompt, str):
        return system_prompt
    if isinstance(system_prompt, dict):
        # SDK preset form {"type": "preset", ...} or {"text": ...}
        return system_prompt.get("text") or system_prompt.get("prompt") or ""
    return str(system_prompt)


def _external_mcp_servers(mcp_servers: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Only external transports (stdio/sse/http) can be connected via loom's
    McpManager. SDK in-process servers (type='sdk') are skipped — those port
    through ``loom_tool_bridge`` into ``loom_inproc_servers`` instead."""
    out: Dict[str, Any] = {}
    for name, cfg in (mcp_servers or {}).items():
        if isinstance(cfg, dict) and cfg.get("type") == "sdk":
            continue
        out[name] = cfg
    return out


def _decide_provider_kind(options: SessionOptions) -> str:
    """Return one of: 'delegated' | 'anthropic' | 'openai_responses' | 'openrouter'."""
    engine_hint = options.extra.get("loom_engine")
    if engine_hint == "delegated":
        return "delegated"
    if engine_hint == "native":
        # fall through to provider selection below
        pass

    provider_hint = options.extra.get("loom_provider") or os.environ.get("AIH_LOOM_PROVIDER")
    if provider_hint in ("anthropic", "openai_responses", "openrouter"):
        return provider_hint
    if provider_hint == "delegated":
        return "delegated"

    model = (options.model or "").lower()
    if model.startswith("claude"):
        # Subscription default: the claude CLI (delegated). Set
        # extra["loom_provider"]="anthropic" for the direct-API-key native path.
        return "delegated" if engine_hint != "native" else "anthropic"
    if model.startswith(("gpt", "o1", "o3", "o4", "codex")):
        return "openai_responses"
    if "/" in model:  # vendor/model OpenRouter slug, e.g. "meta-llama/llama-3.1-70b"
        return "openrouter"
    return "anthropic"


def _build_provider_client(kind: str, options: SessionOptions) -> Any:
    if kind == "openai_responses":
        return OpenAIResponsesClient()
    if kind == "openrouter":
        return OpenRouterClient()
    return AnthropicClient()
