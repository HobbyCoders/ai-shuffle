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
async def api(pki):  # noqa: F811
    _, folder = pki
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.load_cert_chain(folder / "leaf.pem", folder / "leaf.key")
    target = Api()
    server = await asyncio.start_server(target.handle, "127.0.0.1", 0, ssl=context)
    target.port = server.sockets[0].getsockname()[1]
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
    body = json.loads(created["body"])
    assert body["result"]["value"] == "[redacted]" and body["result"]["id"] == "tok_1"
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
    assert shown["shape"] == {"success": True, "result": {"id": "[withheld]", "name": "[withheld]",
                                                          "value": "[withheld]"}}
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
