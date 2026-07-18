"""Unit tests for the Codex/ChatGPT-subscription OAuth flow logic.

All HTTP is mocked via ``httpx.MockTransport`` — NO real network to
auth.openai.com. Covers PKCE S256 correctness, device start/poll (happy path +
pending/slow_down), authorization-code exchange, ``chatgpt_account_id``
extraction from the id_token, refresh rotation, permanent-failure codes, and the
best-effort revoke.
"""

import base64
import hashlib
import json

import httpx
import pytest

from app.core import codex_oauth


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_id_token(claims: dict) -> str:
    """Build an unsigned JWT (header.payload.signature) carrying ``claims``."""
    def _seg(obj):
        raw = json.dumps(obj).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    return f"{_seg({'alg': 'none'})}.{_seg(claims)}.sig"


def _client(handler):
    """An AsyncClient whose requests are served by ``handler`` (no network)."""
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ---------------------------------------------------------------------------
# PKCE
# ---------------------------------------------------------------------------

class TestPKCE:
    def test_challenge_is_s256_of_verifier(self):
        verifier, challenge = codex_oauth.generate_pkce()
        expected = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()
        ).rstrip(b"=").decode()
        assert challenge == expected

    def test_verifier_length_in_rfc_range(self):
        verifier, _ = codex_oauth.generate_pkce()
        assert 43 <= len(verifier) <= 128

    def test_no_padding_and_url_safe(self):
        verifier, challenge = codex_oauth.generate_pkce()
        for s in (verifier, challenge):
            assert "=" not in s and "+" not in s and "/" not in s

    def test_unique_each_call(self):
        assert codex_oauth.generate_pkce()[0] != codex_oauth.generate_pkce()[0]


# ---------------------------------------------------------------------------
# account_id extraction
# ---------------------------------------------------------------------------

class TestAccountIdExtraction:
    def test_top_level_claim(self):
        tok = _make_id_token({"chatgpt_account_id": "acct-top"})
        assert codex_oauth.extract_account_id(tok) == "acct-top"

    def test_namespaced_claim(self):
        tok = _make_id_token(
            {"https://api.openai.com/auth": {"chatgpt_account_id": "acct-ns"}}
        )
        assert codex_oauth.extract_account_id(tok) == "acct-ns"

    def test_missing_claim_returns_none(self):
        tok = _make_id_token({"sub": "user-1"})
        assert codex_oauth.extract_account_id(tok) is None

    def test_none_and_garbage(self):
        assert codex_oauth.extract_account_id(None) is None
        assert codex_oauth.extract_account_id("not-a-jwt") is None


# ---------------------------------------------------------------------------
# Device start
# ---------------------------------------------------------------------------

