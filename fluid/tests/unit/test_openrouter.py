"""OpenRouter provider: SSE normalization, request body, e2e chunk walk."""

import json
from pathlib import Path
from typing import AsyncIterator

import httpx

from omni.messages import Image, Message, Reasoning, Text, ToolResultBlock, ToolUseBlock
from omni.providers._sse import sse_events
from omni.providers.base import ProviderRequest, StreamChunk
from omni.providers.openrouter.client import OpenRouterClient
from omni.providers.openrouter.serialize import (
    ChatCompletionsNormalizer,
    build_request_body,
)

GOLDEN = Path(__file__).parent.parent / "golden" / "openrouter_text_tool.sse"


async def _lines(text: str) -> AsyncIterator[str]:
    for line in text.splitlines(keepends=True):
        yield line


async def _golden_chunks() -> list[StreamChunk]:
    normalizer = ChatCompletionsNormalizer()
    chunks: list[StreamChunk] = []
    async for name, data in sse_events(_lines(GOLDEN.read_text())):
        chunks.extend(normalizer.consume(name, data))
    return chunks


# ─── SSE → chunk normalization ───────────────────────────────────────────────


async def test_sse_normalization_kind_sequence() -> None:
    chunks = await _golden_chunks()
    assert [c.kind for c in chunks] == [
        "message_start",
        "block_start", "text_delta", "text_delta",
        "block_start", "tool_args_delta", "tool_args_delta",
        "block_stop", "block_stop",
        "usage", "message_stop",
    ]


async def test_text_and_tool_args_reassemble() -> None:
    chunks = await _golden_chunks()
    text = "".join(c.payload["text"] for c in chunks if c.kind == "text_delta")
    assert text == "Hello there"
    args = "".join(
        c.payload["partial_json"] for c in chunks if c.kind == "tool_args_delta"
    )
    assert json.loads(args) == {"city": "Paris"}


async def test_tool_block_metadata_and_stop_reason() -> None:
    chunks = await _golden_chunks()
    tool_start = [c for c in chunks if c.kind == "block_start"][1]
    assert tool_start.payload["block_type"] == "tool_use"
    assert tool_start.payload["tool_name"] == "get_weather"
    assert tool_start.payload["tool_use_id"] == "call_1"
    usage = next(c for c in chunks if c.kind == "usage")
    assert usage.payload["stop_reason"] == "tool_use"


async def test_usage_cached_tokens() -> None:
    chunks = await _golden_chunks()
    u = next(c for c in chunks if c.kind == "usage").payload["usage"]
    assert u["input_tokens"] == 148  # 180 prompt - 32 cached
    assert u["cache_read_input_tokens"] == 32
    assert u["output_tokens"] == 24


def test_reasoning_delta_maps_to_thinking() -> None:
    norm = ChatCompletionsNormalizer()
    out = norm.consume(
        "message",
        {"choices": [{"index": 0, "delta": {"reasoning": "hmm"}, "finish_reason": None}]},
    )
    kinds = [c.kind for c in out]
    assert kinds == ["message_start", "block_start", "thinking_delta"]
    assert out[1].payload["block_type"] == "thinking"
    assert out[2].payload["text"] == "hmm"


# ─── request body ────────────────────────────────────────────────────────────


def test_request_body_message_mapping() -> None:
    request = ProviderRequest(
        model="openai/gpt-4o",
        system="be terse",
        messages=[
            Message(role="user", content=[Text("weather?")]),
            Message(
                role="assistant",
                content=[
                    Reasoning(origin="anthropic", raw={"x": 1}),  # dropped
                    Text("checking"),
                    ToolUseBlock(id="call_1", name="get_weather", input={"city": "Paris"}),
                ],
            ),
            Message(
                role="user",
                content=[ToolResultBlock(tool_use_id="call_1", content=[Text("sunny")])],
            ),
        ],
        tools=[
            {
                "name": "get_weather",
                "description": "Get weather",
                "input_schema": {"type": "object", "properties": {}},
            }
        ],
    )
    body = build_request_body(request)
    assert body["stream"] is True
    assert body["stream_options"] == {"include_usage": True}
    assert body["messages"] == [
        {"role": "system", "content": "be terse"},
        {"role": "user", "content": "weather?"},
        {
            "role": "assistant",
            "content": "checking",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "get_weather", "arguments": '{"city":"Paris"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "sunny"},
    ]
    assert body["tools"][0] == {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get weather",
            "parameters": {"type": "object", "properties": {}},
        },
    }


def test_request_body_image_content_parts() -> None:
    request = ProviderRequest(
        model="openai/gpt-4o",
        messages=[
            Message(
                role="user",
                content=[Text("what is this?"), Image(media_type="image/png", data="AAAA")],
            )
        ],
    )
    body = build_request_body(request)
    content = body["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "what is this?"}
    assert content[1] == {
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64,AAAA"},
    }


# ─── fake-transport end-to-end ───────────────────────────────────────────────


async def test_client_end_to_end() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=GOLDEN.read_text().encode(),
            headers={"content-type": "text/event-stream"},
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = OpenRouterClient(api_key="or-test", http_client=http)
    request = ProviderRequest(
        model="openai/gpt-4o", messages=[Message(role="user", content=[Text("hi")])]
    )
    chunks = [c async for c in client.stream(request)]
    await http.aclose()
    assert chunks[0].kind == "message_start"
    assert chunks[-1].kind == "message_stop"
    assert any(c.kind == "tool_args_delta" for c in chunks)
