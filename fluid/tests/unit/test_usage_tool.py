import json

import pytest

from fluid.tools.builtins import register_builtins
from fluid.tools.builtins.usage import handler
from fluid.tools.registry import ToolCtx, ToolRegistry


def test_get_usage_is_registered_for_native_and_code_mode_surfaces() -> None:
    registry = ToolRegistry()
    register_builtins(registry)
    spec = registry.get("GetUsage")
    assert spec is not None
    assert spec.risk == "safe"
    assert spec.code_mode_allowed is True


@pytest.mark.asyncio
async def test_get_usage_reports_unavailable_without_a_host() -> None:
    payload = json.loads((await handler({}, ToolCtx())).text)
    assert payload["available"] is False
    assert payload["freshness"] == "unavailable"
    assert payload["providers"] == []


@pytest.mark.asyncio
async def test_get_usage_reports_used_and_remaining_for_claude_and_provider() -> None:
    snapshot = {
        "available": True,
        "fetched_at": 2_000_000_000,
        "windows": [{"type": "five_hour", "utilization": 25, "resets_at": "r"}],
        "providers": [{
            "provider_id": "codex",
            "status": "fresh",
            "fetched_at": 2_000_000_000_000,
            "windows": [{"type": "requests", "utilization": 40, "resets_at": "p"}],
        }],
    }
    payload = json.loads((await handler(
        {}, ToolCtx(extra={"usage_snapshot": lambda: snapshot})
    )).text)
    assert payload["windows"][0]["remaining_percent"] == 25
    assert payload["windows"][0]["used_percent"] == 75
    assert payload["windows"][0]["resets_at"] == "r"
    assert payload["providers"][0]["windows"][0]["used_percent"] == 40
    assert payload["providers"][0]["windows"][0]["remaining_percent"] == 60


@pytest.mark.asyncio
async def test_provider_snapshot_can_be_available_when_claude_is_unavailable() -> None:
    snapshot = {
        "available": False,
        "fetched_at": None,
        "windows": [],
        "providers": [{"provider_id": "codex", "status": "stale", "fetched_at": 2_000_000_000_000}],
    }
    payload = json.loads((await handler(
        {}, ToolCtx(extra={"usage_snapshot": lambda: snapshot})
    )).text)
    assert payload["available"] is True
    assert payload["claude_available"] is False
    assert payload["freshness"] == "stale"
