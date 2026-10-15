"""Real RSA signatures + local ASGI Bot Framework/JWKS server; no Azure access."""
import asyncio
import base64
import json
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock
from urllib.parse import parse_qs

import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.core import msteams_auth
from app.core.connector_service import receive_teams_activity, teams_runtime_config
from app.core.connectors.msteams import MSTeamsConnector, validate_service_url
from app.core.connectors.base import OutgoingMessage

CLIENT = httpx.AsyncClient
SERVICE = "https://smba.trafficmanager.net/teams/"
APP_ID = "test-bot-app"
TENANT = "00000000-0000-0000-0000-000000000001"


def b64(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


@pytest.fixture(scope="module")
def signing_keys():
    return [rsa.generate_private_key(public_exponent=65537, key_size=2048) for _ in range(2)]


def jwk(private, kid):
    numbers = private.public_key().public_numbers()
    return {"kty": "RSA", "use": "sig", "alg": "RS256", "kid": kid,
            "n": b64(numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")),
            "e": b64(numbers.e.to_bytes((numbers.e.bit_length() + 7) // 8, "big")),
            "endorsements": ["msteams"]}


@pytest.fixture
def activity():
    return {"type": "message", "id": "activity-1", "channelId": "msteams",
            "serviceUrl": SERVICE, "text": "<at>Assistant</at> hello",
            "conversation": {"id": "chat/with:reserved", "tenantId": TENANT},
            "channelData": {"tenant": {"id": TENANT}},
            "from": {"id": "channel-user", "aadObjectId": "allowed-user", "name": "Test User"},
            "recipient": {"id": "bot"}}


@pytest.fixture
def sign(signing_keys):
    def create(changes=None, kid="key-1", key=0, algorithm="RS256"):
        now = time.time()
        claims = {"iss": msteams_auth.ISSUER, "aud": APP_ID,
                  "nbf": now - 30, "exp": now + 300, "serviceurl": SERVICE}
        claims.update(changes or {})
        parts = [b64(json.dumps({"alg": algorithm, "kid": kid}).encode()),
                 b64(json.dumps(claims).encode())]
        signature = signing_keys[key].sign(".".join(parts).encode(), padding.PKCS1v15(), hashes.SHA256())
        return "Bearer " + ".".join([*parts, b64(signature)])
    return create


@pytest.fixture
def cloud(monkeypatch, signing_keys):
    """In-process HTTP server serves metadata, rotating JWKS, OAuth and replies."""
    app = FastAPI()
    state = {"keys": [jwk(signing_keys[0], "key-1")], "gets": [], "tokens": [],
             "replies": [], "statuses": [200], "metadata": {
                 "issuer": msteams_auth.ISSUER,
                 "jwks_uri": "https://login.botframework.com/v1/.well-known/keys"}}

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
    async def handle(path: str, request: Request):
        if request.method == "GET":
            state["gets"].append(str(request.url))
            if path.endswith("openidconfiguration"):
                return state["metadata"]
            if path.endswith("/keys"):
                return {"keys": state["keys"]}
        if path.endswith("/oauth2/v2.0/token"):
            state["tokens"].append(parse_qs((await request.body()).decode()))
            return {"access_token": "outbound-token", "expires_in": 3600}
        raw_url = f"{request.url.scheme}://{request.url.netloc}" + request.scope["raw_path"].decode()
        state["replies"].append((raw_url, request.headers, await request.json()))
        code = state["statuses"].pop(0) if len(state["statuses"]) > 1 else state["statuses"][0]
        return JSONResponse({"id": "reply-1"}, status_code=code, headers={"Retry-After": "0"})

    def client(*args, **kwargs):
        return CLIENT(*args, transport=httpx.ASGITransport(app=app), **kwargs)
    monkeypatch.setattr(httpx, "AsyncClient", client)
    validator = msteams_auth.BotFrameworkAuth()
    monkeypatch.setattr(msteams_auth, "bot_framework_auth", validator)
    state["validator"] = validator
    return state


def connector(tmp_path, **config):
    return MSTeamsConnector("teams-id", "msteams", {
        "app_id": APP_ID, "app_password": "test-secret", "tenant_id": TENANT,
        "allowed_user_ids": ["allowed-user"], "_state_path": str(tmp_path / "refs.json"),
        **config,
    }, AsyncMock(), AsyncMock())


@pytest.mark.asyncio
async def test_valid_cached_auth_and_clock_skew(cloud, sign, activity):
    auth = cloud["validator"]
    await auth.validate(sign(), activity, APP_ID, TENANT)
    await auth.validate(sign({"exp": time.time() - 10}), activity, APP_ID, TENANT)
    assert len(cloud["gets"]) == 2
    auth._expires = 0
    await auth.validate(sign(), activity, APP_ID, TENANT)
    assert len(cloud["gets"]) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("claims", [
    {"iss": "https://attacker.example"}, {"aud": "other-bot"},
    {"exp": 0}, {"exp": None}, {"exp": float("nan")}, {"nbf": 99999999999},
    {"serviceurl": "https://smba.trafficmanager.net/other/"}, {"channelid": "webchat"},
])
async def test_invalid_claims_never_dispatch_or_persist(cloud, sign, activity, tmp_path, claims):
    conn = connector(tmp_path)
    with pytest.raises(msteams_auth.InvalidBotToken):
        await receive_teams_activity(conn, sign(claims), activity)
    assert not conn._conversation_refs
    assert not conn._state_path.exists()
    conn._on_incoming_message.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["missing", "malformed", "signature", "algorithm", "channel", "endorsement", "tenant", "service"])
async def test_reject_invalid_boundaries(cloud, sign, activity, case):
    token = sign()
    if case == "missing": token = ""
    if case == "malformed": token = "Bearer not.a.jwt"
    if case == "signature": token = sign(key=1)
    if case == "algorithm": token = sign(algorithm="HS256")
    if case == "channel": activity["channelId"] = "emulator"
    if case == "endorsement": cloud["keys"][0]["endorsements"] = ["other"]
    if case == "tenant": activity["channelData"]["tenant"]["id"] = "other"
    if case == "service": activity["serviceUrl"] = "https://attacker.example/"
    with pytest.raises(msteams_auth.InvalidBotToken):
        await cloud["validator"].validate(token, activity, APP_ID, TENANT)


@pytest.mark.asyncio
async def test_unknown_kid_refresh_and_bounded_misses(cloud, sign, activity, signing_keys):
    auth = cloud["validator"]
    await auth.validate(sign(), activity, APP_ID)
    cloud["keys"] = [jwk(signing_keys[1], "rotated")]
    await auth.validate(sign(kid="rotated", key=1), activity, APP_ID)
    assert len(cloud["gets"]) == 4
    for _ in range(3):
        with pytest.raises(msteams_auth.InvalidBotToken):
            await auth.validate(sign(kid="unknown"), activity, APP_ID)
    assert len(cloud["gets"]) == 4


@pytest.mark.asyncio
async def test_metadata_cannot_redirect_trust(cloud, sign, activity):
    cloud["metadata"]["jwks_uri"] = "https://attacker.example/keys"
    with pytest.raises(msteams_auth.InvalidBotToken):
        await cloud["validator"].validate(sign(), activity, APP_ID)
    assert len(cloud["gets"]) == 1


@pytest.mark.asyncio
async def test_inbound_normalization_persist_restart_reply_retry(cloud, sign, activity, tmp_path):
    conn = connector(tmp_path)
    await receive_teams_activity(conn, sign(), activity)
    msg = conn._on_incoming_message.await_args.args[0]
    assert (msg.platform_chat_id, msg.platform_user_id, msg.text) == (activity["conversation"]["id"], "allowed-user", "hello")
    assert msg.metadata["tenant_id"] == TENANT
    restarted = connector(tmp_path)
    cloud["statuses"] = [429, 503, 200]
    assert await restarted.send_message(OutgoingMessage(platform_chat_id=msg.platform_chat_id, text="reply"))
    assert len(cloud["tokens"]) == 1
    assert cloud["tokens"][0] == {"grant_type": ["client_credentials"], "client_id": [APP_ID],
                                 "client_secret": ["test-secret"], "scope": ["https://api.botframework.com/.default"]}
    assert len(cloud["replies"]) == 3
    url, headers, body = cloud["replies"][-1]
    assert url == SERVICE + "v3/conversations/chat%2Fwith%3Areserved/activities/activity-1"
    assert body["replyToId"] == "activity-1"
    assert headers["authorization"] == "Bearer outbound-token"
    assert body["text"] == "reply" and body["conversation"] == activity["conversation"]
    assert body["from"] == activity["recipient"]
    assert not connector(tmp_path, app_id="different-bot")._conversation_refs
    assert not connector(tmp_path, tenant_id="different-tenant")._conversation_refs


@pytest.mark.asyncio
async def test_token_cache_refresh_and_failure_reporting(cloud, sign, activity, tmp_path):
    conn = connector(tmp_path)
    await receive_teams_activity(conn, sign(), activity)
    message = OutgoingMessage(platform_chat_id=activity["conversation"]["id"], text="reply")
    assert await conn.send_message(message)
    assert await conn.send_message(message)
    assert len(cloud["tokens"]) == 1
    conn._token_expires_at = 0
    assert await conn.send_message(message)
    assert len(cloud["tokens"]) == 2
    cloud["statuses"] = [401, 200]
    assert await conn.send_message(message)
    assert len(cloud["tokens"]) == 3
    cloud["statuses"] = [503]
    start = len(cloud["replies"])
    assert not await conn.send_message(message)
    assert len(cloud["replies"]) - start == 4
    cloud["statuses"] = [403]
    start = len(cloud["replies"])
    assert not await conn.send_message(message)
    assert len(cloud["replies"]) - start == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["message", "invoke"])
async def test_disallowed_sender_cannot_dispatch_or_save(cloud, sign, activity, tmp_path, kind):
    conn = connector(tmp_path)
    activity["type"] = kind
    activity["from"]["aadObjectId"] = "stranger"
    activity["value"] = {"action": "perm:req:approve"}
    await receive_teams_activity(conn, sign(), activity)
    # Defense in depth at the internal RPC, including invokes.
    await conn.handle_webhook_activity(activity)
    assert not conn._state_path.exists()
    conn._on_incoming_message.assert_not_called()
    conn._on_permission_response.assert_not_called()


@pytest.mark.asyncio
async def test_card_submit_message_is_not_an_empty_agent_message(cloud, sign, activity, tmp_path):
    conn = connector(tmp_path)
    conn._permission_sessions["req"] = "session"
    activity["value"] = {"action": "perm:req:approve"}
    await receive_teams_activity(conn, sign(), activity)
    conn._on_permission_response.assert_awaited_once_with("req", "session", True)
    conn._on_incoming_message.assert_not_called()


@pytest.mark.parametrize("url", ["http://smba.trafficmanager.net/teams", "https://127.0.0.1/", "https://attacker.example/",
    "https://smba.trafficmanager.net@attacker.example/", "https://smba.trafficmanager.net:444/", "https://smba.trafficmanager.net/?redirect=x"])
def test_service_url_fail_closed(url):
    with pytest.raises(ValueError): validate_service_url(url)


def test_runtime_state_path_is_host_controlled(tmp_path, monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    config = teams_runtime_config("../../anything", {"_state_path": "/untrusted", "app_id": APP_ID})
    assert Path(config["_state_path"]).parent == tmp_path / "connectors" / "msteams"
    assert config["app_id"] == APP_ID


@pytest.mark.asyncio
async def test_public_route_checks_enabled_type_and_auth_before_rpc(cloud, sign, activity, tmp_path, monkeypatch):
    from app.api.connectors import router
    from app.core.connectors.manager import connector_manager
    from app.db import database
    app = FastAPI()
    app.include_router(router)
    conn = connector(tmp_path)
    conn.handle_webhook_activity = AsyncMock()
    row = {"id": "teams-id", "type": "msteams", "is_active": True}
    monkeypatch.setattr(database, "get_connector", lambda _: row)
    monkeypatch.setattr(connector_manager, "get_running_connectors", lambda kind: [("teams-id", conn)])
    monkeypatch.setenv("AI_SHUFFLE_EXPERIMENTAL_MSTEAMS", "1")
    url = "/api/v1/connectors/msteams/teams-id/messages"
    async with CLIENT(transport=httpx.ASGITransport(app=app), base_url="https://worker.example") as client:
        response = await client.post(url, json=activity)
        assert response.status_code == 401
        conn.handle_webhook_activity.assert_not_called()
        response = await client.post(url, json=activity, headers={"Authorization": sign()})
        assert response.status_code == 200
        conn.handle_webhook_activity.assert_awaited_once_with(activity)
        auth = {"Authorization": sign()}
        assert (await client.post(url, content="not-json", headers=auth)).status_code == 400
        assert (await client.post(url, content=b"x" * 256001, headers=auth)).status_code == 413
        invalid = {**activity, "value": {"action": 123}}
        assert (await client.post(url, json=invalid, headers=auth)).status_code == 400
        conn.handle_webhook_activity.assert_awaited_once()
        for field, value in (("is_active", False), ("type", "webhook")):
            original = row[field]
            row[field] = value
            assert (await client.post(url, json=activity, headers={"Authorization": sign()})).status_code == 404
            row[field] = original
        monkeypatch.delenv("AI_SHUFFLE_EXPERIMENTAL_MSTEAMS")
        assert (await client.post(url, json=activity, headers={"Authorization": sign()})).status_code == 404


@pytest.mark.asyncio
async def test_authenticated_ingress_through_real_package_rpc(cloud, sign, activity, tmp_path):
    from app.core import connector_packages
    from app.core.connectors import package_proxy
    source = connector_packages._SOURCE_DIR
    spec = connector_packages.CATALOG["packages"]["msteams"]
    cls = package_proxy.proxy_class("msteams", source, spec)
    config = connector(tmp_path).config
    incoming = AsyncMock()
    proxy = cls("teams-id", "msteams", config, incoming, AsyncMock())
    # Initialize the real isolated SDK/worker protocol without its networked
    # connect() handshake. OAuth/reply HTTP is covered by the fake cloud tests.
    proxy._process = await asyncio.create_subprocess_exec(
        sys.executable, "-I", str(source / "package_worker.py"), str(source),
        "msteams", "MSTeamsConnector", stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        env=package_proxy.worker_environment(), limit=2_000_000,
    )
    proxy._reader = asyncio.create_task(proxy._read())
    try:
        await proxy._call("init", connector_id="teams-id", connector_type="msteams", config=config)
        await receive_teams_activity(proxy, sign(), activity)
        incoming.assert_awaited_once()
        assert incoming.await_args.args[0].text == "hello"
        state = json.loads(Path(config["_state_path"]).read_text())
        assert state["references"][activity["conversation"]["id"]]["service_url"] == SERVICE
    finally:
        await proxy.disconnect()
    assert proxy._process.returncode == 0
