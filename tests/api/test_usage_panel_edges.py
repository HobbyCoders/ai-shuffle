"""Edge payloads for the usage panel's data path.

provider fetcher -> normaliser -> ``provider_usage_snapshots`` -> GET
/api/v1/usage/limits. No recorded provider responses exist in the repo, so the
payloads below are built from the field names the normalisers read (Codex
``x-codex-*`` headers, OpenRouter ``/api/v1/key``, SuperGrok
``/v1/billing?format=credits``, fal ``/v1/account/billing``) and from the CLI's
``rate_limit_info`` fields for Claude.

Percent semantics under test: provider windows carry percent USED (0-100);
Claude windows carry percent REMAINING (0-100, converted from the CLI's used
fraction).
"""

import asyncio
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.auth import require_auth
from app.api.usage import router
from app.core import provider_usage
from app.core.provider_usage import (
    _openrouter_limit_resets_at,
    _openrouter_windows_and_credits,
    normalize_codex_headers,
    normalize_fal_billing,
    normalize_grok_billing,
)
from app.core.usage_limits import UsageLimitsMonitor

_CLAUDE = {
    "windows": [], "fetched_at": None, "available": False, "generation": 1,
    "snapshots": [], "self_instance_id": "local-1",
}


@pytest.fixture(autouse=True)
def idle_provider_poll():
    with patch("app.core.provider_usage.provider_usage_monitor") as monitor:
        monitor.refreshing = False
        yield monitor


def _get(path="/api/v1/usage/limits", now=None):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_auth] = lambda: "admin"
    clock = SimpleNamespace(time=lambda: now) if now is not None else time
    with patch("app.api.usage.usage_limits_monitor") as mon, patch("app.api.usage.time", clock):
        mon.get_cluster_snapshot = AsyncMock(return_value=dict(_CLAUDE))
        response = TestClient(app).get(path)
    assert response.status_code == 200
    return response.json()


def _providers(body):
    return {p["provider_id"]: p for p in body["providers"]}


# ---------------------------------------------------------------------------
# Normalisers
# ---------------------------------------------------------------------------


class TestCodexHeaders:
    def _headers(self, primary, secondary):
        return {
            "x-codex-plan-type": "plus",
            "x-codex-primary-used-percent": primary,
            "x-codex-primary-window-minutes": "300",
            "x-codex-primary-reset-at": "1790000000",
            "x-codex-secondary-used-percent": secondary,
            "x-codex-secondary-window-minutes": "10080",
            "x-codex-secondary-reset-after-seconds": "3600",
        }

    def test_zero_and_full_windows_keep_used_percent(self):
        u = normalize_codex_headers(self._headers("0", "100"))
        windows = {w["type"]: w for w in u.windows}
        assert windows["five_hour"]["utilization"] == 0.0
        assert windows["seven_day"]["utilization"] == 100.0
        # Absolute reset passes through; relative reset becomes epoch seconds.
        assert windows["five_hour"]["resets_at"] == "1790000000"
        assert abs(int(windows["seven_day"]["resets_at"]) - (time.time() + 3600)) < 5

    def test_window_without_percent_or_minutes_is_omitted(self):
        headers = self._headers("40", "10")
        del headers["x-codex-secondary-used-percent"]
        headers["x-codex-primary-window-minutes"] = "0"
        assert normalize_codex_headers(headers).windows == []


