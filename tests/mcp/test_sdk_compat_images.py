"""
Regression tests for the SDK MCP compat shim's content handling.

``app.mcp.sdk_compat.patch_sdk_server`` re-registers the ``call_tool`` handler
so tool results return a raw content list (mcp >=1.12 compat). It must:
  - pass ``text`` items through as TextContent (unchanged behaviour),
  - pass ``image`` items through as ImageContent (so computer-use screenshots
    actually reach the model as images, not JSON-stringified base64),
  - JSON-stringify any other content type into text (fallback behaviour).
"""

import base64

import pytest
from mcp.types import TextContent, ImageContent

from app.mcp.sdk_compat import patch_sdk_server


class _FakeTool:
    """Minimal stand-in for an SDK @tool (needs .name and .handler)."""

    def __init__(self, name, result):
        self.name = name
        self._result = result

    async def handler(self, arguments):
        return self._result


class _FakeInstance:
    """Captures the handler registered via @instance.call_tool()."""

    def __init__(self):
        self.handler = None

    def call_tool(self):
        def _decorator(fn):
            self.handler = fn
            return fn
        return _decorator


def _patch(tools):
    instance = _FakeInstance()
    server_cfg = {"name": "test", "instance": instance}
    patch_sdk_server(server_cfg, tools)
    assert instance.handler is not None, "patch did not register a call_tool handler"
    return instance.handler


# A 1x1 transparent PNG.
_PNG_B64 = base64.b64encode(
    base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
    )
).decode()


@pytest.mark.asyncio
async def test_image_content_passes_through_as_image():
    tool = _FakeTool(
        "screenshot",
        {"content": [{"type": "image", "data": _PNG_B64, "mimeType": "image/png"}]},
    )
    handler = _patch([tool])
    result = await handler("screenshot", {})

    assert len(result) == 1
    item = result[0]
    assert isinstance(item, ImageContent)
    assert item.type == "image"
    assert item.data == _PNG_B64
    assert item.mimeType == "image/png"


@pytest.mark.asyncio
async def test_image_defaults_mime_to_png():
    tool = _FakeTool(
        "screenshot",
        {"content": [{"type": "image", "data": _PNG_B64}]},
    )
    handler = _patch([tool])
    result = await handler("screenshot", {})

    assert isinstance(result[0], ImageContent)
    assert result[0].mimeType == "image/png"


@pytest.mark.asyncio
async def test_text_content_still_passes_through_as_text():
    tool = _FakeTool(
        "echo",
        {"content": [{"type": "text", "text": "hello"}]},
    )
    handler = _patch([tool])
    result = await handler("echo", {})

    assert isinstance(result[0], TextContent)
    assert result[0].text == "hello"


@pytest.mark.asyncio
async def test_unknown_content_type_is_json_stringified():
    tool = _FakeTool(
        "weird",
        {"content": [{"type": "resource", "uri": "file://x"}]},
    )
    handler = _patch([tool])
    result = await handler("weird", {})

    assert isinstance(result[0], TextContent)
    assert "resource" in result[0].text
    assert "file://x" in result[0].text


@pytest.mark.asyncio
async def test_mixed_text_and_image():
    tool = _FakeTool(
        "screenshot",
        {"content": [
            {"type": "text", "text": "display 1, 1280x800"},
            {"type": "image", "data": _PNG_B64, "mimeType": "image/png"},
        ]},
    )
    handler = _patch([tool])
    result = await handler("screenshot", {})

    assert len(result) == 2
    assert isinstance(result[0], TextContent)
    assert isinstance(result[1], ImageContent)
