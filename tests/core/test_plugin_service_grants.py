"""Plugin service grants: RequestChatAccess(plugin, service) and ChatHttpRequest
with the owner's OAuth sign-in (app/core/chat_access.py).

The API is the TLS test service from test_chat_services; the token endpoint is
a fake behind ``httpx.MockTransport``; sign-in storage is in memory behind the
real encryption. The agent side never sees a token.
"""

import json
import logging
from dataclasses import replace
from types import SimpleNamespace
from urllib.parse import parse_qs

import httpx
import pytest
from cryptography.fernet import Fernet

from app.core import encryption, plugin_oauth
from app.core.chat_access import AccessDenied, GrantInput, GrantSubmission
from app.core.plugin_oauth import OAuthClient, OAuthConfig, PluginOAuthError, TokenSet
from tests.core.test_chat_services import (  # noqa: F401 - fixtures
    broker,
    pki,
    service,
    trust,
)

PLUGIN = "dns-reader@mp"
SERVICE = "inventory"
ACCESS = "plugin-access-token-0001"
FRESH = "plugin-access-token-0002"
REFRESH = "plugin-refresh-token-0001"
CLIENT_SECRET = "plugin-client-secret-0001"
SECRETS = (ACCESS, FRESH, REFRESH, CLIENT_SECRET)


class Provider:
    """A fake token endpoint: answers refreshes with ``FRESH`` unless told otherwise."""

    def __init__(self):
        self.responses: list = []
        self.calls: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append({k: v[0] for k, v in parse_qs(request.content.decode()).items()})
        if self.responses:
            response = self.responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response
        return httpx.Response(200, json={"access_token": FRESH, "token_type": "Bearer", "expires_in": 3600})


@pytest.fixture
def declared(service):
    """The plugin's declaration, switched on; ``config`` is what it declares now."""
    config = OAuthConfig(
        plugin_id=PLUGIN, service=SERVICE, service_url=f"https://127.0.0.1:{service.port}/api",
        authorize_url="https://auth.test/authorize", token_url="https://auth.test/token",
        scopes=("inventory:read",), access="read",
    )
    return SimpleNamespace(on=True, config=config)


@pytest.fixture
def oauth(monkeypatch, declared):
    rows: dict[str, str] = {}
    monkeypatch.setattr(encryption, "_encryption_key", Fernet.generate_key())
    monkeypatch.setattr(plugin_oauth, "settings", SimpleNamespace(effective_instance_id="worker-one"))
    monkeypatch.setattr(plugin_oauth.database, "get_system_setting", lambda k: rows.get(k))
    monkeypatch.setattr(plugin_oauth.database, "set_system_setting", lambda k, v: rows.__setitem__(k, v))
    monkeypatch.setattr(plugin_oauth.database, "delete_system_setting", lambda k: rows.pop(k, None) is not None)
    plugin_oauth._locks.clear()

    def installed_config(plugin_id, service_name, *, require_on=True):
        if plugin_id != PLUGIN:
            raise PluginOAuthError("not_installed")
        if service_name != SERVICE:
            raise PluginOAuthError("not_declared")
        if require_on and not declared.on:
            raise PluginOAuthError("plugin_off")
        return declared.config

    monkeypatch.setattr(plugin_oauth, "installed_config", installed_config)
    provider = Provider()
    real_post = plugin_oauth._post

    async def post(url, data, http):
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            return await real_post(url, data, client)

    monkeypatch.setattr(plugin_oauth, "_post", post)
    return SimpleNamespace(rows=rows, provider=provider)


def _sign_in(declared, *, refresh=REFRESH):
    client = OAuthClient("client-1", CLIENT_SECRET)
    plugin_oauth.save_client(PLUGIN, SERVICE, client.client_id, client.client_secret)
    plugin_oauth.save_tokens(declared.config, client, TokenSet(
        access_token=ACCESS, refresh_token=refresh, expires_at=None, scopes=declared.config.scopes))


async def _grant(broker, **submission) -> dict:
    pending = await broker.request("chat", "service", "Read the inventory", plugin=PLUGIN, service=SERVICE)
    result = await broker.fulfill("chat", pending["request_id"], GrantSubmission(sign_in=True, **submission))
    assert result == {"granted": True, "saved": False}
    return (await broker.resources("chat"))["grants"][0]


def _clean(*values) -> None:
    text = json.dumps(values, default=str)
    for secret in SECRETS:
        assert secret not in text


