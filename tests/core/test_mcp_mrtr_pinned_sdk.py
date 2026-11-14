"""MRTR ``input_required`` round-trip on the ``mcp`` SDK the app ships (#1560).

fluid's ``McpClient`` was written against mcp 2.x, whose ``call_tool`` takes
``input_responses``/``request_state``. The app pins mcp 1.x (requirements.txt),
where those keywords do not exist; the client used to catch the ``TypeError``
and retry with the tool input alone, so the user's answer and the request
state never reached the server. This file runs on the app's own dependency set
(the backend CI job installs requirements.txt), so it fails whenever the
shipped SDK cannot carry an MRTR answer — the requirement mismatch cannot
come back silently.

The server is a real in-process MCP server on the installed SDK, connected to
``McpClient`` over in-memory streams, and the round-trip goes through the same
``ToolRegistry`` + ``resolve_input_required`` path native sessions use. Its
``input_required`` reply carries no ``content`` (the 2026-07-28 wire shape),
which mcp 1.x's ``CallToolResult`` would reject.
"""

from __future__ import annotations

import contextlib
from typing import Any

import anyio
import mcp.types as mcp_types
import pytest
from fluid.tools.mcp.client import McpClient
from fluid.tools.mrtr import resolve_input_required
from fluid.tools.registry import ToolCtx, ToolRegistry

TOOL = "delete_file"
STATE = "rs-delete-1"
ANSWERS = {"confirm": {"action": "accept", "content": {"ok": True}}}


def _mrtr_server(received: list[dict[str, Any]], *, pad_content: bool):
    """An MCP server whose one tool asks for confirmation before it runs.

    ``pad_content`` adds an empty ``content`` list to the ``input_required``
    reply, as some servers do, so the first leg parses even on mcp 1.x's
    ``CallToolResult``; without it the reply has the bare 2026-07-28 shape.
    """
    from mcp.server.lowlevel import Server

    server = Server("mrtr-fixture")

    @server.list_tools()
    async def _list_tools() -> list[Any]:
        return [mcp_types.Tool(
            name=TOOL,
            description="Delete a file after the user confirms.",
            inputSchema={"type": "object", "properties": {"path": {"type": "string"}}},
        )]

    async def _call_tool(req: Any) -> Any:
        extra = req.params.model_extra or {}
        responses = extra.get("inputResponses")
        state = extra.get("requestState")
        received.append({
            "arguments": req.params.arguments,
            "inputResponses": responses,
            "requestState": state,
        })
        if responses == ANSWERS and state == STATE:
            return mcp_types.ServerResult(mcp_types.CallToolResult(
                content=[mcp_types.TextContent(type="text", text="deleted after confirmation")],
            ))
        padding: dict[str, Any] = {"content": []} if pad_content else {}
        return mcp_types.ServerResult(mcp_types.EmptyResult(
            **padding,
            resultType="input_required",
            requestState=STATE,
            inputRequests={"confirm": {
                "method": "elicitation/create",
                "params": {
                    "mode": "form",
                    "message": "Really delete it?",
                    "requestedSchema": {
                        "type": "object",
                        "properties": {"ok": {"type": "boolean"}},
                    },
                },
            }},
        ))

    server.request_handlers[mcp_types.CallToolRequest] = _call_tool
    return server


@pytest.fixture(params=[False, True], ids=["bare-input-required", "padded-input-required"])
def in_process_server(request, monkeypatch):
    """Route every McpClient transport to a fresh in-process server."""
    from mcp.shared.memory import create_client_server_memory_streams

    received: list[dict[str, Any]] = []
    server = _mrtr_server(received, pad_content=request.param)

    @contextlib.asynccontextmanager
    async def _transport():
        async with (
            create_client_server_memory_streams() as (client_streams, server_streams),
            anyio.create_task_group() as tg,
        ):
            tg.start_soon(
                server.run, server_streams[0], server_streams[1],
                server.create_initialization_options(),
            )
            yield client_streams
            tg.cancel_scope.cancel()

    monkeypatch.setattr(McpClient, "_open_transport", lambda self: (_transport(), None))
    return received


@pytest.mark.asyncio
async def test_input_required_answers_reach_the_server(in_process_server):
    received = in_process_server
    reg = ToolRegistry()
    name = f"mcp__srv__{TOOL}"
    try:
        await reg.connect_mcp("srv", {"transport": "stdio", "command": "unused"})
        first = await reg.dispatch(name, {"path": "a.txt"}, ToolCtx())
        assert first.input_required is not None, first.text
        assert first.input_required.request_state == STATE
        assert "confirm" in first.input_required.requests

        asked: list[Any] = []

        async def collect(marker):
            asked.append(marker)
            return ANSWERS

        outcome = await resolve_input_required(
            first, name=name, collect=collect,
            redispatch=lambda answers, state: reg.dispatch(
                name, {"path": "a.txt"}, ToolCtx(),
                input_responses=answers, request_state=state,
            ),
        )
    finally:
        await reg.aclose()

    assert len(asked) == 1
    assert not outcome.is_error, outcome.text
    assert "deleted after confirmation" in outcome.text
    # The retry is the SAME call plus the answer and the echoed state.
    assert received == [
        {"arguments": {"path": "a.txt"}, "inputResponses": None, "requestState": None},
        {"arguments": {"path": "a.txt"}, "inputResponses": ANSWERS, "requestState": STATE},
    ]
