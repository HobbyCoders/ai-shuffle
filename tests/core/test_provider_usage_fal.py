"""Tests for fal.ai account billing polling (app/core/provider_usage.py).

Covers ``normalize_fal_billing`` (credits.current_balance mapping, unusable
payloads) and ``ProviderUsageMonitor._poll_fal`` (pinned billing URL, Key
auth, 200 upsert under the synthetic ``fal-ai`` id, missing key deletes the
snapshot, credential never logged). No live network.
"""

from __future__ import annotations

import logging
import re
from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.core.provider_usage import (
    FAL_BILLING_HOST,
    FAL_BILLING_URL,
    FAL_USAGE_PROVIDER_ID,
    FAL_USAGE_PROVIDER_NAME,
    FAL_USAGE_PROVIDER_TYPE,
    ProviderUsageMonitor,
    _fal_auth_header,
    _fal_billing_url_is_pinned,
    normalize_fal_billing,
)


_TOKEN_KEY_RE = re.compile(
    r"token|authorization|bearer|cookie|secret|password|api[_-]?key",
    re.I,
)

_KEY = "fal-secret-must-never-log"
# The retired ordinary-key endpoint. The handler still answers it so a
# regression that falls back to it would be visible in ``seen``.
FAL_USER_BALANCE_HOST = "rest.alpha.fal.ai"
_BILLING = {
    "username": "my-team",
    "credits": {"current_balance": 24.5, "currency": "USD"},
}


class TestNormalizeFalBilling:
    def test_maps_current_balance_to_balance_remaining(self):
        credits = normalize_fal_billing(_BILLING)
        assert credits == {"balance_remaining": 24.5, "currency": "USD"}

    def test_flat_current_balance_shape(self):
        credits = normalize_fal_billing({"current_balance": 1.25, "currency": "USD"})
        assert credits == {"balance_remaining": 1.25, "currency": "USD"}

    def test_zero_balance_is_displayable(self):
        credits = normalize_fal_billing({"credits": {"current_balance": 0, "currency": "USD"}})
        assert credits["balance_remaining"] == 0.0

    def test_numeric_strings_are_coerced(self):
        credits = normalize_fal_billing({"credits": {"current_balance": "24.5"}})
        assert credits["balance_remaining"] == pytest.approx(24.5)
        assert "currency" not in credits

    @pytest.mark.parametrize(
        "payload",
        [None, [], "nope", {}, {"credits": {}}, {"credits": {"currency": "USD"}}],
    )
    def test_unusable_payload_returns_none(self, payload):
        assert normalize_fal_billing(payload) is None


class TestFalAuthAndPin:
    def test_auth_header_prefixes_key(self):
        assert _fal_auth_header("abc") == "Key abc"
        assert _fal_auth_header("Key abc") == "Key abc"
        assert _fal_auth_header("key abc") == "key abc"

    def test_billing_url_is_pinned(self):
        assert _fal_billing_url_is_pinned(FAL_BILLING_URL) is True
        assert _fal_billing_url_is_pinned("http://api.fal.ai/v1/account/billing") is False
        assert _fal_billing_url_is_pinned(
            "https://evil.example/v1/account/billing?expand=credits"
        ) is False


@pytest.fixture
def monitor():
    return ProviderUsageMonitor()


@pytest.fixture(autouse=True)
def _credential_generation(mock_db):
    from app.db import database as db
    db.set_system_setting("fal_billing_api_key", _KEY)


_RealAsyncClient = httpx.AsyncClient


def _mock_transport_client(handler, recorded=None):
    def _factory(*args, **kwargs):
        if recorded is not None:
            recorded.append(dict(kwargs))
        kwargs = dict(kwargs)
        kwargs["transport"] = httpx.MockTransport(handler)
        kwargs.setdefault("follow_redirects", False)
        return _RealAsyncClient(*args, **kwargs)

    return _factory


def _fal_handler(
    billing_status=200,
    billing_body=None,
    balance_status=200,
    balance_body=18.11,
    seen=None,
):
    def _handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        assert request.url.scheme == "https"
        assert request.headers.get("authorization") == f"Key {_KEY}"
        if request.url.host == FAL_BILLING_HOST:
            assert request.url.path == "/v1/account/billing"
            assert request.url.params.get("expand") == "credits"
            return httpx.Response(
                billing_status,
                json=billing_body if billing_body is not None else _BILLING,
            )
        if request.url.host == FAL_USER_BALANCE_HOST:
            assert request.url.path == "/billing/user_balance"
            if isinstance(balance_body, (dict, list)):
                return httpx.Response(balance_status, json=balance_body)
            if isinstance(balance_body, (int, float)) and not isinstance(balance_body, bool):
                return httpx.Response(balance_status, json=balance_body)
            return httpx.Response(balance_status, text=str(balance_body))
        return httpx.Response(404, json={})

    return _handler


