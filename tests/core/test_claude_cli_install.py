"""Native CLI jobs use only official commands and preserve the owner's home."""
import subprocess
import sys
import threading
from pathlib import Path
from unittest.mock import Mock

import pytest

from app.core import claude_cli_install as ci, claude_cli_status as cs, cli_update_check as cuc


@pytest.fixture
def worker(monkeypatch, tmp_path):
    store = {}
    monkeypatch.setattr(ci.database, "get_system_setting", store.get)
    monkeypatch.setattr(ci.database, "set_system_setting", lambda k, v: store.update({k: v}))
    monkeypatch.setattr(ci.settings, "data_dir", tmp_path / "data")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(ci, "platform", lambda: "desktop")
    monkeypatch.setattr(ci, "_job", None)
    monkeypatch.setattr(cuc, "_found", {})
    status = cs.ClaudeCliStatus(installed=False)
    monkeypatch.setattr(cs, "get_status", lambda **kw: status)
    return store, status, tmp_path


@pytest.mark.parametrize("where,before,after", [
    ("docker", None, "0\n"), ("docker", "1\n", "0\n"), ("docker", "0\n", "0\n"),
    ("desktop", None, None), ("desktop", "1\n", "1\n"),
])
def test_start_time_install_of_older_images_is_turned_off(worker, monkeypatch, where, before, after):
    """Images built before the removal install Claude Code at container start
    while this file says 1, or while it is missing. Docker writes 0; a desktop
    worker has no entrypoint and leaves the folder alone."""
    _, _, root = worker
    marker = root / "data" / ci.LEGACY_MARKER
    monkeypatch.setattr(ci, "platform", lambda: where)
    if before is not None:
        marker.parent.mkdir(parents=True)
        marker.write_text(before)
    ci.retire_keep_installed()
    assert (marker.read_text() if marker.exists() else None) == after


def _job(kind="install"):
    ci._job = {"kind": kind, "state": "running", "started_at": ci._now(), "finished_at": None,
               "message": None, "output_tail": None}


def _startup(monkeypatch):
    from app.core import startup, credential_gate, claude_auth_mode, claude_relogin_notice
    start = Mock()
    monkeypatch.setattr(ci, "start", start)
    monkeypatch.setattr(claude_auth_mode, "ensure_mode_recorded", lambda signed_in: "subscription")
    monkeypatch.setattr(claude_relogin_notice, "mark_if_needed", lambda: None)
    monkeypatch.setattr(credential_gate, "record_startup", lambda cli: None)
    monkeypatch.setattr(startup.auth_service, "is_setup_required", lambda: True)
    monkeypatch.setattr(startup, "_check_browser_mcp_prereqs", lambda: None)
    monkeypatch.setattr(startup, "_warn_removed_env_vars", lambda: None)
    monkeypatch.setattr(startup, "settings", ci.settings)
    return startup, start


@pytest.mark.parametrize("where", ["desktop", "docker"])
@pytest.mark.parametrize("installed", [False, True])
@pytest.mark.parametrize("old_setting", [None, "1"])
def test_worker_start_never_installs_updates_or_removes(worker, monkeypatch, where, installed, old_setting):
    """Missing or present, with or without the removed "keep installed" row
    still saved: starting the worker starts no install, update or removal."""
    store, cli, _ = worker
    cli.installed, cli.cli_version = installed, "2.1.1" if installed else None
    monkeypatch.setattr(ci, "platform", lambda: where)
    if old_setting:
        store[ci._key("claude_cli_keep_installed")] = old_setting
    startup, start = _startup(monkeypatch)
    run = Mock()
    monkeypatch.setattr(ci, "_run_command", run)
    startup.check_prerequisites()
    start.assert_not_called()
    run.assert_not_called()
    assert ci.payload()["job"] is None


