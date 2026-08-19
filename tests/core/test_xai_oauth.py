"""Unit tests for the xAI / SuperGrok OAuth device-code flow.

All HTTP is mocked via ``httpx.MockTransport`` — NO real network to auth.x.ai.
Covers RFC 8628 device start/poll (pending, slow_down, complete, terminal) and
refresh.
"""

from urllib.parse import parse_qs

import httpx
import pytest

from app.core import xai_oauth


def _form(request: httpx.Request) -> dict:
    """Parse a form-urlencoded request body into a flat str→str map."""
    parsed = parse_qs(request.content.decode(), keep_blank_values=True)
    return {k: v[0] if v else "" for k, v in parsed.items()}


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class TestDeviceStart:
    async def test_happy_path_returns_user_code_and_uris(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            assert str(request.url) == xai_oauth.DEVICE_CODE_URL
            assert request.headers["content-type"].startswith(
                "application/x-www-form-urlencoded"
            )
            seen.update(_form(request))
            return httpx.Response(200, json={
                "device_code": "dev-abc",
                "user_code": "WXYZ-1234",
                "verification_uri": "https://auth.x.ai/activate",
                "verification_uri_complete": "https://auth.x.ai/activate?user_code=WXYZ-1234",
                "interval": 5,
                "expires_in": 900,
            })

        async with _client(handler) as c:
            start = await xai_oauth.start_device_code(client=c)

        assert start.user_code == "WXYZ-1234"
        assert start.device_code == "dev-abc"
        assert start.verification_uri == "https://auth.x.ai/activate"
        assert start.verification_uri_complete == (
            "https://auth.x.ai/activate?user_code=WXYZ-1234"
        )
        assert start.interval == 5
        assert start.expires_in == 900
        assert seen["client_id"] == xai_oauth.client_id()
        assert seen["scope"] == xai_oauth.SCOPE
        assert seen["referrer"] == xai_oauth.REFERRER

    async def test_missing_fields_raises(self):
        def handler(request):
            return httpx.Response(200, json={"device_code": "only-this"})

        async with _client(handler) as c:
            with pytest.raises(xai_oauth.XaiAuthError) as ei:
                await xai_oauth.start_device_code(client=c)
        assert ei.value.code == "invalid_device_response"

    async def test_http_error_raises_flat_error_string(self):
        def handler(request):
            return httpx.Response(400, json={"error": "invalid_client"})

        async with _client(handler) as c:
            with pytest.raises(xai_oauth.XaiAuthError) as ei:
                await xai_oauth.start_device_code(client=c)
        assert ei.value.code == "invalid_client"


class TestDevicePoll:
    async def test_pending_raises_pending(self):
        def handler(request):
            assert str(request.url) == xai_oauth.TOKEN_URL
            body = _form(request)
            assert body["grant_type"] == xai_oauth.DEVICE_GRANT_TYPE
            assert body["device_code"] == "dev-1"
            return httpx.Response(400, json={"error": "authorization_pending"})

        async with _client(handler) as c:
            with pytest.raises(xai_oauth.XaiAuthPending) as ei:
                await xai_oauth.poll_device_code("dev-1", client=c)
        assert ei.value.slow_down is False

    async def test_slow_down_raises_pending_flag(self):
        def handler(request):
            return httpx.Response(400, json={"error": "slow_down"})

        async with _client(handler) as c:
            with pytest.raises(xai_oauth.XaiAuthPending) as ei:
                await xai_oauth.poll_device_code("dev-1", client=c)
        assert ei.value.slow_down is True

    async def test_complete_returns_bundle(self):
        def handler(request):
            return httpx.Response(200, json={
                "access_token": "access-1",
                "refresh_token": "refresh-1",
                "id_token": None,
            })

        async with _client(handler) as c:
            bundle = await xai_oauth.poll_device_code("dev-1", client=c)
        assert bundle.access_token == "access-1"
        assert bundle.refresh_token == "refresh-1"

    async def test_terminal_error_raises(self):
        def handler(request):
            return httpx.Response(400, json={
                "error": "access_denied",
                "error_description": "user denied",
            })

        async with _client(handler) as c:
            with pytest.raises(xai_oauth.XaiAuthError) as ei:
                await xai_oauth.poll_device_code("dev-1", client=c)
        assert ei.value.code == "access_denied"
        assert ei.value.is_permanent is True

    async def test_expired_token_is_terminal(self):
        def handler(request):
            return httpx.Response(400, json={"error": "expired_token"})

        async with _client(handler) as c:
            with pytest.raises(xai_oauth.XaiAuthError) as ei:
                await xai_oauth.poll_device_code("dev-1", client=c)
        assert ei.value.code == "expired_token"
        assert ei.value.is_permanent is True


class TestRefresh:
    async def test_refresh_success(self):
        seen = {}

        def handler(request):
            seen.update(_form(request))
            return httpx.Response(200, json={
                "access_token": "access-new",
                "refresh_token": "refresh-new",
            })

        async with _client(handler) as c:
            bundle = await xai_oauth.refresh_token("refresh-old", client=c)
        assert bundle.access_token == "access-new"
        assert bundle.refresh_token == "refresh-new"
        assert seen["grant_type"] == "refresh_token"
        assert seen["refresh_token"] == "refresh-old"
        assert seen["client_id"] == xai_oauth.client_id()

    async def test_refresh_keeps_prior_refresh_when_omitted(self):
        def handler(request):
            return httpx.Response(200, json={"access_token": "access-new"})

        async with _client(handler) as c:
            bundle = await xai_oauth.refresh_token("refresh-old", client=c)
        assert bundle.refresh_token == "refresh-old"

    async def test_invalid_grant_is_permanent(self):
        def handler(request):
            return httpx.Response(400, json={"error": "invalid_grant"})

        async with _client(handler) as c:
            with pytest.raises(xai_oauth.XaiAuthError) as ei:
                await xai_oauth.refresh_token("dead", client=c)
        assert ei.value.code == "invalid_grant"
        assert ei.value.is_permanent is True
