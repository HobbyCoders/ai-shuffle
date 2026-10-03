"""Owner sign-in routes for plugin services (app/api/plugin_oauth.py).

The provider is a fake token endpoint behind ``httpx.MockTransport``; storage
is an in-memory ``system_settings`` behind the real encryption. Nothing here
reaches a network.
"""

import json
import logging
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api import plugin_oauth as api
from app.core import encryption, plugin_oauth
from app.core.plugin_oauth import PluginOAuthError

PLUGIN = "dns-reader@mp"
SERVICE = "example"
CLIENT_SECRET = "client-secret-never-shown"
CODE = "auth-code-never-shown"
ACCESS = "access-token-never-shown"
REFRESH = "refresh-token-never-shown"
BASE = "https://shuffle.test"
CALLBACK = BASE + api.CALLBACK


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
        {"name": SERVICE, "provider": "custom", "url": "https://api.example.com/v4",
         "access": "read", "purpose": "Read DNS", "auth": auth},
        {"name": "plain", "provider": "custom", "url": "https://api.example.com/v4",
         "access": "read", "purpose": "No sign-in"},
    ]}}}


class Provider:
    """A fake token/revocation endpoint that records each form it receives."""

    def __init__(self):
        self.responses: list = []
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        self.calls.append((str(request.url), form))
        if self.responses:
            return self.responses.pop(0)
        return httpx.Response(200, json={"access_token": ACCESS, "token_type": "Bearer",
                                         "expires_in": 3600, "refresh_token": REFRESH})


@pytest.fixture
def plugin(monkeypatch):
    """One installed plugin; ``on`` is "switched on and approved"."""
    state = SimpleNamespace(on=True, installed=True, manifest=_manifest())

    def installed_manifest(plugin_id, *, require_on):
        if plugin_id != PLUGIN or not state.installed:
            raise PluginOAuthError("not_installed")
        if require_on and not state.on:
            raise PluginOAuthError("plugin_off")
        return state.manifest

    monkeypatch.setattr(plugin_oauth, "_installed_manifest", installed_manifest)
    return state


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


@pytest.fixture
def provider(monkeypatch):
    fake = Provider()
    real_post = plugin_oauth._post

    async def post(url, data, http):
        async with httpx.AsyncClient(transport=httpx.MockTransport(fake)) as client:
            return await real_post(url, data, client)

    monkeypatch.setattr(plugin_oauth, "_post", post)
    return fake


@pytest.fixture
def owner():
    return SimpleNamespace(token="owner-session")


@pytest.fixture
def app(owner):
    api._pending.clear()
    application = FastAPI()
    application.include_router(api.router)
    application.dependency_overrides[api.require_admin] = lambda: owner.token
    application.dependency_overrides[api.optional_owner] = lambda: owner.token
    yield application
    api._pending.clear()


def _client(app, base=BASE) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app), base_url=base)


async def _configure(client):
    response = await client.put("/api/v1/plugins/oauth/client", json={
        "plugin": PLUGIN, "service": SERVICE, "client_id": "client-1", "client_secret": CLIENT_SECRET})
    assert response.status_code == 200, response.text
    return response


async def _start(client) -> tuple[dict, str]:
    response = await client.post("/api/v1/plugins/oauth/start", json={"plugin": PLUGIN, "service": SERVICE})
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    started = response.json()
    query = {k: v[0] for k, v in parse_qs(urlsplit(started["authorization_url"]).query).items()}
    return started, query["state"]


