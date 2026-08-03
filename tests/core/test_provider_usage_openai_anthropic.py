"""Tests for the OpenAI/Anthropic API-key passive header capture (#757).

Mirrors ``tests/core/test_provider_usage.py`` (the Codex U2 test module):
covers ``normalize_openai_headers``/``normalize_anthropic_headers`` (window
mapping, allowlist enforcement — no token ever reaches ``raw``) and the
``capture_openai_headers``/``capture_anthropic_headers`` fire-and-forget
entrypoints (schedules a task, persists a normalized snapshot, never raises
on a DB failure). No live provider calls — every header dict here is
synthetic, built in-process.
"""

import asyncio
from unittest.mock import patch

from app.core import provider_usage


# ---------------------------------------------------------------------------
# _parse_openai_duration_seconds / _openai_reset_epoch
# ---------------------------------------------------------------------------

class TestOpenAiDurationParsing:
    def test_seconds_only(self):
        assert provider_usage._parse_openai_duration_seconds("1s") == 1.0

    def test_minutes_and_seconds(self):
        assert provider_usage._parse_openai_duration_seconds("6m0s") == 360.0

    def test_milliseconds(self):
        assert provider_usage._parse_openai_duration_seconds("200ms") == 0.2

    def test_hours_minutes_seconds(self):
        assert provider_usage._parse_openai_duration_seconds("1h4m0s") == 3840.0

    def test_none_is_none(self):
        assert provider_usage._parse_openai_duration_seconds(None) is None

    def test_empty_string_is_none(self):
        assert provider_usage._parse_openai_duration_seconds("") is None

    def test_garbage_is_none(self):
        assert provider_usage._parse_openai_duration_seconds("not-a-duration") is None

    def test_reset_epoch_is_derived_not_passed_through(self):
        import time

        before = int(time.time())
        result = provider_usage._openai_reset_epoch("60s")
        after = int(time.time())
        assert result is not None
        assert result != "60s"
        derived = int(result)
        assert before + 60 <= derived <= after + 60

    def test_reset_epoch_none_for_missing_duration(self):
        assert provider_usage._openai_reset_epoch(None) is None


# ---------------------------------------------------------------------------
# normalize_openai_headers
# ---------------------------------------------------------------------------

class TestNormalizeOpenAiHeaders:
    def test_requests_and_tokens_windows_computed(self):
        headers = {
            "x-ratelimit-limit-requests": "60",
            "x-ratelimit-limit-tokens": "150000",
            "x-ratelimit-remaining-requests": "59",
            "x-ratelimit-remaining-tokens": "149984",
            "x-ratelimit-reset-requests": "1s",
            "x-ratelimit-reset-tokens": "6m0s",
        }
        u = provider_usage.normalize_openai_headers(headers)
        types = {w["type"] for w in u.windows}
        assert types == {"requests", "tokens"}
        requests_w = next(w for w in u.windows if w["type"] == "requests")
        assert requests_w["utilization"] == (60 - 59) / 60 * 100
        tokens_w = next(w for w in u.windows if w["type"] == "tokens")
        assert tokens_w["utilization"] == (150000 - 149984) / 150000 * 100
        assert requests_w["resets_at"] is not None
        assert tokens_w["resets_at"] is not None
        assert u.credits is None
        assert u.plan_type is None

    def test_missing_remaining_omits_window(self):
        headers = {"x-ratelimit-limit-requests": "60"}
        u = provider_usage.normalize_openai_headers(headers)
        assert u.windows == []

    def test_zero_limit_omits_window(self):
        headers = {
            "x-ratelimit-limit-requests": "0",
            "x-ratelimit-remaining-requests": "0",
        }
        u = provider_usage.normalize_openai_headers(headers)
        assert u.windows == []

    def test_empty_headers_yields_empty_windows(self):
        u = provider_usage.normalize_openai_headers({})
        assert u.windows == []
        assert u.raw == {}

    def test_authorization_header_never_reaches_raw(self):
        headers = {
            "authorization": "Bearer super-secret-key",
            "Authorization": "Bearer super-secret-key",
            "x-ratelimit-limit-requests": "60",
            "x-ratelimit-remaining-requests": "59",
        }
        u = provider_usage.normalize_openai_headers(headers)
        assert "authorization" not in u.raw
        assert "Authorization" not in u.raw
        for value in u.raw.values():
            assert "secret-key" not in str(value)

    def test_raw_only_contains_allowlisted_keys(self):
        headers = {k: "1" for k in provider_usage._OPENAI_HEADER_KEYS}
        headers["some-unrelated-header"] = "y"
        u = provider_usage.normalize_openai_headers(headers)
        assert set(u.raw.keys()) == set(provider_usage._OPENAI_HEADER_KEYS)

    def test_utilization_clamped_to_100(self):
        # Remaining > limit shouldn't happen, but the clamp guards against a
        # negative "used" percent if it ever does.
        headers = {
            "x-ratelimit-limit-requests": "10",
            "x-ratelimit-remaining-requests": "20",
        }
        u = provider_usage.normalize_openai_headers(headers)
        assert u.windows[0]["utilization"] == 0.0


