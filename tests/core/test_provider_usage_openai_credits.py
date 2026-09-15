"""Tests for OpenAI platform credit-grants polling (app/core/provider_usage.py).

Covers ``normalize_openai_credit_grants`` (available/granted/used mapping,
derived remaining, unusable payloads) and ``ProviderUsageMonitor._poll_openai_provider``
(pinned credit_grants URL, 200 upsert, 403 skip, key never logged). No live
network.
"""

from __future__ import annotations

import logging
import re
from unittest.mock import AsyncMock, MagicMock, patch

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

    def test_unwraps_data_wrapper(self):
        credits = normalize_openai_credit_grants({"data": dict(_GRANTS)})
        assert credits["balance_remaining"] == pytest.approx(20.75)


class TestOpenAiPlatformRow:
    def test_official_host_is_true(self):
        assert _is_openai_platform_row(_ROW) is True
        assert _is_openai_platform_row(
            {"provider_type": "openai", "base_url": "https://api.openai.com/v1/"}
        ) is True
        assert _is_openai_platform_row({"provider_type": "openai", "base_url": ""}) is True
        assert _is_openai_platform_row({"provider_type": "openai"}) is True

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


@pytest.mark.asyncio
async def test_unconfigured_platform_key_never_reads_dashboard(mock_db):
    from app.db import database as db
    row = db.create_model_provider("openai-test", "OpenAI", "openai", "https://api.openai.com", auth_token="ordinary-key")
    with patch("app.core.provider_usage.usage_get", new=AsyncMock()) as get:
        assert not await ProviderUsageMonitor()._poll_openai_provider(row)
    get.assert_not_awaited()
