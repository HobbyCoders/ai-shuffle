"""Secret hand-off: a key a service creates goes to a GitHub secret without the agent seeing it.

Real TLS against a local service that answers like Cloudflare's token API and
GitHub's Actions secrets API. The value travels only inside the broker; tests
check every tool result, card and listing for it, and open GitHub's sealed box
with the repository's private key to prove what was written.
"""

import asyncio
import base64
import json
import ssl
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from nacl import public

from app.core import vault_access, vault_services
from app.core.vault_access import AccessDenied
from tests.core.test_vault_services import broker, pending, pki, spec, trust  # noqa: F401 - fixtures

NEW_TOKEN = "cf-test-only-new-token-ABCDEFGHIJ0123456789"
REPO_KEY = public.PrivateKey.generate()
KEY_PATH = "/repos/example-org/website/environments/production/secrets/public-key"
SECRET_PATH = "/repos/example-org/website/environments/production/secrets/CLOUDFLARE_API_TOKEN"


class Api:
    """Answers each (method, path) with what a test set; records every request."""

    def __init__(self):
        self.port = 0
        self.requests = []
        self.routes = {
            ("POST", "/client/v4/accounts/acct/tokens"): (
                "200 OK", {"success": True, "result": {"id": "tok_1", "name": "deploy", "value": NEW_TOKEN}}),
            ("GET", KEY_PATH): (
                "200 OK", {"key_id": "key-1", "key": base64.b64encode(bytes(REPO_KEY.public_key)).decode()}),
            ("PUT", SECRET_PATH): ("201 Created", None),
            ("GET", "/client/v4/echo"): ("200 OK", {"note": f"you sent {NEW_TOKEN}"}),
        }

    async def handle(self, reader, writer):
        try:
            head = await reader.readuntil(b"\r\n\r\n")
        except (asyncio.IncompleteReadError, ConnectionError):
            writer.close()
            return
        lines = head.decode("latin-1").split("\r\n")
        method, target, _ = lines[0].split(" ", 2)
        headers = {k.strip().lower(): v.strip() for k, v in (line.split(":", 1) for line in lines[1:] if line)}
        body = await reader.readexactly(int(headers.get("content-length", "0")))
        self.requests.append({"method": method, "target": target, "headers": headers, "body": body.decode()})
        status, payload = self.routes.get((method, target), ("404 Not Found", {"message": "Not Found"}))
        data = b"" if payload is None else json.dumps(payload).encode()
        writer.write((f"HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {len(data)}\r\n"
                      "Connection: close\r\n\r\n").encode() + data)
        try:
            await writer.drain()
        finally:
            writer.close()


@pytest_asyncio.fixture
async def api(pki, monkeypatch):  # noqa: F811
    _, folder = pki
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.load_cert_chain(folder / "leaf.pem", folder / "leaf.key")
    target = Api()
    server = await asyncio.start_server(target.handle, "127.0.0.1", 0, ssl=context)
    target.port = server.sockets[0].getsockname()[1]
    # This local service stands in for https://api.github.com.
    monkeypatch.setattr(vault_services, "GITHUB_API_HOST", "127.0.0.1")
    monkeypatch.setattr(vault_services, "GITHUB_API_PORT", target.port)
    try:
        yield target
    finally:
        server.close()
        await server.wait_closed()


@pytest.fixture
def bypass(monkeypatch):
    mode = AsyncMock(return_value=True)
    monkeypatch.setattr(vault_access, "session_bypasses_confirmation", mode)
    return mode


async def grants(broker, api):  # noqa: F811
    cloudflare = await broker.create("chat", spec(api, writable=True, prefix="/client/v4", label="Cloudflare"))
    github = await broker.create("chat", spec(api, writable=True, prefix="", label="GitHub"))
    return cloudflare["id"], github["id"]


async def capture_token(broker, cloudflare):  # noqa: F811
    return await broker.run("chat", cloudflare, "http", {
        "method": "POST", "path": "/accounts/acct/tokens", "body": '{"name": "deploy"}',
        "capture": "result.value"})


def copy_args(github, captured, **overrides):
    return {"grant_id": github, "secret_id": captured, "repository": "example-org/website",
            "environment": "production", "name": "CLOUDFLARE_API_TOKEN", **overrides}


