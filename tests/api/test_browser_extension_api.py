"""Routes for the AI Shuffle browser extension (app/api/browser_extension.py)."""

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.api import browser_extension as api
from app.api.auth import require_auth
from app.core import browser_extension as ext

DEVICE = "6f1c2b9e-0d4a-5b8e-9a51-2f3c4d5e6f70"


@pytest.fixture
def client(monkeypatch):
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[require_auth] = lambda: "token"

    def device_for_key(key):
        if key == "aim_extension":
            return {"device_id": DEVICE, "scopes": ["controller", "web:session", "browser:extension"]}
        if key == "aim_phone":
            raise HTTPException(403, "Only the AI Shuffle browser extension can connect here")
        raise HTTPException(401, "This browser is no longer authorized; sign in again")

    async def session(request, session_id):
        return {"id": session_id}

    monkeypatch.setattr(api, "_device_for_key", device_for_key)
    monkeypatch.setattr(api, "require_session", session)
    from app.network import control
    monkeypatch.setattr(control, "owner_of", lambda session: None)
    yield TestClient(app)
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


def test_side_panel_claims_only_chats_with_no_browser_picked(client, monkeypatch):
    from app.core import browser_runtime
    monkeypatch.setattr(browser_runtime, "_runtimes", {})
    with client.websocket_connect("/api/v1/browser-extension/connect") as socket:
        socket.send_json(hello("aim_extension"))
        socket.receive_json()
        assert client.get("/api/v1/sessions/chat-1/browser/target").json()["assigned"] is False
        claimed = client.put("/api/v1/sessions/chat-1/browser/target",
                             json={"browser_id": DEVICE, "only_if_unassigned": True}).json()
        assert claimed["browser_id"] == DEVICE and claimed["assigned"]
        # The owner picks the built-in browser for chat-2; the panel leaves it alone.
        client.put("/api/v1/sessions/chat-2/browser/target", json={"browser_id": None})
        kept = client.put("/api/v1/sessions/chat-2/browser/target",
                          json={"browser_id": DEVICE, "only_if_unassigned": True}).json()
        assert kept["browser_id"] is None and kept["assigned"]
        missing = client.put("/api/v1/sessions/chat-3/browser/target", json={"browser_id": "not-connected"})
        assert missing.status_code == 409


def test_a_chat_running_on_another_worker_cannot_pick_this_browser(client, monkeypatch):
    from app.network import control
    monkeypatch.setattr(control, "owner_of", lambda session: "other-worker")
    response = client.put("/api/v1/sessions/chat-1/browser/target", json={"browser_id": None})
    assert response.status_code == 409
