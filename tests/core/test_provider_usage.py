"""Tests for the Codex usage-header normalizer + capture entrypoint
(app/core/provider_usage.py, Chunk U2).

Covers ``normalize_codex_headers`` (window-minutes mapping, resets_at
precedence, allowlist enforcement — a stray Authorization header must NEVER
reach ``raw``) and ``capture_codex_headers``/``_persist_codex_capture`` (fires
a fire-and-forget upsert; a failing DB write never raises out of the task).
"""

import asyncio
import time
from unittest.mock import patch

import pytest

from app.core import provider_usage


# ---------------------------------------------------------------------------
# normalize_codex_headers — window mapping
# ---------------------------------------------------------------------------

class TestWindowTypeMapping:
    def test_10080_minutes_is_seven_day(self):
        assert provider_usage._window_type_for_minutes(10080) == "seven_day"

    def test_300_minutes_is_five_hour(self):
        assert provider_usage._window_type_for_minutes(300) == "five_hour"

    def test_other_minutes_is_generic_window(self):
        assert provider_usage._window_type_for_minutes(42) == "window_42m"

    def test_none_minutes_is_none(self):
        assert provider_usage._window_type_for_minutes(None) is None

    def test_primary_and_secondary_windows_mapped(self):
        headers = {
            "x-codex-primary-used-percent": "12.5",
            "x-codex-primary-window-minutes": "10080",
            "x-codex-secondary-used-percent": "80",
            "x-codex-secondary-window-minutes": "300",
        }
        u = provider_usage.normalize_codex_headers(headers)
        types = {w["type"] for w in u.windows}
        assert types == {"seven_day", "five_hour"}
        seven_day = next(w for w in u.windows if w["type"] == "seven_day")
        assert seven_day["utilization"] == 12.5
        five_hour = next(w for w in u.windows if w["type"] == "five_hour")
        assert five_hour["utilization"] == 80.0

    def test_generic_fallback_window_included(self):
        headers = {
            "x-codex-primary-used-percent": "5",
            "x-codex-primary-window-minutes": "60",
        }
        u = provider_usage.normalize_codex_headers(headers)
        assert u.windows == [
            {"type": "window_60m", "utilization": 5.0, "resets_at": None}
        ]

    def test_missing_percent_or_minutes_omits_window(self):
        # No minutes header at all -> window omitted entirely.
        headers = {"x-codex-primary-used-percent": "5"}
        u = provider_usage.normalize_codex_headers(headers)
        assert u.windows == []

    def test_empty_headers_yields_empty_windows(self):
        u = provider_usage.normalize_codex_headers({})
        assert u.windows == []
        assert u.plan_type is None
        assert u.raw == {}


# ---------------------------------------------------------------------------
# _resets_at — absolute vs derived
# ---------------------------------------------------------------------------

class TestResetsAt:
    def test_absolute_reset_at_preferred(self):
        assert provider_usage._resets_at("2026-08-01T00:00:00Z", "9999") == (
            "2026-08-01T00:00:00Z"
        )

    def test_reset_after_seconds_derives_epoch(self):
        before = int(time.time())
        result = provider_usage._resets_at(None, "60")
        after = int(time.time())
        assert result is not None
        derived = int(result)
        assert before + 60 <= derived <= after + 60

    def test_both_absent_is_none(self):
        assert provider_usage._resets_at(None, None) is None

    def test_unparseable_reset_after_is_none(self):
        assert provider_usage._resets_at(None, "not-a-number") is None

    def test_window_uses_resets_at_precedence(self):
        headers = {
            "x-codex-primary-used-percent": "10",
            "x-codex-primary-window-minutes": "10080",
            "x-codex-primary-reset-at": "2026-08-01T00:00:00Z",
            "x-codex-primary-reset-after-seconds": "3600",
        }
        u = provider_usage.normalize_codex_headers(headers)
        assert u.windows[0]["resets_at"] == "2026-08-01T00:00:00Z"

    def test_window_derives_resets_at_from_reset_after(self):
        headers = {
            "x-codex-primary-used-percent": "10",
            "x-codex-primary-window-minutes": "300",
            "x-codex-primary-reset-after-seconds": "120",
        }
        u = provider_usage.normalize_codex_headers(headers)
        assert u.windows[0]["resets_at"] is not None
        assert u.windows[0]["resets_at"] != "120"  # derived, not passed through raw