# ---------------------------------------------------------------------------
# normalize_anthropic_headers
# ---------------------------------------------------------------------------

class TestNormalizeAnthropicHeaders:
    def test_all_four_windows_computed(self):
        headers = {
            "anthropic-ratelimit-requests-limit": "50",
            "anthropic-ratelimit-requests-remaining": "45",
            "anthropic-ratelimit-requests-reset": "2026-08-03T12:00:00Z",
            "anthropic-ratelimit-tokens-limit": "40000",
            "anthropic-ratelimit-tokens-remaining": "36000",
            "anthropic-ratelimit-tokens-reset": "2026-08-03T12:00:00Z",
            "anthropic-ratelimit-input-tokens-limit": "20000",
            "anthropic-ratelimit-input-tokens-remaining": "18000",
            "anthropic-ratelimit-input-tokens-reset": "2026-08-03T12:00:00Z",
            "anthropic-ratelimit-output-tokens-limit": "20000",
            "anthropic-ratelimit-output-tokens-remaining": "19000",
            "anthropic-ratelimit-output-tokens-reset": "2026-08-03T12:00:00Z",
        }
        u = provider_usage.normalize_anthropic_headers(headers)
        types = {w["type"] for w in u.windows}
        assert types == {"requests", "tokens", "input_tokens", "output_tokens"}
        requests_w = next(w for w in u.windows if w["type"] == "requests")
        assert requests_w["utilization"] == (50 - 45) / 50 * 100
        assert requests_w["resets_at"] == "2026-08-03T12:00:00Z"  # passed through verbatim
        assert u.credits is None
        assert u.plan_type is None

    def test_missing_remaining_omits_window(self):
        headers = {"anthropic-ratelimit-requests-limit": "50"}
        u = provider_usage.normalize_anthropic_headers(headers)
        assert u.windows == []

    def test_zero_limit_omits_window(self):
        headers = {
            "anthropic-ratelimit-tokens-limit": "0",
            "anthropic-ratelimit-tokens-remaining": "0",
        }
        u = provider_usage.normalize_anthropic_headers(headers)
        assert u.windows == []

    def test_empty_headers_yields_empty_windows(self):
        u = provider_usage.normalize_anthropic_headers({})
        assert u.windows == []
        assert u.raw == {}

    def test_authorization_and_api_key_headers_never_reach_raw(self):
        headers = {
            "authorization": "Bearer super-secret-key",
            "x-api-key": "sk-ant-super-secret",
            "anthropic-ratelimit-requests-limit": "50",
            "anthropic-ratelimit-requests-remaining": "45",
        }
        u = provider_usage.normalize_anthropic_headers(headers)
        assert "authorization" not in u.raw
        assert "x-api-key" not in u.raw
        for value in u.raw.values():
            assert "secret" not in str(value)

    def test_raw_only_contains_allowlisted_keys(self):
        headers = {k: "1" for k in provider_usage._ANTHROPIC_HEADER_KEYS}
        headers["some-unrelated-header"] = "y"
        u = provider_usage.normalize_anthropic_headers(headers)
        assert set(u.raw.keys()) == set(provider_usage._ANTHROPIC_HEADER_KEYS)


# ---------------------------------------------------------------------------
# capture_openai_headers — fire-and-forget entrypoint
# ---------------------------------------------------------------------------

