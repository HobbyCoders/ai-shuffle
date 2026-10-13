"""OpenAI platform row classification and the retired dashboard billing reader.

The undocumented ``dashboard/billing/credit_grants`` reader was removed; an
ordinary platform key never triggers any usage request. No live network.
"""

from __future__ import annotations

import logging
import re
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.core import provider_usage
from app.core.provider_usage import (
    ProviderUsageMonitor,
    _is_openai_platform_row,
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

    def test_dashboard_billing_reader_is_gone(self):
        assert not hasattr(provider_usage, "OPENAI_CREDIT_GRANTS_URL")
        assert not hasattr(provider_usage, "normalize_openai_credit_grants")


@pytest.mark.asyncio
async def test_unconfigured_platform_key_never_reads_dashboard(mock_db):
    from app.db import database as db
    row = db.create_model_provider("openai-test", "OpenAI", "openai", "https://api.openai.com", auth_token="ordinary-key")
    with patch("app.core.provider_usage.usage_get", new=AsyncMock()) as get:
        assert not await ProviderUsageMonitor()._poll_openai_provider(row)
    get.assert_not_awaited()
