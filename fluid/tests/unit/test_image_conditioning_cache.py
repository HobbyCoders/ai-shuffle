"""Regression coverage for #1235: retained screenshots never encode on the loop."""

import base64
import io
import random
import threading
from collections import OrderedDict

import pytest
from PIL import Image as PILImage

from fluid.messages import Image, Message, Text, ToolResultBlock, ToolUseBlock
from fluid.providers.base import ProviderRequest
from fluid.providers.openai_responses.serialize import build_request_body
from fluid.tools import images


@pytest.fixture(autouse=True)
def empty_image_cache(monkeypatch):
    monkeypatch.setattr(images, "_cache", OrderedDict(), raising=False)
    monkeypatch.setattr(images, "_cache_bytes", 0, raising=False)


@pytest.fixture(scope="module")
def screenshot():
    # Already optimized, over 512 KB, below both the edge and megapixel caps.
    # Re-encoding it cannot save bytes: the original must be memoized too.
    raw = random.Random(1235).randbytes(480 * 400 * 3)
    buffer = io.BytesIO()
    PILImage.frombytes("RGB", (480, 400), raw).save(buffer, format="PNG", optimize=True)
    assert len(buffer.getvalue()) > images._REENCODE_RAW_BYTES
    return base64.b64encode(buffer.getvalue()).decode("ascii")


@pytest.fixture
def encoder_threads(monkeypatch):
    threads = []
    save = PILImage.Image.save

    def spy(self, *args, **kwargs):
        threads.append(threading.get_ident())
        return save(self, *args, **kwargs)

    monkeypatch.setattr(PILImage.Image, "save", spy)
    return threads


def image_history(data):
    return [
        Message("user", [Text("inspect the screenshot")]),
        Message("assistant", [ToolUseBlock(id="shot", name="Screenshot", input={})]),
        Message("user", [ToolResultBlock(tool_use_id="shot", content=[Image("image/png", data)])]),
        Message("user", [Text("continue"), Image("image/png", data)]),
    ]


def test_kept_original_is_encoded_only_once(screenshot, encoder_threads):
    original = ("image/png", screenshot)
    assert images.condition_image(*original) == original
    assert images.condition_image(*original) == original
    assert len(encoder_threads) == 1


def test_repeated_request_serialization_and_checkpoints_reuse_image(screenshot, encoder_threads):
    from fluid.context.cache_probe import fingerprint_responses_request

    request = ProviderRequest(model="test-model", messages=image_history(screenshot))
    stored = [m.to_dict() for m in request.messages]
    body = build_request_body(request)
    fingerprint = {}
    fingerprint_responses_request(request, fingerprint, previous_count=0)
    assert build_request_body(request) == body
    again = {}
    fingerprint_responses_request(request, again, previous_count=0)
    assert again == fingerprint
    assert [m.to_dict() for m in request.messages] == stored
    assert len(encoder_threads) == 1


@pytest.mark.parametrize("dialect", ["anthropic", "responses", "chat"])
def test_all_serializers_share_the_memo(dialect, screenshot, encoder_threads):
    from fluid.providers.anthropic.serialize import build_request_body as anthropic_body
    from fluid.providers.openrouter.serialize import build_request_body as chat_body

    request = ProviderRequest(model="test-model", messages=image_history(screenshot))
    serialize = {"anthropic": anthropic_body, "responses": build_request_body, "chat": chat_body}[dialect]
    body = serialize(request)
    assert serialize(request) == body
    assert len(encoder_threads) == 1


def test_wire_body_and_fingerprints_match_without_memo(monkeypatch, screenshot):
    from fluid.context.cache_probe import fingerprint_responses_request

    request = ProviderRequest(model="test-model", messages=image_history(screenshot))
    with monkeypatch.context() as cold:
        cold.setattr(images, "_CACHE_MAX_BYTES", 0)
        expected_body = build_request_body(request)
        expected_fingerprint = {}
        fingerprint_responses_request(request, expected_fingerprint, previous_count=3)
    actual_fingerprint = {}
    fingerprint_responses_request(request, actual_fingerprint, previous_count=3)
    assert build_request_body(request) == expected_body
    assert actual_fingerprint == expected_fingerprint


