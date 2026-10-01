"""Images a bridged tool returns reach the delegated CLI.

The delegated leg serves the app's own tools to the ``claude`` CLI through the
bundled MCP bridge. ``ToolRegistry._normalize`` turns SDK image blocks into
``ToolOutcome.images``; the bridge used to answer a call with text only, so a
computer-use screenshot or an image read through the attachment reader reached
the model as a placeholder line. These tests drive the real parent socket and
the real child call handler.
"""

from __future__ import annotations

import base64
import inspect
from contextlib import asynccontextmanager
from typing import Any

import mcp.types as t
import pytest

from fluid.tools.inproc import create_inproc_server, tool
from fluid.tools.mcp import bundled_server
from fluid.tools.mcp.bundled_server import (
    _answer,
    build_child_server,
    serve_registry_socket,
)
from fluid.tools.registry import ToolCtx, ToolImage, ToolOutcome, ToolRegistry, ToolSpec

PNG_B64 = base64.b64encode(b"\x89PNG\r\n\x1a\nfake-pixels").decode("ascii")


def _modern_server_api() -> bool:
    from mcp.server import Server

    return "on_list_tools" in inspect.signature(Server.__init__).parameters


@asynccontextmanager
async def _served(reg: ToolRegistry):
    """The parent bridge on an ephemeral loopback port, and the child server."""
    server = await serve_registry_socket(reg, "tcp:127.0.0.1:0", ToolCtx(), token="tok")
    try:
        port = server.sockets[0].getsockname()[1]
        yield build_child_server(f"tcp:127.0.0.1:{port}", "tok")
    finally:
        server.close()
        await server.wait_closed()


async def _child_call(child: Any, name: str, arguments: dict[str, Any]) -> Any:
    """Invoke the child's tools/call handler; returns the CallToolResult."""
    if _modern_server_api():  # pragma: no cover - mcp>=2.0 only
        params = t.CallToolRequestParams(name=name, arguments=arguments)
        return await child.get_request_handler("tools/call").handler(None, params)
    # mcp 1.x validates arguments against the cached tool list: prime it.
    await child.request_handlers[t.ListToolsRequest](t.ListToolsRequest(method="tools/list"))
    req = t.CallToolRequest(
        method="tools/call", params=t.CallToolRequestParams(name=name, arguments=arguments),
    )
    return (await child.request_handlers[t.CallToolRequest](req)).root


def _mime(block: Any) -> str:
    return getattr(block, "mimeType", None) or getattr(block, "mime_type")


def _is_error(result: Any) -> bool:
    return bool(getattr(result, "isError", None) or getattr(result, "is_error", False))


@tool("screenshot", "returns the screen", {"type": "object", "properties": {}})
async def _screenshot(args: dict) -> dict:
    return {"content": [
        {"type": "text", "text": '{"success": true}'},
        {"type": "image", "data": PNG_B64, "mimeType": "image/png"},
    ]}


async def test_inproc_image_reaches_the_child_as_image_content() -> None:
    reg = ToolRegistry()
    reg.register_inproc(create_inproc_server("computer-use", [_screenshot]))

    async with _served(reg) as child:
        result = await _child_call(child, "mcp__computer-use__screenshot", {})

    assert [c.type for c in result.content] == ["text", "image"]
    assert result.content[0].text == '{"success": true}'
    assert result.content[1].data == PNG_B64
    assert _mime(result.content[1]) == "image/png"
    assert not _is_error(result)


async def test_builtin_tool_images_reach_the_child() -> None:
    reg = ToolRegistry()

    async def read_image(args: dict, ctx: ToolCtx) -> ToolOutcome:
        return ToolOutcome(
            text="[image: a.jpg, image/jpeg, 4 bytes]",
            images=(ToolImage(media_type="image/jpeg", data=PNG_B64),),
        )

    reg.register_builtin(
        ToolSpec(name="mcp__attachments__Read", description="read",
                 input_schema={"type": "object", "properties": {}}),
        read_image,
    )

    async with _served(reg) as child:
        result = await _child_call(child, "mcp__attachments__Read", {})

    assert [c.type for c in result.content] == ["text", "image"]
    assert result.content[1].data == PNG_B64
    assert _mime(result.content[1]) == "image/jpeg"


