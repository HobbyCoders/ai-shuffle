"""Tests for the shared ``rate_limit`` helpers in app/core/query_engine.py:

- ``_rate_limit_event_dict``: builds the WS frame emitted by BOTH streaming paths
  (interactive ``stream_query`` and offloaded ``stream_to_websocket``), stamped
  with this install's ``instance_id``.
- ``_apply_rate_limit_live_event``: persists the live refinement into the shared
  usage snapshot via ``usage_limits_monitor.apply_live_event_and_broadcast`` — the
  only cross-install propagation channel — and never raises out.

Both streaming sites now delegate to these helpers, so covering them here covers
both sites' rate_limit behaviour without a full SDK streaming harness.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.core.query_engine import _apply_rate_limit_live_event, _rate_limit_event_dict


def _rli(**kwargs):
    """Duck-typed stand-in for the SDK's RateLimitInfo."""
    base = {
        "status": "allowed",
        "rate_limit_type": "five_hour",
        "resets_at": "2026-07-09T05:00:00+00:00",
        "utilization": 0.42,
        "overage_status": None,
        "overage_resets_at": None,
        "overage_disabled_reason": None,
    }
    base.update(kwargs)
    return SimpleNamespace(**base)


class TestRateLimitEventDict:
    def test_builds_frame_with_instance_id(self):
        from app.core.config import settings

        frame = _rate_limit_event_dict(_rli(), "sess-1")
        assert frame["type"] == "rate_limit"
        assert frame["status"] == "allowed"
        assert frame["rate_limit_type"] == "five_hour"
        assert frame["resets_at"] == "2026-07-09T05:00:00+00:00"
        assert frame["utilization"] == 0.42
        assert frame["session_id"] == "sess-1"
        # Stamped with THIS install so the frontend attributes it per-account.
        assert frame["instance_id"] == settings.effective_instance_id

    def test_carries_overage_fields_when_present(self):
        frame = _rate_limit_event_dict(
            _rli(overage_status="active", overage_resets_at="x", overage_disabled_reason="y"),
            "sess-2",
        )
        assert frame["overage_status"] == "active"
        assert frame["overage_resets_at"] == "x"
        assert frame["overage_disabled_reason"] == "y"


class TestApplyRateLimitLiveEvent:
    @pytest.mark.asyncio
    async def test_forwards_to_monitor(self):
        mon = SimpleNamespace(apply_live_event_and_broadcast=AsyncMock())
        with patch("app.core.usage_limits.usage_limits_monitor", mon):
            await _apply_rate_limit_live_event(_rli(utilization=0.5))
        mon.apply_live_event_and_broadcast.assert_awaited_once_with(
            "five_hour", 0.5, "2026-07-09T05:00:00+00:00", status="allowed",
        )

    @pytest.mark.asyncio
    async def test_forwards_rejected_verdict_without_utilization(self):
        mon = SimpleNamespace(apply_live_event_and_broadcast=AsyncMock())
        with patch("app.core.usage_limits.usage_limits_monitor", mon):
            await _apply_rate_limit_live_event(_rli(status="rejected", utilization=None))
        mon.apply_live_event_and_broadcast.assert_awaited_once_with(
            "five_hour", None, "2026-07-09T05:00:00+00:00", status="rejected",
        )

    @pytest.mark.asyncio
    async def test_never_raises_when_monitor_fails(self):
        mon = SimpleNamespace(
            apply_live_event_and_broadcast=AsyncMock(side_effect=RuntimeError("boom"))
        )
        with patch("app.core.usage_limits.usage_limits_monitor", mon):
            # Must swallow the error so a DB/monitor hiccup can't break streaming.
            await _apply_rate_limit_live_event(_rli())
