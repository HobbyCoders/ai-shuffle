"""Routes for the AI Shuffle browser extension (app/api/browser_extension.py)."""

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.api import browser_extension as api
from app.api.auth import require_auth
from app.core import browser_extension as ext

DEVICE = "6f1c2b9e-0d4a-5b8e-9a51-2f3c4d5e6f70"


OWNER_ACCOUNT = "acct-owner"
REVOKED = set()
TURNS = {}


@pytest.fixture
def client(monkeypatch):
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[require_auth] = lambda: "token"

    # The caller a request acts as: x-test-as "api" (an API key) or
    # "paired:<account>" (a controller signed in with that Shuffle account).
    @app.middleware("http")
    async def caller(request, call_next):
        who = request.headers.get("x-test-as", "")
        if who == "api":
            request.state.api_user = {"id": "api-user-1"}
        elif who.startswith("paired:"):
            request.state.paired_client = {"id": "controller-of-" + who[7:]}
        elif who == "extension":  # the extension's own side panel
            request.state.paired_client = {"id": "pc-extension"}
        return await call_next(request)

    def device_for_key(key):
        if key == "aim_extension" and "pc-extension" not in REVOKED:
            return {"device_id": DEVICE, "paired_client_id": "pc-extension",
                    "scopes": ["controller", "web:session", "browser:extension"]}
        if key == "aim_phone":
            raise HTTPException(403, "Only the AI Shuffle browser extension can connect here")
        raise HTTPException(401, "This browser is no longer authorized; sign in again")

    async def session(request, session_id):
        return {"id": session_id, "turn_count": TURNS.get(session_id, 0)}

    from app.core import mobile_tool_identity, preview_access
    monkeypatch.setattr(api, "_device_for_key", device_for_key)
    monkeypatch.setattr(api, "_device_account", lambda device: OWNER_ACCOUNT)
    monkeypatch.setattr(api, "require_session", session)
    monkeypatch.setattr(preview_access, "grant", lambda request: None)
    monkeypatch.setattr(mobile_tool_identity.mobile_hub, "ownership",
                        lambda device_id=None, *, paired_client_id=None: (
                            {"account_id": paired_client_id[len("controller-of-"):]}
                            if paired_client_id and paired_client_id.startswith("controller-of-") else None))
    from app.network import control
    monkeypatch.setattr(control, "owner_of", lambda session: None)
    yield TestClient(app)
    REVOKED.clear()
    TURNS.clear()
    ext._browsers.clear()
    ext._targets.clear()
    ext._relay_tokens.clear()


def hello(key, browser_id="claimed-by-extension"):
    return {"event": "hello", "key": key, "browser_id": browser_id, "name": "Chrome on Windows",
            "product": "Chrome", "version": "131.0"}


def test_extension_connects_as_its_enrolled_device_not_the_id_it_claims(client):
    with client.websocket_connect("/api/v1/browser-extension/connect") as socket:
        socket.send_json(hello("aim_extension", browser_id="someone-elses-browser"))
        assert socket.receive_json() == {"event": "welcome", "browser_id": DEVICE}
        assert [b["id"] for b in ext.browsers()] == [DEVICE]
        assert ext.get_browser("someone-elses-browser") is None
        socket.send_json({"event": "ping"})
        assert socket.receive_json() == {"event": "pong"}
    assert ext.browsers() == []


@pytest.mark.parametrize("key,status,code", [("aim_expired", 401, 4001), ("aim_phone", 403, 4003)])
def test_unauthorized_or_non_extension_keys_are_refused(client, key, status, code):
    with client.websocket_connect("/api/v1/browser-extension/connect") as socket:
        socket.send_json(hello(key))
        refused = socket.receive_json()
        assert refused["event"] == "refused" and refused["status"] == status
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_json()
        assert closed.value.code == code
    assert ext.browsers() == []


def test_relay_refuses_anything_but_loopback(client):
    # TestClient's peer is "testclient", not a loopback address.
    with pytest.raises(WebSocketDisconnect) as closed:
        with client.websocket_connect("/api/v1/browser-extension/cdp/anything") as socket:
            socket.receive_text()
    assert closed.value.code == 4003


def test_side_panel_claims_only_new_chats_with_no_browser_picked(client, monkeypatch):
    from app.core import browser_runtime
    monkeypatch.setattr(browser_runtime, "_runtimes", {"chat-4": object()})
    TURNS["chat-3"] = 2
    claim = {"browser_id": DEVICE, "only_if_new": True}
    with client.websocket_connect("/api/v1/browser-extension/connect") as socket:
        socket.send_json(hello("aim_extension"))
        socket.receive_json()
        assert client.get("/api/v1/sessions/chat-1/browser/target").json()["assigned"] is False
        claimed = client.put("/api/v1/sessions/chat-1/browser/target", json=claim).json()
        assert claimed["browser_id"] == DEVICE and claimed["assigned"]
        # The owner picks the built-in browser for chat-2; the panel leaves it alone.
        client.put("/api/v1/sessions/chat-2/browser/target", json={"browser_id": None})
        kept = client.put("/api/v1/sessions/chat-2/browser/target", json=claim).json()
        assert kept["browser_id"] is None and kept["assigned"]
        # Viewing an older chat, or one whose built-in browser is open, never moves it.
        for older in ("chat-3", "chat-4"):
            left = client.put(f"/api/v1/sessions/{older}/browser/target", json=claim).json()
            assert left["browser_id"] is None and not left["assigned"]
        missing = client.put("/api/v1/sessions/chat-5/browser/target", json={"browser_id": "not-connected"})
        assert missing.status_code == 409


