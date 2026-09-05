"""Tests for SuperGrok subscription usage (app/core/provider_usage.py).

Covers ``normalize_grok_billing`` (config wrapper vs flat payload, on-demand
fallback, period-present → 0%, productUsage skip/map incl. the never-surfaced
GrokBuild product, raw/credits allowlist)
and ``ProviderUsageMonitor._poll_grok_provider`` / ``poll_once`` (OAuth token
from ``codex_access_token``, settings failure does not drop billing, 401 is
swallowed, API-key xai rows are never polled). No real network.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.core.provider_usage import (
    GROK_BILLING_HOST,
    GROK_BILLING_URL,
    GROK_SETTINGS_URL,
    GROK_TOKEN_AUTH,
    ProviderUsageMonitor,
    _grok_url_is_pinned,
    _is_grok_subscription_row,
    normalize_grok_billing,
)


_TOKEN_KEY_RE = re.compile(
    r"token|authorization|bearer|cookie|secret|password|api[_-]?key",
    re.I,
)

_PERIOD_END = "2026-08-17T00:00:00Z"
_PERIOD = {
    "type": "USAGE_PERIOD_TYPE_WEEKLY",
    "start": "2026-08-10T00:00:00Z",
    "end": _PERIOD_END,
}

_BILLING_INNER: Dict[str, Any] = {
    "creditUsagePercent": 34.0,
    "currentPeriod": dict(_PERIOD),
    "billingPeriodEnd": _PERIOD_END,
    "productUsage": [
        {"product": "Api", "usagePercent": 34.0},
        {"product": "GrokChat"},
        {"product": "GrokBuild", "usagePercent": 12.0},
    ],
    "onDemandUsed": {"val": 0},
    "onDemandCap": {"val": 0},
}

_BILLING_WRAPPED = {"config": dict(_BILLING_INNER)}

_SETTINGS = {"subscription_tier_display": "SuperGrok Heavy"}

_GROK_ROW = {
    "id": "prov-grok-1",
    "provider_type": "grok",
    "auth_mode": "grok",
    "name": "Grok",
    "needs_reauth": False,
    "auth_token": "xai-api-key-must-never-be-sent",
}

_CODEX_TOKENS = {
    "codex_access_token": "enc:grok-access",
    "needs_reauth": False,
}

_ACCESS = "oauth-access-token"


def _assert_no_token_keys(obj: Any) -> None:
    if isinstance(obj, dict):
        for key, value in obj.items():
            assert _TOKEN_KEY_RE.search(str(key)) is None, key
            _assert_no_token_keys(value)
    elif isinstance(obj, list):
        for item in obj:
            _assert_no_token_keys(item)


def _windows_by_type(windows):
    return {w["type"]: w for w in windows}


# ---------------------------------------------------------------------------
# _is_grok_subscription_row
# ---------------------------------------------------------------------------


class TestIsGrokSubscriptionRow:
    def test_provider_type_grok(self):
        assert _is_grok_subscription_row({"provider_type": "grok"}) is True

    def test_auth_mode_grok(self):
        assert _is_grok_subscription_row({"auth_mode": "grok"}) is True

    def test_xai_api_key_row_is_not(self):
        assert _is_grok_subscription_row({"provider_type": "xai"}) is False

    def test_empty_is_not(self):
        assert _is_grok_subscription_row(None) is False
        assert _is_grok_subscription_row({}) is False


class TestGrokUrlPin:
    def test_billing_and_settings_urls_are_pinned(self):
        assert _grok_url_is_pinned(GROK_BILLING_URL) is True
        assert _grok_url_is_pinned(GROK_SETTINGS_URL) is True
        assert GROK_BILLING_HOST == "cli-chat-proxy.grok.com"

    def test_rejects_http_and_wrong_host(self):
        assert _grok_url_is_pinned("http://cli-chat-proxy.grok.com/v1/billing") is False
        assert _grok_url_is_pinned("https://grok.com/v1/billing") is False
        assert _grok_url_is_pinned("https://evil.example/v1/billing") is False


# ---------------------------------------------------------------------------
# normalize_grok_billing
# ---------------------------------------------------------------------------


class TestNormalizeGrokBilling:
    def test_config_wrapped_percent_period_and_products(self):
        u = normalize_grok_billing(_BILLING_WRAPPED)
        by_type = _windows_by_type(u.windows)
        assert by_type["seven_day"]["utilization"] == 34.0
        assert by_type["seven_day"]["resets_at"] == _PERIOD_END
        assert by_type["grok_api"]["utilization"] == 34.0
        # GrokBuild is deliberately never surfaced as a window.
        assert "grok_build" not in by_type
        assert "grok_chat" not in by_type
        assert u.credits["credit_usage_percent"] == 34.0
        assert u.credits["on_demand_used"] == 0.0
        assert u.credits["on_demand_cap"] == 0.0
        assert u.credits["period_type"] == "USAGE_PERIOD_TYPE_WEEKLY"
        assert u.credits["period_end"] == _PERIOD_END
        assert set(u.credits) <= {
            "credit_usage_percent",
            "on_demand_used",
            "on_demand_cap",
            "period_type",
            "period_end",
        }

    def test_top_level_payload_same_as_config_wrapped(self):
        wrapped = normalize_grok_billing(_BILLING_WRAPPED)
        flat = normalize_grok_billing(dict(_BILLING_INNER))
        assert wrapped.windows == flat.windows
        assert wrapped.credits == flat.credits
        assert wrapped.raw == flat.raw

    def test_missing_percent_falls_back_to_on_demand_ratio(self):
        u = normalize_grok_billing(
            {
                "onDemandUsed": {"val": 25},
                "onDemandCap": {"val": 100},
                "currentPeriod": dict(_PERIOD),
            }
        )
        seven = _windows_by_type(u.windows)["seven_day"]
        assert seven["utilization"] == 25.0
        assert u.credits["on_demand_used"] == 25.0
        assert u.credits["on_demand_cap"] == 100.0
        assert "credit_usage_percent" not in u.credits

    def test_missing_percent_zero_cap_with_period_is_zero_not_missing(self):
        u = normalize_grok_billing(
            {
                "onDemandUsed": {"val": 0},
                "onDemandCap": {"val": 0},
                "currentPeriod": dict(_PERIOD),
            }
        )
        assert _windows_by_type(u.windows)["seven_day"]["utilization"] == 0.0

    def test_missing_percent_absent_cap_with_period_is_zero(self):
        u = normalize_grok_billing({"currentPeriod": dict(_PERIOD)})
        assert _windows_by_type(u.windows)["seven_day"]["utilization"] == 0.0

    def test_missing_percent_and_period_omits_seven_day(self):
        u = normalize_grok_billing({"onDemandUsed": {"val": 1}, "onDemandCap": {"val": 0}})
        assert "seven_day" not in _windows_by_type(u.windows)
        assert u.windows == []

    def test_product_without_usage_percent_is_skipped(self):
        u = normalize_grok_billing(
            {
                "creditUsagePercent": 1.0,
                "currentPeriod": dict(_PERIOD),
                "productUsage": [
                    {"product": "GrokChat"},
                    {"product": "GrokBuild", "usagePercent": 12.0},
                    {"product": "Api", "usagePercent": "n/a"},
                ],
            }
        )
        # GrokChat/Api carry no usable percent; GrokBuild is dropped by policy.
        assert set(_windows_by_type(u.windows)) == {"seven_day"}

    def test_grok_build_product_never_becomes_a_window(self):
        u = normalize_grok_billing(
            {
                "creditUsagePercent": 1.0,
                "currentPeriod": dict(_PERIOD),
                "productUsage": [
                    {"product": "GrokBuild", "usagePercent": 12.0},
                    {"product": "Api", "usagePercent": 34.0},
                    {"product": "GrokChat", "usagePercent": 7.0},
                ],
            }
        )
        by_type = _windows_by_type(u.windows)
        assert "grok_build" not in by_type
        assert by_type["grok_api"]["utilization"] == 34.0
        assert by_type["grok_chat"]["utilization"] == 7.0

    def test_unknown_product_becomes_grok_slug(self):
        u = normalize_grok_billing(
            {
                "creditUsagePercent": 1.0,
                "productUsage": [{"product": "Something Else", "usagePercent": 5}],
            }
        )
        assert "grok_something_else" in _windows_by_type(u.windows)

    def test_never_raises_on_garbage(self):
        assert normalize_grok_billing(None).windows == []
        assert normalize_grok_billing("nope").windows == []
        assert normalize_grok_billing([]).windows == []

    def test_raw_and_credits_never_contain_token_like_keys(self):
        payload = {
            "config": {
                **_BILLING_INNER,
                "Authorization": "Bearer leaked",
                "access_token": "tok",
                "cookie": "sid=1",
                "api_key": "xai-secret",
            },
            "Authorization": "Bearer leaked",
            "token": "nope",
        }
        u = normalize_grok_billing(payload)
        _assert_no_token_keys(u.raw)
        _assert_no_token_keys(u.credits)
        assert "Authorization" not in u.raw
        assert "access_token" not in u.raw
        assert "cookie" not in u.raw
        assert "leaked" not in str(u.raw)
        assert "leaked" not in str(u.credits)
        assert set(u.raw) <= {
            "creditUsagePercent",
            "currentPeriod",
            "billingPeriodEnd",
            "productUsage",
            "onDemandUsed",
            "onDemandCap",
        }

    def test_raw_product_usage_omits_percentless_entries_secrets(self):
        u = normalize_grok_billing(_BILLING_WRAPPED)
        products = {p["product"] for p in u.raw["productUsage"]}
        # GrokChat has no usagePercent — still listed if present, but only
        # allowlisted fields. Either skipped or product-only is fine.
        for item in u.raw["productUsage"]:
            assert set(item) <= {"product", "usagePercent"}
        assert "Api" in products
        assert "GrokBuild" in products


# ---------------------------------------------------------------------------
# _poll_grok_provider
# ---------------------------------------------------------------------------


@pytest.fixture
def monitor():
    return ProviderUsageMonitor()


_RealAsyncClient = httpx.AsyncClient


def _mock_transport_client(handler, recorded=None):
    """Patch target: AsyncClient that talks to MockTransport, never the network."""

    def _factory(*args, **kwargs):
        if recorded is not None:
            recorded.append(dict(kwargs))
        kwargs = dict(kwargs)
        kwargs["transport"] = httpx.MockTransport(handler)
        kwargs.setdefault("follow_redirects", False)
        return _RealAsyncClient(*args, **kwargs)

    return _factory


def _billing_settings_handler(
    billing_status=200,
    billing_body=None,
    settings_status=200,
    settings_body=None,
    seen=None,
):
    def _handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        assert request.url.host == GROK_BILLING_HOST
        assert request.url.scheme == "https"
        assert request.headers.get("authorization") == f"Bearer {_ACCESS}"
        assert request.headers.get("x-xai-token-auth") == GROK_TOKEN_AUTH
        assert "xai-api-key-must-never-be-sent" not in str(request.headers)
        if request.url.path.endswith("/billing"):
            return httpx.Response(
                billing_status,
                json=billing_body if billing_body is not None else _BILLING_WRAPPED,
            )
        if request.url.path.endswith("/settings"):
            return httpx.Response(
                settings_status,
                json=settings_body if settings_body is not None else _SETTINGS,
            )
        return httpx.Response(404, json={})

    return _handler


def _token_patches(tokens: Optional[Dict[str, Any]] = _CODEX_TOKENS, access: str = _ACCESS):
    return (
        patch("app.db.database.get_codex_tokens", return_value=tokens),
        patch("app.core.provider_usage.encryption.is_encrypted", return_value=True),
        patch("app.core.provider_usage.encryption.decrypt_value", return_value=access),
    )


class TestPollGrokProvider:
    @pytest.mark.asyncio
    async def test_billing_is_saved_before_optional_settings(self, monitor):
        upsert = MagicMock()
        async def get(url, headers, timeout):
            if "/settings" in url:
                upsert.assert_called_once()
                assert upsert.call_args.args[3]
                return {}, 500
            return {"creditUsagePercent": 34}, 200

        t1, t2, t3 = _token_patches()
        with t1, t2, t3, patch("app.core.provider_usage._grok_get", new=AsyncMock(side_effect=get)), patch(
            "app.db.database.upsert_provider_usage_snapshot", upsert
        ), patch("app.db.database.get_all_provider_usage_snapshots", return_value=[]):
            await monitor._poll_grok_provider(_GROK_ROW)
        upsert.assert_called_once()

    @pytest.mark.asyncio
    async def test_200_billing_and_settings_upserts_plan_from_settings(self, monitor):
        upsert = MagicMock()
        seen = []
        recorded = []
        t1, t2, t3 = _token_patches()
        with t1, t2, t3, patch(
            "app.core.provider_usage.httpx.AsyncClient",
            side_effect=_mock_transport_client(
                _billing_settings_handler(seen=seen), recorded=recorded
            ),
        ), patch("app.db.database.upsert_provider_usage_snapshot", upsert):
            await monitor._poll_grok_provider(_GROK_ROW)

        assert upsert.call_count == 2
        assert upsert.call_args_list[0].args[2] == "SuperGrok"
        assert upsert.call_args_list[0].args[6] == upsert.call_args_list[1].args[6]
        args = upsert.call_args[0]
        provider_id, provider_type, plan_type, windows, credits, raw, fetched_at = args
        assert provider_id == "prov-grok-1"
        assert provider_type == "grok"
        assert plan_type == "SuperGrok Heavy"
        by_type = _windows_by_type(windows)
        assert by_type["seven_day"]["utilization"] == 34.0
        assert by_type["grok_api"]["utilization"] == 34.0
        assert "grok_build" not in by_type
        assert credits["credit_usage_percent"] == 34.0
        _assert_no_token_keys(raw)
        _assert_no_token_keys(credits)
        assert fetched_at is not None
        assert {r.url.path for r in seen} >= {"/v1/billing", "/v1/settings"}
        assert all(kwargs.get("follow_redirects") is False for kwargs in recorded)

    @pytest.mark.asyncio
    async def test_settings_500_still_upserts_with_supergrok_fallback(self, monitor):
        upsert = MagicMock()
        t1, t2, t3 = _token_patches()
        with t1, t2, t3, patch(
            "app.core.provider_usage.httpx.AsyncClient",
            side_effect=_mock_transport_client(
                _billing_settings_handler(settings_status=500, settings_body={})
            ),
        ), patch("app.db.database.upsert_provider_usage_snapshot", upsert):
            await monitor._poll_grok_provider(_GROK_ROW)

        upsert.assert_called_once()
        assert upsert.call_args[0][2] == "SuperGrok"
        assert upsert.call_args[0][3][0]["type"] == "seven_day"

    @pytest.mark.asyncio
    async def test_401_billing_does_not_raise_or_upsert(self, monitor):
        upsert = MagicMock()
        t1, t2, t3 = _token_patches()
        with t1, t2, t3, patch(
            "app.core.provider_usage.httpx.AsyncClient",
            side_effect=_mock_transport_client(
                _billing_settings_handler(billing_status=401, billing_body={})
            ),
        ), patch(
            "app.core.provider_usage._maybe_refresh_grok_on_401",
            new=AsyncMock(return_value=False),
        ), patch("app.db.database.upsert_provider_usage_snapshot", upsert):
            await monitor._poll_grok_provider(_GROK_ROW)  # must not raise

        upsert.assert_not_called()

    @pytest.mark.asyncio
    async def test_row_without_access_token_skips(self, monitor):
        upsert = MagicMock()
        t1, t2, t3 = _token_patches(tokens={"codex_access_token": None, "needs_reauth": False})
        with t1, t2, t3, patch(
            "app.core.provider_usage.httpx.AsyncClient",
            side_effect=AssertionError("must not HTTP without a token"),
        ), patch("app.db.database.upsert_provider_usage_snapshot", upsert):
            await monitor._poll_grok_provider(_GROK_ROW)

        upsert.assert_not_called()

    @pytest.mark.asyncio
    async def test_needs_reauth_skips(self, monitor):
        upsert = MagicMock()
        with patch(
            "app.db.database.get_codex_tokens",
            return_value={"codex_access_token": "enc:x", "needs_reauth": True},
        ), patch(
            "app.core.provider_usage.httpx.AsyncClient",
            side_effect=AssertionError("must not HTTP when needs_reauth"),
        ), patch("app.db.database.upsert_provider_usage_snapshot", upsert):
            await monitor._poll_grok_provider(_GROK_ROW)

        upsert.assert_not_called()

    @pytest.mark.asyncio
    async def test_bearer_token_never_appears_in_logs(self, monitor, caplog):
        t1, t2, t3 = _token_patches()
        with t1, t2, t3, patch(
            "app.core.provider_usage.httpx.AsyncClient",
            side_effect=_mock_transport_client(
                _billing_settings_handler(billing_status=401, billing_body={})
            ),
        ), patch(
            "app.core.provider_usage._maybe_refresh_grok_on_401",
            new=AsyncMock(return_value=False),
        ), patch("app.db.database.upsert_provider_usage_snapshot", MagicMock()):
            with caplog.at_level(logging.DEBUG):
                await monitor._poll_grok_provider(_GROK_ROW)

        assert _ACCESS not in caplog.text
        assert "xai-api-key-must-never-be-sent" not in caplog.text
        assert "enc:grok-access" not in caplog.text


class TestPollOnceSkipsXai:
    @pytest.mark.asyncio
    async def test_api_key_xai_row_is_never_polled(self, monitor):
        rows = [
            {
                "id": "p-xai",
                "provider_type": "xai",
                "auth_mode": "api",
                "auth_token": "xai-key",
            },
            {"id": "p-grok", "provider_type": "grok", "auth_mode": "grok"},
        ]
        with patch("app.db.database.get_enabled_model_providers", return_value=rows), \
             patch.object(monitor, "_poll_provider", new=AsyncMock()) as poll_or, \
             patch.object(monitor, "_poll_grok_provider", new=AsyncMock()) as poll_grok, \
             patch.object(monitor, "_poll_openai_provider", new=AsyncMock()) as poll_openai, \
             patch.object(monitor, "_poll_fal", new=AsyncMock()):
            await monitor.poll_once()

        poll_or.assert_not_called()
        poll_openai.assert_not_called()
        poll_grok.assert_called_once_with(rows[1])