def test_install_success_records_ownership(worker, monkeypatch):
    store, cli, root = worker
    def install(command):
        expected = (["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                     "irm https://claude.ai/install.ps1 | iex"] if sys.platform == "win32" else
                    ["bash", "-c", "set -o pipefail; curl -fsSL https://claude.ai/install.sh | bash"])
        assert command == expected
        cli.installed, cli.binary_path, cli.cli_version = True, str(ci._launcher()), "2.2.1"
        return 0, "installed", False
    monkeypatch.setattr(ci, "_run_command", install)
    _job()
    ci._execute("install")
    result = ci.payload()
    assert result["job"]["state"] == "succeeded"
    assert result["installed_by_app"] and result["can_remove"]
    assert result["version"] == "2.2.1"
    assert "keep_installed" not in result
    assert result["check_updates"] is True and result["latest_version"] is None


@pytest.mark.parametrize("timeout", [False, True])
def test_install_failure_reports_output(worker, monkeypatch, timeout):
    monkeypatch.setattr(ci, "_run_command", lambda cmd: (1, "curl: Could not resolve host", timeout))
    _job()
    ci._execute("install")
    job = ci.payload()["job"]
    assert job["state"] == "failed" and job["finished_at"]
    assert job["output_tail"] == "curl: Could not resolve host"
    assert ("10 minutes" if timeout else "network connection") in job["message"]


def test_concurrent_start_returns_current_job(worker, monkeypatch):
    started, finish = threading.Event(), threading.Event()
    threads = []
    real_thread = threading.Thread
    def thread(**kw):
        t = real_thread(**kw)
        threads.append(t)
        return t
    def install(command):
        started.set()
        assert finish.wait(5)
        return 1, "offline", False
    monkeypatch.setattr(ci.threading, "Thread", thread)
    monkeypatch.setattr(ci, "_run_command", install)
    try:
        first = ci.start("install")
        assert started.wait(5)
        second = ci.start("update")
        assert second["job"] == first["job"] and len(threads) == 1
    finally:
        finish.set()
        for t in threads:
            t.join(5)
    assert ci.payload()["job"]["state"] == "failed"


def _parent_env(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:secret@db/app")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy:3128")
    monkeypatch.setenv("no_proxy", "localhost")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/claude-config")


def test_installer_env_is_an_allow_list(worker, monkeypatch):
    _parent_env(monkeypatch)
    seen = {}

    def popen(command, **kw):
        seen.update(kw["env"])
        return Mock(pid=123, returncode=0)
    monkeypatch.setattr(ci.subprocess, "Popen", popen)
    assert ci._run_command(["bash"])[0] == 0
    assert set(seen) <= ci._INSTALLER_ENV
    for secret in ("ANTHROPIC_API_KEY", "DATABASE_URL", "GITHUB_TOKEN"):
        assert secret not in seen
    assert seen["HTTPS_PROXY"] == "http://proxy:3128"
    assert {key.upper(): value for key, value in seen.items()}["NO_PROXY"] == "localhost"
    assert seen["CLAUDE_CONFIG_DIR"] == "/claude-config"
    assert seen["PATH"].split(ci.os.pathsep)[0] == str(ci._launcher().parent)


def test_installer_env_keeps_npm_settings_and_mtls_but_not_npm_tokens(worker, monkeypatch):
    """#1890: an npm-installed CLI's update needs its env-only npm prefix,
    registry and CA; a proxy may need Claude Code's client certificate. npm
    auth tokens stay out."""
    passed = {
        "NPM_CONFIG_PREFIX": "/opt/npm-global", "npm_config_registry": "https://npm.example/",
        "npm_config_cafile": "/etc/ssl/corp.pem", "NPM_CONFIG_STRICT_SSL": "true",
        "CLAUDE_CODE_CLIENT_CERT": "/certs/client.pem", "CLAUDE_CODE_CLIENT_KEY": "/certs/client.key",
        "CLAUDE_CODE_CLIENT_KEY_PASSPHRASE": "pass",
    }
    withheld = {"npm_config__authToken": "npm_secret", "NPM_CONFIG__AUTH": "dXNlcjpwYXNz",
                "npm_config_//npm.example/:_authToken": "npm_secret2"}
    for name, value in {**passed, **withheld}.items():
        monkeypatch.setenv(name, value)
    seen = {}

    def popen(command, **kw):
        seen.update(kw["env"])
        return Mock(pid=123, returncode=0)
    monkeypatch.setattr(ci.subprocess, "Popen", popen)
    assert ci._run_command(["bash"])[0] == 0
    fold = str.upper if ci.sys.platform == "win32" else str
    names = {fold(k) for k in seen}
    assert {fold(k) for k in passed} <= names
    assert not {fold(k) for k in withheld} & names
    assert "npm_secret" not in seen.values()


@pytest.mark.skipif(ci.sys.platform == "win32", reason="POSIX env(1)")
def test_installer_child_sees_only_allowed_variables(worker, monkeypatch):
    import shutil
    _parent_env(monkeypatch)
    code, tail, _ = ci._run_command([shutil.which("env")])
    names = {line.split("=", 1)[0] for line in tail.splitlines() if "=" in line}
    assert code == 0 and "HTTPS_PROXY" in names
    assert names <= ci._INSTALLER_ENV
    assert "sk-ant-secret" not in tail and "user:secret" not in tail


def test_hard_timeout_kills_process_group(worker, monkeypatch):
    proc = Mock(pid=123, returncode=-9)
    proc.wait.side_effect = [subprocess.TimeoutExpired("installer", 600), None]
    monkeypatch.setattr(ci.subprocess, "Popen", lambda *a, **kw: proc)
    kill = Mock()
    from types import SimpleNamespace
    monkeypatch.setattr(ci, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(ci.os, "killpg", kill, raising=False)
    monkeypatch.setattr(ci.signal, "SIGKILL", 9, raising=False)
    code, tail, timeout = ci._run_command(["bash"])
    assert timeout and code == -9
    assert proc.wait.call_args_list[0].kwargs == {"timeout": 600}
    kill.assert_called_once_with(123, ci.signal.SIGKILL)


def test_windows_install_uses_official_powershell(worker, monkeypatch):
    monkeypatch.setattr(ci.sys, "platform", "win32")
    def install(command):
        assert command == ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                           "irm https://claude.ai/install.ps1 | iex"]
        return 1, "offline", False
    monkeypatch.setattr(ci, "_run_command", install)
    _job()
    ci._execute("install")
    assert ci._job["state"] == "failed" and ci._job["output_tail"] == "offline"


def test_real_command_capture_is_bounded(worker):
    code, tail, timeout = ci._run_command([
        sys.executable, "-c", "import sys; [print(f'line-{i}') for i in range(1, 101)]; sys.exit(3)",
    ])
    assert code == 3 and not timeout
    assert len(tail.splitlines()) == 40 and tail.splitlines()[-1] == "line-100"


def test_docker_native_install_counts_as_owned(worker, monkeypatch):
    _, cli, _ = worker
    cli.installed, cli.binary_path = True, str(ci._launcher())
    monkeypatch.setattr(ci, "platform", lambda: "docker")
    assert ci.payload()["installed_by_app"] and ci.payload()["can_remove"]


def test_remove_refused_for_manual_install(worker):
    _, cli, _ = worker
    cli.installed, cli.binary_path = True, str(ci._launcher())
    with pytest.raises(ValueError, match="not installed by AI Shuffle"):
        ci.start("remove")
    assert ci._job is None


def test_remove_preserves_claude_home(worker):
    store, cli, root = worker
    versions = root / ".local" / "share" / "claude" / "versions"
    versions.mkdir(parents=True)
    (versions / "2.2.1").write_text("native")
    ci._launcher().parent.mkdir(parents=True)
    if sys.platform == "win32":
        ci._launcher().write_bytes((versions / "2.2.1").read_bytes())
    else:
        ci._launcher().symlink_to(versions / "2.2.1")
    home = root / ".claude"
    home.mkdir()
    (home / "settings.json").write_text("owner settings")
    cli.installed, cli.binary_path = True, str(ci._launcher())
    store[ci._key("claude_cli_installed_by_app")] = "1"
    _job("remove")
    ci._execute("remove")
    assert ci._job["state"] == "succeeded"
    assert not ci._launcher().exists() and not versions.parent.exists()
    assert (home / "settings.json").read_text() == "owner settings"


def test_update_uses_cli_updater_and_reports_versions(worker, monkeypatch):
    _, cli, _ = worker
    cli.installed, cli.binary_path, cli.cli_version = True, str(ci._launcher()), "2.1.1"
    def update(command):
        assert command == [cli.binary_path, "update"]
        cli.cli_version = "2.2.1"
        return 0, "updated", False
    # Probes return separate observations, like the real probe.
    before = cs.ClaudeCliStatus(installed=True, binary_path=cli.binary_path, cli_version="2.1.1")
    probes = iter([before, cli])
    monkeypatch.setattr(cs, "get_status", lambda **kw: next(probes))
    monkeypatch.setattr(ci, "_run_command", update)
    _job("update")
    ci._execute("update")
    assert ci._job["state"] == "succeeded"
    assert "2.1.1 to 2.2.1" in ci._job["message"]


def test_container_start_has_no_installer():
    """The image's entrypoint and the files it ships no longer install Claude Code."""
    root = Path(__file__).resolve().parents[2]
    entrypoint = (root / "entrypoint.sh").read_text(encoding="utf-8")
    for gone in ("claude_cli_keep_installed", "install-claude-cli", "claude.ai/install", ci.LEGACY_MARKER):
        assert gone not in entrypoint
    assert not (root / "docker" / "install-claude-cli.sh").exists()
    assert "install-claude-cli" not in (root / "Dockerfile").read_text(encoding="utf-8")


# --- Review fixes (#1865) ---------------------------------------------------

def test_cli_settings_stay_on_this_worker():
    from app.network.policy import is_shared_setting_key
    for name in ("claude_cli_keep_installed", "claude_cli_installed_by_app", "claude_cli_startup_check_at",
                 "claude_cli_update_check", "antigravity_cli_update_check", "antigravity_cli_startup_check_at"):
        assert not is_shared_setting_key(name + ":same-instance-id")


def _owned_install(store, cli, root):
    versions = root / ".local" / "share" / "claude" / "versions"
    versions.mkdir(parents=True)
    binary = versions / "2.2.1"
    ci._launcher().parent.mkdir(parents=True)
    if sys.platform == "win32":
        from distlib.scripts import ScriptMaker

        binary.write_text("native fixture")
        maker = ScriptMaker(None, str(ci._launcher().parent))
        maker.executable = sys.executable
        maker.variants = {""}
        maker.script_template = "import time; time.sleep(30)\n"
        maker.make("claude = unused:main")
    else:
        # Bash is already required on POSIX. Unlike Python it does not
        # need a private runtime beside its executable; unlike a coreutils
        # applet it can run under the launcher name.
        binary.write_bytes(Path("/bin/bash").read_bytes())
        binary.chmod(0o755)
        ci._launcher().symlink_to(binary)
    cli.installed, cli.binary_path = True, str(ci._launcher())
    store[ci._key("claude_cli_installed_by_app")] = "1"
    return binary


def test_remove_refused_while_cli_runs(worker):
    store, cli, root = worker
    _owned_install(store, cli, root)
    # A shell builtin keeps the installed executable itself blocked until
    # cleanup, with no sleep utility or short-lived child to race.
    running = subprocess.Popen([str(ci._launcher()), "-c", "read -r line"], stdin=subprocess.PIPE)
    try:
        assert ci._in_use() == [running.pid]
        with pytest.raises(ValueError, match="Claude Code is running"):
            ci.start("remove")
        _job("remove")
        ci._execute("remove")
        assert ci._job["state"] == "failed" and "Claude Code is running" in ci._job["message"]
        assert ci._launcher().exists()
    finally:
        import psutil
        children = psutil.Process(running.pid).children(recursive=True)
        for child in children:
            child.kill()
        running.kill()
        running.wait()
        running.stdin.close()
        psutil.wait_procs(children, timeout=5)
    assert ci._in_use() == []


def test_installer_gets_no_stdin(worker):
    code, tail, _ = ci._run_command([
        sys.executable, "-c", "import sys; print('got' if sys.stdin.readline() else 'eof')",
    ])
    assert code == 0 and tail == "eof"
    assert not (ci.settings.effective_data_dir / ci.JOB_RECORD).exists()


def test_job_orphaned_by_restart_blocks_then_is_stopped(worker, monkeypatch):
    ci.settings.effective_data_dir.mkdir(parents=True)
    orphan = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                              start_new_session=sys.platform != "win32")
    try:
        ci._record_job(orphan.pid)
        with pytest.raises(ValueError, match="before the worker restarted"):
            ci.start("install")
        assert ci._job is None
        monkeypatch.setattr(ci, "TIMEOUT_SECONDS", 0)
        monkeypatch.setattr(ci, "_run_command", lambda cmd: (1, "offline", False))
        ci.start("install")
        exit_code = orphan.wait(5)
        # Windows termination codes do not encode POSIX signals.
        assert exit_code != 0 if sys.platform == "win32" else exit_code == -9
    finally:
        if orphan.poll() is None:
            orphan.kill()
        orphan.wait(timeout=5)
    for t in threading.enumerate():
        if t.name == "claude-cli-install":
            t.join(5)
    assert not (ci.settings.effective_data_dir / ci.JOB_RECORD).exists()