def test_only_the_browsers_owner_account_or_the_worker_login_can_use_it(client):
    with client.websocket_connect("/api/v1/browser-extension/connect") as socket:
        # The hello cannot name another account than the enrollment's.
        socket.send_json({**hello("aim_extension"), "account_id": "acct-intruder"})
        socket.receive_json()
        assert ext.get_browser(DEVICE).account_id == OWNER_ACCOUNT
        url = "/api/v1/sessions/chat-1/browser/target"
        for outsider in ("api", "paired:acct-intruder"):
            listed = client.get(url, headers={"x-test-as": outsider}).json()
            assert listed["browsers"] == []
            picked = client.put(url, json={"browser_id": DEVICE}, headers={"x-test-as": outsider})
            assert picked.status_code == 409 and not ext.session_assigned("chat-1")
        owner = {"x-test-as": "paired:" + OWNER_ACCOUNT}
        assert [b["id"] for b in client.get(url, headers=owner).json()["browsers"]] == [DEVICE]
        assert client.put(url, json={"browser_id": DEVICE}, headers=owner).json()["browser_id"] == DEVICE
        # The worker's own login sees it too.
        assert [b["id"] for b in client.get(url).json()["browsers"]] == [DEVICE]
        # Once the chat is on the owner's browser, an outsider cannot move it
        # back to the built-in browser either.
        for outsider in ("api", "paired:acct-intruder"):
            moved = client.put(url, json={"browser_id": None}, headers={"x-test-as": outsider})
            assert moved.status_code == 403 and ext.session_target("chat-1") == DEVICE


def test_panel_sockets_get_the_same_browser_access_as_requests():
    from types import SimpleNamespace

    from app.core import mobile_tool_identity as identity
    owner_browser = SimpleNamespace(paired_client_id="pc-extension", account_id=OWNER_ACCOUNT)

    def access(principal):
        return api.socket_browser_access(SimpleNamespace(state=SimpleNamespace(mobile_tool_principal=principal)))

    admin = {"kind": "admin", "principal_id": "install"}
    assert access(admin)(owner_browser) and access(admin)(None)
    for refused in (None, {"kind": "api_user", "principal_id": "api-user-1"},
                    {"kind": "admin", "principal_id": identity.PREVIEW_PRINCIPAL_ID},
                    {**admin, identity.PREVIEW_LIMITED_KEY: True},
                    {"kind": "paired", "principal_id": "controller-of-acct-intruder", "account_id": "acct-intruder"}):
        assert not access(refused)(owner_browser)
    owner = {"kind": "paired", "principal_id": "controller-of-owner", "account_id": OWNER_ACCOUNT}
    assert access(owner)(owner_browser) and not access(owner)(None)


def test_the_extensions_side_panel_may_use_its_own_browser_even_without_an_account(client, monkeypatch):
    monkeypatch.setattr(api, "_device_account", lambda device: None)  # paired straight to the worker
    with client.websocket_connect("/api/v1/browser-extension/connect") as socket:
        socket.send_json(hello("aim_extension"))
        socket.receive_json()
        url = "/api/v1/sessions/chat-1/browser/target"
        panel = {"x-test-as": "extension"}
        assert [b["id"] for b in client.get(url, headers=panel).json()["browsers"]] == [DEVICE]
        claimed = client.put(url, json={"browser_id": DEVICE, "only_if_new": True}, headers=panel).json()
        assert claimed["browser_id"] == DEVICE
        # Another account-less controller does not get it.
        assert client.get(url, headers={"x-test-as": "paired:"}).json()["browsers"] == []


def test_removing_the_extension_closes_its_socket_at_once(client):
    from app.api.ws_key_watch import close_paired_client_sockets
    with client.websocket_connect("/api/v1/browser-extension/connect") as socket:
        socket.send_json(hello("aim_extension"))
        socket.receive_json()
        REVOKED.add("pc-extension")
        # Settings -> Devices -> Remove signals the device's sockets; no frame from the extension.
        close_paired_client_sockets("pc-extension")
        refused = socket.receive_json()
        assert refused["event"] == "refused" and refused["status"] == 401
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_json()
        assert closed.value.code == 4001
    assert ext.browsers() == []


def test_a_chat_running_on_another_worker_cannot_pick_this_browser(client, monkeypatch):
    from app.network import control
    monkeypatch.setattr(control, "owner_of", lambda session: "other-worker")
    response = client.put("/api/v1/sessions/chat-1/browser/target", json={"browser_id": None})
    assert response.status_code == 409
    listed = client.get("/api/v1/sessions/chat-1/browser/target").json()
    assert listed["remote"] is True and listed["browsers"] == []