async def test_the_card_names_the_plugin_service_and_only_a_sign_in_answers_it(
        broker, service, trust, declared, oauth):
    pending = await broker.request("chat", "service", "Read the inventory", plugin=PLUGIN, service=SERVICE)
    assert set(pending) == {"request_id", "status", "kind", "expires_at"}
    card = (await broker.snapshot("chat"))["requests"][0]
    assert card["plugin"] == PLUGIN and card["service"] == SERVICE
    assert card["suggested_url"] == declared.config.service_url and card["service_provider"] == "custom"
    # A typed key or another address cannot answer a plugin request.
    typed = GrantInput(kind="service", label="x", url=f"https://127.0.0.1:{service.port}/api", secret="k" * 12)
    with pytest.raises(AccessDenied, match="signing in"):
        await broker.fulfill("chat", pending["request_id"], GrantSubmission(connection=typed))
    with pytest.raises(AccessDenied, match="signing in"):
        await broker.fulfill("chat", pending["request_id"], GrantSubmission(connection=typed, sign_in=True))
    # Not signed in yet: refused, and the card stays open for the sign-in.
    with pytest.raises(AccessDenied, match="Sign in to inventory first"):
        await broker.fulfill("chat", pending["request_id"], GrantSubmission(sign_in=True))
    assert pending["request_id"] in broker.requests
    _sign_in(declared)
    with pytest.raises(AccessDenied, match="read-only"):
        await broker.fulfill("chat", pending["request_id"], GrantSubmission(sign_in=True, writable=True))
    await broker.fulfill("chat", pending["request_id"], GrantSubmission(sign_in=True, minutes=5))
    handle = (await broker.resources("chat"))["grants"][0]
    assert set(handle) == {"id", "kind", "expires_at", "writable"} and handle["writable"] is False
    grant = (await broker.snapshot("chat"))["grants"][0]
    assert grant["plugin"] == PLUGIN and grant["service"] == SERVICE
    assert grant["target"] == declared.config.service_url
    # The grant checked TLS only; nothing was sent and no token was used.
    assert service.requests == [] and oauth.provider.calls == []
    _clean(pending, card, handle, grant)


@pytest.mark.parametrize("args, message", [
    ({"plugin": PLUGIN}, "plugin id and the service name"),
    ({"plugin": PLUGIN, "service": SERVICE, "suggested_url": "https://api.example.com"}, "omit service_provider"),
    ({"plugin": "other@mp", "service": SERVICE}, "not installed"),
    ({"plugin": PLUGIN, "service": "other"}, "does not declare"),
])
async def test_bad_plugin_requests_open_no_card(broker, declared, oauth, args, message):
    with pytest.raises(AccessDenied, match=message):
        await broker.request("chat", "service", "Read", **args)
    assert not broker.requests


async def test_a_plugin_that_is_off_cannot_be_requested(broker, declared, oauth):
    declared.on = False
    with pytest.raises(AccessDenied, match="Turn this plugin on"):
        await broker.request("chat", "service", "Read", plugin=PLUGIN, service=SERVICE)
    with pytest.raises(AccessDenied):
        await broker.request("chat", "folder", "Read", plugin=PLUGIN, service=SERVICE)


async def test_requests_carry_the_token_and_results_never_show_it(broker, service, trust, declared, oauth, caplog):
    caplog.set_level(logging.DEBUG)
    _sign_in(declared)
    handle = await _grant(broker)
    result = await broker.run("chat", handle["id"], "http", {"method": "GET", "path": "/echo?x=1"})
    sent = service.requests[0]
    assert sent["target"] == "/api/echo?x=1" and sent["headers"]["authorization"] == f"Bearer {ACCESS}"
    assert result["status"] == 200 and "[redacted]" in result["body"]
    # Declared read access keeps GET/HEAD only.
    with pytest.raises(AccessDenied, match="read-only"):
        await broker.run("chat", handle["id"], "http", {"method": "POST", "path": "/items", "body": "{}"})
    assert len(service.requests) == 1 and oauth.provider.calls == []
    _clean(result, await broker.resources("chat"), await broker.snapshot("chat"))
    assert all(secret not in caplog.text for secret in SECRETS)


@pytest.mark.parametrize("path", [
    "//evil.example/steal", "/../outside", "/%2e%2e/outside", "/a/..;/b", "https://evil.example/x", "/x\\y",
])
async def test_requests_stay_on_the_declared_address(broker, service, trust, declared, oauth, path):
    _sign_in(declared)
    handle = await _grant(broker)
    with pytest.raises(AccessDenied):
        await broker.run("chat", handle["id"], "http", {"method": "GET", "path": path})
    assert service.requests == [] and oauth.provider.calls == []


async def test_a_changed_declaration_or_switched_off_plugin_stops_the_grant(broker, service, trust, declared, oauth):
    _sign_in(declared)
    handle = await _grant(broker)
    declared.config = replace(declared.config, service_url="https://elsewhere.example/api")
    with pytest.raises(AccessDenied, match="changed"):
        await broker.run("chat", handle["id"], "http", {"method": "GET", "path": "/echo"})
    declared.config = replace(declared.config, service_url=f"https://127.0.0.1:{service.port}/api")
    declared.on = False
    with pytest.raises(AccessDenied, match="Turn this plugin on"):
        await broker.run("chat", handle["id"], "http", {"method": "GET", "path": "/echo"})
    assert service.requests == []