def test_a_failing_marker_write_never_stops_boot(worker, monkeypatch, caplog):
    startup, start = _startup(monkeypatch)
    monkeypatch.setattr(ci, "retire_keep_installed", Mock(side_effect=OSError("read-only data dir")))
    startup.check_prerequisites()
    start.assert_not_called()
    assert "Could not turn off the start-time Claude Code install" in caplog.text


# --- Update check when the worker starts --------------------------------------

@pytest.fixture
def asking(monkeypatch):
    """Let the check run (the suite normally never asks a vendor) on this thread."""
    monkeypatch.delenv("TESTING", raising=False)

    class Inline:
        def __init__(self, target, args=(), **kw):
            self.run = lambda: target(*args)

        def start(self):
            self.run()
    monkeypatch.setattr(ci.threading, "Thread", Inline)
    start = Mock()
    monkeypatch.setattr(ci, "start", start)
    return start


def test_startup_check_reports_a_newer_version_and_installs_nothing(worker, asking, monkeypatch):
    _, cli, _ = worker
    cli.installed, cli.cli_version, cli.binary_path = True, "2.1.323", str(ci._launcher())
    run = Mock()
    monkeypatch.setattr(ci, "_run_command", run)
    monkeypatch.setattr(ci, "latest_release", lambda: "2.1.330")
    assert ci.check_on_startup(cli) is True
    assert ci.payload()["latest_version"] == "2.1.330"
    assert cuc.available() == [{"provider": "claude", "name": "Claude Code",
                                "installed_version": "2.1.323", "latest_version": "2.1.330"}]
    asking.assert_not_called()
    run.assert_not_called()
    assert ci.payload()["job"] is None


