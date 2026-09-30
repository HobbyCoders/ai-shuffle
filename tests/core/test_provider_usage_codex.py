"""Tests for ChatGPT/Codex subscription usage polling.

Covers ``app.core.codex_usage`` (window mapping, clamping, reset fallback,
credits, the raw allowlist and the host pin) and
``ProviderUsageMonitor._poll_codex_provider`` (OAuth token from
``codex_access_token``, one refresh after a 401, failure recording, a
window-less reading never erasing the previous one, and OpenAI API-key rows
never being polled). No real network.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.core import codex_usage
from app.core.codex_rate_limit_resets import CODEX_USER_AGENT
from app.core.codex_usage import CodexUsageError, normalize_usage
from app.core.provider_usage import ProviderUsageMonitor
from app.core.usage_polling import capability_fingerprint
from app.core.usage_sources import is_codex_subscription_row, polled_sources


_TOKEN_KEY_RE = re.compile(
    r"token|authorization|bearer|cookie|secret|password|api[_-]?key",
    re.I,
)
_IDENTITY_KEY_RE = re.compile(r"user_id|account_id|email|promo", re.I)

_ACCESS = "codex-oauth-access-token"
_ACCOUNT = "acct-123"

_CODEX_ROW = {
    "id": "prov-codex-1",
    "provider_type": "chatgpt",
    "auth_mode": "chatgpt",
    "name": "ChatGPT",
    "needs_reauth": False,
}

_CODEX_TOKENS = {
    "codex_access_token": "enc:codex-access",
    "codex_account_id": _ACCOUNT,
    "needs_reauth": False,
}

_RESET_AT = 1790000000

_USAGE_BODY: Dict[str, Any] = {
    "plan_type": "pro",
    "user_id": "user-must-not-be-stored",
    "account_id": "account-must-not-be-stored",
    "email": "nope@example.com",
    "promo": {"copy": "upgrade now"},
    "rate_limit": {
        "limit_reached": False,
        "primary_window": {
            "used_percent": 12.5,
            "limit_window_seconds": 604800,
            "reset_at": _RESET_AT,
        },
        "secondary_window": {
            "used_percent": 4.0,
            "limit_window_seconds": 18000,
            "reset_at": _RESET_AT - 600000,
        },
    },
    "rate_limit_reached_type": None,
    "credits": {"has_credits": True, "balance": 3.5, "unlimited": False},
    "rate_limit_reset_credits": {"available_count": 2},
    "spend_control": {"reached": False},
}


def _assert_no_token_keys(obj: Any) -> None:
    if isinstance(obj, dict):
        for key, value in obj.items():
            assert _TOKEN_KEY_RE.search(str(key)) is None, key
            assert _IDENTITY_KEY_RE.search(str(key)) is None, key
            _assert_no_token_keys(value)
    elif isinstance(obj, list):
        for item in obj:
            _assert_no_token_keys(item)


def _windows_by_type(windows):
    return {w["type"]: w for w in windows}


def _token_patches(tokens: Optional[Dict[str, Any]] = _CODEX_TOKENS, access: str = _ACCESS):
    return (
        patch("app.db.database.get_codex_tokens", return_value=tokens),
        patch("app.core.provider_usage.encryption.is_encrypted", return_value=True),
        patch("app.core.provider_usage.encryption.decrypt_value", return_value=access),
    )


# ---------------------------------------------------------------------------
# row classification, pin and headers
# ---------------------------------------------------------------------------


class TestIsCodexSubscriptionRow:
    def test_chatgpt_provider_type(self):
        assert is_codex_subscription_row({"provider_type": "chatgpt"}) is True

    def test_chatgpt_auth_mode(self):
        assert is_codex_subscription_row({"auth_mode": "chatgpt"}) is True

    def test_openai_api_key_row_is_not(self):
        assert is_codex_subscription_row({"provider_type": "openai"}) is False
        assert is_codex_subscription_row({"provider_type": "openai", "auth_mode": "api"}) is False

    def test_empty_is_not(self):
        assert is_codex_subscription_row(None) is False
        assert is_codex_subscription_row({}) is False


class TestUsageUrlPin:
    def test_usage_url_is_pinned(self):
        assert codex_usage.USAGE_HOST == "chatgpt.com"
        assert codex_usage.url_is_pinned(codex_usage.USAGE_URL) is True

    def test_rejects_http_and_wrong_host(self):
        assert codex_usage.url_is_pinned("http://chatgpt.com/backend-api/wham/usage") is False
        assert codex_usage.url_is_pinned("https://evil.example/backend-api/wham/usage") is False
        assert codex_usage.url_is_pinned("https://chatgpt.com.evil.example/usage") is False

    def test_registry_lists_the_same_url(self):
        source = {s.id: s for s in polled_sources(_CODEX_ROW)}["codex_usage"]
        assert [e.url for e in source.endpoints] == [codex_usage.USAGE_URL]

    def test_chatgpt_row_is_polled_and_openai_row_is_not(self):
        assert "codex_usage" in {s.id for s in polled_sources(_CODEX_ROW)}
        assert "codex_usage" not in {
            s.id for s in polled_sources({"provider_type": "openai", "auth_mode": "api"})
        }


class TestUsageHeaders:
    def test_bearer_agent_and_account(self):
        headers = codex_usage.usage_headers(_ACCESS, _ACCOUNT)
        assert headers["Authorization"] == f"Bearer {_ACCESS}"
        assert headers["User-Agent"] == CODEX_USER_AGENT
        assert headers["ChatGPT-Account-Id"] == _ACCOUNT

    def test_account_header_omitted_when_absent_or_blank(self):
        assert "ChatGPT-Account-Id" not in codex_usage.usage_headers(_ACCESS, None)
        assert "ChatGPT-Account-Id" not in codex_usage.usage_headers(_ACCESS, "   ")


class TestWindowTypeForMinutes:
    def test_known_windows(self):
        assert codex_usage.window_type_for_minutes(10080) == "seven_day"
        assert codex_usage.window_type_for_minutes(300) == "five_hour"

    def test_unknown_window_keeps_its_length(self):
        assert codex_usage.window_type_for_minutes(60) == "window_60m"

    def test_absent_or_nonpositive_has_no_type(self):
        assert codex_usage.window_type_for_minutes(None) is None
        assert codex_usage.window_type_for_minutes(0) is None
        assert codex_usage.window_type_for_minutes(-5) is None

    def test_shared_with_the_header_capture_mapping(self):
        from app.core import provider_usage

        assert provider_usage._window_type_for_minutes is codex_usage.window_type_for_minutes


# ---------------------------------------------------------------------------
# normalize_usage
# ---------------------------------------------------------------------------


class TestNormalizeUsage:
    def test_full_body_maps_both_windows_plan_and_credits(self):
        u = normalize_usage(_USAGE_BODY)
        by_type = _windows_by_type(u.windows)
        assert by_type["seven_day"]["utilization"] == 12.5
        assert by_type["seven_day"]["resets_at"] == str(_RESET_AT)
        assert by_type["five_hour"]["utilization"] == 4.0
        assert u.plan_type == "pro"
        assert u.credits == {"has_credits": True, "balance": 3.5, "unlimited": False}

    def test_primary_window_is_first(self):
        assert [w["type"] for w in normalize_usage(_USAGE_BODY).windows] == [
            "seven_day",
            "five_hour",
        ]

    def test_reset_after_seconds_is_used_when_reset_at_is_absent(self):
        body = {
            "rate_limit": {
                "primary_window": {
                    "used_percent": 1,
                    "limit_window_seconds": 604800,
                    "reset_after_seconds": 120,
                }
            }
        }
        u = normalize_usage(body, now=1_000_000.0)
        assert _windows_by_type(u.windows)["seven_day"]["resets_at"] == "1000120"

    def test_window_without_a_reset_still_reports_utilization(self):
        u = normalize_usage(
            {"rate_limit": {"primary_window": {"used_percent": 9, "limit_window_seconds": 604800}}}
        )
        assert _windows_by_type(u.windows)["seven_day"]["resets_at"] is None

    def test_utilization_is_clamped(self):
        high = normalize_usage(
            {"rate_limit": {"primary_window": {"used_percent": 143, "limit_window_seconds": 604800}}}
        )
        low = normalize_usage(
            {"rate_limit": {"primary_window": {"used_percent": -8, "limit_window_seconds": 604800}}}
        )
        assert high.windows[0]["utilization"] == 100.0
        assert low.windows[0]["utilization"] == 0.0

    def test_window_without_a_length_is_dropped(self):
        # Without the length, a weekly allowance and a five-hour one look alike.
        u = normalize_usage({"rate_limit": {"primary_window": {"used_percent": 50}}})
        assert u.windows == []

    def test_window_without_a_percentage_is_dropped(self):
        u = normalize_usage({"rate_limit": {"primary_window": {"limit_window_seconds": 604800}}})
        assert u.windows == []

    def test_unknown_window_length_keeps_its_own_type(self):
        u = normalize_usage(
            {"rate_limit": {"primary_window": {"used_percent": 3, "limit_window_seconds": 3600}}}
        )
        assert u.windows[0]["type"] == "window_60m"

    def test_missing_secondary_window_is_normal(self):
        body = {"rate_limit": {"primary_window": dict(_USAGE_BODY["rate_limit"]["primary_window"])}}
        u = normalize_usage(body)
        assert [w["type"] for w in u.windows] == ["seven_day"]

    def test_empty_body_yields_no_windows_rather_than_raising(self):
        u = normalize_usage({})
        assert u.windows == []
        assert u.plan_type is None

    def test_non_object_body_is_an_invalid_contract(self):
        for bad in (None, "nope", [], 7):
            with pytest.raises(CodexUsageError) as excinfo:
                normalize_usage(bad)
            assert excinfo.value.kind == "invalid_contract"

    def test_raw_is_allowlisted_and_carries_no_identity_or_credential(self):
        body = dict(_USAGE_BODY)
        body["Authorization"] = "Bearer leaked"
        body["access_token"] = "tok"
        u = normalize_usage(body)
        _assert_no_token_keys(u.raw)
        _assert_no_token_keys(u.credits)
        assert u.raw["source"] == "codex_usage"
        assert u.raw["plan_type"] == "pro"
        assert u.raw["reset_credits_available"] == 2
        assert set(u.raw) <= {
            "source",
            "plan_type",
            "limit_reached",
            "rate_limit_reached_type",
            "spend_control_reached",
            "reset_credits_available",
        }
        assert "leaked" not in str(u.raw)
        assert "user-must-not-be-stored" not in str(u.raw)
        assert "nope@example.com" not in str(u.raw)
        assert "upgrade now" not in str(u.raw)

    def test_garbage_subsections_degrade_instead_of_failing(self):
        u = normalize_usage(
            {"plan_type": 7, "rate_limit": "nope", "credits": [], "spend_control": None}
        )
        assert u.windows == []
        assert u.plan_type is None
        assert u.credits == {"has_credits": None, "balance": None, "unlimited": None}


# ---------------------------------------------------------------------------
# _poll_codex_provider
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


def _usage_handler(status=200, body=None, seen=None):
    def _handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        assert request.url.scheme == "https"
        assert request.url.host == codex_usage.USAGE_HOST
        assert request.headers.get("authorization") == f"Bearer {_ACCESS}"
        assert request.headers.get("chatgpt-account-id") == _ACCOUNT
        assert request.headers.get("user-agent") == CODEX_USER_AGENT
        return httpx.Response(status, json=_USAGE_BODY if body is None else body)

    return _handler


class TestPollCodexProvider:
    @pytest.mark.asyncio
    async def test_200_upserts_a_chatgpt_snapshot(self, monitor):
        upsert = MagicMock()
        seen, recorded = [], []
        t1, t2, t3 = _token_patches()
        with t1, t2, t3, patch(
            "app.core.provider_usage.httpx.AsyncClient",
            side_effect=_mock_transport_client(_usage_handler(seen=seen), recorded=recorded),
        ), patch("app.db.database.get_provider_usage_raw", return_value=None), patch(
            "app.db.database.upsert_provider_usage_snapshot", upsert
        ):
            await monitor._poll_codex_provider(_CODEX_ROW)

        upsert.assert_called_once()
        provider_id, ptype, plan, windows, credits, raw, fetched_at = upsert.call_args[0]
        assert provider_id == "prov-codex-1"
        assert ptype == "chatgpt"
        assert plan == "pro"
        assert _windows_by_type(windows)["seven_day"]["utilization"] == 12.5
        assert raw["source"] == "codex_usage"
        _assert_no_token_keys(raw)
        _assert_no_token_keys(credits)
        assert fetched_at
        assert [r.url.path for r in seen] == ["/backend-api/wham/usage"]
        assert all(kwargs.get("follow_redirects") is False for kwargs in recorded)

    @pytest.mark.asyncio
    async def test_401_refreshes_once_then_retries(self, monitor):
        upsert = MagicMock()
        statuses = [401, 200]

        async def get(_headers):
            return _USAGE_BODY, statuses.pop(0)

        refresh = AsyncMock(return_value=True)
        t1, t2, t3 = _token_patches()
        with t1, t2, t3, patch(
            "app.core.provider_usage._codex_usage_get", new=AsyncMock(side_effect=get)
        ), patch(
            "app.core.provider_usage._maybe_refresh_codex_on_401", new=refresh
        ), patch("app.db.database.get_provider_usage_raw", return_value=None), patch(
            "app.db.database.upsert_provider_usage_snapshot", upsert
        ):
            await monitor._poll_codex_provider(_CODEX_ROW)

        refresh.assert_awaited_once()
        assert statuses == []
        upsert.assert_called_once()

    @pytest.mark.asyncio
    async def test_401_with_failed_refresh_records_denial_and_does_not_upsert(self, monitor):
        upsert = MagicMock()
        failure = MagicMock()
        t1, t2, t3 = _token_patches()
        with t1, t2, t3, patch(
            "app.core.provider_usage._codex_usage_get", new=AsyncMock(return_value=({}, 401))
        ), patch(
            "app.core.provider_usage._maybe_refresh_codex_on_401",
            new=AsyncMock(return_value=False),
        ), patch("app.db.database.get_provider_usage_raw", return_value=None), patch(
            "app.db.database.record_provider_usage_failure", failure
        ), patch("app.db.database.upsert_provider_usage_snapshot", upsert):
            await monitor._poll_codex_provider(_CODEX_ROW)  # must not raise

        upsert.assert_not_called()
        failure.assert_called_once()
        assert failure.call_args[0][1:] == ("chatgpt", "billing_access_denied")
        assert failure.call_args[1]["denied_fingerprint"] == capability_fingerprint(
            "codex_usage", codex_usage.usage_headers(_ACCESS, _ACCOUNT)
        )

    @pytest.mark.asyncio
    async def test_known_denied_fingerprint_is_not_reprobed(self, monitor):
        fingerprint = capability_fingerprint(
            "codex_usage", codex_usage.usage_headers(_ACCESS, _ACCOUNT)
        )
        upsert = MagicMock()
        t1, t2, t3 = _token_patches()
        with t1, t2, t3, patch(
            "app.db.database.get_provider_usage_raw",
            return_value={"denied_fingerprint": fingerprint},
        ), patch(
            "app.core.provider_usage.httpx.AsyncClient",
            side_effect=AssertionError("must not re-probe a denied capability"),
        ), patch("app.db.database.upsert_provider_usage_snapshot", upsert):
            await monitor._poll_codex_provider(_CODEX_ROW)

        upsert.assert_not_called()

    @pytest.mark.asyncio
    async def test_500_records_a_failure_and_does_not_upsert(self, monitor):
        upsert = MagicMock()
        failure = MagicMock()
        t1, t2, t3 = _token_patches()
        with t1, t2, t3, patch(
            "app.core.provider_usage._codex_usage_get", new=AsyncMock(return_value=(None, 500))
        ), patch("app.db.database.get_provider_usage_raw", return_value=None), patch(
            "app.db.database.record_provider_usage_failure", failure
        ), patch("app.db.database.upsert_provider_usage_snapshot", upsert):
            await monitor._poll_codex_provider(_CODEX_ROW)

        upsert.assert_not_called()
        failure.assert_called_once()
        assert failure.call_args[0][2] == "unavailable"
        assert failure.call_args[1]["denied_fingerprint"] is None

    @pytest.mark.asyncio
    async def test_window_less_reading_never_erases_the_previous_snapshot(self, monitor):
        upsert = MagicMock()
        failure = MagicMock()
        t1, t2, t3 = _token_patches()
        with t1, t2, t3, patch(
            "app.core.provider_usage._codex_usage_get",
            new=AsyncMock(return_value=({"plan_type": "pro"}, 200)),
        ), patch("app.db.database.get_provider_usage_raw", return_value=None), patch(
            "app.db.database.record_provider_usage_failure", failure
        ), patch("app.db.database.upsert_provider_usage_snapshot", upsert):
            await monitor._poll_codex_provider(_CODEX_ROW)

        upsert.assert_not_called()
        failure.assert_not_called()

    @pytest.mark.asyncio
    async def test_needs_reauth_row_skips(self, monitor):
        upsert = MagicMock()
        with patch(
            "app.db.database.get_codex_tokens",
            return_value={"codex_access_token": "enc:x", "needs_reauth": True},
        ), patch(
            "app.core.provider_usage.httpx.AsyncClient",
            side_effect=AssertionError("must not HTTP when needs_reauth"),
        ), patch("app.db.database.upsert_provider_usage_snapshot", upsert):
            await monitor._poll_codex_provider(_CODEX_ROW)

        upsert.assert_not_called()

    @pytest.mark.asyncio
    async def test_row_without_access_token_skips(self, monitor):
        upsert = MagicMock()
        t1, t2, t3 = _token_patches(tokens={"codex_access_token": None, "needs_reauth": False})
        with t1, t2, t3, patch(
            "app.core.provider_usage.httpx.AsyncClient",
            side_effect=AssertionError("must not HTTP without a token"),
        ), patch("app.db.database.upsert_provider_usage_snapshot", upsert):
            await monitor._poll_codex_provider(_CODEX_ROW)

        upsert.assert_not_called()

    @pytest.mark.asyncio
    async def test_deferred_budget_is_swallowed(self, monitor):
        from app.core.usage_polling import UsageDeferred

        upsert = MagicMock()
        t1, t2, t3 = _token_patches()
        with t1, t2, t3, patch(
            "app.core.provider_usage._codex_usage_get",
            new=AsyncMock(side_effect=UsageDeferred()),
        ), patch("app.db.database.get_provider_usage_raw", return_value=None), patch(
            "app.db.database.upsert_provider_usage_snapshot", upsert
        ):
            await monitor._poll_codex_provider(_CODEX_ROW)  # must not raise

        upsert.assert_not_called()

    @pytest.mark.asyncio
    async def test_bearer_token_never_appears_in_logs(self, monitor, caplog):
        t1, t2, t3 = _token_patches()
        with t1, t2, t3, patch(
            "app.core.provider_usage._codex_usage_get", new=AsyncMock(return_value=({}, 500))
        ), patch("app.db.database.get_provider_usage_raw", return_value=None), patch(
            "app.db.database.record_provider_usage_failure", MagicMock()
        ), patch("app.db.database.upsert_provider_usage_snapshot", MagicMock()):
            with caplog.at_level(logging.DEBUG):
                await monitor._poll_codex_provider(_CODEX_ROW)

        assert _ACCESS not in caplog.text
        assert "enc:codex-access" not in caplog.text


class TestPollOnceDispatch:
    @pytest.mark.asyncio
    async def test_chatgpt_row_is_polled_and_openai_api_row_is_not(self, monitor):
        rows = [
            {"id": "p-openai", "provider_type": "openai", "auth_mode": "api"},
            {"id": "p-codex", "provider_type": "chatgpt", "auth_mode": "chatgpt"},
        ]
        with patch("app.db.database.get_enabled_model_providers", return_value=rows), \
             patch.object(monitor, "_poll_provider", new=AsyncMock()), \
             patch.object(monitor, "_poll_grok_provider", new=AsyncMock()) as poll_grok, \
             patch.object(monitor, "_poll_openai_provider", new=AsyncMock()), \
             patch.object(monitor, "_poll_codex_provider", new=AsyncMock()) as poll_codex, \
             patch.object(monitor, "_poll_fal", new=AsyncMock()):
            await monitor.poll_once()

        poll_grok.assert_not_called()
        poll_codex.assert_called_once_with(rows[1])

@pytest.mark.parametrize("balance, expected", [(12.5, {"balance": 12.5}), (0, None), (-1, None), ("Infinity", None), (True, None)])
async def test_codex_polled_extra_credits_reach_usage_api(mock_db, monkeypatch, balance, expected):
    from app.api.usage import _load_provider_usage
    from app.core import provider_usage as producer
    from app.db import database as db
    import httpx

    row = db.create_model_provider("chat-extra", "ChatGPT", "chatgpt", "https://chatgpt.com")
    monkeypatch.setattr(producer.ProviderUsageMonitor, "_codex_credentials", staticmethod(lambda _: _credentials()))

    async def fetch(*_, **__):
        return httpx.Response(200, json={"plan_type": "pro", "rate_limit": {"secondary_window": {"used_percent": 29, "limit_window_seconds": 604800, "reset_at": 1791247630}}, "credits": {"balance": balance}})

    async def _credentials():
        return "test-token-only", "test-account-only"

    monkeypatch.setattr(producer, "_registered_get", fetch)
    await producer.ProviderUsageMonitor()._poll_codex_provider(row)
    entries = {s.provider_id: s for s in await _load_provider_usage()}
    assert entries["chat-extra"].credits == expected
    assert entries["chat-extra"].windows[0].type == "seven_day"
    assert entries["chat-extra"].windows[0].utilization == 29
