import json
import time
from datetime import datetime, timezone

import pytest

from fluid.tools.builtins import register_builtins
from fluid.tools.builtins.usage import _payload, handler
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


@pytest.mark.parametrize("resets_at", [
    lambda now: str(int(now) - 60),
    lambda now: int(now * 1000) - 60_000,
    lambda now: datetime.fromtimestamp(now - 60, timezone.utc).isoformat(),
])
def test_rolled_over_window_reports_fresh_quota(resets_at) -> None:
    out = _payload({
        "available": True,
        "fetched_at": time.time() - 3600,
        "windows": [{"type": "five_hour", "utilization": 3.0, "resets_at": resets_at(time.time())},
                    {"type": "seven_day", "utilization": 3.0, "resets_at": str(int(time.time()) + 600)}],
    })
    # The UI shows 100% left once resets_at passes; the tool must agree.
    rolled, live = out["windows"]
    assert rolled["remaining_percent"] == 100.0 and rolled["used_percent"] == 0.0
    assert rolled["rolled_over"] is True
    assert live["remaining_percent"] == 3.0 and "rolled_over" not in live
