"""Tests for the per-session computer-use halt switch and its API surface.

The chat Stop button must halt computer-use for that session alone (the
SessionRuntime interrupt with mode="stop"), the session's next turn must
re-arm it, and other sessions must be unaffected. The global toggle lives in
the /computer-use endpoints; host Stop releases active sessions without a resume route.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from unittest.mock import patch

from app.api.auth import require_admin, require_auth
from app.api.computer_use import router
from app.mcp.computer_use import abort


class TestPerSessionHalt:
    def setup_method(self):
        abort.resume()
        abort.resume("s1")
        abort.resume("s2")

    def test_sessions_are_independent(self):
        abort.request_stop("s1")
        assert abort.is_stopped("s1")
        assert not abort.is_stopped("s2")
        assert not abort.is_stopped(None)

    def test_resume_clears_only_that_session(self):
        abort.request_stop("s1")
        abort.request_stop("s2")
        abort.resume("s1")
        assert not abort.is_stopped("s1")
        assert abort.is_stopped("s2")

    def test_global_key_for_none(self):
        abort.request_stop(None)
        assert abort.is_stopped(None)
        assert abort.is_stopped("")  # falsy ids share the global key
        abort.resume(None)
        assert not abort.is_stopped(None)

    def test_any_stopped_reflects_any_flag(self):
        assert not abort.any_stopped()
        abort.request_stop("s1")
        assert abort.any_stopped()
        abort.resume("s1")
        assert not abort.any_stopped()

    def test_request_stop_idempotent(self):
        abort.request_stop("s1")
        abort.request_stop("s1")  # still halted, no error
        assert abort.is_stopped("s1")


@pytest.fixture
def api():
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_auth] = lambda: "test-session-token"
    app.dependency_overrides[require_admin] = lambda: "test-session-token"
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def _toggle_off():
    from app.db.settings import set_system_setting

    set_system_setting("computer_use_enabled", "false")
    set_system_setting("computer_use_input_mode", "shared_desktop")
    yield
    set_system_setting("computer_use_enabled", "false")
    set_system_setting("computer_use_input_mode", "shared_desktop")


class TestEndpoints:
    def test_status_shape_and_toggle_roundtrip(self, api):
        from app.api import computer_use as cu_api

        with patch.object(cu_api, "computer_use_available", return_value=(True, "ok")):
            r = api.get("/api/v1/computer-use/status")
        assert r.status_code == 200
        body = r.json()
        assert body["available"] is True
        assert body["reason"] == "ok"
        assert body["enabled"] is False
        assert body["active"] is False

        r = api.get("/api/v1/computer-use/enabled")
        assert r.status_code == 200
        assert r.json()["enabled"] is False

        r = api.put("/api/v1/computer-use/enabled", json={"enabled": True})
        assert r.status_code == 200
        assert r.json()["enabled"] is True

        with patch.object(cu_api, "computer_use_available", return_value=(True, "ok")):
            r = api.get("/api/v1/computer-use/status")
        assert r.json()["enabled"] is True

    def test_stop_releases_active_sessions_but_never_resumes(self, api):
        from app.mcp.computer_use import activity, access
        abort.resume("active")
        activity.record(2, session_id="active")
        access.grant("active", ["Notepad"])
        r = api.post("/api/v1/computer-use/stop")
        assert r.status_code == 200
        assert r.json() == {"stopped": 1}
        assert abort.is_stopped("active")
        assert not access.is_granted("active")
        assert not activity.snapshot()["active"]
        assert api.post("/api/v1/computer-use/resume").status_code in (404, 405)
        abort.resume("active")

    def test_disable_immediately_stops_active_control(self, api):
        from app.mcp.computer_use import activity
        activity.record(1, session_id="active")
        assert api.put("/api/v1/computer-use/enabled", json={"enabled": False}).status_code == 200
        assert abort.is_stopped("active")
        assert not activity.snapshot()["active"]
        abort.resume("active")

    def test_input_mode_defaults_to_shared_and_roundtrips(self, api):
        from app.api import computer_use as cu_api

        assert api.get("/api/v1/computer-use/input-mode").json() == {"input_mode": "shared_desktop"}
        r = api.put("/api/v1/computer-use/input-mode", json={"input_mode": "dedicated_desktop"})
        assert r.status_code == 200
        assert r.json() == {"input_mode": "dedicated_desktop"}
        assert cu_api.is_dedicated_desktop() is True
        with patch.object(cu_api, "computer_use_available", return_value=(True, "ok")):
            assert api.get("/api/v1/computer-use/status").json()["input_mode"] == "dedicated_desktop"
        api.put("/api/v1/computer-use/input-mode", json={"input_mode": "shared_desktop"})
        assert cu_api.is_dedicated_desktop() is False

    def test_input_mode_rejects_unknown_values(self, api):
        r = api.put("/api/v1/computer-use/input-mode", json={"input_mode": "anything"})
        assert r.status_code == 422

    def test_unknown_or_unreadable_mode_reads_as_shared(self):
        from app.api import computer_use as cu_api
        from app.db.settings import set_system_setting

        set_system_setting("computer_use_input_mode", "garbage")
        assert cu_api.is_dedicated_desktop() is False
        with patch.object(cu_api, "get_system_setting", side_effect=RuntimeError("db down")):
            assert cu_api.is_dedicated_desktop() is False

    def test_input_mode_change_is_owner_only(self):
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[require_auth] = lambda: "test-session-token"
        with TestClient(app) as client:
            r = client.put("/api/v1/computer-use/input-mode", json={"input_mode": "dedicated_desktop"})
        assert r.status_code in (401, 403)

    def test_stop_requires_authentication(self):
        app = FastAPI()
        app.include_router(router)
        with TestClient(app) as client:
            assert client.post("/api/v1/computer-use/stop").status_code in (401, 403)
