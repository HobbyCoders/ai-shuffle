"""Tests for fal.ai balance polling (app/core/provider_usage.py).

Covers ``normalize_fal_user_balance`` (bare and wrapped balances, unusable
payloads) and ``ProviderUsageMonitor._poll_fal`` (the ordinary ``fal_api_key``,
the pinned balance URL, Key auth, 200 upsert under the synthetic ``fal-ai``
id, refusals, credential never logged). No live network.
"""

from __future__ import annotations

import logging
import math
from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.core.provider_usage import (
    FAL_BALANCE_HOST,
    FAL_BALANCE_URL,
    FAL_USAGE_PROVIDER_ID,
    FAL_USAGE_PROVIDER_NAME,
    FAL_USAGE_PROVIDER_TYPE,
    ProviderUsageMonitor,
    _fal_auth_header,
    _fal_balance_url_is_pinned,
    normalize_fal_user_balance,
)

_KEY = "fal-secret-must-never-log"
# The documented Admin billing API. The ordinary key is never sent there.
FAL_ADMIN_BILLING_HOST = "api.fal.ai"


class TestNormalizeFalUserBalance:
    @pytest.mark.parametrize("payload, expected", [
        (18.11, 18.11),
        (0, 0.0),
        ("24.5", 24.5),
        ('"24.5"', 24.5),
        ({"balance": 3.5}, 3.5),
        ({"user_balance": "7"}, 7.0),
        ({"current_balance": 1.25}, 1.25),
        ({"credits": {"current_balance": 9}}, 9.0),
    ])
    def test_reads_bare_and_wrapped_balances(self, payload, expected):
        assert normalize_fal_user_balance(payload) == {"balance_remaining": expected}

    def test_keeps_a_reported_currency(self):
        assert normalize_fal_user_balance({"balance": 2, "currency": "USD"}) == {
            "balance_remaining": 2.0, "currency": "USD",
        }

    @pytest.mark.parametrize("payload", [
        None, True, [], "nope", "", "nan", "inf", math.nan, math.inf, {}, {"balance": None},
        {"balance": True}, {"credits": {}}, {"credits": {"currency": "USD"}},
    ])
    def test_unusable_payload_returns_none(self, payload):
        assert normalize_fal_user_balance(payload) is None


class TestFalAuthAndPin:
    def test_auth_header_prefixes_key(self):
        assert _fal_auth_header("abc") == "Key abc"
        assert _fal_auth_header("Key abc") == "Key abc"
        assert _fal_auth_header("key abc") == "key abc"

    def test_balance_url_is_pinned(self):
        assert FAL_BALANCE_URL == "https://rest.alpha.fal.ai/billing/user_balance"
        assert _fal_balance_url_is_pinned(FAL_BALANCE_URL) is True
        assert _fal_balance_url_is_pinned("http://rest.alpha.fal.ai/billing/user_balance") is False
        assert _fal_balance_url_is_pinned("https://evil.example/billing/user_balance") is False
        assert _fal_balance_url_is_pinned("https://api.fal.ai/v1/account/billing") is False


@pytest.fixture
def monitor():
    return ProviderUsageMonitor()


