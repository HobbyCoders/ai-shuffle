"""
Tests for the Claude-in-Chrome management + CDP proxy API (app/api/chrome_mcp.py).

This router is the in-app replacement for the old ``CHROME_DEBUG=1`` + expose-:9222
DevTools dance: it reports structured status (chrome-doctor as JSON), toggles the
``claude-in-chrome`` built-in, lists CDP page targets, and proxies a localhost-only
CDP websocket behind the normal admin login.

The probes all read host state (``/proc``, the profile dir, ``~/.claude.json``, the
localhost DevTools HTTP port), so the tests patch those boundaries and assert on the
status-assembly logic, the next-step guidance branches, the safety check that the
CDP proxy only ever resolves a localhost target, and that the endpoints are admin-only.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from unittest.mock import patch, MagicMock, AsyncMock

import app.api.chrome_mcp as cm
from app.api.chrome_mcp import router, _build_status, _resolve_target_ws
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


def _patch_probes(**overrides):
    """Patch every host-state probe used by _build_status with sane defaults.

    Returns a contextlib.ExitStack-style list of patchers already started; callers
    use it via the `with _Probes(...)` helper below.
    """
    defaults = {
        "_chrome_running": True,
        "_extension_installed": True,
        "_bridge_active": True,
        "_paired_device": "My Laptop",
        "_cdp_enabled": True,
        "_extension_options_url": "chrome-extension://abc/options.html",
    }
    defaults.update(overrides)
    return defaults


_UNSET = object()


class _Probes:
    """Context manager that patches all _build_status host probes."""

    def __init__(self, cdp_version=_UNSET, disabled_servers=None, **overrides):
        self.values = _patch_probes(**overrides)
        # _cdp_get is async; default to a reachable DevTools port unless a test
        # explicitly passes cdp_version=None to simulate the port being down.
        self.cdp_version = {"Browser": "Chrome/138"} if cdp_version is _UNSET else cdp_version
        self.disabled_servers = disabled_servers or []
        self._patchers = []

    def __enter__(self):
        for name, value in self.values.items():
            p = patch.object(cm, name, return_value=value)
            p.start()
            self._patchers.append(p)
        # Async CDP HTTP probe.
        p = patch.object(cm, "_cdp_get", new=AsyncMock(return_value=self.cdp_version))
        p.start()
        self._patchers.append(p)
        # MCP service: drives `enabled`.
        svc = MagicMock()
        svc.get_disabled_servers.return_value = self.disabled_servers
        p = patch.object(cm, "get_mcp_service", return_value=svc)
        p.start()
        self._patchers.append(p)
        self.service = svc
        return self

    def __exit__(self, *exc):
        for p in reversed(self._patchers):
            p.stop()
        return False


# =============================================================================
# _build_status — assembly + readiness + guidance branches
# =============================================================================
class TestBuildStatus:
    @pytest.mark.asyncio
    async def test_fully_ready(self):
        with _Probes() as _:
            status = await _build_status()
        assert status.enabled is True
        assert status.chrome_running is True
        assert status.cdp_available is True
        assert status.cdp_enabled is True
        assert status.extension_installed is True
        assert status.bridge_active is True
        assert status.paired is True
        assert status.paired_device_name == "My Laptop"
        assert status.ready is True
        assert status.next_step.startswith("Ready")

    @pytest.mark.asyncio
    async def test_disabled_builtin(self):
        with _Probes(disabled_servers=[BROWSER_SERVER_NAME]):
            status = await _build_status()
        assert status.enabled is False
        assert status.ready is False
        assert "disabled" in status.next_step.lower()

    @pytest.mark.asyncio
    async def test_chrome_not_running_skips_cdp_probe(self):
        # When chrome isn't running, the CDP HTTP probe must not even be attempted.
        with _Probes(_chrome_running=False) as probes:
            status = await _build_status()
        assert status.chrome_running is False
        assert status.cdp_available is False
        probes  # cdp_get default would have returned a version; gate is chrome_running
        assert "not running" in status.next_step.lower()

    @pytest.mark.asyncio
    async def test_not_paired_but_cdp_ready_points_to_setup(self):
        with _Probes(_paired_device=None, _bridge_active=False):
            status = await _build_status()
        assert status.paired is False
        assert status.ready is False
        assert "Set up browser" in status.next_step

    @pytest.mark.asyncio
    async def test_not_paired_and_cdp_disabled_asks_for_env(self):
        with _Probes(_paired_device=None, _bridge_active=False, _cdp_enabled=False):
            status = await _build_status()
        assert status.paired is False
        assert "CHROME_CDP=1" in status.next_step

    @pytest.mark.asyncio
    async def test_not_paired_cdp_enabled_but_port_down_asks_restart(self):
        # CHROME_CDP=1 but the running chrome hasn't opened the port yet.
        with _Probes(_paired_device=None, _bridge_active=False, cdp_version=None):
            status = await _build_status()
        assert status.cdp_available is False
        assert status.cdp_enabled is True
        assert "restart" in status.next_step.lower()

    @pytest.mark.asyncio
    async def test_extension_options_url_omitted_when_not_installed(self):
        with _Probes(_extension_installed=False, _paired_device=None, _bridge_active=False):
            status = await _build_status()
        assert status.extension_installed is False
        assert status.extension_options_url is None


# =============================================================================
# HTTP endpoints
# =============================================================================
class TestEndpoints:
    def test_status_endpoint(self, client):
        with _Probes():
            resp = client.get("/api/v1/chrome-mcp/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ready"] is True
        assert body["cdp_port"] == cm.CDP_PORT

    def test_toggle_calls_service_and_returns_status(self, client):
        with _Probes(disabled_servers=[]) as probes:
            resp = client.post("/api/v1/chrome-mcp/toggle", json={"enabled": False})
        assert resp.status_code == 200
        probes.service.set_server_enabled.assert_called_once_with(BROWSER_SERVER_NAME, False)

    def test_targets_lists_pages_first(self, client):
        targets = [
            {"id": "bg1", "type": "background_page", "title": "bg", "url": "chrome://x"},
            {"id": "pg1", "type": "page", "title": "Tab", "url": "https://claude.ai"},
        ]
        with patch.object(cm, "_cdp_get", new=AsyncMock(return_value=targets)):
            resp = client.get("/api/v1/chrome-mcp/targets")
        assert resp.status_code == 200
        body = resp.json()
        assert [t["id"] for t in body] == ["pg1", "bg1"]  # page sorted first

    def test_targets_empty_when_cdp_down(self, client):
        with patch.object(cm, "_cdp_get", new=AsyncMock(return_value=None)):
            resp = client.get("/api/v1/chrome-mcp/targets")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_endpoints_require_admin(self):
        # No dependency override → require_admin should reject.
        bare = FastAPI()
        bare.include_router(router)
        with TestClient(bare) as c:
            assert c.get("/api/v1/chrome-mcp/status").status_code in (401, 403)


# =============================================================================
# CDP target resolution — the localhost-only safety gate
# =============================================================================
class TestResolveTargetWs:
    @pytest.mark.asyncio
    async def test_resolves_matching_localhost_target(self):
        ws = f"ws://{cm.CDP_HOST}:{cm.CDP_PORT}/devtools/page/PAGE1"
        targets = [{"id": "PAGE1", "type": "page", "webSocketDebuggerUrl": ws}]
        with patch.object(cm, "_cdp_get", new=AsyncMock(return_value=targets)):
            url = await _resolve_target_ws("PAGE1")
        assert url == ws

    @pytest.mark.asyncio
    async def test_rejects_non_localhost_upstream(self):
        # A target whose websocket points off-box must never be proxied.
        evil = "ws://10.0.0.5:9221/devtools/page/PAGE1"
        targets = [{"id": "PAGE1", "type": "page", "webSocketDebuggerUrl": evil}]
        with patch.object(cm, "_cdp_get", new=AsyncMock(return_value=targets)):
            url = await _resolve_target_ws("PAGE1")
        assert url is None

    @pytest.mark.asyncio
    async def test_rejects_bad_target_id_shape(self):
        targets = [{"id": "PAGE1", "type": "page", "webSocketDebuggerUrl": "ws://x"}]
        with patch.object(cm, "_cdp_get", new=AsyncMock(return_value=targets)):
            url = await _resolve_target_ws("../etc/passwd")
        assert url is None

    @pytest.mark.asyncio
    async def test_falls_back_to_first_page_when_no_id(self):
        ws = f"ws://{cm.CDP_HOST}:{cm.CDP_PORT}/devtools/page/FIRST"
        targets = [
            {"id": "BG", "type": "background_page", "webSocketDebuggerUrl": "ws://other"},
            {"id": "FIRST", "type": "page", "webSocketDebuggerUrl": ws},
        ]
        with patch.object(cm, "_cdp_get", new=AsyncMock(return_value=targets)):
            url = await _resolve_target_ws(None)
        assert url == ws

    @pytest.mark.asyncio
    async def test_none_when_no_targets(self):
        with patch.object(cm, "_cdp_get", new=AsyncMock(return_value=[])):
            assert await _resolve_target_ws(None) is None