async def test_a_401_refreshes_once_and_retries_once(broker, service, trust, declared, oauth, caplog):
    caplog.set_level(logging.DEBUG)
    _sign_in(declared)
    handle = await _grant(broker)
    service.rejected = {f"Bearer {ACCESS}"}
    # The old token rides in the query to prove it is redacted from the retry too.
    result = await broker.run("chat", handle["id"], "http", {"method": "GET", "path": f"/items?old={ACCESS}"})
    assert result["status"] == 200 and "[redacted]" in result["body"]
    assert [r["headers"]["authorization"] for r in service.requests] == [f"Bearer {ACCESS}", f"Bearer {FRESH}"]
    assert len(oauth.provider.calls) == 1 and oauth.provider.calls[0]["grant_type"] == "refresh_token"
    assert oauth.provider.calls[0]["refresh_token"] == REFRESH
    # The refreshed token is saved and reused without another refresh.
    await broker.run("chat", handle["id"], "http", {"method": "GET", "path": "/items"})
    assert service.requests[-1]["headers"]["authorization"] == f"Bearer {FRESH}"
    assert len(oauth.provider.calls) == 1
    _clean(result)
    # httpx logs request URLs, and this test put the old token in one on purpose.
    logs = "\n".join(r.getMessage() for r in caplog.records if "?old=" not in r.getMessage())
    assert all(secret not in logs for secret in SECRETS)


async def test_a_second_401_is_returned_without_another_refresh(broker, service, trust, declared, oauth):
    _sign_in(declared)
    handle = await _grant(broker)
    service.rejected = {f"Bearer {ACCESS}", f"Bearer {FRESH}"}
    result = await broker.run("chat", handle["id"], "http", {"method": "GET", "path": "/items"})
    assert result["status"] == 401 and len(service.requests) == 2 and len(oauth.provider.calls) == 1
    _clean(result)


@pytest.mark.parametrize("answer, message, kept", [
    (httpx.Response(400, json={"error": "invalid_grant", "error_description": REFRESH}),
     "Sign in to this service again.", False),
    (httpx.ConnectError("down"), "The service could not be reached. Try again later.", True),
])
async def test_a_failed_refresh_is_reported_plainly(broker, service, trust, declared, oauth, answer, message, kept):
    _sign_in(declared)
    handle = await _grant(broker)
    service.rejected = {f"Bearer {ACCESS}"}
    oauth.provider.responses.append(answer)
    with pytest.raises(AccessDenied) as caught:
        await broker.run("chat", handle["id"], "http", {"method": "GET", "path": "/items"})
    assert str(caught.value) == f"The sign-in for {SERVICE} could not be used. {message}"
    _clean(str(caught.value))
    assert len(service.requests) == 1
    # A refused refresh removes the dead sign-in; an outage keeps it.
    assert plugin_oauth.status(declared.config)["state"] == ("signed_in" if kept else "signed_out")


async def test_a_signed_out_service_refuses_before_sending(broker, service, trust, declared, oauth):
    _sign_in(declared)
    handle = await _grant(broker)
    plugin_oauth.delete_tokens(PLUGIN, SERVICE)
    with pytest.raises(AccessDenied, match="Sign in to this service again"):
        await broker.run("chat", handle["id"], "http", {"method": "GET", "path": "/items"})
    assert service.requests == []


async def test_write_access_follows_the_declaration_and_one_shot_approval(broker, service, trust, declared, oauth):
    declared.config = replace(declared.config, access="write")
    _sign_in(declared)
    handle = await _grant(broker, writable=True)
    assert handle["writable"] is True
    import asyncio

    from tests.core.test_chat_services import pending as pending_action
    run = asyncio.create_task(broker.run("chat", handle["id"], "http",
                                         {"method": "POST", "path": "/items", "body": "{}"}))
    action = await pending_action(broker)
    assert action.arguments == {"method": "POST", "path": "/items", "body": "{}", "content_type": "application/json"}
    _clean(action.public())
    await broker.decide("chat", action.id, True)
    result = await run
    assert result["status"] == 200 and service.requests[0]["headers"]["authorization"] == f"Bearer {ACCESS}"


async def test_the_tools_return_no_token(broker, service, trust, declared, oauth, monkeypatch):
    from app.core.provider.sdk_tool_records import recorded_sdk_server_tools
    from app.mcp import chat_access_sdk as sdk

    monkeypatch.setattr(sdk, "broker", broker)
    tools = {t.name: t for t in recorded_sdk_server_tools(sdk.create_chat_access_server("chat"))}
    response = await tools["RequestChatAccess"].handler(
        {"kind": "service", "purpose": "Read the inventory", "plugin": PLUGIN, "service": SERVICE})
    pending = json.loads(response["content"][0]["text"])
    assert pending["status"] == "awaiting_user"
    _sign_in(declared)
    await broker.fulfill("chat", pending["request_id"], GrantSubmission(sign_in=True))
    handle = (await broker.resources("chat"))["grants"][0]
    response = await tools["ChatHttpRequest"].handler(
        {"grant_id": handle["id"], "method": "GET", "path": "/echo", "body": "", "content_type": ""})
    assert not response.get("isError") and json.loads(response["content"][0]["text"])["status"] == 200
    _clean(response, await tools["ChatResources"].handler({}))
