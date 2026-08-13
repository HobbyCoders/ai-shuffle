"""McpClient compatibility with the released ``mcp`` SDK (floor: mcp>=1.2).

``test_mcp_client.py`` exercises the full stack against the in-repo fake server,
but that fixture needs MCP 2.0 server APIs (``mcp.server.caching``) that no
released ``mcp`` wheel ships yet, so it cannot run on the installed SDK. These
tests cover the two spots where the client assumed 2.0-only *client* attributes
and blew up with AttributeError on every real connect:

  * the handshake read ``session.protocol_version`` — absent on ClientSession;
    the negotiated version lives on the handshake *result* (``protocolVersion``)
  * ``_do_list`` read ``tool.input_schema`` — the ``Tool`` model only has the
    camelCase ``inputSchema``
"""

import asyncio
from typing import Any

import pytest

from omni.tools.mcp.client import McpClient


class _FakeStreams:
    async def __aenter__(self):
        return (object(), object())

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    """Stands in for ``mcp.client.session.ClientSession``.

    Mirrors the released SDK: no ``discover``, no ``protocol_version``; only
    ``initialize()``, returning an ``InitializeResult``-shaped object.
    """

    def __init__(self, read, write):
        self.initialized = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def initialize(self):
        self.initialized = True
        return type("InitializeResult", (), {"protocolVersion": "2025-06-18"})()


async def _start_fake_client(monkeypatch) -> McpClient:
    import mcp.client.session as session_mod
    import mcp.client.stdio as stdio_mod

    monkeypatch.setattr(session_mod, "ClientSession", _FakeSession)
    monkeypatch.setattr(stdio_mod, "StdioServerParameters", lambda **kw: kw)
    monkeypatch.setattr(stdio_mod, "stdio_client", lambda params: _FakeStreams())

    client = McpClient("fake", {"transport": "stdio", "command": "noop", "args": []})
    await client.start()
    return client


@pytest.mark.asyncio
async def test_handshake_reads_protocol_from_result(monkeypatch):
    client = await _start_fake_client(monkeypatch)
    try:
        # Legacy handshake (no ``discover`` on the released ClientSession) and
        # the negotiated version comes off the result, not the session.
        assert client.legacy is True
        assert client.protocol == "2025-06-18"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_handshake_survives_result_without_protocol(monkeypatch):
    class _NoVersionSession(_FakeSession):
        async def initialize(self):
            return object()

    import mcp.client.session as session_mod
    import mcp.client.stdio as stdio_mod

    monkeypatch.setattr(session_mod, "ClientSession", _NoVersionSession)
    monkeypatch.setattr(stdio_mod, "StdioServerParameters", lambda **kw: kw)
    monkeypatch.setattr(stdio_mod, "stdio_client", lambda params: _FakeStreams())

    client = McpClient("fake", {"transport": "stdio", "command": "noop", "args": []})
    await client.start()  # must not raise
    try:
        assert client.protocol is None
    finally:
        await client.aclose()


class _CamelCaseTool:
    """``mcp.types.Tool`` as the released SDK models it: camelCase only."""

    def __init__(self) -> None:
        self.name = "get_weather"
        self.description = "look up weather"
        self.inputSchema = {"type": "object", "required": ["city"]}
        self.annotations = {"readOnlyHint": True}


class _ListResult:
    tools = [_CamelCaseTool()]


@pytest.mark.asyncio
async def test_do_list_reads_camelcase_input_schema():
    class _Session:
        async def list_tools(self) -> Any:
            return _ListResult()

    client = McpClient("fake", {"transport": "stdio", "command": "noop"})
    tools = await client._do_list(_Session())

    assert len(tools) == 1
    assert tools[0].name == "get_weather"
    assert tools[0].input_schema == {"type": "object", "required": ["city"]}
    assert tools[0].risk == "safe"


@pytest.mark.asyncio
async def test_do_list_defaults_missing_schema():
    class _NoSchemaTool:
        name = "bare"
        description = None
        annotations = None

    class _Result:
        tools = [_NoSchemaTool()]

    class _Session:
        async def list_tools(self) -> Any:
            return _Result()

    client = McpClient("fake", {"transport": "stdio", "command": "noop"})
    tools = await client._do_list(_Session())

    assert tools[0].input_schema == {"type": "object"}
    assert tools[0].description == ""