def test_byte_bound_eviction_and_oversized_entry(monkeypatch, screenshot):
    # Fit two entries exactly; accessing one makes the other the LRU victim.
    size = images._cache_entry_bytes(("image/png", b"x" * 32), ("image/png", screenshot))
    monkeypatch.setattr(images, "_CACHE_MAX_BYTES", size * 2)
    calls = []

    def condition(media_type, data):
        calls.append(data)
        return media_type, data

    monkeypatch.setattr(images, "_condition_image", condition)
    a, b, c = (screenshot[:-1] + suffix for suffix in "abc")
    for data in (a, b, a, c):
        images.condition_image("image/png", data)
    assert calls == [a, b, c]
    assert images._cache_bytes == sum(entry[1] for entry in images._cache.values())
    assert images._cache_bytes <= images._CACHE_MAX_BYTES
    assert len(images._cache) == 2
    images.condition_image("image/png", b)
    assert calls == [a, b, c, b]  # b, not a, was evicted
    images.condition_image("image/png", screenshot * 3)
    assert len(images._cache) == 2  # oversized entries are not retained
    assert images._cache_bytes <= images._CACHE_MAX_BYTES


def test_concurrent_conditioning_encodes_once(screenshot, encoder_threads):
    from concurrent.futures import ThreadPoolExecutor

    barrier = threading.Barrier(4)

    def condition(_):
        barrier.wait(timeout=5)
        return images.condition_image("image/png", screenshot)

    with ThreadPoolExecutor(max_workers=4) as executor:
        assert list(executor.map(condition, range(4))) == [("image/png", screenshot)] * 4
    assert len(encoder_threads) == 1


@pytest.mark.parametrize("provider", ["responses", "anthropic", "openrouter", "local"])
@pytest.mark.parametrize("budget", [0, 32 * 1024 * 1024], ids=["no-retention", "memo"])
async def test_provider_stream_encodes_off_loop(provider, budget, monkeypatch, screenshot, encoder_threads):
    import httpx
    from fluid.providers.anthropic.client import AnthropicClient
    from fluid.providers.local.client import LocalClient
    from fluid.providers.openai_responses.client import OpenAIResponsesClient
    from fluid.providers.openrouter.client import OpenRouterClient

    monkeypatch.setattr(images, "_CACHE_MAX_BYTES", budget)
    sent = []

    def handler(request):
        sent.append(request.content)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=b"")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        kwargs = dict(api_key="test-only", http_client=http)
        client = {
            "responses": lambda: OpenAIResponsesClient(**kwargs),
            "anthropic": lambda: AnthropicClient(**kwargs),
            "openrouter": lambda: OpenRouterClient(**kwargs),
            "local": lambda: LocalClient(base_url="http://model.invalid/v1", **kwargs),
        }[provider]()
        request = ProviderRequest(model="test-model", messages=image_history(screenshot))
        _ = [chunk async for chunk in client.stream(request)]
    assert sent
    assert encoder_threads
    assert threading.get_ident() not in encoder_threads


async def test_native_fingerprint_and_tool_ingest_encode_off_loop(screenshot, encoder_threads, monkeypatch):
    from fluid.engine.native import NativeLoop
    from fluid.providers.base import ProviderCaps, StreamChunk
    from fluid.store.base import MemoryStore
    from fluid.tools.registry import ToolImage, ToolOutcome

    class Provider:
        name = "openai_responses"
        caps = ProviderCaps()

        async def stream(self, request):
            yield StreamChunk("message_stop", {})

    loop = NativeLoop(provider=Provider(), store=MemoryStore(), model="test-model")
    await loop._stream_once("session", "turn", image_history(screenshot))
    assert encoder_threads and threading.get_ident() not in encoder_threads
    # Force the ingest path cold, so a hit from the request cannot hide a bug.
    images._cache.clear()
    images._cache_bytes = 0
    encoder_threads.clear()

    async def gated(*args):
        return ToolOutcome(text="screenshot", images=(ToolImage(media_type="image/png", data=screenshot),))

    monkeypatch.setattr(loop, "_gated_call", gated)
    result = await loop._dispatch_tool("session", "turn", {
        "tool_use_id": "shot", "name": "Screenshot", "input": {},
    })
    assert Image("image/png", screenshot) in result.content
    assert encoder_threads and threading.get_ident() not in encoder_threads


