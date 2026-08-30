"""Tests for app.api.browser_panel (register / status / WS auth) and the
extracted check_session_access_for_api_user helper.
"""

import pytest
from unittest.mock import AsyncMock
from fastapi import HTTPException
from starlette.websockets import WebSocketDisconnect

import app.api.browser_panel as bp
from app.api.sessions_helpers import check_session_access_for_api_user
from app.core import browser_panel_registry as registry


# ===========================================================================
# Extracted pure access helper
# ===========================================================================
class TestAccessHelper:
    def test_admin_has_full_access(self):
        # api_user None == admin: never raises.
        check_session_access_for_api_user(None, {"project_id": "p1", "profile_id": "x"})

    def test_project_match_ok(self):
        check_session_access_for_api_user({"project_id": "p1"}, {"project_id": "p1"})

    def test_project_mismatch_denied(self):
        with pytest.raises(HTTPException) as exc:
            check_session_access_for_api_user({"project_id": "p1"}, {"project_id": "p2"})
        assert exc.value.status_code == 403

    def test_profile_mismatch_denied(self):
        with pytest.raises(HTTPException) as exc:
            check_session_access_for_api_user({"profile_id": "a"}, {"profile_id": "b"})
        assert exc.value.status_code == 403

    def test_unrestricted_api_user_ok(self):
        # No project/profile restriction on the user -> allowed.
        check_session_access_for_api_user({"id": "u"}, {"project_id": "p", "profile_id": "x"})


# ===========================================================================
# POST /api/v1/browser/panel/register
# ===========================================================================
class TestRegisterEndpoint:
    def test_rejects_bad_token(self, client):
        registry.clear("sess-reg")
        registry.issue_token("sess-reg")  # mint a real token so a wrong one 403s
        resp = client.post(
            "/api/v1/browser/panel/register",
            json={"session_id": "sess-reg", "token": "not-the-token"},
        )
        assert resp.status_code == 403
        assert registry.get_target("sess-reg") is None

    def test_registers_target_on_valid_token(self, client):
        registry.clear("sess-reg2")
        tok = registry.issue_token("sess-reg2")
        resp = client.post(
            "/api/v1/browser/panel/register",
            json={
                "session_id": "sess-reg2",
                "token": tok,
                "target_id": "t1",
                "tab_id": "tabA",
                "url": "http://ex/",
            },
        )
        assert resp.status_code == 204
        tgt = registry.get_target("sess-reg2")
        assert tgt is not None
        assert tgt.target_id == "t1"
        assert tgt.url == "http://ex/"


# ===========================================================================
# GET /api/v1/sessions/{id}/browser/panel
# ===========================================================================
class TestPanelStatusEndpoint:
    def test_status_reports_target(self, authenticated_client, monkeypatch):
        from app.db import database

        database.create_session(session_id="sess-status", profile_id="default")
        registry.clear("sess-status")
        registry.register("sess-status", "tgt", None, "http://z/")
        monkeypatch.setattr(bp, "_browser_running", AsyncMock(return_value=True))

        resp = authenticated_client.get("/api/v1/sessions/sess-status/browser/panel")
        assert resp.status_code == 200
        body = resp.json()
        assert body == {"has_target": True, "url": "http://z/", "browser_running": True}

    def test_status_404_for_unknown_session(self, authenticated_client, monkeypatch):
        monkeypatch.setattr(bp, "_browser_running", AsyncMock(return_value=False))
        resp = authenticated_client.get("/api/v1/sessions/does-not-exist/browser/panel")
        assert resp.status_code == 404


# ===========================================================================
# WS /api/v1/ws/browser/{id} — auth close codes + handshake
# ===========================================================================
class _DummyHub:
    def last_bounds(self):
        return None

    async def send_to_viewer(self, viewer, msg):
        return True

    async def dispatch_input(self, cmd):
        return None

    async def ensure_target(self, url):
        return False

    async def dispatch_inspect(self, viewer, inspect):
        return None


