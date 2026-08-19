"""Unit coverage for SuperGrok banked reset-credit inventory + consume.

All network I/O uses ``httpx.MockTransport``. No test reaches the live network.
"""

from datetime import datetime
from urllib.parse import urlsplit

import httpx
import pytest

from app.core import grok_rate_limit_resets as grok
from app.core.grok_rate_limit_resets import (
    BILLING_URL,
    CONSUME_RESET_CREDIT_URL,
    DEFAULT_DESCRIPTION,
    DEFAULT_TITLE,
    GROK_BILLING_HOST,
    GROK_TOKEN_AUTH,
    GrokResetCreditsError,
    RESET_CREDITS_URL,
    RESET_TOKENS_URL,
    consume_reset_credit,
    get_reset_credits,
    normalize_consume,
    normalize_inventory,
)

ACCESS_TOKEN = "grok-reset-access-secret"


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _rfc3339_unix(value: str) -> int:
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())


class TestNormalizeInventory:
    def test_codex_like_json(self):
        payload = {
            "available_count": 1,
            "credits": [
                {
                    "id": "credit-1",
                    "reset_type": "grok_rate_limits",
                    "status": "available",
                    "granted_at": "2026-06-17T00:00:00Z",
                    "expires_at": "2026-09-13T00:00:00Z",
                    "title": "Usage Limit Reset",
                    "description": "Ready",
                }
            ],
        }
        result = normalize_inventory(payload)
        assert result["available_count"] == 1
        assert result["credits"][0]["id"] == "credit-1"
        assert result["credits"][0]["reset_type"] == "grok_rate_limits"
        assert result["credits"][0]["granted_at"] == _rfc3339_unix(
            "2026-06-17T00:00:00+00:00"
        )

    def test_grok_com_available_count_tokens_shape(self):
        payload = {
            "availableCount": 1,
            "tokens": [{"tokenId": "tok-1", "expiresAt": "2026-09-13T00:00:00Z"}],
        }
        result = normalize_inventory(payload)
        assert result["available_count"] == 1
        assert result["credits"] == [
            {
                "id": "tok-1",
                "reset_type": "grok_rate_limits",
                "status": "available",
                "granted_at": 0,
                "expires_at": _rfc3339_unix("2026-09-13T00:00:00+00:00"),
                "title": DEFAULT_TITLE,
                "description": DEFAULT_DESCRIPTION,
            }
        ]

    def test_nested_config_reset_tokens(self):
        payload = {
            "config": {
                "resetTokens": [
                    {"id": "nested-1", "expiry": 1789257600, "status": "unused"}
                ]
            }
        }
        credit = normalize_inventory(payload)["credits"][0]
        assert credit["id"] == "nested-1"
        assert credit["expires_at"] == 1789257600
        assert credit["status"] == "available"
        assert credit["granted_at"] == 0

    def test_rejects_tokens_in_errors_and_unknown_payloads(self):
        with pytest.raises(GrokResetCreditsError) as exc:
            normalize_inventory(["not", "an", "object"])
        assert ACCESS_TOKEN not in str(exc.value)
        assert exc.value.kind == "invalid_contract"
        with pytest.raises(GrokResetCreditsError):
            normalize_inventory({"availableCount": -1, "tokens": []})


class TestNormalizeConsume:
    @pytest.mark.parametrize(
        ("payload", "outcome"),
        [
            ({"code": "reset", "windows_reset": 2}, "reset"),
            ({"code": "ok"}, "reset"),
            ({"outcome": "success"}, "reset"),
            ({"success": True}, "reset"),
            ({"error": "already_used"}, "already_redeemed"),
            ({"code": "no_credit"}, "no_credit"),
            ({"result": "not_found"}, "no_credit"),
            ({"code": "no_usage"}, "nothing_to_reset"),
        ],
    )
    def test_maps_upstream_codes(self, payload, outcome):
        result = normalize_consume(payload)
        assert result["outcome"] == outcome
        assert result["windows_reset"] >= 0

    def test_rejects_unmappable_without_token(self):
        with pytest.raises(GrokResetCreditsError) as exc:
            normalize_consume({})
        assert ACCESS_TOKEN not in str(exc.value)