class TestPollFal:
    @pytest.mark.asyncio
    async def test_200_upserts_synthetic_snapshot(self, monitor):
        upsert = MagicMock()
        seen = []
        recorded = []
        with patch(
            "app.api.settings_crypto.get_decrypted_api_key", return_value=_KEY
        ), patch(
            "app.core.provider_usage.httpx.AsyncClient",
            side_effect=_mock_transport_client(_fal_handler(seen=seen), recorded),
        ), patch("app.db.database.upsert_provider_usage_snapshot", upsert), patch(
            "app.db.database.delete_provider_usage_snapshot", MagicMock()
        ) as delete:
            await monitor._poll_fal()

        delete.assert_not_called()
        upsert.assert_called_once()
        provider_id, provider_type, plan_type, windows, credits, raw, fetched_at = (
            upsert.call_args[0]
        )
        assert provider_id == FAL_USAGE_PROVIDER_ID
        assert provider_type == FAL_USAGE_PROVIDER_TYPE
        assert provider_id == "fal-ai"
        assert FAL_USAGE_PROVIDER_NAME == "fal.ai"
        assert plan_type is None
        assert windows == []
        assert credits == {"balance_remaining": 24.5, "currency": "USD"}
        assert raw["username"] == "my-team"
        assert raw["current_balance"] == 24.5
        assert raw["source"] == "fal_admin"
        assert isinstance(fetched_at, int)
        assert recorded and recorded[0].get("follow_redirects") is False
        assert len(seen) == 1

    @pytest.mark.asyncio
    async def test_missing_key_does_not_publish_a_snapshot(self, monitor):
        delete = MagicMock(return_value=True)
        upsert = MagicMock()
        with patch(
            "app.api.settings_crypto.get_decrypted_api_key", return_value=None
        ), patch("app.db.database.delete_provider_usage_snapshot", delete), patch(
            "app.db.database.upsert_provider_usage_snapshot", upsert
        ):
            await monitor._poll_fal()

        # Removal invalidates in the credential transaction, not a delayed poll.
        delete.assert_not_called()
        upsert.assert_not_called()

    @pytest.mark.asyncio
    async def test_admin_403_stops_without_alpha_fallback(self, monitor):
        upsert = MagicMock()
        seen = []
        with patch(
            "app.api.settings_crypto.get_decrypted_api_key", return_value=_KEY
        ), patch(
            "app.core.provider_usage.httpx.AsyncClient",
            side_effect=_mock_transport_client(
                _fal_handler(
                    billing_status=403,
                    billing_body={},
                    balance_status=200,
                    balance_body=18.11,
                    seen=seen,
                )
            ),
        ), patch("app.db.database.upsert_provider_usage_snapshot", upsert):
            await monitor._poll_fal()

        upsert.assert_not_called()
        assert [r.url.host for r in seen] == [FAL_BILLING_HOST]

    @pytest.mark.asyncio
    async def test_both_endpoints_fail_does_not_upsert(self, monitor):
        upsert = MagicMock()
        with patch(
            "app.api.settings_crypto.get_decrypted_api_key", return_value=_KEY
        ), patch(
            "app.core.provider_usage.httpx.AsyncClient",
            side_effect=_mock_transport_client(
                _fal_handler(billing_status=403, billing_body={}, balance_status=403)
            ),
        ), patch("app.db.database.upsert_provider_usage_snapshot", upsert):
            await monitor._poll_fal()
        upsert.assert_not_called()

    @pytest.mark.asyncio
    async def test_key_never_appears_in_logs(self, monitor, caplog):
        caplog.set_level(logging.DEBUG)
        with patch(
            "app.api.settings_crypto.get_decrypted_api_key", return_value=_KEY
        ), patch(
            "app.core.provider_usage.httpx.AsyncClient",
            side_effect=_mock_transport_client(
                _fal_handler(billing_status=500, billing_body={}, balance_status=500)
            ),
        ), patch("app.db.database.upsert_provider_usage_snapshot", MagicMock()):
            await monitor._poll_fal()

        assert _KEY not in caplog.text
