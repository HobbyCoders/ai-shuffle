"""Plugin service OAuth core (app/core/plugin_oauth.py).

Every provider call is faked with ``httpx.MockTransport``; storage is an
in-memory ``system_settings`` behind the real encryption.
"""

import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from cryptography.fernet import Fernet

from app.core import encryption, plugin_oauth
from app.core.plugin_oauth import OAuthClient, PluginOAuthError, TokenSet

PLUGIN = "dns-reader@mp"
SECRET = "client-secret-never-shown"
ACCESS = "access-token-never-shown"
REFRESH = "refresh-token-never-shown"


def _manifest(**auth_changes) -> dict:
    auth = {
        "type": "oauth2",
        "authorize_url": "https://dash.example.com/oauth2/auth",
        "token_url": "https://dash.example.com/oauth2/token",
        "scopes": ["zone:read"],
        "pkce": True,
        "revoke_url": "https://dash.example.com/oauth2/revoke",
        **auth_changes,
    }
    return {"name": "dns-reader", "extensions": {"ai.shuffle": {"services": [
        {"name": "example", "provider": "custom", "url": "https://api.example.com/v4",
         "access": "read", "purpose": "Read DNS", "auth": auth},
        {"name": "plain", "provider": "custom", "url": "https://api.example.com/v4",
         "access": "read", "purpose": "No sign-in"},
    ]}}}


def _config(**auth_changes):
    return plugin_oauth.config_from_manifest(PLUGIN, _manifest(**auth_changes), "example")


@pytest.fixture
def store(monkeypatch):
    rows: dict[str, str] = {}
    monkeypatch.setattr(encryption, "_encryption_key", Fernet.generate_key())
    monkeypatch.setattr(plugin_oauth, "settings", SimpleNamespace(effective_instance_id="worker-one"))
    monkeypatch.setattr(plugin_oauth.database, "get_system_setting", lambda k: rows.get(k))
    monkeypatch.setattr(plugin_oauth.database, "set_system_setting", lambda k, v: rows.__setitem__(k, v))
    monkeypatch.setattr(plugin_oauth.database, "delete_system_setting", lambda k: rows.pop(k, None) is not None)
    plugin_oauth._locks.clear()
    return rows


