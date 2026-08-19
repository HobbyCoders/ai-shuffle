"""Tests for the OpenRouter usage poller (app/core/provider_usage.py, Chunk U3).

Covers:
  * ``_openrouter_windows_and_credits`` — AMENDMENT 1 window/credits mapping:
    a non-null ``limit`` produces exactly one clamped ``credit_limit`` window
    plus full dollar figures in ``credits``; a null ``limit`` produces empty
    windows with credits still populated.
  * ``_merge_openrouter_credits`` — folds the ACCOUNT ledger from
    ``/api/v1/credits`` (``total_credits``/``total_usage`` and the derived
    ``balance_remaining``) into the ``/key`` credits blob without clobbering
    ``usage``; any unusable payload leaves the blob untouched.
  * ``ProviderUsageMonitor.poll_once`` — polls ``openrouter`` and Grok
    subscription rows, skips chatgpt/anthropic/openai/xai API-key rows, and
    a read failure is swallowed (never raises).
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
    _merge_openrouter_credits,
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
# _merge_openrouter_credits
# ---------------------------------------------------------------------------

class TestMergeOpenrouterCredits:
    def test_adds_totals_and_derived_balance(self):
        merged = _merge_openrouter_credits(
            {"usage": 3.0, "limit": None},
            {"total_credits": 25.0, "total_usage": 4.25},
        )
        assert merged["total_credits"] == 25.0
        assert merged["total_usage"] == 4.25
        assert merged["balance_remaining"] == pytest.approx(20.75)

    def test_key_usage_field_is_not_clobbered(self):
        # /key's `usage` is the API KEY's spend; /credits' `total_usage` is the
        # ACCOUNT's lifetime spend. They must coexist.
        merged = _merge_openrouter_credits(
            {"usage": 3.0},
            {"total_credits": 25.0, "total_usage": 4.25},
        )
        assert merged["usage"] == 3.0
        assert merged["total_usage"] == 4.25

    def test_input_dict_is_not_mutated(self):
        original = {"usage": 3.0}
        _merge_openrouter_credits(original, {"total_credits": 5.0, "total_usage": 1.0})
        assert original == {"usage": 3.0}

    def test_negative_balance_is_preserved(self):
        merged = _merge_openrouter_credits(
            {}, {"total_credits": 1.0, "total_usage": 2.5}
        )
        assert merged["balance_remaining"] == pytest.approx(-1.5)

    def test_numeric_strings_are_coerced(self):
        merged = _merge_openrouter_credits(
            {}, {"total_credits": "25", "total_usage": "4.25"}
        )
        assert merged["balance_remaining"] == pytest.approx(20.75)

    @pytest.mark.parametrize(
        "data",
        [
            None,
            [],
            "nope",
            {},
            {"total_credits": 25.0},
            {"total_usage": 4.25},
            {"total_credits": "oops", "total_usage": 4.25},
            {"total_credits": None, "total_usage": None},
        ],
    )
    def test_unusable_payload_leaves_credits_untouched(self, data):
        credits = {"usage": 3.0, "limit": 10.0}
        assert _merge_openrouter_credits(credits, data) == credits


# ---------------------------------------------------------------------------
# ProviderUsageMonitor.poll_once
# ---------------------------------------------------------------------------

@pytest.fixture
def monitor():
    return ProviderUsageMonitor()


class TestPollOnce:
    @pytest.mark.asyncio
    async def test_skips_chatgpt_anthropic_openai_xai_rows(self, monitor):
        rows = [
            {"id": "p-chatgpt", "provider_type": "chatgpt", "base_url": "https://x"},
            {"id": "p-anthropic", "provider_type": "anthropic", "base_url": "https://x"},
            {"id": "p-openai", "provider_type": "openai", "base_url": "https://api.openai.com"},
            {"id": "p-xai", "provider_type": "xai", "base_url": "https://api.x.ai", "auth_token": "xai-key"},
        ]
        with patch("app.db.database.get_enabled_model_providers", return_value=rows), \
             patch.object(monitor, "_poll_provider", new=AsyncMock()) as poll_provider, \
             patch.object(monitor, "_poll_grok_provider", new=AsyncMock()) as poll_grok:
            await monitor.poll_once()

        poll_provider.assert_not_called()
        poll_grok.assert_not_called()

    @pytest.mark.asyncio
    async def test_polls_openrouter_and_grok_subscription_rows(self, monitor):
        rows = [
            {"id": "p-chatgpt", "provider_type": "chatgpt", "base_url": "https://x"},
            {"id": "p-or", "provider_type": "openrouter", "base_url": "https://openrouter.ai/api/v1"},
            {"id": "p-grok", "provider_type": "grok", "auth_mode": "grok"},
            {"id": "p-xai", "provider_type": "xai", "auth_mode": "api"},
        ]
        with patch("app.db.database.get_enabled_model_providers", return_value=rows), \
             patch.object(monitor, "_poll_provider", new=AsyncMock()) as poll_provider, \
             patch.object(monitor, "_poll_grok_provider", new=AsyncMock()) as poll_grok:
            await monitor.poll_once()

        poll_provider.assert_called_once_with(rows[1])
        poll_grok.assert_called_once_with(rows[2])

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

_CREDITS_RESPONSE = {
    "data": {
        "total_credits": 25.0,
        "total_usage": 4.25,
    }
}

_PROVIDER_ROW = {
    "id": "prov-or-1",
    "provider_type": "openrouter",
    "base_url": "https://openrouter.ai/api/v1",
    "auth_token": "enc:whatever",
}


def _two_call_httpx(credits_status=200, credits_body=None, credits_exc=None):
    """httpx.AsyncClient patch serving /key then /credits per request URL."""

    def _factory(*args, **kwargs):
        async def _get(url, headers=None):
            if url.endswith("/credits"):
                if credits_exc is not None:
                    raise credits_exc
                resp = MagicMock()
                resp.status_code = credits_status
                resp.json.return_value = (
                    credits_body if credits_body is not None else {}
                )
                return resp
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = _KEY_RESPONSE
            return resp

        ctx = MagicMock()
        client = MagicMock()
        client.get = AsyncMock(side_effect=_get)
        ctx.__aenter__ = AsyncMock(return_value=client)
        ctx.__aexit__ = AsyncMock(return_value=False)
        return ctx

    return _factory


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
    async def test_urls_are_key_and_credits_endpoints_off_base_url_origin(self, monitor):
        calls = []

        def _client_factory(*args, **kwargs):
            async def _get(url, headers=None):
                calls.append((url, headers))
                resp = MagicMock()
                resp.status_code = 200
                resp.json.return_value = (
                    _CREDITS_RESPONSE if url.endswith("/credits") else _KEY_RESPONSE
                )
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

        auth = {"Authorization": "Bearer sk-or-super-secret"}
        assert calls == [
            ("https://openrouter.ai/api/v1/key", auth),
            ("https://openrouter.ai/api/v1/credits", auth),
        ]

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
    async def test_credits_endpoint_balance_lands_in_snapshot(self, monitor):
        upsert = MagicMock()
        with patch(
            "app.core.provider_usage._decrypt_provider_auth_token",
            return_value="sk-or-super-secret",
        ), patch(
            "app.core.provider_usage.httpx.AsyncClient",
            side_effect=_two_call_httpx(200, _CREDITS_RESPONSE),
        ), patch("app.db.database.upsert_provider_usage_snapshot", upsert):
            await monitor._poll_provider(_PROVIDER_ROW)

        credits = upsert.call_args[0][4]
        assert credits["total_credits"] == 25.0
        assert credits["total_usage"] == 4.25
        assert credits["balance_remaining"] == pytest.approx(20.75)
        # /key's own figures survive alongside the account ledger.
        assert credits["usage"] == 3.0
        assert credits["limit"] == 10.0

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "kwargs",
        [
            {"credits_status": 404},
            {"credits_status": 200, "credits_body": {"data": {}}},
            {"credits_exc": ConnectionError("boom")},
        ],
    )
    async def test_credits_failure_still_upserts_key_snapshot(self, monitor, kwargs):
        upsert = MagicMock()
        with patch(
            "app.core.provider_usage._decrypt_provider_auth_token",
            return_value="sk-or-super-secret",
        ), patch(
            "app.core.provider_usage.httpx.AsyncClient",
            side_effect=_two_call_httpx(**kwargs),
        ), patch("app.db.database.upsert_provider_usage_snapshot", upsert):
            await monitor._poll_provider(_PROVIDER_ROW)  # must not raise

        upsert.assert_called_once()
        args = upsert.call_args[0]
        assert args[3] == [
            {"type": "credit_limit", "utilization": 30.0, "resets_at": None}
        ]
        credits = args[4]
        assert credits["usage"] == 3.0
        assert "balance_remaining" not in credits

    @pytest.mark.asyncio
    async def test_credits_call_is_skipped_when_key_fetch_fails(self, monitor):
        calls = []

        def _factory(*args, **kwargs):
            async def _get(url, headers=None):
                calls.append(url)
                resp = MagicMock()
                resp.status_code = 401
                resp.json.return_value = {}
                return resp

            ctx = MagicMock()
            client = MagicMock()
            client.get = AsyncMock(side_effect=_get)
            ctx.__aenter__ = AsyncMock(return_value=client)
            ctx.__aexit__ = AsyncMock(return_value=False)
            return ctx

        with patch(
            "app.core.provider_usage._decrypt_provider_auth_token",
            return_value="sk-or-super-secret",
        ), patch(
            "app.core.provider_usage.httpx.AsyncClient", side_effect=_factory
        ), patch("app.db.database.upsert_provider_usage_snapshot", MagicMock()):
            await monitor._poll_provider(_PROVIDER_ROW)

        assert calls == ["https://openrouter.ai/api/v1/key"]

    @pytest.mark.asyncio
    async def test_key_never_appears_in_credits_failure_logs(self, monitor, caplog):
        secret = "sk-or-super-secret-value"
        with patch(
            "app.core.provider_usage._decrypt_provider_auth_token", return_value=secret
        ), patch(
            "app.core.provider_usage.httpx.AsyncClient",
            side_effect=_two_call_httpx(credits_exc=ConnectionError("boom")),
        ), patch("app.db.database.upsert_provider_usage_snapshot", MagicMock()):
            with caplog.at_level(logging.DEBUG):
                await monitor._poll_provider(_PROVIDER_ROW)

        assert secret not in caplog.text

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