def opened(api):
    """What GitHub stored: the sealed box opened with the repository's private key."""
    put = next(r for r in api.requests if r["method"] == "PUT")
    sent = json.loads(put["body"])
    assert sent["key_id"] == "key-1" and NEW_TOKEN not in put["body"]
    return public.SealedBox(REPO_KEY).decrypt(base64.b64decode(sent["encrypted_value"])).decode()


async def test_a_created_token_reaches_github_without_the_agent_seeing_it(broker, api, trust, bypass):  # noqa: F811
    cloudflare, github = await grants(broker, api)
    seen = []
    created = await capture_token(broker, cloudflare)
    seen.append(created)
    assert created["status"] == 200 and created["capture"]["captured"] is True
    assert created["headers"] == {} and json.loads(created["body"])["shape"] == {
        "[key 1]": True, "result": {"[key 1]": "[withheld]", "[key 2]": "[withheld]", "value": "[captured]"}}
    secret_id = created["capture"]["secret_id"]
    listed = await broker.resources("chat")
    seen.append(listed)
    assert [c["secret_id"] for c in listed["captured_secrets"]] == [secret_id]

    copying = asyncio.create_task(broker.run("chat", github, "copy_secret", copy_args(github, secret_id)))
    action = await pending(broker)
    # Bypass does not skip this card, and switching to Bypass does not release it.
    await broker.resolve_mode_actions("chat")
    assert not action.decision.done() and action.always
    card = action.public()
    seen += [card, await broker.snapshot("chat")]
    assert card["operation"] == "copy_secret"
    assert card["arguments"] == {
        "secret": "Cloudflare: POST /accounts/acct/tokens → result.value", "secret_id": secret_id,
        "repository": "example-org/website", "environment": "production", "name": "CLOUDFLARE_API_TOKEN"}
    assert not any(r["method"] == "PUT" for r in api.requests)
    await broker.decide("chat", action.id, True)
    result = await copying
    seen.append(result)
    assert result == {"written": True, "repository": "example-org/website", "environment": "production",
                      "name": "CLOUDFLARE_API_TOKEN", "status": 201}
    assert opened(api) == NEW_TOKEN
    assert NEW_TOKEN not in json.dumps(seen)
    assert base64.b64encode(NEW_TOKEN.encode()).decode() not in json.dumps(seen)


async def test_a_denied_copy_writes_nothing(broker, api, trust, bypass):  # noqa: F811
    cloudflare, github = await grants(broker, api)
    secret_id = (await capture_token(broker, cloudflare))["capture"]["secret_id"]
    copying = asyncio.create_task(broker.run("chat", github, "copy_secret", copy_args(github, secret_id)))
    action = await pending(broker)
    await broker.decide("chat", action.id, False)
    with pytest.raises(AccessDenied, match="denied"):
        await copying
    assert [r["method"] for r in api.requests] == ["POST"]


async def test_away_refuses_a_copy_without_waiting(broker, api, trust, bypass, monkeypatch):  # noqa: F811
    from app.core import away_mode

    cloudflare, github = await grants(broker, api)
    secret_id = (await capture_token(broker, cloudflare))["capture"]["secret_id"]
    monkeypatch.setattr(away_mode, "answers_questions_async", AsyncMock(return_value=True))
    monkeypatch.setattr(away_mode, "record_decision", AsyncMock())
    with pytest.raises(AccessDenied, match=away_mode.DEFERRED_MESSAGE[:20]):
        await asyncio.wait_for(broker.run("chat", github, "copy_secret", copy_args(github, secret_id)), 5)
    assert not any(r["target"] == KEY_PATH for r in api.requests)


async def test_a_wrong_field_withholds_the_body_and_shows_only_its_shape(broker, api, trust, bypass):  # noqa: F811
    cloudflare, _ = await grants(broker, api)
    result = await broker.run("chat", cloudflare, "http", {
        "method": "POST", "path": "/accounts/acct/tokens", "body": "{}", "capture": "result.token"})
    assert NEW_TOKEN not in json.dumps(result)
    assert result["capture"]["captured"] is False and "secret_id" not in result["capture"]
    shown = json.loads(result["body"])
    assert shown["shape"] == {"[key 1]": True, "result": {"[key 1]": "[withheld]", "[key 2]": "[withheld]",
                                                          "[key 3]": "[withheld]"}}
    assert not broker.captures


