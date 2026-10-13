"""Provider failure evidence survives the engine and reaches durable app logs."""
import asyncio
import json
import logging

import httpx
import pytest

from app.core import error_log_handler
from app.core.provider.harness_adapter import HarnessAdapter
from app.core.provider.types import SessionOptions
from app.db import error_logs
import fluid.engine.native as native
from fluid.engine.native import NativeLoop
from fluid.events import ErrorEvent, Usage, UserTurn
from fluid.providers._retry import RetryPolicy
from fluid.providers.openai_responses.client import OpenAIResponsesClient
from fluid.store.base import MemoryStore


@pytest.mark.asyncio
async def test_transport_failure_has_app_session_and_engine_turn(monkeypatch):
    class Truncated(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'event: response.created\ndata: {"response":{"id":"resp_test","status":"in_progress"}}\n\n'
            yield b'event: response.output_text.delta\ndata: {"output_index":0,"delta":"private body"}\n\n'
            raise httpx.RemoteProtocolError("peer closed connection (incomplete chunked read)")

    rows = []
    monkeypatch.setattr(native, "_empty_retry_delay", lambda attempt: 0.0)
    monkeypatch.setattr(error_logs, "record_error", lambda **row: rows.append(row))
    monkeypatch.setattr(error_log_handler, "_capture_level", lambda: logging.WARNING)
    handler = error_log_handler.ErrorLogDBHandler()
    logger = logging.getLogger("app.core.provider.harness_adapter")
    logger.addHandler(handler)
    adapter = HarnessAdapter()
    adapter._options = SessionOptions(model="test-model", extra={"app_session_id": "app-session-test"})
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(200, stream=Truncated(), headers={"x-request-id": "upstream-123"})
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            provider = OpenAIResponsesClient(api_key="private-key", http_client=http, retry=RetryPolicy(max_retries=0))
            loop = NativeLoop(provider=provider, store=MemoryStore(), model="test-model")
            try:
                await loop.submit(UserTurn(content="private prompt"))
                await asyncio.wait_for(loop.wait_turn(), 5)
                events = loop.bus.log(loop.session_id)
                failures = [e for e in events if isinstance(e, Usage) and "transport_failure" in e.raw]
                # The drop left no closed tool call, so the request was re-sent
                # within the reconnect budget; every attempt dropped the same way.
                attempts = 1 + native.TRANSPORT_DROP_MAX_RETRIES
                assert len(failures) == attempts
                assert any(isinstance(e, ErrorEvent) for e in events)
                assert len(calls) == attempts
                usage_event = failures[-1]
                translated = adapter._translate(usage_event)
                assert translated[0].usage["transport_failure"] == usage_event.raw["transport_failure"]
                assert len(rows) == 1
                row = rows[0]
                assert row["session_id"] == "app-session-test"
                assert row["level"] == "WARNING"
                assert usage_event.session_id in row["message"]
                assert usage_event.turn_id in row["message"]
                assert "upstream-123" in row["message"]
                for private in ("private-key", "private prompt", "private body"):
                    assert private not in json.dumps(row)
            finally:
                await loop.aclose()
    finally:
        logger.removeHandler(handler)