class TestCaptureOpenAiHeaders:
    async def test_capture_schedules_a_pending_task(self):
        headers = {"x-ratelimit-limit-requests": "60"}
        with patch("app.core.provider_usage._persist_openai_capture") as fake_persist:
            async def _noop(*a, **k):
                return None

            fake_persist.side_effect = _noop
            provider_usage.capture_openai_headers("prov-openai-1", headers)
            assert len(provider_usage._pending_capture_tasks) == 1
            await asyncio.gather(*list(provider_usage._pending_capture_tasks))
        assert len(provider_usage._pending_capture_tasks) == 0
        fake_persist.assert_called_once()
        called_provider_id, called_headers = fake_persist.call_args.args
        assert called_provider_id == "prov-openai-1"
        assert called_headers == {"x-ratelimit-limit-requests": "60"}

    async def test_capture_noop_when_no_allowlisted_headers_present(self):
        before = len(provider_usage._pending_capture_tasks)
        provider_usage.capture_openai_headers("prov-openai-2", {"some-other-header": "x"})
        assert len(provider_usage._pending_capture_tasks) == before

    async def test_persist_failure_never_raises(self):
        with patch(
            "app.core.provider_usage.run_sync",
            side_effect=RuntimeError("db exploded"),
        ):
            await provider_usage._persist_openai_capture(
                "prov-openai-3", {"x-ratelimit-limit-requests": "60"}
            )

    async def test_persist_writes_normalized_snapshot(self):
        captured = {}

        async def _fake_run_sync(fn, *args, **kwargs):
            captured["fn"] = fn
            captured["args"] = args
            return None

        with patch("app.core.provider_usage.run_sync", side_effect=_fake_run_sync):
            await provider_usage._persist_openai_capture(
                "prov-openai-4",
                {
                    "x-ratelimit-limit-requests": "60",
                    "x-ratelimit-remaining-requests": "30",
                },
            )
        assert captured["fn"].__name__ == "upsert_provider_usage_snapshot"
        provider_id, provider_type, plan_type, windows, credits, raw, fetched_at = (
            captured["args"]
        )
        assert provider_id == "prov-openai-4"
        assert provider_type == "openai"
        assert plan_type is None
        assert credits is None
        assert windows == [{"type": "requests", "utilization": 50.0, "resets_at": None}]
        assert isinstance(fetched_at, int)


# ---------------------------------------------------------------------------
# capture_anthropic_headers — fire-and-forget entrypoint
# ---------------------------------------------------------------------------

class TestCaptureAnthropicHeaders:
    async def test_capture_schedules_a_pending_task(self):
        headers = {"anthropic-ratelimit-requests-limit": "50"}
        with patch("app.core.provider_usage._persist_anthropic_capture") as fake_persist:
            async def _noop(*a, **k):
                return None

            fake_persist.side_effect = _noop
            provider_usage.capture_anthropic_headers("prov-anthropic-1", headers)
            assert len(provider_usage._pending_capture_tasks) == 1
            await asyncio.gather(*list(provider_usage._pending_capture_tasks))
        assert len(provider_usage._pending_capture_tasks) == 0
        fake_persist.assert_called_once()
        called_provider_id, called_headers = fake_persist.call_args.args
        assert called_provider_id == "prov-anthropic-1"
        assert called_headers == {"anthropic-ratelimit-requests-limit": "50"}

    async def test_capture_noop_when_no_allowlisted_headers_present(self):
        before = len(provider_usage._pending_capture_tasks)
        provider_usage.capture_anthropic_headers(
            "prov-anthropic-2", {"some-other-header": "x"}
        )
        assert len(provider_usage._pending_capture_tasks) == before

    async def test_persist_failure_never_raises(self):
        with patch(
            "app.core.provider_usage.run_sync",
            side_effect=RuntimeError("db exploded"),
        ):
            await provider_usage._persist_anthropic_capture(
                "prov-anthropic-3", {"anthropic-ratelimit-requests-limit": "50"}
            )

    async def test_persist_writes_normalized_snapshot(self):
        captured = {}

        async def _fake_run_sync(fn, *args, **kwargs):
            captured["fn"] = fn
            captured["args"] = args
            return None

        with patch("app.core.provider_usage.run_sync", side_effect=_fake_run_sync):
            await provider_usage._persist_anthropic_capture(
                "prov-anthropic-4",
                {
                    "anthropic-ratelimit-requests-limit": "50",
                    "anthropic-ratelimit-requests-remaining": "25",
                    "anthropic-ratelimit-requests-reset": "2026-08-03T12:00:00Z",
                },
            )
        assert captured["fn"].__name__ == "upsert_provider_usage_snapshot"
        provider_id, provider_type, plan_type, windows, credits, raw, fetched_at = (
            captured["args"]
        )
        assert provider_id == "prov-anthropic-4"
        assert provider_type == "anthropic"
        assert plan_type is None
        assert credits is None
        assert windows == [
            {
                "type": "requests",
                "utilization": 50.0,
                "resets_at": "2026-08-03T12:00:00Z",
            }
        ]
        assert isinstance(fetched_at, int)