class TestOpenRouterKey:
    def test_period_limit_uses_limit_remaining_not_lifetime_usage(self):
        # Lifetime usage ($50) is far above the $10 monthly limit; this period
        # has used $2 of it. The bar must say 20%, not a clamped 100%.
        windows, credits = _openrouter_windows_and_credits(
            {"limit": 10.0, "limit_remaining": 8.0, "limit_reset": "monthly", "usage": 50.0}
        )
        assert windows[0]["utilization"] == pytest.approx(20.0)
        assert credits["usage"] == 50.0

    @pytest.mark.parametrize("remaining,expected", [(10.0, 0.0), (0.0, 100.0), (-1.0, 100.0)])
    def test_edges_clamp(self, remaining, expected):
        windows, _ = _openrouter_windows_and_credits({"limit": 10.0, "limit_remaining": remaining})
        assert windows[0]["utilization"] == expected

    def test_no_limit_has_no_window(self):
        assert _openrouter_windows_and_credits({"limit": None, "usage": 3.0})[0] == []

    @pytest.mark.parametrize("cadence,now,expected", [
        # Wed 2026-09-23 15:30 UTC.
        ("daily", "2026-09-23T15:30:00", "2026-09-24T00:00:00"),
        ("weekly", "2026-09-23T15:30:00", "2026-09-28T00:00:00"),
        ("weekly", "2026-09-28T00:00:00", "2026-10-05T00:00:00"),
        ("monthly", "2026-09-23T15:30:00", "2026-10-01T00:00:00"),
        ("monthly", "2026-12-31T23:59:00", "2027-01-01T00:00:00"),
        ("MONTHLY", "2026-02-10T00:00:00", "2026-03-01T00:00:00"),
    ])
    def test_reset_cadence_is_next_utc_boundary(self, cadence, now, expected):
        at = datetime.fromisoformat(now).replace(tzinfo=timezone.utc).timestamp()
        want = datetime.fromisoformat(expected).replace(tzinfo=timezone.utc).timestamp()
        assert _openrouter_limit_resets_at(cadence, now=at) == str(int(want))

    @pytest.mark.parametrize("cadence", [None, "", "yearly", 7])
    def test_no_or_unknown_cadence_has_no_reset(self, cadence):
        assert _openrouter_limit_resets_at(cadence) is None


class TestGrokBilling:
    def test_zero_percent_is_a_reading(self):
        u = normalize_grok_billing({"config": {"creditUsagePercent": 0,
                                               "currentPeriod": {"end": "2026-10-01T00:00:00Z"}}})
        assert u.windows == [{"type": "seven_day", "utilization": 0.0, "resets_at": "2026-10-01T00:00:00Z"}]

    def test_over_limit_clamps_to_100(self):
        u = normalize_grok_billing({"creditUsagePercent": 130})
        assert u.windows[0]["utilization"] == 100.0

    def test_null_percent_without_period_has_no_window(self):
        assert normalize_grok_billing({"config": {"creditUsagePercent": None}}).windows == []


def test_fal_zero_balance_is_a_balance_not_missing():
    assert normalize_fal_billing({"credits": {"current_balance": 0, "currency": "USD"}}) == {
        "balance_remaining": 0.0, "currency": "USD",
    }
    assert normalize_fal_billing({"credits": {"current_balance": None}}) is None


# ---------------------------------------------------------------------------
# Codex capture keeps the last full reading
# ---------------------------------------------------------------------------


