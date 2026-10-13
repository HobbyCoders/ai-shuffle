"""GetUsage normalization edges: junk percentages and stamps, rollover,
freshness units, snapshot model objects, and a failing host callback."""

from __future__ import annotations

import json
import time

import pytest

from fluid.tools.builtins import usage
from fluid.tools.builtins.usage import (
    _epoch_seconds,
    _freshness,
    _oldest_observation,
    _payload,
    _percent,
    _reset_passed,
    _window,
    handler,
)
from fluid.tools.registry import ToolCtx


@pytest.fixture(autouse=True)
def _reset_last_providers(monkeypatch):
    monkeypatch.setattr(usage, "_last_providers", None)


@pytest.mark.parametrize("value, expected", [
    ("12.345", 12.35), (-5, 0.0), (250, 100.0),
    ("abc", None), (None, None), (float("nan"), None), (float("inf"), None),
])
def test_percent_clamps_and_rejects(value, expected) -> None:
    assert _percent(value) == expected


def test_reset_passed_accepts_seconds_ms_and_iso() -> None:
    now = time.time()
    assert _reset_passed(None) is False and _reset_passed(True) is False
    assert _reset_passed(now - 10) is True
    assert _reset_passed((now - 10) * 1000) is True
    assert _reset_passed(now + 3600) is False
    assert _reset_passed("2001-01-01T00:00:00Z") is True
    assert _reset_passed("2001-01-01T00:00:00") is True  # zone-less reads as UTC
    assert _reset_passed("2999-01-01T00:00:00+00:00") is False
    assert _reset_passed("not a date") is False


def test_window_rejects_junk_and_marks_rollover() -> None:
    assert _window("x", source="s", remaining_input=True) is None
    assert _window({"utilization": "n/a"}, source="s", remaining_input=True) is None
    rolled = _window(
        {"utilization": 30, "resets_at": time.time() - 5, "observed_at": 1_700_000_000},
        source="s", remaining_input=False,
    )
    assert rolled["rolled_over"] is True and rolled["remaining_percent"] == 100.0
    assert rolled["type"] == "unknown" and rolled["observed_at"] == 1_700_000_000


@pytest.mark.parametrize("value, expected", [
    (None, None), (False, None), ("x", None), (0, None), (-3, None),
    (float("nan"), None), (1_700_000_000, 1_700_000_000), (1_700_000_000_000, 1_700_000_000),
])
def test_epoch_seconds(value, expected) -> None:
    assert _epoch_seconds(value) == expected


def test_oldest_observation_prefers_per_window_stamps() -> None:
    windows = [{"observed_at": 1_000_000_100}, {"x": 1}, "junk"]
    assert _oldest_observation(1_000_000_500, windows) == 1_000_000_100
    assert _oldest_observation("unparseable", []) == "unparseable"
    # A real window without its own stamp makes the age unknown (#1661).
    assert _oldest_observation(1_000_000_500, [*windows, {"utilization": 40}]) is None


def test_freshness_units_and_junk() -> None:
    assert _freshness(time.time(), available=False) == "unavailable"
    assert _freshness(None) == "unknown"
    assert _freshness("garbage") == "unknown"
    assert _freshness(time.time() * 1000) == "fresh"
    assert _freshness(time.time() - 3600) == "stale"


def test_payload_accepts_model_objects_and_rejects_non_dicts() -> None:
    class Snapshot:
        def model_dump(self, mode):
            assert mode == "json"
            return {"available": False, "snapshots": ["junk", {
                "instance_id": "i1", "available": True, "fetched_at": time.time(),
                "windows": [{"type": "five_hour", "utilization": 10}],
            }]}

    result = _payload(Snapshot())
    assert result["claude_available"] is True  # an account reported availability
    # Claude windows report REMAINING in "utilization" (remaining_input=True).
    assert result["accounts"][0]["windows"][0]["remaining_percent"] == 10.0
    with pytest.raises(TypeError):
        _payload(["not", "a", "dict"])


def test_payload_provider_status_credits_and_unread_replay() -> None:
    first = _payload({"providers": ["junk", {
        "provider_id": "p", "status": "custom-status", "fetched_at": time.time(),
        "credits": {"balance": 5, "api_key": "never-copied"},
    }]})
    entry = first["providers"][0]
    assert entry["credits"] == {"balance": 5}
    assert entry["freshness"] == "fresh" and entry["status"] == "custom-status"

    replay = _payload({"providers": None})
    assert replay["providers_unavailable"] is True
    assert replay["providers"][0]["last_known"] is True
    assert replay["providers"][0]["freshness"] == "stale"
    assert "last values read" in replay["providers_reason"]


def test_payload_unread_without_history_explains_itself() -> None:
    result = _payload({"providers": None})
    assert result["available"] is False
    assert "does not mean no providers" in result["providers_reason"]
    assert result["reason"].startswith("provider usage rows are temporarily unavailable")


async def test_handler_awaits_async_callbacks_and_contains_failures() -> None:
    async def snapshot():
        return {"available": True, "fetched_at": time.time(), "windows": []}

    ok = json.loads((await handler({}, ToolCtx(extra={"usage_snapshot": snapshot}))).text)
    assert ok["available"] is True and ok["freshness"] == "fresh"

    def broken():
        raise RuntimeError("host down")

    failed = json.loads((await handler({}, ToolCtx(extra={"usage_snapshot": broken}))).text)
    assert failed["available"] is False and failed["freshness"] == "unavailable"
