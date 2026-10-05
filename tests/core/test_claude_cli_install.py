"""Native CLI jobs use only official commands and preserve the owner's home."""
import subprocess
import threading
from pathlib import Path
from unittest.mock import Mock

import pytest

from app.core import claude_cli_install as ci, claude_cli_status as cs


@pytest.fixture
def worker(monkeypatch, tmp_path):
    store = {}
    monkeypatch.setattr(ci.database, "get_system_setting", store.get)
    monkeypatch.setattr(ci.database, "set_system_setting", lambda k, v: store.update({k: v}))
    monkeypatch.setattr(ci.settings, "data_dir", tmp_path / "data")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(ci, "platform", lambda: "desktop")
    monkeypatch.setattr(ci, "_job", None)
    monkeypatch.setattr(ci.claude_auth_mode, "has_stored_api_key", lambda: False)
    status = cs.ClaudeCliStatus(installed=False)
    monkeypatch.setattr(cs, "get_status", lambda **kw: status)
    return store, status, tmp_path


@pytest.mark.parametrize("installed,signed_in,key,legacy,expected", [
    (True, False, False, False, True),
    (False, True, False, False, True),
    (False, False, True, False, True),
    (False, False, False, True, True),
    (False, False, False, False, False),
])
def test_migration_once(worker, monkeypatch, installed, signed_in, key, legacy, expected):
    store, cli, root = worker
    cli.installed, cli.signed_in = installed, signed_in
    monkeypatch.setattr(ci.claude_auth_mode, "has_stored_api_key", lambda: key)
    if legacy:
        store[ci.database.CLAUDE_RELOGIN_NEEDED_MARKER] = "1"
    assert ci.ensure_recorded(cli) is expected
    assert (root / "data" / ci.MARKER).read_text() == ("1\n" if expected else "0\n")
    cli.installed, cli.signed_in = not installed, not signed_in
    assert ci.ensure_recorded(cli) is expected
    ci.set_keep_installed(not expected)
    assert ci.ensure_recorded(cli) is not expected


def _job(kind="install"):
    ci._job = {"kind": kind, "state": "running", "started_at": ci._now(), "finished_at": None,
               "message": None, "output_tail": None}


@pytest.mark.parametrize("keep,expected", [(False, False), (True, True)])
def test_native_worker_start_installs_only_when_enabled(worker, monkeypatch, keep, expected):
    from app.core import startup, credential_gate, claude_relogin_notice
    store, _, _ = worker
    store[ci._key("claude_cli_keep_installed")] = "1" if keep else "0"
    start = Mock()
    monkeypatch.setattr(ci, "start", start)
    monkeypatch.setattr(ci.claude_auth_mode, "ensure_mode_recorded", lambda signed_in: "subscription")
    monkeypatch.setattr(claude_relogin_notice, "mark_if_needed", lambda: None)
    monkeypatch.setattr(credential_gate, "record_startup", lambda cli: None)
    monkeypatch.setattr(startup.auth_service, "is_setup_required", lambda: True)
    monkeypatch.setattr(startup, "_check_browser_mcp_prereqs", lambda: None)
    monkeypatch.setattr(startup, "_warn_removed_env_vars", lambda: None)
    startup.check_prerequisites()
    if expected:
        start.assert_called_once_with("install")
    else:
        start.assert_not_called()


def test_install_success_sets_marker_and_ownership(worker, monkeypatch):
    store, cli, root = worker
    def install(command):
        assert command == ["bash", "-c", "set -o pipefail; curl -fsSL https://claude.ai/install.sh | bash"]
        cli.installed, cli.binary_path, cli.cli_version = True, str(ci._launcher()), "2.2.1"
        return 0, "installed", False
    monkeypatch.setattr(ci, "_run_command", install)
    _job()
    ci._execute("install")
    result = ci.payload()
    assert result["job"]["state"] == "succeeded"
    assert result["keep_installed"] and result["installed_by_app"] and result["can_remove"]
    assert result["version"] == "2.2.1"