@pytest.mark.parametrize("latest", ["2.1.323", "2.1.300", "2.2.0-beta.1", ""])
def test_startup_check_stays_quiet_without_a_newer_plain_version(worker, asking, monkeypatch, latest):
    _, cli, _ = worker
    cli.installed, cli.cli_version = True, "2.1.323"
    monkeypatch.setattr(ci, "latest_release", lambda: latest)
    assert ci.check_on_startup(cli) is True
    assert ci.payload()["latest_version"] is None and cuc.available() == []


def test_startup_check_survives_an_offline_worker(worker, asking, monkeypatch, caplog):
    _, cli, _ = worker
    cli.installed, cli.cli_version = True, "2.1.323"
    monkeypatch.setattr(ci, "latest_release", Mock(side_effect=OSError("no route to host")))
    with caplog.at_level("INFO"):
        assert ci.check_on_startup(cli) is True
    assert "could not check for an update" in caplog.text
    assert cuc.available() == []


@pytest.mark.parametrize("installed,version,testing,switch", [
    (False, None, False, None),       # nothing installed
    (True, None, False, None),        # installed, version unknown
    (True, "2.1.323", True, None),    # the test suite
    (True, "2.1.323", False, "0"),    # the owner turned the check off
])
def test_startup_check_is_skipped(worker, asking, monkeypatch, installed, version, testing, switch):
    store, cli, _ = worker
    cli.installed, cli.cli_version = installed, version
    if testing:
        monkeypatch.setenv("TESTING", "1")
    if switch:
        store[cuc._key(cuc.CLAUDE)] = switch
    release = Mock(return_value="9.9.9")
    monkeypatch.setattr(ci, "latest_release", release)
    assert ci.check_on_startup(cli) is False
    release.assert_not_called()
    assert cuc.available() == []


