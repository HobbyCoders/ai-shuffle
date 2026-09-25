"""Tests for app.api.browser_panel (register / status / WS auth) and the
extracted check_session_access_for_api_user helper.
"""

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import app.api.browser_panel as bp
from app.api.auth import require_auth
from app.api.sessions_helpers import check_session_access_for_api_user
from app.core import browser_panel_registry as registry


@pytest.fixture
def client(mock_db):
    app = FastAPI()
    app.include_router(bp.router)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def authenticated_client(client):
    client.app.dependency_overrides[require_auth] = lambda: "test-session-token"
    yield client
    client.app.dependency_overrides.clear()


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
            check_session_access_for_api_user(
                {"project_id": "p1"}, {"project_id": "p2"}
            )
        assert exc.value.status_code == 403

    def test_profile_mismatch_denied(self):
        with pytest.raises(HTTPException) as exc:
            check_session_access_for_api_user({"profile_ids": ["a"]}, {"profile_id": "b"})
        assert exc.value.status_code == 403

    def test_profile_in_assigned_agents_ok(self):
        check_session_access_for_api_user({"profile_ids": ["a", "b"]}, {"profile_id": "b"})

    def test_unrestricted_api_user_ok(self):
        # No project/profile restriction on the user -> allowed.
        check_session_access_for_api_user(
            {"id": "u"}, {"project_id": "p", "profile_id": "x"}
        )


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
        await viewer.send_json(msg)
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
            return _DummyHub(), websocket

        async def _detach(session_id, viewer):
            return None

        monkeypatch.setattr(bp.browser_stream, "attach_viewer", _attach)
        monkeypatch.setattr(bp.browser_stream, "detach_viewer", _detach)

    def test_auth_failure_closes_4001(self, client, monkeypatch):
        monkeypatch.setattr(
            bp, "authenticate_websocket", AsyncMock(return_value=(False, None))
        )
        with client.websocket_connect("/api/v1/ws/browser/s1?token=bad") as ws:
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_json()
        assert exc.value.code == 4001

    def test_missing_session_closes_4004(self, client, monkeypatch):
        monkeypatch.setattr(
            bp, "authenticate_websocket", AsyncMock(return_value=(True, None))
        )
        monkeypatch.setattr(bp.database, "get_session", lambda sid: None)
        with client.websocket_connect("/api/v1/ws/browser/s1?token=x") as ws:
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_json()
        assert exc.value.code == 4004

    def test_access_denied_closes_4003(self, client, monkeypatch):
        api_user = {"project_id": "p-allowed"}
        monkeypatch.setattr(
            bp, "authenticate_websocket", AsyncMock(return_value=(True, api_user))
        )
        monkeypatch.setattr(
            bp.database, "get_session", lambda sid: {"id": sid, "project_id": "p-other"}
        )
        with client.websocket_connect("/api/v1/ws/browser/s1?token=x") as ws:
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_json()
        assert exc.value.code == 4003

    def test_handshake_then_protocol_violation_closes_4400(self, client, monkeypatch):
        monkeypatch.setattr(
            bp, "authenticate_websocket", AsyncMock(return_value=(True, None))
        )
        monkeypatch.setattr(bp.database, "get_session", lambda sid: {"id": sid})
        monkeypatch.setattr(bp, "_browser_running", AsyncMock(return_value=False))
        self._patch_hub(monkeypatch)

        with client.websocket_connect("/api/v1/ws/browser/s1?token=x") as ws:
            hello = ws.receive_json()
            assert hello["protocol"] == 2
            assert hello["viewer_id"]
            assert hello["capabilities"] == ["workspace-control"]
            status = ws.receive_json()
            assert status["type"] == "status"
            assert status["state"] == "browser_off"
            assert ws.receive_json()["type"] == "workspace"

            ws.send_json({"type": "totally-bogus"})
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_json()
        assert exc.value.code == 4400

    @pytest.mark.parametrize(
        "message",
        [
            {"type": "input", "kind": "mouse", "event": "move", "x": 3, "y": 4},
            {"type": "viewport", "width": 1280, "height": 800},
            {"type": "navigate", "url": "https://example.test"},
            {"type": "inspect", "x": 1, "y": 2, "mode": "select"},
        ],
    )
    def test_no_runtime_never_falls_back_to_cdp(self, client, monkeypatch, message):
        registry.clear("s1")
        monkeypatch.setattr(
            bp, "authenticate_websocket", AsyncMock(return_value=(True, None))
        )
        monkeypatch.setattr(bp.database, "get_session", lambda sid: {"id": sid})
        monkeypatch.setattr(bp, "_browser_running", AsyncMock(return_value=False))
        self._patch_hub(monkeypatch)
        with client.websocket_connect("/api/v1/ws/browser/s1?token=x") as ws:
            ws.receive_json()
            ws.receive_json()
            assert ws.receive_json()["state"]["available"] is False
            ws.send_json({"type": "pong"})
            ws.send_json(message)
            assert ws.receive_json()["type"] == "error"
        registry.clear("s1")

    @pytest.mark.parametrize(
        "message", ["x" * 32769, '{"type":"input","x":NaN}', "[1]", "{"],
        ids=["oversized", "nonfinite", "array", "invalid-json"],
    )
    def test_bounded_finite_ws_payloads(self, client, monkeypatch, message):
        monkeypatch.setattr(
            bp, "authenticate_websocket", AsyncMock(return_value=(True, None))
        )
        monkeypatch.setattr(bp.database, "get_session", lambda sid: {"id": sid})
        monkeypatch.setattr(bp, "_browser_running", AsyncMock(return_value=False))
        self._patch_hub(monkeypatch)
        with client.websocket_connect("/api/v1/ws/browser/s1?token=x") as ws:
            for _ in range(3):
                ws.receive_json()
            ws.send_text(message)
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_json()
            assert exc.value.code == 4400


