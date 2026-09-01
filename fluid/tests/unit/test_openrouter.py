"""OpenRouter provider: SSE normalization, request body, e2e chunk walk."""

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from fluid.messages import (
    Image,
    Message,
    Reasoning,
    Text,
    ToolResultBlock,
    ToolUseBlock,
)
from fluid.providers import _chat_completions
from fluid.providers._retry import RetryPolicy
from fluid.providers._sse import sse_events
from fluid.providers.base import ProviderRequest, StreamChunk
from fluid.providers.openrouter.client import OpenRouterClient
from fluid.providers.openrouter.serialize import (
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


def test_usage_cache_write_tokens_are_accounted_separately() -> None:
    u = _terminal_usage_payload({
        "prompt_tokens": 180,
        "completion_tokens": 20,
        "total_tokens": 200,
        "prompt_tokens_details": {
            "cached_tokens": 30,
            "cache_write_tokens": 40,
        },
    })
    assert u["input_tokens"] == 110
    assert u["cache_read_input_tokens"] == 30
    assert u["cache_creation_input_tokens"] == 40


def _terminal_usage_payload(usage: dict) -> dict:
    norm = ChatCompletionsNormalizer()
    norm.consume(
        "message",
        {"choices": [{"index": 0, "delta": {"content": "hi"}, "finish_reason": "stop"}]},
    )
    out = norm.consume("message", {"choices": [], "usage": usage})
    return next(c for c in out if c.kind == "usage").payload["usage"]


def test_usage_carries_provider_reported_cost() -> None:
    u = _terminal_usage_payload(
        {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120, "cost": 0.00123}
    )
    assert u["cost_usd"] == pytest.approx(0.00123)
    assert u["input_tokens"] == 100


def test_usage_without_cost_has_no_cost_key() -> None:
    u = _terminal_usage_payload(
        {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}
    )
    assert "cost_usd" not in u


def test_native_finish_reason_rides_the_terminal_chunks() -> None:
    """OpenRouter hides upstream failures behind finish_reason "stop".

    The real verdict lives in native_finish_reason, and the engine needs it to
    explain an empty completion, so it travels on both terminal chunks.
    """
    norm = ChatCompletionsNormalizer()
    norm.consume(
        "message",
        {"choices": [{"index": 0, "delta": {"content": ""},
                      "finish_reason": "stop",
                      "native_finish_reason": "network_error"}]},
    )
    assert norm.native_stop_reason == "network_error"
    out = norm.consume(
        "message",
        {"choices": [], "usage": {"prompt_tokens": 0, "completion_tokens": 0}},
    )
    usage = next(c for c in out if c.kind == "usage")
    assert usage.payload["stop_reason"] == "end_turn"
    assert usage.payload["native_stop_reason"] == "network_error"
    stop = next(c for c in out if c.kind == "message_stop")
    assert stop.payload["native_stop_reason"] == "network_error"


def test_no_native_finish_reason_leaves_the_terminal_chunks_bare() -> None:
    norm = ChatCompletionsNormalizer()
    norm.consume(
        "message",
        {"choices": [{"index": 0, "delta": {"content": "hi"}, "finish_reason": "stop"}]},
    )
    out = norm.consume(
        "message",
        {"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 1}},
    )
    assert norm.native_stop_reason is None
    assert "native_stop_reason" not in next(
        c for c in out if c.kind == "usage"
    ).payload
    assert next(c for c in out if c.kind == "message_stop").payload == {}


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
    assert body["usage"] == {"include": True}
    assert body["provider"] == {"allow_fallbacks": False}
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


# ─── cache breakpoints ───────────────────────────────────────────────────────


def _cacheable_request(model: str, breakpoints: int) -> ProviderRequest:
    return ProviderRequest(
        model=model,
        system="be terse",
        messages=[
            Message(role="user", content=[Text("weather?")]),
            Message(
                role="assistant",
                content=[
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
        cache_breakpoints=breakpoints,
    )


def test_cache_breakpoints_placed_for_anthropic_model() -> None:
    body = build_request_body(_cacheable_request("anthropic/claude-haiku-4.5", 3))
    system = body["messages"][0]
    assert system["role"] == "system"
    assert system["content"] == [
        {"type": "text", "text": "be terse", "cache_control": {"type": "ephemeral"}}
    ]
    last = body["messages"][-1]
    assert last["role"] == "tool"
    assert last["content"] == [
        {"type": "text", "text": "sunny", "cache_control": {"type": "ephemeral"}}
    ]
    # Exactly two markers: system tail + newest history block, nowhere else.
    marked = [
        part
        for msg in body["messages"]
        if isinstance(msg.get("content"), list)
        for part in msg["content"]
        if "cache_control" in part
    ]
    assert len(marked) == 2
    assert "cache_control" not in json.dumps(body["tools"])


def test_cache_breakpoints_ignored_for_non_anthropic_model() -> None:
    body = build_request_body(_cacheable_request("openai/gpt-4o", 3))
    assert body["messages"][0] == {"role": "system", "content": "be terse"}
    assert "cache_control" not in json.dumps(body)


def test_zero_breakpoints_is_a_noop() -> None:
    body = build_request_body(_cacheable_request("anthropic/claude-haiku-4.5", 0))
    assert "cache_control" not in json.dumps(body)


def test_single_breakpoint_goes_to_system_tail() -> None:
    body = build_request_body(_cacheable_request("anthropic/claude-haiku-4.5", 1))
    assert body["messages"][0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert body["messages"][-1]["content"] == "sunny"  # budget spent on system


def test_trailing_breakpoint_lands_on_text_part_not_image() -> None:
    request = ProviderRequest(
        model="anthropic/claude-haiku-4.5",
        messages=[
            Message(
                role="user",
                content=[Text("what is this?"), Image(media_type="image/png", data="AAAA")],
            )
        ],
        cache_breakpoints=2,
    )
    body = build_request_body(request)
    text_part, image_part = body["messages"][0]["content"]
    assert text_part["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in image_part


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
    assert chunks[0].kind == "provider_request"
    assert chunks[1].kind == "message_start"
    assert chunks[-1].kind == "message_stop"
    assert any(c.kind == "tool_args_delta" for c in chunks)
    usage = next(c.payload["usage"] for c in chunks if c.kind == "usage")
    assert len(usage["provider_requests"]["attempts"]) == 1


async def test_client_retry_is_bounded_visible_and_correlated() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(503, json={"error": {"message": "busy"}})
        return httpx.Response(
            200,
            content=GOLDEN.read_text().encode(),
            headers={
                "content-type": "text/event-stream",
                "x-generation-id": "gen_header_2",
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = OpenRouterClient(
        api_key="or-test", http_client=http,
        retry=RetryPolicy(max_retries=1, base_delay=0),
    )
    request = ProviderRequest(
        model="openai/gpt-4o",
        messages=[Message(role="user", content=[Text("hi")])],
    )
    chunks = [c async for c in client.stream(request)]
    await http.aclose()

    assert len(calls) == 2
    call_ids = [request.headers["x-fluid-call-id"] for request in calls]
    assert call_ids[0] == call_ids[1]
    usage = next(c.payload["usage"] for c in chunks if c.kind == "usage")
    attempts = usage["provider_requests"]["attempts"]
    assert attempts == [
        {"attempt": 1, "status": 503},
        {"attempt": 2, "status": 200, "generation_id": "gen_header_2"},
    ]


async def test_default_openrouter_transport_failure_does_not_retry() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("ambiguous timeout", request=request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = OpenRouterClient(api_key="or-test", http_client=http)
    request = ProviderRequest(
        model="openai/gpt-4o",
        messages=[Message(role="user", content=[Text("hi")])],
    )
    chunks = [c async for c in client.stream(request)]
    await http.aclose()

    assert calls == 1
    error = next(c for c in chunks if c.kind == "error")
    assert error.payload["provider_requests"]["attempts"] == [
        {"attempt": 1, "transport_error": "ReadTimeout"}
    ]


def test_request_body_keeps_session_and_disables_provider_fallback() -> None:
    request = ProviderRequest(
        model="x-ai/grok-4.6",
        messages=[Message(role="user", content=[Text("hi")])],
        extra={"session_id": "ses-sticky"},
    )
    body = build_request_body(request)
    assert body["session_id"] == "ses-sticky"
    assert body["provider"] == {"allow_fallbacks": False}


async def _stream_with(sse_text: str) -> list:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=sse_text.encode(), headers={"content-type": "text/event-stream"}
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    # No retries: these tests look at how one response is classified, not at
    # the backoff the default policy would spend on a retriable one.
    client = OpenRouterClient(
        api_key="or-test", http_client=http, retry=RetryPolicy(max_retries=0)
    )
    request = ProviderRequest(
        model="stealth/ox-alpha", messages=[Message(role="user", content=[Text("hi")])]
    )
    chunks = [c async for c in client.stream(request)]
    await http.aclose()
    return chunks


async def test_in_stream_error_chunk_surfaces_as_error() -> None:
    # OpenRouter answers upstream failures with HTTP 200 and an error chunk.
    sse = (
        'data: {"error":{"message":"Provider returned error","code":502}}\n\n'
        "data: [DONE]\n\n"
    )
    chunks = await _stream_with(sse)
    # The vendored stream contract leads with a provider_request evidence chunk.
    kinds = [c.kind for c in chunks if c.kind != "provider_request"]
    assert kinds == ["message_start", "error"]
    assert chunks[-1].payload["status"] == 502
    assert "Provider returned error" in chunks[-1].payload["message"]


async def test_in_stream_cloudflare_520_is_retriable() -> None:
    # Observed on Z.AI behind OpenRouter: HTTP 200, an error chunk carrying
    # Cloudflare's 520. Marking it fatal cost real calls and a human "Resume".
    sse = (
        'data: {"error":{"message":"Provider returned error","code":520,'
        '"metadata":{"provider_name":"Z.AI"}}}\n\n'
        "data: [DONE]\n\n"
    )
    chunks = await _stream_with(sse)
    assert chunks[-1].kind == "error"
    assert chunks[-1].payload["status"] == 520
    assert chunks[-1].payload["retriable"] is True


async def test_in_stream_numeric_string_code_is_classified() -> None:
    sse = (
        'data: {"error":{"message":"Web server is returning an unknown error",'
        '"code":"522"}}\n\n'
        "data: [DONE]\n\n"
    )
    chunks = await _stream_with(sse)
    assert chunks[-1].payload["retriable"] is True


async def test_in_stream_auth_error_stays_fatal() -> None:
    sse = (
        'data: {"error":{"message":"No auth credentials found","code":401}}\n\n'
        "data: [DONE]\n\n"
    )
    chunks = await _stream_with(sse)
    assert chunks[-1].kind == "error"
    assert chunks[-1].payload["status"] == 401
    assert chunks[-1].payload["retriable"] is False


async def test_in_stream_error_without_a_code_stays_fatal() -> None:
    # Nothing to classify on: assume fatal rather than loop on a bad request.
    sse = (
        'data: {"error":{"message":"model not found"}}\n\n'
        "data: [DONE]\n\n"
    )
    chunks = await _stream_with(sse)
    assert chunks[-1].payload["retriable"] is False


def test_cloudflare_edge_statuses_are_retriable() -> None:
    from fluid.providers._retry import RETRIABLE_STATUSES, is_retriable_status

    assert {520, 521, 522, 523, 524} <= RETRIABLE_STATUSES
    assert is_retriable_status(520) and is_retriable_status("429")
    assert not is_retriable_status(400)
    assert not is_retriable_status("rate_limit_exceeded")
    assert not is_retriable_status(None)
    # A bool is not a status, however happily Python would compare it.
    assert not is_retriable_status(True)


async def test_stream_without_usage_chunk_is_an_error() -> None:
    sse = (
        'data: {"choices":[{"index":0,"delta":{"content":"hi"},"finish_reason":null}]}\n\n'
        "data: [DONE]\n\n"
    )
    chunks = await _stream_with(sse)
    assert chunks[-1].kind == "error"
    assert "terminal usage chunk" in chunks[-1].payload["message"]


async def _stream_bytes(chunks, monkeypatch, timeout: float) -> list:
    """Drive the client against a hand-fed byte stream with a tiny watchdog."""
    monkeypatch.setattr(_chat_completions, "FIRST_DATA_TIMEOUT", timeout)

    async def body() -> AsyncIterator[bytes]:
        for piece in chunks:
            if isinstance(piece, float):
                await asyncio.sleep(piece)
            else:
                yield piece

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=body(), headers={"content-type": "text/event-stream"}
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = OpenRouterClient(api_key="or-test", http_client=http)
    request = ProviderRequest(
        model="stealth/ox-alpha", messages=[Message(role="user", content=[Text("hi")])]
    )
    out = [c async for c in client.stream(request)]
    await http.aclose()
    return out


async def test_hung_stream_gives_up_after_the_first_data_deadline(monkeypatch) -> None:
    # A dead replica: 200 headers, keepalive comments, no data event ever.
    keepalive = b": OPENROUTER PROCESSING\n\n"
    chunks = await _stream_bytes([keepalive, keepalive, 5.0], monkeypatch, 0.05)
    error = next(c for c in chunks if c.kind == "error")
    assert "terminal usage chunk" in error.payload["message"]
    assert "no data within 0.05s" in error.payload["message"]
    assert error.payload["retriable"] is False
    # Keepalive comments are not data: nothing was ever normalized.
    assert not any(c.kind == "message_start" for c in chunks)


async def test_first_event_in_time_disarms_the_watchdog(monkeypatch) -> None:
    # First data lands well inside the deadline; the rest of the stream may
    # then take as long as it likes.
    head = (
        'data: {"choices":[{"index":0,"delta":{"content":"hi"}}]}\n\n'
    ).encode()
    tail = (
        'data: {"choices":[],"usage":{"prompt_tokens":1,"completion_tokens":1}}\n\n'
        "data: [DONE]\n\n"
    ).encode()
    chunks = await _stream_bytes([head, 0.2, tail], monkeypatch, 0.05)
    kinds = [c.kind for c in chunks]
    assert "error" not in kinds
    assert kinds[-1] == "message_stop"
    assert any(c.kind == "text_delta" for c in chunks)


def test_effort_clamped_into_reasoning() -> None:
    request = ProviderRequest(
        model="meta-llama/llama-4",
        messages=[Message(role="user", content=[Text("hi")])],
        effort="max",
    )
    body = build_request_body(request)
    assert body["reasoning"] == {"effort": "high"}


def test_effort_passthrough_low() -> None:
    request = ProviderRequest(
        model="meta-llama/llama-4",
        messages=[Message(role="user", content=[Text("hi")])],
        effort="low",
    )
    body = build_request_body(request)
    assert body["reasoning"] == {"effort": "low"}


# ─── retries: a refused request retries, an ambiguous transport failure does not


def _request() -> ProviderRequest:
    return ProviderRequest(
        model="stealth/ox-alpha", messages=[Message(role="user", content=[Text("hi")])]
    )


def _fast_retries() -> RetryPolicy:
    return RetryPolicy(max_retries=3, max_transport_retries=0, base_delay=0, jitter=0)


def _sse_response(body: str, **headers: str) -> httpx.Response:
    return httpx.Response(
        200, content=body.encode(),
        headers={"content-type": "text/event-stream", **headers},
    )


def test_default_policy_retries_statuses_but_not_transport_failures() -> None:
    policy = OpenRouterClient(api_key="or-test")._retry
    assert policy.max_retries == 3
    assert policy.transport_retries == 0


async def test_rate_limit_status_is_retried() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                429, json={"error": {"message": "slow down"}}, headers={"retry-after": "0"}
            )
        return _sse_response(GOLDEN.read_text())

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = OpenRouterClient(api_key="or-test", http_client=http, retry=_fast_retries())
    chunks = [c async for c in client.stream(_request())]
    await http.aclose()

    assert calls == 2
    assert chunks[-1].kind == "message_stop"


async def test_in_stream_retriable_error_before_content_is_retried() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return _sse_response(
                'data: {"error":{"message":"Provider returned error","code":520}}\n\n'
                "data: [DONE]\n\n"
            )
        return _sse_response(GOLDEN.read_text())

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = OpenRouterClient(api_key="or-test", http_client=http, retry=_fast_retries())
    chunks = [c async for c in client.stream(_request())]
    await http.aclose()

    assert calls == 2
    kinds = [c.kind for c in chunks if c.kind != "provider_request"]
    # One message begins, on the attempt that produced content.
    assert kinds.count("message_start") == 1
    assert "error" not in kinds
    assert kinds[-1] == "message_stop"


async def test_in_stream_error_after_content_is_not_retried() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _sse_response(
            'data: {"choices":[{"index":0,"delta":{"content":"hi"},"finish_reason":null}]}\n\n'
            'data: {"error":{"message":"upstream died","code":520}}\n\n'
            "data: [DONE]\n\n"
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = OpenRouterClient(api_key="or-test", http_client=http, retry=_fast_retries())
    chunks = [c async for c in client.stream(_request())]
    await http.aclose()

    assert calls == 1
    kinds = [c.kind for c in chunks if c.kind != "provider_request"]
    assert kinds == ["message_start", "block_start", "text_delta", "error"]
    assert chunks[-1].payload["retriable"] is True


def test_retry_after_accepts_seconds_and_http_dates() -> None:
    from datetime import datetime, timedelta, timezone
    from email.utils import format_datetime

    from fluid.providers._retry import parse_retry_after

    assert parse_retry_after("7") == 7.0
    soon = datetime.now(timezone.utc) + timedelta(seconds=20)
    wait = parse_retry_after(format_datetime(soon, usegmt=True))
    assert wait is not None and 17.0 <= wait <= 20.0
    assert parse_retry_after("Thu, 01 Jan 1970 00:00:00 GMT") == 0.0
    assert parse_retry_after("later") is None
    assert parse_retry_after(None) is None


def test_delay_honors_retry_after_and_adds_bounded_jitter() -> None:
    policy = RetryPolicy(base_delay=1.0, max_delay=30.0, jitter=0.5)
    for _ in range(50):
        assert 4.0 <= policy.delay(0, "4") <= 6.0
    assert RetryPolicy(base_delay=0, jitter=0.5).delay(3) == 0.0
    assert RetryPolicy(base_delay=1.0, jitter=0).delay(2) == 4.0


# ─── tool calls without an index, content filters ────────────────────────────


def _chunk(delta: dict, finish_reason: str | None = None) -> dict:
    return {"choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}]}


def _collect_args(chunks: list[StreamChunk]) -> list[dict]:
    args: dict[int, str] = {}
    for c in chunks:
        if c.kind == "tool_args_delta":
            index = c.payload["index"]
            args[index] = args.get(index, "") + c.payload["partial_json"]
    return [json.loads(v) for _, v in sorted(args.items())]


def test_tool_calls_without_index_stay_distinct_by_id() -> None:
    n = ChatCompletionsNormalizer()
    out: list[StreamChunk] = []
    out += n.consume("", _chunk({"tool_calls": [
        {"id": "call_a", "type": "function", "function": {"name": "Read", "arguments": ""}},
    ]}))
    out += n.consume("", _chunk({"tool_calls": [
        {"function": {"arguments": '{"path":"a"}'}},
    ]}))
    out += n.consume("", _chunk({"tool_calls": [
        {"id": "call_b", "type": "function",
         "function": {"name": "Read", "arguments": '{"path":"b"}'}},
    ]}, "tool_calls"))
    out += n.consume("", {"choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 1}})

    starts = [c for c in out if c.kind == "block_start"]
    assert [s.payload["tool_use_id"] for s in starts] == ["call_a", "call_b"]
    assert _collect_args(out) == [{"path": "a"}, {"path": "b"}]


def test_tool_calls_without_index_or_id_split_on_a_new_name() -> None:
    n = ChatCompletionsNormalizer()
    out: list[StreamChunk] = []
    out += n.consume("", _chunk({"tool_calls": [
        {"function": {"name": "Read", "arguments": '{"path":'}},
    ]}))
    out += n.consume("", _chunk({"tool_calls": [{"function": {"arguments": '"a"}'}}]}))
    out += n.consume("", _chunk({"tool_calls": [
        {"function": {"name": "Glob", "arguments": '{"pattern":"*"}'}},
    ]}, "tool_calls"))
    out += n.consume("", {"choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 1}})

    starts = [c for c in out if c.kind == "block_start"]
    assert [s.payload["tool_name"] for s in starts] == ["Read", "Glob"]
    assert _collect_args(out) == [{"path": "a"}, {"pattern": "*"}]


def test_content_filter_before_any_output_is_an_error() -> None:
    n = ChatCompletionsNormalizer()
    out = n.consume("", _chunk({}, "content_filter"))
    out += n.consume("", {"choices": [], "usage": {"prompt_tokens": 9, "completion_tokens": 0}})
    assert [c.kind for c in out] == ["message_start", "error"]
    assert "content filter" in out[-1].payload["message"]
    assert out[-1].payload["retriable"] is False


def test_content_filter_after_output_keeps_the_text_and_names_the_cause() -> None:
    n = ChatCompletionsNormalizer()
    out = n.consume("", _chunk({"content": "partial"}))
    out += n.consume("", _chunk({}, "content_filter"))
    out += n.consume("", {"choices": [], "usage": {"prompt_tokens": 9, "completion_tokens": 2}})
    usage = next(c for c in out if c.kind == "usage")
    assert usage.payload["stop_reason"] == "end_turn"
    assert usage.payload["native_stop_reason"] == "content_filter"
    assert any(c.kind == "text_delta" for c in out)