class Provider:
    """A fake token endpoint that records each form it receives."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("content-type", "").startswith("application/json"):
            form = json.loads(request.content.decode())
        else:
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        self.calls.append((str(request.url), form))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self))


def _tokens(**extra) -> httpx.Response:
    body = {"access_token": ACCESS, "token_type": "Bearer", "expires_in": 3600,
            "refresh_token": REFRESH, **extra}
    return httpx.Response(200, json={k: v for k, v in body.items() if v is not None})


# ─── declaration ─────────────────────────────────────────────────────────────


def test_the_declaration_is_read_and_checked_again():
    config = _config()
    assert (config.authorize_url, config.token_url, config.scopes, config.revoke_url) == (
        "https://dash.example.com/oauth2/auth", "https://dash.example.com/oauth2/token",
        ("zone:read",), "https://dash.example.com/oauth2/revoke")
    for service in ("plain", "missing"):
        with pytest.raises(PluginOAuthError) as caught:
            plugin_oauth.config_from_manifest(PLUGIN, _manifest(), service)
        assert caught.value.reason == "not_declared"
    # A marketplace manifest reaches disk unchecked; a bad one is refused here.
    with pytest.raises(PluginOAuthError) as caught:
        _config(token_url="https://evil.net/token", client_secret="x")
    assert caught.value.reason == "invalid_declaration"
    assert str(caught.value) == plugin_oauth._MESSAGES["invalid_declaration"]
    with pytest.raises(PluginOAuthError):
        plugin_oauth.config_from_manifest("not a plugin id", _manifest(), "example")


# ─── authorize URL ───────────────────────────────────────────────────────────


def test_the_authorize_url_carries_state_and_a_pkce_challenge():
    config = _config(scopes=["zone:read", "dns:read"])
    first = plugin_oauth.start_sign_in(config, OAuthClient("client-1", SECRET), "https://shuffle.example/cb")
    second = plugin_oauth.start_sign_in(config, OAuthClient("client-1", SECRET), "https://shuffle.example/cb")
    parts = urlsplit(first.url)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == config.authorize_url
    query = {k: v[0] for k, v in parse_qs(parts.query).items()}
    assert query == {
        "response_type": "code", "client_id": "client-1", "redirect_uri": "https://shuffle.example/cb",
        "state": first.state, "code_challenge": plugin_oauth.pkce_challenge(first.code_verifier),
        "code_challenge_method": "S256", "scope": "zone:read dns:read",
    }
    assert 43 <= len(first.code_verifier) <= 128
    assert first.state != second.state and first.code_verifier != second.code_verifier
    # The verifier and the client secret never travel in the browser URL.
    assert first.code_verifier not in first.url and SECRET not in first.url
    assert first.code_verifier not in repr(first)


def test_the_rfc_7636_example_challenge():
    assert plugin_oauth.pkce_challenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk") == (
        "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM")


@pytest.mark.parametrize("redirect", [
    "http://shuffle.example/cb", "https://shuffle.example/cb#x", "https://u:p@shuffle.example/cb", "cb",
])
def test_a_bad_redirect_is_refused(redirect):
    with pytest.raises(PluginOAuthError) as caught:
        plugin_oauth.start_sign_in(_config(), OAuthClient("client-1"), redirect)
    assert caught.value.reason == "invalid_redirect"


def test_loopback_http_redirect_and_no_scopes_are_allowed():
    request = plugin_oauth.start_sign_in(_config(scopes=[]), OAuthClient("c"), "http://127.0.0.1:8000/cb")
    assert "scope" not in parse_qs(urlsplit(request.url).query)


def test_a_bad_client_id_is_refused():
    with pytest.raises(PluginOAuthError) as caught:
        plugin_oauth.start_sign_in(_config(), OAuthClient("has space"), "https://shuffle.example/cb")
    assert caught.value.reason == "invalid_client"


# ─── exchange, refresh, revoke ───────────────────────────────────────────────


async def test_exchange_posts_the_code_and_verifier_and_parses_tokens():
    provider = Provider(_tokens(scope="zone:read extra"))
    async with provider.client() as http:
        tokens = await plugin_oauth.exchange_code(
            _config(), OAuthClient("client-1", SECRET), code="the-code", code_verifier="v" * 43,
            redirect_uri="https://shuffle.example/cb", http=http, now=1000.0)
    assert (tokens.access_token, tokens.refresh_token, tokens.expires_at, tokens.scopes) == (
        ACCESS, REFRESH, 4600.0, ("zone:read", "extra"))
    [(url, form)] = provider.calls
    assert url == "https://dash.example.com/oauth2/token"
    assert form == {"grant_type": "authorization_code", "code": "the-code",
                    "redirect_uri": "https://shuffle.example/cb", "code_verifier": "v" * 43,
                    "client_id": "client-1", "client_secret": SECRET}
    assert ACCESS not in repr(tokens) and REFRESH not in repr(tokens)


async def test_a_form_encoded_answer_and_comma_scopes_are_understood():
    provider = Provider(httpx.Response(
        200, text=f"access_token={ACCESS}&token_type=bearer&scope=repo,read:org",
        headers={"content-type": "application/x-www-form-urlencoded"}))
    async with provider.client() as http:
        tokens = await plugin_oauth.exchange_code(
            _config(), OAuthClient("client-1"), code="c", code_verifier="v" * 43,
            redirect_uri="https://shuffle.example/cb", http=http)
    assert tokens.scopes == ("repo", "read:org") and tokens.expires_at is None
    assert "client_secret" not in provider.calls[0][1]


@pytest.mark.parametrize(("response", "reason"), [
    (httpx.Response(400, json={"error": "invalid_grant", "error_description": ACCESS}), "refused"),
    (httpx.Response(200, json={"error": "access_denied", "echo": SECRET}), "refused"),
    (httpx.Response(503, text=f"upstream said {SECRET}"), "unavailable"),
    (httpx.Response(302, headers={"location": "https://evil.net"}), "bad_response"),
    (httpx.Response(200, json={"token_type": "bearer"}), "bad_response"),
    (httpx.Response(200, json={"access_token": ACCESS, "token_type": "mac"}), "bad_response"),
    (httpx.Response(200, json={"access_token": ACCESS, "expires_in": "soon"}), "bad_response"),
    (httpx.Response(200, text="not json " + ACCESS), "bad_response"),
    (httpx.Response(200, json={"access_token": "x" * 70_000}), "bad_response"),
    (httpx.ConnectError(f"boom {SECRET}"), "unavailable"),
])
async def test_provider_failures_have_fixed_wording_and_no_provider_text(response, reason):
    provider = Provider(response)
    async with provider.client() as http:
        with pytest.raises(PluginOAuthError) as caught:
            await plugin_oauth.exchange_code(
                _config(), OAuthClient("client-1", SECRET), code="c", code_verifier="v" * 43,
                redirect_uri="https://shuffle.example/cb", http=http)
    error = caught.value
    assert error.reason == reason
    assert str(error) == plugin_oauth._MESSAGES[reason]
    for secret in (SECRET, ACCESS, "invalid_grant", "evil.net"):
        assert secret not in str(error) and secret not in repr(error)
    # Nothing chained: a traceback cannot print the httpx error or the body.
    assert error.__cause__ is None
    assert error.__context__ is None or error.__suppress_context__


async def test_refresh_keeps_the_old_refresh_token_and_granted_scopes():
    previous = TokenSet(access_token="old", refresh_token=REFRESH, expires_at=0.0, scopes=("zone:read",))
    provider = Provider(httpx.Response(200, json={"access_token": "new", "expires_in": 60}))
    async with provider.client() as http:
        fresh = await plugin_oauth.refresh_tokens(
            _config(), OAuthClient("client-1", SECRET), previous, http=http, now=10.0)
    assert (fresh.access_token, fresh.refresh_token, fresh.expires_at, fresh.scopes) == (
        "new", REFRESH, 70.0, ("zone:read",))
    assert provider.calls[0][1] == {"grant_type": "refresh_token", "refresh_token": REFRESH,
                                    "client_id": "client-1", "client_secret": SECRET}
    with pytest.raises(PluginOAuthError) as caught:
        await plugin_oauth.refresh_tokens(_config(), OAuthClient("c"), TokenSet(access_token="a"))
    assert caught.value.reason == "needs_sign_in"


async def test_revoke_posts_the_token_and_never_raises():
    provider = Provider(httpx.Response(200), httpx.Response(400), httpx.ConnectError("x"))
    async with provider.client() as http:
        client = OAuthClient("client-1")
        assert await plugin_oauth.revoke_token(_config(), client, REFRESH, http=http) is True
        assert await plugin_oauth.revoke_token(_config(), client, REFRESH, http=http) is False
        assert await plugin_oauth.revoke_token(_config(), client, REFRESH, http=http) is False
        assert await plugin_oauth.revoke_token(_config(revoke_url=None), client, REFRESH, http=http) is False
    assert provider.calls[0] == ("https://dash.example.com/oauth2/revoke", {
        "token": REFRESH, "token_type_hint": "refresh_token", "client_id": "client-1"})
    assert len(provider.calls) == 3


# ─── storage ─────────────────────────────────────────────────────────────────


def _sign_in(config, *, expires_at=None, refresh=REFRESH, client=None):
    client = client or OAuthClient("client-1", SECRET)
    plugin_oauth.save_client(config.plugin_id, config.service, client.client_id, client.client_secret)
    plugin_oauth.save_tokens(config, client, TokenSet(
        access_token=ACCESS, refresh_token=refresh, expires_at=expires_at, scopes=config.scopes), now=5.0)


def test_client_and_tokens_round_trip_encrypted(store):
    config = _config()
    _sign_in(config, expires_at=4000.0)
    assert len(store) == 2
    assert all(k.startswith("plugin_oauth:worker-one:") for k in store)
    for ciphertext in store.values():
        assert ciphertext.startswith("enc:")
        for plain in (SECRET, ACCESS, REFRESH, "client-1", PLUGIN, "zone:read"):
            assert plain not in ciphertext
    assert plugin_oauth.load_client(PLUGIN, "example") == OAuthClient("client-1", SECRET)
    stored = plugin_oauth.load_tokens(PLUGIN, "example")
    assert (stored.tokens.access_token, stored.tokens.refresh_token, stored.tokens.expires_at) == (
        ACCESS, REFRESH, 4000.0)
    assert (stored.requested_scopes, stored.token_url, stored.client_id, stored.obtained_at) == (
        ("zone:read",), config.token_url, "client-1", 5.0)
    assert SECRET not in repr(plugin_oauth.load_client(PLUGIN, "example"))

    assert plugin_oauth.delete_tokens(PLUGIN, "example") is True
    assert plugin_oauth.load_tokens(PLUGIN, "example") is None
    assert plugin_oauth.delete_client(PLUGIN, "example") is True
    assert not store


def test_rows_are_bound_to_their_key_and_worker(store, monkeypatch):
    _sign_in(_config())
    other = plugin_oauth.config_from_manifest("other@mp", _manifest(), "example")
    plugin_oauth.save_client(other.plugin_id, other.service, "client-2")
    # A row copied under another plugin's key does not unlock.
    store[plugin_oauth._key("other@mp", "example", "client")] = store[plugin_oauth._key(PLUGIN, "example", "client")]
    with pytest.raises(PluginOAuthError) as caught:
        plugin_oauth.load_client("other@mp", "example")
    assert caught.value.reason == "storage"
    # Plain text in the slot is never read as a client.
    store[plugin_oauth._key("other@mp", "example", "client")] = json.dumps({"client_id": "x"})
    with pytest.raises(PluginOAuthError):
        plugin_oauth.load_client("other@mp", "example")
    # Another worker reading the same database finds nothing of its own.
    monkeypatch.setattr(plugin_oauth, "settings", SimpleNamespace(effective_instance_id="worker-two"))
    assert plugin_oauth.load_client(PLUGIN, "example") is None


def test_locked_storage_refuses_to_save(store, monkeypatch):
    monkeypatch.setattr(encryption, "_encryption_key", None)
    with pytest.raises(PluginOAuthError) as caught:
        plugin_oauth.save_client(PLUGIN, "example", "client-1", SECRET)
    assert caught.value.reason == "storage_locked"
    assert not store


def test_a_new_client_id_drops_the_old_sign_in(store):
    config = _config()
    _sign_in(config)
    plugin_oauth.save_client(PLUGIN, "example", "client-1", "rotated-secret")
    assert plugin_oauth.load_tokens(PLUGIN, "example") is not None
    plugin_oauth.save_client(PLUGIN, "example", "client-2", "")
    assert plugin_oauth.load_tokens(PLUGIN, "example") is None
    assert plugin_oauth.status(config)["state"] == "signed_out"


def test_plugin_oauth_rows_are_never_network_shared():
    from app.network.policy import is_shared_setting_key
    assert not is_shared_setting_key("plugin_oauth:worker-one:abc:tokens")


# ─── status ──────────────────────────────────────────────────────────────────


def test_status_reports_a_sign_in_without_secrets(store):
    config = _config()
    assert plugin_oauth.status(config)["state"] == "not_configured"
    plugin_oauth.save_client(PLUGIN, "example", "client-1", SECRET)
    assert plugin_oauth.status(config)["state"] == "signed_out"
    _sign_in(config, expires_at=4000.0)
    report = plugin_oauth.status(config, now=100.0)
    assert report == {
        "plugin_id": PLUGIN, "service": "example", "state": "signed_in", "reason": "",
        "client_configured": True, "client_source": "owner",
        "requested_scopes": ["zone:read"], "scopes": ["zone:read"],
        "missing_scopes": [], "expires_at": 4000.0, "can_refresh": True,
    }
    text = json.dumps(report)
    for secret in (SECRET, ACCESS, REFRESH, "client-1"):
        assert secret not in text


def test_expired_without_a_refresh_token_needs_sign_in(store):
    config = _config()
    _sign_in(config, expires_at=50.0, refresh="")
    assert plugin_oauth.status(config, now=10.0)["state"] == "signed_in"
    report = plugin_oauth.status(config, now=100.0)
    assert (report["state"], report["reason"]) == ("needs_sign_in", "expired")


async def test_widened_scopes_need_a_new_sign_in(store):
    _sign_in(_config(scopes=["zone:read"]), expires_at=None)
    widened = _config(scopes=["zone:read", "zone:edit"])
    report = plugin_oauth.status(widened)
    assert (report["state"], report["reason"], report["missing_scopes"]) == (
        "needs_sign_in", "scopes_widened", ["zone:edit"])
    with pytest.raises(PluginOAuthError) as caught:
        await plugin_oauth.access_token_for_use(widened)
    assert caught.value.reason == "needs_sign_in"
    # Narrowing keeps the sign-in.
    assert plugin_oauth.status(_config(scopes=[]))["state"] == "signed_in"


def test_a_moved_token_endpoint_needs_a_new_sign_in(store):
    _sign_in(_config())
    moved = _config(token_url="https://login.example.com/token")
    assert plugin_oauth.status(moved)["reason"] == "endpoint_changed"


def test_unreadable_saved_sign_in_reads_as_needs_sign_in(store):
    config = _config()
    _sign_in(config)
    store[plugin_oauth._key(PLUGIN, "example", "tokens")] = "enc:garbage"
    report = plugin_oauth.status(config)
    assert (report["state"], report["reason"]) == ("needs_sign_in", "unreadable")


# ─── higher-level steps ──────────────────────────────────────────────────────


async def test_complete_sign_in_saves_tokens_and_returns_status(store):
    config = _config()
    plugin_oauth.save_client(PLUGIN, "example", "client-1", SECRET)
    request = plugin_oauth.start_sign_in(config, OAuthClient("client-1", SECRET), "https://shuffle.example/cb")
    provider = Provider(_tokens())
    async with provider.client() as http:
        report = await plugin_oauth.complete_sign_in(config, request, "the-code", http=http, now=0.0)
    assert report["state"] == "signed_in" and report["expires_at"] == 3600.0
    assert provider.calls[0][1]["code_verifier"] == request.code_verifier
    assert ACCESS not in json.dumps(report)
    assert plugin_oauth.load_tokens(PLUGIN, "example").tokens.access_token == ACCESS


async def test_complete_sign_in_without_a_client_is_refused(store):
    config = _config()
    request = plugin_oauth.start_sign_in(config, OAuthClient("client-1"), "https://shuffle.example/cb")
    async with Provider().client() as http:
        with pytest.raises(PluginOAuthError) as caught:
            await plugin_oauth.complete_sign_in(config, request, "code", http=http)
    assert caught.value.reason == "not_configured"


async def test_access_token_is_reused_until_due_then_refreshed(store):
    config = _config()
    _sign_in(config, expires_at=1000.0)
    provider = Provider(httpx.Response(200, json={"access_token": "fresh", "expires_in": 3600}))
    async with provider.client() as http:
        assert await plugin_oauth.access_token_for_use(config, http=http, now=100.0) == ACCESS
        assert provider.calls == []
        assert await plugin_oauth.access_token_for_use(config, http=http, now=950.0) == "fresh"
        assert await plugin_oauth.access_token_for_use(config, http=http, now=960.0) == "fresh"
    assert len(provider.calls) == 1
    stored = plugin_oauth.load_tokens(PLUGIN, "example")
    assert (stored.tokens.refresh_token, stored.tokens.expires_at) == (REFRESH, 4550.0)


async def test_a_refused_refresh_signs_out_and_an_outage_keeps_tokens(store):
    config = _config()
    _sign_in(config, expires_at=0.0)
    provider = Provider(httpx.ConnectError("down"), httpx.Response(400, json={"error": "invalid_grant"}))
    async with provider.client() as http:
        with pytest.raises(PluginOAuthError) as caught:
            await plugin_oauth.access_token_for_use(config, http=http, now=10.0)
        assert caught.value.reason == "unavailable"
        assert plugin_oauth.load_tokens(PLUGIN, "example") is not None
        with pytest.raises(PluginOAuthError) as caught:
            await plugin_oauth.access_token_for_use(config, http=http, now=10.0)
        assert caught.value.reason == "needs_sign_in"
    assert plugin_oauth.load_tokens(PLUGIN, "example") is None
    assert plugin_oauth.status(config)["state"] == "signed_out"


async def test_force_refresh_after_a_401(store):
    config = _config()
    _sign_in(config, expires_at=None)
    provider = Provider(httpx.Response(200, json={"access_token": "fresh"}))
    async with provider.client() as http:
        assert await plugin_oauth.access_token_for_use(config, http=http, force_refresh=True) == "fresh"


async def test_sign_out_removes_tokens_first_and_revokes(store):
    config = _config()
    _sign_in(config)
    provider = Provider(httpx.ConnectError("down"))
    async with provider.client() as http:
        result = await plugin_oauth.sign_out(config, http=http)
    assert result == {"signed_out": True, "revoked": False}
    assert plugin_oauth.load_tokens(PLUGIN, "example") is None
    assert plugin_oauth.load_client(PLUGIN, "example") is not None
    assert provider.calls[0][1]["token"] == REFRESH

    _sign_in(config)
    provider = Provider(httpx.Response(200))
    async with provider.client() as http:
        assert await plugin_oauth.sign_out(config, http=http) == {"signed_out": True, "revoked": True}


# ─── a client with no setup: shipped by the plugin, or registered ───────────

REGISTER = "https://dash.example.com/oauth2/register"
CB = "https://shuffle.example/cb"
CB_LOCAL = "http://127.0.0.1:8000/cb"
REG_SECRET = "registered-secret-never-shown"


def _registered(client_id="reg-1", **extra) -> httpx.Response:
    return httpx.Response(201, json={"client_id": client_id, "token_endpoint_auth_method": "none", **extra})


def _tokens_for(config, client):
    plugin_oauth.save_tokens(config, client, TokenSet(
        access_token=ACCESS, refresh_token=REFRESH, expires_at=None, scopes=config.scopes), now=5.0)


async def test_a_shipped_client_signs_in_with_no_setup_and_no_secret(store):
    config = _config(client_id="shipped-1")
    report = plugin_oauth.status(config)
    assert (report["state"], report["client_source"], report["client_configured"]) == (
        "signed_out", "plugin", False)
    client, source = await plugin_oauth.client_for_sign_in(config, CB)
    assert (client, source) == (OAuthClient("shipped-1"), "plugin")
    request = plugin_oauth.start_sign_in(config, client, CB)
    assert parse_qs(urlsplit(request.url).query)["client_id"] == ["shipped-1"]
    provider = Provider(_tokens())
    async with provider.client() as http:
        report = await plugin_oauth.complete_sign_in(config, request, "the-code", http=http, now=0.0)
    assert (report["state"], report["client_source"]) == ("signed_in", "plugin")
    form = provider.calls[0][1]
    assert form["client_id"] == "shipped-1" and "client_secret" not in form
    assert not any(k.endswith(":client") for k in store)
    # The refresh uses the shipped client too, still without a secret.
    provider = Provider(httpx.Response(200, json={"access_token": "fresh"}))
    async with provider.client() as http:
        assert await plugin_oauth.access_token_for_use(config, http=http, force_refresh=True) == "fresh"
    assert provider.calls[0][1]["client_id"] == "shipped-1" and "client_secret" not in provider.calls[0][1]


async def test_an_owner_client_wins_over_the_shipped_one(store):
    config = _config(client_id="shipped-1")
    _tokens_for(config, OAuthClient("shipped-1"))
    assert plugin_oauth.status(config)["state"] == "signed_in"
    plugin_oauth.save_client(PLUGIN, "example", "owner-1", SECRET)
    report = plugin_oauth.status(config)
    assert (report["state"], report["client_source"], report["client_configured"]) == (
        "signed_out", "owner", True)
    assert await plugin_oauth.client_for_sign_in(config, CB) == (OAuthClient("owner-1", SECRET), "owner")
    # A sign-in started with the shipped client cannot finish once the owner's is in effect.
    request = plugin_oauth.start_sign_in(config, OAuthClient("shipped-1"), CB)
    async with Provider().client() as http:
        with pytest.raises(PluginOAuthError) as caught:
            await plugin_oauth.complete_sign_in(config, request, "code", http=http)
    assert caught.value.reason == "not_configured"
    # Removing the owner's client falls back to the shipped one.
    plugin_oauth.delete_client(PLUGIN, "example")
    assert plugin_oauth.status(config)["client_source"] == "plugin"


async def test_registration_registers_a_public_client_once_per_address(store, caplog):
    caplog.set_level("DEBUG")
    config = _config(registration_url=REGISTER)
    report = plugin_oauth.status(config)
    assert (report["state"], report["client_source"]) == ("signed_out", "registered")
    provider = Provider(_registered("reg-1", client_secret=REG_SECRET), _registered("reg-2"))
    async with provider.client() as http:
        first = await plugin_oauth.client_for_sign_in(config, CB, http=http)
        again = await plugin_oauth.client_for_sign_in(config, CB, http=http)
        local = await plugin_oauth.client_for_sign_in(config, CB_LOCAL, http=http)
    assert first == again == (OAuthClient("reg-1", REG_SECRET), "registered")
    assert local == (OAuthClient("reg-2"), "registered")
    assert len(provider.calls) == 2
    url, body = provider.calls[0]
    assert url == REGISTER
    assert body == {
        "client_name": "AI Shuffle", "redirect_uris": [CB], "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
        "scope": "zone:read",
    }
    assert provider.calls[1][1]["redirect_uris"] == [CB_LOCAL]
    # Kept encrypted, with nothing readable in the row.
    [row] = [v for k, v in store.items() if k.endswith(":registered")]
    assert row.startswith("enc:") and "reg-1" not in row and REG_SECRET not in row

    # Signed in from the local address; the sign-in is usable from any chat.
    request = plugin_oauth.start_sign_in(config, local[0], CB_LOCAL)
    provider = Provider(_tokens(), httpx.Response(200, json={"access_token": "renewed-access-never-shown"}))
    async with provider.client() as http:
        report = await plugin_oauth.complete_sign_in(config, request, "the-code", http=http, now=0.0)
        assert (report["state"], report["client_source"]) == ("signed_in", "registered")
        assert await plugin_oauth.access_token_for_use(
            config, http=http, force_refresh=True) == "renewed-access-never-shown"
    assert provider.calls[0][1]["client_id"] == "reg-2" and provider.calls[1][1]["client_id"] == "reg-2"
    text = json.dumps(plugin_oauth.status(config)) + "\n".join(r.getMessage() for r in caplog.records)
    for secret in (REG_SECRET, ACCESS, REFRESH, "renewed-access-never-shown"):
        assert secret not in text


@pytest.mark.parametrize("response", [
    httpx.Response(400, json={"error": "invalid_client_metadata", "error_description": REG_SECRET}),
    httpx.Response(201, json={"client_secret": REG_SECRET}),
    httpx.Response(201, json={"client_id": "has space"}),
    httpx.Response(201, json={"client_id": "reg-1", "redirect_uris": ["https://elsewhere.example/cb"]}),
    httpx.Response(201, text="not json"),
], ids=["refused", "no-client-id", "bad-client-id", "other-redirect", "not-json"])
async def test_a_refused_registration_falls_back_to_an_owner_client(store, caplog, response):
    caplog.set_level("DEBUG")
    config = _config(registration_url=REGISTER)
    provider = Provider(response, _registered("reg-1"))
    async with provider.client() as http:
        with pytest.raises(PluginOAuthError) as caught:
            await plugin_oauth.client_for_sign_in(config, CB, http=http, now=1000.0)
        assert caught.value.reason == "registration_failed"
        assert str(caught.value) == plugin_oauth._MESSAGES["registration_failed"]
        report = plugin_oauth.status(config, now=1000.0)
        assert (report["state"], report["reason"], report["client_source"]) == (
            "not_configured", "registration_failed", "none")
        # Not tried again for a while: the owner can enter a client meanwhile.
        with pytest.raises(PluginOAuthError):
            await plugin_oauth.client_for_sign_in(config, CB, http=http, now=1000.0 + 60)
        assert len(provider.calls) == 1
        # Later it is tried again.
        later = 1000.0 + plugin_oauth.REGISTRATION_RETRY + 1
        assert plugin_oauth.status(config, now=later)["state"] == "signed_out"
        assert await plugin_oauth.client_for_sign_in(config, CB, http=http, now=later) == (
            OAuthClient("reg-1"), "registered")
    # An owner client works at once, whatever the registration did.
    plugin_oauth.save_client(PLUGIN, "example", "owner-1")
    assert plugin_oauth.status(config)["client_source"] == "owner"
    logs = "\n".join(r.getMessage() for r in caplog.records)
    assert REG_SECRET not in logs and REG_SECRET not in str(caught.value)


async def test_an_unreachable_registration_is_not_remembered(store):
    config = _config(registration_url=REGISTER)
    provider = Provider(httpx.ConnectError("down"), httpx.Response(503))
    async with provider.client() as http:
        for _ in range(2):
            with pytest.raises(PluginOAuthError) as caught:
                await plugin_oauth.client_for_sign_in(config, CB, http=http)
            assert caught.value.reason == "unavailable"
    assert plugin_oauth.status(config)["state"] == "signed_out"


async def test_no_client_anywhere_needs_an_owner_client(store):
    config = _config()
    report = plugin_oauth.status(config)
    assert (report["state"], report["client_source"]) == ("not_configured", "none")
    with pytest.raises(PluginOAuthError) as caught:
        await plugin_oauth.client_for_sign_in(config, CB)
    assert caught.value.reason == "not_configured"
    with pytest.raises(PluginOAuthError) as caught:
        await plugin_oauth.access_token_for_use(config)
    assert caught.value.reason == "not_configured"
    with pytest.raises(PluginOAuthError) as caught:
        await plugin_oauth.client_for_sign_in(_config(client_id="shipped-1"), "http://192.168.1.5/cb")
    assert caught.value.reason == "invalid_redirect"


@pytest.mark.parametrize(("change", "reason"), [
    ({"client_id": "shipped-2"}, "client_changed"),
    ({"registration_url": "https://dash.example.com/oauth2/register-v2"}, "endpoint_changed"),
    ({"authorize_url": "https://dash.example.com/oauth2/authorize"}, "endpoint_changed"),
    ({"revoke_url": None}, "endpoint_changed"),
    ({"token_url": "https://dash.example.com/oauth2/token-v2"}, "endpoint_changed"),
    ({"scopes": ["zone:read", "zone:edit"]}, "scopes_widened"),
])
async def test_a_changed_declaration_needs_a_new_sign_in(store, change, reason):
    declared = {"client_id": "shipped-1", "registration_url": REGISTER}
    config = _config(**declared)
    _tokens_for(config, OAuthClient("shipped-1"))
    assert plugin_oauth.status(config)["state"] == "signed_in"
    updated = _config(**{**declared, **change})
    report = plugin_oauth.status(updated)
    assert (report["state"], report["reason"]) == ("needs_sign_in", reason)
    with pytest.raises(PluginOAuthError) as caught:
        await plugin_oauth.access_token_for_use(updated)
    assert caught.value.reason == "needs_sign_in"


def test_an_owner_sign_in_also_ends_when_the_shipped_client_changes(store):
    config = _config(client_id="shipped-1")
    _sign_in(config)  # with the owner's client-1
    assert plugin_oauth.status(config)["state"] == "signed_in"
    report = plugin_oauth.status(_config(client_id="shipped-2"))
    assert (report["state"], report["reason"]) == ("needs_sign_in", "client_changed")


async def test_a_registered_sign_in_ends_when_its_registration_is_gone(store):
    config = _config(registration_url=REGISTER)
    async with Provider(_registered("reg-1")).client() as http:
        client, _ = await plugin_oauth.client_for_sign_in(config, CB, http=http)
    _tokens_for(config, client)
    assert plugin_oauth.status(config)["state"] == "signed_in"
    store.pop(plugin_oauth._key(PLUGIN, "example", "registered"))
    report = plugin_oauth.status(config)
    assert (report["state"], report["reason"]) == ("needs_sign_in", "client_changed")


def test_a_sign_in_saved_before_declarations_were_recorded_still_works(store):
    config = _config()
    _sign_in(config)
    key = plugin_oauth._key(PLUGIN, "example", "tokens")
    data = plugin_oauth._open(key)
    data.pop("declaration")
    data.pop("binding")
    plugin_oauth._seal(key, data)
    assert plugin_oauth.load_tokens(PLUGIN, "example").declaration == {}
    assert plugin_oauth.status(config)["state"] == "signed_in"