class TestWorkspaceEndpoints:
    @pytest.mark.parametrize("operation", ["register", "poll", "report"])
    def test_requires_panel_token(self, client, operation):
        registry.clear("control-api")
        registry.issue_token("control-api")
        response = client.post(
            "/api/v1/browser/panel/control/" + operation,
            json=dict(session_id="control-api", token="wrong", runtime_id="runtime"),
        )
        assert response.status_code == 403
        registry.clear("control-api")

    def test_register_report_supersession_and_limits(self, client):
        from app.core import browser_control as control

        registry.clear("control-api")
        token = registry.issue_token("control-api")
        auth = dict(session_id="control-api", token=token, runtime_id="runtime")
        prefix = "/api/v1/browser/panel/control/"
        assert client.post(prefix + "register", json=auth).json() == {"ok": True, "viewer_count": 0}
        value = control.unavailable("runtime")
        value["available"] = True
        assert (
            client.post(
                prefix + "report", json={**auth, "state": value, "results": []}
            ).status_code
            == 204
        )
        assert (
            client.post("/api/v1/browser/panel/register", json=auth).status_code == 409
        )
        broker = control.get("control-api")
        client.portal.call(broker.attach, "passive", AsyncMock())
        assert client.post(prefix + "poll", json=auth).json() == {
            "commands": [], "viewer_count": 1,
        }
        client.portal.call(broker.detach, "passive")
        assert client.post(prefix + "poll", json=auth).json()["viewer_count"] == 0
        client.portal.call(broker.attach, "reopened", AsyncMock())
        assert client.post(prefix + "poll", json=auth).json()["viewer_count"] == 1
        invalid = {**value, "revision": -1}
        assert (
            client.post(prefix + "report", json={**auth, "state": invalid}).status_code
            == 422
        )
        assert (
            client.post(
                prefix + "register", json={**auth, "runtime_id": "new"}
            ).status_code
            == 200
        )
        for operation in ("register", "poll", "report"):
            assert (
                client.post(
                    prefix + operation, json={**auth, "state": value}
                ).status_code
                == 409
            )
        assert (
            client.post(
                prefix + "report", content=b"x" * (control.MAX_BODY + 1)
            ).status_code
            == 413
        )
        registry.clear("control-api")

    @pytest.mark.parametrize(
        "body",
        [
            [],
            {},
            {"session_id": "x", "token": [], "runtime_id": "r"},
            {"session_id": "x", "token": "x", "runtime_id": "r", "x": float("nan")},
        ],
    )
    def test_invalid_body(self, client, body):
        import json

        response = client.post(
            "/api/v1/browser/panel/control/register", content=json.dumps(body)
        )
        assert response.status_code == 422

    def test_two_viewers_command_result_and_normalized_input(self, client, monkeypatch):
        from app.core import browser_control as control

        registry.clear("two-viewers")
        monkeypatch.setattr(
            bp, "authenticate_websocket", AsyncMock(return_value=(True, None))
        )
        monkeypatch.setattr(bp.database, "get_session", lambda sid: {"id": sid})
        monkeypatch.setattr(bp, "_browser_running", AsyncMock(return_value=False))
        TestBrowserPanelWebSocket()._patch_hub(monkeypatch)
        auth = dict(
            session_id="two-viewers",
            token=registry.issue_token("two-viewers"),
            runtime_id="runtime",
        )
        prefix = "/api/v1/browser/panel/control/"
        assert client.post(prefix + "register", json=auth).status_code == 200
        state = control.unavailable("runtime")
        state.update(
            available=True,
            active_tab_id="tab",
            target_id="target",
            tabs=[
                dict(
                    id="tab", url="https://example.test", title="Example", current=True
                )
            ],
        )
        assert (
            client.post(prefix + "report", json={**auth, "state": state}).status_code
            == 204
        )
        with (
            client.websocket_connect("/api/v1/ws/browser/two-viewers") as first,
            client.websocket_connect("/api/v1/ws/browser/two-viewers") as second,
        ):
            hello = first.receive_json()
            other = second.receive_json()
            assert hello["viewer_id"] != other["viewer_id"]
            for socket in (first, second):
                assert socket.receive_json()["type"] == "status"
                assert socket.receive_json()["type"] == "workspace"
            identity = dict(runtime_id="runtime", tab_id="tab")
            first.send_json(
                dict(
                    type="workspace-command",
                    id="take",
                    op="takeover",
                    args={},
                    **identity,
                )
            )
            commands = client.post(prefix + "poll", json=auth).json()["commands"]
            assert len(commands) == 1
            assert commands[0]["viewer_id"] == hello["viewer_id"]
            state["revision"] = 1
            state["control"].update(owner="human", viewer_id=hello["viewer_id"])
            assert (
                client.post(
                    prefix + "report",
                    json={
                        **auth,
                        "state": state,
                        "results": [dict(id="take", ok=True)],
                    },
                ).status_code
                == 204
            )
            assert first.receive_json()["type"] == "workspace"
            assert first.receive_json() == dict(
                type="command-result", id="take", ok=True
            )
            assert (
                second.receive_json()["state"]["control"]["viewer_id"]
                == hello["viewer_id"]
            )
            first.send_json(
                dict(type="input", id="text", kind="text", text="hello", **identity)
            )
            commands = client.post(prefix + "poll", json=auth).json()["commands"]
            assert commands[0]["op"] == "input"
            assert commands[0]["args"] == dict(
                method="Input.insertText", params=dict(text="hello")
            )
            second.send_json(
                dict(
                    type="workspace-command",
                    id="denied",
                    op="navigate",
                    args={"url": "https://example.test"},
                    **identity,
                )
            )
            denied = second.receive_json()
            assert denied["id"] == "denied" and denied["ok"] is False
        registry.clear("two-viewers")