# ---------------------------------------------------------------------------
# Allowlist enforcement (security hard invariant)
# ---------------------------------------------------------------------------

class TestAllowlistEnforcement:
    def test_authorization_header_never_reaches_raw(self):
        headers = {
            "authorization": "Bearer super-secret-token",
            "Authorization": "Bearer super-secret-token",
            "chatgpt-account-id": "acct-123",
            "cookie": "session=abc",
            "x-codex-plan-type": "pro",
        }
        u = provider_usage.normalize_codex_headers(headers)
        assert "authorization" not in u.raw
        assert "Authorization" not in u.raw
        assert "chatgpt-account-id" not in u.raw
        assert "cookie" not in u.raw
        for value in u.raw.values():
            assert "secret-token" not in str(value)
        assert u.raw == {"x-codex-plan-type": "pro"}

    def test_raw_only_contains_allowlisted_keys(self):
        headers = {k: "x" for k in provider_usage._CODEX_HEADER_KEYS}
        headers["some-unrelated-header"] = "y"
        u = provider_usage.normalize_codex_headers(headers)
        assert set(u.raw.keys()) == set(provider_usage._CODEX_HEADER_KEYS)

    def test_credits_parsed_from_headers(self):
        headers = {
            "x-codex-credits-has-credits": "true",
            "x-codex-credits-balance": "3.5",
            "x-codex-credits-unlimited": "false",
        }
        u = provider_usage.normalize_codex_headers(headers)
        assert u.credits == {"has_credits": True, "balance": 3.5, "unlimited": False}

    def test_plan_type_passed_through(self):
        u = provider_usage.normalize_codex_headers({"x-codex-plan-type": "plus"})
        assert u.plan_type == "plus"


# ---------------------------------------------------------------------------
# capture_codex_headers — fire-and-forget entrypoint
# ---------------------------------------------------------------------------

class TestCaptureCodexHeaders:
    async def test_capture_schedules_a_pending_task(self):
        headers = {"x-codex-plan-type": "pro"}
        with patch(
            "app.core.provider_usage._persist_codex_capture",
        ) as fake_persist:
            # patch with an async no-op so create_task gets a real coroutine
            async def _noop(*a, **k):
                return None

            fake_persist.side_effect = _noop
            provider_usage.capture_codex_headers("prov-1", headers)
            assert len(provider_usage._pending_capture_tasks) == 1
            await asyncio.gather(*list(provider_usage._pending_capture_tasks))
        # Task is discarded from the pending set once it completes.
        assert len(provider_usage._pending_capture_tasks) == 0
        fake_persist.assert_called_once()
        called_provider_id, called_headers = fake_persist.call_args.args
        assert called_provider_id == "prov-1"
        assert called_headers == {"x-codex-plan-type": "pro"}

    async def test_capture_noop_when_no_allowlisted_headers_present(self):
        before = len(provider_usage._pending_capture_tasks)
        provider_usage.capture_codex_headers("prov-2", {"some-other-header": "x"})
        assert len(provider_usage._pending_capture_tasks) == before

    async def test_persist_failure_never_raises(self):
        """A failing DB write inside _persist_codex_capture must not propagate."""
        with patch(
            "app.db.async_database.run_sync",
            side_effect=RuntimeError("db exploded"),
        ):
            # Should not raise.
            await provider_usage._persist_codex_capture(
                "prov-3", {"x-codex-plan-type": "pro"}
            )

    async def test_persist_writes_normalized_snapshot(self):
        captured = {}

        async def _fake_run_sync(fn, *args, **kwargs):
            captured["fn"] = fn
            captured["args"] = args
            return None

        with patch("app.db.async_database.run_sync", side_effect=_fake_run_sync):
            await provider_usage._persist_codex_capture(
                "prov-4",
                {
                    "x-codex-plan-type": "pro",
                    "x-codex-primary-used-percent": "50",
                    "x-codex-primary-window-minutes": "10080",
                },
            )
        assert captured["fn"].__name__ == "upsert_provider_usage_snapshot"
        provider_id, provider_type, plan_type, windows, credits, raw, fetched_at = (
            captured["args"]
        )
        assert provider_id == "prov-4"
        assert provider_type == "chatgpt"
        assert plan_type == "pro"
        assert windows == [
            {"type": "seven_day", "utilization": 50.0, "resets_at": None}
        ]
        assert isinstance(fetched_at, int)