async def test_an_unsuccessful_response_is_returned_and_captures_nothing(broker, api, trust, bypass):  # noqa: F811
    cloudflare, _ = await grants(broker, api)
    result = await broker.run("chat", cloudflare, "http", {
        "method": "POST", "path": "/accounts/other/tokens", "body": "{}", "capture": "result.value"})
    assert result["status"] == 404 and json.loads(result["body"]) == {"message": "Not Found"}
    assert result["capture"] == {"field": "result.value", "captured": False,
                                 "reason": "the service answered 404; nothing was captured"}
    assert not broker.captures


async def test_a_captured_value_stays_redacted_when_a_service_echoes_it(broker, api, trust, bypass):  # noqa: F811
    cloudflare, _ = await grants(broker, api)
    await capture_token(broker, cloudflare)
    echoed = await broker.run("chat", cloudflare, "http", {"method": "GET", "path": "/echo"})
    assert NEW_TOKEN not in json.dumps(echoed) and "[redacted]" in echoed["body"]


async def test_a_capture_stays_with_its_chat_and_expires(broker, api, trust, bypass, monkeypatch):  # noqa: F811
    cloudflare, github = await grants(broker, api)
    secret_id = (await capture_token(broker, cloudflare))["capture"]["secret_id"]
    with pytest.raises(AccessDenied, match="No such captured secret"):
        await broker._capture("other-chat", secret_id)
    broker.captures[secret_id].expires_at = 0
    with pytest.raises(AccessDenied, match="expired"):
        await broker.run("chat", github, "copy_secret", copy_args(github, secret_id))
    assert secret_id not in broker.captures and not broker.actions
    assert not any(r["target"] == KEY_PATH for r in api.requests)


@pytest.mark.parametrize("overrides, message", [
    ({"repository": "example-org"}, "owner/name"),
    ({"repository": "example-org/../x"}, "owner/name"),
    ({"environment": "prod/../x"}, "environment"),
    ({"name": "GITHUB_TOKEN"}, "GITHUB_"),
    ({"name": "has-dash"}, "letters, digits"),
    ({"secret_id": "sec_fabricated"}, "No such captured secret"),
])
async def test_a_bad_target_is_refused_before_anything_is_asked_or_sent(
        broker, api, trust, bypass, overrides, message):  # noqa: F811
    cloudflare, github = await grants(broker, api)
    secret_id = (await capture_token(broker, cloudflare))["capture"]["secret_id"]
    with pytest.raises(AccessDenied, match=message):
        await broker.run("chat", github, "copy_secret", copy_args(github, secret_id, **overrides))
    assert not broker.actions and [r["method"] for r in api.requests] == ["POST"]


async def test_a_read_only_github_grant_cannot_write_a_secret(broker, api, trust, bypass):  # noqa: F811
    cloudflare, _ = await grants(broker, api)
    read_only = (await broker.create("chat", spec(api, prefix="", label="GitHub read")))["id"]
    secret_id = (await capture_token(broker, cloudflare))["capture"]["secret_id"]
    with pytest.raises(AccessDenied, match="read-only"):
        await broker.run("chat", read_only, "copy_secret", copy_args(read_only, secret_id))
    assert not broker.actions


async def test_a_repository_secret_uses_the_actions_paths(broker, api, trust, bypass):  # noqa: F811
    key_path, secret_path, target = vault_services.github_secret_paths("example-org/website", "", "DEPLOY_KEY")
    assert key_path == "/repos/example-org/website/actions/secrets/public-key"
    assert secret_path == "/repos/example-org/website/actions/secrets/DEPLOY_KEY"
    assert target == {"repository": "example-org/website", "environment": "", "name": "DEPLOY_KEY"}
    spaced = vault_services.github_secret_paths("example-org/website", "staging east", "DEPLOY_KEY")[0]
    assert spaced == "/repos/example-org/website/environments/staging%20east/secrets/public-key"


