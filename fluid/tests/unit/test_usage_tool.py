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


def test_unread_provider_rows_are_reported_unavailable_not_empty(monkeypatch) -> None:
    import fluid.tools.builtins.usage as usage

    monkeypatch.setattr(usage, "_last_providers", None, raising=False)
    out = _payload({"available": False, "fetched_at": None, "windows": [], "providers": None})
    assert out["providers"] == []
    assert out["providers_unavailable"] is True
    assert "temporarily unavailable" in out["providers_reason"]
    assert "does not mean no providers" in out["providers_reason"]
    assert "temporarily unavailable" in out["reason"]


def test_unread_provider_rows_keep_the_last_known_values(monkeypatch) -> None:
    import fluid.tools.builtins.usage as usage

    monkeypatch.setattr(usage, "_last_providers", None, raising=False)
    good = _payload({
        "available": False, "fetched_at": None, "windows": [],
        "providers": [{
            "provider_id": "codex", "status": "stale", "fetched_at": 2_000_000_000_000,
            "windows": [{"type": "requests", "utilization": 40, "resets_at": "p"}],
        }],
    })
    assert "providers_unavailable" not in good
    out = _payload({"available": False, "fetched_at": None, "windows": [], "providers": None})
    assert out["providers"][0]["provider_id"] == "codex"
    assert out["providers"][0]["windows"] == good["providers"][0]["windows"]
    assert out["providers"][0]["windows"][0]["remaining_percent"] == 60
    assert out["available"] is True
    assert out["providers_unavailable"] is True
    assert "last values read" in out["providers_reason"]


def test_replayed_provider_rows_are_not_reported_fresh(monkeypatch) -> None:
    """R6-6: rows the host just failed to read are last-known, not fresh."""
    import fluid.tools.builtins.usage as usage

    monkeypatch.setattr(usage, "_last_providers", None, raising=False)
    now_ms = time.time() * 1000
    good = _payload({
        "available": False, "fetched_at": None, "windows": [],
        "providers": [{
            "provider_id": "codex", "fetched_at": now_ms,
            "windows": [{"type": "requests", "utilization": 40, "resets_at": None}],
        }],
    })
    assert good["providers"][0]["freshness"] == "fresh"
    assert good["providers"][0]["status"] == "fresh"
    assert good["freshness"] == "fresh"

    out = _payload({"available": False, "fetched_at": None, "windows": [], "providers": None})
    (replayed,) = out["providers"]
    assert replayed["freshness"] == "stale"
    assert replayed["status"] == "stale"
    assert replayed["source"] == "last_known_provider_snapshot"
    assert replayed["last_known"] is True
    assert replayed["fetched_at"] == now_ms
    assert out["freshness"] == "stale"
    # The cache itself keeps the values as read.
    assert usage._last_providers[0]["freshness"] == "fresh"


def test_an_empty_provider_list_still_means_none_configured(monkeypatch) -> None:
    import fluid.tools.builtins.usage as usage

    monkeypatch.setattr(usage, "_last_providers", [{"provider_id": "old"}], raising=False)
    out = _payload({"available": False, "fetched_at": None, "windows": [], "providers": []})
    assert out["providers"] == []
    assert "providers_unavailable" not in out
    assert out["reason"] == "no cached subscription usage is available"


def test_claude_freshness_follows_the_oldest_window_like_the_panel() -> None:
    """One CLI event dates one window. The panel shows "Updated 20m ago" for a
    bucket whose other window was last reported 20 minutes ago; the tool must
    not call the same data fresh because ``fetched_at`` moved."""
    now = time.time()
    out = _payload({
        "available": True,
        "fetched_at": now - 5,
        "windows": [
            {"type": "five_hour", "utilization": 60, "observed_at": int((now - 5) * 1000)},
            {"type": "seven_day", "utilization": 90, "observed_at": int((now - 1200) * 1000)},
        ],
        "snapshots": [{
            "instance_id": "a", "available": True, "fetched_at": int((now - 5) * 1000),
            "windows": [{"type": "seven_day", "utilization": 90, "observed_at": int((now - 1200) * 1000)}],
        }],
    })
    assert out["claude_freshness"] == "stale"
    assert out["freshness"] == "stale"
    assert out["accounts"][0]["freshness"] == "stale"
    assert out["windows"][1]["observed_at"] == int((now - 1200) * 1000)
    # Windows persisted before per-window dating fall back to fetched_at.
    legacy = _payload({"available": True, "fetched_at": now - 5,
                       "windows": [{"type": "five_hour", "utilization": 60}]})
    assert legacy["claude_freshness"] == "fresh"
    assert "observed_at" not in legacy["windows"][0]


def test_zone_less_reset_is_read_as_utc(monkeypatch) -> None:
    """A zone-less ISO reset is UTC; reading it as host-local time would keep a
    window "not yet reset" for hours on a host west of UTC."""
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    time.tzset()
    try:
        passed = datetime.fromtimestamp(time.time() - 60, timezone.utc).replace(tzinfo=None)
        pending = datetime.fromtimestamp(time.time() + 600, timezone.utc).replace(tzinfo=None)
        out = _payload({"available": True, "fetched_at": time.time(), "windows": [
            {"type": "five_hour", "utilization": 10, "resets_at": passed.isoformat()},
            {"type": "seven_day", "utilization": 10, "resets_at": pending.isoformat()},
        ]})
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()
    rolled, live = out["windows"]
    assert rolled.get("rolled_over") is True and rolled["remaining_percent"] == 100.0
    assert "rolled_over" not in live and live["remaining_percent"] == 10.0


@pytest.mark.parametrize("utilization,used,remaining", [(0, 0.0, 100.0), (100, 100.0, 0.0), (130, 100.0, 0.0)])
def test_provider_edges_match_the_panel(utilization, used, remaining, monkeypatch) -> None:
    import fluid.tools.builtins.usage as usage

    monkeypatch.setattr(usage, "_last_providers", None, raising=False)
    out = _payload({"available": False, "fetched_at": None, "windows": [], "providers": [{
        "provider_id": "cx", "status": "fresh", "fetched_at": time.time() * 1000,
        "windows": [{"type": "five_hour", "utilization": utilization, "resets_at": None}],
    }]})
    window = out["providers"][0]["windows"][0]
    assert (window["used_percent"], window["remaining_percent"]) == (used, remaining)


def test_provider_credits_carry_the_figures_the_panel_shows(monkeypatch) -> None:
    import fluid.tools.builtins.usage as usage

    monkeypatch.setattr(usage, "_last_providers", None, raising=False)
    out = _payload({"available": False, "fetched_at": None, "windows": [], "providers": [{
        "provider_id": "or", "status": "fresh", "fetched_at": time.time() * 1000,
        "credits": {"balance_remaining": 4.5, "total_usage": 5.5, "total_credits": 10,
                    "currency": "USD", "secret": "x"},
    }]})
    assert out["providers"][0]["credits"] == {
        "balance_remaining": 4.5, "total_usage": 5.5, "total_credits": 10, "currency": "USD",
    }
