"""Golden-fixture test: Anthropic SSE → StreamChunks → NativeLoop events.

Drives the full M0 pipeline off a recorded SSE stream with a fake provider,
asserting the normalized chunk grammar, the emitted event sequence, the
assembled assistant message (incl. verbatim reasoning envelope and
byte-accumulated tool args), and cache accounting.
"""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from fluid.engine.native import NativeLoop
from fluid.events import (
    TextDelta,
    ThinkingDelta,
    ToolUse,
    TurnComplete,
    Usage,
    UserTurn,
)
from fluid.messages import Reasoning, ToolUseBlock
from fluid.providers._sse import sse_events
from fluid.providers.anthropic.serialize import sse_to_chunk
from fluid.providers.base import ProviderCaps, ProviderRequest, StreamChunk
from fluid.store.base import MemoryStore

GOLDEN = Path(__file__).parent.parent / "golden" / "anthropic_text_thinking_tool.sse"


async def _lines(text: str) -> AsyncIterator[str]:
    for line in text.splitlines(keepends=True):
        yield line


async def golden_chunks() -> list[StreamChunk]:
    chunks = []
    async for name, data in sse_events(_lines(GOLDEN.read_text())):
        chunk = sse_to_chunk(name, data)
        if chunk:
            chunks.append(chunk)
    return chunks


class FakeProvider:
    name = "anthropic"
    caps = ProviderCaps(explicit_cache_breakpoints=True, max_cache_breakpoints=4)

    def __init__(self) -> None:
        self.requests: list[ProviderRequest] = []

    async def stream(self, request: ProviderRequest) -> AsyncIterator[StreamChunk]:
        self.requests.append(request)
        for chunk in await golden_chunks():
            yield chunk


async def test_sse_normalization() -> None:
    kinds = [c.kind for c in await golden_chunks()]
    assert kinds == [
        "message_start",
        "block_start", "thinking_delta", "signature_delta", "block_stop",
        "block_start", "text_delta", "text_delta", "block_stop",
        "block_start", "tool_args_delta", "tool_args_delta", "block_stop",
        "usage", "message_stop",
    ]


async def test_native_loop_golden() -> None:
    provider = FakeProvider()
    store = MemoryStore()
    loop = NativeLoop(provider=provider, store=store, model="claude-sonnet-5", system="sys")

    await loop.submit(UserTurn(content="hi"))
    await loop.wait_turn()

    events = loop.bus.log(loop.session_id)
    types = [type(e).__name__ for e in events]
    assert types[0] == "TurnStarted"
    assert types[-1] == "TurnComplete"
    assert [e.seq for e in events] == list(range(1, len(events) + 1))

    text = "".join(e.text for e in events if isinstance(e, TextDelta))
    assert text == "Hello world"
    thinking = "".join(e.text for e in events if isinstance(e, ThinkingDelta))
    assert thinking == "Let me think."

    tool_uses = [e for e in events if isinstance(e, ToolUse)]
    assert len(tool_uses) == 1
    assert tool_uses[0].name == "get_weather"
    assert tool_uses[0].input == {"city": "Paris"}  # byte-accumulated across deltas

    usage = next(e for e in events if isinstance(e, Usage))
    assert usage.cache_read_tokens == 100
    assert usage.output_tokens == 42
    assert usage.cache_hit_rate == pytest.approx(100 / 240)

    done = next(e for e in events if isinstance(e, TurnComplete))
    assert done.stop_reason == "tool_use"

    # Assembled assistant message: thinking + reasoning envelope + text + tool_use
    path = await store.load_path(loop.session_id)
    assert [n.message.role for n in path] == ["user", "assistant"]
    assistant = path[-1].message
    kinds = [type(b).__name__ for b in assistant.content]
    assert kinds == ["Thinking", "Reasoning", "Text", "ToolUseBlock"]
    reasoning = assistant.content[1]
    assert isinstance(reasoning, Reasoning)
    assert reasoning.origin == "anthropic"
    assert reasoning.signature == "c2lnbmVk"
    assert reasoning.raw == {"type": "thinking", "thinking": "Let me think.",
                             "signature": "c2lnbmVk"}
    tool_block = assistant.content[3]
    assert isinstance(tool_block, ToolUseBlock)
    assert tool_block.input == {"city": "Paris"}

    # Cache plan flowed into the request
    assert provider.requests[0].cache_breakpoints == 2


# ─── blocks the engine has no model for survive assembly ─────────────────────


async def test_redacted_and_server_tool_blocks_survive_assembly() -> None:
    import asyncio

    from fluid.messages import Reasoning as _Reasoning
    from fluid.providers.anthropic.serialize import build_request_body

    redacted = {"type": "redacted_thinking", "data": "OPAQUE"}
    search_result = {
        "type": "web_search_tool_result", "tool_use_id": "srvtoolu_1",
        "content": [{"type": "web_search_result", "url": "https://example.test"}],
    }
    chunks = [
        StreamChunk("message_start", {"usage": {"input_tokens": 10}}),
        StreamChunk("block_start", {
            "index": 0, "block_type": "redacted_thinking", "block": redacted,
        }),
        StreamChunk("block_stop", {"index": 0}),
        StreamChunk("block_start", {
            "index": 1, "block_type": "server_tool_use",
            "tool_name": "web_search", "tool_use_id": "srvtoolu_1",
            "block": {"type": "server_tool_use", "id": "srvtoolu_1",
                      "name": "web_search", "input": {}},
        }),
        StreamChunk("tool_args_delta", {"index": 1, "partial_json": '{"query": "fluid"}'}),
        StreamChunk("block_stop", {"index": 1}),
        StreamChunk("block_start", {
            "index": 2, "block_type": "web_search_tool_result", "block": search_result,
        }),
        StreamChunk("block_stop", {"index": 2}),
        StreamChunk("block_start", {"index": 3, "block_type": "text"}),
        StreamChunk("text_delta", {"index": 3, "text": "found it"}),
        StreamChunk("block_stop", {"index": 3}),
        StreamChunk("usage", {"usage": {"output_tokens": 5}, "stop_reason": "end_turn"}),
        StreamChunk("message_stop", {}),
    ]

    class Provider:
        name = "anthropic"
        caps = ProviderCaps(server_tools=True)

        async def stream(self, request: ProviderRequest) -> AsyncIterator[StreamChunk]:
            for chunk in chunks:
                yield chunk

    store = MemoryStore()
    loop = NativeLoop(provider=Provider(), store=store, model="claude-opus-5")
    await loop.submit(UserTurn(content="search"))
    await asyncio.wait_for(loop.wait_turn(), timeout=5)

    nodes = await store.load_path(loop.session_id)
    assistant = nodes[-1].message
    assert assistant.role == "assistant"
    raws = [b.raw for b in assistant.content if isinstance(b, _Reasoning)]
    assert raws == [
        redacted,
        {"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search",
         "input": {"query": "fluid"}},
        search_result,
    ]
    # No ToolUse was dispatched for the server-side call.
    assert not any(isinstance(e, ToolUse) for e in loop.bus.log(loop.session_id))
    # And the next request carries them back in place.
    body = build_request_body(ProviderRequest(
        model="claude-opus-5", messages=[n.message for n in nodes],
    ))
    assert body["messages"][-1]["content"][:3] == raws
    assert body["messages"][-1]["content"][3] == {"type": "text", "text": "found it"}