async def test_real_read_tool_produces_conditioned_history_off_loop(tmp_path, screenshot, encoder_threads):
    import asyncio
    import json
    from fluid.engine.native import NativeLoop
    from fluid.events import ToolResult, UserTurn
    from fluid.permissions.engine import PermissionEngine
    from fluid.providers.base import ProviderCaps, StreamChunk
    from fluid.store.base import MemoryStore
    from fluid.tools.builtins import register_builtins
    from fluid.tools.registry import ToolCtx, ToolRegistry

    path = tmp_path / "screenshot.png"
    path.write_bytes(base64.b64decode(screenshot))

    class Provider:
        name = "openai_responses"
        caps = ProviderCaps()

        def __init__(self):
            self.requests = []

        async def stream(self, request):
            self.requests.append(request)
            if len(self.requests) == 1:
                yield StreamChunk("block_start", {
                    "index": 0, "block_type": "tool_use", "tool_use_id": "read-shot", "tool_name": "Read",
                })
                yield StreamChunk("tool_args_delta", {
                    "index": 0, "partial_json": json.dumps({"file_path": str(path)}),
                })
                yield StreamChunk("block_stop", {"index": 0})
                yield StreamChunk("usage", {"stop_reason": "tool_use"})
            else:
                yield StreamChunk("block_start", {"index": 0, "block_type": "text"})
                yield StreamChunk("text_delta", {"index": 0, "text": "Screenshot received."})
                yield StreamChunk("block_stop", {"index": 0})
            yield StreamChunk("message_stop", {})

    registry = ToolRegistry()
    register_builtins(registry)
    provider = Provider()
    loop = NativeLoop(
        provider=provider, store=MemoryStore(), model="test-model", registry=registry,
        permissions=PermissionEngine("never_ask"), ctx=ToolCtx(cwd=str(tmp_path)),
    )
    try:
        await loop.submit(UserTurn(content="Read the screenshot."))
        await asyncio.wait_for(loop.wait_turn(), timeout=5)
        assert len(provider.requests) == 2
        results = [b for m in provider.requests[-1].messages for b in m.content if isinstance(b, ToolResultBlock)]
        assert any(Image("image/png", screenshot) in result.content for result in results)
        assert len(encoder_threads) == 1
        assert threading.get_ident() not in encoder_threads
        # The real ingest outcome still obeys the event-bus no-base64 contract.
        assert all(screenshot not in event.output for event in loop.bus.log(loop.session_id)
                   if isinstance(event, ToolResult))
    finally:
        await loop.aclose()


async def test_cancelled_fingerprint_cannot_publish_stale_state(monkeypatch):
    import asyncio
    from fluid.engine.native import NativeLoop
    from fluid.providers.base import ProviderCaps
    from fluid.store.base import MemoryStore

    class Provider:
        name = "openai_responses"
        caps = ProviderCaps()

    loop = NativeLoop(provider=Provider(), store=MemoryStore(), model="test-model")
    started, finished = asyncio.Event(), asyncio.Event()
    release = threading.Event()
    event_loop = asyncio.get_running_loop()

    def compute(request):
        event_loop.call_soon_threadsafe(started.set)
        assert release.wait(timeout=5)
        event_loop.call_soon_threadsafe(finished.set)
        return {"stale": "fingerprint"}, "stale system"

    monkeypatch.setattr(loop, "_compute_request_fingerprint", compute)
    task = asyncio.create_task(loop._stream_once("session", "turn", []))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        loop._prefix_fingerprint = {"new": "fingerprint"}
        loop._pending_system_text = "new system"
    finally:
        release.set()
        await asyncio.wait_for(finished.wait(), timeout=5)
    assert loop._prefix_fingerprint == {"new": "fingerprint"}
    assert loop._pending_system_text == "new system"