class TestNetwork:
    def test_urls_are_pinned(self):
        for url in (RESET_CREDITS_URL, RESET_TOKENS_URL, BILLING_URL, CONSUME_RESET_CREDIT_URL):
            parsed = urlsplit(url)
            assert parsed.scheme == "https"
            assert parsed.hostname == GROK_BILLING_HOST
        assert grok._url_is_pinned("https://grok.com/v1/billing") is False

    @pytest.mark.asyncio
    async def test_errors_never_include_tokens_or_upstream_bodies(self):
        raw_body = "RAW-UPSTREAM-SECRET-BODY Bearer " + ACCESS_TOKEN

        def handler(request):
            return httpx.Response(503, text=raw_body)

        async with _client(handler) as client:
            with pytest.raises(GrokResetCreditsError) as exc:
                await get_reset_credits(ACCESS_TOKEN, client=client)
        combined = str(exc.value) + repr(exc.value)
        assert ACCESS_TOKEN not in combined
        assert raw_body not in combined

    @pytest.mark.asyncio
    async def test_does_not_follow_redirects_off_host(self):
        seen = []

        def handler(request):
            seen.append(str(request.url))
            if str(request.url) == RESET_CREDITS_URL:
                return httpx.Response(
                    302, headers={"Location": "https://evil.example/steal"}
                )
            return httpx.Response(200, json={"availableCount": 0, "tokens": []})

        async with _client(handler) as client:
            await get_reset_credits(ACCESS_TOKEN, client=client)
        assert all("evil.example" not in url for url in seen)

    @pytest.mark.asyncio
    async def test_primary_inventory_url_and_headers(self):
        captured = {}

        def handler(request):
            captured["url"] = str(request.url)
            captured["headers"] = request.headers
            if str(request.url) != RESET_CREDITS_URL:
                raise AssertionError("must not fall through")
            return httpx.Response(
                200, json={"availableCount": 1, "tokens": [{"tokenId": "t1"}]}
            )

        async with _client(handler) as client:
            result = await get_reset_credits(ACCESS_TOKEN, client=client)
        assert result["credits"][0]["id"] == "t1"
        assert captured["url"] == RESET_CREDITS_URL
        assert captured["headers"]["authorization"] == f"Bearer {ACCESS_TOKEN}"
        assert captured["headers"]["x-xai-token-auth"] == GROK_TOKEN_AUTH

    @pytest.mark.asyncio
    async def test_falls_back_to_nested_billing(self):
        def handler(request):
            if str(request.url) in (RESET_CREDITS_URL, RESET_TOKENS_URL):
                return httpx.Response(404)
            return httpx.Response(
                200, json={"config": {"resetTokens": [{"tokenId": "from-billing"}]}}
            )

        async with _client(handler) as client:
            result = await get_reset_credits(ACCESS_TOKEN, client=client)
        assert result["credits"][0]["id"] == "from-billing"

    @pytest.mark.asyncio
    async def test_consume_primary_and_fallback(self):
        seen = []

        def handler(request):
            seen.append(str(request.url))
            if str(request.url) == CONSUME_RESET_CREDIT_URL:
                return httpx.Response(404)
            return httpx.Response(200, json={"status": "already_used"})

        async with _client(handler) as client:
            result = await consume_reset_credit(
                ACCESS_TOKEN,
                credit_id="tok-9",
                idempotency_key="idem",
                client=client,
            )
        assert result["outcome"] == "already_redeemed"
        assert seen[0] == CONSUME_RESET_CREDIT_URL
        assert seen[1].endswith("/v1/billing/reset-tokens/tok-9/redeem")