class TestDeviceStart:
    async def test_happy_path_returns_user_code_and_verifier(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path.endswith("/api/accounts/deviceauth/usercode")
            body = dict(httpx.QueryParams(request.content.decode()))
            seen.update(body)
            return httpx.Response(200, json={
                "device_code": "dev-123",
                "user_code": "WXYZ-1234",
                "verification_uri": "https://auth.openai.com/codex/device",
                "interval": 5,
                "expires_in": 900,
            })

        async with _client(handler) as c:
            start = await codex_oauth.start_device_code(client=c)

        assert start.user_code == "WXYZ-1234"
        assert start.device_code == "dev-123"
        assert start.verification_uri == "https://auth.openai.com/codex/device"
        assert start.code_verifier  # PKCE verifier minted
        # The S256 challenge sent matches the verifier we got back.
        expected = base64.urlsafe_b64encode(
            hashlib.sha256(start.code_verifier.encode()).digest()
        ).rstrip(b"=").decode()
        assert seen["code_challenge"] == expected
        assert seen["code_challenge_method"] == "S256"
        assert seen["client_id"] == codex_oauth.client_id()

    async def test_http_error_raises(self):
        def handler(request):
            return httpx.Response(400, json={"error": "invalid_client"})

        async with _client(handler) as c:
            with pytest.raises(codex_oauth.CodexAuthError) as ei:
                await codex_oauth.start_device_code(client=c)
        assert ei.value.code == "invalid_client"


# ---------------------------------------------------------------------------
# Device poll
# ---------------------------------------------------------------------------

class TestDevicePoll:
    async def test_pending_raises_pending(self):
        def handler(request):
            return httpx.Response(200, json={"error": "authorization_pending"})

        async with _client(handler) as c:
            with pytest.raises(codex_oauth.CodexAuthPending) as ei:
                await codex_oauth.poll_device_code("dev", "verifier", client=c)
        assert ei.value.slow_down is False

    async def test_slow_down_raises_pending_slowdown(self):
        def handler(request):
            return httpx.Response(200, json={"error": "slow_down"})

        async with _client(handler) as c:
            with pytest.raises(codex_oauth.CodexAuthPending) as ei:
                await codex_oauth.poll_device_code("dev", "verifier", client=c)
        assert ei.value.slow_down is True

    async def test_denied_raises_error(self):
        def handler(request):
            return httpx.Response(200, json={"error": "access_denied"})

        async with _client(handler) as c:
            with pytest.raises(codex_oauth.CodexAuthError) as ei:
                await codex_oauth.poll_device_code("dev", "verifier", client=c)
        assert ei.value.code == "access_denied"

    async def test_direct_token_response(self):
        id_token = _make_id_token({"chatgpt_account_id": "acct-direct"})

        def handler(request):
            return httpx.Response(200, json={
                "access_token": "at-1", "refresh_token": "rt-1", "id_token": id_token,
            })

        async with _client(handler) as c:
            bundle = await codex_oauth.poll_device_code("dev", "verifier", client=c)
        assert bundle.access_token == "at-1"
        assert bundle.refresh_token == "rt-1"
        assert bundle.account_id == "acct-direct"

    async def test_code_exchange_path(self):
        id_token = _make_id_token({"chatgpt_account_id": "acct-exch"})
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            if request.url.path.endswith("/deviceauth/token"):
                return httpx.Response(200, json={"code": "auth-code-1"})
            if request.url.path.endswith("/oauth/token"):
                body = dict(httpx.QueryParams(request.content.decode()))
                assert body["grant_type"] == "authorization_code"
                assert body["code"] == "auth-code-1"
                assert body["code_verifier"] == "the-verifier"
                return httpx.Response(200, json={
                    "access_token": "at-x", "refresh_token": "rt-x",
                    "id_token": id_token,
                })
            return httpx.Response(404)

        async with _client(handler) as c:
            bundle = await codex_oauth.poll_device_code(
                "dev", "the-verifier", client=c
            )
        assert bundle.access_token == "at-x"
        assert bundle.account_id == "acct-exch"
        assert any(p.endswith("/oauth/token") for p in calls)


# ---------------------------------------------------------------------------
# Refresh (rotating)
# ---------------------------------------------------------------------------

class TestRefresh:
    async def test_rotation_returns_new_tokens(self):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path.endswith("/oauth/token")
            body = json.loads(request.content.decode())
            assert body["grant_type"] == "refresh_token"
            assert body["refresh_token"] == "old-rt"
            return httpx.Response(200, json={
                "access_token": "new-at", "refresh_token": "new-rt",
            })

        async with _client(handler) as c:
            bundle = await codex_oauth.refresh_tokens("old-rt", client=c)
        assert bundle.access_token == "new-at"
        assert bundle.refresh_token == "new-rt"  # ROTATED

    async def test_missing_refresh_falls_back_to_prior(self):
        def handler(request):
            return httpx.Response(200, json={"access_token": "new-at"})

        async with _client(handler) as c:
            bundle = await codex_oauth.refresh_tokens("old-rt", client=c)
        assert bundle.refresh_token == "old-rt"  # preserved, never dropped

    @pytest.mark.parametrize("code", [
        "refresh_token_expired", "refresh_token_reused", "refresh_token_invalidated",
    ])
    async def test_permanent_failure_codes(self, code):
        def handler(request):
            return httpx.Response(400, json={"error": code})

        async with _client(handler) as c:
            with pytest.raises(codex_oauth.CodexAuthError) as ei:
                await codex_oauth.refresh_tokens("old-rt", client=c)
        assert ei.value.code == code
        assert ei.value.is_permanent is True

    async def test_transient_failure_not_permanent(self):
        def handler(request):
            return httpx.Response(503, json={"error": "server_error"})

        async with _client(handler) as c:
            with pytest.raises(codex_oauth.CodexAuthError) as ei:
                await codex_oauth.refresh_tokens("old-rt", client=c)
        assert ei.value.is_permanent is False


# ---------------------------------------------------------------------------
# Revoke
# ---------------------------------------------------------------------------

class TestRevoke:
    async def test_revoke_success(self):
        def handler(request):
            assert request.url.path.endswith("/oauth/revoke")
            return httpx.Response(200)

        async with _client(handler) as c:
            assert await codex_oauth.revoke_token("rt", client=c) is True

    async def test_revoke_failure_returns_false_never_raises(self):
        def handler(request):
            return httpx.Response(500)

        async with _client(handler) as c:
            assert await codex_oauth.revoke_token("rt", client=c) is False


# ---------------------------------------------------------------------------
# client_id override
# ---------------------------------------------------------------------------

class TestClientId:
    def test_default(self, monkeypatch):
        monkeypatch.delenv("CODEX_APP_SERVER_LOGIN_CLIENT_ID", raising=False)
        assert codex_oauth.client_id() == "app_EMoamEEZ73f0CkXaXp7hrann"

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("CODEX_APP_SERVER_LOGIN_CLIENT_ID", "app_custom")
        assert codex_oauth.client_id() == "app_custom"
