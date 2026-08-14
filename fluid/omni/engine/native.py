"""NativeLoop — the harness-owned agent loop.

Turn lifecycle:
  UserTurn → persist user node → build request (append-only prefix + cache
  plan, tool defs, plan-mode filter) → stream provider chunks → re-emit deltas
  as Events → persist assistant node → if the model stopped on tool_use,
  dispatch each tool (PreToolUse hook → PermissionEngine → dispatch →
  PostToolUse), append a ToolResult user message, and loop back for the next
  model call → else TurnComplete.

Tools/permissions/hooks are all optional: with no registry the loop behaves as
the M0 text/thinking streamer (a tool_use stop is terminal).
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from omni.context.cache_plan import plan_cache
from omni.context.trim import ToolResultTrimmer, TrimConfig
from omni.engine.base import EngineCaps
from omni.events.bus import EventBus
from omni.events.model import (
    BlockStart,
    BlockStop,
    ErrorEvent,
    Event,
    Interrupted,
    HandoffCreated,
    PermissionRequest,
    QuestionRequest,
    TextDelta,
    ThinkingDelta,
    ToolResult,
    ToolUse,
    TurnComplete,
    TurnStarted,
    Usage,
)
from omni.events.ops import (
    Fork,
    Handoff,
    Interrupt,
    Op,
    RespondPermission,
    RespondQuestion,
    Rewind,
    SetModel,
    SetPermissionMode,
    UserTurn,
)
from omni.hooks.bus import (
    POST_TOOL_USE,
    PRE_TOOL_USE,
    TURN_END,
    TURN_START,
    HookBus,
)
from omni.messages import Message, Reasoning, Text, Thinking, ToolResultBlock, ToolUseBlock
from omni.permissions.engine import PermissionEngine, plan_mode_tools
from omni.providers.base import ProviderClient, ProviderRequest
from omni.codemode.session import CodeModeSession, register_code_mode_tools
from omni.store.base import SessionMeta, SessionNode, SessionStore, new_id
from omni.tools.builtins.tool_search import render_deferred_catalog
from omni.tools.registry import ToolCtx, ToolOutcome, ToolRegistry


@dataclass
class _CallResult:
    """Outcome of one streamed model call."""

    message: Message
    tool_uses: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)  # tokens (int) + optional cost_usd (float)
    stop_reason: str = "end_turn"
    interrupted: bool = False
    error_message: str | None = None  # set when stop_reason == "error"
    error_retriable: bool = False


# Provider error-message markers that mean "the prompt no longer fits" — the
# one failure the trimmer can recover from by force-trimming and retrying.
_OVERFLOW_MARKERS = (
    "prompt is too long",
    "context_length_exceeded",
    "maximum context length",
    "exceed context limit",
    "context window",
)


def _is_context_overflow(message: str) -> bool:
    lowered = message.lower()
    return any(marker in lowered for marker in _OVERFLOW_MARKERS)


def _plain_text(blocks: list[Any]) -> str:
    """Concatenated Text-block text of a message (for the episodic journal)."""
    return "\n".join(b.text for b in blocks if isinstance(b, Text) and b.text).strip()


LOST_TOOL_RESULT_TEXT = (
    "[interrupted: this tool call never completed (e.g. the server restarted "
    "mid-call); its result was lost]"
)


def repair_dangling_tool_calls(history: list[Message]) -> list[Message]:
    """Splice a synthetic result after every tool call that never got one.

    A transcript whose assistant message holds a tool call with no matching
    tool result is malformed, and providers reject the WHOLE request for it
    ("No tool output found for function call call_XXX" on the OpenAI dialect) —
    which poisons the session permanently: every later turn replays the same
    broken prefix. The hole appears whenever the process dies between the model
    call and the result append (a server restart or crash mid tool dispatch),
    and latently if a trimmer/view ever drops a result while keeping its call.

    Repair happens here, at request-build time, at the Message level: nothing is
    written back to the store (the tree keeps its true, incomplete shape) and
    nothing is provider-specific. The synthetic result goes DIRECTLY AFTER the
    message carrying the call — the OpenAI dialect requires a call's output to
    precede any later item — and is marked is_error so the model reads it as a
    failure rather than as output worth acting on.

    Pure and idempotent: repaired history has no dangling calls, so a second
    pass returns it unchanged (the same list object when nothing was missing).
    """
    resolved = {
        block.tool_use_id
        for message in history
        for block in message.content
        if isinstance(block, ToolResultBlock)
    }
    out: list[Message] = []
    repaired = False
    for message in history:
        out.append(message)
        missing: list[str] = []
        for block in message.content:
            if (
                isinstance(block, ToolUseBlock)
                and block.id
                and block.id not in resolved
                and block.id not in missing
            ):
                missing.append(block.id)
        if not missing:
            continue
        repaired = True
        resolved.update(missing)
        out.append(Message(
            role="user",
            content=[
                ToolResultBlock(
                    tool_use_id=call_id,
                    content=[Text(LOST_TOOL_RESULT_TEXT)],
                    is_error=True,
                )
                for call_id in missing
            ],
        ))
    return out if repaired else history


class NativeLoop:
    caps = EngineCaps(
        inproc_tools=True, schema_plan_mode=True,
        fork=True, rewind=True, cache_metrics=True,
    )

    def __init__(
        self,
        *,
        provider: ProviderClient,
        store: SessionStore,
        bus: EventBus | None = None,
        session_id: str | None = None,
        model: str,
        system: str | None = None,
        max_tokens: int = 8192,
        effort: str | None = None,
        registry: ToolRegistry | None = None,
        permissions: PermissionEngine | None = None,
        hooks: HookBus | None = None,
        ctx: ToolCtx | None = None,
        plan_mode: bool = False,
        codemode: "CodeModeSession | None" = None,
        max_model_calls_per_turn: int = 50,
        trim: TrimConfig | None = None,
        mcp_servers: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.provider = provider
        self.store = store
        self.bus = bus or EventBus()
        self.model = model
        self.system = system
        self.max_tokens = max_tokens
        # Reasoning-depth knob, provider-mapped by each serializer (Anthropic
        # output_config.effort; OpenAI-shaped reasoning.effort, clamped).
        self.effort = effort
        self.registry = registry
        self.permissions = permissions
        self.hooks = hooks
        self.ctx = ctx or ToolCtx()
        if registry is not None:
            # The ToolSearch builtin reaches the registry through the ctx.
            self.ctx.extra.setdefault("tool_registry", registry)
        self.plan_mode = plan_mode
        self._codemode = codemode
        if codemode is not None and registry is not None:
            register_code_mode_tools(registry, codemode)
        self.max_model_calls_per_turn = max_model_calls_per_turn
        self._session: SessionMeta | None = None
        self._session_id = session_id
        self._interrupted = asyncio.Event()
        self._turn_task: asyncio.Task | None = None
        self._pending_permissions: dict[str, asyncio.Future] = {}
        self._pending_questions: dict[str, asyncio.Future] = {}
        # Nested-instruction injection: instruction-file paths already injected
        # this session (each nested CLAUDE.md/AGENTS.md enters context once).
        self._nested_injected: set[str] = set()
        # Deferred-catalog injection: tool names already announced to the model
        # (each known-but-unloaded tool is listed once, on the user turn after
        # it becomes known — the static system prompt stays byte-stable).
        self._deferred_announced: set[str] = set()
        # Cache-safe tool-result trimming (view-level; the stored tree keeps
        # full fidelity). Sticky per engine lifetime — see context/trim.py.
        self._trimmer = ToolResultTrimmer(config=trim or TrimConfig())
        # MCP servers from config, connected lazily on the first turn (the
        # constructor is sync; connecting needs the event loop).
        self._mcp_servers = dict(mcp_servers or {})
        self._mcp_connected = False

    async def _ensure_session(self) -> SessionMeta:
        if self._session is None:
            if self._session_id is not None:
                self._session = await self.store.get_session(self._session_id)
            if self._session is None:
                self._session = await self.store.create_session(self._session_id)
        return self._session

    @property
    def session_id(self) -> str:
        assert self._session is not None, "no session yet — submit a turn first"
        return self._session.session_id

    # ─── ExecutionEngine interface ───────────────────────────────────────

    async def submit(self, op: Op) -> None:
        if isinstance(op, UserTurn):
            if self._turn_task and not self._turn_task.done():
                raise RuntimeError("a turn is already running; Interrupt it first")
            self._interrupted.clear()
            self._turn_task = asyncio.create_task(self._run_turn(op))
        elif isinstance(op, Interrupt):
            self._interrupted.set()
        elif isinstance(op, RespondPermission):
            fut = self._pending_permissions.get(op.request_id)
            if fut is not None and not fut.done():
                fut.set_result(op)
        elif isinstance(op, RespondQuestion):
            fut = self._pending_questions.get(op.request_id)
            if fut is not None and not fut.done():
                fut.set_result(op)
        elif isinstance(op, SetModel):
            # Hot-swap: the next _build_request reads self.model.
            self.model = op.model
        elif isinstance(op, SetPermissionMode):
            # Accept omni's native policies directly, plus the Claude-Code-style
            # mode names clients (AI-Shuffle, the CLI) already speak.
            mode = op.mode
            policy = mode if mode in ("never_ask", "on_write", "always_ask") else {
                "bypassPermissions": "never_ask",
                "acceptEdits": "on_write",
                "default": "on_write",
                "plan": "always_ask",
            }.get(mode, "on_write")
            if self.permissions is not None:
                self.permissions.approval_policy = policy
            self.plan_mode = mode == "plan"
        elif isinstance(op, Handoff):
            await self._do_handoff(op)
        elif isinstance(op, Fork):
            # Move the head to an earlier node; the next parent-less UserTurn
            # continues from there. Nothing is copied — the shared prefix stays
            # byte-identical, which is what makes forks cache-preserving.
            meta = await self._ensure_session()
            await self.store.set_head(meta.session_id, op.from_node_id)
            meta.head_node_id = op.from_node_id
        elif isinstance(op, Rewind):
            # Move the head back to ``to_node_id``. Nodes after it remain in the
            # tree but off-path; a subsequent turn branches a sibling.
            meta = await self._ensure_session()
            await self.store.set_head(meta.session_id, op.to_node_id)
            meta.head_node_id = op.to_node_id
        else:
            raise NotImplementedError(f"op not supported: {type(op).__name__}")

    async def _do_handoff(self, op: Handoff) -> None:
        """Amp-style handoff: ask the model for a structured handoff document
        over the current session, then seed a NEW session with it as the first
        user message. Nothing is summarized in place — the old session stays
        intact; the new one starts small, with a fresh cacheable prefix.
        """
        meta = await self._ensure_session()
        nodes = await self.store.load_path(meta.session_id)

        # Bounded plain-text transcript for the summary call (newest last).
        lines: list[str] = []
        for node in nodes[-40:]:
            text = node.message.text().strip()
            if text:
                lines.append(f"{node.message.role}: {text[:2000]}")
        goal_line = f"\nThe stated goal for the next session: {op.goal}" if op.goal else ""
        prompt = (
            "Write a handoff document for a fresh agent session continuing this "
            "work. Structure it as: Goal, Current state (what is done and "
            "verified), Key decisions and constraints, Relevant files/paths, "
            "Next steps (ordered). Be specific; the reader has NO other "
            f"context.{goal_line}\n\n--- transcript ---\n" + "\n".join(lines)
        )
        request = ProviderRequest(
            model=self.model,
            messages=[Message(role="user", content=[Text(prompt)])],
            system="You write precise engineering handoff documents.",
            tools=[],
            max_tokens=self.max_tokens,
            cache_breakpoints=0,
        )
        parts: list[str] = []
        async for chunk in self.provider.stream(request):
            if chunk.kind == "text_delta":
                parts.append(chunk.payload["text"])
        content = "".join(parts).strip() or "(handoff document generation failed)"

        new_meta = await self.store.create_session()
        first = SessionNode(
            node_id=new_id("node"), parent_id=None,
            message=Message(role="user", content=[Text(content)]),
        )
        await self.store.append(new_meta.session_id, first)
        await self._emit(HandoffCreated(
            session_id=meta.session_id,
            new_session_id=new_meta.session_id,
            content=content,
            goal=op.goal,
        ))

    async def _emit(self, event: Event) -> Event:
        """Publish to the bus (assigns seq) and persist for durable replay."""
        sequenced = await self.bus.publish(event)
        await self.store.append_event(event.session_id, sequenced)
        return sequenced

    def events(self, *, since_seq: int = 0) -> AsyncIterator[Event]:
        assert self._session is not None, "no session yet — submit a turn first"
        return self.bus.subscribe(self.session_id, since_seq=since_seq)

    async def wait_turn(self) -> None:
        if self._turn_task:
            await self._turn_task

    async def aclose(self) -> None:
        self._interrupted.set()
        if self._turn_task and not self._turn_task.done():
            self._turn_task.cancel()
            try:
                await self._turn_task
            except asyncio.CancelledError:
                pass
        if self._codemode is not None:
            await self._codemode.aclose()
        await self.bus.close()

    # ─── Turn lifecycle ──────────────────────────────────────────────────

    async def _run_turn(self, op: UserTurn) -> None:
        meta = await self._ensure_session()
        sid = meta.session_id
        turn_id = new_id("turn")

        # Connect config MCP servers before building the user node so newly
        # known deferred tools can be announced on this very turn.
        await self._connect_mcp_once(sid, turn_id)
        content: list[Any] = (
            [Text(op.content)] if isinstance(op.content, str) else list(op.content)
        )
        reminder = self._deferred_catalog_reminder()
        if reminder is not None:
            content.append(Text(reminder))
        user_node = SessionNode(
            node_id=new_id("node"),
            parent_id=op.parent_node_id or meta.head_node_id,
            message=Message(role="user", content=content),
        )
        await self.store.append(sid, user_node)
        if self._codemode is not None:
            # Episodic half of the continual journal: the intent behind the turn.
            self._codemode.record_event("user", _plain_text(content))
        await self._emit(
            TurnStarted(
                session_id=sid, node_id=user_node.node_id, turn_id=turn_id,
                model=self.model, provider=self.provider.name,
            )
        )
        if self.hooks is not None:
            await self.hooks.emit(TURN_START, {"session_id": sid, "turn_id": turn_id})
        try:
            await self._agent_loop(sid, turn_id, user_node)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # surface, never swallow
            await self._emit(
                ErrorEvent(session_id=sid, turn_id=turn_id, message=repr(exc))
            )

    async def _connect_mcp_once(self, sid: str, turn_id: str) -> None:
        """Connect configured MCP servers on the first turn (idempotent).

        Their tools become searchable/loadable via ToolSearch (progressive
        disclosure) without entering the prompt schema. A server that fails to
        connect surfaces as a non-fatal ErrorEvent; the turn proceeds.
        """
        if self._mcp_connected or not self._mcp_servers or self.registry is None:
            return
        self._mcp_connected = True
        for name, cfg in self._mcp_servers.items():
            try:
                await self.registry.connect_mcp(name, dict(cfg))
            except Exception as exc:
                await self._emit(ErrorEvent(
                    session_id=sid, turn_id=turn_id,
                    message=f"mcp server {name!r} failed to connect: {exc!r}",
                    retriable=False,
                ))

    def _deferred_catalog_reminder(self) -> str | None:
        """Announce known-but-unloaded tools, once per tool (name + one-liner).

        Rides the user turn as a system-reminder block so the static system
        prompt — compiled before MCP servers connect — stays byte-stable for
        the cache. Without this the model has ToolSearch but no idea what
        exists to search for. Skipped under code-mode, where discovery goes
        through the in-kernel list_tools/search_tools instead.
        """
        if self.registry is None or self._codemode is not None:
            return None
        specs = [
            s for s in self.registry.deferred_tools()
            if s.name not in self._deferred_announced
        ]
        if not specs:
            return None
        self._deferred_announced.update(s.name for s in specs)
        return render_deferred_catalog(specs)

    # ─── Agent loop (model call → tool dispatch → repeat) ────────────────

    async def _agent_loop(self, sid: str, turn_id: str, user_node: SessionNode) -> None:
        parent = user_node
        num_calls = 0
        total_usage: dict[str, Any] = {}
        final_stop = "end_turn"
        overflow_retried = False

        while True:
            nodes = await self.store.load_path(sid, parent.node_id)
            history = repair_dangling_tool_calls(self._trimmer.apply(nodes))
            result = await self._stream_once(sid, turn_id, history)
            num_calls += 1
            if result.stop_reason == "error":
                if (
                    not overflow_retried
                    and result.error_message
                    and _is_context_overflow(result.error_message)
                    and self._trimmer.force_trim(nodes)
                ):
                    # The prompt outgrew the window: batch-trim everything
                    # eligible and retry the call once on the smaller view.
                    overflow_retried = True
                    continue
                await self._emit(ErrorEvent(
                    session_id=sid, turn_id=turn_id,
                    message=result.error_message or "provider error",
                    retriable=result.error_retriable,
                ))
                return  # leave the turn open-ended
            if "cost_usd" not in result.usage:
                # No provider-reported cost — fall back to the provider's
                # optional pricing-table estimate for this call.
                est = getattr(self.provider, "estimate_cost", None)
                if est is not None:
                    cost = est(self.model, result.usage)
                    if cost is not None:
                        result.usage["cost_usd"] = cost
            self._accumulate_usage(total_usage, result.usage)

            node = SessionNode(
                node_id=new_id("node"), parent_id=parent.node_id, message=result.message
            )
            await self.store.append(sid, node)
            await self._emit(Usage(
                session_id=sid, node_id=node.node_id, turn_id=turn_id,
                input_tokens=result.usage.get("input_tokens", 0),
                output_tokens=result.usage.get("output_tokens", 0),
                cache_read_tokens=result.usage.get("cache_read_input_tokens", 0),
                cache_creation_tokens=result.usage.get("cache_creation_input_tokens", 0),
                raw=dict(result.usage),
            ))
            parent = node

            if result.interrupted:
                await self._emit(Interrupted(session_id=sid, node_id=node.node_id, turn_id=turn_id))
                return

            # Only run the tool loop when tools are actually configured; without
            # a registry a tool_use stop is terminal (M0 behavior).
            dispatchable = (
                self.registry is not None
                and result.stop_reason == "tool_use"
                and bool(result.tool_uses)
            )
            if not dispatchable:
                final_stop = result.stop_reason
                break
            if num_calls >= self.max_model_calls_per_turn:
                final_stop = "max_calls"
                break

            blocks: list[Any] = []
            for tu in result.tool_uses:
                blocks.append(await self._dispatch_tool_interruptible(sid, turn_id, tu))
            tr_node = SessionNode(
                node_id=new_id("node"), parent_id=parent.node_id,
                message=Message(role="user", content=blocks),
            )
            await self.store.append(sid, tr_node)
            parent = tr_node
            if self._interrupted.is_set():
                # Interrupt landed during tool dispatch (e.g. while parked on a
                # permission prompt) — stop before issuing another model call.
                await self._emit(Interrupted(
                    session_id=sid, node_id=parent.node_id, turn_id=turn_id))
                return

        if self._codemode is not None:
            # Episodic journal: the turn's final text (acceptance/feedback signal).
            self._codemode.record_event("assistant", _plain_text(result.message.content))
        if self.hooks is not None:
            await self.hooks.emit(TURN_END, {"session_id": sid, "turn_id": turn_id})
        # Cost rides its own TurnComplete field; keep the usage dict token-shaped.
        cost_total = total_usage.pop("cost_usd", None)
        await self._emit(TurnComplete(
            session_id=sid, node_id=parent.node_id, turn_id=turn_id,
            stop_reason=final_stop or "end_turn", num_model_calls=num_calls,
            usage=dict(total_usage), cost_usd=cost_total,
        ))

    def _build_request(self, history: list[Message]) -> ProviderRequest:
        tools_api: list[dict[str, Any]] = []
        if self.registry is not None:
            if self._codemode is not None:
                # Code-mode replaces N schemas with the four-tool surface; the
                # work-tools stay in the registry (reachable via host calls) but
                # never enter the prompt schema.
                specs = [
                    s for s in self.registry.list_tools(loaded_only=True)
                    if s.source == "codemode"
                ]
            else:
                specs = self.registry.list_tools(loaded_only=True)
                if self.plan_mode:
                    specs = plan_mode_tools(specs)
            tools_api = [s.to_api() for s in specs]
        plan = plan_cache(self.provider.caps, has_tools=bool(tools_api))
        return ProviderRequest(
            model=self.model,
            messages=history,
            system=self.system,
            tools=tools_api,
            max_tokens=self.max_tokens,
            effort=self.effort,
            cache_breakpoints=plan.breakpoints,
        )

    async def _stream_once(
        self, sid: str, turn_id: str, history: list[Message]
    ) -> _CallResult:
        request = self._build_request(history)

        blocks: dict[int, dict[str, Any]] = {}
        usage_totals: dict[str, Any] = {}
        tool_uses: list[dict[str, Any]] = []
        stop_reason = "end_turn"
        interrupted = False

        async for chunk in self.provider.stream(request):
            if self._interrupted.is_set():
                interrupted = True
                break
            payload = chunk.payload
            if chunk.kind == "message_start":
                self._merge_usage(usage_totals, payload.get("usage", {}))
            elif chunk.kind == "block_start":
                index = payload["index"]
                blocks[index] = {
                    "type": payload["block_type"],
                    "text": "",
                    "args": "",
                    "signature": "",
                    "tool_name": payload.get("tool_name"),
                    "tool_use_id": payload.get("tool_use_id"),
                }
                await self._emit(
                    BlockStart(
                        session_id=sid, turn_id=turn_id, index=index,
                        block_type=payload["block_type"],
                        tool_name=payload.get("tool_name"),
                        tool_use_id=payload.get("tool_use_id"),
                    )
                )
            elif chunk.kind == "text_delta":
                blocks[payload["index"]]["text"] += payload["text"]
                await self._emit(
                    TextDelta(session_id=sid, turn_id=turn_id,
                              index=payload["index"], text=payload["text"])
                )
            elif chunk.kind == "thinking_delta":
                blocks[payload["index"]]["text"] += payload["text"]
                await self._emit(
                    ThinkingDelta(session_id=sid, turn_id=turn_id,
                                  index=payload["index"], text=payload["text"])
                )
            elif chunk.kind == "tool_args_delta":
                blocks[payload["index"]]["args"] += payload["partial_json"]
            elif chunk.kind == "signature_delta":
                blocks[payload["index"]]["signature"] += payload["signature"]
            elif chunk.kind == "block_stop":
                index = payload["index"]
                await self._emit(BlockStop(session_id=sid, turn_id=turn_id, index=index))
                block = blocks.get(index)
                if block is not None and payload.get("block"):
                    # Authoritative done item (OpenAI Responses): carries the
                    # encrypted reasoning content / full function-call arguments
                    # that the deltas alone don't (serialize.py docstring).
                    block["done_item"] = payload["block"]
                if block and block["type"] == "tool_use":
                    raw_args = block["args"]
                    if not raw_args:
                        # No deltas streamed — fall back to the done item's
                        # complete arguments (a JSON string on Responses).
                        fallback = (block.get("done_item") or {}).get("arguments")
                        if isinstance(fallback, str):
                            raw_args = fallback
                    args = json.loads(raw_args) if raw_args else {}
                    block["input"] = args
                    tool_uses.append({
                        "tool_use_id": block["tool_use_id"] or "",
                        "name": block["tool_name"] or "",
                        "input": args,
                    })
                    await self._emit(
                        ToolUse(session_id=sid, turn_id=turn_id,
                                tool_use_id=block["tool_use_id"] or "",
                                name=block["tool_name"] or "", input=args)
                    )
            elif chunk.kind == "usage":
                self._merge_usage(usage_totals, payload.get("usage", {}))
                if payload.get("stop_reason"):
                    stop_reason = payload["stop_reason"]
            elif chunk.kind == "error":
                # No ErrorEvent here — _agent_loop decides whether this error
                # is terminal or recoverable (context overflow → forced trim
                # + one retry) before surfacing it.
                return _CallResult(
                    message=self._assemble_message(blocks), usage=usage_totals,
                    stop_reason="error",
                    error_message=str(payload.get("message", payload)),
                    error_retriable=bool(payload.get("retriable")),
                )

        # Reasoning-channel answer fallback (observed on GLM-4.5 via
        # OpenRouter): the model ends its final call with the entire answer in
        # thinking_deltas and an EMPTY text channel. Without this, the turn's
        # Message.text() is "" and text-channel clients render nothing. When a
        # completed non-tool call produced thinking but zero text, promote the
        # thinking content to a synthetic text block (streamed as a TextDelta
        # so live clients see it too).
        if not interrupted and not tool_uses:
            has_text = any(
                b["type"] == "text" and b["text"] for b in blocks.values()
            )
            thinking_text = "".join(
                b["text"] for _, b in sorted(blocks.items())
                if b["type"] == "thinking" and b["text"]
            )
            if not has_text and thinking_text:
                index = (max(blocks) + 1) if blocks else 0
                await self._emit(BlockStart(
                    session_id=sid, turn_id=turn_id, index=index, block_type="text",
                ))
                await self._emit(TextDelta(
                    session_id=sid, turn_id=turn_id, index=index, text=thinking_text,
                ))
                await self._emit(BlockStop(session_id=sid, turn_id=turn_id, index=index))
                blocks[index] = {
                    "type": "text", "text": thinking_text, "args": "",
                    "signature": "", "tool_name": None, "tool_use_id": None,
                }

        return _CallResult(
            message=self._assemble_message(blocks),
            tool_uses=tool_uses,
            usage=usage_totals,
            stop_reason=stop_reason,
            interrupted=interrupted,
        )

    # ─── Tool dispatch ───────────────────────────────────────────────────

    async def _dispatch_tool_interruptible(
        self, sid: str, turn_id: str, tu: dict[str, Any]
    ) -> ToolResultBlock:
        """Dispatch one tool call, racing it against the interrupt flag.

        Interrupt used to be invisible here: the flag was polled only between
        provider stream chunks and after a full dispatch round, so a
        long-running tool (a sync Task worker, a slow shell command) pinned the
        turn arbitrarily long after the user pressed stop. An interrupt now
        cancels the in-flight dispatch. The cancelled call still surfaces as an
        is_error ToolResult — every tool_use must get a result or the
        transcript is malformed for the next model call — and the agent loop's
        existing post-dispatch interrupt check then ends the turn.
        """
        if not self._interrupted.is_set():
            dispatch = asyncio.create_task(self._dispatch_tool(sid, turn_id, tu))
            interrupt_wait = asyncio.create_task(self._interrupted.wait())
            try:
                done, _ = await asyncio.wait(
                    {dispatch, interrupt_wait}, return_when=asyncio.FIRST_COMPLETED
                )
            finally:
                interrupt_wait.cancel()
            # Prefer the finished dispatch even when the interrupt landed in the
            # same tick — a real result beats a synthesized cancellation.
            if dispatch in done:
                return dispatch.result()
            dispatch.cancel()
            try:
                await dispatch
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        text = "Tool execution cancelled: the turn was interrupted."
        await self._emit(ToolResult(
            session_id=sid, turn_id=turn_id, tool_use_id=tu["tool_use_id"],
            name=tu["name"], output=text, is_error=True,
        ))
        return ToolResultBlock(
            tool_use_id=tu["tool_use_id"], content=[Text(text)], is_error=True,
        )

    async def _dispatch_tool(
        self, sid: str, turn_id: str, tu: dict[str, Any]
    ) -> ToolResultBlock:
        tool_use_id = tu["tool_use_id"]
        # Expose the call's id to handlers for the duration of the dispatch
        # (dispatches are sequential): host tools that spawn UI-joined work
        # (the Task dispatcher) need it to correlate their cards with this
        # tool_use block.
        self.ctx.extra["tool_use_id"] = tool_use_id
        try:
            outcome = await self._gated_call(sid, turn_id, tu["name"], tool_use_id, tu["input"])
        finally:
            self.ctx.extra.pop("tool_use_id", None)
        content = [Text(outcome.text)]
        # Nested-instruction injection (instruction-file seam): when a tool
        # touches a path whose own directory carries a CLAUDE.md/AGENTS.md not
        # in the project stack, ride those instructions into context on this
        # tool result — model-facing only, once per file per session.
        reminder = self._nested_instruction_reminder(tu["input"])
        if reminder:
            content.append(Text(reminder))
        return ToolResultBlock(
            tool_use_id=tool_use_id,
            content=content,
            is_error=outcome.is_error,
        )

    # Tool-input keys that name a filesystem target worth checking for nested
    # instruction files.
    _PATH_INPUT_KEYS = ("file_path", "path", "notebook_path", "directory")

    def _nested_instruction_reminder(self, input: dict[str, Any]) -> str | None:
        from pathlib import Path

        from omni.context.instructions import _walk_up_dirs, nested_instructions

        if not isinstance(input, dict):
            return None
        targets = [
            v for k in self._PATH_INPUT_KEYS
            if isinstance((v := input.get(k)), str) and v
        ]
        if not targets:
            return None
        # Directories already covered by the project stack (git root → cwd)
        # never re-inject.
        try:
            covered = {str(d) for d in _walk_up_dirs(Path(self.ctx.cwd or "."))}
        except OSError:
            covered = set()
        blocks: list[str] = []
        for target in targets:
            try:
                for f in nested_instructions(target):
                    directory = str(Path(f.path).parent)
                    if directory in covered or f.path in self._nested_injected:
                        continue
                    self._nested_injected.add(f.path)
                    if f.content.strip():
                        blocks.append(f"# From: {f.path}\n{f.content.rstrip()}")
            except OSError:
                continue
        if not blocks:
            return None
        joined = "\n\n".join(blocks)
        return (
            "<system-reminder>\nInstructions apply to the directory just "
            f"accessed:\n\n{joined}\n</system-reminder>"
        )

    async def _gated_call(
        self, sid: str, turn_id: str, name: str, tool_use_id: str,
        input: dict[str, Any],
    ) -> ToolOutcome:
        """The gated tool-execution path for the JSON tool surface:
        PreToolUse hook → PermissionEngine → dispatch → PostToolUse.

        ``run_python`` is intercepted here and handed to the code-mode session,
        whose in-kernel tool calls are gated host-side by ``HostBridge`` (the
        same hook + permission + doom-loop pipeline, including the
        ``code_mode_allowed=False`` escape hatch).
        """
        assert self.registry is not None
        spec = self.registry.get(name)

        # PreToolUse hook: deny short-circuits with an in-context error result;
        # modify replaces the input before permissions/dispatch see it.
        if self.hooks is not None:
            decision = await self.hooks.emit(
                PRE_TOOL_USE, {"name": name, "input": input, "tool_use_id": tool_use_id}
            )
            if decision.action == "deny":
                return await self._finish_tool(
                    sid, turn_id, name, tool_use_id,
                    ToolOutcome(text=f"blocked by hook: {decision.note or 'denied'}", is_error=True),
                )
            if decision.action == "modify" and decision.replacement_input is not None:
                input = decision.replacement_input

        # Permission gate.
        if self.permissions is not None and spec is not None:
            verdict = self.permissions.evaluate(spec, input)
            deny_message: str | None = None
            if verdict == "allow" and self.permissions.note_execution(name, input):
                # Doom-loop checkpoint. Under never_ask there may be nobody
                # listening for permission prompts (headless embedders), so an
                # "ask" would park the turn forever — deny with in-context
                # feedback instead; that alone breaks the repeat pattern.
                if self.permissions.approval_policy == "never_ask":
                    return await self._finish_tool(
                        sid, turn_id, name, tool_use_id,
                        ToolOutcome(
                            text=(
                                "doom-loop guard: this exact tool call has now "
                                "been repeated several times in a row — not "
                                "running it again. Change the input or the "
                                "approach."
                            ),
                            is_error=True,
                        ),
                    )
                verdict = "ask"
            if verdict == "ask":
                verdict, deny_message, updated_input = await self._await_permission(
                    sid, turn_id, name, tool_use_id, input, spec.risk,
                )
                if verdict != "deny" and updated_input is not None:
                    input = updated_input
            if verdict == "deny":
                return await self._finish_tool(
                    sid, turn_id, name, tool_use_id,
                    # The responder's reason is what the model reads back; only
                    # fall back to the generic text when none was given.
                    ToolOutcome(text=deny_message or "permission denied", is_error=True),
                )

        # run_python is engine-driven: it opens the kernel; the kernel's in-sandbox
        # tool calls are gated by HostBridge (see CodeModeSession.run_python).
        if self._codemode is not None and name == "run_python":
            outcome = await self._codemode.run_python(
                sid=sid, turn_id=turn_id, input=input, loop=self
            )
        else:
            outcome = await self.registry.dispatch(name, input, self.ctx)

        # MRTR (MCP spec 2026-07-28): a tools/call can pause on input_required.
        # Collect answers via QuestionRequest/RespondQuestion round-trips, then
        # retry the SAME call with inputResponses + the echoed request_state.
        # Bounded — a server that keeps pausing eventually gets its marker
        # surfaced as an in-context error instead of wedging the turn.
        rounds = 0
        while outcome.input_required is not None and rounds < 3:
            rounds += 1
            marker = outcome.input_required
            answers = await self._collect_input_responses(
                sid, turn_id, name, tool_use_id, marker
            )
            if answers is None:  # interrupted mid-question
                return await self._finish_tool(
                    sid, turn_id, name, tool_use_id,
                    ToolOutcome(text="input_required round-trip interrupted", is_error=True),
                )
            outcome = await self.registry.dispatch(
                name, input, self.ctx,
                input_responses=answers, request_state=marker.request_state,
            )
        if outcome.input_required is not None:
            outcome = ToolOutcome(
                text=f"tool {name} still input_required after {rounds} round-trips",
                is_error=True,
            )
        return await self._finish_tool(sid, turn_id, name, tool_use_id, outcome)

    async def _collect_input_responses(
        self, sid: str, turn_id: str, name: str, tool_use_id: str, marker: Any
    ) -> dict[str, Any] | None:
        """One QuestionRequest/RespondQuestion round-trip per MRTR request.

        Returns {server-request-id: answer} for the retry's ``inputResponses``,
        or None if the turn was interrupted while parked on a question.
        """
        answers: dict[str, Any] = {}
        for server_req_id, descriptor in (marker.requests or {}).items():
            request_id = new_id("q")
            fut: asyncio.Future = asyncio.get_running_loop().create_future()
            self._pending_questions[request_id] = fut
            desc = descriptor if isinstance(descriptor, dict) else {}
            await self._emit(QuestionRequest(
                session_id=sid, turn_id=turn_id, request_id=request_id,
                tool_use_id=tool_use_id, name=name,
                message=str(desc.get("message", "") or f"{marker.server} needs input for {name}"),
                schema=desc.get("schema"),
            ))
            interrupt_wait = asyncio.create_task(self._interrupted.wait())
            try:
                done, _ = await asyncio.wait(
                    {fut, interrupt_wait}, return_when=asyncio.FIRST_COMPLETED
                )
                if fut not in done:
                    return None
                resp: RespondQuestion = fut.result()
            finally:
                interrupt_wait.cancel()
                self._pending_questions.pop(request_id, None)
            answers[server_req_id] = resp.answer
        return answers

    async def _await_permission(
        self, sid: str, turn_id: str, name: str, tool_use_id: str,
        input: dict[str, Any], risk: str,
    ) -> tuple[str, str | None, dict[str, Any] | None]:
        """Publish a PermissionRequest and park the turn until RespondPermission.

        Returns ``(verdict, deny_message, updated_input)``: the responder's
        reason (surfaced as the denied tool's output) and any rewritten tool
        input it handed back on an allow.
        """
        request_id = new_id("perm")
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending_permissions[request_id] = fut
        await self._emit(PermissionRequest(
            session_id=sid, turn_id=turn_id, request_id=request_id,
            tool_use_id=tool_use_id, name=name, input=input, risk=risk,
        ))
        interrupt_wait = asyncio.create_task(self._interrupted.wait())
        try:
            done, _ = await asyncio.wait(
                {fut, interrupt_wait}, return_when=asyncio.FIRST_COMPLETED
            )
            if fut not in done:  # Interrupt arrived while parked on the prompt
                return ("deny", "interrupted while awaiting permission", None)
            resp: RespondPermission = fut.result()
        finally:
            interrupt_wait.cancel()
            self._pending_permissions.pop(request_id, None)
        allowed = resp.decision in ("allow", "allow_always")
        if resp.decision == "allow_always" and self.permissions is not None:
            # Sticky for the rest of the session: future calls to this tool
            # skip the prompt (fix: allow_always used to be a one-shot allow).
            self.permissions.allow_always(name)
        return (
            "allow" if allowed else "deny",
            resp.message,
            resp.updated_input if allowed else None,
        )

    async def _finish_tool(
        self, sid: str, turn_id: str, name: str, tool_use_id: str, outcome: ToolOutcome
    ) -> ToolOutcome:
        if self.hooks is not None:
            await self.hooks.emit(
                POST_TOOL_USE,
                {"name": name, "tool_use_id": tool_use_id,
                 "output": outcome.text, "is_error": outcome.is_error},
            )
        await self._emit(ToolResult(
            session_id=sid, turn_id=turn_id, tool_use_id=tool_use_id, name=name,
            output=outcome.text, is_error=outcome.is_error, ui=outcome.ui,
        ))
        return outcome

    @staticmethod
    def _merge_usage(totals: dict[str, Any], usage: dict[str, Any]) -> None:
        """WITHIN one model call: usage chunks are cumulative snapshots
        (message_start carries input counts, the terminal chunk output counts),
        so token keys take max(); a provider-reported cost rides one terminal
        chunk and sums harmlessly."""
        for key, value in usage.items():
            if key == "cost_usd":
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    totals[key] = totals.get(key, 0.0) + float(value)
                continue
            if isinstance(value, int):
                totals[key] = max(totals.get(key, 0), value)

    @staticmethod
    def _accumulate_usage(totals: dict[str, Any], usage: dict[str, Any]) -> None:
        """ACROSS the agent loop's model calls: every call bills its own
        tokens, so token keys (and cost) SUM into the turn totals."""
        for key, value in usage.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            totals[key] = totals.get(key, 0) + value

    def _assemble_message(self, blocks: dict[int, dict[str, Any]]) -> Message:
        content: list[Any] = []
        for index in sorted(blocks):
            block = blocks[index]
            if block["type"] == "text":
                content.append(Text(block["text"]))
            elif block["type"] == "thinking":
                content.append(Thinking(block["text"]))
                content.append(self._reasoning_envelope(block))
            elif block["type"] == "tool_use":
                content.append(
                    ToolUseBlock(id=block["tool_use_id"] or "",
                                 name=block["tool_name"] or "",
                                 input=block.get("input", {}))
                )
        return Message(role="assistant", content=content)

    def _reasoning_envelope(self, block: dict[str, Any]) -> Reasoning:
        """Build the provider-tagged Reasoning envelope for one thinking block.

        The origin MUST be the provider that produced the reasoning — each
        serializer round-trips only its own origin and drops foreign ones. For
        OpenAI Responses the authoritative reasoning item (encrypted_content)
        arrives as the block's done item; for Anthropic it's the accumulated
        thinking text + signature.
        """
        origin = self.provider.name
        done = block.get("done_item")
        if origin == "openai_responses":
            return Reasoning(
                origin=origin,
                summary_text=block["text"] or None,
                encrypted_content=(done or {}).get("encrypted_content"),
                raw=done,
            )
        if origin == "anthropic":
            return Reasoning(
                origin=origin,
                summary_text=block["text"],
                signature=block["signature"] or None,
                raw={"type": "thinking", "thinking": block["text"],
                     "signature": block["signature"]},
            )
        # Other providers (openrouter, ...): no verbatim round-trip contract —
        # keep the summary text; serializers drop what they can't resend.
        return Reasoning(origin=origin, summary_text=block["text"] or None, raw=done)