def test_the_dev_loop_asks_no_vendor_about_its_scripted_cli(worker, asking, monkeypatch):
    # Its fake claude has a made-up version: a real answer would put a banner
    # over every UI journey.
    _, cli, _ = worker
    cli.installed, cli.cli_version = True, "2.1.323"
    release = Mock(return_value="9.9.9")
    monkeypatch.setattr(ci, "latest_release", release)
    monkeypatch.setenv("SHUFFLE_DEV_FAKE_AI_URL", "http://127.0.0.1:4010")
    monkeypatch.setattr("app.preview_run.disposable_preview_data", lambda: True)
    assert cuc.suppressed() is True
    assert ci.check_on_startup(cli) is False
    release.assert_not_called()
    # The same variable on a real install changes nothing.
    monkeypatch.setattr("app.preview_run.disposable_preview_data", lambda: False)
    assert cuc.suppressed() is False


def test_the_switch_is_on_by_default_and_turning_it_off_drops_the_notice(worker, asking, monkeypatch):
    store, cli, _ = worker
    cli.installed, cli.cli_version = True, "2.1.323"
    monkeypatch.setattr(ci, "latest_release", lambda: "2.1.330")
    assert ci.payload()["check_updates"] is True
    ci.check_on_startup(cli)
    assert cuc.available()
    cuc.set_enabled(cuc.CLAUDE, False)
    assert store[cuc._key(cuc.CLAUDE)] == "0"
    assert ci.payload()["check_updates"] is False and ci.payload()["latest_version"] is None
    assert cuc.available() == []
    # The Antigravity switch is its own row.
    assert cuc.enabled(cuc.ANTIGRAVITY) is True
    cuc.set_enabled(cuc.CLAUDE, True)
    assert ci.payload()["check_updates"] is True


