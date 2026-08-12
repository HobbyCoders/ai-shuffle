"""NativeLoop — the harness-owned agent loop (M0: text/thinking streaming, no tools).

Turn lifecycle:
  UserTurn → persist user node → build request (append-only prefix + cache
  plan) → stream provider chunks → re-emit deltas as Events → persist
  assistant node → TurnComplete.

Tool dispatch, permissions, hooks, and trimming attach to this loop in M2+;
the chunk-accumulation scaffolding for tool args is already in place so the
streaming grammar does not change shape later.
"""

from __future__ import annotations

import asyncio
import json
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
    TextDelta,
    ThinkingDelta,
    ToolUse,
    TurnComplete,
    TurnStarted,
    Usage,
)
from loom.events.ops import Interrupt, Op, UserTurn
from loom.messages import Message, Reasoning, Text, Thinking, ToolUseBlock
from loom.providers.base import ProviderClient, ProviderRequest
from loom.store.base import SessionMeta, SessionNode, SessionStore, new_id


class NativeLoop:
    caps = EngineCaps(fork=True, rewind=True, cache_metrics=True)

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
    ) -> None:
        self.provider = provider
        self.store = store
        self.bus = bus or EventBus()
        self.model = model
        self.system = system
        self.max_tokens = max_tokens
        self._session: SessionMeta | None = None
        self._session_id = session_id
        self._interrupted = asyncio.Event()
        self._turn_task: asyncio.Task | None = None

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
        else:
            raise NotImplementedError(f"op not supported in M0: {type(op).__name__}")

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
        await self.bus.publish(
            TurnStarted(
                session_id=sid, node_id=user_node.node_id, turn_id=turn_id,
                model=self.model, provider=self.provider.name,
            )
        )
        try:
            await self._model_call(sid, turn_id, user_node)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # surface, never swallow
            await self.bus.publish(
                ErrorEvent(session_id=sid, turn_id=turn_id, message=repr(exc))
            )

    async def _model_call(self, sid: str, turn_id: str, parent: SessionNode) -> None:
        history = [n.message for n in await self.store.load_path(sid, parent.node_id)]
        request = ProviderRequest(
            model=self.model,
            messages=history,
            system=self.system,
            max_tokens=self.max_tokens,
            cache_breakpoints=plan_cache(self.provider.caps).breakpoints,
        )

        blocks: dict[int, dict[str, Any]] = {}
        usage_totals: dict[str, int] = {}
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
                await self.bus.publish(
                    BlockStart(
                        session_id=sid, turn_id=turn_id, index=index,
                        block_type=payload["block_type"],
                        tool_name=payload.get("tool_name"),
                        tool_use_id=payload.get("tool_use_id"),
                    )
                )
            elif chunk.kind == "text_delta":
                blocks[payload["index"]]["text"] += payload["text"]
                await self.bus.publish(
                    TextDelta(session_id=sid, turn_id=turn_id,
                              index=payload["index"], text=payload["text"])
                )
            elif chunk.kind == "thinking_delta":
                blocks[payload["index"]]["text"] += payload["text"]
                await self.bus.publish(
                    ThinkingDelta(session_id=sid, turn_id=turn_id,
                                  index=payload["index"], text=payload["text"])
                )
            elif chunk.kind == "tool_args_delta":
                blocks[payload["index"]]["args"] += payload["partial_json"]
            elif chunk.kind == "signature_delta":
                blocks[payload["index"]]["signature"] += payload["signature"]
            elif chunk.kind == "block_stop":
                index = payload["index"]
                await self.bus.publish(BlockStop(session_id=sid, turn_id=turn_id, index=index))
                block = blocks.get(index)
                if block and block["type"] == "tool_use":
                    args = json.loads(block["args"]) if block["args"] else {}
                    block["input"] = args
                    await self.bus.publish(
                        ToolUse(session_id=sid, turn_id=turn_id,
                                tool_use_id=block["tool_use_id"] or "",
                                name=block["tool_name"] or "", input=args)
                    )
            elif chunk.kind == "usage":
                self._merge_usage(usage_totals, payload.get("usage", {}))
                if payload.get("stop_reason"):
                    stop_reason = payload["stop_reason"]
            elif chunk.kind == "error":
                await self.bus.publish(
                    ErrorEvent(session_id=sid, turn_id=turn_id,
                               message=str(payload.get("message", payload)),
                               retriable=bool(payload.get("retriable")))
                )
                return

        assistant_msg = self._assemble_message(blocks)
        node = SessionNode(node_id=new_id("node"), parent_id=parent.node_id, message=assistant_msg)
        await self.store.append(sid, node)

        usage_event = Usage(
            session_id=sid, node_id=node.node_id, turn_id=turn_id,
            input_tokens=usage_totals.get("input_tokens", 0),
            output_tokens=usage_totals.get("output_tokens", 0),
            cache_read_tokens=usage_totals.get("cache_read_input_tokens", 0),
            cache_creation_tokens=usage_totals.get("cache_creation_input_tokens", 0),
            raw=dict(usage_totals),
        )
        await self.bus.publish(usage_event)

        if interrupted:
            await self.bus.publish(Interrupted(session_id=sid, node_id=node.node_id, turn_id=turn_id))
            return
        await self.bus.publish(
            TurnComplete(session_id=sid, node_id=node.node_id, turn_id=turn_id,
                         stop_reason=stop_reason or "end_turn", num_model_calls=1,
                         usage=dict(usage_totals))
        )

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