@pytest.mark.parametrize("timeout", [False, True])
def test_install_failure_reports_output(worker, monkeypatch, timeout):
    monkeypatch.setattr(ci, "_run_command", lambda cmd: (1, "curl: Could not resolve host", timeout))
    _job()
    ci._execute("install")
    job = ci.payload()["job"]
    assert job["state"] == "failed" and job["finished_at"]
    assert job["output_tail"] == "curl: Could not resolve host"
    assert ("10 minutes" if timeout else "network connection") in job["message"]
    assert not ci.keep_installed()


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
    assert seen["HTTPS_PROXY"] == "http://proxy:3128" and seen["no_proxy"] == "localhost"
    assert seen["CLAUDE_CONFIG_DIR"] == "/claude-config"
    assert seen["PATH"].split(ci.os.pathsep)[0] == str(ci._launcher().parent)


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
    monkeypatch.setattr(ci.os, "killpg", kill)
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
    code, tail, timeout = ci._run_command(["bash", "-c", "for i in {1..100}; do echo line-$i; done; exit 3"])
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
    ci._launcher().symlink_to(versions / "2.2.1")
    home = root / ".claude"
    home.mkdir()
    (home / "settings.json").write_text("owner settings")
    cli.installed, cli.binary_path = True, str(ci._launcher())
    store[ci._key("claude_cli_installed_by_app")] = "1"
    ci.set_keep_installed(True)
    _job("remove")
    ci._execute("remove")
    assert ci._job["state"] == "succeeded"
    assert not ci._launcher().exists() and not versions.parent.exists()
    assert (home / "settings.json").read_text() == "owner settings"
    assert not ci.keep_installed()


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


