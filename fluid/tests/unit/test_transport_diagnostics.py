"""Transport failures retain metadata without changing replay policy."""
import asyncio
import json
import logging

import httpx
import pytest

from fluid.providers._chat_completions import stream_chat_completions
from fluid.providers._retry import RetryPolicy
from fluid.providers._transport import StreamDiagnostics
from fluid.providers.anthropic.client import AnthropicClient
from fluid.providers.base import ProviderRequest
from fluid.providers.openai_responses.client import OpenAIResponsesClient

URL = "https://user:password@provider.test/private-path?api_key=query-secret"
REQUEST = ProviderRequest(model="test-model", messages=[])


def event(name, data):
    return f"event: {name}\ndata: {json.dumps(data)}\n\n".encode()


def start(kind):
    if kind == "responses":
        return event("response.created", {"response": {"id": "resp_test", "status": "in_progress"}})
    if kind == "anthropic":
        return event("message_start", {"message": {"usage": {"input_tokens": 5}}}) + event(
            "content_block_start", {"index": 0, "content_block": {"type": "text", "text": ""}})
    return event("message", {"choices": [{"delta": {"content": "body-secret"}}]})


class BrokenStream(httpx.AsyncByteStream):
    def __init__(self, prefix=b"", error=None):
        self.prefix = prefix
        self.error = error or httpx.RemoteProtocolError("peer closed connection (incomplete chunked read)")
        self.closed = False

    async def __aiter__(self):
        if self.prefix:
            yield self.prefix
        raise self.error

    async def aclose(self):
        self.closed = True


def stream(kind, http, retry):
    if kind == "responses":
        return OpenAIResponsesClient(api_key="auth-secret", base_url=URL, http_client=http, retry=retry).stream(REQUEST)
    if kind == "anthropic":
        return AnthropicClient(api_key="auth-secret", base_url=URL, http_client=http, retry=retry).stream(REQUEST)
    return stream_chat_completions(URL, {"model": "test-model", "messages": []},
                                   {"Authorization": "Bearer auth-secret"}, http_client=http,
                                   timeout=5, retry=retry, track_provider_requests=True)


@pytest.mark.parametrize("kind", ["responses", "anthropic", "chat"])
@pytest.mark.parametrize("midstream", [False, True])
async def test_failure_metadata_and_retry_budget(kind, midstream, caplog):
    bodies = []
    def handle(request):
        body = BrokenStream(start(kind) if midstream else b"")
        bodies.append(body)
        return httpx.Response(200, stream=body, headers={
            "x-request-id": "req-123", "cf-ray": "ray-456", "set-cookie": "cookie-secret",
            "authorization": "auth-secret", "x-debug": "debug-secret",
            "request-id": "unsafe header value\nsecret",
        })
    caplog.set_level(logging.WARNING)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        chunks = [c async for c in stream(kind, http, RetryPolicy(max_retries=1, base_delay=0))]
        assert not http.is_closed  # injected client still belongs to the host
    failure = chunks[-1].payload["transport_failure"]
    # The Responses lane holds message_start until the first content item, so
    # a drop right after response.created has delivered nothing and replays.
    delivered = midstream and kind != "responses"
    assert chunks[-1].kind == "error"
    assert chunks[-1].payload["retriable"] is False
    assert len(bodies) == (1 if delivered else 2)
    assert all(b.closed for b in bodies)
    assert failure["host"] == "provider.test"
    assert failure["status"] == 200
    assert failure["http_version"] == "HTTP/1.1"
    assert failure["response_ids"] == {"x-request-id": "req-123", "cf-ray": "ray-456"}
    assert failure["chunks_delivered"] is delivered
    assert failure["bytes_received"] == (len(start(kind)) if midstream else 0)
    assert (failure["last_line_age_seconds"] is not None) is midstream
    assert failure["incomplete_chunked_read"]
    assert failure["exception_chain"] == ["RemoteProtocolError"]
    records = [r for r in caplog.records if r.name == "fluid.providers._transport"]
    assert len(records) == len(bodies)
    assert all(json.loads(r.getMessage().split(": ", 1)[1])["retrying"] for r in records[:-1])
    captured = caplog.text + json.dumps(failure)
    for secret in ("auth-secret", "cookie-secret", "debug-secret", "body-secret", "query-secret", "private-path", "password", "unsafe header"):
        assert secret not in captured


@pytest.mark.parametrize("kind", ["responses", "anthropic", "chat"])
async def test_connect_failure_with_no_headers_does_not_override_no_retry(kind, caplog):
    calls = 0
    def handle(request):
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("exception-secret")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        chunks = [c async for c in stream(kind, http, RetryPolicy(max_transport_retries=0))]
    failure = chunks[-1].payload["transport_failure"]
    assert calls == 1
    assert failure["status"] is None
    assert failure["headers_after_seconds"] is None
    assert failure["last_line_age_seconds"] is None
    assert failure["bytes_received"] == 0
    assert "exception-secret" not in caplog.text