async def test_only_the_github_api_receives_a_copy(broker, api, trust, bypass):  # noqa: F811
    # A service answering the public-key request with its own key could open
    # the sealed value, while the card says GitHub.
    cloudflare, _ = await grants(broker, api)
    under_a_path = (await broker.create("chat", spec(api, writable=True, prefix="/api/v3", label="Proxy")))["id"]
    secret_id = (await capture_token(broker, cloudflare))["capture"]["secret_id"]
    for grant in (cloudflare, under_a_path):
        with pytest.raises(AccessDenied, match="api.github.com"):
            await broker.run("chat", grant, "copy_secret", copy_args(grant, secret_id))
    assert not broker.actions and [r["method"] for r in api.requests] == ["POST"]


def test_the_github_api_is_its_public_address_trusted_by_the_system():
    origin = vault_services.parse_origin("https://api.github.com")
    assert vault_services.is_github_api(origin, "") is True
    assert vault_services.is_github_api(origin, "SHA256:" + "A" * 43) is False
    assert vault_services.is_github_api(vault_services.parse_origin("https://api.github.com/x"), "") is False
    assert vault_services.is_github_api(vault_services.parse_origin("https://github.example.com"), "") is False


async def test_a_capture_response_never_shows_a_value_even_for_a_wrong_text_field(
        broker, api, trust, bypass):  # noqa: F811
    api.routes[("POST", "/client/v4/accounts/acct/tokens")] = ("200 OK", {"result": {
        "id": "tok_0123456789abcdef", "value": NEW_TOKEN, "expires_on": 1767225600}})
    cloudflare, _ = await grants(broker, api)
    result = await broker.run("chat", cloudflare, "http", {
        "method": "POST", "path": "/accounts/acct/tokens", "body": "{}", "capture": "result.id"})
    assert result["capture"]["captured"] is True and NEW_TOKEN not in json.dumps(result)
    assert result["headers"] == {} and json.loads(result["body"])["shape"] == {
        "result": {"id": "[captured]", "[key 2]": "[withheld]", "[key 3]": "[number]"}}


@pytest.mark.parametrize("value, reason", [
    ("test-only-line-one\ntest-only-line-two", "one line"),
    ("short", "one line"),
    (None, "one line"),
])
async def test_values_a_redactor_cannot_follow_are_not_captured(
        broker, api, trust, bypass, value, reason):  # noqa: F811
    api.routes[("POST", "/client/v4/accounts/acct/tokens")] = ("200 OK", {"result": {"value": value}})
    cloudflare, _ = await grants(broker, api)
    result = await capture_token(broker, cloudflare)
    assert result["capture"]["captured"] is False and reason in result["capture"]["reason"]
    assert "line-one" not in json.dumps(result) and not broker.captures


async def test_the_grants_own_credential_is_not_captured(broker, api, trust, bypass):  # noqa: F811
    from tests.core.test_vault_services import SECRET

    api.routes[("POST", "/client/v4/accounts/acct/tokens")] = ("200 OK", {"result": {"value": SECRET}})
    cloudflare, _ = await grants(broker, api)
    result = await capture_token(broker, cloudflare)
    assert result["capture"]["captured"] is False and "signs in with" in result["capture"]["reason"]
    assert SECRET not in json.dumps(result) and not broker.captures


async def test_a_batch_redacts_an_echo_of_a_value_another_item_captured(
        broker, api, trust, bypass, monkeypatch):  # noqa: F811
    from app.core.provider.sdk_tool_records import recorded_sdk_server_tools
    from app.mcp import vault_sdk as sdk

    monkeypatch.setattr(sdk, "broker", broker)
    cloudflare, _ = await grants(broker, api)
    tools = {t.name: t for t in recorded_sdk_server_tools(sdk.create_vault_server("chat"))}
    response = await tools["ChatHttpRequest"].handler({"requests": [
        {"grant_id": cloudflare, "method": "GET", "path": "/echo"},
        {"grant_id": cloudflare, "method": "POST", "path": "/accounts/acct/tokens", "body": "{}",
         "capture": "result.value"}]})
    text = response["content"][0]["text"]
    results = json.loads(text)["results"]
    assert results[1]["result"]["capture"]["captured"] is True
    assert NEW_TOKEN not in text and "[redacted]" in results[0]["result"]["body"]


