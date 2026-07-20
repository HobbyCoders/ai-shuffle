"""Tests for the OpenRouter usage poller (app/core/provider_usage.py, Chunk U3).

Covers:
  * ``_openrouter_windows_and_credits`` — AMENDMENT 1 window/credits mapping:
    a non-null ``limit`` produces exactly one clamped ``credit_limit`` window
    plus full dollar figures in ``credits``; a null ``limit`` produces empty
    windows with credits still populated.
  * ``ProviderUsageMonitor.poll_once`` — skips non-``openrouter`` provider
    rows, and a read failure is swallowed (never raises).
  * ``ProviderUsageMonitor._poll_provider`` — upserts a normalised snapshot on
    a 200 response (raw whitelist enforced — the key never lands in ``raw``);
    a non-200 response, a decrypt failure, and a network exception are all
    swallowed without raising and without an upsert; the Bearer key never
    appears in any captured log record.
"""

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.provider_usage import (
    ProviderUsageMonitor,
    _openrouter_windows_and_credits,
)


# ---------------------------------------------------------------------------
# _openrouter_windows_and_credits
# ---------------------------------------------------------------------------

class TestOpenrouterWindowsAndCredits:
    def test_non_null_limit_produces_one_credit_limit_window(self):
        data = {
            "limit": 10.0,
            "limit_remaining": 5.0,
            "usage": 5.0,
            "usage_daily": 0.10,
            "usage_weekly": 1.5,
            "usage_monthly": 5.0,
            "is_free_tier": False,
            "label": "my-key",
        }
        windows, credits = _openrouter_windows_and_credits(data)

        assert windows == [
            {"type": "credit_limit", "utilization": 50.0, "resets_at": None}
        ]
        assert credits == {
            "usage": 5.0,
            "usage_daily": 0.10,
            "usage_weekly": 1.5,
            "usage_monthly": 5.0,
            "limit": 10.0,
            "limit_remaining": 5.0,
            "is_free_tier": False,
        }

    def test_utilization_is_clamped_to_100(self):
        data = {"limit": 10.0, "usage": 25.0}
        windows, _ = _openrouter_windows_and_credits(data)
        assert windows[0]["utilization"] == 100.0

    def test_null_limit_produces_empty_windows_but_credits_populated(self):
        data = {
            "limit": None,
            "usage": 3.10,
            "usage_daily": 0.05,
            "usage_weekly": 0.80,
            "usage_monthly": 3.10,
            "is_free_tier": True,
        }
        windows, credits = _openrouter_windows_and_credits(data)

        assert windows == []
        assert credits["usage"] == 3.10
        assert credits["usage_daily"] == 0.05
        assert credits["usage_weekly"] == 0.80
        assert credits["usage_monthly"] == 3.10
        assert credits["limit"] is None
        assert credits["is_free_tier"] is True

    def test_zero_or_negative_limit_is_treated_as_unlimited(self):
        windows, credits = _openrouter_windows_and_credits({"limit": 0, "usage": 1.0})
        assert windows == []
        assert credits["limit"] == 0.0

    def test_missing_usage_with_a_limit_omits_the_window(self):
        # Can't compute a utilization without a usage figure — no window,
        # but the (null) dollar figures still land in credits.
        windows, credits = _openrouter_windows_and_credits({"limit": 10.0})
        assert windows == []
        assert credits["usage"] is None

    def test_unparseable_fields_do_not_raise(self):
        windows, credits = _openrouter_windows_and_credits(
            {"limit": "oops", "usage": "also-oops"}
        )
        assert windows == []
        assert credits["limit"] is None
        assert credits["usage"] is None


# ---------------------------------------------------------------------------
# ProviderUsageMonitor.poll_once
# ---------------------------------------------------------------------------

@pytest.fixture
def monitor():
    return ProviderUsageMonitor()


