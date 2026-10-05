"""A screenshot tool result reaches the provider as a real image, every dialect.

Desktop and browser screenshots are in-process / MCP tool results that carry a
JSON text block plus a PNG. These tests run the real NativeLoop and the real
provider clients against the scripted loopback provider and assert on the
request bodies the provider received: the image part is present with its media
type and exact bytes, and its base64 never appears as text. They also pin the
mid-turn image count rung: a long act-and-look turn does not resend every
screenshot, and never drops the newest one.
"""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import io
import json
import sys
from pathlib import Path
from typing import Any

import fluid.engine.native as native_mod
import pytest
from fluid.context.compaction import CompactionConfig
from fluid.engine.native import NativeLoop
from fluid.events import UserTurn
from fluid.permissions.engine import PermissionEngine
from fluid.providers._retry import RetryPolicy
from fluid.providers.anthropic.client import AnthropicClient
from fluid.providers.local.client import LocalClient
from fluid.providers.openai_responses.client import OpenAIResponsesClient
from fluid.providers.openrouter.client import OpenRouterClient
from fluid.store.base import MemoryStore
from fluid.tools.inproc import create_inproc_server, tool
from fluid.tools.registry import ToolRegistry


def _load_fake_server():
    name = "fake_provider_server"
    if name in sys.modules:
        return sys.modules[name]
    path = Path(__file__).parent.parent / "fixtures" / "fake_provider_server.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


fps = _load_fake_server()
FAST_RETRY = RetryPolicy(max_retries=1, base_delay=0.01, max_delay=0.02, jitter=0)
MODELS = {
    "anthropic": "claude-sonnet-4-5",
    "openrouter": "vendor/fake-model",
    "local": "local-model",
    "responses": "gpt-5",
}


def _png(shade: int) -> str:
    from PIL import Image

    with Image.new("RGB", (4, 3), (shade, 0, 0)) as image:
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _client(kind: str, url: str) -> Any:
    if kind == "anthropic":
        return AnthropicClient(api_key="k", base_url=url + "/v1/messages", retry=FAST_RETRY)
    if kind == "openrouter":
        return OpenRouterClient(
            api_key="k", base_url=url + "/v1/chat/completions", retry=FAST_RETRY,
        )
    if kind == "local":
        return LocalClient(base_url=url, retry=FAST_RETRY)
    return OpenAIResponsesClient(api_key="k", base_url=url + "/v1/responses", retry=FAST_RETRY)


def _replies(kind: str):
    return {"anthropic": fps.Messages, "responses": fps.Responses}.get(kind, fps.Chat)


def _screenshot_server(shots: list[str], *, errors: bool = False) -> Any:
    """A computer-use-shaped server: JSON metadata text plus one PNG per call.
    ``errors``: every result is an error that still carries a fresh image
    (the browser's stale_screenshot refusal)."""

    @tool("screenshot", "Capture the screen.", {"type": "object", "properties": {}})
    async def screenshot(args: dict) -> dict:
        data = _png(len(shots) + 1)
        shots.append(data)
        return {**({"isError": True} if errors else {}), "content": [
            {"type": "text", "text": json.dumps(
                {"success": True, "virtual_width": 4, "virtual_height": 3}
            )},
            {"type": "image", "data": data, "mimeType": "image/png"},
        ]}

    return create_inproc_server("computer-use", [screenshot])


def _wire_images(value: Any):
    """(media_type, base64) of every image part in a request body."""
    if isinstance(value, list):
        for item in value:
            yield from _wire_images(item)
    elif isinstance(value, dict):
        if value.get("type") == "image":
            yield value["source"]["media_type"], value["source"]["data"]
        elif value.get("type") in ("image_url", "input_image"):
            url = value["image_url"]
            if isinstance(url, dict):
                url = url["url"]
            head, data = url.split(",", 1)
            yield head.removeprefix("data:").removesuffix(";base64"), data
        else:
            for item in value.values():
                yield from _wire_images(item)


def _text_values(value: Any):
    """Every string in the body that is NOT an image payload."""
    if isinstance(value, list):
        for item in value:
            yield from _text_values(item)
    elif isinstance(value, dict):
        if value.get("type") in ("image", "image_url", "input_image"):
            return
        for item in value.values():
            yield from _text_values(item)
    elif isinstance(value, str):
        yield value