@pytest.fixture(autouse=True)
def _credential_generation(mock_db, monkeypatch):
    from app.db import database as db
    for name in ("FAL_KEY", "FAL_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    db.set_system_setting("fal_api_key", _KEY)


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


def _fal_handler(status=200, body=18.11, seen=None, as_text=False):
    def _handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        assert request.url.scheme == "https"
        assert request.headers.get("authorization") == f"Key {_KEY}"
        if request.url.host == FAL_BALANCE_HOST:
            assert request.url.path == "/billing/user_balance"
            if as_text:
                return httpx.Response(status, text=str(body))
            return httpx.Response(status, json=body)
        return httpx.Response(404, json={})

    return _handler


def _poll_patches(handler, *, key=_KEY, recorded=None):
    return (
        patch("app.api.settings_crypto.get_decrypted_api_key", return_value=key),
        patch("app.core.provider_usage.httpx.AsyncClient",
              side_effect=_mock_transport_client(handler, recorded)),
    )


class TestPollFal:
    @pytest.mark.asyncio
    async def test_200_upserts_synthetic_snapshot_from_the_ordinary_key(self, monitor):
        upsert = MagicMock()
        seen = []
        recorded = []
        key_patch, client_patch = _poll_patches(_fal_handler(seen=seen), recorded=recorded)
        with key_patch as read_key, client_patch, patch(
            "app.db.database.upsert_provider_usage_snapshot", upsert
        ), patch("app.db.database.delete_provider_usage_snapshot", MagicMock()) as delete:
            await monitor._poll_fal()

        read_key.assert_called_once_with("fal_api_key")
        delete.assert_not_called()
        upsert.assert_called_once()
        provider_id, provider_type, plan_type, windows, credits, raw, fetched_at = (
            upsert.call_args[0]
        )
        assert provider_id == FAL_USAGE_PROVIDER_ID == "fal-ai"
        assert provider_type == FAL_USAGE_PROVIDER_TYPE
        assert FAL_USAGE_PROVIDER_NAME == "fal.ai"
        assert plan_type is None
        assert windows == []
        assert credits == {"balance_remaining": 18.11}
        assert raw == {"source": "fal_user_balance"}
        assert isinstance(fetched_at, int)
        assert recorded and recorded[0].get("follow_redirects") is False
        assert [r.url.host for r in seen] == [FAL_BALANCE_HOST]

    @pytest.mark.asyncio
    async def test_plain_text_balance_is_read(self, monitor):
        upsert = MagicMock()
        key_patch, client_patch = _poll_patches(_fal_handler(body="5.75", as_text=True))
        with key_patch, client_patch, patch("app.db.database.upsert_provider_usage_snapshot", upsert):
            await monitor._poll_fal()
        assert upsert.call_args[0][4] == {"balance_remaining": 5.75}

    @pytest.mark.asyncio
    async def test_missing_key_does_not_publish_a_snapshot(self, monitor):
        delete = MagicMock(return_value=True)
        upsert = MagicMock()
        seen = []
        key_patch, client_patch = _poll_patches(_fal_handler(seen=seen), key=None)
        with key_patch, client_patch, patch(
            "app.db.database.delete_provider_usage_snapshot", delete
        ), patch("app.db.database.upsert_provider_usage_snapshot", upsert):
            await monitor._poll_fal()

        # Removal invalidates in the credential transaction, not a delayed poll.
        delete.assert_not_called()
        upsert.assert_not_called()
        assert seen == []

    @pytest.mark.asyncio
    async def test_retired_billing_key_alone_is_never_used(self, monitor, mock_db):
        from app.db import database as db

        db.delete_system_setting("fal_api_key")
        db.set_system_setting("fal_billing_api_key", "old-admin-key")
        seen = []
        upsert = MagicMock()
        with patch("app.core.provider_usage.httpx.AsyncClient",
                   side_effect=_mock_transport_client(_fal_handler(seen=seen))), patch(
            "app.db.database.upsert_provider_usage_snapshot", upsert
        ):
            await monitor._poll_fal()
        assert seen == []
        upsert.assert_not_called()

    @pytest.mark.asyncio
    async def test_403_records_denial_without_a_balance(self, monitor):
        upsert = MagicMock()
        failure = MagicMock()
        seen = []
        key_patch, client_patch = _poll_patches(_fal_handler(status=403, body={}, seen=seen))
        with key_patch, client_patch, patch(
            "app.db.database.upsert_provider_usage_snapshot", upsert
        ), patch("app.db.database.record_provider_usage_failure", failure):
            await monitor._poll_fal()

        upsert.assert_not_called()
        assert failure.call_args[0][:3] == (FAL_USAGE_PROVIDER_ID, FAL_USAGE_PROVIDER_TYPE, "billing_access_denied")
        assert failure.call_args.kwargs["denied_fingerprint"]
        assert [r.url.host for r in seen] == [FAL_BALANCE_HOST]
        assert FAL_ADMIN_BILLING_HOST not in {r.url.host for r in seen}

    @pytest.mark.asyncio
    async def test_unreadable_balance_records_unavailable(self, monitor):
        upsert = MagicMock()
        failure = MagicMock()
        key_patch, client_patch = _poll_patches(_fal_handler(body={"nothing": 1}))
        with key_patch, client_patch, patch(
            "app.db.database.upsert_provider_usage_snapshot", upsert
        ), patch("app.db.database.record_provider_usage_failure", failure):
            await monitor._poll_fal()
        upsert.assert_not_called()
        assert failure.call_args[0][2] == "unavailable"

    @pytest.mark.asyncio
    async def test_key_never_appears_in_logs(self, monitor, caplog):
        caplog.set_level(logging.DEBUG)
        key_patch, client_patch = _poll_patches(_fal_handler(status=500, body={}))
        with key_patch, client_patch, patch(
            "app.db.database.upsert_provider_usage_snapshot", MagicMock()
        ):
            await monitor._poll_fal()

        assert _KEY not in caplog.text