@pytest.mark.parametrize("marker,has_data,has_cli,expected", [
    (None, False, False, False), ("1", False, False, True),
    ("0", True, True, False), (None, True, False, True), (None, False, True, True),
])
def test_entrypoint_decision(tmp_path, marker, has_data, has_cli, expected):
    entrypoint = (Path(__file__).resolve().parents[2] / "entrypoint.sh").read_text()
    decision = entrypoint[entrypoint.index("claude_cli_keep_installed() {"):entrypoint.index("# Direct --user")]
    if marker is not None:
        (tmp_path / ci.MARKER).write_text(marker + "\n")
    (tmp_path / ".claude").mkdir()
    if has_data:
        (tmp_path / ".claude" / "settings.json").write_text("existing")
    command = "command() { if [ \"$2\" = claude ]; then return " + ("0" if has_cli else "1") + "; fi; builtin command \"$@\"; }\n"
    script = command + decision + "\nif claude_cli_keep_installed; then echo INSTALL; fi"
    result = subprocess.run(["bash", "-c", script], env={**ci.os.environ, "HOME": str(tmp_path), "DATA_DIR": str(tmp_path)}, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert ("INSTALL" in result.stdout) is expected
    assert entrypoint.count("if claude_cli_keep_installed; then") == 1
    assert "if HOME=/home/appuser claude_cli_keep_installed; then" in entrypoint


# --- Review fixes (#1865) ---------------------------------------------------

def test_cli_settings_stay_on_this_worker():
    from app.network.policy import is_shared_setting_key
    for name in ("claude_cli_keep_installed", "claude_cli_installed_by_app"):
        assert not is_shared_setting_key(name + ":same-instance-id")


@pytest.mark.parametrize("where,expected", [("docker", True), ("desktop", False)])
def test_migration_counts_claude_home_on_docker_only(worker, monkeypatch, where, expected):
    _, cli, root = worker
    monkeypatch.setattr(ci, "platform", lambda: where)
    (root / ".claude").mkdir()
    (root / ".claude" / ".credentials.json").write_text("{}")
    assert ci.ensure_recorded(cli) is expected


def _owned_install(store, cli, root):
    versions = root / ".local" / "share" / "claude" / "versions"
    versions.mkdir(parents=True)
    binary = versions / "2.2.1"
    binary.write_bytes(Path("/bin/sleep").read_bytes())
    binary.chmod(0o755)
    ci._launcher().parent.mkdir(parents=True)
    ci._launcher().symlink_to(binary)
    cli.installed, cli.binary_path = True, str(ci._launcher())
    store[ci._key("claude_cli_installed_by_app")] = "1"
    ci.set_keep_installed(True)
    return binary


def test_remove_refused_while_cli_runs(worker):
    store, cli, root = worker
    _owned_install(store, cli, root)
    running = subprocess.Popen([str(ci._launcher()), "30"])
    try:
        assert ci._in_use() == [running.pid]
        with pytest.raises(ValueError, match="Claude Code is running"):
            ci.start("remove")
        _job("remove")
        ci._execute("remove")
        assert ci._job["state"] == "failed" and "Claude Code is running" in ci._job["message"]
        assert ci._launcher().exists() and ci.keep_installed()
    finally:
        running.kill()
        running.wait()
    assert ci._in_use() == []


def test_installer_gets_no_stdin(worker):
    code, tail, _ = ci._run_command(["bash", "-c", "if read -r line; then echo got; else echo eof; fi"])
    assert code == 0 and tail == "eof"
    assert not (ci.settings.effective_data_dir / ci.JOB_RECORD).exists()


def test_job_orphaned_by_restart_blocks_then_is_stopped(worker, monkeypatch):
    ci.settings.effective_data_dir.mkdir(parents=True)
    orphan = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        ci._record_job(orphan.pid)
        with pytest.raises(ValueError, match="before the worker restarted"):
            ci.start("install")
        assert ci._job is None
        monkeypatch.setattr(ci, "TIMEOUT_SECONDS", 0)
        monkeypatch.setattr(ci, "_run_command", lambda cmd: (1, "offline", False))
        ci.start("install")
        assert orphan.wait(5) == -9
    finally:
        if orphan.poll() is None:
            orphan.kill()
    for t in threading.enumerate():
        if t.name == "claude-cli-install":
            t.join(5)
    assert not (ci.settings.effective_data_dir / ci.JOB_RECORD).exists()


def _startup(monkeypatch):
    from app.core import startup, credential_gate, claude_relogin_notice
    start = Mock()
    monkeypatch.setattr(ci, "start", start)
    monkeypatch.setattr(ci.claude_auth_mode, "ensure_mode_recorded", lambda signed_in: "subscription")
    monkeypatch.setattr(claude_relogin_notice, "mark_if_needed", lambda: None)
    monkeypatch.setattr(credential_gate, "record_startup", lambda cli: None)
    monkeypatch.setattr(startup.auth_service, "is_setup_required", lambda: True)
    monkeypatch.setattr(startup, "_check_browser_mcp_prereqs", lambda: None)
    monkeypatch.setattr(startup, "_warn_removed_env_vars", lambda: None)
    monkeypatch.setattr(startup, "settings", ci.settings)
    return startup, start


@pytest.mark.parametrize("marker,expected", [(False, True), (True, False)])
def test_docker_first_start_installs_when_migration_turns_on(worker, monkeypatch, marker, expected):
    store, _, root = worker
    monkeypatch.setattr(ci, "platform", lambda: "docker")
    monkeypatch.setattr(ci.claude_auth_mode, "has_stored_api_key", lambda: True)
    if marker:
        ci.set_keep_installed(True)
    startup, start = _startup(monkeypatch)
    startup.check_prerequisites()
    assert ci.keep_installed()
    assert start.called is expected


def test_setting_failure_never_stops_boot(worker, monkeypatch):
    startup, start = _startup(monkeypatch)
    monkeypatch.setattr(ci, "ensure_recorded", Mock(side_effect=OSError("read-only data dir")))
    startup.check_prerequisites()
    start.assert_not_called()
