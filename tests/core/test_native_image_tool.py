"""Configured Responses transport through saved images and durable tool replay."""

import asyncio
import base64
import json
import struct
import zlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fluid.engine.native import NativeLoop
from fluid.events.model import ToolResult, event_from_dict, event_to_dict
from fluid.providers._retry import RetryPolicy
from fluid.providers.base import StreamChunk
from fluid.providers.openai_responses.client import OpenAIResponsesClient
from fluid.store.base import MemoryStore
from fluid.tools.registry import ToolCtx

from app.core.provider.harness_adapter import HarnessAdapter
from app.core.provider.native_images import SPEC, make_handler
from app.core.query_streaming import extract_mcp_image


def png():
    def chunk(kind, data):
        return struct.pack("!I", len(data)) + kind + data + struct.pack("!I", zlib.crc32(kind + data))
    pixels = (b"\x00" + bytes((90, 120, 240)) * 16) * 16
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack("!2I5B", 16, 16, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(pixels)) + chunk(b"IEND", b""))


def stream_response(data):
    item = {"id": "ig_test", "type": "image_generation_call", "status": "completed",
            "result": base64.b64encode(data).decode(), "output_format": "png"}
    events = [
        ("response.created", {"response": {"id": "resp_test"}}),
        ("response.output_item.done", {"item": item}),
        ("response.completed", {"response": {"id": "resp_test", "status": "completed", "output": [item],
                                             "usage": {"input_tokens": 10, "output_tokens": 5}}}),
    ]
    return "".join(f"event: {kind}\ndata: {json.dumps(body)}\n\n" for kind, body in events).encode()


