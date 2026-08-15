"""claude CLI stream-json → fluid Event mapping.

The delegated backend spawns the official ``claude`` CLI in
``--input-format stream-json --output-format stream-json`` mode. Every line the
CLI writes is a JSON-serialized ``claude_agent_sdk`` message. This module turns
that vocabulary into the *same* ``Event`` model ``NativeLoop`` emits, so a client
cannot tell the two engines apart (modulo the documented capability matrix).

Frame vocabulary handled (see ``claude_agent_sdk`` message_parser + query.py):

  * ``{"type":"system","subtype":"init","session_id":...}`` — captures
    ``cli_session_id`` (the ``--resume`` handle). TurnStarted is emitted by the
    loop at turn start (native parity: a persistent process only emits ``init``
    once, so binding TurnStarted to it would miss turns 2+).
  * ``{"type":"assistant","message":{"content":[...],"usage":{...},...}}`` — each
    content block becomes BlockStart + a whole-block delta + BlockStop (+ ToolUse
    for ``tool_use``); ``usage`` becomes a per-model-call Usage event. The full
    message is assembled into a ``SessionNode`` for persistence. NOTE: the CLI
    splits a multi-block model message into ONE assistant frame PER BLOCK, each
    repeating the same message ``id`` and ``usage`` — hence the message-level
    block cursor (in-frame positions are always 0) and the usage-once guard.
  * ``{"type":"user","message":{"content":[{"type":"tool_result",...}]}}`` —
    ToolResult events (name resolved via the ``tool_use_id`` → name map).
  * ``{"type":"stream_event","event":{<raw Anthropic SSE>}}`` — real streaming
    deltas (only with ``--include-partial-messages``). Reuses the Anthropic
    ``sse_to_chunk`` serializer, then maps chunks exactly as ``NativeLoop`` does.
    When partial streaming is on, the block indices seen here suppress the
    whole-block re-emission from the ``assistant`` frames of the SAME message
    (dedupe); ``message_start`` is what clears that state, one message at a time.
  * ``{"type":"result","subtype":...,"total_cost_usd":...,"num_turns":...}`` —
    TurnComplete (+ a fallback Usage if no assistant frame carried usage).
  * ``{"type":"rate_limit_event","rate_limit_info":{...}}`` — RateLimit.
  * ``{"type":"control_request","request":{"subtype":"can_use_tool",...}}`` —
    a PermissionRequest event plus a ``PermissionBridge`` the loop uses to answer
    with the matching ``control_response`` frame.

Unknown frame types are ignored (forward-compat). The mapping helpers are pure;
the ``StreamJsonMapper`` is the small stateful driver (block indices, the
tool-name map, dedupe tracking, node-id generation) around them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from fluid.events.model import (
    BlockStart,
    BlockStop,
    Event,
    PermissionRequest,
    RateLimit,
    TextDelta,
    ThinkingDelta,
    ToolResult,
    ToolUse,
    TurnComplete,
    Usage,
)
from fluid.messages import Message, Reasoning, Text, Thinking, ToolResultBlock, ToolUseBlock
from fluid.providers.anthropic.serialize import sse_to_chunk
from fluid.store.base import new_id


# ─── Pure helpers ────────────────────────────────────────────────────────────


def stop_reason_from_subtype(subtype: str | None) -> str:
    """CLI ``result.subtype`` → fluid stop_reason (``success`` → ``end_turn``)."""
    if subtype in (None, "success"):
        return "end_turn"
    return subtype


def flatten_tool_content(content: Any) -> str:
    """Reduce a tool_result ``content`` (str | list[block] | any) to model text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for c in content:
            if isinstance(c, dict):
                if c.get("type") == "text":
                    parts.append(c.get("text", ""))
                else:
                    parts.append(json.dumps(c, default=str))
            else:
                parts.append(str(c))
        return "".join(parts)
    return json.dumps(content, default=str)


