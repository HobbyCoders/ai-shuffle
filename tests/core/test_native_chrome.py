"""
Tests for native-install browser support:

  * ``builtin_mcp_servers._browser_mcp_entry`` — the env → docker-path → repo
    fallback resolution order.
  * ``native_chrome.resolve_chrome_executable`` — Chrome/Edge/Chromium discovery.
  * ``native_chrome.ensure_started`` — the no-op guards (container).

Pure/logic tests only: nothing here spawns Chrome or binds a port, and no
POSIX-only APIs are exercised (the module and these tests import on Linux CI).
"""

import asyncio
import os
from pathlib import Path
from unittest.mock import patch, AsyncMock, Mock

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
# browser_label — names the browser that actually runs
# =============================================================================
class TestBrowserLabel:
    def test_container_is_always_chromium(self):
        # The image links Debian Chromium as google-chrome-stable; the
        # executable name must not make the label claim Google Chrome.
        with patch.object(nc, "_in_container", return_value=True), \
             patch.object(nc, "resolve_chrome_executable",
                          return_value="/usr/bin/google-chrome-stable"):
            assert nc.browser_label() == "Chromium"

    @pytest.mark.parametrize("exe,label", [
        (r"C:\Program Files\Google\Chrome\Application\chrome.exe", "Google Chrome"),
        (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe", "Microsoft Edge"),
        ("/usr/bin/google-chrome-stable", "Google Chrome"),
        ("/usr/bin/chromium-browser", "Chromium"),
        ("/opt/other/browser-bin", "browser"),
    ])
    def test_native_label_follows_resolved_executable(self, exe, label):
        with patch.object(nc, "_in_container", return_value=False), \
             patch.object(nc, "resolve_chrome_executable", return_value=exe), \
             patch("app.core.native_chrome.os.path.realpath", side_effect=lambda p: p):
            assert nc.browser_label() == label

    def test_native_symlink_to_chromium_is_reported_as_chromium(self):
        with patch.object(nc, "_in_container", return_value=False), \
             patch.object(nc, "resolve_chrome_executable",
                          return_value="/usr/bin/google-chrome-stable"), \
             patch("app.core.native_chrome.os.path.realpath",
                   return_value="/usr/lib/chromium/chromium"):
            assert nc.browser_label() == "Chromium"

    def test_no_browser_found(self):
        with patch.object(nc, "_in_container", return_value=False), \
             patch.object(nc, "resolve_chrome_executable", return_value=None):
            assert nc.browser_label() == "browser"


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
    @pytest.fixture(autouse=True)
    def no_browser(self, monkeypatch):
        from app.core.config import settings
        monkeypatch.setattr(settings, "start_chrome", True)
        monkeypatch.setattr(nc, "ensure_started", AsyncMock())
        monkeypatch.setattr(nc, "_supervisor_task", None)

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
        nc.ensure_started.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_waits_for_demand_started_cdp(self, monkeypatch):
        monkeypatch.setattr(nc, "_supervisor_task", Mock(done=lambda: False))
        probe = AsyncMock(side_effect=[False, True])
        monkeypatch.setattr(nc, "_cdp_answers", probe)
        response = Mock()
        response.json.return_value = {"id": "T1"}
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.put.return_value = response
        monkeypatch.setattr(nc.httpx, "AsyncClient", lambda **kw: client)
        assert await nc.create_page("about:blank") == {"id": "T1"}
        assert probe.await_count == 2
        client.put.assert_awaited_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("enabled,port", [(False, None), (True, 12345)])
    async def test_disabled_or_other_port_does_not_launch(self, monkeypatch, enabled, port):
        from app.core.config import settings
        monkeypatch.setattr(settings, "start_chrome", enabled)
        monkeypatch.setattr(nc, "_cdp_port", lambda: 9221)
        monkeypatch.setattr(nc.httpx, "AsyncClient", Mock(side_effect=OSError("offline")))
        assert await nc.create_page("about:blank", port=port) is None
        nc.ensure_started.assert_not_called()

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


@pytest.mark.asyncio
async def test_prereq_check_does_not_start_chrome(monkeypatch):
    from app.core import startup

    monkeypatch.setattr(startup.settings, "start_chrome", True)
    start = AsyncMock()
    monkeypatch.setattr(nc, "ensure_started", start)
    startup._check_browser_mcp_prereqs()
    await asyncio.sleep(0)
    start.assert_not_called()


@pytest.mark.asyncio
async def test_concurrent_demand_starts_only_one_supervisor(monkeypatch, tmp_path):
    monkeypatch.setattr(nc, "_in_container", lambda: False)
    monkeypatch.setattr(nc, "_supervisor_task", None)
    monkeypatch.setattr(nc, "_proc", None)
    monkeypatch.setattr(nc, "_stopping", False)
    monkeypatch.setattr(nc, "_last_detail", None)
    monkeypatch.setattr(nc, "resolve_chrome_executable", lambda: "fixture-chrome")
    monkeypatch.setattr(nc, "_profile_dir", lambda: tmp_path)
    monkeypatch.setattr(nc, "_egress_command", lambda exe, args: [exe, *args])
    monkeypatch.setattr(nc, "_egress_env", lambda: {})

    async def probe(port):
        await asyncio.sleep(0)
        return False

    monkeypatch.setattr(nc, "_cdp_answers", probe)
    process = Mock()
    process.pid = 123
    process.returncode = None
    process.wait = AsyncMock()
    exited = asyncio.Event()

    async def wait():
        await exited.wait()
        return 0

    process.wait.side_effect = wait
    process.terminate.side_effect = lambda: exited.set()
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(nc.asyncio, "create_subprocess_exec", spawn)
    try:
        await asyncio.gather(*(nc.ensure_started() for _ in range(8)))
        await asyncio.sleep(0)
        spawn.assert_awaited_once()
        await nc.ensure_started()
        spawn.assert_awaited_once()
    finally:
        await nc.stop()


@pytest.mark.asyncio
async def test_external_browser_is_never_supervised_or_stopped(monkeypatch):
    monkeypatch.setattr(nc, "_in_container", lambda: False)
    monkeypatch.setattr(nc, "_supervisor_task", None)
    monkeypatch.setattr(nc, "_proc", None)
    monkeypatch.setattr(nc, "_stopping", False)
    monkeypatch.setattr(nc, "_last_detail", None)
    monkeypatch.setattr(nc, "_cdp_answers", AsyncMock(return_value=True))
    spawn = AsyncMock()
    monkeypatch.setattr(nc.asyncio, "create_subprocess_exec", spawn)
    await nc.ensure_started()
    assert "external browser" in nc.get_detail()
    await nc.stop()
    spawn.assert_not_called()



@pytest.mark.asyncio
async def test_rapid_launcher_exit_surfaces_its_stderr(monkeypatch, tmp_path):
    """The launcher reports its own failures only on stderr ("[browser-egress]
    could not start: ..."). That output goes to a log under the data dir and
    its last lines reach get_detail(), instead of a bare crash-loop code."""
    monkeypatch.setattr(nc, "_stopping", False)
    monkeypatch.setattr(nc, "_proc", None)
    monkeypatch.setattr(nc, "_last_detail", None)
    monkeypatch.setattr(nc, "_egress_command", lambda exe, args: [exe, *args])
    monkeypatch.setattr(nc, "_egress_env", lambda: {})
    monkeypatch.setattr(nc, "_launcher_log_path", lambda port: tmp_path / "launcher.log", raising=False)
    monkeypatch.setattr(nc, "_RELAUNCH_BACKOFF_S", 0.0, raising=False)
    spawns = []

    async def spawn(*argv, **kwargs):
        err = kwargs.get("stderr")
        spawns.append(err)
        if hasattr(err, "write"):
            err.write(b"policy: default\n[browser-egress] could not start: boom\n")
            err.flush()
        process = Mock()
        process.pid = 4242
        process.returncode = 1
        process.wait = AsyncMock(return_value=1)
        return process

    monkeypatch.setattr(nc.asyncio, "create_subprocess_exec", spawn)
    await nc._supervise("fixture-chrome", 9221)

    assert len(spawns) == 5
    assert all(err is not asyncio.subprocess.DEVNULL for err in spawns)
    detail = nc.get_detail()
    assert "crash-looped" in detail
    assert "[browser-egress] could not start: boom" in detail
    assert "could not start: boom" in (tmp_path / "launcher.log").read_text()


def test_the_browser_is_launched_without_the_server_run_marker(monkeypatch):
    # The browser serves the install, not one server run: the next boot must
    # never stop it as the previous run's orphan (#1711).
    from app.core import process_ledger

    monkeypatch.setenv(process_ledger.RUN_MARKER, "run-1")
    monkeypatch.setenv("SOME_SETTING", "kept")
    env = nc._egress_env()
    assert process_ledger.RUN_MARKER not in env
    assert env["SOME_SETTING"] == "kept"
