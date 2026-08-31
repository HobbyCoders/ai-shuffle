"""Tests for the per-session computer-use halt switch and its API surface.

The chat Stop button must halt computer-use for that session alone (the
SessionRuntime interrupt with mode="stop"), the session's next turn must
re-arm it, and other sessions must be unaffected. The global toggle lives in
the /computer-use endpoints; the old emergency-stop stop/resume routes are
gone.
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
    yield
    set_system_setting("computer_use_enabled", "false")


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

    def test_stop_and_resume_routes_are_gone(self, api):
        assert api.post("/api/v1/computer-use/stop").status_code in (404, 405)
        assert api.post("/api/v1/computer-use/resume").status_code in (404, 405)
