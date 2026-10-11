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
    description = handlers["GetUsage"].description
    assert "Reports percent left (used_percent is 100 minus left)" in description
    assert "as shown in the Usage UI" not in description
    assert "Quote percentages as 'left'" in description
    assert "subscription rows in the Usage popover show the same left number" in description
    assert "other API-key provider windows and Settings meters currently display used" in description
    assert "used_percent is 100 minus left" in description
    assert "Reports used and remaining percentages" not in description
    assert "reset timestamps, source, freshness, and unavailable data explicitly" in description
    assert "Read-only: it never refreshes providers or reads credentials." in description
    # Measured description fixture plus a small allowance.
    assert len(description) <= 433 + 20
    paragraphs = description.split("\n\n")
    assert len(paragraphs) == len(set(paragraphs))
    assert description.count("Quote percentages as 'left'") == 1


def test_get_usage_returns_formatted_cached_payload():
    payload = {
        "available": True,
        "fetched_at": 2_000_000_000,
        "windows": [{"type": "five_hour", "utilization": 25}],
        "snapshots": [{
            "available": True,
            "windows": [{"type": "seven_day", "utilization": 80}],
        }],
        "providers": [{
            "provider_id": "test",
            "windows": [{"type": "requests", "utilization": 40}],
            "credits": {"balance_remaining": 4.5, "limit_remaining": 2.5, "limit": 10},
        }],
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
    for window, left in (
        (body["windows"][0], 25),
        (body["accounts"][0]["windows"][0], 80),
        (body["providers"][0]["windows"][0], 60),
    ):
        assert list(window)[:4] == ["type", "left_percent", "remaining_percent", "used_percent"]
        assert window["left_percent"] == window["remaining_percent"] == left
        assert window["used_percent"] == 100 - left
    assert body["providers"][0]["credits"] == {
        "balance_remaining": 4.5, "limit_remaining": 2.5, "limit": 10,
    }
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
