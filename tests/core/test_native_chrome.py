"""
Tests for native-install browser support:

  * ``builtin_mcp_servers._browser_mcp_entry`` — the env → docker-path → repo
    fallback resolution order.
  * ``native_chrome.resolve_chrome_executable`` — Chrome/Edge/Chromium discovery.
  * ``native_chrome.ensure_started`` — the no-op guards (container).

Pure/logic tests only: nothing here spawns Chrome or binds a port, and no
POSIX-only APIs are exercised (the module and these tests import on Linux CI).
"""

import os
from pathlib import Path
from unittest.mock import patch, AsyncMock

import pytest

import app.core.native_chrome as nc
from app.core.builtin_mcp_servers import (
    _browser_mcp_entry,
    DEFAULT_BROWSER_MCP_ENTRY,
)


# =============================================================================
# _browser_mcp_entry resolution
# =============================================================================
class TestBrowserMcpEntry:
    def test_env_override_wins(self):
        with patch.dict(os.environ, {"BROWSER_MCP_ENTRY": "/custom/entry.js"}):
            # Even if the docker path exists, the explicit override wins.
            with patch("app.core.builtin_mcp_servers.os.path.exists", return_value=True):
                assert _browser_mcp_entry() == "/custom/entry.js"

    def test_docker_path_when_it_exists(self):
        env = {k: v for k, v in os.environ.items() if k != "BROWSER_MCP_ENTRY"}
        with patch.dict(os.environ, env, clear=True):
            with patch("app.core.builtin_mcp_servers.os.path.exists", return_value=True):
                assert _browser_mcp_entry() == DEFAULT_BROWSER_MCP_ENTRY

    def test_repo_fallback_when_nothing_else(self):
        env = {k: v for k, v in os.environ.items() if k != "BROWSER_MCP_ENTRY"}
        with patch.dict(os.environ, env, clear=True):
            with patch("app.core.builtin_mcp_servers.os.path.exists", return_value=False):
                entry = _browser_mcp_entry()

        p = Path(entry)
        # Points at <repo_root>/browser-mcp/dist/index.js.
        assert p.name == "index.js"
        assert p.parent.name == "dist"
        assert p.parent.parent.name == "browser-mcp"
        # Repo root is two parents above app/core/builtin_mcp_servers.py.
        import app.core.builtin_mcp_servers as bm

        repo_root = Path(bm.__file__).resolve().parents[2]
        assert p == repo_root / "browser-mcp" / "dist" / "index.js"


# =============================================================================
# resolve_chrome_executable
# =============================================================================
class TestResolveChromeExecutable:
    def test_env_var_set_and_exists_wins(self):
        with patch.dict(os.environ, {"CHROME_EXECUTABLE": "/opt/my/chrome"}):
            with patch("app.core.native_chrome.os.path.exists", return_value=True):
                assert nc.resolve_chrome_executable() == "/opt/my/chrome"

    def test_env_var_set_but_missing_falls_through(self):
        first_std = r"C:\Program Files\Google\Chrome\Application\chrome.exe"

        def exists(path):
            # env path does NOT exist; the first standard path does.
            return path == first_std

        env = dict(os.environ)
        env["CHROME_EXECUTABLE"] = r"C:\nope\chrome.exe"
        with patch.dict(os.environ, env, clear=True):
            with patch("app.core.native_chrome.os.path.exists", side_effect=exists):
                assert nc.resolve_chrome_executable() == first_std

    def test_first_existing_standard_path_wins(self):
        first_std = r"C:\Program Files\Google\Chrome\Application\chrome.exe"

        def exists(path):
            return path == first_std

        env = {k: v for k, v in os.environ.items() if k != "CHROME_EXECUTABLE"}
        with patch.dict(os.environ, env, clear=True):
            with patch("app.core.native_chrome.os.path.exists", side_effect=exists):
                assert nc.resolve_chrome_executable() == first_std

    def test_returns_none_when_nothing_found(self):
        env = {k: v for k, v in os.environ.items() if k != "CHROME_EXECUTABLE"}
        with patch.dict(os.environ, env, clear=True):
            with patch("app.core.native_chrome.os.path.exists", return_value=False), \
                 patch("app.core.native_chrome.shutil.which", return_value=None):
                assert nc.resolve_chrome_executable() is None


# =============================================================================
# ensure_started — no-op guards
# =============================================================================
class TestEnsureStartedNoOps:
    @pytest.mark.asyncio
    async def test_noop_in_container(self):
        nc._supervisor_task = None
        with patch("app.core.native_chrome._in_container", return_value=True), \
             patch(
                 "app.core.native_chrome.asyncio.create_subprocess_exec",
                 new=AsyncMock(side_effect=AssertionError("must not spawn")),
             ):
            await nc.ensure_started()
        assert nc._supervisor_task is None


class TestCreatePage:
    @pytest.mark.asyncio
    async def test_put_json_new_returns_tab(self):
        class _Resp:
            def raise_for_status(self):
                return None

            def json(self):
                return {"id": "T1", "url": "https://ex.test/"}

        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return None

            async def put(self, url):
                assert "/json/new?" in url
                assert "https://ex.test/" in url
                return _Resp()

        with patch("app.core.native_chrome.httpx.AsyncClient", return_value=_Client()):
            tab = await nc.create_page("https://ex.test/")
        assert tab == {"id": "T1", "url": "https://ex.test/"}

    @pytest.mark.asyncio
    async def test_create_page_none_on_error(self):
        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return None

            async def put(self, url):
                raise OSError("down")

        with patch("app.core.native_chrome.httpx.AsyncClient", return_value=_Client()):
            assert await nc.create_page("https://ex.test/") is None

