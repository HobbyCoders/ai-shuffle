"""M2 — MCP 2.0 client against the in-repo fake stdio server.

Covers connect → list/search/load/dispatch, the ttlMs list cache, and the MRTR
``input_required`` round-trip surfacing as a structured marker.
"""

import sys
from pathlib import Path

from fluid.tools.registry import InputRequired, ToolCtx, ToolRegistry

FIXTURE = str(Path(__file__).parent.parent / "fixtures" / "fake_mcp_server.py")


def _config(ttl_ms: int | None = None) -> dict:
    env = dict(__import__("os").environ)
    if ttl_ms is not None:
        env["FLUID_FAKE_TTL"] = str(ttl_ms)
    return {"transport": "stdio", "command": sys.executable, "args": [FIXTURE], "env": env}


async def test_connect_list_search_load_dispatch() -> None:
    reg = ToolRegistry()
    try:
        specs = await reg.connect_mcp("srv", _config())
        names = {s.name for s in specs}
        assert names == {"mcp__srv__get_weather", "mcp__srv__write_note", "mcp__srv__need_input"}

        # Progressive disclosure: MCP schemas are known but not loaded yet.
        assert reg.list_tools(loaded_only=True) == []
        # ...but searchable across the full catalog.
        found = reg.search_tools("weather")
        assert [s.name for s in found] == ["mcp__srv__get_weather"]

        loaded = reg.load_schema("mcp__srv__get_weather")
        assert loaded.input_schema["required"] == ["city"]
        assert [s.name for s in reg.list_tools(loaded_only=True)] == ["mcp__srv__get_weather"]

        # read-only annotation → safe; unannotated write tool → write
        assert reg.get("mcp__srv__get_weather").risk == "safe"
        assert reg.get("mcp__srv__write_note").risk == "write"

        out = await reg.dispatch("mcp__srv__get_weather", {"city": "Paris"}, ToolCtx())
        assert out.text == "Paris: sunny, 21C"
        assert not out.is_error
    finally:
        await reg.aclose()


async def test_ttl_list_cache() -> None:
    reg = ToolRegistry()
    try:
        await reg.connect_mcp("srv", _config(ttl_ms=60000))
        client = reg._manager.client("srv")
        # connect already did one fetch; a repeat within TTL is served from cache.
        assert client.list_fetches == 1
        await reg._manager.refresh("srv")
        assert client.list_fetches == 1  # cache hit, no round-trip
        # Handshake facts depend on the installed SDK generation: a 2.0 SDK
        # speaks server/discover (legacy False, 2026-07-28); a 1.x SDK falls
        # back to initialize with its own latest version.
        from mcp.client.session import ClientSession

        modern_sdk = hasattr(ClientSession, "discover")
        assert client.legacy is (not modern_sdk)
        if modern_sdk:
            assert client.protocol == "2026-07-28"
        else:
            assert client.protocol  # some version was negotiated
    finally:
        await reg.aclose()


async def test_legacy_no_ttl_refetches() -> None:
    reg = ToolRegistry()
    try:
        await reg.connect_mcp("srv", _config(ttl_ms=None))  # no cache hint
        client = reg._manager.client("srv")
        assert client.list_fetches == 1
        await reg._manager.refresh("srv")
        assert client.list_fetches == 2  # nothing cached without a TTL
    finally:
        await reg.aclose()


async def test_mrtr_input_required_surfaces_marker() -> None:
    reg = ToolRegistry()
    try:
        await reg.connect_mcp("srv", _config())
        out = await reg.dispatch("mcp__srv__need_input", {}, ToolCtx())
        assert out.input_required is not None
        marker = out.input_required
        assert isinstance(marker, InputRequired)
        assert marker.server == "srv"
        assert marker.tool == "need_input"
        assert marker.request_state == "rs-1"
        assert "confirm" in marker.requests
        assert marker.requests["confirm"]["method"] == "elicitation/create"
    finally:
        await reg.aclose()
