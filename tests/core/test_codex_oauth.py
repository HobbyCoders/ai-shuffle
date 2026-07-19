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
            # Real contract: JSON body, ``device_auth_id`` + string ``interval``
            # + ISO ``expires_at`` in the response (NOT RFC-8628 field names).
            assert request.url.path.endswith("/api/accounts/deviceauth/usercode")
            assert request.headers["content-type"].startswith("application/json")
            seen.update(json.loads(request.content.decode()))
            return httpx.Response(200, json={
                "device_auth_id": "deviceauth_abc123",
                "user_code": "WXYZ-1234",
                "interval": "5",
                "expires_at": "2099-01-01T00:00:00Z",
            })

        async with _client(handler) as c:
            start = await codex_oauth.start_device_code(client=c)

        assert start.user_code == "WXYZ-1234"
        assert start.device_code == "deviceauth_abc123"
        assert start.verification_uri == codex_oauth.DEVICE_VERIFICATION_URL
        assert start.interval == 5
        assert start.expires_in > 0
        assert start.code_verifier  # fallback PKCE verifier minted
        # Reference client sends ONLY client_id — the server mints the PKCE
        # pair and returns the verifier in the success poll.
        assert seen == {"client_id": codex_oauth.client_id()}

    async def test_http_error_raises_nested_error_object(self):
        def handler(request):
            return httpx.Response(400, json={"error": {
                "message": "bad client", "type": "invalid_request_error",
                "code": "invalid_client",
            }})

        async with _client(handler) as c:
            with pytest.raises(codex_oauth.CodexAuthError) as ei:
                await codex_oauth.start_device_code(client=c)
        assert ei.value.code == "invalid_client"

    async def test_http_error_raises_flat_error_string(self):
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
            # Real contract: the pending signal is a nested error object.
            return httpx.Response(400, json={"error": {
                "message": "Device authorization is pending. Please try again.",
                "type": "invalid_request_error",
                "code": "deviceauth_authorization_pending",
            }})

        async with _client(handler) as c:
            with pytest.raises(codex_oauth.CodexAuthPending) as ei:
                await codex_oauth.poll_device_code("dev", "verifier", "AAAA-BBBBB", client=c)
        assert ei.value.slow_down is False

    async def test_slow_down_raises_pending_slowdown(self):
        def handler(request):
            return httpx.Response(400, json={"error": {"code": "deviceauth_slow_down"}})

        async with _client(handler) as c:
            with pytest.raises(codex_oauth.CodexAuthPending) as ei:
                await codex_oauth.poll_device_code("dev", "verifier", "AAAA-BBBBB", client=c)
        assert ei.value.slow_down is True

    async def test_denied_raises_error(self):
        def handler(request):
            return httpx.Response(400, json={"error": {"code": "access_denied"}})

        async with _client(handler) as c:
            with pytest.raises(codex_oauth.CodexAuthError) as ei:
                await codex_oauth.poll_device_code("dev", "verifier", "AAAA-BBBBB", client=c)
        assert ei.value.code == "access_denied"

    async def test_poll_sends_json_device_auth_id_and_user_code(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path.endswith("/api/accounts/deviceauth/token")
            assert request.headers["content-type"].startswith("application/json")
            seen.update(json.loads(request.content.decode()))
            return httpx.Response(400, json={"error": {
                "code": "deviceauth_authorization_pending"}})

        async with _client(handler) as c:
            with pytest.raises(codex_oauth.CodexAuthPending):
                await codex_oauth.poll_device_code(
                    "deviceauth_abc123", "verifier", "WXYZ-1234", client=c
                )
        assert seen == {
            "client_id": codex_oauth.client_id(),
            "device_auth_id": "deviceauth_abc123",
            "user_code": "WXYZ-1234",
        }

    async def test_direct_token_response(self):
        id_token = _make_id_token({"chatgpt_account_id": "acct-direct"})

        def handler(request):
            return httpx.Response(200, json={
                "access_token": "at-1", "refresh_token": "rt-1", "id_token": id_token,
            })

        async with _client(handler) as c:
            bundle = await codex_oauth.poll_device_code("dev", "verifier", "AAAA-BBBBB", client=c)
        assert bundle.access_token == "at-1"
        assert bundle.refresh_token == "rt-1"
        assert bundle.account_id == "acct-direct"

    async def test_code_exchange_uses_server_minted_verifier(self):
        # Real contract: the success poll carries the SERVER-minted PKCE pair;
        # the exchange must use THAT verifier, not our local fallback.
        id_token = _make_id_token({"chatgpt_account_id": "acct-exch"})
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            if request.url.path.endswith("/deviceauth/token"):
                return httpx.Response(200, json={
                    "authorization_code": "auth-code-1",
                    "code_challenge": "server-challenge",
                    "code_verifier": "server-verifier",
                })
            if request.url.path.endswith("/oauth/token"):
                body = dict(httpx.QueryParams(request.content.decode()))
                assert body["grant_type"] == "authorization_code"
                assert body["code"] == "auth-code-1"
                assert body["code_verifier"] == "server-verifier"
                assert body["redirect_uri"] == codex_oauth.DEVICE_CALLBACK_URL
                return httpx.Response(200, json={
                    "access_token": "at-x", "refresh_token": "rt-x",
                    "id_token": id_token,
                })
            return httpx.Response(404)

        async with _client(handler) as c:
            bundle = await codex_oauth.poll_device_code(
                "dev", "local-fallback-verifier", "AAAA-BBBBB", client=c
            )
        assert bundle.access_token == "at-x"
        assert bundle.account_id == "acct-exch"
        assert any(p.endswith("/oauth/token") for p in calls)

    async def test_code_exchange_falls_back_to_local_verifier(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/deviceauth/token"):
                return httpx.Response(200, json={"authorization_code": "auth-code-2"})
            if request.url.path.endswith("/oauth/token"):
                body = dict(httpx.QueryParams(request.content.decode()))
                assert body["code_verifier"] == "the-verifier"
                return httpx.Response(200, json={
                    "access_token": "at-y", "refresh_token": "rt-y",
                })
            return httpx.Response(404)

        async with _client(handler) as c:
            bundle = await codex_oauth.poll_device_code(
                "dev", "the-verifier", "AAAA-BBBBB", client=c
            )
        assert bundle.access_token == "at-y"


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