class TestPollOnce:
    @pytest.mark.asyncio
    async def test_skips_non_openrouter_rows(self, monitor):
        rows = [
            {"id": "p-chatgpt", "provider_type": "chatgpt", "base_url": "https://x"},
            {"id": "p-anthropic", "provider_type": "anthropic", "base_url": "https://x"},
        ]
        with patch("app.db.database.get_enabled_model_providers", return_value=rows), \
             patch.object(monitor, "_poll_provider", new=AsyncMock()) as poll_provider:
            await monitor.poll_once()

        poll_provider.assert_not_called()

    @pytest.mark.asyncio
    async def test_polls_only_openrouter_rows(self, monitor):
        rows = [
            {"id": "p-chatgpt", "provider_type": "chatgpt", "base_url": "https://x"},
            {"id": "p-or", "provider_type": "openrouter", "base_url": "https://openrouter.ai/api/v1"},
        ]
        with patch("app.db.database.get_enabled_model_providers", return_value=rows), \
             patch.object(monitor, "_poll_provider", new=AsyncMock()) as poll_provider:
            await monitor.poll_once()

        poll_provider.assert_called_once_with(rows[1])

    @pytest.mark.asyncio
    async def test_provider_read_failure_does_not_raise(self, monitor):
        with patch(
            "app.db.database.get_enabled_model_providers",
            side_effect=RuntimeError("db down"),
        ):
            await monitor.poll_once()  # must not raise

    @pytest.mark.asyncio
    async def test_include_token_passed_as_keyword(self, monitor):
        # AMENDMENT 2: include_token must be an explicit keyword (functools.partial),
        # never a positional argument.
        captured = {}

        def _fake_get_enabled(*args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            return []

        with patch("app.db.database.get_enabled_model_providers", side_effect=_fake_get_enabled):
            await monitor.poll_once()

        assert captured["args"] == ()
        assert captured["kwargs"] == {"include_token": True}


# ---------------------------------------------------------------------------
# ProviderUsageMonitor._poll_provider
# ---------------------------------------------------------------------------

def _fake_httpx(status_code=200, json_body=None, raise_exc=None):
    """Build a patch target for httpx.AsyncClient used as an async ctx manager."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_body if json_body is not None else {}

    client = MagicMock()
    if raise_exc is not None:
        client.get = AsyncMock(side_effect=raise_exc)
    else:
        client.get = AsyncMock(return_value=resp)

    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=client)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=ctx)


_KEY_RESPONSE = {
    "data": {
        "label": "my-key",
        "limit": 10.0,
        "limit_remaining": 7.0,
        "limit_reset": "monthly",
        "usage": 3.0,
        "usage_daily": 0.10,
        "usage_weekly": 1.0,
        "usage_monthly": 3.0,
        "is_free_tier": False,
        # Not in the allowlist — must never reach `raw`.
        "key": "sk-or-super-secret",
    }
}

_PROVIDER_ROW = {
    "id": "prov-or-1",
    "provider_type": "openrouter",
    "base_url": "https://openrouter.ai/api/v1",
    "auth_token": "enc:whatever",
}


class TestPollProvider:
    @pytest.mark.asyncio
    async def test_upserts_normalized_snapshot_on_200(self, monitor):
        upsert = MagicMock()
        with patch(
            "app.core.provider_usage._decrypt_provider_auth_token",
            return_value="sk-or-super-secret",
        ), patch(
            "app.core.provider_usage.httpx.AsyncClient", _fake_httpx(200, _KEY_RESPONSE)
        ), patch("app.db.database.upsert_provider_usage_snapshot", upsert):
            await monitor._poll_provider(_PROVIDER_ROW)

        upsert.assert_called_once()
        args, kwargs = upsert.call_args
        provider_id, provider_type, plan_type, windows, credits, raw, fetched_at = args
        assert provider_id == "prov-or-1"
        assert provider_type == "openrouter"
        assert plan_type is None
        assert windows == [{"type": "credit_limit", "utilization": 30.0, "resets_at": None}]
        assert credits["usage"] == 3.0
        assert credits["usage_monthly"] == 3.0
        assert fetched_at is not None

        # Raw whitelist enforcement: the key must never appear anywhere in `raw`.
        assert "key" not in raw
        assert "sk-or-super-secret" not in raw.values()
        assert raw["label"] == "my-key"
        assert raw["limit"] == 10.0

    @pytest.mark.asyncio
    async def test_url_is_key_endpoint_off_base_url_origin(self, monitor):
        captured = {}

        def _client_factory(*args, **kwargs):
            fake = _fake_httpx(200, _KEY_RESPONSE)()

            async def _get(url, headers=None):
                captured["url"] = url
                captured["headers"] = headers
                resp = MagicMock()
                resp.status_code = 200
                resp.json.return_value = _KEY_RESPONSE
                return resp

            fake_ctx = MagicMock()
            client = MagicMock()
            client.get = AsyncMock(side_effect=_get)
            fake_ctx.__aenter__ = AsyncMock(return_value=client)
            fake_ctx.__aexit__ = AsyncMock(return_value=False)
            return fake_ctx

        with patch(
            "app.core.provider_usage._decrypt_provider_auth_token",
            return_value="sk-or-super-secret",
        ), patch(
            "app.core.provider_usage.httpx.AsyncClient", side_effect=_client_factory
        ), patch("app.db.database.upsert_provider_usage_snapshot", MagicMock()):
            await monitor._poll_provider(_PROVIDER_ROW)

        assert captured["url"] == "https://openrouter.ai/api/v1/key"
        assert captured["headers"] == {"Authorization": "Bearer sk-or-super-secret"}

    @pytest.mark.asyncio
    async def test_non_200_does_not_raise_and_does_not_upsert(self, monitor):
        upsert = MagicMock()
        with patch(
            "app.core.provider_usage._decrypt_provider_auth_token",
            return_value="sk-or-super-secret",
        ), patch(
            "app.core.provider_usage.httpx.AsyncClient", _fake_httpx(401, {})
        ), patch("app.db.database.upsert_provider_usage_snapshot", upsert):
            await monitor._poll_provider(_PROVIDER_ROW)

        upsert.assert_not_called()

    @pytest.mark.asyncio
    async def test_network_exception_does_not_raise_and_does_not_upsert(self, monitor):
        upsert = MagicMock()
        with patch(
            "app.core.provider_usage._decrypt_provider_auth_token",
            return_value="sk-or-super-secret",
        ), patch(
            "app.core.provider_usage.httpx.AsyncClient",
            _fake_httpx(raise_exc=ConnectionError("boom")),
        ), patch("app.db.database.upsert_provider_usage_snapshot", upsert):
            await monitor._poll_provider(_PROVIDER_ROW)  # must not raise

        upsert.assert_not_called()

    @pytest.mark.asyncio
    async def test_decrypt_failure_does_not_raise_and_does_not_upsert(self, monitor):
        upsert = MagicMock()
        with patch(
            "app.core.provider_usage._decrypt_provider_auth_token",
            side_effect=ValueError("decryption failed"),
        ), patch("app.db.database.upsert_provider_usage_snapshot", upsert):
            await monitor._poll_provider(_PROVIDER_ROW)  # must not raise

        upsert.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_key_or_base_url_skips_without_error(self, monitor):
        upsert = MagicMock()
        with patch(
            "app.core.provider_usage._decrypt_provider_auth_token", return_value=None
        ), patch("app.db.database.upsert_provider_usage_snapshot", upsert):
            await monitor._poll_provider(_PROVIDER_ROW)

        upsert.assert_not_called()

    @pytest.mark.asyncio
    async def test_key_never_appears_in_any_log_record(self, monitor, caplog):
        secret = "sk-or-super-secret-value"
        with patch(
            "app.core.provider_usage._decrypt_provider_auth_token", return_value=secret
        ), patch(
            "app.core.provider_usage.httpx.AsyncClient", _fake_httpx(401, {})
        ), patch("app.db.database.upsert_provider_usage_snapshot", MagicMock()):
            with caplog.at_level(logging.DEBUG):
                await monitor._poll_provider(_PROVIDER_ROW)

        assert secret not in caplog.text