async def test_current_provider_request_saved_media_and_durable_inline_replay(tmp_path):
    data, requests = png(), []

    def transport(request):
        requests.append(request)
        return httpx.Response(200, content=stream_response(data), headers={"content-type": "text/event-stream"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        provider = OpenAIResponsesClient(api_key="only-configured-test-key", base_url="https://configured.example/v1/responses", http_client=http)
        engine = NativeLoop(provider=provider, store=MemoryStore(), model="configured-orchestrator")
        engine.record_auxiliary_usage = AsyncMock()
        outcome = await make_handler(lambda: engine, lambda: "app-session")(
            {"prompt": "A tree", "image_model": "requested-image-model"}, ToolCtx(cwd=str(tmp_path)))
    assert not outcome.is_error
    assert len(requests) == 1
    request = requests[0]
    assert str(request.url) == "https://configured.example/v1/responses"
    assert request.headers["authorization"] == "Bearer only-configured-test-key"
    body = json.loads(request.content)
    assert body["model"] == "configured-orchestrator"
    assert body["tool_choice"] == {"type": "image_generation"}
    assert body["tools"] == [{"type": "image_generation", "model": "requested-image-model", "output_format": "png", "partial_images": 0}]
    result = json.loads(outcome.text)
    assert Path(result["images"][0]["path"]).read_bytes() == data
    assert "session_id=app-session" in result["image_url"]
    assert len(outcome.images) == 1  # terminal response repeated the same image
    engine.record_auxiliary_usage.assert_awaited_once()
    assert engine.record_auxiliary_usage.call_args.args[0]["output_tokens"] == 5
    engine._emit = AsyncMock()
    await engine._finish_tool("engine-session", "turn", SPEC.name, "image-call", outcome)
    event = next(c.args[0] for c in engine._emit.call_args_list if isinstance(c.args[0], ToolResult))
    reopened = event_from_dict(json.loads(json.dumps(event_to_dict(event))))
    block = HarnessAdapter()._translate(reopened)[0].content[0]
    assert extract_mcp_image(block.content) == "data:image/png;base64," + base64.b64encode(data).decode()


@pytest.mark.parametrize("status", [400, 403, 404, 429, 500])
async def test_rejection_is_specific_to_native_route_and_never_replayed(tmp_path, status):
    requests = []

    def transport(request):
        requests.append(request)
        return httpx.Response(status, json={"error": {"code": "unsupported_tool", "message": "secret-must-not-leak"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        provider = OpenAIResponsesClient(api_key="test-only", http_client=http, retry=RetryPolicy(base_delay=0))
        engine = SimpleNamespace(provider=provider, model="configured", record_auxiliary_usage=AsyncMock())
        outcome = await make_handler(lambda: engine, lambda: "session")({"prompt": "A tree"}, ToolCtx(cwd=str(tmp_path)))
    assert len(requests) == 1 and outcome.is_error
    assert f"HTTP {status}" in outcome.text
    assert "fal.ai availability is independent" in outcome.text
    assert "secret-must-not-leak" not in outcome.text
    assert not (tmp_path / "generated-images").exists()


async def test_uncertain_transport_failure_is_not_replayed(tmp_path):
    requests = []

    def transport(request):
        requests.append(request)
        raise httpx.ReadTimeout("may already have generated an image", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        provider = OpenAIResponsesClient(api_key="test-only", http_client=http, retry=RetryPolicy(base_delay=0))
        engine = SimpleNamespace(provider=provider, model="configured", record_auxiliary_usage=AsyncMock())
        outcome = await make_handler(lambda: engine, lambda: "session")({"prompt": "A tree"}, ToolCtx(cwd=str(tmp_path)))
    assert outcome.is_error and len(requests) == 1


async def test_cancelled_stream_preserves_observed_usage_without_publishing_preview(tmp_path):
    entered, closed = asyncio.Event(), asyncio.Event()

    class Provider:
        name = "openai_responses"

        async def stream(self, request):
            try:
                yield StreamChunk("usage", {"usage": {"input_tokens": 10}})
                yield StreamChunk("image_generation_progress", {"partial_image_b64": base64.b64encode(png()).decode()})
                entered.set()
                await asyncio.Event().wait()
            finally:
                closed.set()

    engine = SimpleNamespace(provider=Provider(), model="configured", record_auxiliary_usage=AsyncMock())
    task = asyncio.create_task(make_handler(lambda: engine, lambda: "session")({"prompt": "A tree"}, ToolCtx(cwd=str(tmp_path))))
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set()
    engine.record_auxiliary_usage.assert_awaited_once()
    assert not (tmp_path / "generated-images").exists()


@pytest.mark.parametrize("result", ["not-base64", base64.b64encode(b"not an image").decode()])
async def test_invalid_images_are_not_published(tmp_path, result):
    class Provider:
        name = "openai_responses"

        async def stream(self, request):
            yield StreamChunk("image_generation", {"status": "completed", "result": result})

    engine = SimpleNamespace(provider=Provider(), model="configured", record_auxiliary_usage=AsyncMock())
    outcome = await make_handler(lambda: engine, lambda: "session")({"prompt": "A tree"}, ToolCtx(cwd=str(tmp_path)))
    assert outcome.is_error
    assert list((tmp_path / "generated-images").iterdir()) == []


async def test_chat_mode_creates_only_a_session_owned_artifact(tmp_path, mock_db, monkeypatch):
    from app.core import artifact_store
    from app.db import artifacts, sessions

    monkeypatch.setattr(artifact_store, "ARTIFACTS_DIR", tmp_path / "artifacts")
    sessions.create_session("image-chat", "default")
    data = png()

    def transport(request):
        return httpx.Response(200, content=stream_response(data))

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        engine = SimpleNamespace(provider=OpenAIResponsesClient(api_key="test-only", http_client=http),
                                 model="configured", record_auxiliary_usage=AsyncMock())
        outcome = await make_handler(lambda: engine, lambda: "image-chat", chat_mode=True)(
            {"prompt": "A tree"}, ToolCtx(cwd=str(tmp_path / "forbidden-or-absent-workspace")))
    assert not outcome.is_error
    result = json.loads(outcome.text)
    assert "path" not in result["images"][0]
    assert result["image_url"].endswith("?session_id=image-chat")
    digest = result["image_url"].split("/artifacts/", 1)[1].split("?", 1)[0]
    assert artifacts.get_artifact(digest)["created_by_session"] == "image-chat"
    assert artifact_store.artifact_path(digest).read_bytes() == data
    assert not (tmp_path / "forbidden-or-absent-workspace").exists()


@pytest.mark.parametrize("kind,mode,exposed", [("openai_responses", "code", True),
                                               ("openai_responses", "chat", True),
                                               ("anthropic", "code", False), ("openrouter", "code", False)])
async def test_registry_exposure_tracks_transport_not_model_name(kind, mode, exposed):
    from app.core.provider.types import SessionOptions

    adapter = HarnessAdapter()
    adapter._provider_kind = kind
    registry = await adapter._build_registry(SessionOptions(model="unrelated-model-name", extra={"session_mode": mode}), native=True)
    assert (SPEC.name in {s.name for s in registry.list_tools()}) is exposed


async def test_full_native_loop_calls_image_provider_and_records_its_usage_once(tmp_path):
    from fluid.events.model import TurnComplete, Usage
    from fluid.events.ops import UserTurn
    from fluid.permissions.engine import PermissionEngine
    from fluid.tools.registry import ToolRegistry

    calls = []

    def transport(request):
        body = json.loads(request.content)
        calls.append(body)
        if any(t.get("type") == "image_generation" for t in body.get("tools", [])):
            return httpx.Response(200, content=stream_response(png()))
        if len(calls) == 1:
            item = {"id": "fc1", "type": "function_call", "call_id": "call1", "name": SPEC.name,
                    "arguments": json.dumps({"prompt": "Draw a tree"})}
        else:
            item = {"id": "text1", "type": "message", "content": [{"type": "output_text", "text": "Image saved."}]}
        events = [("response.output_item.done", {"item": item}),
                  ("response.completed", {"response": {"status": "completed", "usage": {"input_tokens": 2, "output_tokens": 1}}})]
        return httpx.Response(200, content="".join(f"event: {k}\ndata: {json.dumps(v)}\n\n" for k, v in events).encode())

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        registry = ToolRegistry()
        engine = NativeLoop(provider=OpenAIResponsesClient(api_key="test-only", http_client=http),
                            model="configured", store=MemoryStore(), registry=registry,
                            permissions=PermissionEngine(approval_policy="never_ask"), ctx=ToolCtx(cwd=str(tmp_path)))
        registry.register_builtin(SPEC, make_handler(lambda: engine, lambda: "app-session"))
        try:
            await engine.submit(UserTurn(content="Draw a tree with the native image tool."))
            await asyncio.wait_for(engine.wait_turn(), 5)
            events = list(engine.bus.log(engine.session_id))
            assert len(calls) == 3
            results = [e for e in events if isinstance(e, ToolResult)]
            assert len(results) == 1 and not results[0].is_error and len(results[0].images) == 1
            auxiliary = [e for e in events if isinstance(e, Usage) and e.raw.get("auxiliary_source") == "native_image_generation"]
            assert len(auxiliary) == 1 and auxiliary[0].output_tokens == 5
            assert auxiliary[0].raw["cost_incomplete"] is True
            complete = next(e for e in events if isinstance(e, TurnComplete))
            assert complete.usage["output_tokens"] == 7
        finally:
            await engine.aclose()