async def test_sign_in_happy_path_saves_tokens_and_shows_none(app, store, plugin, provider, caplog):
    caplog.set_level(logging.DEBUG)
    async with _client(app) as client:
        saved = await _configure(client)
        assert CLIENT_SECRET not in saved.text
        status = (await client.get("/api/v1/plugins/oauth/status", params={"plugin": PLUGIN})).json()
        assert status["redirect_uri"] == CALLBACK and status["enabled"] is True
        # Only the service that declares a sign-in is listed.
        assert [s["service"] for s in status["services"]] == [SERVICE]
        entry = status["services"][0]
        assert entry["state"] == "signed_out" and entry["client_id"] == "client-1"
        assert entry["authorize_host"] == "dash.example.com" and entry["access"] == "read"

        started, state = await _start(client)
        assert started["redirect_uri"] == CALLBACK and started["expires_in"] == api.TTL
        query = {k: v[0] for k, v in parse_qs(urlsplit(started["authorization_url"]).query).items()}
        assert query["redirect_uri"] == CALLBACK and query["client_id"] == "client-1"
        assert query["code_challenge_method"] == "S256" and query["scope"] == "zone:read"
        assert CLIENT_SECRET not in json.dumps(started)

        page = await client.get("/api/v1/plugins/oauth/callback", params={"state": state, "code": CODE})
        assert page.status_code == 200 and "Signed in to example" in page.text
        assert page.headers["cache-control"] == "no-store"
        assert page.headers["referrer-policy"] == "no-referrer"
        assert started["flow_id"] in page.text and '"state": "signed_in"' in page.text
        for secret in (CODE, ACCESS, REFRESH, CLIENT_SECRET, state):
            assert secret not in page.text
        url, form = provider.calls[0]
        assert url == "https://dash.example.com/oauth2/token"
        assert form["code"] == CODE and form["redirect_uri"] == CALLBACK and form["code_verifier"]

        status = (await client.get("/api/v1/plugins/oauth/status",
                                   params={"plugin": PLUGIN, "service": SERVICE})).json()
        assert status["services"][0]["state"] == "signed_in"
        assert all(secret not in json.dumps(status) for secret in (ACCESS, REFRESH, CLIENT_SECRET))

        # A replayed callback finds nothing: the state was single-use.
        replay = await client.get("/api/v1/plugins/oauth/callback", params={"state": state, "code": CODE})
        assert replay.status_code == 400 and len(provider.calls) == 1
    # The test client logs the URLs it (as the browser) requests; everything
    # the worker logs must be free of the code, tokens and client secret.
    worker_logs = "\n".join(r.getMessage() for r in caplog.records if BASE not in r.getMessage())
    assert "dash.example.com/oauth2/token" in worker_logs
    for secret in (CODE, ACCESS, REFRESH, CLIENT_SECRET):
        assert secret not in worker_logs


async def test_unknown_state_and_another_browser_are_refused(app, store, plugin, provider):
    async with _client(app) as client:
        await _configure(client)
        _, state = await _start(client)
        unknown = await client.get("/api/v1/plugins/oauth/callback", params={"state": "made-up", "code": CODE})
        assert unknown.status_code == 400
        missing = await client.get("/api/v1/plugins/oauth/callback", params={"code": CODE})
        assert missing.status_code == 400
    # Another browser has the state but not the HttpOnly cookie. Its attempt
    # does not consume the sign-in of the browser that started it.
    async with _client(app) as other:
        stolen = await other.get("/api/v1/plugins/oauth/callback", params={"state": state, "code": CODE})
        assert stolen.status_code == 400
    assert state in api._pending and provider.calls == []


async def test_another_owner_session_is_refused(app, store, plugin, provider, owner):
    async with _client(app) as client:
        await _configure(client)
        _, state = await _start(client)
        owner.token = "a-different-session"
        response = await client.get("/api/v1/plugins/oauth/callback", params={"state": state, "code": CODE})
        assert response.status_code == 400 and provider.calls == []
        owner.token = None
        response = await client.get("/api/v1/plugins/oauth/callback", params={"state": state, "code": CODE})
        assert response.status_code == 400 and provider.calls == []


async def test_expired_state_is_refused_and_dropped(app, store, plugin, provider):
    async with _client(app) as client:
        await _configure(client)
        _, state = await _start(client)
        api._pending[state].expires = 0
        response = await client.get("/api/v1/plugins/oauth/callback", params={"state": state, "code": CODE})
        assert response.status_code == 400 and "expired" in response.json()["detail"]
        assert state not in api._pending and provider.calls == []


