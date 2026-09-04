"""Tests for OpenAI platform credit-grants polling (app/core/provider_usage.py).

Covers ``normalize_openai_credit_grants`` (available/granted/used mapping,
derived remaining, unusable payloads) and ``ProviderUsageMonitor._poll_openai_provider``
(pinned credit_grants URL, 200 upsert, 403 skip, key never logged). No live
network.
"""

from __future__ import annotations

import logging
import re
from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.core.provider_usage import (
    OPENAI_CREDIT_GRANTS_HOST,
    OPENAI_CREDIT_GRANTS_URL,
    ProviderUsageMonitor,
    _is_openai_platform_row,
    _openai_credit_grants_url_is_pinned,
    normalize_openai_credit_grants,
)


_TOKEN_KEY_RE = re.compile(
    r"token|authorization|bearer|cookie|secret|password|api[_-]?key",
    re.I,
)

_KEY = "sk-openai-secret-must-never-log"
_ROW = {
    "id": "prov-openai-1",
    "provider_type": "openai",
    "base_url": "https://api.openai.com/v1",
    "auth_token": "enc:openai-key",
}
_GRANTS = {
    "total_granted": 25.0,
    "total_used": 4.25,
    "total_available": 20.75,
    "grants": {"data": [{"grant_amount": 25.0, "used_amount": 4.25}]},
}


class TestNormalizeOpenAiCreditGrants:
    def test_maps_available_to_balance_remaining(self):
        credits = normalize_openai_credit_grants(_GRANTS)
        assert credits == {
            "balance_remaining": 20.75,
            "total_available": 20.75,
            "total_granted": 25.0,
            "total_credits": 25.0,
            "total_used": 4.25,
            "total_usage": 4.25,
        }

    def test_derives_available_from_granted_minus_used(self):
        credits = normalize_openai_credit_grants(
            {"total_granted": 10.0, "total_used": 2.5}
        )
        assert credits["balance_remaining"] == pytest.approx(7.5)
        assert credits["total_available"] == pytest.approx(7.5)

    def test_available_only_is_enough(self):
        credits = normalize_openai_credit_grants({"total_available": 3.0})
        assert credits == {"balance_remaining": 3.0, "total_available": 3.0}

    def test_zero_available_is_displayable(self):
        credits = normalize_openai_credit_grants(
            {"total_granted": 5.0, "total_used": 5.0, "total_available": 0.0}
        )
        assert credits["balance_remaining"] == 0.0

    def test_numeric_strings_are_coerced(self):
        credits = normalize_openai_credit_grants(
            {"total_granted": "25", "total_used": "4.25", "total_available": "20.75"}
        )
        assert credits["balance_remaining"] == pytest.approx(20.75)

    def test_grants_array_never_copied(self):
        credits = normalize_openai_credit_grants(_GRANTS)
        assert "grants" not in credits

    @pytest.mark.parametrize(
        "payload",
        [None, [], "nope", {}, {"total_granted": 10.0}, {"total_used": 1.0}],
    )
    def test_unusable_payload_returns_none(self, payload):
        assert normalize_openai_credit_grants(payload) is None


class TestOpenAiPlatformRow:
    def test_official_host_is_true(self):
        assert _is_openai_platform_row(_ROW) is True
        assert _is_openai_platform_row(
            {"provider_type": "openai", "base_url": "https://api.openai.com/v1/"}
        ) is True

    def test_custom_host_is_false(self):
        assert _is_openai_platform_row(
            {"provider_type": "openai", "base_url": "https://my-proxy.example/v1"}
        ) is False

    def test_other_types_are_false(self):
        assert _is_openai_platform_row({"provider_type": "chatgpt"}) is False
        assert _is_openai_platform_row({"provider_type": "xai"}) is False
        assert _is_openai_platform_row(None) is False

    def test_credit_grants_url_is_pinned(self):
        assert _openai_credit_grants_url_is_pinned(OPENAI_CREDIT_GRANTS_URL) is True
        assert _openai_credit_grants_url_is_pinned(
            "http://api.openai.com/v1/dashboard/billing/credit_grants"
        ) is False
        assert _openai_credit_grants_url_is_pinned(
            "https://evil.example/v1/dashboard/billing/credit_grants"
        ) is False


