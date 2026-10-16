"""app.api.emulator: the Phone panel's HTTP and WebSocket contract.

docs/internal/phone-panel-api.md is the contract. adb and gRPC are fakes; no
emulator runs.
"""

from __future__ import annotations

import asyncio
import base64
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import app.api.emulator as api
from app.core.android_emulator import adb as adb_mod
from app.core.android_emulator import grpc_control, stream
from app.core.android_emulator.layout import SdkLayout
from app.core.android_emulator.manager import EmulatorManager
from tests.core.test_android_emulator import (
    FILE, IMAGE, LINUX, FakeAdb, FakeGrpc, _axml, _installed, _zip,
)


class StreamingGrpc(FakeGrpc):
    async def stream_screenshots(self, width=0, height=0, fmt=2):
        w, h = 576, 1280
        yield grpc_control.Frame(w, h, b"\x20" * (w * h * 3), 1, fmt)
        await asyncio.sleep(3600)


@pytest.fixture
def manager(tmp_path, monkeypatch):
    from app.core.android_emulator import manager as manager_mod

    monkeypatch.setattr(manager_mod, "check_host", lambda: LINUX)
    layout = SdkLayout(tmp_path / "android-sdk")
    _installed(layout)
    mgr = EmulatorManager(layout)
    monkeypatch.setattr(api, "get_manager", lambda: mgr)
    monkeypatch.setattr(stream, "_hubs", {})
    return mgr


@pytest.fixture
def fake_adb(monkeypatch):
    fake = FakeAdb()
    monkeypatch.setattr(adb_mod, "run", fake)
    return fake


@pytest.fixture
def client(manager):
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[api.require_emulator_owner] = lambda: "owner"
    with TestClient(app) as test_client:
        yield test_client


def _running(manager, grpc=None):
    dev = manager.device("pixel")
    dev.state, dev.console_port, dev.grpc = "running", 5554, grpc
    dev.booted.set()
    return dev


def test_status_shape(client):
    body = client.get("/api/v1/emulator/status").json()
    assert set(body) == {"available", "reason", "message", "install", "license_url", "devices"}
    assert body["available"] is True and body["reason"] == "ready" and body["install"] is None
    assert body["devices"] == [{"id": "pixel", "name": "Pixel", "state": "stopped", "api": 35,
                                "width": 1080, "height": 2400, "frame_source": ""}]


def test_setup_requires_accepting_the_license(client, manager, monkeypatch):
    response = client.post("/api/v1/emulator/setup", json={})
    assert response.status_code == 400
    assert response.json()["code"] == "license_required"

    async def fake_setup(accept_license, system_image=None):
        return {"reason": "installing", "accepted": accept_license}

    monkeypatch.setattr(manager, "setup", fake_setup)
    response = client.post("/api/v1/emulator/setup", json={"accept_license": True})
    assert response.status_code == 202
    assert response.json() == {"reason": "installing", "accepted": True}


def test_start_and_stop_return_the_device(client, manager, monkeypatch):
    async def fake_start(device_id):
        dev = manager.device(device_id)
        dev.state = "booting"
        return dev

    monkeypatch.setattr(manager, "start", fake_start)
    response = client.post("/api/v1/emulator/devices/pixel/start")
    assert response.status_code == 200 and response.json()["state"] == "booting"
    response = client.post("/api/v1/emulator/devices/pixel/stop")
    assert response.status_code == 200 and response.json()["state"] == "stopped"
    assert client.post("/api/v1/emulator/devices/nexus/stop").status_code == 404


def test_install_takes_one_apk(client, manager, fake_adb):
    url = "/api/v1/emulator/devices/pixel/install"
    apk_bytes = _zip([("AndroidManifest.xml", _axml("com.example.todo"), FILE)])
    response = client.post(url, files={"apk": ("app.apk", apk_bytes)})
    assert response.status_code == 409 and response.json()["code"] == "not_running"
    _running(manager)
    fake_adb.reply(" install -r -t -g", b"Success\n")
    response = client.post(url, files={"apk": ("app.apk", apk_bytes, "application/vnd.android.package-archive")})
    assert response.status_code == 200 and response.json() == {"package": "com.example.todo"}
    assert not list((manager.layout.root / ".uploads").iterdir()), "uploads are removed"
    response = client.post(url, files={"apk": ("notes.txt", b"just text")})
    assert response.status_code == 400 and response.json()["code"] == "bad_apk"
    assert client.post(url, files={"file": ("app.apk", apk_bytes)}).status_code == 400
    assert client.post(url, json={"apk": "x"}).status_code == 400


def test_install_size_is_capped(client, manager, monkeypatch):
    _running(manager)
    monkeypatch.setattr(api, "MAX_BODY", 1024)
    response = client.post("/api/v1/emulator/devices/pixel/install",
                           files={"apk": ("big.apk", b"\0" * 4096)})
    assert response.status_code == 413