class TestCodexCapturePersistence:
    def _seed(self):
        from app.db import database as db

        db.create_model_provider(provider_id="cx", name="ChatGPT", provider_type="chatgpt",
                                 base_url="https://chatgpt.com")
        db.upsert_provider_usage_snapshot(
            "cx", "chatgpt", "plus",
            [{"type": "five_hour", "utilization": 42.0, "resets_at": "1790000000"}],
            None, {}, int(time.time() * 1000) - 60_000,
        )

    def test_capture_without_window_headers_keeps_windows_and_plan(self):
        from app.db import database as db

        self._seed()
        # An error response carrying only the credits headers.
        asyncio.run(provider_usage._persist_codex_capture(
            "cx", {"x-codex-credits-has-credits": "false", "x-codex-credits-balance": "0"},
        ))
        row = next(r for r in db.get_all_provider_usage_snapshots() if r["provider_id"] == "cx")
        assert row["windows"] == [{"type": "five_hour", "utilization": 42.0, "resets_at": "1790000000"}]
        assert row["plan_type"] == "plus"
        # R9-14: the carried windows keep the reading's own time, not "now".
        assert row["fetched_at"] < int(time.time() * 1000) - 50_000

    def test_carried_windows_go_stale_per_the_usual_interval(self):
        """R9-14: a stream of window-less captures kept stamping the old
        windows as just fetched, so an hours-old quota read "fresh"."""
        from app.db import database as db

        db.create_model_provider(provider_id="cx", name="ChatGPT", provider_type="chatgpt",
                                 base_url="https://chatgpt.com")
        old_ms = int(time.time() * 1000) - 3_600_000
        db.upsert_provider_usage_snapshot(
            "cx", "chatgpt", "plus",
            [{"type": "five_hour", "utilization": 42.0, "resets_at": "1790000000"}],
            None, {}, old_ms,
        )
        asyncio.run(provider_usage._persist_codex_capture(
            "cx", {"x-codex-credits-has-credits": "false", "x-codex-credits-balance": "0"},
        ))
        entry = _providers(_get())["cx"]
        assert entry["windows"][0]["utilization"] == 42.0
        assert entry["fetched_at"] == old_ms
        assert entry["status"] == "stale"

    def test_capture_with_windows_replaces_them(self):
        from app.db import database as db

        self._seed()
        asyncio.run(provider_usage._persist_codex_capture("cx", {
            "x-codex-primary-used-percent": "5", "x-codex-primary-window-minutes": "300",
        }))
        row = next(r for r in db.get_all_provider_usage_snapshots() if r["provider_id"] == "cx")
        assert row["windows"] == [{"type": "five_hour", "utilization": 5.0, "resets_at": None}]
        assert row["plan_type"] == "plus"


# ---------------------------------------------------------------------------
# Route: status, cooldown and isolation with a frozen clock
# ---------------------------------------------------------------------------


class TestRouteStatusAndCooldown:
    NOW = 1_790_000_000.0

    def _grok(self, fetched_ms, windows=None):
        from app.db import database as db

        db.create_model_provider(provider_id="gk", name="grok", provider_type="grok",
                                 base_url="https://example.test")
        db.upsert_provider_usage_snapshot("gk", "grok", "SuperGrok",
                                          windows or [{"type": "seven_day", "utilization": 100.0}],
                                          None, {}, fetched_ms)

    def test_fresh_then_stale_and_cooldown_clears(self):
        now_ms = int(self.NOW * 1000)
        self._grok(now_ms - 10_000)
        entry = _providers(_get(now=self.NOW))["gk"]
        assert entry["status"] == "fresh"
        assert entry["cooldown_until"] == now_ms - 10_000 + 300_000
        # Exactly at the interval the cooldown is over and the row is not stale yet.
        entry = _providers(_get(now=self.NOW + 290))["gk"]
        assert entry["cooldown_until"] is None and entry["status"] == "fresh"
        entry = _providers(_get(now=self.NOW + 291))["gk"]
        assert entry["status"] == "stale" and entry["cooldown_until"] is None
        # 100% used survives the round trip unchanged (percent USED).
        assert entry["windows"][0]["utilization"] == 100.0

    def test_failed_attempt_keeps_last_reading_and_reports_error(self):
        from app.db.provider_usage_snapshots import record_provider_usage_failure

        self._grok(int(time.time() * 1000) - 20_000)
        record_provider_usage_failure("gk", "grok", "timeout")
        entry = _providers(_get())["gk"]
        assert entry["status"] == "stale" and entry["last_error_kind"] == "timeout"
        assert entry["windows"][0]["utilization"] == 100.0
        assert entry["cooldown_until"] > entry["last_attempt_at"] > entry["last_success_at"]

    def test_refused_capability_has_no_cooldown(self):
        from app.db.provider_usage_snapshots import record_provider_usage_failure

        self._grok(int(time.time() * 1000))
        record_provider_usage_failure("gk", "grok", "billing_access_denied")
        entry = _providers(_get())["gk"]
        assert entry["cooldown_until"] is None
        assert entry["last_error_kind"] == "billing_access_denied"

    def test_reset_in_the_past_is_served_verbatim(self):
        # The client decides rollover against its own clock; the route must not
        # drop or rewrite a past reset.
        self._grok(int(time.time() * 1000), [{"type": "seven_day", "utilization": 80.0,
                                              "resets_at": 1_600_000_000}])
        entry = _providers(_get())["gk"]
        assert entry["windows"][0]["resets_at"] == "1600000000"

    def test_one_unreadable_row_does_not_hide_the_others(self):
        from app.db import database as db

        self._grok(int(time.time() * 1000))
        db.create_model_provider(provider_id="or", name="router", provider_type="openrouter",
                                 base_url="https://openrouter.ai/api/v1")
        rows = db.get_all_provider_usage_snapshots()
        # A row whose plan type is not a string fails the response model.
        rows.append({"provider_id": "or", "provider_type": "openrouter", "plan_type": {"bad": 1},
                     "windows": [], "credits": {"usage": 1.0}, "raw": {}, "fetched_at": 1})
        with patch("app.db.database.get_all_provider_usage_snapshots", return_value=rows):
            body = _get()
        assert body["providers"] is not None
        assert set(_providers(body)) == {"gk"}

    def test_one_unreadable_window_drops_only_that_window(self):
        self._grok(int(time.time() * 1000), [
            {"type": "seven_day", "utilization": "n/a"},
            {"utilization": 3.0},
            {"type": "grok_chat", "utilization": 12.5, "resets_at": None},
        ])
        entry = _providers(_get())["gk"]
        assert entry["windows"] == [{"type": "grok_chat", "utilization": 12.5, "resets_at": None}]


