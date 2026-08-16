"""Text-protocol tool calling for models without reliable native tool calls.

Many local models (small GGUF quants especially) either don't emit OpenAI
``tool_calls`` at all or emit them unreliably — and fluid's native loop ends
the turn when no tool_calls arrive. This module makes tool calling work anyway,
entirely at the provider layer: :class:`TextToolsAdapter` is a decorating
``ProviderClient`` that

* strips ``tools`` from the outgoing body and instead appends a compact
  tool-protocol instruction block (plus the JSON schemas) to the system prompt;
* serializes ``ToolUseBlock``/``ToolResultBlock`` history to the same text
  protocol (``<tool_call>{…}</tool_call>`` / ``<tool_result name="…">…
  </tool_result>``) so multi-turn transcripts stay coherent;
* incrementally parses the streamed text, suppressing the raw marker text and
  emitting synthetic ``block_start``/``tool_args_delta``/``block_stop``
  StreamChunks equivalent to native tool calling — the engine loop needs zero
  changes and can't tell the difference;
* rewrites the terminal ``usage`` chunk's stop_reason to ``tool_use`` when any
  call was parsed, so the loop dispatches instead of ending the turn.

Marker handling: a partial-marker suffix is held back at chunk boundaries so
half-written markers never leak into ``text_delta``; code-fence-wrapped blocks
(````` ```json\\n<tool_call>… `````) are recognized and the fence suppressed;
``<think>…</think>`` passes through as ``thinking_delta``s.

Malformed JSON inside a completed ``<tool_call>`` block: the tool_use is
emitted anyway — with the salvaged tool name when one can be found and an
``{"error": …, "raw": …}`` argument payload — so the tool layer produces a
clean in-context error the model can react to, instead of the turn dying.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any, AsyncIterator

from fluid.messages import Image, Message, Text, ToolResultBlock, ToolUseBlock
from fluid.providers.base import ProviderCaps, ProviderRequest, StreamChunk
from fluid.providers._chat_completions import system_message

TOOL_OPEN = "<tool_call>"
TOOL_CLOSE = "</tool_call>"
THINK_OPEN = "<think>"
THINK_CLOSE = "</think>"

# A tool_call opener, optionally preceded by an adjacent code-fence opener
# (```json\n etc.) that should be suppressed along with the marker.
_OPEN_RE = re.compile(
    r"(?P<fence>```[a-zA-Z0-9_-]{0,16}[ \t]*\r?\n[ \t]*)?<tool_call>"
)
# Closing fence right after a fenced block's </tool_call>.
_CLOSE_FENCE_RE = re.compile(r"^[ \t]*\r?\n?[ \t]*```[ \t]*(\r?\n)?")
# Inner fence, when the model fenced the JSON *inside* the markers.
_INNER_FENCE_OPEN_RE = re.compile(r"^```[a-zA-Z0-9_-]{0,16}[ \t]*\r?\n")
_INNER_FENCE_CLOSE_RE = re.compile(r"\r?\n[ \t]*```[ \t]*$")

_NAME_SALVAGE_RE = re.compile(r'"name"\s*:\s*"([^"\\]+)"')

# How far back a partial marker can start (longest hold-back candidate:
# a fence opener + language tag + newline + a partial "<tool_call>").
_HOLD_SCAN = 64


def _viable_marker_prefix(s: str) -> bool:
    """Could ``s`` still grow into ``<tool_call>``, ``<think>``, or a
    code-fence opener followed by ``<tool_call>``?"""
    if not s:
        return False
    if TOOL_OPEN.startswith(s) or THINK_OPEN.startswith(s):
        return True
    if s in ("`", "``"):
        return True  # may still become a ``` fence
    if s.startswith("```"):
        m = re.match(r"```[a-zA-Z0-9_-]{0,16}(\r?\n[ \t]*)?", s)
        if m is None:
            return False
        rest = s[m.end():]
        return rest == "" or TOOL_OPEN.startswith(rest)
    return False


# Parser events: ("text", str) | ("thinking", str) | ("tool_call", name, args)
ParserEvent = tuple


class TextToolParser:
    """Incremental parser: feed text deltas, get ordered events.

    Text outside markers streams through with a held-back suffix wherever that
    suffix could still be the start of a marker (or of a fence + marker), so a
    marker split across chunk boundaries never leaks. Tool-call JSON is
    buffered until its closing marker and emitted as one event.
    """

    def __init__(self) -> None:
        self._buf = ""
        self._state = "text"  # "text" | "call" | "think"
        self._fenced = False

    def feed(self, text: str) -> list[ParserEvent]:
        self._buf += text
        return self._drain(final=False)

    def finalize(self) -> list[ParserEvent]:
        """Flush at end of stream: held text is released; an unterminated
        ``<tool_call>`` is emitted as a call if its JSON parses, else restored
        as literal text so nothing silently disappears."""
        return self._drain(final=True)

    def _drain(self, final: bool) -> list[ParserEvent]:
        out: list[ParserEvent] = []
        while True:
            if self._state == "text":
                if not self._text_step(out, final):
                    break
            elif self._state == "call":
                if not self._call_step(out, final):
                    break
            else:
                if not self._think_step(out, final):
                    break
        return out

    # Each step returns True to keep draining, False when it consumed all it
    # can for now (held-back tail stays in the buffer).

    def _text_step(self, out: list[ParserEvent], final: bool) -> bool:
        m = _OPEN_RE.search(self._buf)
        t = self._buf.find(THINK_OPEN)
        call_at = m.start() if m else -1
        # Pick whichever marker opens earliest.
        if call_at >= 0 and (t < 0 or call_at <= t):
            if self._buf[:call_at]:
                out.append(("text", self._buf[:call_at]))
            self._fenced = bool(m.group("fence"))
            self._buf = self._buf[m.end():]
            self._state = "call"
            return True
        if t >= 0:
            if self._buf[:t]:
                out.append(("text", self._buf[:t]))
            self._buf = self._buf[t + len(THINK_OPEN):]
            self._state = "think"
            return True
        if final:
            if self._buf:
                out.append(("text", self._buf))
            self._buf = ""
            return False
        hold = self._hold_len()
        if len(self._buf) > hold:
            out.append(("text", self._buf[: len(self._buf) - hold]))
            self._buf = self._buf[len(self._buf) - hold:]
        return False

    def _call_step(self, out: list[ParserEvent], final: bool) -> bool:
        j = self._buf.find(TOOL_CLOSE)
        if j < 0:
            if final:
                out.append(self._unterminated_call(self._buf))
                self._buf = ""
            return False
        raw = self._buf[:j]
        self._buf = self._buf[j + len(TOOL_CLOSE):]
        if self._fenced:
            self._buf = _CLOSE_FENCE_RE.sub("", self._buf, count=1)
        out.append(self._parse_call(raw))
        self._state = "text"
        return True

    def _think_step(self, out: list[ParserEvent], final: bool) -> bool:
        j = self._buf.find(THINK_CLOSE)
        if j < 0:
            if final:
                if self._buf:
                    out.append(("thinking", self._buf))
                self._buf = ""
                return False
            n = len(self._buf)
            hold = 0
            for i in range(max(0, n - len(THINK_CLOSE)), n):
                if THINK_CLOSE.startswith(self._buf[i:]):
                    hold = n - i
                    break
            if n > hold:
                out.append(("thinking", self._buf[: n - hold]))
                self._buf = self._buf[n - hold:]
            return False
        if self._buf[:j]:
            out.append(("thinking", self._buf[:j]))
        self._buf = self._buf[j + len(THINK_CLOSE):]
        self._state = "text"
        return True

    def _hold_len(self) -> int:
        n = len(self._buf)
        for i in range(max(0, n - _HOLD_SCAN), n):
            if _viable_marker_prefix(self._buf[i:]):
                return n - i
        return 0

    def _parse_call(self, raw: str) -> ParserEvent:
        raw_s = raw.strip()
        # Model fenced the JSON inside the markers — strip that too.
        raw_s = _INNER_FENCE_OPEN_RE.sub("", raw_s)
        raw_s = _INNER_FENCE_CLOSE_RE.sub("", raw_s).strip()
        try:
            data = json.loads(raw_s)
        except (json.JSONDecodeError, ValueError):
            data = None
        if isinstance(data, dict) and isinstance(data.get("name"), str):
            args = data.get("arguments")
            if args is None:
                args = data.get("parameters", data.get("args"))
            if isinstance(args, str):
                # Some models double-encode the arguments object.
                try:
                    parsed = json.loads(args)
                    args = parsed if isinstance(parsed, dict) else {"value": parsed}
                except (json.JSONDecodeError, ValueError):
                    args = {"value": args}
            if args is None:
                args = {}
            if not isinstance(args, dict):
                args = {"value": args}
            return ("tool_call", data["name"], args)
        # Malformed: emit the tool_use anyway with a salvaged name (or "") and
        # an error payload — the tool layer errors in-context and the model
        # gets to retry, instead of the block vanishing or the turn dying.
        salvaged = _NAME_SALVAGE_RE.search(raw_s)
        return (
            "tool_call",
            salvaged.group(1) if salvaged else "",
            {
                "error": "malformed JSON inside <tool_call> block",
                "raw": raw_s[:2000],
            },
        )

    def _unterminated_call(self, raw: str) -> ParserEvent:
        """Stream ended inside a <tool_call> (e.g. max_tokens): emit the call
        if the JSON is complete, otherwise restore the literal text."""
        raw_s = raw.strip()
        try:
            data = json.loads(raw_s)
        except (json.JSONDecodeError, ValueError):
            data = None
        if isinstance(data, dict) and isinstance(data.get("name"), str):
            return self._parse_call(raw)
        return ("text", TOOL_OPEN + raw)


# ─── system-prompt protocol block ────────────────────────────────────────────


def render_tool_protocol(tools: list[dict[str, Any]]) -> str:
    """The instruction block + JSON schemas appended to the system prompt."""
    lines = [
        "# Tool calling",
        "You have access to the tools listed below. To call a tool, emit a block",
        "of EXACTLY this form in your reply:",
        "",
        '<tool_call>{"name": "tool_name", "arguments": {"param": "value"}}</tool_call>',
        "",
        "Rules:",
        '- The block content must be a single JSON object with "name" and "arguments".',
        '- "arguments" must be a JSON object matching the tool\'s input schema.',
        "- To make several calls in one reply, emit several <tool_call> blocks.",
        "- Do not wrap the block in code fences and do not put prose inside it.",
        "- After your reply, each call's output is returned to you in a",
        '  <tool_result name="..."> block; use it to continue the task.',
        "- Only the tools listed below exist. Never invent tool names.",
        "",
        "## Available tools",
    ]
    for tool in tools:
        lines.append(f"### {tool.get('name')}")
        description = tool.get("description", "")
        if description:
            lines.append(description)
        schema = tool.get("input_schema", {"type": "object", "properties": {}})
        lines.append(
            "Input schema: " + json.dumps(schema, separators=(",", ":"), ensure_ascii=False)
        )
        lines.append("")
    return "\n".join(lines).rstrip()


# ─── history serialization (blocks → protocol text) ──────────────────────────


def tool_call_text(name: str, args: dict[str, Any]) -> str:
    payload = json.dumps(
        {"name": name, "arguments": args}, separators=(",", ":"), ensure_ascii=False
    )
    return f"\n{TOOL_OPEN}{payload}{TOOL_CLOSE}"


def tool_result_to_text(block: ToolResultBlock, name: str) -> str:
    parts: list[str] = []
    for c in block.content:
        if isinstance(c, Text):
            parts.append(c.text)
        elif isinstance(c, Image):
            parts.append("[image omitted]")
        else:
            parts.append(json.dumps(c.to_dict(), separators=(",", ":"), ensure_ascii=False))
    body = "".join(parts)
    error = ' error="true"' if block.is_error else ""
    return f'<tool_result name="{name}"{error}>\n{body}\n</tool_result>'


def serialize_history(messages: list[Message]) -> list[Message]:
    """Rewrite tool blocks into their text-protocol form.

    ``ToolUseBlock`` → assistant text ``<tool_call>{…}</tool_call>``;
    ``ToolResultBlock`` → ``<tool_result name="…">…</tool_result>`` text (the
    name recovered from the matching call's id, since results only carry ids).
    Everything else passes through untouched; messages without tool blocks are
    returned as-is.
    """
    names = {
        block.id: block.name
        for msg in messages
        for block in msg.content
        if isinstance(block, ToolUseBlock)
    }
    out: list[Message] = []
    for msg in messages:
        if not any(
            isinstance(b, (ToolUseBlock, ToolResultBlock)) for b in msg.content
        ):
            out.append(msg)
            continue
        content: list[Any] = []
        for block in msg.content:
            if isinstance(block, ToolUseBlock):
                content.append(Text(tool_call_text(block.name, block.input)))
            elif isinstance(block, ToolResultBlock):
                content.append(
                    Text(tool_result_to_text(block, names.get(block.tool_use_id, "")))
                )
            else:
                content.append(block)
        out.append(Message(role=msg.role, content=content, provider_meta=msg.provider_meta))
    return out


# ─── the decorating ProviderClient ───────────────────────────────────────────


class TextToolsAdapter:
    """Wrap any ``ProviderClient`` so tool calling runs over the text protocol.

    Synthetic tool_use blocks use indexes from 1000 up and thinking blocks
    (from ``<think>``) from -1000 up, so they never collide with the inner
    client's block indexes and sort sensibly in message assembly (thinking
    before text, tool calls after).
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.name: str = inner.name
        self.caps: ProviderCaps = inner.caps

    def __getattr__(self, item: str) -> Any:
        # Delegate optional protocol extensions (estimate_cost, …).
        return getattr(self._inner, item)

    def _prepare(self, request: ProviderRequest) -> ProviderRequest:
        sm = system_message(request.system)
        system = sm["content"] if sm else ""
        if request.tools:
            protocol = render_tool_protocol(request.tools)
            system = f"{system}\n\n{protocol}" if system else protocol
        return ProviderRequest(
            model=request.model,
            messages=serialize_history(request.messages),
            system=system or None,
            tools=[],  # never sent natively in text mode
            cache_breakpoints=request.cache_breakpoints,
            max_tokens=request.max_tokens,
            thinking=request.thinking,
            effort=request.effort,
            extra=request.extra,
        )

    async def stream(self, request: ProviderRequest) -> AsyncIterator[StreamChunk]:
        parser = TextToolParser()
        tool_index = 1000
        think_index = -1000
        open_think: int | None = None
        made_tool_call = False
        text_index = 0  # index of the inner text block, for flushed tails
        finalized = False

        def close_think(out: list[StreamChunk]) -> None:
            nonlocal open_think
            if open_think is not None:
                out.append(StreamChunk("block_stop", {"index": open_think}))
                open_think = None

        def map_events(events: list[ParserEvent]) -> list[StreamChunk]:
            nonlocal tool_index, think_index, open_think, made_tool_call
            out: list[StreamChunk] = []
            for event in events:
                if event[0] == "text":
                    close_think(out)
                    out.append(
                        StreamChunk("text_delta", {"index": text_index, "text": event[1]})
                    )
                elif event[0] == "thinking":
                    if open_think is None:
                        open_think = think_index
                        think_index += 1
                        out.append(StreamChunk("block_start", {
                            "index": open_think, "block_type": "thinking",
                            "tool_name": None, "tool_use_id": None, "block": {},
                        }))
                    out.append(
                        StreamChunk("thinking_delta", {"index": open_think, "text": event[1]})
                    )
                else:  # ("tool_call", name, args)
                    close_think(out)
                    made_tool_call = True
                    _, tool_name, args = event
                    index = tool_index
                    tool_index += 1
                    tool_use_id = "call_" + uuid.uuid4().hex[:12]
                    out.append(StreamChunk("block_start", {
                        "index": index, "block_type": "tool_use",
                        "tool_name": tool_name, "tool_use_id": tool_use_id,
                        "block": {},
                    }))
                    out.append(StreamChunk("tool_args_delta", {
                        "index": index,
                        "partial_json": json.dumps(args, ensure_ascii=False),
                    }))
                    out.append(StreamChunk("block_stop", {"index": index}))
            return out

        async for chunk in self._inner.stream(self._prepare(request)):
            kind = chunk.kind
            if kind == "text_delta":
                text_index = chunk.payload.get("index", 0)
                for out in map_events(parser.feed(chunk.payload["text"])):
                    yield out
                continue
            if kind in ("usage", "message_stop", "error") and not finalized:
                # Terminal (or terminal-adjacent) chunk: flush held text and
                # any unterminated marker before letting it through.
                finalized = True
                for out in map_events(parser.finalize()):
                    yield out
                tail: list[StreamChunk] = []
                close_think(tail)
                for out in tail:
                    yield out
            if kind == "usage" and made_tool_call:
                # The model "stopped" as far as the server knows — rewrite so
                # the engine loop dispatches the parsed calls.
                payload = dict(chunk.payload)
                payload["stop_reason"] = "tool_use"
                yield StreamChunk("usage", payload)
                continue
            yield chunk