class TestBrowserPanelWebSocket:
    def _patch_hub(self, monkeypatch):
        async def _attach(session_id, websocket):
            return _DummyHub(), object()

        async def _detach(session_id, viewer):
            return None

        monkeypatch.setattr(bp.browser_stream, "attach_viewer", _attach)
        monkeypatch.setattr(bp.browser_stream, "detach_viewer", _detach)

    def test_auth_failure_closes_4001(self, client, monkeypatch):
        monkeypatch.setattr(bp, "authenticate_websocket", AsyncMock(return_value=(False, None)))
        with client.websocket_connect("/api/v1/ws/browser/s1?token=bad") as ws:
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_json()
        assert exc.value.code == 4001

    def test_missing_session_closes_4004(self, client, monkeypatch):
        monkeypatch.setattr(bp, "authenticate_websocket", AsyncMock(return_value=(True, None)))
        monkeypatch.setattr(bp.database, "get_session", lambda sid: None)
        with client.websocket_connect("/api/v1/ws/browser/s1?token=x") as ws:
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_json()
        assert exc.value.code == 4004

    def test_access_denied_closes_4003(self, client, monkeypatch):
        api_user = {"project_id": "p-allowed"}
        monkeypatch.setattr(bp, "authenticate_websocket", AsyncMock(return_value=(True, api_user)))
        monkeypatch.setattr(
            bp.database, "get_session", lambda sid: {"id": sid, "project_id": "p-other"}
        )
        with client.websocket_connect("/api/v1/ws/browser/s1?token=x") as ws:
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_json()
        assert exc.value.code == 4003

    def test_handshake_then_protocol_violation_closes_4400(self, client, monkeypatch):
        monkeypatch.setattr(bp, "authenticate_websocket", AsyncMock(return_value=(True, None)))
        monkeypatch.setattr(bp.database, "get_session", lambda sid: {"id": sid})
        monkeypatch.setattr(bp, "_browser_running", AsyncMock(return_value=False))
        self._patch_hub(monkeypatch)

        with client.websocket_connect("/api/v1/ws/browser/s1?token=x") as ws:
            hello = ws.receive_json()
            assert hello == {"type": "hello", "protocol": 1}
            status = ws.receive_json()
            assert status["type"] == "status"
            assert status["state"] == "browser_off"

            ws.send_json({"type": "totally-bogus"})
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_json()
        assert exc.value.code == 4400

    def test_handshake_accepts_pong_and_forwards_input(self, client, monkeypatch):
        monkeypatch.setattr(bp, "authenticate_websocket", AsyncMock(return_value=(True, None)))
        monkeypatch.setattr(bp.database, "get_session", lambda sid: {"id": sid})
        monkeypatch.setattr(bp, "_browser_running", AsyncMock(return_value=False))

        dispatched = []
        hub = _DummyHub()

        async def _dispatch(cmd):
            dispatched.append(cmd)

        hub.dispatch_input = _dispatch

        async def _attach(session_id, websocket):
            return hub, object()

        async def _detach(session_id, viewer):
            return None

        monkeypatch.setattr(bp.browser_stream, "attach_viewer", _attach)
        monkeypatch.setattr(bp.browser_stream, "detach_viewer", _detach)

        with client.websocket_connect("/api/v1/ws/browser/s1?token=x") as ws:
            ws.receive_json()  # hello
            ws.receive_json()  # status
            ws.send_json({"type": "pong"})  # ignored, no close
            ws.send_json({
                "type": "input", "kind": "mouse", "event": "move", "x": 3, "y": 4,
            })
            # Give the server loop a moment, then close cleanly from the client.
        # After the context exits the connection is closed; the input we sent
        # should have been translated + dispatched.
        assert dispatched and dispatched[0]["method"] == "Input.dispatchMouseEvent"

    def test_handshake_forwards_viewport(self, client, monkeypatch):
        monkeypatch.setattr(bp, "authenticate_websocket", AsyncMock(return_value=(True, None)))
        monkeypatch.setattr(bp.database, "get_session", lambda sid: {"id": sid})
        monkeypatch.setattr(bp, "_browser_running", AsyncMock(return_value=False))

        dispatched = []
        hub = _DummyHub()

        async def _dispatch(cmd):
            dispatched.append(cmd)

        hub.dispatch_input = _dispatch

        async def _attach(session_id, websocket):
            return hub, object()

        async def _detach(session_id, viewer):
            return None

        monkeypatch.setattr(bp.browser_stream, "attach_viewer", _attach)
        monkeypatch.setattr(bp.browser_stream, "detach_viewer", _detach)

        with client.websocket_connect("/api/v1/ws/browser/s1?token=x") as ws:
            ws.receive_json()  # hello
            ws.receive_json()  # status
            ws.send_json({"type": "viewport", "width": 1280, "height": 800})
        assert dispatched and dispatched[0]["method"] == "Emulation.setDeviceMetricsOverride"
        assert dispatched[0]["params"]["width"] == 1280
        assert dispatched[0]["params"]["height"] == 800

    def test_inspect_is_dispatched_to_hub(self, client, monkeypatch):
        monkeypatch.setattr(bp, "authenticate_websocket", AsyncMock(return_value=(True, None)))
        monkeypatch.setattr(bp.database, "get_session", lambda sid: {"id": sid})
        monkeypatch.setattr(bp, "_browser_running", AsyncMock(return_value=False))

        inspected = []
        hub = _DummyHub()

        async def _dispatch_inspect(viewer, inspect):
            inspected.append(inspect)

        hub.dispatch_inspect = _dispatch_inspect

        async def _attach(session_id, websocket):
            return hub, object()

        async def _detach(session_id, viewer):
            return None

        monkeypatch.setattr(bp.browser_stream, "attach_viewer", _attach)
        monkeypatch.setattr(bp.browser_stream, "detach_viewer", _detach)

        with client.websocket_connect("/api/v1/ws/browser/s1?token=x") as ws:
            ws.receive_json()  # hello
            ws.receive_json()  # status
            ws.send_json({"type": "inspect", "x": 12, "y": 34, "mode": "select"})
        assert inspected and inspected[0].mode == "select"
        assert (inspected[0].x, inspected[0].y) == (12.0, 34.0)

    def test_invalid_inspect_closes_4400(self, client, monkeypatch):
        monkeypatch.setattr(bp, "authenticate_websocket", AsyncMock(return_value=(True, None)))
        monkeypatch.setattr(bp.database, "get_session", lambda sid: {"id": sid})
        monkeypatch.setattr(bp, "_browser_running", AsyncMock(return_value=False))
        self._patch_hub(monkeypatch)

        with client.websocket_connect("/api/v1/ws/browser/s1?token=x") as ws:
            ws.receive_json()  # hello
            ws.receive_json()  # status
            ws.send_json({"type": "inspect", "x": 1, "y": 2, "mode": "drag"})
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_json()
        assert exc.value.code == 4400