def test_unpadded_and_url_safe_base64_echoes_are_redacted():
    value = "test-only/value+with?=padding"
    needles = vault_services.held_needles((value,))
    for encoded in (base64.b64encode(value.encode()), base64.urlsafe_b64encode(value.encode())):
        for form in (encoded, encoded.rstrip(b"=")):
            assert vault_services.redact(b"x " + form, needles) == b"x [redacted]"


async def test_a_copy_card_left_open_when_the_chat_goes_away_is_refused(
        broker, api, trust, bypass, monkeypatch):  # noqa: F811
    from app.core import away_mode

    cloudflare, github = await grants(broker, api)
    secret_id = (await capture_token(broker, cloudflare))["capture"]["secret_id"]
    copying = asyncio.create_task(broker.run("chat", github, "copy_secret", copy_args(github, secret_id)))
    action = await pending(broker)
    monkeypatch.setattr(away_mode, "answers_questions_async", AsyncMock(return_value=True))
    monkeypatch.setattr(away_mode, "record_decision", AsyncMock())
    with pytest.raises(AccessDenied, match=away_mode.DEFERRED_MESSAGE[:20]):
        await broker.decide("chat", action.id, True)
    with pytest.raises(AccessDenied, match=away_mode.DEFERRED_MESSAGE[:20]):
        await copying
    assert [r["method"] for r in api.requests] == ["POST"]


async def test_concurrent_captures_cannot_pass_the_limit(broker, api, trust, bypass):  # noqa: F811
    cloudflare, _ = await grants(broker, api)
    owner = await vault_access.session_owner("chat")
    for index in range(vault_access.CAPTURE_LIMIT - 1):
        broker._hold("chat", owner, f"test-only-held-{index:04d}", "earlier")
    outcomes = await asyncio.gather(capture_token(broker, cloudflare), capture_token(broker, cloudflare),
                                    return_exceptions=True)
    assert sum(isinstance(o, AccessDenied) for o in outcomes) == 1
    assert len(broker._held("chat")) == vault_access.CAPTURE_LIMIT and not broker.capturing
    assert [r["method"] for r in api.requests] == ["POST"]  # only one credential was minted


async def test_a_chat_changing_hands_ends_its_captures(broker, api, trust, bypass, monkeypatch):  # noqa: F811
    from app.network import handoff

    monkeypatch.setattr(vault_access, "broker", broker)
    from app.db import base

    committed = []
    monkeypatch.setattr(base, "after_commit", lambda conn, callback: committed.append(callback))
    cloudflare, github = await grants(broker, api)
    secret_id = (await capture_token(broker, cloudflare))["capture"]["secret_id"]
    handoff._forget_vault_captures(object(), "chat")  # called where a move flips ownership, both ways
    # A move that rolls back keeps them: nothing happens before the commit.
    assert broker._held("chat") and (await broker.resources("chat"))["captured_secrets"]
    for callback in committed:
        callback()
    assert "captured_secrets" not in await broker.resources("chat")
    with pytest.raises(AccessDenied, match="expired"):
        await broker.run("chat", github, "copy_secret", copy_args(github, secret_id))
    assert not broker.actions and not broker._held("chat")


async def test_a_credential_used_as_a_key_is_not_shown(broker, api, trust, bypass):  # noqa: F811
    api.routes[("POST", "/client/v4/accounts/acct/tokens")] = ("200 OK", {"result": {
        "id": "tok_0123456789abcdef", "tokens": {NEW_TOKEN: {"active": True}, "AbCdEfGhIjKlMnOpQr": 1}}})
    cloudflare, _ = await grants(broker, api)
    result = await broker.run("chat", cloudflare, "http", {
        "method": "POST", "path": "/accounts/acct/tokens", "body": "{}", "capture": "result.id"})
    assert NEW_TOKEN not in json.dumps(result) and "AbCdEf" not in json.dumps(result)
    assert json.loads(result["body"])["shape"]["result"] == {
        "id": "[captured]", "[key 2]": {"[key 1]": {"[key 1]": True}, "[key 2]": "[number]"}}


