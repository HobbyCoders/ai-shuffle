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

from typing import Any

import pytest

from fluid.tools.mcp.client import McpClient


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


# ─── deadlines and the streamable-HTTP transport ─────────────────────────────


class _TimeoutAwareSession(_FakeSession):
    """The 2.0 signature: ``read_timeout_seconds: float | None``."""

    seen: dict[str, Any] = {}

    def __init__(self, read, write, read_timeout_seconds: "float | None" = None):
        super().__init__(read, write)
        _TimeoutAwareSession.seen["read_timeout_seconds"] = read_timeout_seconds


@pytest.mark.asyncio
async def test_call_timeout_reaches_the_session_read_timeout(monkeypatch):
    import mcp.client.session as session_mod
    import mcp.client.stdio as stdio_mod

    monkeypatch.setattr(session_mod, "ClientSession", _TimeoutAwareSession)
    monkeypatch.setattr(stdio_mod, "StdioServerParameters", lambda **kw: kw)
    monkeypatch.setattr(stdio_mod, "stdio_client", lambda params: _FakeStreams())

    client = McpClient("fake", {"transport": "stdio", "command": "noop", "call_timeout": 7})
    await client.start()
    try:
        assert _TimeoutAwareSession.seen["read_timeout_seconds"] == 7
    finally:
        await client.aclose()


def test_timeout_defaults_and_bad_values():
    client = McpClient("fake", {"command": "noop", "connect_timeout": "abc", "call_timeout": -1})
    assert client.connect_timeout == 30
    assert client.call_timeout == 120
    assert client.shutdown_timeout == 5


def test_http_transport_resolves_the_2_0_symbol_and_passes_headers(monkeypatch):
    """``streamable_http_client`` (2.0) is used, and ``headers`` reach the
    httpx client it is handed; the client is returned so the actor owns it."""
    import mcp.client.streamable_http as http_mod

    from fluid.tools.mcp.client import _open_http_transport

    assert hasattr(http_mod, "streamable_http_client") or hasattr(
        http_mod, "streamablehttp_client"
    )
    calls: dict[str, Any] = {}

    def fake_connect(url, *, http_client=None, terminate_on_close=True):
        calls["url"] = url
        calls["http_client"] = http_client
        return "cm"

    def fake_factory(headers=None, timeout=None, auth=None):
        calls["factory_headers"] = headers
        return {"client": headers}

    monkeypatch.setattr(http_mod, "streamable_http_client", fake_connect, raising=False)
    monkeypatch.setattr(http_mod, "create_mcp_http_client", fake_factory, raising=False)

    cm, owned = _open_http_transport(
        {"url": "https://x/mcp", "headers": {"Authorization": "Bearer t"}}
    )
    assert cm == "cm" and calls["url"] == "https://x/mcp"
    assert calls["factory_headers"] == {"Authorization": "Bearer t"}
    assert calls["http_client"] == {"client": {"Authorization": "Bearer t"}}
    assert owned is calls["http_client"]

    calls.clear()
    cm, owned = _open_http_transport({"url": "https://x/mcp"})
    assert calls["http_client"] is None and owned is None


def test_http_transport_falls_back_to_the_1_x_symbol(monkeypatch):
    import mcp.client.streamable_http as http_mod

    from fluid.tools.mcp.client import _open_http_transport

    calls: dict[str, Any] = {}

    def legacy_connect(url, headers=None, timeout=30, sse_read_timeout=300):
        calls["url"] = url
        calls["headers"] = headers
        return "legacy-cm"

    monkeypatch.delattr(http_mod, "streamable_http_client", raising=False)
    monkeypatch.setattr(http_mod, "streamablehttp_client", legacy_connect, raising=False)

    cm, owned = _open_http_transport({"url": "https://x/mcp", "headers": {"A": "b"}})
    assert cm == "legacy-cm" and owned is None
    assert calls == {"url": "https://x/mcp", "headers": {"A": "b"}}