async def test_image_only_result_has_no_empty_text_block() -> None:
    reg = ToolRegistry()

    async def only_image(args: dict, ctx: ToolCtx) -> ToolOutcome:
        return ToolOutcome(text="", images=(ToolImage(media_type="image/png", data=PNG_B64),))

    reg.register_builtin(
        ToolSpec(name="Shot", description="d", input_schema={"type": "object"}), only_image,
    )
    async with _served(reg) as child:
        result = await _child_call(child, "Shot", {})

    assert [c.type for c in result.content] == ["image"]


async def test_oversized_image_is_dropped_with_a_note(monkeypatch) -> None:
    monkeypatch.setattr(bundled_server, "IMAGE_BUDGET_CHARS", 64)
    small = base64.b64encode(b"x" * 12).decode("ascii")  # 16 chars
    big = base64.b64encode(b"y" * 90).decode("ascii")  # 120 chars
    reg = ToolRegistry()

    async def two_images(args: dict, ctx: ToolCtx) -> ToolOutcome:
        return ToolOutcome(text="two shots", images=(
            ToolImage(media_type="image/png", data=small),
            ToolImage(media_type="image/png", data=big),
        ))

    reg.register_builtin(
        ToolSpec(name="Shots", description="d", input_schema={"type": "object"}), two_images,
    )

    reply = await _answer(reg, ToolCtx(), {"op": "call", "name": "Shots", "arguments": {}})
    assert reply["images"] == [{"media_type": "image/png", "data": small}]
    assert reply["text"].startswith("two shots\n")
    assert "image omitted" in reply["text"]
    assert "too large" in reply["text"]

    async with _served(reg) as child:
        result = await _child_call(child, "Shots", {})
    assert [c.type for c in result.content] == ["text", "image"]
    assert result.content[1].data == small


async def test_text_only_result_is_unchanged() -> None:
    reg = ToolRegistry()

    @tool("echo", "echo", {"type": "object", "properties": {}})
    async def echo(args: dict) -> dict:
        return {"content": [{"type": "text", "text": "hello"}]}

    reg.register_inproc(create_inproc_server("s", [echo]))

    reply = await _answer(reg, ToolCtx(), {"op": "call", "name": "mcp__s__echo", "arguments": {}})
    assert reply == {"text": "hello", "is_error": False}

    async with _served(reg) as child:
        result = await _child_call(child, "mcp__s__echo", {})
    assert [c.type for c in result.content] == ["text"]
    assert result.content[0].text == "hello"


async def test_error_flag_survives_next_to_an_image() -> None:
    reg = ToolRegistry()

    async def failing(args: dict, ctx: ToolCtx) -> ToolOutcome:
        return ToolOutcome(
            text="partial", is_error=True,
            images=(ToolImage(media_type="image/png", data=PNG_B64),),
        )

    reg.register_builtin(
        ToolSpec(name="Fail", description="d", input_schema={"type": "object"}), failing,
    )
    async with _served(reg) as child:
        result = await _child_call(child, "Fail", {})
    assert _is_error(result)
    assert [c.type for c in result.content] == ["text", "image"]


@pytest.mark.parametrize("data", [b"raw-bytes"])
def test_bytes_image_data_is_base64_encoded(data: bytes) -> None:
    from fluid.tools.mcp.bundled_server import _call_answer

    outcome = ToolOutcome(text="x", images=(ToolImage(media_type="image/png", data=data),))  # type: ignore[arg-type]
    reply = _call_answer(outcome)
    assert reply["images"][0]["data"] == base64.b64encode(data).decode("ascii")