@pytest.fixture
def server():
    srv = fps.FakeProviderServer()
    try:
        yield srv
    finally:
        srv.close()


@pytest.fixture(autouse=True)
def _no_empty_retry_backoff(monkeypatch):
    monkeypatch.setattr(native_mod, "_empty_retry_delay", lambda attempt: 0.0)


async def _run(loop: NativeLoop) -> None:
    await loop.submit(UserTurn(content="look at the screen"))
    await asyncio.wait_for(loop.wait_turn(), 20.0)


@pytest.mark.parametrize("kind", ["anthropic", "responses", "openrouter", "local"])
async def test_screenshot_result_is_a_real_image_part_in_the_next_request(server, kind):
    shots: list[str] = []
    registry = ToolRegistry()
    registry.register_inproc(_screenshot_server(shots))
    replies = _replies(kind)
    server.script(
        replies.tool_calls([{"id": "call_s", "name": "mcp__computer-use__screenshot",
                             "args": {}}]),
        replies.text("I can see it."),
    )
    loop = NativeLoop(
        provider=_client(kind, server.url), store=MemoryStore(), model=MODELS[kind],
        registry=registry, permissions=PermissionEngine("never_ask"),
    )
    try:
        await _run(loop)
    finally:
        await loop.aclose()

    assert len(shots) == 1
    body = server.requests[1].body
    assert list(_wire_images(body)) == [("image/png", shots[0])]
    # Not stringified anywhere: no text field carries the base64 or a dumped block.
    for text in _text_values(body):
        assert shots[0] not in text
        assert '"mimeType"' not in text and "'type': 'image'" not in text
    # The metadata text still reaches the model next to the image.
    assert any('"virtual_width": 4' in text for text in _text_values(body))
    if kind == "anthropic":
        [result] = [
            block for message in body["messages"] for block in message["content"]
            if block.get("type") == "tool_result"
        ]
        assert [c["type"] for c in result["content"]] == ["text", "image"]
        assert result["is_error"] is False
    elif kind == "responses":
        [output] = [i for i in body["input"] if i.get("type") == "function_call_output"]
        assert [part["type"] for part in output["output"]] == ["input_text", "input_image"]
    else:
        # chat/completions: tool content is text; the image rides the user
        # message that follows, labelled with the call it answers.
        messages = body["messages"]
        tool_at = next(i for i, m in enumerate(messages) if m["role"] == "tool")
        assert isinstance(messages[tool_at]["content"], str)
        follow = messages[tool_at + 1]
        assert follow["role"] == "user"
        assert [p["type"] for p in follow["content"]][:2] == ["text", "image_url"]


@pytest.mark.parametrize("errors", [False, True], ids=["results", "error_results"])
async def test_a_long_screenshot_turn_keeps_the_newest_images_and_caps_the_rest(server, errors):
    steps = 16
    shots: list[str] = []
    registry = ToolRegistry()
    registry.register_inproc(_screenshot_server(shots, errors=errors))
    server.script(
        *[
            fps.Messages.tool_calls([{"id": f"call_{i}", "name": "mcp__computer-use__screenshot",
                                     "args": {}}])
            for i in range(steps)
        ],
        fps.Messages.text("done"),
    )
    config = CompactionConfig(live_image_cap=6, live_image_keep=2)
    loop = NativeLoop(
        provider=_client("anthropic", server.url), store=MemoryStore(),
        model=MODELS["anthropic"], registry=registry,
        # Sixteen identical screenshots in a row: the host raises the repeat
        # guard for screen tools; this test is about the image rung only.
        permissions=PermissionEngine("never_ask", doom_loop_threshold_for=lambda _n: 100),
        compaction=config,
    )
    try:
        await _run(loop)
    finally:
        await loop.aclose()

    assert len(shots) == steps
    counts = []
    for index, request in enumerate(server.requests[1:], start=1):
        images = [data for _, data in _wire_images(request.body)]
        counts.append(len(images))
        # The observation the model is about to act on is always there.
        assert images[-1] == shots[index - 1]
        assert len(images) <= config.live_image_cap
    # One batch per (cap - keep) new screenshots, never one per call: the
    # count climbs to the cap, drops to keep + 1, climbs again.
    assert counts == [1, 2, 3, 4, 5, 6, 3, 4, 5, 6, 3, 4, 5, 6, 3, 4]