# ---------------------------------------------------------------------------
# Claude live events (used fraction in, remaining percent out)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("used,remaining", [(0.0, 100.0), (1.0, 0.0), (1.4, 0.0), (0.004, 99.6), (-0.2, 100.0)])
def test_claude_live_event_edges(used, remaining):
    monitor = UsageLimitsMonitor()
    snapshot = monitor.apply_live_event("five_hour", used, 1_790_000_000)
    window = snapshot["windows"][0]
    assert window["utilization"] == pytest.approx(remaining)
    assert window["resets_at"] == 1_790_000_000
    assert isinstance(window["observed_at"], int)


def test_claude_event_without_utilization_or_type_is_ignored():
    monitor = UsageLimitsMonitor()
    assert monitor.apply_live_event("five_hour", None, 1) is None
    assert monitor.apply_live_event(None, 0.5, 1) is None
    assert monitor.apply_live_event("five_hour", float("nan"), 1) is None
    assert monitor._windows == []


@pytest.mark.parametrize("used", [None, 0.3, 1.0])
def test_claude_rejected_event_reads_exhausted(used):
    """The CLI cut the window off: 100% used / 0% left even with no utilization."""
    monitor = UsageLimitsMonitor()
    snapshot = monitor.apply_live_event("five_hour", used, 1_790_000_000, status="rejected")
    window = snapshot["windows"][0]
    assert window["utilization"] == 0.0
    assert window["resets_at"] == 1_790_000_000
    assert isinstance(window["observed_at"], int)

async def test_official_openai_model_account_is_explicitly_unavailable_before_any_poll(mock_db):
    from app.db import database as db
    from app.api.usage import _load_provider_usage

    db.create_model_provider("new-api", "OpenAI API", "openai", "https://api.openai.com/v1")
    db.create_model_provider("custom-api", "Private proxy", "openai", "https://proxy.example/v1")
    db.create_model_provider("disabled-api", "Disabled API", "openai", "https://api.openai.com/v1")
    db.set_model_provider_enabled("disabled-api", False)
    entries = await _load_provider_usage()
    assert len(entries) == 1
    assert entries[0].provider_id == "new-api"
    assert entries[0].status == "unavailable"
    assert entries[0].last_error_kind == "unsupported"
    assert entries[0].credits is None and entries[0].windows == []
