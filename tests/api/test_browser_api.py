"""
Tests for the browser-automation management API (app/api/browser.py).

The router is deliberately tiny now that the built-in ``browser`` server is a
login-less spawned MCP server (no account, extension, or pairing): it reports
``{enabled, browser_running, tool_server_ok}`` and toggles the built-in. The one
live probe is a loopback CDP ``GET /json/version`` (``browser_running``), so the
tests patch that boundary and the MCP service, then assert the status-assembly
logic, the toggle path, and that the endpoints are admin-only.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from unittest.mock import patch, MagicMock, AsyncMock

import app.api.browser as br
from app.api.browser import router, _build_status
from app.api.auth import require_admin
from app.core.builtin_mcp_servers import BROWSER_SERVER_NAME


# =============================================================================
# Fixtures
# =============================================================================
@pytest.fixture
def app():
    application = FastAPI()
    application.include_router(router)
    application.dependency_overrides[require_admin] = lambda: "test-admin-token"
    return application


@pytest.fixture
def client(app):
    with TestClient(app) as test_client:
        yield test_client


_UNSET = object()


class _Probes:
    """Patch the two boundaries _build_status touches: the CDP probe + MCP service.

    ``cdp_version`` drives ``browser_running`` (a reachable ``/json/version``
    response vs None); ``disabled_servers`` drives ``enabled``.
    """

    def __init__(self, cdp_version=_UNSET, disabled_servers=None):
        self.cdp_version = {"Browser": "Chrome/138"} if cdp_version is _UNSET else cdp_version
        self.disabled_servers = disabled_servers or []
        self._patchers = []

    def __enter__(self):
        p = patch.object(br, "_cdp_get", new=AsyncMock(return_value=self.cdp_version))
        p.start()
        self._patchers.append(p)
        svc = MagicMock()
        svc.get_disabled_servers.return_value = self.disabled_servers
        p = patch.object(br, "get_mcp_service", return_value=svc)
        p.start()
        self._patchers.append(p)
        self.service = svc
        return self

    def __exit__(self, *exc):
        for p in reversed(self._patchers):
            p.stop()
        return False


# =============================================================================
# _build_status — assembly
# =============================================================================
class TestBuildStatus:
    @pytest.mark.asyncio
    async def test_enabled_and_browser_reachable(self):
        with _Probes():
            status = await _build_status()
        assert status.enabled is True
        assert status.browser_running is True
        assert status.tool_server_ok is True

    @pytest.mark.asyncio
    async def test_disabled_builtin(self):
        with _Probes(disabled_servers=[BROWSER_SERVER_NAME]):
            status = await _build_status()
        assert status.enabled is False
        # Even if the browser is reachable, disabled means the tools can't run.
        assert status.browser_running is True
        assert status.tool_server_ok is False

    @pytest.mark.asyncio
    async def test_enabled_but_browser_unreachable(self):
        # CDP probe fails → browser_running False → tool_server_ok False.
        with _Probes(cdp_version=None):
            status = await _build_status()
        assert status.enabled is True
        assert status.browser_running is False
        assert status.tool_server_ok is False

    @pytest.mark.asyncio
    async def test_tool_server_ok_requires_both(self):
        with _Probes(disabled_servers=[BROWSER_SERVER_NAME], cdp_version=None):
            status = await _build_status()
        assert status.enabled is False
        assert status.browser_running is False
        assert status.tool_server_ok is False

    @pytest.mark.asyncio
    async def test_reachable_has_no_detail(self):
        with _Probes():
            status = await _build_status()
        assert status.detail is None

    @pytest.mark.asyncio
    async def test_unreachable_missing_entry_reports_not_built(self):
        # No supervisor detail + the browser-mcp entry file does not exist →
        # detail explains the tool server was never built.
        with _Probes(cdp_version=None), \
             patch.object(br.native_chrome, "get_detail", return_value=None), \
             patch.object(br.os.path, "exists", return_value=False):
            status = await _build_status()
        assert status.browser_running is False
        assert status.detail is not None
        assert status.detail.startswith("browser-mcp not built")

    @pytest.mark.asyncio
    async def test_unreachable_prefers_supervisor_detail(self):
        # When the native supervisor has a detail, it wins over the entry check.
        with _Probes(cdp_version=None), \
             patch.object(
                 br.native_chrome,
                 "get_detail",
                 return_value="external browser on port 9221",
             ):
            status = await _build_status()
        assert status.detail == "external browser on port 9221"


# =============================================================================
# HTTP endpoints
# =============================================================================
class TestEndpoints:
    def test_status_endpoint(self, client):
        with _Probes():
            resp = client.get("/api/v1/browser/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body == {
            "enabled": True,
            "browser_running": True,
            "tool_server_ok": True,
            "detail": None,
        }

    def test_toggle_calls_service_and_returns_status(self, client):
        with _Probes(disabled_servers=[]) as probes:
            resp = client.post("/api/v1/browser/toggle", json={"enabled": False})
        assert resp.status_code == 200
        probes.service.set_server_enabled.assert_called_once_with(BROWSER_SERVER_NAME, False)

    def test_toggle_reflects_new_status(self, client):
        # Service reports the server disabled after the toggle → enabled False.
        with _Probes(disabled_servers=[BROWSER_SERVER_NAME]):
            resp = client.post("/api/v1/browser/toggle", json={"enabled": False})
        assert resp.status_code == 200
        assert resp.json()["enabled"] is False

    def test_endpoints_require_admin(self):
        # No dependency override → require_admin should reject.
        bare = FastAPI()
        bare.include_router(router)
        with TestClient(bare) as c:
            assert c.get("/api/v1/browser/status").status_code in (401, 403)
            assert c.post("/api/v1/browser/toggle", json={"enabled": True}).status_code in (401, 403)