@pytest.mark.parametrize("kind", ["responses", "anthropic", "chat"])
async def test_cancellation_is_not_reported_as_a_transport_failure(kind, caplog):
    body = BrokenStream(error=asyncio.CancelledError())
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=body))) as http:
        with pytest.raises(asyncio.CancelledError):
            _ = [c async for c in stream(kind, http, RetryPolicy(base_delay=0))]
    assert body.closed
    assert "Provider transport failure" not in caplog.text


async def test_real_incomplete_chunked_read():
    """httpcore produces the reported exception when the server omits the final chunk."""
    done = asyncio.Event()
    async def handle(reader, writer):
        try:
            await reader.readuntil(b"\r\n\r\n")
            payload = start("responses")
            writer.write(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n"
                         b"Content-Type: text/event-stream\r\nX-Request-Id: real-req\r\n\r\n")
            writer.write(f"{len(payload):x}\r\n".encode() + payload + b"\r\n")
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            done.set()
    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    try:
        port = server.sockets[0].getsockname()[1]
        async with httpx.AsyncClient(trust_env=False) as http:
            client = OpenAIResponsesClient(api_key="test", base_url=f"http://127.0.0.1:{port}/responses",
                                           http_client=http, retry=RetryPolicy(max_retries=0))
            chunks = await asyncio.wait_for(_collect(client.stream(REQUEST)), 5)
        failure = chunks[-1].payload["transport_failure"]
        assert failure["incomplete_chunked_read"]
        assert failure["response_ids"]["x-request-id"] == "real-req"
        # Only response.created arrived: the held message_start was never
        # delivered, so nothing reached the consumer before the failure.
        assert not failure["chunks_delivered"]
        assert failure["exception_chain"][:2] == ["RemoteProtocolError", "RemoteProtocolError"]
        await asyncio.wait_for(done.wait(), 5)
    finally:
        server.close()
        await server.wait_closed()


async def test_native_turn_preserves_real_truncated_stream_after_bounded_retries(monkeypatch):
    import fluid.engine.native as native
    from fluid.engine.native import NativeLoop
    monkeypatch.setattr(native, "_empty_retry_delay", lambda attempt: 0.0)
    from fluid.events import ErrorEvent, UserTurn, Usage
    from fluid.store.base import MemoryStore

    requests = []

    async def handle(reader, writer):
        try:
            requests.append(await reader.readuntil(b"\r\n\r\n"))
            payload = start("responses") + event("response.output_item.added", {
                "item": {"id": "msg_partial", "type": "message"},
            }) + event("response.output_text.delta", {
                "item_id": "msg_partial", "delta": "Keep this partial answer.",
            })
            writer.write(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n"
                         b"Content-Type: text/event-stream\r\n\r\n")
            writer.write(f"{len(payload):x}\r\n".encode() + payload + b"\r\n")
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    try:
        port = server.sockets[0].getsockname()[1]
        async with httpx.AsyncClient(trust_env=False) as http:
            client = OpenAIResponsesClient(
                api_key="test", base_url=f"http://127.0.0.1:{port}/responses",
                http_client=http, retry=RetryPolicy(base_delay=0),
            )
            store = MemoryStore()
            loop = NativeLoop(provider=client, store=store, model="test")
            try:
                await loop.submit(UserTurn(content="Start"))
                await asyncio.wait_for(loop.wait_turn(), 5)
                # Every attempt drops mid-reply: the first call plus the
                # automatic reconnects, then the turn ends on the error.
                assert len(requests) == 1 + native.TRANSPORT_DROP_MAX_RETRIES
                events = loop.bus.log(loop.session_id)
                error = next(e for e in events if isinstance(e, ErrorEvent))
                assert "connection was interrupted" in error.message
                assert not error.retriable
                evidence = next(e.raw["transport_failure"] for e in events
                                if isinstance(e, Usage) and "transport_failure" in e.raw)
                assert evidence["incomplete_chunked_read"]
                assert evidence["chunks_delivered"] and not evidence["retrying"]
                assert any(n.message.text() == "Keep this partial answer."
                           for n in await store.load_path(loop.session_id))
            finally:
                await loop.aclose()
    finally:
        server.close()
        await server.wait_closed()


async def _collect(source):
    return [chunk async for chunk in source]


def test_elapsed_and_idle_timing(monkeypatch):
    clock = iter([10.0, 12.0, 20.0])
    monkeypatch.setattr("fluid.providers._transport.time.monotonic", lambda: next(clock))
    diag = StreamDiagnostics("responses", URL, "model", 2)
    diag.received_headers(httpx.Response(200))
    diag.last_line_at = 19.0
    evidence = diag.failure(httpx.ReadTimeout("private-error"), yielded=True, retrying=False)
    assert evidence["elapsed_seconds"] == 10
    assert evidence["headers_after_seconds"] == 2
    assert evidence["last_line_age_seconds"] == 1