@pytest.fixture
def monitor():
    return ProviderUsageMonitor()


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


def _grants_handler(status=200, body=None, seen=None):
    def _handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        assert request.url.host == OPENAI_CREDIT_GRANTS_HOST
        assert request.url.scheme == "https"
        assert request.url.path == "/v1/dashboard/billing/credit_grants"
        assert request.headers.get("authorization") == f"Bearer {_KEY}"
        return httpx.Response(status, json=body if body is not None else _GRANTS)

    return _handler


class TestPollOpenAiProvider:
    @pytest.mark.asyncio
    async def test_200_upserts_credits_only_snapshot(self, monitor):
        upsert = MagicMock()
        seen = []
        recorded = []
        with patch(
            "app.core.provider_usage._decrypt_provider_auth_token", return_value=_KEY
        ), patch(
            "app.core.provider_usage.httpx.AsyncClient",
            side_effect=_mock_transport_client(_grants_handler(seen=seen), recorded),
        ), patch("app.db.database.upsert_provider_usage_snapshot", upsert):
            await monitor._poll_openai_provider(_ROW)

        upsert.assert_called_once()
        provider_id, provider_type, plan_type, windows, credits, raw, fetched_at = (
            upsert.call_args[0]
        )
        assert provider_id == "prov-openai-1"
        assert provider_type == "openai"
        assert plan_type is None
        assert windows == []
        assert credits["balance_remaining"] == pytest.approx(20.75)
        assert credits["total_usage"] == pytest.approx(4.25)
        assert raw == {
            "total_granted": 25.0,
            "total_used": 4.25,
            "total_available": 20.75,
        }
        assert "grants" not in raw
        assert isinstance(fetched_at, int)
        assert recorded and recorded[0].get("follow_redirects") is False
        assert len(seen) == 1

    @pytest.mark.asyncio
    async def test_403_does_not_raise_or_upsert(self, monitor):
        upsert = MagicMock()
        with patch(
            "app.core.provider_usage._decrypt_provider_auth_token", return_value=_KEY
        ), patch(
            "app.core.provider_usage.httpx.AsyncClient",
            side_effect=_mock_transport_client(_grants_handler(status=403, body={})),
        ), patch("app.db.database.upsert_provider_usage_snapshot", upsert):
            await monitor._poll_openai_provider(_ROW)

        upsert.assert_not_called()

    @pytest.mark.asyncio
    async def test_401_does_not_raise_or_upsert(self, monitor):
        upsert = MagicMock()
        with patch(
            "app.core.provider_usage._decrypt_provider_auth_token", return_value=_KEY
        ), patch(
            "app.core.provider_usage.httpx.AsyncClient",
            side_effect=_mock_transport_client(_grants_handler(status=401, body={})),
        ), patch("app.db.database.upsert_provider_usage_snapshot", upsert):
            await monitor._poll_openai_provider(_ROW)
        upsert.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_key_skips(self, monitor):
        upsert = MagicMock()
        with patch(
            "app.core.provider_usage._decrypt_provider_auth_token", return_value=None
        ), patch("app.db.database.upsert_provider_usage_snapshot", upsert):
            await monitor._poll_openai_provider(_ROW)
        upsert.assert_not_called()

    @pytest.mark.asyncio
    async def test_bearer_key_never_appears_in_logs(self, monitor, caplog):
        caplog.set_level(logging.DEBUG)
        with patch(
            "app.core.provider_usage._decrypt_provider_auth_token", return_value=_KEY
        ), patch(
            "app.core.provider_usage.httpx.AsyncClient",
            side_effect=_mock_transport_client(_grants_handler(status=500, body={})),
        ), patch("app.db.database.upsert_provider_usage_snapshot", MagicMock()):
            await monitor._poll_openai_provider(_ROW)

        assert _KEY not in caplog.text
        for record in caplog.records:
            assert _TOKEN_KEY_RE.search(record.getMessage()) is None or _KEY not in record.getMessage()