async def test_a_shape_names_only_the_capture_fields_keys(broker, api, trust, bypass):  # noqa: F811
    # A second credential may be a key that reads like words; only the path the agent gave is named.
    api.routes[("POST", "/client/v4/accounts/acct/tokens")] = ("200 OK", {"result": {
        "api-key": NEW_TOKEN, "recovery": {"lakes_amber_elm_orbit_spire": True}}, "items": [{"x": 1}]})
    cloudflare, _ = await grants(broker, api)
    result = await broker.run("chat", cloudflare, "http", {
        "method": "POST", "path": "/accounts/acct/tokens", "body": "{}", "capture": "result.api-key"})
    assert result["capture"]["captured"] is True and "lakes" not in json.dumps(result)
    assert json.loads(result["body"])["shape"] == {
        "result": {"api-key": "[captured]", "[key 2]": {"[key 1]": True}}, "[key 2]": [{"[key 1]": "[number]"}]}


async def test_an_escaped_echo_of_the_grants_credential_is_not_captured(broker, api, trust, bypass):  # noqa: F811
    from urllib.parse import quote

    from tests.core.test_vault_services import SECRET

    api.routes[("POST", "/client/v4/accounts/acct/tokens")] = ("200 OK", {"result": {
        "value": quote(SECRET, safe="")}})
    cloudflare, _ = await grants(broker, api)
    result = await capture_token(broker, cloudflare)
    assert result["capture"]["captured"] is False and "signs in with" in result["capture"]["reason"]
    assert not broker.captures


async def test_a_late_redaction_covers_a_value_cut_at_the_limit(broker):  # noqa: F811
    value = "test-only-held-value-0123456789"
    broker._hold("chat", await vault_access.session_owner("chat"), value, "earlier")
    again = broker.redact_held("chat", {"status": 200, "headers": {}, "body": "echo " + value[:-1],
                                        "truncated": True})
    assert value[:-4] not in again["body"]


@pytest.mark.parametrize("field", ["data.0.token", "data.00.token"])
async def test_a_capture_through_a_list_is_marked(broker, api, trust, bypass, field):  # noqa: F811
    api.routes[("POST", "/client/v4/accounts/acct/tokens")] = ("200 OK", {"data": [{"token": NEW_TOKEN}]})
    cloudflare, _ = await grants(broker, api)
    result = await broker.run("chat", cloudflare, "http", {
        "method": "POST", "path": "/accounts/acct/tokens", "body": "{}", "capture": field})
    assert result["capture"]["captured"] is True
    assert json.loads(result["body"])["shape"] == {"data": [{"token": "[captured]"}]}


@pytest.mark.parametrize("field", ["", "a..b", ".a", "a b", "a/b", "x" * 65, ".".join("a" * 9)])
def test_capture_fields_are_plain_dot_paths(field):
    with pytest.raises(AccessDenied, match="dot path"):
        vault_services.capture_field(field)


async def test_the_tools_expose_capture_and_copy(broker, api, trust, bypass, monkeypatch):  # noqa: F811
    from app.core.provider.sdk_tool_records import recorded_sdk_server_tools
    from app.mcp import vault_sdk as sdk

    monkeypatch.setattr(sdk, "broker", broker)
    cloudflare, github = await grants(broker, api)
    tools = {t.name: t for t in recorded_sdk_server_tools(sdk.create_vault_server("chat"))}
    assert "mcp__chat-access__ChatCopySecret" in sdk.TOOL_NAMES
    response = await tools["ChatHttpRequest"].handler({"requests": [{
        "grant_id": cloudflare, "method": "POST", "path": "/accounts/acct/tokens", "body": "{}",
        "capture": "result.value"}]})
    text = response["content"][0]["text"]
    assert NEW_TOKEN not in text
    secret_id = json.loads(text)["results"][0]["result"]["capture"]["secret_id"]
    copying = asyncio.create_task(tools["ChatCopySecret"].handler(copy_args(github, secret_id)))
    await broker.decide("chat", (await pending(broker)).id, True)
    copied = await copying
    assert json.loads(copied["content"][0]["text"])["written"] is True and NEW_TOKEN not in json.dumps(copied)
    assert opened(api) == NEW_TOKEN