def test_notice_goes_away_once_the_install_catches_up(worker, asking, monkeypatch):
    """Updated in the app, by the CLI's own updater, or by hand."""
    _, cli, _ = worker
    cli.installed, cli.cli_version, cli.binary_path = True, "2.1.323", str(ci._launcher())
    monkeypatch.setattr(ci, "latest_release", lambda: "2.1.330")
    ci.check_on_startup(cli)
    assert cuc.available()
    cli.cli_version = "2.1.330"
    assert cuc.available() == [] and ci.payload()["latest_version"] is None

    cli.cli_version = "2.1.323"
    ci.check_on_startup(cli)
    assert cuc.available()

    def update(command):
        cli.cli_version = "2.1.329"   # an update that lands on any version clears the old answer
        return 0, "updated", False
    monkeypatch.setattr(ci, "_run_command", update)
    _job("update")
    ci._execute("update")
    assert ci._job["state"] == "succeeded" and cuc.available() == []


def test_a_removed_cli_has_no_notice(worker, asking, monkeypatch):
    _, cli, _ = worker
    cli.installed, cli.cli_version = True, "2.1.323"
    monkeypatch.setattr(ci, "latest_release", lambda: "2.1.330")
    ci.check_on_startup(cli)
    cli.installed, cli.cli_version = False, None
    assert cuc.available() == [] and ci.payload()["latest_version"] is None


@pytest.mark.parametrize("chosen,channel", [("stable", "stable"), ("latest", "latest"), (None, "latest"), ("nightly", "latest")])
def test_check_follows_the_channel_chosen_in_claude_code(worker, monkeypatch, chosen, channel):
    import json
    _, _, root = worker
    home = root / "claude-home"
    home.mkdir()
    if chosen:
        (home / "settings.json").write_text(json.dumps({"autoUpdatesChannel": chosen}))
    monkeypatch.setattr(ci, "_installer_env", lambda: {"CLAUDE_CONFIG_DIR": str(home)})
    asked = []

    def get(url, **kw):
        asked.append((url, kw))
        return Mock(text="2.1.330\n", raise_for_status=lambda: Mock(text="2.1.330\n"))
    import httpx
    monkeypatch.setattr(httpx, "get", get)
    assert ci.latest_release() == "2.1.330"
    assert asked[0][0] == ci.RELEASES_URL + channel
    assert asked[0][1]["timeout"] == ci.CHECK_TIMEOUT_SECONDS


def test_a_channel_that_names_no_version_is_refused(worker, monkeypatch):
    import httpx
    monkeypatch.setattr(ci, "_installer_env", lambda: {})
    monkeypatch.setattr(httpx, "get", lambda url, **kw: Mock(raise_for_status=lambda: Mock(text="<html>not found</html>")))
    with pytest.raises(ValueError, match="did not name a version"):
        ci.latest_release()


def test_worker_start_checks_for_an_update_and_survives_a_failure(worker, monkeypatch, caplog):
    _, cli, _ = worker
    cli.installed = True
    startup, start = _startup(monkeypatch)
    check = Mock(side_effect=[True, RuntimeError("database unavailable")])
    monkeypatch.setattr(ci, "check_on_startup", check)
    with caplog.at_level("INFO"):
        startup.check_prerequisites()
        startup.check_prerequisites()
    assert check.call_count == 2 and check.call_args.args == (cli,)
    assert "checking for an update" in caplog.text
    assert "Could not check for a Claude Code update" in caplog.text
    start.assert_not_called()