def usage_fields(usage: dict[str, Any]) -> dict[str, Any]:
    """Extract the four canonical token counters (CLI key names == fluid's)."""
    return {
        "input_tokens": usage.get("input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "cache_read_tokens": usage.get("cache_read_input_tokens", 0),
        "cache_creation_tokens": usage.get("cache_creation_input_tokens", 0),
    }


# ─── Bridge + result records ─────────────────────────────────────────────────


@dataclass(frozen=True)
class PermissionBridge:
    """A ``can_use_tool`` control_request awaiting a ``control_response`` reply."""

    cli_request_id: str
    fluid_request_id: str
    tool_name: str
    tool_use_id: str
    input: dict[str, Any]
    risk: str = "safe"


@dataclass
class Mapped:
    """Everything one frame produces: events to emit, an optional node to
    persist, and (for permissions) the bridge the loop must answer."""

    events: list[Event] = field(default_factory=list)
    message: Message | None = None
    node_id: str | None = None
    permission: PermissionBridge | None = None
    is_result: bool = False


# ─── Stateful mapper ─────────────────────────────────────────────────────────


class StreamJsonMapper:
    def __init__(
        self,
        *,
        session_id: str,
        turn_id: str,
        model: str,
        provider: str = "claude",
        user_node_id: str,
    ) -> None:
        self.session_id = session_id
        self.turn_id = turn_id
        self.model = model
        self.provider = provider
        self.user_node_id = user_node_id
        self.cli_session_id: str | None = None

        self._tool_names: dict[str, str] = {}  # tool_use_id → tool name
        self._usage_emitted = False
        self._last_node_id: str = user_node_id
        # Dedupe: block indices already streamed via stream_event for the CURRENT
        # model message (their whole-block re-emission is suppressed). Cleared on
        # the message_start chunk — NOT per assistant frame: the CLI splits one
        # model message into one `assistant` frame PER CONTENT BLOCK, so a
        # per-frame reset would drop the dedupe state before the sibling frames
        # arrive and every block after the first would be re-emitted whole.
        self._streamed: set[int] = set()
        # In-flight stream_event tool_use blocks: index → accumulated args/name.
        self._se_tool: dict[int, dict[str, Any]] = {}
        # Message-level index of the next block an `assistant` frame reports.
        # Those per-block frames each carry a one-element content list, so the
        # in-frame position is always 0 and cannot be matched against the
        # message-level indices in ``_streamed``; this running cursor supplies
        # the real index. Reset on message_start with ``_streamed``.
        self._block_cursor = 0
        # Message id whose `usage` was already turned into a Usage event. Every
        # per-block frame of a message repeats that message's usage verbatim, so
        # without this a two-block message double-counts its tokens and cost.
        self._usage_msg_id: str | None = None

    # ─── frame dispatch ──────────────────────────────────────────────────

    def feed(self, frame: dict[str, Any]) -> Mapped:
        ftype = frame.get("type")
        if ftype == "system":
            return self._on_system(frame)
        if ftype == "assistant":
            return self._on_assistant(frame)
        if ftype == "user":
            return self._on_user(frame)
        if ftype == "stream_event":
            return self._on_stream_event(frame)
        if ftype == "result":
            return self._on_result(frame)
        if ftype == "rate_limit_event":
            return self._on_rate_limit(frame)
        if ftype == "control_request":
            return self._on_control_request(frame)
        # control_response (ours, echoed) and any unknown type: ignore.
        return Mapped()

    # ─── event builders (carry the fixed session/turn ids) ───────────────

    def _block_start(self, index: int, block_type: str, **kw: Any) -> BlockStart:
        return BlockStart(
            session_id=self.session_id, turn_id=self.turn_id,
            index=index, block_type=block_type, **kw,
        )

    def _block_stop(self, index: int) -> BlockStop:
        return BlockStop(session_id=self.session_id, turn_id=self.turn_id, index=index)

    def _text_delta(self, index: int, text: str) -> TextDelta:
        return TextDelta(session_id=self.session_id, turn_id=self.turn_id, index=index, text=text)

    def _thinking_delta(self, index: int, text: str) -> ThinkingDelta:
        return ThinkingDelta(session_id=self.session_id, turn_id=self.turn_id, index=index, text=text)

    def _tool_use(self, tool_use_id: str, name: str, input: dict[str, Any]) -> ToolUse:
        return ToolUse(
            session_id=self.session_id, turn_id=self.turn_id,
            tool_use_id=tool_use_id, name=name, input=input,
        )

    def _usage_event(self, node_id: str, usage: dict[str, Any]) -> Usage:
        return Usage(
            session_id=self.session_id, node_id=node_id, turn_id=self.turn_id,
            raw=dict(usage), **usage_fields(usage),
        )

    # ─── per-frame handlers ──────────────────────────────────────────────

    def _on_system(self, frame: dict[str, Any]) -> Mapped:
        if frame.get("subtype") == "init":
            self.cli_session_id = frame.get("session_id") or self.cli_session_id
        return Mapped()

    def _on_assistant(self, frame: dict[str, Any]) -> Mapped:
        msg = frame.get("message", {})
        blocks = msg.get("content", []) or []
        events: list[Event] = []
        content: list[Any] = []

        for index, block in enumerate(blocks):
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            # Suppression is decided on the MESSAGE-level index (the one the
            # stream events carry); emission keeps the in-frame index, which is
            # what the non-partial path has always used.
            streamed = (self._block_cursor + index) in self._streamed

            if btype == "text":
                text = block.get("text", "")
                content.append(Text(text))
                if not streamed:
                    events.append(self._block_start(index, "text"))
                    events.append(self._text_delta(index, text))
                    events.append(self._block_stop(index))
            elif btype == "thinking":
                thinking = block.get("thinking", "")
                signature = block.get("signature")
                content.append(Thinking(thinking))
                content.append(Reasoning(
                    origin="anthropic", summary_text=thinking, signature=signature,
                    raw={"type": "thinking", "thinking": thinking, "signature": signature},
                ))
                if not streamed:
                    events.append(self._block_start(index, "thinking"))
                    events.append(self._thinking_delta(index, thinking))
                    events.append(self._block_stop(index))
            elif btype in ("tool_use", "server_tool_use"):
                tid = block.get("id", "")
                name = block.get("name", "")
                inp = block.get("input", {}) or {}
                self._tool_names[tid] = name
                content.append(ToolUseBlock(id=tid, name=name, input=inp))
                if not streamed:
                    # Order mirrors NativeLoop: BlockStart, BlockStop, then ToolUse.
                    events.append(self._block_start(index, "tool_use", tool_name=name, tool_use_id=tid))
                    events.append(self._block_stop(index))
                    events.append(self._tool_use(tid, name, inp))
            # tool_result / advisor blocks inside an assistant frame are rare and
            # not part of the native event shape; left out deliberately.

        node_id = new_id("node")
        self._last_node_id = node_id

        usage = msg.get("usage")
        msg_id = msg.get("id")
        if usage and (msg_id is None or msg_id != self._usage_msg_id):
            events.append(self._usage_event(node_id, usage))
            self._usage_emitted = True
            self._usage_msg_id = msg_id

        # Advance past the blocks this frame reported: the next `assistant`
        # frame of the same message continues where this one stopped.
        self._block_cursor += len(blocks)

        return Mapped(events=events, message=Message(role="assistant", content=content), node_id=node_id)

    def _on_user(self, frame: dict[str, Any]) -> Mapped:
        msg = frame.get("message", {})
        raw_blocks = msg.get("content", [])
        events: list[Event] = []
        content: list[Any] = []

        blocks = raw_blocks if isinstance(raw_blocks, list) else []
        for block in blocks:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            tid = block.get("tool_use_id", "")
            name = self._tool_names.get(tid, "unknown")
            raw_content = block.get("content")
            text = flatten_tool_content(raw_content)
            is_error = bool(block.get("is_error"))
            content.append(ToolResultBlock(
                tool_use_id=tid, content=[Text(text)], is_error=is_error,
            ))
            events.append(ToolResult(
                session_id=self.session_id, turn_id=self.turn_id,
                tool_use_id=tid, name=name, output=text, is_error=is_error, ui=raw_content,
            ))

        if not content:
            return Mapped()

        node_id = new_id("node")
        self._last_node_id = node_id
        return Mapped(events=events, message=Message(role="user", content=content), node_id=node_id)

    def _on_stream_event(self, frame: dict[str, Any]) -> Mapped:
        event = frame.get("event", {}) or {}
        chunk = sse_to_chunk(event.get("type", ""), event)
        if chunk is None:
            return Mapped()
        kind = chunk.kind
        p = chunk.payload
        events: list[Event] = []

        if kind == "message_start":
            # A new model message begins: the previous message's streamed-block
            # bookkeeping no longer applies, and its `assistant` frames have all
            # been consumed by now (they are emitted per block, before or right
            # after the block's own stream events, never after the NEXT
            # message_start). Deliberately NOT message_stop: the CLI may flush a
            # message's per-block assistant frames after it.
            self._streamed.clear()
            self._se_tool.clear()
            self._block_cursor = 0
        elif kind == "block_start":
            index = p["index"]
            self._streamed.add(index)
            btype = p.get("block_type", "text")
            if btype == "tool_use":
                tid = p.get("tool_use_id")
                name = p.get("tool_name")
                self._se_tool[index] = {"args": "", "tool_use_id": tid, "name": name}
                if tid:
                    self._tool_names[tid] = name or ""
                events.append(self._block_start(index, "tool_use", tool_name=name, tool_use_id=tid))
            else:
                events.append(self._block_start(index, btype))
        elif kind == "text_delta":
            self._streamed.add(p["index"])
            events.append(self._text_delta(p["index"], p["text"]))
        elif kind == "thinking_delta":
            self._streamed.add(p["index"])
            events.append(self._thinking_delta(p["index"], p["text"]))
        elif kind == "tool_args_delta":
            block = self._se_tool.get(p["index"])
            if block is not None:
                block["args"] += p["partial_json"]
        elif kind == "signature_delta":
            pass  # captured verbatim from the assistant frame for the node
        elif kind == "block_stop":
            index = p["index"]
            events.append(self._block_stop(index))
            block = self._se_tool.get(index)
            if block is not None:
                args = json.loads(block["args"]) if block["args"] else {}
                events.append(self._tool_use(block["tool_use_id"] or "", block["name"] or "", args))
        # usage(message_delta) / message_stop from the partial stream are
        # ignored: Usage is emitted once from the assistant frame.
        return Mapped(events=events)

    def _on_result(self, frame: dict[str, Any]) -> Mapped:
        subtype = frame.get("subtype", "success")
        usage = frame.get("usage") or {}
        events: list[Event] = []
        if not self._usage_emitted and usage:
            events.append(self._usage_event(self._last_node_id, usage))
        num_turns = frame.get("num_turns") or 1
        events.append(TurnComplete(
            session_id=self.session_id, node_id=self._last_node_id, turn_id=self.turn_id,
            stop_reason=stop_reason_from_subtype(subtype),
            num_model_calls=int(num_turns),
            usage=dict(usage),
            cost_usd=frame.get("total_cost_usd"),
        ))
        return Mapped(events=events, is_result=True)

    def _on_rate_limit(self, frame: dict[str, Any]) -> Mapped:
        info = frame.get("rate_limit_info", {}) or {}
        return Mapped(events=[RateLimit(
            session_id=self.session_id, turn_id=self.turn_id,
            kind=info.get("rateLimitType") or info.get("status") or "unknown",
            resets_at=info.get("resetsAt"),
            utilization=info.get("utilization"),
        )])

    def _on_control_request(self, frame: dict[str, Any]) -> Mapped:
        req = frame.get("request", {}) or {}
        if req.get("subtype") != "can_use_tool":
            return Mapped()  # hook_callback / mcp_message / etc. are loop-internal
        fluid_rid = new_id("perm")
        tool_name = req.get("tool_name", "")
        tid = req.get("tool_use_id") or ""
        inp = req.get("input", {}) or {}
        risk = req.get("risk", "safe")
        bridge = PermissionBridge(
            cli_request_id=frame.get("request_id", ""), fluid_request_id=fluid_rid,
            tool_name=tool_name, tool_use_id=tid, input=inp, risk=risk,
        )
        event = PermissionRequest(
            session_id=self.session_id, turn_id=self.turn_id, request_id=fluid_rid,
            tool_use_id=tid, name=tool_name, input=inp, risk=risk,
        )
        return Mapped(events=[event], permission=bridge)
