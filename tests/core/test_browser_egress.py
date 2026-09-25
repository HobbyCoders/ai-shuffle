"""The supervised browser is launched behind the egress proxy, with the app's policy.

Covers ``app.core.browser_egress`` (policy env + launch argv), the native
supervisor's use of it (``app.core.native_chrome``), and the Docker launch
script ``docker/start-chrome.sh`` run for real with stub ``node``/Chrome.
"""

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

import app.core.native_chrome as nc
from app.core import browser_egress, mcp_config
from app.core.config import settings

REPO_ROOT = Path(__file__).resolve().parents[2]
START_CHROME = REPO_ROOT / "docker" / "start-chrome.sh"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in ("BROWSER_SSRF_ALLOW", "BROWSER_SSRF_BLOCK", "BROWSER_SSRF_DENY_PORTS", "BROWSER_LOOPBACK_HOST"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(mcp_config, "_own_routable_address", lambda: None)
    monkeypatch.setattr(settings, "browser_ssrf_allow_loopback", None, raising=False)


def test_egress_env_is_the_apps_base_browser_policy_and_nothing_else(monkeypatch):
    monkeypatch.setenv("BROWSER_SSRF_BLOCK", "203.0.113.0/24")
    monkeypatch.setenv("CHROME_CDP_PORT", "9333")
    env = browser_egress.egress_env()
    base = mcp_config._build_browser_server_env(None)
    assert env["BROWSER_SSRF_ALLOW"] == base["BROWSER_SSRF_ALLOW"]
    assert env["BROWSER_SSRF_DENY_PORTS"] == base["BROWSER_SSRF_DENY_PORTS"]
    assert str(settings.port) in env["BROWSER_SSRF_DENY_PORTS"].split(",")
    assert env["BROWSER_SSRF_BLOCK"] == "203.0.113.0/24"
    assert env["CHROME_CDP_PORT"] == "9333"
    assert set(env) <= set(browser_egress.EGRESS_ENV_KEYS)


def test_egress_env_follows_a_disabled_loopback_setting(monkeypatch):
    monkeypatch.setattr(settings, "browser_ssrf_allow_loopback", False)
    assert "BROWSER_SSRF_ALLOW" not in browser_egress.egress_env()


def test_launch_command_wraps_the_browser(monkeypatch, tmp_path):
    launcher = tmp_path / "egress-launch.js"
    launcher.write_text("")
    monkeypatch.setattr(browser_egress, "launcher_path", lambda: launcher)
    assert browser_egress.launch_command("/bin/chrome", ["--a", "about:blank"], node="/bin/node") == [
        "/bin/node", str(launcher), "--watch-stdin", "--", "/bin/chrome", "--a", "about:blank",
    ]


def test_launcher_sits_beside_the_mcp_entry(monkeypatch):
    monkeypatch.setenv("BROWSER_MCP_ENTRY", "/opt/x/dist/index.js")
    assert browser_egress.launcher_path() == Path("/opt/x/dist/egress-launch.js")


def test_launch_command_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setattr(browser_egress, "launcher_path", lambda: tmp_path / "missing.js")
    with pytest.raises(browser_egress.EgressUnavailable, match="missing"):
        browser_egress.launch_command("/bin/chrome", [], node="/bin/node")
    (tmp_path / "missing.js").write_text("")
    monkeypatch.setattr(browser_egress.shutil, "which", lambda name: None)
    with pytest.raises(browser_egress.EgressUnavailable, match="node"):
        browser_egress.launch_command("/bin/chrome", [])


def test_main_prints_key_value_lines(monkeypatch, capsys):
    monkeypatch.setattr(browser_egress, "egress_env", lambda: {"BROWSER_SSRF_ALLOW": "127.0.0.0/8", "BROWSER_SSRF_BLOCK": "bad\nX=1"})
    browser_egress.main()
    assert capsys.readouterr().out == "BROWSER_SSRF_ALLOW=127.0.0.0/8\n"


# ---------------------------------------------------------------------------
# Native supervisor


def _reset_supervisor(monkeypatch, tmp_path):
    monkeypatch.setattr(nc, "_in_container", lambda: False)
    monkeypatch.setattr(nc, "_supervisor_task", None)
    monkeypatch.setattr(nc, "_proc", None)
    monkeypatch.setattr(nc, "_stopping", False)
    monkeypatch.setattr(nc, "_last_detail", None)
    monkeypatch.setattr(nc, "resolve_chrome_executable", lambda: "/fixture/chrome")
    monkeypatch.setattr(nc, "_profile_dir", lambda: tmp_path)
    monkeypatch.setattr(nc, "_cdp_answers", AsyncMock(return_value=False))


@pytest.mark.asyncio
async def test_native_browser_is_spawned_behind_the_proxy(monkeypatch, tmp_path):
    _reset_supervisor(monkeypatch, tmp_path)
    launcher = tmp_path / "egress-launch.js"
    launcher.write_text("")
    monkeypatch.setattr(browser_egress, "launcher_path", lambda: launcher)
    monkeypatch.setattr(browser_egress.shutil, "which", lambda name: "/fixture/node")
    monkeypatch.setattr(settings, "browser_ssrf_allow_loopback", True)

    exited = asyncio.Event()
    process = Mock(pid=1, returncode=None)
    process.stdin = Mock()
    process.stdin.close.side_effect = lambda: exited.set()

    async def wait():
        await exited.wait()
        return 0

    process.wait = AsyncMock(side_effect=wait)
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(nc.asyncio, "create_subprocess_exec", spawn)
    try:
        await nc.ensure_started()
        for _ in range(50):
            if spawn.await_count:
                break
            await asyncio.sleep(0.01)
        argv = list(spawn.await_args.args)
        assert argv[:5] == ["/fixture/node", str(launcher), "--watch-stdin", "--", "/fixture/chrome"]
        assert f"--remote-debugging-port={nc._cdp_port()}" in argv
        kwargs = spawn.await_args.kwargs
        assert kwargs["stdin"] is asyncio.subprocess.PIPE
        assert kwargs["env"]["BROWSER_SSRF_ALLOW"].startswith("127.0.0.0/8,::1/128")
        assert str(settings.port) in kwargs["env"]["BROWSER_SSRF_DENY_PORTS"].split(",")
    finally:
        await nc.stop()
    # Stopping closes the launcher's stdin (its cue to stop Chrome) first.
    process.stdin.close.assert_called_once()


@pytest.mark.asyncio
async def test_native_browser_is_not_started_without_the_proxy(monkeypatch, tmp_path):
    _reset_supervisor(monkeypatch, tmp_path)
    monkeypatch.setattr(browser_egress, "launcher_path", lambda: tmp_path / "absent.js")
    monkeypatch.setattr(browser_egress.shutil, "which", lambda name: "/fixture/node")
    spawn = AsyncMock()
    monkeypatch.setattr(nc.asyncio, "create_subprocess_exec", spawn)
    await nc.ensure_started()
    assert "egress proxy missing" in nc.get_detail()
    assert nc._supervisor_task is None
    spawn.assert_not_called()
    await nc.stop()


# ---------------------------------------------------------------------------
# Docker launch script


@pytest.mark.skipif(os.name == "nt", reason="POSIX-only: runs a bash script")
@pytest.mark.parametrize("policy_ok", [True, False], ids=["app-policy", "policy-failed"])
def test_start_chrome_execs_chrome_through_the_egress_launcher(tmp_path, policy_ok):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    record = tmp_path / "node-call"
    # Stub node: record argv and the policy env it was given.
    (bindir / "node").write_text(
        "#!/bin/bash\n"
        f'printf "%s\\n" "$@" > "{record}"\n'
        f'echo "ALLOW=${{BROWSER_SSRF_ALLOW-unset}}" >> "{record}"\n'
        f'echo "DENY=${{BROWSER_SSRF_DENY_PORTS-unset}}" >> "{record}"\n'
    )
    app_root = tmp_path / "app"
    module = app_root / "app" / "core"
    module.mkdir(parents=True)
    (app_root / "app" / "__init__.py").write_text("")
    (module / "__init__.py").write_text("")
    (module / "browser_egress.py").write_text(
        "print('noise line')\nprint('BROWSER_SSRF_ALLOW=127.0.0.0/8')\n"
        "print('BROWSER_SSRF_DENY_PORTS=8000,8001')\nprint('LD_PRELOAD=/evil.so')\n"
        if policy_ok
        else "raise SystemExit(1)\n"
    )
    (bindir / "python").write_text(f'#!/bin/bash\nexec "{sys.executable}" "$@"\n')
    for stub in bindir.iterdir():
        stub.chmod(0o755)
    entry = tmp_path / "egress-launch.js"
    entry.write_text("")
    home = tmp_path / "home"
    home.mkdir()
    env = {
        "PATH": f"{bindir}:/usr/bin:/bin",
        "HOME": str(home),
        "CHROME_EXECUTABLE": "/fixture/chrome",
        "BROWSER_EGRESS_ENTRY": str(entry),
        "AIS_APP_ROOT": str(app_root),
        "BROWSER_SSRF_ALLOW": "10.0.0.0/8",
    }
    subprocess.run(["bash", str(START_CHROME)], env=env, check=True, timeout=30,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    lines = record.read_text().splitlines()
    assert lines[:3] == [str(entry), "--", "/fixture/chrome"]
    assert "--remote-debugging-port=9221" in lines
    assert "about:blank" in lines
    if policy_ok:
        assert "ALLOW=127.0.0.0/8" in lines
        assert "DENY=8000,8001" in lines
        assert not any("LD_PRELOAD" in line or "evil" in line for line in lines)
    else:
        assert "ALLOW=unset" in lines


@pytest.mark.skipif(os.name == "nt", reason="POSIX-only: runs a bash script")
def test_start_chrome_refuses_without_the_launcher(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    result = subprocess.run(
        ["bash", str(START_CHROME)],
        env={"PATH": "/usr/bin:/bin", "HOME": str(home), "CHROME_EXECUTABLE": "/bin/true",
             "BROWSER_EGRESS_ENTRY": str(tmp_path / "absent.js")},
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 1
    assert "egress proxy missing" in result.stderr


def test_module_entry_point_prints_the_policy_for_the_shell():
    """What start-chrome.sh runs in the image: ``python -m app.core.browser_egress``."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("BROWSER_SSRF_")}
    env["BROWSER_SSRF_ALLOW"] = "127.0.0.0/8"
    out = subprocess.run(
        [sys.executable, "-m", "app.core.browser_egress"],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=60, check=True,
    ).stdout
    pairs = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    assert pairs["BROWSER_SSRF_ALLOW"] == "127.0.0.0/8"
    assert pairs["BROWSER_SSRF_DENY_PORTS"]
