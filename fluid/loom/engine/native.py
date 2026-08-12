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

from loom.context.cache_plan import plan_cache
from loom.engine.base import EngineCaps
from loom.events.bus import EventBus
from loom.events.model import (
    BlockStart,
    BlockStop,
    ErrorEvent,
    Event,
    Interrupted,
    PermissionRequest,
    TextDelta,
    ThinkingDelta,
    ToolResult,
    ToolUse,
    TurnComplete,
    TurnStarted,
    Usage,
)
from loom.events.ops import Fork, Interrupt, Op, RespondPermission, Rewind, UserTurn
from loom.hooks.bus import (
    POST_TOOL_USE,
    PRE_TOOL_USE,
    TURN_END,
    TURN_START,
    HookBus,
)
from loom.messages import Message, Reasoning, Text, Thinking, ToolResultBlock, ToolUseBlock
from loom.permissions.engine import PermissionEngine, plan_mode_tools
from loom.providers.base import ProviderClient, ProviderRequest
from loom.codemode.session import CodeModeSession, register_code_mode_tools
from loom.store.base import SessionMeta, SessionNode, SessionStore, new_id
from loom.tools.registry import ToolCtx, ToolOutcome, ToolRegistry


@dataclass
class _CallResult:
    """Outcome of one streamed model call."""

    message: Message
    tool_uses: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    stop_reason: str = "end_turn"
    interrupted: bool = False


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
        registry: ToolRegistry | None = None,
        permissions: PermissionEngine | None = None,
        hooks: HookBus | None = None,
        ctx: ToolCtx | None = None,
        plan_mode: bool = False,
        codemode: "CodeModeSession | None" = None,
        max_model_calls_per_turn: int = 50,
    ) -> None:
        self.provider = provider
        self.store = store
        self.bus = bus or EventBus()
        self.model = model
        self.system = system
        self.max_tokens = max_tokens
        self.registry = registry
        self.permissions = permissions
        self.hooks = hooks
        self.ctx = ctx or ToolCtx()
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

        content = [Text(op.content)] if isinstance(op.content, str) else list(op.content)
        user_node = SessionNode(
            node_id=new_id("node"),
            parent_id=op.parent_node_id or meta.head_node_id,
            message=Message(role="user", content=content),
        )
        await self.store.append(sid, user_node)
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

    # ─── Agent loop (model call → tool dispatch → repeat) ────────────────

    async def _agent_loop(self, sid: str, turn_id: str, user_node: SessionNode) -> None:
        parent = user_node
        num_calls = 0
        total_usage: dict[str, int] = {}
        final_stop = "end_turn"

        while True:
            history = [n.message for n in await self.store.load_path(sid, parent.node_id)]
            result = await self._stream_once(sid, turn_id, history)
            num_calls += 1
            if result.stop_reason == "error":
                return  # ErrorEvent already emitted; leave the turn open-ended
            self._merge_usage(total_usage, result.usage)

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
                blocks.append(await self._dispatch_tool(sid, turn_id, tu))
            tr_node = SessionNode(
                node_id=new_id("node"), parent_id=parent.node_id,
                message=Message(role="user", content=blocks),
            )
            await self.store.append(sid, tr_node)
            parent = tr_node

        if self.hooks is not None:
            await self.hooks.emit(TURN_END, {"session_id": sid, "turn_id": turn_id})
        await self._emit(TurnComplete(
            session_id=sid, node_id=parent.node_id, turn_id=turn_id,
            stop_reason=final_stop or "end_turn", num_model_calls=num_calls,
            usage=dict(total_usage),
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
            cache_breakpoints=plan.breakpoints,
        )

    async def _stream_once(
        self, sid: str, turn_id: str, history: list[Message]
    ) -> _CallResult:
        request = self._build_request(history)

        blocks: dict[int, dict[str, Any]] = {}
        usage_totals: dict[str, int] = {}
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
                if block and block["type"] == "tool_use":
                    args = json.loads(block["args"]) if block["args"] else {}
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
                await self._emit(
                    ErrorEvent(session_id=sid, turn_id=turn_id,
                               message=str(payload.get("message", payload)),
                               retriable=bool(payload.get("retriable")))
                )
                return _CallResult(
                    message=self._assemble_message(blocks), usage=usage_totals,
                    stop_reason="error",
                )

        return _CallResult(
            message=self._assemble_message(blocks),
            tool_uses=tool_uses,
            usage=usage_totals,
            stop_reason=stop_reason,
            interrupted=interrupted,
        )

    # ─── Tool dispatch ───────────────────────────────────────────────────

    async def _dispatch_tool(
        self, sid: str, turn_id: str, tu: dict[str, Any]
    ) -> ToolResultBlock:
        tool_use_id = tu["tool_use_id"]
        outcome = await self._gated_call(sid, turn_id, tu["name"], tool_use_id, tu["input"])
        return ToolResultBlock(
            tool_use_id=tool_use_id,
            content=[Text(outcome.text)],
            is_error=outcome.is_error,
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
            if verdict == "allow" and self.permissions.note_execution(name, input):
                verdict = "ask"  # doom-loop checkpoint
            if verdict == "ask":
                verdict = await self._await_permission(sid, turn_id, name, tool_use_id, input, spec.risk)
            if verdict == "deny":
                return await self._finish_tool(
                    sid, turn_id, name, tool_use_id,
                    ToolOutcome(text="permission denied", is_error=True),
                )

        # run_python is engine-driven: it opens the kernel; the kernel's in-sandbox
        # tool calls are gated by HostBridge (see CodeModeSession.run_python).
        if self._codemode is not None and name == "run_python":
            outcome = await self._codemode.run_python(
                sid=sid, turn_id=turn_id, input=input, loop=self
            )
        else:
            outcome = await self.registry.dispatch(name, input, self.ctx)
        return await self._finish_tool(sid, turn_id, name, tool_use_id, outcome)

    async def _await_permission(
        self, sid: str, turn_id: str, name: str, tool_use_id: str,
        input: dict[str, Any], risk: str,
    ) -> str:
        """Publish a PermissionRequest and park the turn until RespondPermission."""
        request_id = new_id("perm")
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending_permissions[request_id] = fut
        await self._emit(PermissionRequest(
            session_id=sid, turn_id=turn_id, request_id=request_id,
            tool_use_id=tool_use_id, name=name, input=input, risk=risk,
        ))
        try:
            resp: RespondPermission = await fut
        finally:
            self._pending_permissions.pop(request_id, None)
        return "allow" if resp.decision in ("allow", "allow_always") else "deny"

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
    def _merge_usage(totals: dict[str, int], usage: dict[str, Any]) -> None:
        for key, value in usage.items():
            if isinstance(value, int):
                totals[key] = max(totals.get(key, 0), value)

    @staticmethod
    def _assemble_message(blocks: dict[int, dict[str, Any]]) -> Message:
        content: list[Any] = []
        for index in sorted(blocks):
            block = blocks[index]
            if block["type"] == "text":
                content.append(Text(block["text"]))
            elif block["type"] == "thinking":
                content.append(Thinking(block["text"]))
                content.append(
                    Reasoning(
                        origin="anthropic",
                        summary_text=block["text"],
                        signature=block["signature"] or None,
                        raw={"type": "thinking", "thinking": block["text"],
                             "signature": block["signature"]},
                    )
                )
            elif block["type"] == "tool_use":
                content.append(
                    ToolUseBlock(id=block["tool_use_id"] or "",
                                 name=block["tool_name"] or "",
                                 input=block.get("input", {}))
                )
        return Message(role="assistant", content=content)