async def test_a_plugin_that_is_off_or_unapproved_cannot_start(app, store, plugin, provider):
    async with _client(app) as client:
        await _configure(client)
        plugin.on = False
        response = await client.post("/api/v1/plugins/oauth/start", json={"plugin": PLUGIN, "service": SERVICE})
        assert response.status_code == 409
        assert response.json()["detail"] == plugin_oauth._MESSAGES["plugin_off"]
        status = (await client.get("/api/v1/plugins/oauth/status", params={"plugin": PLUGIN})).json()
        assert status["enabled"] is False
        # Turned off between start and callback: nothing is saved.
        plugin.on = True
        _, state = await _start(client)
        plugin.on = False
        page = await client.get("/api/v1/plugins/oauth/callback", params={"state": state, "code": CODE})
        assert page.status_code == 200 and "did not complete" in page.text
        assert provider.calls == [] and not any(k.endswith(":tokens") for k in store)


async def test_a_changed_declaration_between_start_and_callback_saves_nothing(app, store, plugin, provider):
    async with _client(app) as client:
        await _configure(client)
        _, state = await _start(client)
        plugin.manifest = _manifest(scopes=["zone:read", "zone:edit"])
        page = await client.get("/api/v1/plugins/oauth/callback", params={"state": state, "code": CODE})
        assert "did not complete" in page.text and provider.calls == []


async def test_start_needs_a_client_and_an_https_or_local_address(app, store, plugin, provider):
    async with _client(app) as client:
        response = await client.post("/api/v1/plugins/oauth/start", json={"plugin": PLUGIN, "service": SERVICE})
        assert response.status_code == 409
        assert response.json()["detail"] == plugin_oauth._MESSAGES["not_configured"]
        await _configure(client)
        response = await client.post("/api/v1/plugins/oauth/start", json={"plugin": PLUGIN, "service": "plain"})
        assert response.status_code == 404
    # A plain-HTTP LAN address cannot be a return address, and is not a
    # transport the client secret may travel over.
    async with _client(app, "http://192.168.1.20:8000") as lan:
        response = await lan.post("/api/v1/plugins/oauth/start", json={"plugin": PLUGIN, "service": SERVICE})
        assert response.status_code == 400
        assert response.json()["detail"] == plugin_oauth._MESSAGES["invalid_redirect"]
        response = await lan.put("/api/v1/plugins/oauth/client", json={
            "plugin": PLUGIN, "service": SERVICE, "client_id": "client-2", "client_secret": CLIENT_SECRET})
        assert response.status_code == 403 and CLIENT_SECRET not in response.text
    async with _client(app, "http://localhost:8000") as local:
        _, state = await _start(local)
        assert api._pending[state].request.redirect_uri == "http://localhost:8000" + api.CALLBACK


async def test_cancelled_at_the_provider_changes_nothing(app, store, plugin, provider):
    async with _client(app) as client:
        await _configure(client)
        _, state = await _start(client)
        page = await client.get("/api/v1/plugins/oauth/callback", params={"state": state, "error": "access_denied"})
        assert "cancelled" in page.text and provider.calls == []
        assert state not in api._pending


async def test_a_refused_code_reports_fixed_wording(app, store, plugin, provider):
    provider.responses.append(httpx.Response(400, json={"error": "invalid_grant", "detail": ACCESS}))
    async with _client(app) as client:
        await _configure(client)
        _, state = await _start(client)
        page = await client.get("/api/v1/plugins/oauth/callback", params={"state": state, "code": CODE})
        assert plugin_oauth._MESSAGES["refused"] in page.text and ACCESS not in page.text


async def test_client_validation_never_echoes_the_secret(app, store, plugin):
    async with _client(app) as client:
        response = await client.put("/api/v1/plugins/oauth/client", json={
            "plugin": PLUGIN, "service": SERVICE, "client_id": "client-1",
            "client_secret": CLIENT_SECRET, "extra": CLIENT_SECRET})
        assert response.status_code == 422 and CLIENT_SECRET not in response.text
        response = await client.put("/api/v1/plugins/oauth/client", json={
            "plugin": PLUGIN, "service": SERVICE, "client_id": "has space", "client_secret": CLIENT_SECRET})
        assert response.status_code == 400 and CLIENT_SECRET not in response.text


