"""Delegated MCP exposure for the cache-only GetUsage tool."""

import asyncio
import json
from unittest.mock import AsyncMock, patch

import app.mcp.inproc as sdk


def _handlers(is_admin=True):
    from app.mcp import builtin_tools

    captured = {}
    real_create = sdk.create_tool_server

    def _spy(name, version, tools):
        captured["tools"] = tools
        return real_create(name=name, version=version, tools=tools)

    with patch.object(sdk, "create_tool_server", side_effect=_spy):
        builtin_tools.create_builtin_tools_server(session_id="usage-test", is_admin=is_admin)
    return {tool.name: tool for tool in captured["tools"]}


def test_get_usage_is_registered_and_allowlisted():
    handlers = _handlers()
    from app.mcp.builtin_tools import get_builtin_tool_names

    assert "GetUsage" in handlers
    assert "mcp__builtins__GetUsage" in get_builtin_tool_names()


def test_get_usage_returns_formatted_cached_payload():
    payload = {
        "available": True,
        "fetched_at": 2_000_000_000,
        "windows": [{"type": "five_hour", "utilization": 25}],
        "snapshots": [],
        "providers": [],
    }
    with patch(
        "app.api.usage.get_cached_usage_payload",
        new_callable=AsyncMock,
        return_value=payload,
    ) as get_cached:
        result = asyncio.run(_handlers()["GetUsage"].handler({}))

    body = json.loads(result["content"][0]["text"])
    assert body["success"] is True
    assert body["windows"][0]["used_percent"] == 75
    get_cached.assert_awaited_once()


def test_get_usage_is_refused_for_an_api_key_session():
    """Every snapshot row is the owner's account data; a key's session reads none."""
    from app.api.usage import API_KEY_USAGE_REASON

    with patch(
        "app.api.usage.get_cached_usage_payload",
        new_callable=AsyncMock,
        return_value={"available": True, "windows": [{"type": "five_hour", "utilization": 25}],
                      "providers": [{"provider_id": "owner-openrouter", "credits": {"balance": 99}}]},
    ) as get_cached:
        result = asyncio.run(_handlers(is_admin=False)["GetUsage"].handler({}))

    body = json.loads(result["content"][0]["text"])
    assert body["available"] is False and body["denied"] is True
    assert body["reason"] == API_KEY_USAGE_REASON
    assert body["windows"] == [] and body["providers"] == [] and body["accounts"] == []
    get_cached.assert_not_awaited()
