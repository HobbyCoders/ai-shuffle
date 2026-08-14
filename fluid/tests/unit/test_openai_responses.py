"""OpenAI Responses provider: SSE normalization, request body, e2e chunk walk."""

from pathlib import Path
from typing import Any, AsyncIterator

import httpx

from omni.messages import Message, Reasoning, Text, ToolResultBlock, ToolUseBlock
from omni.providers._sse import sse_events
from omni.providers.base import ProviderRequest, StreamChunk
from omni.providers.openai_responses.client import OpenAIResponsesClient
from omni.providers.openai_responses.serialize import (
    ResponsesStreamNormalizer,
    build_request_body,
)

GOLDEN = Path(__file__).parent.parent / "golden" / "openai_responses_text_reasoning_tool.sse"


async def _lines(text: str) -> AsyncIterator[str]:
    for line in text.splitlines(keepends=True):
        yield line


async def _golden_chunks() -> list[StreamChunk]:
    normalizer = ResponsesStreamNormalizer()
    chunks: list[StreamChunk] = []
    async for name, data in sse_events(_lines(GOLDEN.read_text())):
        chunks.extend(normalizer.consume(name, data))
    return chunks


# ─── SSE → chunk normalization ───────────────────────────────────────────────


async def test_sse_normalization_kind_sequence() -> None:
    chunks = await _golden_chunks()
    assert [c.kind for c in chunks] == [
        "message_start",
        "block_start", "thinking_delta", "block_stop",
        "block_start", "text_delta", "text_delta", "block_stop",
        "block_start", "tool_args_delta", "tool_args_delta", "block_stop",
        "usage", "message_stop",
    ]


async def test_block_start_indices_and_tool_metadata() -> None:
    chunks = await _golden_chunks()
    starts = [c for c in chunks if c.kind == "block_start"]
    assert [s.payload["index"] for s in starts] == [0, 1, 2]
    assert [s.payload["block_type"] for s in starts] == ["thinking", "text", "tool_use"]
    tool_start = starts[2]
    assert tool_start.payload["tool_name"] == "get_weather"
    assert tool_start.payload["tool_use_id"] == "call_xyz"


async def test_tool_args_accumulate_byte_exact() -> None:
    import json

    chunks = await _golden_chunks()
    args = "".join(
        c.payload["partial_json"] for c in chunks if c.kind == "tool_args_delta"
    )
    assert json.loads(args) == {"city": "Paris"}


async def test_block_stop_carries_full_done_item() -> None:
    # response.completed.output is empty; the authoritative items ride on
    # block_stop under payload["block"].
    chunks = await _golden_chunks()
    stops = [c for c in chunks if c.kind == "block_stop"]
    reasoning_block = stops[0].payload["block"]
    assert reasoning_block["type"] == "reasoning"
    assert reasoning_block["encrypted_content"] == "ENCRYPTED_BLOB_RS1"
    fc_block = stops[2].payload["block"]
    assert fc_block["type"] == "function_call"
    assert fc_block["arguments"] == '{"city": "Paris"}'
    assert fc_block["call_id"] == "call_xyz"


async def test_usage_maps_cached_tokens_anthropic_style() -> None:
    chunks = await _golden_chunks()
    usage = next(c for c in chunks if c.kind == "usage")
    u = usage.payload["usage"]
    # input_tokens 200 includes 64 cached → 136 exclusive + 64 cache_read.
    assert u["input_tokens"] == 136
    assert u["cache_read_input_tokens"] == 64
    assert u["output_tokens"] == 30
    assert usage.payload["stop_reason"] == "tool_use"


# ─── request body ────────────────────────────────────────────────────────────


def _request_with_history() -> ProviderRequest:
    reasoning_item = {
        "id": "rs_1",
        "type": "reasoning",
        "status": "completed",
        "encrypted_content": "BLOB",
        "summary": [{"type": "summary_text", "text": "thought"}],
        "content": [],
    }
    return ProviderRequest(
        model="gpt-5.6-sol",
        system="be terse",
        messages=[
            Message(role="user", content=[Text("hi")]),
            Message(
                role="assistant",
                content=[
                    Reasoning(origin="openai_responses", raw=reasoning_item),
                    Reasoning(origin="anthropic", raw={"type": "thinking", "thinking": "x"}),
                    Text("prev answer"),
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
                "input_schema": {
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                    "required": ["city"],
                },
            }
        ],
    )


def test_request_body_envelope_and_tools() -> None:
    body = build_request_body(_request_with_history())
    assert body["store"] is False
    assert body["stream"] is True
    assert body["include"] == ["reasoning.encrypted_content"]
    assert body["instructions"] == "be terse"
    assert body["tools"] == [
        {
            "type": "function",
            "name": "get_weather",
            "description": "Get weather",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        }
    ]


def test_request_body_reasoning_verbatim_and_foreign_dropped() -> None:
    body = build_request_body(_request_with_history())
    items = body["input"]
    types = [it["type"] for it in items]
    # user message, own reasoning (verbatim), assistant message, function_call,
    # function_call_output. The foreign (anthropic) reasoning is dropped entirely.
    assert types == [
        "message",
        "reasoning",
        "message",
        "function_call",
        "function_call_output",
    ]
    reasoning = items[1]
    # id + status stripped on stateless resend; everything else verbatim.
    assert "id" not in reasoning and "status" not in reasoning
    assert reasoning["encrypted_content"] == "BLOB"
    assert reasoning["summary"] == [{"type": "summary_text", "text": "thought"}]

    assert items[0] == {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]}
    assert items[2] == {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": "prev answer"}],
    }
    assert items[3] == {
        "type": "function_call",
        "call_id": "call_1",
        "name": "get_weather",
        "arguments": '{"city":"Paris"}',
    }
    assert items[4] == {
        "type": "function_call_output",
        "call_id": "call_1",
        "output": "sunny",
    }