async def test_sign_out_revokes_and_cleans_up_after_uninstall(app, store, plugin, provider):
    async with _client(app) as client:
        await _configure(client)
        _, state = await _start(client)
        await client.get("/api/v1/plugins/oauth/callback", params={"state": state, "code": CODE})
        result = (await client.post("/api/v1/plugins/oauth/sign-out",
                                    json={"plugin": PLUGIN, "service": SERVICE})).json()
        assert result == {"signed_out": True, "revoked": True}
        url, form = provider.calls[-1]
        assert url == "https://dash.example.com/oauth2/revoke" and form["token"] == REFRESH
        # Uninstalled: the local sign-in and client are still removable.
        _, state = await _start(client)
        await client.get("/api/v1/plugins/oauth/callback", params={"state": state, "code": CODE})
        plugin.installed = False
        result = (await client.post("/api/v1/plugins/oauth/sign-out",
                                    json={"plugin": PLUGIN, "service": SERVICE})).json()
        assert result == {"signed_out": True, "revoked": False}
        deleted = await client.delete("/api/v1/plugins/oauth/client", params={"plugin": PLUGIN, "service": SERVICE})
        assert deleted.json() == {"deleted": True} and store == {}


async def test_pending_sign_ins_are_bounded(app, store, plugin, provider, monkeypatch):
    monkeypatch.setattr(api, "MAX_PENDING", 2)
    async with _client(app) as client:
        await _configure(client)
        await _start(client)
        await _start(client)
        response = await client.post("/api/v1/plugins/oauth/start", json={"plugin": PLUGIN, "service": SERVICE})
        assert response.status_code == 429


def test_the_callback_is_routed_before_the_plugin_catch_all():
    from app.api import plugins
    from app.main import app as main_app

    # This FastAPI keeps each included router as one entry ("original_router").
    routers = [getattr(route, "original_router", route) for route in main_app.routes]
    assert routers.index(api.router) < routers.index(plugins.router)


# ─── which plugins count as installed, on and approved ──────────────────────


def _installed(monkeypatch, tmp_path, **details):
    root = tmp_path / "dns-reader"
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(json.dumps(_manifest()), encoding="utf-8")
    record = SimpleNamespace(install_path=str(root), version="1.0.0", enabled=True, approval=None)
    record.__dict__.update(details)
    service = SimpleNamespace(get_plugin_details=lambda plugin_id: record if plugin_id == PLUGIN else None)
    from app.core import plugin_service
    monkeypatch.setattr(plugin_service, "get_plugin_service", lambda: service)


@pytest.mark.parametrize("details, reason", [
    ({"enabled": False}, "plugin_off"),
    ({"approval": "pending"}, "plugin_off"),
    ({"enabled": True, "approval": "pending"}, "plugin_off"),
])
def test_only_switched_on_approved_plugins_are_usable(monkeypatch, tmp_path, details, reason):
    _installed(monkeypatch, tmp_path, **details)
    with pytest.raises(PluginOAuthError) as caught:
        plugin_oauth.installed_config(PLUGIN, SERVICE)
    assert caught.value.reason == reason
    assert plugin_oauth.plugin_ready(PLUGIN) is False
    # Reading the declaration (status, sign-out) does not need the plugin on.
    assert plugin_oauth.installed_config(PLUGIN, SERVICE, require_on=False).service == SERVICE


def test_an_installed_plugin_declaration_is_read_from_disk(monkeypatch, tmp_path):
    _installed(monkeypatch, tmp_path)
    config = plugin_oauth.installed_config(PLUGIN, SERVICE)
    assert config.service_url == "https://api.example.com/v4" and config.access == "read"
    assert plugin_oauth.plugin_ready(PLUGIN) is True
    assert plugin_oauth.installed_services(PLUGIN) == [SERVICE]
    with pytest.raises(PluginOAuthError) as caught:
        plugin_oauth.installed_config("other@mp", SERVICE)
    assert caught.value.reason == "not_installed"