def test_only_the_owner_gets_in(manager, monkeypatch):
    import app.api.auth as auth

    app = FastAPI()
    app.include_router(api.router)

    def api_user(request):
        request.state.is_admin = False
        request.state.api_user = {"id": "u1"}
        return "api_key:u1"

    def owner(request):
        request.state.is_admin = True
        request.state.api_user = None
        return "local-owner"

    with TestClient(app) as client:
        monkeypatch.setattr(auth, "require_auth", api_user)
        assert client.get("/api/v1/emulator/status").status_code == 403
        assert client.post("/api/v1/emulator/devices/pixel/start").status_code == 403
        monkeypatch.setattr(auth, "require_auth", owner)
        assert client.get("/api/v1/emulator/status").status_code == 200


async def test_websocket_owner_check(monkeypatch):
    import app.api.ws_auth as ws_auth

    class WS:
        class state:
            mobile_tool_principal = None

    socket = WS()

    async def as_api_user(websocket, token):
        return True, {"id": "u1"}

    monkeypatch.setattr(ws_auth, "authenticate_websocket", as_api_user)
    assert await api._owner_socket(socket, "k") is False

    async def as_owner(websocket, token):
        websocket.state.mobile_tool_principal = {"kind": "admin"}
        return True, None

    monkeypatch.setattr(ws_auth, "authenticate_websocket", as_owner)
    assert await api._owner_socket(socket, None) is True

    async def as_paired(websocket, token):
        websocket.state.mobile_tool_principal = {"kind": "paired", "credential_hash": "h"}
        return True, None

    from app.db import pairing

    monkeypatch.setattr(ws_auth, "authenticate_websocket", as_paired)
    monkeypatch.setattr(pairing, "get_paired_client_by_session_hash", lambda h: {"account_owner_admin": False})
    assert await api._owner_socket(socket, "t") is False
    monkeypatch.setattr(pairing, "get_paired_client_by_session_hash", lambda h: {"account_owner_admin": True})
    assert await api._owner_socket(socket, "t") is True


def _allow_ws(monkeypatch, allowed=True):
    async def owner(websocket, token):
        return allowed

    monkeypatch.setattr(api, "_owner_socket", owner)


def test_stream_refuses_strangers(client, monkeypatch):
    _allow_ws(monkeypatch, False)
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/api/v1/emulator/devices/pixel/stream") as ws:
            ws.receive_json()
    assert exc.value.code == 4001


def test_stream_sends_state_frames_and_takes_input(client, manager, monkeypatch, fake_adb):
    _allow_ws(monkeypatch)
    grpc = StreamingGrpc()
    dev = _running(manager, grpc)
    fake_adb.reply("dumpsys input", b"  SurfaceOrientation: 1\n")
    with client.websocket_connect("/api/v1/emulator/devices/pixel/stream") as ws:
        first = ws.receive_json()
        assert first == {"type": "state", "state": "running", "message": "", "width": 1080, "height": 2400}
        frame = ws.receive_json()
        assert frame["type"] == "frame" and frame["format"] == "jpeg" and frame["seq"] == 1
        assert (frame["width"], frame["height"]) == (1080, 2400)  # device pixels
        assert base64.b64decode(frame["data"])[:2] == b"\xff\xd8"
        assert dev.viewers == 1
        ws.send_text(json.dumps({"type": "tap", "x": 540, "y": 1200}))
        ws.send_text(json.dumps({"type": "key", "key": "BACK"}))
        ws.send_text(json.dumps({"type": "swipe", "x1": 10, "y1": 2000, "x2": 10, "y2": 300, "duration_ms": 60}))
        ws.send_text(json.dumps({"type": "text", "text": "hi"}))
        ws.send_text(json.dumps({"type": "tap", "x": 5000, "y": 1}))
        error = ws.receive_json()
        assert error["type"] == "error" and error["code"] == "bad_point"
        ws.send_text(json.dumps({"type": "rotate"}))
        rotated = ws.receive_json()
        assert rotated["type"] == "state" and (rotated["width"], rotated["height"]) == (2400, 1080)
        ws.send_text(json.dumps({"type": "launch-missiles"}))
        assert ws.receive_json()["code"] == "bad_input"
    assert ("touch", 540, 1200, 512) in grpc.calls
    assert ("key", "GoBack") in grpc.calls and ("text", "hi") in grpc.calls
    assert any(call[0] == "touch" and call[1:3] == (10, 300) for call in grpc.calls)
    assert any(call.endswith("emu rotate") for call in fake_adb.calls)
    assert dev.viewers == 0


def test_stream_reports_state_changes_and_closes_on_garbage(client, manager, monkeypatch):
    _allow_ws(monkeypatch)
    dev = manager.device("pixel")
    with client.websocket_connect("/api/v1/emulator/devices/pixel/stream") as ws:
        assert ws.receive_json()["state"] == "stopped"
        dev.set_state("booting", "Starting the emulator")
        message = ws.receive_json()
        assert message["type"] == "state" and message["state"] == "booting"
        ws.send_text("not json")
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == 4400
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/api/v1/emulator/devices/nexus/stream") as ws:
            ws.receive_json()
    assert exc.value.code == 4004