def test_extra_enables_stateful_passthrough() -> None:
    request = ProviderRequest(
        model="gpt-5.6-sol",
        messages=[Message(role="user", content=[Text("hi")])],
        extra={"previous_response_id": "resp_prev", "store": True},
    )
    body = build_request_body(request)
    assert body["previous_response_id"] == "resp_prev"
    assert body["store"] is True  # extra overrides the default


# ─── fake-transport end-to-end ───────────────────────────────────────────────


async def test_client_end_to_end_chunk_walk() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=GOLDEN.read_text().encode(),
            headers={"content-type": "text/event-stream"},
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = OpenAIResponsesClient(api_key="sk-test", http_client=http)
    request = ProviderRequest(
        model="gpt-5.6-sol", messages=[Message(role="user", content=[Text("weather?")])]
    )
    chunks = [c async for c in client.stream(request)]
    await http.aclose()

    assert chunks[0].kind == "message_start"
    assert chunks[-1].kind == "message_stop"
    # The done reasoning item survived the wire → block_stop carries it.
    stops = [c for c in chunks if c.kind == "block_stop"]
    assert stops[0].payload["block"]["encrypted_content"] == "ENCRYPTED_BLOB_RS1"


async def test_client_refreshes_and_retries_once_on_401() -> None:
    class FakeAuth:
        account_id = "acct_1"

        def __init__(self) -> None:
            self.tokens = ["stale", "fresh"]
            self.on_401_calls = 0

        async def get_access_token(self) -> str:
            return self.tokens[0]

        async def on_401(self) -> None:
            self.on_401_calls += 1
            self.tokens.pop(0)

    auth = FakeAuth()
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("authorization", ""))
        if request.headers.get("authorization") == "Bearer stale":
            return httpx.Response(401, json={"error": {"message": "expired"}})
        return httpx.Response(
            200,
            content=GOLDEN.read_text().encode(),
            headers={"content-type": "text/event-stream"},
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = OpenAIResponsesClient(auth=auth, http_client=http)
    request = ProviderRequest(
        model="gpt-5.6-sol", messages=[Message(role="user", content=[Text("hi")])]
    )
    chunks = [c async for c in client.stream(request)]
    await http.aclose()

    assert auth.on_401_calls == 1
    assert seen == ["Bearer stale", "Bearer fresh"]
    assert chunks[-1].kind == "message_stop"
    assert not any(c.kind == "error" for c in chunks)


async def test_client_account_id_header_present() -> None:
    class FakeAuth:
        account_id = "acct_42"

        async def get_access_token(self) -> str:
            return "tok"

        async def on_401(self) -> None:  # pragma: no cover - not hit here
            pass

    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["account"] = request.headers.get("chatgpt-account-id")
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(
            200,
            content=GOLDEN.read_text().encode(),
            headers={"content-type": "text/event-stream"},
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = OpenAIResponsesClient(
        auth=FakeAuth(),
        base_url="https://chatgpt.com/backend-api/codex/responses",
        http_client=http,
    )
    request = ProviderRequest(
        model="gpt-5.6-sol", messages=[Message(role="user", content=[Text("hi")])]
    )
    async for _ in client.stream(request):
        pass
    await http.aclose()
    assert captured["account"] == "acct_42"
    assert captured["auth"] == "Bearer tok"


def test_effort_overrides_thinking_and_clamps() -> None:
    from omni.messages import Message as _Msg, Text as _Text

    request = ProviderRequest(
        model="gpt-5.6",
        messages=[_Msg(role="user", content=[_Text("hi")])],
        thinking={"budget_tokens": 1024},
        effort="xhigh",
    )
    body = build_request_body(request)
    assert body["reasoning"] == {"effort": "high", "summary": "auto"}


def test_thinking_effort_still_honored_without_explicit_effort() -> None:
    from omni.messages import Message as _Msg, Text as _Text

    request = ProviderRequest(
        model="gpt-5.6",
        messages=[_Msg(role="user", content=[_Text("hi")])],
        thinking={"effort": "low"},
    )
    body = build_request_body(request)
    assert body["reasoning"] == {"effort": "low", "summary": "auto"}
