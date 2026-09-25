"""Anthropic client: an error sent in place of content retries, one after content surfaces.

Offline: the Messages endpoint is an ``httpx.MockTransport``.
"""

import httpx

from fluid.messages import Message, Text
from fluid.providers._retry import RetryPolicy
from fluid.providers.anthropic.client import AnthropicClient
from fluid.providers.base import ProviderRequest, StreamChunk

MESSAGE_START = (
    "event: message_start\n"
    'data: {"type":"message_start","message":{"id":"msg_01","usage":{"input_tokens":5}}}\n\n'
)
TEXT_BLOCK = (
    "event: content_block_start\n"
    'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n'
    "event: content_block_delta\n"
    'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"hi"}}\n\n'
    "event: content_block_stop\n"
    'data: {"type":"content_block_stop","index":0}\n\n'
)
TAIL = (
    "event: message_delta\n"
    'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":1}}\n\n'
    "event: message_stop\n"
    'data: {"type":"message_stop"}\n\n'
)


def _error_event(error_type: str) -> str:
    return (
        "event: error\n"
        f'data: {{"type":"error","error":{{"type":"{error_type}","message":"{error_type}"}}}}\n\n'
    )


def _sse(body: str) -> httpx.Response:
    return httpx.Response(
        200, content=body.encode(), headers={"content-type": "text/event-stream"}
    )


async def _run(handler, *, retries: int = 1) -> list[StreamChunk]:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = AnthropicClient(
        api_key="sk-test", http_client=http,
        retry=RetryPolicy(max_retries=retries, base_delay=0, jitter=0),
    )
    request = ProviderRequest(
        model="claude-opus-5", messages=[Message(role="user", content=[Text("hi")])]
    )
    chunks = [c async for c in client.stream(request)]
    await http.aclose()
    return chunks


async def test_overloaded_before_content_is_retried() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return _sse(MESSAGE_START + _error_event("overloaded_error"))
        return _sse(MESSAGE_START + TEXT_BLOCK + TAIL)

    chunks = await _run(handler)
    assert calls == 2
    kinds = [c.kind for c in chunks]
    assert kinds.count("message_start") == 1
    assert "error" not in kinds
    assert kinds[-1] == "message_stop"


async def test_timeout_error_before_content_is_retried() -> None:
    """R10-25: Anthropic documents ``timeout_error`` (504) as transient."""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return _sse(MESSAGE_START + _error_event("timeout_error"))
        return _sse(MESSAGE_START + TEXT_BLOCK + TAIL)

    chunks = await _run(handler)
    assert calls == 2
    assert "error" not in [c.kind for c in chunks]


async def test_gateway_504_is_retried() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(504, text="upstream request timeout")
        return _sse(MESSAGE_START + TEXT_BLOCK + TAIL)

    chunks = await _run(handler)
    assert calls == 2
    assert chunks[-1].kind == "message_stop"


async def test_overloaded_after_content_surfaces_as_a_retriable_error() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _sse(MESSAGE_START + TEXT_BLOCK + _error_event("overloaded_error"))

    chunks = await _run(handler)
    assert calls == 1
    assert any(c.kind == "text_delta" for c in chunks)
    assert chunks[-1].kind == "error"
    assert chunks[-1].payload["retriable"] is True


async def test_invalid_request_error_is_not_retried() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _sse(MESSAGE_START + _error_event("invalid_request_error"))

    chunks = await _run(handler)
    assert calls == 1
    assert [c.kind for c in chunks] == ["message_start", "error"]
    assert chunks[-1].payload["retriable"] is False


async def test_exhausted_retries_surface_the_overload() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _sse(MESSAGE_START + _error_event("overloaded_error"))

    chunks = await _run(handler, retries=2)
    assert calls == 3
    assert [c.kind for c in chunks] == ["message_start", "error"]
    assert chunks[-1].payload["type"] == "overloaded_error"
    assert chunks[-1].payload["retriable"] is True
