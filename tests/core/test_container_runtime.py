"""Container lifecycle guards. No application startup or real configured database."""
import importlib.util
import json
import os
import sys
import threading
from pathlib import Path
from unittest.mock import Mock

import pytest

pytest.importorskip("fcntl", reason="The Linux container supervisor requires POSIX file locking")

# The installed Docker SDK is also named docker; load our standalone supervisor
# by its source path rather than depending on namespace-package resolution.
_spec = importlib.util.spec_from_file_location(
    "shuffle_container_runtime", Path(__file__).resolve().parents[2] / "docker" / "container_runtime.py",
)
assert _spec is not None and _spec.loader is not None
runtime = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(runtime)
from app.core.config import Settings


@pytest.fixture
def settings_factory(monkeypatch, tmp_path):
    for name in list(os.environ):
        if name.startswith("POSTGRES_") or name in {"DATABASE_URL", "DATABASE_MODE", "AI_SHUFFLE_ENV_FILE"}:
            monkeypatch.delenv(name)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    return lambda **kw: Settings(_env_file=None, **kw)




@pytest.mark.parametrize("action", ["run", "dump", "restore", "remap-home"])
def test_root_phase_imports_only_image_bootstrap_and_installed_dependencies(tmp_path, action):
    """Stop at the real privilege boundary in a fresh, isolated interpreter."""
    import subprocess
    project = Path(__file__).resolve().parents[2]
    script = """
import importlib.util, json, pathlib, sys
spec = importlib.util.spec_from_file_location("rt", sys.argv[1])
rt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rt)
sys.path.insert(0, str(pathlib.Path(sys.argv[1]).resolve().parents[1]))
def boundary(*args, **kwargs):
    rt.isolation()
    origins = {name: str(pathlib.Path(mod.__file__).resolve())
               for name, mod in sys.modules.copy().items() if getattr(mod, "__file__", None)}
    print(json.dumps(origins))
    raise SystemExit(0)
rt.drop_privileges = boundary
rt.remap_home = boundary
rt.os.geteuid = lambda: 0
rt.main(sys.argv[2:])
"""
    env = dict(os.environ, DATA_DIR=str(tmp_path), WORKSPACE_DIR=str(tmp_path / "workspace"),
               AI_SHUFFLE_ENV_FILE="/dev/null")
    args = [action]
    if action == "remap-home":
        for name in ("old-uid", "old-gid", "new-uid", "new-gid"):
            args += ["--" + name, "41001"]
    elif action != "run":
        args += ["--file", "-"]
    result = subprocess.run([sys.executable, "-I", "-c", script,
                             str(project / "docker/container_runtime.py"), *args],
                            cwd=tmp_path, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    origins = json.loads(result.stdout)
    assert not any(name == "app" or name.startswith("app.") for name in origins), origins
    for origin in origins.values():
        path = Path(origin)
        assert not path.is_relative_to(tmp_path), origin
        if path.is_relative_to(project):
            assert path.parent == project / "docker", origin
        else:
            assert any(path.is_relative_to(Path(prefix)) for prefix in (sys.prefix, sys.base_prefix)), origin
            prefix = next(Path(prefix) for prefix in (sys.prefix, sys.base_prefix)
                          if path.is_relative_to(Path(prefix)))
            info = path.stat()
            assert info.st_uid == prefix.stat().st_uid and not info.st_mode & 0o022, origin


@pytest.mark.parametrize("local_mode", ["false", "true"])
@pytest.mark.parametrize("source", ["defaults", "file", "environment"])
def test_startup_paths_match_application_settings(settings_factory, tmp_path, monkeypatch, local_mode, source):
    monkeypatch.setenv("LOCAL_MODE", local_mode)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("DATA_DIR", raising=False)
    monkeypatch.delenv("WORKSPACE_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    if source != "defaults":
        (tmp_path / ".env").write_text('data_dir="./file data"\nworkspace_dir=./file-workspace\n')
    if source == "environment":
        monkeypatch.setenv("DATA_DIR", str(tmp_path / "env-data"))
        monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path / "env-workspace"))
    startup, app = runtime.load_startup_settings(), runtime.load_settings()
    assert startup.effective_data_dir == app.effective_data_dir
    assert startup.effective_workspace_dir == app.effective_workspace_dir


def test_startup_paths_honor_referenced_env_file(settings_factory, tmp_path, monkeypatch):
    config = tmp_path / "referenced-config"
    config.write_text('WORKSPACE_DIR="./quoted workspace"\n')
    monkeypatch.setenv("AI_SHUFFLE_ENV_FILE", str(config))
    assert runtime.load_startup_settings().effective_workspace_dir == runtime.load_settings().effective_workspace_dir


def test_fresh_defaults_to_embedded(settings_factory):
    assert runtime.Database(settings_factory()).mode == "embedded"


@pytest.mark.parametrize("name,value", [("POSTGRES_PASSWORD", "synthetic"), ("POSTGRES_PORT", "garbage"), ("POSTGRES_HOST", "external.invalid"), ("DATABASE_MODE", "external"), ("DATABASE_URL", "postgresql://never:never@external.invalid/prod")])
def test_retired_environment_cannot_select_external(settings_factory, monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    cfg = settings_factory()
    assert runtime.Database(cfg).mode == "embedded"
    assert not any(k.startswith(("postgres_", "database_")) for k in cfg.model_fields)


def test_env_file_database_settings_are_ignored(settings_factory, tmp_path, monkeypatch):
    envfile = tmp_path / "synthetic-config"
    envfile.write_text("POSTGRES_PASSWORD=from-file\nDATABASE_URL=postgres://u:never@external.invalid/name\nDATABASE_MODE=external\n")
    monkeypatch.setenv("AI_SHUFFLE_ENV_FILE", str(envfile))
    cfg = runtime.load_settings()
    assert runtime.Database(cfg).mode == "embedded"
    with pytest.raises(RuntimeError, match="supervisor"):
        cfg.get_database_url()


def test_legacy_and_missing_external_config_fail_closed(settings_factory, tmp_path):
    (tmp_path / "identity.json").write_text("retained")
    with pytest.raises(runtime.RuntimeFailure, match="unrecognized"):
        runtime.Database(settings_factory()).open()
    (tmp_path / "identity.json").unlink()
    runtime.write_private(tmp_path / "database-mode.json", json.dumps({"version": 1, "mode": "external"}))
    with pytest.raises(runtime.RuntimeFailure, match="external"):
        runtime.Database(settings_factory()).open()
    assert not (tmp_path / "postgres").exists()


def test_sqlite_rejected_even_with_external_config(settings_factory, tmp_path):
    legacy = tmp_path / "db.sqlite"
    legacy.write_bytes(b"legacy sentinel")
    with pytest.raises(runtime.RuntimeFailure, match="SQLite"):
        runtime.Database(settings_factory(postgres_password="synthetic")).open()
    assert legacy.read_bytes() == b"legacy sentinel"
    assert not (tmp_path / "postgres").exists()


@pytest.mark.parametrize("working_directory", ["project", "elsewhere"])
def test_direct_runtime_cli_loads_app_without_pythonpath(settings_factory, tmp_path, working_directory):
    import subprocess
    import sys

    project = Path(__file__).resolve().parents[2]
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    (tmp_path / "db.sqlite").write_text("legacy-sentinel")
    env.update(AI_SHUFFLE_ENV_FILE="/dev/null", DATA_DIR=str(tmp_path), DATABASE_MODE="invalid-probe")
    result = subprocess.run(
        [sys.executable, "-I", str(project / "docker/container_runtime.py"), "run"],
        cwd=project if working_directory == "project" else tmp_path,
        env=env, capture_output=True, text=True, timeout=20,
    )
    # Reach configuration validation without opening a database or starting the
    # app. The image invokes this absolute script, not an imported test module.
    assert result.returncode == 1
    assert "legacy SQLite detected" in result.stderr
    assert "ModuleNotFoundError" not in result.stderr
    assert not (tmp_path / "postgres").exists()


def test_main_diagnostics_keep_type_and_call_site_but_not_secret_values(monkeypatch, capsys):
    def fail_settings():
        raise PermissionError("postgresql://user:private-test-password@host/database")
    monkeypatch.setattr(runtime, "load_settings", fail_settings)
    monkeypatch.setattr(runtime, "drop_privileges", lambda settings, **kw: None)
    monkeypatch.setattr(runtime.signal, "signal", lambda *args: None)
    assert runtime.main(["run"]) == 1
    output = capsys.readouterr().err
    assert "PermissionError at container_runtime.py:" in output
    assert "private-test-password" not in output
    assert "postgresql://" not in output


def test_retired_external_settings_do_not_bypass_storage_safety(settings_factory, tmp_path, monkeypatch):
    forbidden = tmp_path / "postgres"
    forbidden.symlink_to("/definitely/not/a/cluster")
    monkeypatch.setenv("DATABASE_MODE", "external")
    with pytest.raises(runtime.RuntimeFailure, match="symlink"):
        runtime.Database(settings_factory()).open()
    assert forbidden.is_symlink()


def test_dump_never_initializes(settings_factory, tmp_path):
    with pytest.raises(runtime.RuntimeFailure, match="retained"):
        runtime.Database(settings_factory()).open(purpose="dump")
    assert not (tmp_path / "postgres").exists()


def test_symlink_and_partial_state_are_not_repaired(settings_factory, tmp_path):
    pg = tmp_path / "postgres"
    pg.symlink_to(tmp_path / "missing")
    with pytest.raises(runtime.RuntimeFailure, match="symlink"):
        runtime.Database(settings_factory()).open()
    pg.unlink()
    pg.mkdir(mode=0o700)
    (pg / "password").write_text("do-not-reset")
    with pytest.raises(runtime.RuntimeFailure, match="partial|unrecognized"):
        runtime.Database(settings_factory()).open()
    assert (pg / "password").read_text() == "do-not-reset"


def test_lifetime_lock_is_exclusive_and_reusable(tmp_path):
    lock = runtime.ClusterLock(tmp_path / "lock")
    assert lock.acquire()
    other = runtime.ClusterLock(tmp_path / "lock")
    assert not other.acquire()
    lock.close()
    assert other.acquire()
    other.close()


def test_missing_password_wrong_version_and_retained_pid_delegation(settings_factory, tmp_path, monkeypatch):
    db = runtime.Database(settings_factory())
    pg = tmp_path / "postgres"
    pg.mkdir(mode=0o700)
    data = pg / "data"
    data.mkdir(mode=0o700)
    runtime.write_private(pg / "cluster.json", json.dumps({"version": 1, "mode": "embedded", "state": "ready", "system_identifier": "123"}))
    runtime.write_private(pg / "password", "a" * 96)
    runtime.write_private(data / "PG_VERSION", "17\n")
    with pytest.raises(runtime.RuntimeFailure, match="major"):
        db.validate_cluster()
    (data / "PG_VERSION").write_text("16\n")
    monkeypatch.setattr(db, "control_identifier", lambda: "123")
    pid = data / "postmaster.pid"
    runtime.write_private(pid, "999999\n")
    before = pid.stat()
    # Validation must leave PostgreSQL's lock file intact for PostgreSQL itself
    # to adjudicate. It is not evidence of corruption or permission to repair.
    db.validate_cluster()
    assert pid.read_bytes() == b"999999\n"
    assert (pid.stat().st_ino, pid.stat().st_mtime_ns) == (before.st_ino, before.st_mtime_ns)
    assert db.identifier == "123"
    (pg / "password").unlink()
    with pytest.raises(runtime.RuntimeFailure, match="password"):
        db.validate_cluster()
    assert pid.read_bytes() == b"999999\n"


def test_cluster_identity_and_restore_failure_are_binding(settings_factory, tmp_path, monkeypatch):
    db = runtime.Database(settings_factory())
    pg = tmp_path / "postgres"
    pg.mkdir(mode=0o700)
    (pg / "data").mkdir(mode=0o700)
    runtime.write_private(pg / "cluster.json", json.dumps({"version": 1, "mode": "embedded", "state": "ready", "system_identifier": "123"}))
    runtime.write_private(pg / "password", "a" * 96)
    runtime.write_private(pg / "data" / "PG_VERSION", "16\n")
    monkeypatch.setattr(db, "control_identifier", lambda: "456")
    with pytest.raises(runtime.RuntimeFailure, match="identity"):
        db.validate_cluster()
    monkeypatch.setattr(db, "control_identifier", lambda: "123")
    runtime.write_private(pg / "restore-incomplete", "in progress")
    with pytest.raises(runtime.RuntimeFailure, match="restore"):
        db.validate_cluster()


def test_unexpected_clean_app_exit_still_fails_container():
    app = Mock()
    app.poll.return_value = 0
    db = Mock(process=None)
    assert runtime.watch(app, db, None) == 1


def test_readiness_monitor_counts_consecutive_failures_only():
    # DESK-04: a live but unready application is reported after N failures in
    # a row; any success resets the count; first boot gets a larger allowance.
    answers = []
    monitor = runtime.ReadinessMonitor("http://127.0.0.1:8000/health/ready", probe=lambda url: answers.pop(0), failures=4, startup_failures=6)
    answers[:] = [False] * 5
    assert [monitor.check() for _ in range(5)] == [False] * 5, "before the first success the startup allowance applies"
    answers[:] = [False]
    assert monitor.check() is True
    # An application without /health/live (404) keeps the readiness limit.
    monitor = runtime.ReadinessMonitor("http://127.0.0.1:8000/health/ready", probe=lambda url: answers.pop(0),
                                       live_probe=lambda url: None, failures=4, startup_failures=6)
    answers[:] = [True, False, False, False, True, False, False, False, False]
    assert [monitor.check() for _ in range(8)] == [False] * 8
    assert monitor.failures == 3
    assert monitor.check() is True
    assert monitor.failures == 4
    assert runtime.READY_PROBE_STARTUP_FAILURES >= runtime.READY_PROBE_FAILURES


def test_busy_but_live_app_is_never_restarted_and_a_frozen_one_is():
    # #436: readiness failed while the app was busy and it was killed with
    # every agent. After the first ready answer only liveness restarts it.
    live = []
    monitor = runtime.ReadinessMonitor("http://127.0.0.1:8000/health/ready", probe=lambda url: False,
                                       live_probe=lambda url: live.append(url) or True, failures=4, live_failures=3)
    monitor.record(True)
    assert not any(monitor.check() for _ in range(200)), "not ready but live: reported, never restarted"
    assert monitor.failures == 200 and monitor.live_failures == 0
    assert live[0] == "http://127.0.0.1:8000/health/live"

    answers = iter([False, False, True, False, False, False])
    monitor.live_probe = lambda url: next(answers)
    assert [monitor.check() for _ in range(6)] == [False, False, False, False, False, True]
    assert monitor.live_failures == 3, "only consecutive liveness failures count"
    assert runtime.LIVE_PROBE_FAILURES * runtime.READY_PROBE_INTERVAL == 300, "a frozen worker is restarted after five minutes"
    assert runtime.FREEZE_CAPTURE_AT < runtime.LIVE_PROBE_FAILURES


def test_probe_live_distinguishes_missing_endpoint_from_failure():
    import http.server

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response({"/a/health/live": 200, "/b/health/live": 404}.get(self.path, 503))
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        assert runtime.probe_live(base + "/a/health/live", timeout=2) is True
        assert runtime.probe_live(base + "/b/health/live", timeout=2) is None
        assert runtime.probe_live(base + "/c/health/live", timeout=2) is False
    finally:
        server.shutdown()
        server.server_close()
    assert runtime.probe_live(base + "/a/health/live", timeout=0.5) is False


def test_watch_leaves_a_busy_app_running_and_restarts_a_frozen_one_after_capture(monkeypatch, capsys):
    monkeypatch.setattr(runtime, "READY_PROBE_INTERVAL", 0)
    captured = []
    monkeypatch.setattr(runtime, "capture_freeze", lambda process: captured.append(process))
    app, replacement = Mock(), Mock()
    app.poll.return_value = replacement.poll.return_value = None
    ready = iter([True] + [False] * 8)
    live = iter([True] * 4 + [False] * 4)
    monitor = runtime.ReadinessMonitor("http://127.0.0.1:1/health/ready", probe=lambda _: next(ready),
                                       live_probe=lambda _: next(live), failures=2, live_failures=4)
    replaced = []
    def relaunch(current):
        replaced.append(current)
        runtime.STOP.set()
        return replacement
    try:
        assert runtime.watch(app, Mock(process=None), None, monitor, relaunch=relaunch) == 0
    finally:
        runtime.STOP.clear()
    assert replaced == [app], "restarted once, after four liveness failures, not after two readiness failures"
    assert captured == [app, app], "diagnostics at the second liveness failure and before the stop"
    err = capsys.readouterr().err
    assert "not ready for 2 probes in a row but still live; leaving it running" in err
    assert "liveness probe failed 4 times in a row; capturing diagnostics before stopping it" in err
    assert "liveness probe failed 4 times in a row; restarting the application" in err
    assert "application liveness probe failed (1 of 4 in a row before a restart)" in err


def test_log_lines_carry_a_utc_timestamp(capsys):
    import re
    runtime.log("hello")
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ container runtime: hello\n", capsys.readouterr().err)


def _fake_proc(root, pid, *, caught_mask="0000000000000200"):
    process = root / str(pid)
    (process / "task").mkdir(parents=True)
    (process / "status").write_text(f"Name:\tpython\nSigCgt:\t{caught_mask}\n")
    for tid, (state, wchan, utime) in {pid: ("S", "do_epoll_wait", 100), pid + 1: ("D", "btrfs_sync_file", 250)}.items():
        task = process / "task" / str(tid)
        task.mkdir()
        (task / "stat").write_text(f"{tid} (py thread) {state} 1 1 1 0 -1 0 0 0 0 0 {utime} 50 0 0 20 0 2 0\n")
        (task / "wchan").write_text(wchan)
    return process


def test_capture_freeze_signals_only_a_registered_handler_and_logs_thread_states(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(runtime, "STACK_DUMP_SETTLE", 0)
    import signal
    monkeypatch.setattr(runtime, "_PROC", tmp_path)
    monkeypatch.setattr(runtime, "_thread_cpu", {})
    monkeypatch.setattr(runtime.shutil, "which", lambda name: None)
    monkeypatch.setattr(runtime.os, "sysconf", lambda name: 100, raising=False)
    sent = []
    monkeypatch.setattr(runtime.os, "kill", lambda pid, sig: sent.append((pid, sig)))
    process = Mock(pid=4321)
    process.poll.return_value = None
    _fake_proc(tmp_path, 4321)  # bit 9 = SIGUSR1 (10) caught
    runtime.capture_freeze(process)
    assert sent == [(4321, signal.SIGUSR1)]
    err = capsys.readouterr().err
    assert "thread 4321 (py thread) state=S wchan=do_epoll_wait cpu=1.50s" in err
    assert "thread 4322 (py thread) state=D wchan=btrfs_sync_file cpu=3.00s" in err
    stat = tmp_path / "4321" / "task" / "4322" / "stat"
    stat.write_text(stat.read_text().replace(" 250 50 ", " 750 50 "))
    runtime.capture_freeze(process)
    assert "cpu=8.00s (+5.00s since the last capture)" in capsys.readouterr().err

    # SIGUSR1 terminates an app that never registered it: not sent.
    sent.clear()
    (tmp_path / "4321" / "status").write_text("Name:\tpython\nSigCgt:\t0000000000004002\n")
    runtime.capture_freeze(process)
    assert sent == []
    assert "has no SIGUSR1 stack-dump handler" in capsys.readouterr().err

    # Exited or mock processes are skipped outright.
    process.poll.return_value = 0
    runtime.capture_freeze(process)
    runtime.capture_freeze(Mock())
    assert sent == []


def test_capture_freeze_runs_py_spy_only_when_installed(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(runtime, "STACK_DUMP_SETTLE", 0)
    monkeypatch.setattr(runtime, "_PROC", tmp_path)
    monkeypatch.setattr(runtime.os, "kill", lambda pid, sig: None)
    process = Mock(pid=77)
    process.poll.return_value = None
    _fake_proc(tmp_path, 77)
    calls = []
    monkeypatch.setattr(runtime.shutil, "which", lambda name: "/usr/bin/py-spy" if name == "py-spy" else None)
    monkeypatch.setattr(runtime.subprocess, "run", lambda args, **kwargs: calls.append(args) or Mock(
        stdout="Thread 77 (active): \"MainThread\"\n", stderr=""))
    runtime.capture_freeze(process)
    assert calls == [["/usr/bin/py-spy", "dump", "--native", "--pid", "77"]]
    assert 'py-spy: Thread 77 (active): "MainThread"' in capsys.readouterr().err


def test_probe_ready_uses_status_and_never_raises():
    import http.server
    import threading

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200 if self.path == "/health/ready" else 503)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        assert runtime.probe_ready(base + "/health/ready", timeout=2) is True
        assert runtime.probe_ready(base + "/health/not-ready", timeout=2) is False
    finally:
        server.shutdown()
        server.server_close()
    assert runtime.probe_ready(base + "/health/ready", timeout=0.5) is False, "connection refused counts as a failure"


def test_watch_exits_non_zero_when_live_app_stops_answering_readiness(monkeypatch, capsys):
    app = Mock()
    app.poll.return_value = None
    db = Mock(process=None)
    monkeypatch.setattr(runtime, "READY_PROBE_INTERVAL", 0.05)
    probes = []
    monitor = runtime.ReadinessMonitor("http://127.0.0.1:1/health/ready", probe=lambda url: probes.append(url) or False, failures=3, startup_failures=3)
    assert runtime.watch(app, db, None, monitor) == 1
    assert len(probes) == 3 and probes[0] == "http://127.0.0.1:1/health/ready"
    assert "readiness probe failed 3 times in a row" in capsys.readouterr().err


def test_watch_logs_transient_readiness_degradation_and_recovery(monkeypatch, capsys):
    app = Mock()
    app.poll.return_value = None
    monkeypatch.setattr(runtime, "READY_PROBE_INTERVAL", 0)
    answers = iter([True, False, False, True])
    probes = []
    def probe(url):
        answer = next(answers)
        probes.append(answer)
        if len(probes) == 4:
            runtime.STOP.set()
        return answer
    monitor = runtime.ReadinessMonitor("http://127.0.0.1:1/health/ready", probe=probe, failures=3)
    relaunch = Mock()
    try:
        assert runtime.watch(app, Mock(process=None), None, monitor, relaunch=relaunch) == 0
    finally:
        runtime.STOP.clear()
    relaunch.assert_not_called()
    err = capsys.readouterr().err
    assert err.count("application readiness degraded") == 1
    assert err.count("application readiness recovered") == 1
    assert err.index("degraded") < err.index("recovered")


def test_watch_restarts_a_crashed_or_unready_app_in_place_within_a_budget(monkeypatch, capsys):
    # #2112: a container whose restart policy is "no" stayed down after the
    # readiness watchdog exited it. The app is restarted inside the container
    # first; only repeated failures exit, for the restart policy to handle.
    db = Mock(process=None)
    monkeypatch.setattr(runtime, "READY_PROBE_INTERVAL", 0.01)
    crashed = Mock()
    crashed.poll.return_value = 1
    started = []
    def relaunch(current):
        started.append(current)
        app = Mock()
        app.poll.return_value = 1  # every replacement crashes too
        return app
    assert runtime.watch(crashed, db, None, relaunch=relaunch) == 1
    assert len(started) == runtime.RELAUNCH_LIMIT and started[0] is crashed
    err = capsys.readouterr().err
    assert err.count("restarting the application") == runtime.RELAUNCH_LIMIT
    assert "application exited unexpectedly; exiting so the container restarts" in err

    # A live app that stops answering readiness is replaced, then supervised.
    hung, healthy = Mock(), Mock()
    hung.poll.return_value = healthy.poll.return_value = None
    answers = iter([False, False, True])
    def probe(_):
        answer = next(answers)
        if answer:
            runtime.STOP.set()
        return answer
    monitor_type = runtime.ReadinessMonitor
    def monitor(url):
        return monitor_type(url, probe=probe, failures=2, startup_failures=2)
    monkeypatch.setattr(runtime, "ReadinessMonitor", monitor)
    replaced = []
    def relaunch_hung(current):
        replaced.append(current)
        return healthy
    runtime.STOP.clear()
    try:
        assert runtime.watch(hung, db, None, monitor("http://127.0.0.1:1/health/ready"), relaunch=relaunch_hung) == 0
    finally:
        runtime.STOP.clear()
    assert replaced == [hung]
    assert "readiness probe failed 2 times in a row; restarting the application" in capsys.readouterr().err


def test_watch_budget_forgets_old_restarts(monkeypatch):
    from types import SimpleNamespace
    clock = [0.0]
    monkeypatch.setattr(runtime, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    db = Mock(process=None)
    crashes = []
    def relaunch(current):
        crashes.append(clock[0])
        clock[0] += runtime.RELAUNCH_WINDOW / 2  # one crash every 7.5 minutes
        app = Mock()
        app.poll.return_value = None if len(crashes) > 6 else 1
        if len(crashes) > 6:
            runtime.STOP.set()
        return app
    first = Mock()
    first.poll.return_value = 1
    runtime.STOP.clear()
    try:
        assert runtime.watch(first, db, None, relaunch=relaunch) == 0
    finally:
        runtime.STOP.clear()
    assert len(crashes) == 7, "spaced-out crashes never exhaust the budget"


def test_watch_without_a_replacement_or_when_blocked_exits():
    db = Mock(process=None)
    app = Mock()
    app.poll.return_value = 1
    assert runtime.watch(app, db, None, relaunch=lambda current: None) == 1
    def broken(current):
        raise OSError("synthetic spawn failure")
    assert runtime.watch(app, db, None, relaunch=broken) == 1
    # PostgreSQL or Chromium exiting still exits the container.
    db.process = Mock()
    db.process.poll.return_value = 1
    app.poll.return_value = None
    assert runtime.watch(app, db, None, relaunch=Mock()) == 1


def test_run_probes_the_application_port(settings_factory, monkeypatch):
    cfg = settings_factory(port=18080)
    db = runtime.Database(cfg)
    db.password = "synthetic-password"
    for name in ("open", "close", "publish_descriptor"):
        monkeypatch.setattr(db, name, Mock())
    monkeypatch.setattr(runtime, "Database", lambda settings: db)
    monkeypatch.setenv("START_CHROME", "0")
    monkeypatch.setattr(runtime.subprocess, "Popen", lambda *args, **kwargs: Mock())
    seen = {}
    monkeypatch.setattr(runtime, "watch", lambda app, db, chrome, monitor=None, updates=None, restart=None, relaunch=None, chrome_current=None: seen.update(url=monitor.url) or 0)
    monkeypatch.setattr(runtime, "shutdown", Mock())
    runtime.STOP.clear()
    assert runtime.run(cfg, "run") == 0
    assert seen["url"] == "http://127.0.0.1:18080/health/ready"


def test_backend_refuses_legacy_sqlite_before_creating_directories_or_connecting(tmp_path, monkeypatch):
    from app.core import startup
    legacy = tmp_path / "db.sqlite"
    legacy.write_bytes(b"legacy sentinel")
    monkeypatch.setattr(startup.settings, "data_dir", tmp_path)
    monkeypatch.setattr(startup, "ensure_directories", lambda: pytest.fail("must fail before any startup writes"))
    with pytest.raises(RuntimeError, match="Legacy SQLite"):
        startup.init_core()
    assert legacy.read_bytes() == b"legacy sentinel"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["db.sqlite"]


def test_run_publishes_private_descriptor_before_spawning_worker(settings_factory, tmp_path, monkeypatch):
    cfg = settings_factory()
    db = runtime.Database(cfg)
    db.password = "synthetic-password"
    monkeypatch.setattr(db, "open", Mock())
    monkeypatch.setattr(db, "close", Mock())
    monkeypatch.setattr(runtime, "Database", lambda settings: db)
    monkeypatch.setenv("START_CHROME", "0")
    monkeypatch.setenv("DATABASE_URL", "postgresql://bad:synthetic@external.invalid/prod")
    monkeypatch.setenv("POSTGRES_HOST", "external.invalid")
    monkeypatch.setenv("PGHOSTADDR", "203.0.113.2")
    monkeypatch.setenv("UNRELATED_APP_SETTING", "preserved")
    def spawn(args, *, env, **kwargs):
        assert cfg.get_database_connection()["password"] == "synthetic-password"
        assert not any(k.startswith(("DATABASE_", "POSTGRES_", "PG")) for k in env)
        assert env["UNRELATED_APP_SETTING"] == "preserved"
        assert env["DATA_DIR"] == str(tmp_path)
        return Mock()
    monkeypatch.setattr(runtime.subprocess, "Popen", spawn)
    monkeypatch.setattr(runtime, "watch", lambda *args, **kwargs: 0)
    monkeypatch.setattr(runtime, "shutdown", Mock())
    runtime.STOP.clear()
    assert runtime.run(cfg, "run") == 0


_SUPERVISOR_SECRETS = {
    "SECRET_KEY": "synthetic-secret-key",
    "OWNER_PASSWORD": "synthetic-owner-password",
    "CLUSTER_SECRET": "synthetic-cluster-secret",
    "LICENSE_KEY": "synthetic-license",
    "DATABASE_URL": "postgresql://u:synthetic@external.invalid/prod",
    "ANTHROPIC_API_KEY": "sk-synthetic",
    "OPENAI_API_KEY": "sk-synthetic-2",
    "GITHUB_TOKEN": "ghp-synthetic",
    "SOME_SERVICE_TOKEN": "synthetic-token",
    "STRIPE_KEY": "synthetic-stripe",
    "SHUFFLE_DESKTOP_LIFECYCLE_TOKEN": "synthetic-lifecycle-token",
}


def _secret_environment(monkeypatch):
    for name, value in _SUPERVISOR_SECRETS.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("LANG", "C.UTF-8")
    monkeypatch.setenv("LC_MESSAGES", "C")
    monkeypatch.setenv("TZ", "UTC")


def _leaked(env):
    return sorted(k for k, v in env.items() if k in _SUPERVISOR_SECRETS or v in _SUPERVISOR_SECRETS.values())


def test_postgres_server_gets_only_its_own_environment(settings_factory, monkeypatch, tmp_path):
    """R13-1: PostgreSQL (and its forked backends) ran with every secret the
    supervisor holds, readable by same-uid processes in /proc/<pid>/environ."""
    _secret_environment(monkeypatch)
    monkeypatch.setenv("UNRELATED_APP_SETTING", "not-for-postgres")
    # R14-2: an unpacked PostgreSQL finds libpq through the operator's library path.
    monkeypatch.setenv("LD_LIBRARY_PATH", "/opt/pg/lib")
    db = runtime.Database(settings_factory())
    db.pg.mkdir(parents=True)
    monkeypatch.setattr(db, "command", lambda *a, **k: "postgres (PostgreSQL) 16.4")
    spawned = []
    monkeypatch.setattr(runtime.subprocess, "Popen", lambda args, **kw: spawned.append((args, kw["env"])) or Mock())
    try:
        db.start()
    finally:
        db.pg_log.close()
        db.private_runtime.cleanup()
    ((args, env),) = spawned
    assert args[0] == "postgres"
    assert _leaked(env) == []
    assert "UNRELATED_APP_SETTING" not in env
    assert env["PATH"] == os.environ["PATH"]
    assert (env["LANG"], env["LC_MESSAGES"], env["TZ"], env["LC_ALL"]) == ("C.UTF-8", "C", "UTC", "C")
    # The maintenance clients get the same base plus their own PG* settings.
    client = runtime.client_env("postgresql://u:pw@127.0.0.1:5432/db")
    assert _leaked(client) == [] and client["PGPASSWORD"] == "pw"
    assert env["LD_LIBRARY_PATH"] == client["LD_LIBRARY_PATH"] == "/opt/pg/lib"


def test_runtime_selection_restarts_chromium_and_restores_it_with_application(settings_factory, image_here, tmp_path, monkeypatch):
    cfg = settings_factory()
    db = runtime.Database(cfg)
    for name in ("open", "close", "publish_descriptor"):
        monkeypatch.setattr(db, name, Mock())
    monkeypatch.setattr(runtime, "Database", lambda settings: db)
    monkeypatch.setenv("START_CHROME", "1")
    monkeypatch.setenv("HOME", str(tmp_path))
    updates = Mock()
    updates.root = tmp_path / "updates"
    updates.state = {}
    updates.lifecycle.side_effect = lambda action: {"active_sessions": 0, "stopped": True}
    selections = []
    original_path = os.environ.get("PATH")
    original_library_path = os.environ.get("LD_LIBRARY_PATH")

    def launch(env, activate=False):
        chosen = "new" if activate else "old"
        return ["python", "-m", "app.main"], None, dict(
            env, PATH=f"/runtimes/{chosen}/bin", CHROME_EXECUTABLE=f"/runtimes/{chosen}/chrome",
            LD_LIBRARY_PATH=f"/runtimes/{chosen}/lib", SHUFFLE_RUNTIME_OVERLAYS="1")

    updates.launch.side_effect = launch
    module = Mock(ContainerUpdates=Mock(return_value=updates), UpdateError=RuntimeError)
    monkeypatch.setattr(runtime, "load_updater", lambda: module)
    browsers = []

    def spawn(command, *, env, **kwargs):
        selections.append(env["CHROME_EXECUTABLE"])
        process = Mock()
        process.poll.return_value = None
        browsers.append(process)
        return process

    monkeypatch.setattr(runtime.subprocess, "Popen", spawn)
    monkeypatch.setattr(runtime, "start_app", Mock(side_effect=lambda *args: Mock()))
    stopped = []
    monkeypatch.setattr(runtime, "stop_group", lambda process, timeout: stopped.append(process))
    monkeypatch.setattr(runtime, "shutdown", Mock())

    def watch(app, db, chrome, monitor, updates, restart, relaunch, chrome_current=None):
        assert chrome_current() is browsers[0]
        app = restart(app)
        assert chrome_current() is browsers[1]
        relaunch(app)
        assert chrome_current() is browsers[2]
        return 0

    monkeypatch.setattr(runtime, "watch", watch)
    runtime.STOP.clear()
    assert runtime.run(cfg, "run") == 0
    assert selections == ["/runtimes/old/chrome", "/runtimes/new/chrome", "/runtimes/old/chrome"]
    assert browsers[0] in stopped and browsers[1] in stopped
    updates.activating.assert_called_once()
    updates.failed.assert_called_once()
    # The PostgreSQL supervisor's own environment was never changed.
    assert os.environ.get("PATH") == original_path
    assert os.environ.get("LD_LIBRARY_PATH") == original_library_path


def test_chrome_gets_no_secrets_or_lifecycle_token(settings_factory, tmp_path, monkeypatch):
    """R13-1: chrome-supervisor, start-chrome and Chromium inherited the app's
    secrets, and the lifecycle token that authorizes the internal desktop routes."""
    cfg = settings_factory()
    db = runtime.Database(cfg)
    db.password = "synthetic-password"
    for name in ("open", "close", "publish_descriptor"):
        monkeypatch.setattr(db, name, Mock())
    monkeypatch.setattr(runtime, "Database", lambda settings: db)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("START_CHROME", "1")
    monkeypatch.setenv("CHROME_CDP_PORT", "9221")
    monkeypatch.setenv("BROWSER_SSRF_ALLOW", "10.0.0.0/8")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:3128")
    _secret_environment(monkeypatch)
    spawned = {}

    def spawn(args, *, env, **kwargs):
        chrome = Path(args[0]).name in ("chrome-supervisor", "chrome-supervisor.sh")
        spawned["chrome-supervisor" if chrome else "app"] = env
        if chrome:
            spawned["command"] = args
        return Mock()

    monkeypatch.setattr(runtime.subprocess, "Popen", spawn)
    monkeypatch.setattr(runtime, "watch", lambda *args, **kwargs: 0)
    monkeypatch.setattr(runtime, "shutdown", Mock())
    # A running source worker may export its production image directory.
    monkeypatch.setattr(runtime, "image_dir", lambda: tmp_path / "image")
    runtime.STOP.clear()
    assert runtime.run(cfg, "run") == 0
    chrome, app = spawned["chrome-supervisor"], spawned["app"]
    assert _leaked(chrome) == []
    for name in ("CHROME_CDP_PORT", "BROWSER_SSRF_ALLOW", "HTTPS_PROXY", "HOME", "PATH", "LANG", "TZ"):
        assert chrome[name] == os.environ[name]
    assert chrome["DATA_DIR"] == str(tmp_path)
    # Chromium's launch scripts run from this source's private copy (#1821),
    # removed when the supervisor ends.
    private = Path(spawned["command"][0]).parent
    assert spawned["command"] == [str(private / "chrome-supervisor.sh")]
    assert chrome["START_CHROME_COMMAND"] == str(private / "start-chrome.sh")
    assert not private.exists()
    # The app itself still gets what it needs.
    assert app["SECRET_KEY"] == "synthetic-secret-key"
    assert app["SHUFFLE_DESKTOP_LIFECYCLE_TOKEN"] == "synthetic-lifecycle-token"


def test_chrome_env_drops_a_token_the_supervisor_adds():
    env = {"PATH": "/bin", "CHROME_CDP": "1", "SHUFFLE_DESKTOP_LIFECYCLE_TOKEN": "t",
           "SHUFFLE_CONTAINER_UPDATES": "/data/application-updates", "GH_TOKEN": "g", "FAL_KEY": "f"}
    assert runtime.chrome_env(env) == {"PATH": "/bin", "CHROME_CDP": "1",
                                       "SHUFFLE_CONTAINER_UPDATES": "/data/application-updates"}


def test_main_marks_the_supervisor_non_dumpable_after_dropping_privileges(monkeypatch, settings_factory):
    """R13-1: the supervisor holds every secret in its own environ."""
    order = []
    cfg = settings_factory()
    monkeypatch.setattr(runtime, "load_settings", lambda: order.append("settings") or cfg)
    monkeypatch.setattr(runtime, "drop_privileges", lambda settings, **kw: order.append("drop"))
    monkeypatch.setattr(runtime, "harden_process_introspection", lambda settings: order.append("harden"))
    monkeypatch.setattr(runtime, "run", lambda settings, action, filename=None: order.append(action) or 0)
    assert runtime.main(["run"]) == 0
    assert order == ["drop", "settings", "harden", "run"]


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="prctl is Linux-only")
@pytest.mark.parametrize("allow,expected", [(False, "True 0"), (True, "False 1")])
def test_supervisor_hardening_really_clears_dumpable(allow, expected):
    import subprocess

    script = (
        "import ctypes, ctypes.util, importlib.util, sys, types\n"
        "spec = importlib.util.spec_from_file_location('rt', sys.argv[1])\n"
        "rt = importlib.util.module_from_spec(spec); spec.loader.exec_module(rt)\n"
        "done = rt.harden_process_introspection(types.SimpleNamespace(allow_process_introspection=sys.argv[2] == '1'))\n"
        "libc = ctypes.CDLL(ctypes.util.find_library('c') or None)\n"
        "print(done, libc.prctl(3, 0, 0, 0, 0))\n"
    )
    path = Path(__file__).resolve().parents[2] / "docker" / "container_runtime.py"
    out = subprocess.run([sys.executable, "-c", script, str(path), "1" if allow else "0"],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.strip() == expected


def test_shutdown_order(monkeypatch):
    calls = []
    app, chrome = Mock(), Mock()
    db = Mock()
    db.close.side_effect = lambda: calls.append("postgres")
    monkeypatch.setattr(runtime, "stop_group", lambda process, timeout: calls.append("app" if process is app else "chrome"))
    runtime.shutdown(app, chrome, db)
    assert calls == ["app", "chrome", "postgres"]


def test_control_warnings_are_errors_not_accepted_identifiers(settings_factory, monkeypatch):
    db = runtime.Database(settings_factory())
    command = Mock(return_value="pg_control version number: 1300\nCatalog version number: 202307071\nDatabase system identifier: 123\n")
    monkeypatch.setattr(db, "command", command)
    assert db.control_identifier() == "123"
    assert command.call_args.kwargs["reject_stderr"] is True


def test_command_timeout_and_errors_do_not_disclose_output(settings_factory):
    import sys
    db = runtime.Database(settings_factory())
    with pytest.raises(runtime.RuntimeFailure, match="timed out"):
        db.command([sys.executable, "-c", "import time; time.sleep(20)"], timeout=0.05)
    with pytest.raises(runtime.RuntimeFailure) as caught:
        db.command([sys.executable, "-c", "import sys; print('synthetic-secret', file=sys.stderr); sys.exit(1)"])
    assert "synthetic-secret" not in str(caught.value)


def test_dump_refuses_existing_backup_and_symlink(settings_factory, tmp_path):
    db = runtime.Database(settings_factory())
    backup = tmp_path / "sentinel.dump"
    backup.write_bytes(b"preserve")
    with pytest.raises(FileExistsError):
        db.dump(str(backup))
    link = tmp_path / "backup-link"
    link.symlink_to(backup)
    with pytest.raises(FileExistsError):
        db.dump(str(link))
    assert backup.read_bytes() == b"preserve"




@pytest.mark.parametrize("name", ["DATA_DIR", "WORKSPACE_DIR"])
@pytest.mark.parametrize("path", ["/app/docker/__pycache__", "/usr/local/lib/python3.12/site-packages/sitecustomize", "/opt/fluid/cache", "/etc/cache"])
def test_root_mount_preparation_refuses_unmounted_image_paths(monkeypatch, name, path):
    monkeypatch.setattr(runtime, "mounted_directory", lambda _: False)
    with pytest.raises(runtime.RuntimeFailure, match=name + ".*mount a volume.*outside the image trees"):
        runtime.validate_mount_root(name, Path(path))




@pytest.mark.parametrize("field,name", [("data_dir", "DATA_DIR"), ("workspace_dir", "WORKSPACE_DIR")])
def test_root_rejects_image_storage_before_any_preparation(settings_factory, tmp_path, monkeypatch, field, name):
    from types import SimpleNamespace
    cfg = settings_factory(**{field: Path("/app/docker/__pycache__")})
    monkeypatch.setattr(runtime.os, "geteuid", lambda: 0)
    monkeypatch.setattr(runtime.pwd, "getpwnam", lambda _: SimpleNamespace(pw_uid=1000, pw_gid=1000, pw_dir="/home/appuser"))
    monkeypatch.setattr(runtime, "mounted_directory", lambda _: False)
    mkdir, chown = Mock(), Mock()
    monkeypatch.setattr(Path, "mkdir", mkdir)
    monkeypatch.setattr(runtime.os, "chown", chown)
    with pytest.raises(runtime.RuntimeFailure, match=name + ".*mount a volume"):
        runtime.drop_privileges(cfg)
    mkdir.assert_not_called()
    chown.assert_not_called()


def test_root_mount_preparation_preserves_volume_inside_app(monkeypatch):
    monkeypatch.setattr(runtime, "mounted_directory", lambda path: path == Path("/app/data"))
    assert runtime.validate_mount_root("DATA_DIR", Path("/app/data")) == Path("/app/data")


@pytest.mark.parametrize("name,path", [("DATA_DIR", "/data"), ("WORKSPACE_DIR", "/workspace")])
def test_root_mount_preparation_preserves_defaults(monkeypatch, name, path):
    monkeypatch.setattr(runtime, "mounted_directory", lambda _: pytest.fail("defaults need no mount exception"))
    assert runtime.validate_mount_root(name, Path(path)) == Path(path)


@pytest.mark.parametrize("path,escaped", [("/app/data", "/app/data"), ("/app/my data", r"/app/my\040data")])
def test_root_mount_detection_includes_same_device_bind_mounts(monkeypatch, path, escaped):
    original = Path.read_text
    monkeypatch.setattr(runtime.os.path, "ismount", lambda _: False)
    mountinfo = "44 31 8:1 /retained " + escaped + " rw - ext4 /dev/test rw\n"
    monkeypatch.setattr(Path, "read_text", lambda self, *a, **kw: mountinfo if str(self) == "/proc/self/mountinfo" else original(self, *a, **kw))
    assert runtime.mounted_directory(Path(path))
    assert not runtime.mounted_directory(Path(path + "-other"))




@pytest.fixture
def home_ownership(monkeypatch):
    """Real walks/inodes/hardlinks with simulated IDs and no privileged writes."""
    def configure(owners=None, aliases=None):
        owners, aliases = dict(owners or {}), dict(aliases or {})
        original_stat, original_fstat = os.stat, os.fstat
        changed = []
        def fd_path(fd):
            return Path(os.readlink(f"/proc/self/fd/{fd}"))
        def decorate(path, info):
            result = list(aliases.get(path, info))
            owner = owners.get(path, (41001, 41001))
            result[4], result[5] = owner
            return os.stat_result(result)
        def stat_path(path, *, dir_fd=None, follow_symlinks=True):
            info = original_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)
            resolved = fd_path(path) if isinstance(path, int) else (fd_path(dir_fd) / path if dir_fd is not None else Path(path))
            return decorate(resolved, info)
        def fstat(fd):
            return decorate(fd_path(fd), original_fstat(fd))
        def fchown(fd, uid, gid):
            path = fd_path(fd)
            changed.append((path, uid, gid))
            owners[path] = (uid, gid)
        monkeypatch.setenv("AGENT_ISOLATION_SERVICE_UID", "64000")
        monkeypatch.setattr(runtime.os, "stat", stat_path)
        monkeypatch.setattr(runtime.os, "fstat", fstat)
        monkeypatch.setattr(runtime.os, "fchown", fchown)
        monkeypatch.setattr(runtime, "mount_points", lambda: set())
        return changed
    return configure


@pytest.mark.parametrize("kind", ["exec-file", "hardlink", "symlink", "mount", "swap"])
def test_home_root_repair_safety(tmp_path, monkeypatch, home_ownership, kind):
    home, data, workspace = (tmp_path / name for name in ("home", "data", "workspace"))
    for directory in (home, data, workspace):
        directory.mkdir()
    victim = home / "exec-file"
    victim.write_text("docker exec style write")
    outside = tmp_path / "shadow-like"
    outside.write_text("protected fixture")
    owners = {victim: (0, 0), outside: (0, 0)}
    if kind == "hardlink":
        (home / "linked").hardlink_to(victim)
        owners[home / "linked"] = (0, 0)
    elif kind == "symlink":
        victim.unlink()
        victim.symlink_to(outside)
    elif kind == "mount":
        victim.unlink()
        victim.mkdir()
        (victim / "foreign").write_text("another mount")
        owners[victim / "foreign"] = (0, 0)
    changed = home_ownership(owners)
    if kind == "mount":
        monkeypatch.setattr(runtime, "mount_points", lambda: {victim})
    elif kind == "swap":
        original_open = os.open
        def swapped(name, flags, *args, **kwargs):
            return original_open(outside if name == "exec-file" else name, flags, *args, **kwargs)
        monkeypatch.setattr(runtime.os, "open", swapped)
    result = runtime.repair_home_root_files(home, data, workspace, 41002, 41002)
    assert changed == ([(victim, 41002, 41002)] if kind == "exec-file" else [])
    assert result["repaired"] == (1 if kind == "exec-file" else 0)
    assert result["reasons"].get({"hardlink": "hardlink", "symlink": "type", "mount": "mount", "swap": "changed"}.get(kind, "not-root"), 0)


def test_home_root_repair_fixed_mounts_and_storage_aliases(tmp_path, monkeypatch, home_ownership):
    home, data, workspace = (tmp_path / name for name in ("home", "data", "workspace"))
    volume = home / ".local"
    for directory in (volume, data, workspace):
        directory.mkdir(parents=True)
    paths = [volume / "repair", home / "data-alias", home / "workspace-alias"]
    paths[0].write_text("fixture")
    for alias in paths[1:]:
        alias.mkdir()
        (alias / "untouched").write_text("retained storage")
    changed = home_ownership({path: (0, 0) for path in [volume, *paths, *(p / "untouched" for p in paths[1:])]},
                             aliases={paths[1]: data.stat(), paths[2]: workspace.stat()})
    monkeypatch.setattr(runtime, "mount_points", lambda: {volume})
    result = runtime.repair_home_root_files(home, data, workspace, 41002, 41002)
    assert {entry[0] for entry in changed} == {volume, paths[0]}
    assert result["reasons"]["storage"] == 4  # both aliases in shallow and general passes
    again = runtime.repair_home_root_files(home, data, workspace, 41002, 41002)
    assert again["repaired"] == 0
    assert len(changed) == 2


def test_home_root_repair_prioritizes_fluid_before_large_decoy(tmp_path, monkeypatch, home_ownership):
    home, data, workspace = (tmp_path / name for name in ("home", "data", "workspace"))
    decoy = home / ".cache"
    decoy.mkdir(parents=True)
    for index in range(30):
        (decoy / str(index)).write_text("decoy")
    target = home / ".fluid" / "exec-file"
    target.parent.mkdir()
    target.write_text("repair me")
    data.mkdir()
    workspace.mkdir()
    changed = home_ownership({target: (0, 0)})
    result = runtime.repair_home_root_files(home, data, workspace, 41002, 41002, max_entries=4)
    assert changed == [(target, 41002, 41002)]
    assert result["limited"]


def test_home_root_repair_prioritizes_shallow_home_before_large_local(tmp_path, home_ownership):
    home, data, workspace = (tmp_path / name for name in ("home", "data", "workspace"))
    local = home / ".local"
    local.mkdir(parents=True)
    for index in range(30):
        (local / str(index)).write_text("large package tree")
    target = home / "root-exec-file"
    target.write_text("repair before descending")
    data.mkdir()
    workspace.mkdir()
    changed = home_ownership({target: (0, 0)})
    result = runtime.repair_home_root_files(home, data, workspace, 41002, 41002, max_entries=5)
    assert changed == [(target, 41002, 41002)]
    assert result["limited"]


def test_home_root_repair_checks_time_inside_directory_listing(tmp_path, monkeypatch, home_ownership):
    home, data, workspace = (tmp_path / name for name in ("home", "data", "workspace"))
    for directory in (home, data, workspace):
        directory.mkdir()
    for index in range(30):
        (home / str(index)).write_text("fixture")
    home_ownership()
    original_scandir = os.scandir
    clock, reads = [0], []
    class SlowListing:
        def __init__(self, fd):
            self.iterator = original_scandir(fd)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.iterator.close()
        def __iter__(self):
            return self
        def __next__(self):
            reads.append(1)
            clock[0] += 2
            return next(self.iterator)
    monkeypatch.setattr(runtime.os, "scandir", SlowListing)
    monkeypatch.setattr(runtime.time, "monotonic", lambda: clock[0])
    result = runtime.repair_home_root_files(home, data, workspace, 41002, 41002)
    assert result["limited"]
    assert len(reads) <= 3


def test_home_root_repair_is_bounded_and_logs_capped_relative_sample(tmp_path, monkeypatch, home_ownership):
    home, data, workspace = (tmp_path / name for name in ("home", "data", "workspace"))
    for directory in (home, data, workspace):
        directory.mkdir()
    files = [home / f"file-{index}" for index in range(20)]
    for path in files:
        path.write_text("contents never logged")
    changed = home_ownership({path: (0, 0) for path in files})
    messages = []
    monkeypatch.setattr(runtime, "log", messages.append)
    result = runtime.repair_home_root_files(home, data, workspace, 41002, 41002, max_entries=10)
    assert result["visited"] == 10
    assert result["limited"]
    assert 0 < len(changed) < len(files)
    assert len(messages) == 1
    assert "limit" in messages[0] and "repaired=" in messages[0] and "skipped=" in messages[0]
    assert str(home) not in messages[0] and "contents never logged" not in messages[0]
    sample = json.loads(messages[0].split("sample=", 1)[1])
    assert len(sample) <= 5


def test_home_root_repair_refuses_symlinked_fixed_volume_parent(tmp_path, monkeypatch, home_ownership):
    home, data, workspace, outside = (tmp_path / name for name in ("home", "data", "workspace", "outside"))
    for directory in (home, data, workspace, outside / "gh"):
        directory.mkdir(parents=True)
    (home / ".config").symlink_to(outside, target_is_directory=True)
    changed = home_ownership({outside / "gh": (0, 0)})
    monkeypatch.setattr(runtime, "mount_points", lambda: {home / ".config" / "gh"})
    result = runtime.repair_home_root_files(home, data, workspace, 41002, 41002)
    assert changed == []
    # Both the fixed .config root and its fixed gh child refuse that parent.
    assert result["reasons"]["unavailable"] == 2


@pytest.mark.parametrize("change", ["device", "hardlink-after-open"])
def test_home_root_repair_rechecks_device_and_link_count(tmp_path, monkeypatch, home_ownership, change):
    home, data, workspace = (tmp_path / name for name in ("home", "data", "workspace"))
    for directory in (home, data, workspace):
        directory.mkdir()
    victim = home / "victim"
    victim.write_text("fixture")
    aliases = {}
    if change == "device":
        info = list(victim.stat())
        info[2] += 1
        aliases[victim] = os.stat_result(info)
    changed = home_ownership({victim: (0, 0)}, aliases=aliases)
    if change == "hardlink-after-open":
        original_open = os.open
        def linked(name, flags, *args, **kwargs):
            fd = original_open(name, flags, *args, **kwargs)
            if name == "victim":
                (home / "late-link").hardlink_to(victim)
            return fd
        monkeypatch.setattr(runtime.os, "open", linked)
    result = runtime.repair_home_root_files(home, data, workspace, 41002, 41002)
    assert changed == []
    # A foreign device is excluded in both passes; a late hardlink is caught
    # after open in the first pass and excluded before open in the second.
    assert result["reasons"]["device" if change == "device" else "changed"] == (2 if change == "device" else 1)


@pytest.mark.parametrize("storage", ["data", "workspace"])
def test_home_root_repair_never_repairs_home_used_as_storage(tmp_path, home_ownership, storage):
    home, outside = tmp_path / "home", tmp_path / "outside"
    home.mkdir()
    outside.mkdir()
    victim = home / "victim"
    victim.write_text("fixture")
    changed = home_ownership({home: (0, 0), victim: (0, 0)})
    result = runtime.repair_home_root_files(home, home if storage == "data" else outside,
                                           home if storage == "workspace" else outside, 41002, 41002)
    assert changed == []
    assert result["reasons"]["storage"] == len(runtime.HOME_VOLUMES) + 2


def test_home_root_repair_priority_child_cannot_bypass_storage_ancestor(tmp_path, home_ownership):
    home, data, workspace = (tmp_path / name for name in ("home", "data", "workspace"))
    config = home / ".config"
    gh = config / "gh"
    for directory in (gh, data, workspace):
        directory.mkdir(parents=True)
    victim = gh / "untouched"
    victim.write_text("retained data")
    changed = home_ownership({victim: (0, 0)}, aliases={config: data.stat()})
    result = runtime.repair_home_root_files(home, data, workspace, 41002, 41002)
    assert changed == []
    assert result["reasons"]["storage"] == 4  # .config, .config/gh, shallow and general walks


def test_home_root_repair_bounds_directory_depth(tmp_path, home_ownership):
    home, data, workspace = (tmp_path / name for name in ("home", "data", "workspace"))
    deep = home.joinpath(*(["nested"] * 66))
    for directory in (deep, data, workspace):
        directory.mkdir(parents=True)
    victim = deep / "untouched"
    victim.write_text("beyond depth budget")
    changed = home_ownership({victim: (0, 0)})
    result = runtime.repair_home_root_files(home, data, workspace, 41002, 41002)
    assert changed == []
    assert result["reasons"]["depth"] == 1


def test_home_root_repair_time_budget_does_not_block_startup(tmp_path, monkeypatch, home_ownership):
    home, data, workspace = (tmp_path / name for name in ("home", "data", "workspace"))
    for directory in (home, data, workspace):
        directory.mkdir()
    changed = home_ownership({home: (0, 0)})
    clock = iter([0, 6])
    monkeypatch.setattr(runtime.time, "monotonic", lambda: next(clock))
    result = runtime.repair_home_root_files(home, data, workspace, 41002, 41002)
    assert changed == [] and result["limited"] and result["visited"] == 0


def test_remap_home_startup_repairs_as_root_before_drop(monkeypatch):
    from types import SimpleNamespace
    settings = SimpleNamespace(effective_data_dir=Path("/data"), effective_workspace_dir=Path("/workspace"))
    monkeypatch.setattr(runtime, "load_startup_settings", lambda: settings)
    monkeypatch.setattr(runtime.os, "geteuid", lambda: 0)
    monkeypatch.setattr(runtime, "drop_privileges", lambda *a, **kw: pytest.fail("repair must precede drop"))
    monkeypatch.setattr(runtime, "load_settings", lambda: pytest.fail("no root application imports"))
    calls = []
    monkeypatch.setattr(runtime, "remap_home", lambda *args: calls.append(("remap", args)))
    monkeypatch.setattr(runtime, "repair_home_root_files", lambda *args: calls.append(("repair", args)))
    assert runtime.main(["remap-home", "--old-uid", "41001", "--old-gid", "41001",
                         "--new-uid", "41002", "--new-gid", "41002"]) == 0
    assert [name for name, _ in calls] == ["remap", "repair"]
    assert calls[1][1] == (Path("/home/appuser"), Path("/data"), Path("/workspace"), 41002, 41002)


def test_home_remap_skips_foreign_owners_and_all_hardlinks(tmp_path, monkeypatch, home_ownership):
    home, data = tmp_path / "home", tmp_path / "data"
    home.mkdir()
    data.mkdir()
    for name in ("single", "owned", "root-file", "service-file"):
        (home / name).write_text("fixture")
    (home / "owned-link").hardlink_to(home / "owned")
    (home / "root-link").hardlink_to(home / "root-file")
    changed = home_ownership({home / name: (uid, 41001) for name, uid in
                              (("root-file", 0), ("root-link", 0), ("service-file", 64000))})
    runtime.remap_home(home, data, 41001, 41001, 41002, 41002)
    assert changed == [(home / "single", 41002, 41002), (home, 41002, 41002)]


def test_home_remap_prunes_nested_mounts_and_data_bind_aliases(tmp_path, monkeypatch, home_ownership):
    home, data = tmp_path / "home", tmp_path / "data"
    data.mkdir()
    volume = home / ".claude"
    alias, nested = volume / "data", volume / "other-volume"
    for directory in (alias, nested):
        directory.mkdir(parents=True)
        (directory / "untouched").write_text("fixture")
    (volume / "migrate").write_text("fixture")
    changed = home_ownership(aliases={alias: data.stat()})
    monkeypatch.setattr(runtime, "mount_points", lambda: {volume, nested})
    runtime.remap_home(home, data, 41001, 41001, 41002, 41002)
    assert {entry[0] for entry in changed} == {home, volume, volume / "migrate"}


@pytest.mark.parametrize("mode", ["on", "required", "off"])
def test_home_remap_invalid_service_uid_obeys_isolation_mode(tmp_path, monkeypatch, home_ownership, mode):
    home, data = tmp_path / "home", tmp_path / "data"
    home.mkdir()
    data.mkdir()
    changed = home_ownership()
    monkeypatch.setenv("AGENT_ISOLATION", mode)
    monkeypatch.setenv("AGENT_ISOLATION_SERVICE_UID", "abc")
    if mode == "required":
        with pytest.raises(runtime.RuntimeFailure, match="required agent isolation: startup refused; AGENT_ISOLATION_SERVICE_UID"):
            runtime.remap_home(home, data, 41001, 41001, 41002, 41002)
        assert changed == []
    else:
        runtime.remap_home(home, data, 41001, 41001, 41002, 41002)
        assert changed == [(home, 41002, 41002)]


def test_mounted_home_already_owned_by_puid_64000_with_isolation_off(tmp_path, monkeypatch, home_ownership):
    home, data = tmp_path / "home", tmp_path / "data"
    data.mkdir()
    volume = home / ".local"
    volume.mkdir(parents=True)
    changed = home_ownership({home: (64000, 64000), volume: (64000, 64000)})
    monkeypatch.setenv("AGENT_ISOLATION", "off")
    monkeypatch.setattr(runtime, "mount_points", lambda: {volume})
    runtime.remap_home(home, data, 64000, 64000, 64000, 64000)
    assert changed == []


@pytest.mark.parametrize("owner", [0, 41003, 64000])
def test_mounted_home_root_preparation_without_image_id_change(tmp_path, monkeypatch, home_ownership, owner):
    home, data = tmp_path / "home", tmp_path / "data"
    data.mkdir()
    volume = home / ".local"
    volume.mkdir(parents=True)
    (volume / "own-file").write_text("fixture")
    changed = home_ownership({volume: (owner, owner), volume / "own-file": (owner, owner)})
    monkeypatch.setattr(runtime, "mount_points", lambda: {volume})
    if owner == 64000:
        with pytest.raises(runtime.RuntimeFailure, match="belongs to the service account.*PUID:PGID"):
            runtime.remap_home(home, data, 41002, 41002, 41002, 41002)
        assert changed == []
    else:
        runtime.remap_home(home, data, 41002, 41002, 41002, 41002)
        expected = {volume} if owner == 0 else {volume, volume / "own-file"}
        assert {entry[0] for entry in changed} == expected
        count = len(changed)
        runtime.remap_home(home, data, 41002, 41002, 41002, 41002)
        assert len(changed) == count


def test_storage_directory_creation_refuses_parent_swap(tmp_path, monkeypatch):
    parent, outside = tmp_path / "parent", tmp_path / "outside"
    parent.mkdir()
    outside.mkdir()
    original = os.mkdir
    def swap(component, *args, **kwargs):
        if component == "parent":
            parent.rename(tmp_path / "moved")
            parent.symlink_to(outside, target_is_directory=True)
        return original(component, *args, **kwargs)
    monkeypatch.setattr(runtime.os, "mkdir", swap)
    with pytest.raises(OSError):
        runtime.open_directory(parent / "runtime", create=True)
    assert not (outside / "runtime").exists()


def test_home_prepare_rejects_symlinked_mount_parent(tmp_path, monkeypatch, home_ownership):
    home, data, code = tmp_path / "home", tmp_path / "data", tmp_path / "code"
    home.mkdir()
    data.mkdir()
    (code / "gh").mkdir(parents=True)
    (home / ".config").symlink_to(code, target_is_directory=True)
    changed = home_ownership({code / "gh": (0, 0), home / ".config" / "gh": (0, 0)})
    # A cached mount path from before a concurrent parent rename/symlink swap.
    monkeypatch.setattr(runtime, "mount_points", lambda: {home / ".config" / "gh"})
    with pytest.raises(runtime.RuntimeFailure, match="home volume.*real directories"):
        runtime.remap_home(home, data, 41002, 41002, 41002, 41002)
    assert changed == []


@pytest.mark.parametrize("owner", [0, 41001])
def test_home_remap_rechecks_pinned_inode_after_entry_swap(tmp_path, monkeypatch, home_ownership, owner):
    home, data = tmp_path / "home", tmp_path / "data"
    home.mkdir()
    data.mkdir()
    (home / "victim").write_text("fixture")
    protected = tmp_path / "protected"
    protected.write_text("outside the walk")
    changed = home_ownership({protected: (owner, 41001)})
    original_open = os.open
    def swapped(name, flags, *args, **kwargs):
        return original_open(protected if name == "victim" else name, flags, *args, **kwargs)
    monkeypatch.setattr(runtime.os, "open", swapped)
    runtime.remap_home(home, data, 41001, 41001, 41002, 41002)
    assert changed == [(home, 41002, 41002)]


def test_mounted_home_migration_commits_root_last_and_resumes(tmp_path, monkeypatch, home_ownership):
    home, data = tmp_path / "home", tmp_path / "data"
    data.mkdir()
    volume = home / ".local"
    (volume / "sub").mkdir(parents=True)
    for path in (volume / "first", volume / "second", volume / "sub" / "tail"):
        path.write_text("fixture")
    entries = [volume, volume / "sub", volume / "first", volume / "second", volume / "sub" / "tail"]
    changed = home_ownership({path: (41003, 41003) for path in entries})
    monkeypatch.setattr(runtime, "mount_points", lambda: {volume})
    original = os.fchown
    class Interrupted(Exception):
        pass
    def interrupt(fd, uid, gid):
        if len(changed) == 2:
            raise Interrupted
        original(fd, uid, gid)
    monkeypatch.setattr(runtime.os, "fchown", interrupt)
    with pytest.raises(Interrupted):
        runtime.remap_home(home, data, 41002, 41002, 41002, 41002)
    assert volume.stat().st_uid == 41003
    monkeypatch.setattr(runtime.os, "fchown", original)
    runtime.remap_home(home, data, 41002, 41002, 41002, 41002)
    assert all(path.stat().st_uid == 41002 for path in entries)
    assert changed[-1] == (volume, 41002, 41002)


@pytest.mark.parametrize("raw", ["abc", "0", "standalone"])
def test_home_prepare_tolerates_unused_isolation_setting(tmp_path, monkeypatch, home_ownership, raw):
    home, data = tmp_path / "home", tmp_path / "data"
    home.mkdir()
    data.mkdir()
    changed = home_ownership()
    monkeypatch.setenv("AGENT_ISOLATION", "off")
    monkeypatch.setenv("AGENT_ISOLATION_SERVICE_UID", raw)
    if raw == "standalone":
        monkeypatch.setattr(runtime, "_isolation", None)
        monkeypatch.setattr(runtime, "_ISOLATION_FILE", tmp_path / "missing.py")
    runtime.remap_home(home, data, 41002, 41002, 41002, 41002)
    assert changed == []


def test_drop_refuses_interrupted_account_home(settings_factory, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(runtime.os, "geteuid", lambda: 0)
    monkeypatch.setattr(runtime.pwd, "getpwnam", lambda _: SimpleNamespace(pw_uid=41001, pw_gid=41001, pw_dir="/"))
    with pytest.raises(runtime.RuntimeFailure, match="appuser home must be /home/appuser"):
        runtime.drop_privileges(settings_factory())


def test_isolated_drop_keeps_full_agent_path_but_trusts_supervisor(settings_factory, tmp_path, monkeypatch):
    from types import SimpleNamespace
    effective = [0]
    tool_path = "/home/appuser/.local/bin:/usr/bin"
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("SHUFFLE_CONTAINER_PATH", tool_path)
    monkeypatch.delenv("PUID", raising=False)
    monkeypatch.delenv("PGID", raising=False)
    monkeypatch.setattr(runtime, "_agent_path", None)
    monkeypatch.setattr(runtime.os, "geteuid", lambda: effective[0])
    monkeypatch.setattr(runtime.os, "fchown", Mock())
    monkeypatch.setattr(runtime.pwd, "getpwnam", lambda _: SimpleNamespace(pw_uid=41001, pw_gid=41001, pw_dir="/home/appuser"))
    split = object()
    monkeypatch.setattr(runtime, "isolate", lambda *args: split)
    def drop(ident):
        assert ident is split and os.environ["PATH"] == "/usr/bin"
        effective[0] = 41001
    module = SimpleNamespace(drop=drop, trusted_path=lambda original: "/usr/bin" if original == tool_path else "wrong")
    monkeypatch.setattr(runtime, "isolation", lambda: module)
    runtime.drop_privileges(settings_factory(workspace_dir=tmp_path))
    assert runtime._agent_path == tool_path
    assert os.environ["PATH"] == "/usr/bin"
    assert "SHUFFLE_CONTAINER_PATH" not in os.environ


@pytest.mark.parametrize("action", ["dump", "restore"])
@pytest.mark.parametrize("puid", [99, 3456])
@pytest.mark.parametrize("state", ["empty", "populated", "symlink"])
def test_maintenance_prepares_only_empty_mapped_data_root(settings_factory, tmp_path, monkeypatch, home_ownership, action, puid, state):
    from types import SimpleNamespace
    data, workspace = tmp_path / "data", tmp_path / "unused-workspace"
    actual = tmp_path / "outside" if state == "symlink" else data
    actual.mkdir()
    if state == "symlink":
        data.symlink_to(actual, target_is_directory=True)
    elif state == "populated":
        (data / "sentinel").write_text("retained state")
    changed = home_ownership({actual: (1000, 1000), actual / "sentinel": (1000, 1000)})
    assert actual.stat().st_uid == 1000
    cfg = settings_factory(data_dir=data, workspace_dir=workspace)
    identity = [0, 0]
    pgid = 100
    monkeypatch.setenv("PUID", str(puid))
    monkeypatch.setenv("PGID", str(pgid))
    monkeypatch.setenv("AGENT_ISOLATION", "off")
    monkeypatch.setattr(runtime.os, "geteuid", lambda: identity[0])
    monkeypatch.setattr(runtime.os, "getuid", lambda: identity[0])
    monkeypatch.setattr(runtime.os, "getgid", lambda: identity[1])
    monkeypatch.setattr(runtime.os, "initgroups", Mock())
    monkeypatch.setattr(runtime.os, "setuid", lambda value: identity.__setitem__(0, value))
    monkeypatch.setattr(runtime.os, "setgid", lambda value: identity.__setitem__(1, value))
    monkeypatch.setattr(runtime.pwd, "getpwnam", lambda _: SimpleNamespace(
        pw_uid=puid, pw_gid=pgid, pw_name="appuser", pw_dir="/home/appuser"))
    monkeypatch.setattr(runtime.os, "access", lambda path, mode: Path(path).stat().st_uid == identity[0])
    monkeypatch.setattr(runtime, "maintenance_isolation", lambda *args: None)
    monkeypatch.setattr(runtime, "isolate", lambda *args: pytest.fail("maintenance must not migrate ownership"))
    monkeypatch.setattr(runtime, "load_startup_settings", lambda: cfg)
    monkeypatch.setattr(runtime, "load_settings", lambda: cfg)
    monkeypatch.setattr(runtime, "harden_process_introspection", lambda settings: None)
    calls = []
    monkeypatch.setattr(runtime, "run", lambda settings, action, file: calls.append((action, file, identity[:])) or 0)
    result = runtime.main([action, "--file", "/restore/db.dump"])
    if state == "empty":
        assert result == 0
        assert changed == [(data, puid, pgid)]
        assert calls == [(action, "/restore/db.dump", [puid, pgid])]
    else:
        assert result == 1
        assert changed == [] and calls == []
        assert actual.stat().st_uid == 1000
        if state == "populated":
            assert (data / "sentinel").read_text() == "retained state"
            assert (data / "sentinel").stat().st_uid == 1000
    assert not workspace.exists()  # database-only maintenance does not prepare it


def test_empty_custom_uid_mount_only_is_chowned(settings_factory, tmp_path, monkeypatch):
    from types import SimpleNamespace
    data = tmp_path / "empty"
    data.mkdir()
    workspace = tmp_path / "retained"
    workspace.mkdir()
    (workspace / "sentinel").write_text("preserve")
    uid = [0]
    monkeypatch.delenv("PUID", raising=False)
    monkeypatch.delenv("PGID", raising=False)
    monkeypatch.setattr(runtime.os, "geteuid", lambda: uid[0])
    monkeypatch.setattr(runtime.pwd, "getpwnam", lambda _: SimpleNamespace(pw_uid=1000, pw_gid=1000, pw_name="appuser", pw_dir="/home/appuser"))
    changed = []
    monkeypatch.setattr(runtime.os, "fchown", lambda fd, uid, gid: changed.append(
        (Path(os.readlink(f"/proc/self/fd/{fd}")), uid, gid)))
    monkeypatch.setattr(runtime.os, "initgroups", Mock())
    monkeypatch.setattr(runtime.os, "setgid", Mock())
    tool_path = "/home/appuser/.local/bin:/usr/bin"
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("SHUFFLE_CONTAINER_PATH", tool_path)
    monkeypatch.setattr(runtime, "isolate", lambda *args: None)
    def setuid(value):
        assert os.environ["PATH"] == "/usr/bin"  # root still uses image tools
        uid[0] = value
    monkeypatch.setattr(runtime.os, "setuid", setuid)
    runtime.drop_privileges(settings_factory(data_dir=data, workspace_dir=workspace))
    assert os.environ["PATH"] == tool_path
    assert "SHUFFLE_CONTAINER_PATH" not in os.environ
    assert changed == [(data, 1000, 1000)]
    assert (workspace / "sentinel").read_text() == "preserve"


def test_descriptor_written_atomically_and_consumed_by_backend(settings_factory, tmp_path):
    cfg = settings_factory()
    db = runtime.Database(cfg, port=6543)
    db.password = "synthetic-test-password"
    db.publish_descriptor()
    path = tmp_path / "managed-postgres.json"
    assert path.stat().st_mode & 0o777 == 0o600
    assert set(json.loads(path.read_text())) == {"version", "host", "port", "user", "password", "database"}
    assert cfg.get_database_connection()["port"] == 6543
    db.password = "replacement-synthetic"
    db.publish_descriptor()
    assert cfg.get_database_connection()["password"] == "replacement-synthetic"
    assert not list(tmp_path.glob("*.new-*"))


def test_client_environment_uses_normalized_credentials_not_inherited_pg(monkeypatch):
    monkeypatch.setenv("PGSERVICE", "production-must-not-be-read")
    monkeypatch.setenv("PGPASSFILE", "/never/read")
    env = runtime.client_env("postgresql://user:p%40ss%2Fword@127.0.0.1:6543/synthetic")
    assert env["PGPASSWORD"] == "p@ss/word"
    assert env["PGHOST"] == "127.0.0.1"
    assert env["PGPORT"] == "6543"
    assert "PGSERVICE" not in env and "PGPASSFILE" not in env


def test_dump_stdout_is_binary_and_diagnostics_stay_on_stderr(settings_factory, monkeypatch, capsys):
    db = runtime.Database(settings_factory())
    db.url = "postgresql://u:synthetic@127.0.0.1/test"
    command = Mock()
    monkeypatch.setattr(db, "command", command)
    db.dump("-")
    args, kwargs = command.call_args
    assert args[0] == ["pg_dump", "--format=custom", "--no-owner", "--no-privileges"]
    assert kwargs["output"] is not None
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "completed" in captured.err


def test_direct_user_mismatch_is_clear(settings_factory, monkeypatch):
    monkeypatch.setenv("PUID", str(os.geteuid() + 1))
    with pytest.raises(runtime.RuntimeFailure, match="direct --user"):
        runtime.drop_privileges(settings_factory())


@pytest.fixture
def pg_enabled():
    if os.environ.get("CONTAINER_RUNTIME_TEST_PG") != "1":
        pytest.skip("requires explicit disposable PostgreSQL 16 binaries opt-in")
    import socket
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    runtime.STOP.clear()
    return port


def test_real_pg_retains_identity_authenticates_and_dumps_online_offline(settings_factory, tmp_path, pg_enabled):
    import hashlib
    cfg = settings_factory()
    db = runtime.Database(cfg, port=pg_enabled)
    try:
        db.open()
        db.publish_descriptor()
        import psycopg2
        with psycopg2.connect(**cfg.get_database_connection()) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT current_user, inet_server_addr()")
                assert cursor.fetchone() == ("ai_shuffle", "127.0.0.1")
        assert db.query("SHOW server_version_num").startswith("16")
        assert db.query("SELECT rolsuper FROM pg_roles WHERE rolname=current_user") == "f"
        assert db.query("SHOW listen_addresses") == "127.0.0.1"
        assert db.query("SELECT rolpassword LIKE 'SCRAM-SHA-256$%' FROM pg_authid WHERE rolname='ai_shuffle'", admin=True) == "t"
        db.query("CREATE TABLE synthetic_probe (value text); INSERT INTO synthetic_probe VALUES ('retained');")
        identity = db.identifier
        password_digest = hashlib.sha256((db.pg / "password").read_bytes()).hexdigest()
        assert (db.pg / "password").stat().st_mode & 0o777 == 0o600
        online = runtime.Database(cfg)
        try:
            online.open(purpose="dump")
            online.dump(str(tmp_path.parent / (tmp_path.name + "-online.dump")))
        finally:
            online.close()
        assert db.query("SELECT value FROM synthetic_probe") == "retained"
        import subprocess
        import sys
        env = {k: v for k, v in os.environ.items() if not k.startswith("POSTGRES_") and k not in {"DATABASE_URL", "DATABASE_MODE", "AI_SHUFFLE_ENV_FILE"}}
        env.pop("PYTHONPATH", None)
        env.update(AI_SHUFFLE_ENV_FILE="/dev/null", DATA_DIR=str(tmp_path))
        streamed = subprocess.run([sys.executable, "-I", str(Path(runtime.__file__)), "dump", "--file", "-"], env=env, capture_output=True, timeout=20)
        assert streamed.returncode == 0, streamed.stderr.decode()
        assert streamed.stdout.startswith(b"PGDMP")
        assert db.password.encode() not in streamed.stderr
        contender = runtime.Database(cfg)
        try:
            with pytest.raises(runtime.RuntimeFailure, match="another supervisor"):
                contender.open()
        finally:
            contender.close()
    finally:
        db.close()
    assert not (db.data / "postmaster.pid").exists()
    retained = runtime.Database(cfg, port=pg_enabled)
    try:
        retained.open(purpose="dump")
        assert retained.identifier == identity
        assert hashlib.sha256((retained.pg / "password").read_bytes()).hexdigest() == password_digest
        assert retained.query("SELECT value FROM synthetic_probe") == "retained"
        archive = tmp_path.parent / (tmp_path.name + "-offline.dump")
        retained.dump(str(archive))
        assert archive.read_bytes().startswith(b"PGDMP")
        assert archive.stat().st_mode & 0o777 == 0o600
    finally:
        retained.close()
    control = retained.data / "global" / "pg_control"
    original = control.read_bytes()
    corrupted = bytearray(original)
    corrupted[100] ^= 1
    control.write_bytes(corrupted)
    broken = runtime.Database(cfg, port=pg_enabled)
    try:
        with pytest.raises(runtime.RuntimeFailure, match="invalid PostgreSQL control data"):
            broken.open()
        assert control.read_bytes() == corrupted
    finally:
        broken.close()


def test_real_pg_failed_role_ddl_never_logs_generated_password(settings_factory, pg_enabled):
    db = runtime.Database(settings_factory(), port=pg_enabled)
    try:
        db.open()
        assert db.query("SHOW log_statement") == "none"
        assert db.query("SHOW log_min_error_statement") == "panic"
        logpath = db.pg / "server.log"
        previous_log_size = logpath.stat().st_size
        # Bootstrap-style DDL can fail (the role already exists here). Keep
        # PostgreSQL's error diagnostic, but never its password-bearing SQL.
        with pytest.raises(runtime.RuntimeFailure, match="psql failed") as caught:
            db.query(f"CREATE ROLE {runtime.APP_USER} LOGIN PASSWORD '{db.password}';", admin=True)
        failure_log = logpath.read_bytes()[previous_log_size:]
        assert b'role "ai_shuffle" already exists' in failure_log
        assert b"CREATE ROLE" not in failure_log
        assert db.password.encode() not in logpath.read_bytes()
        assert db.password not in str(caught.value)
        assert db.query() == "1"
    finally:
        db.close()


def test_real_pg_update_copy_is_taken_put_back_and_dropped(settings_factory, pg_enabled):
    import psycopg2
    cfg = settings_factory()
    db = runtime.Database(cfg, port=pg_enabled)
    snapshot, discard = "ai_shuffle_rollback_" + "b" * 12, "ai_shuffle_failed_" + "b" * 12
    try:
        db.open()
        db.publish_descriptor()
        db.query("CREATE TABLE kept (value text); INSERT INTO kept VALUES ('before');")
        free, need = db.snapshot_space()
        assert free > 0 and need >= runtime.SNAPSHOT_MARGIN
        # A connection the supervisor did not close in time is ended, not waited on.
        lingering = psycopg2.connect(**cfg.get_database_connection())
        try:
            db.snapshot(snapshot)
        finally:
            lingering.close()
        assert db.update_databases() == [snapshot]
        db.query("CREATE TABLE migrated (value text); UPDATE kept SET value = 'after';")
        lingering = psycopg2.connect(**cfg.get_database_connection())
        try:
            db.restore_snapshot(snapshot, discard)
        finally:
            lingering.close()
        assert db.query("SELECT value FROM kept") == "before"
        assert db.query("SELECT to_regclass('migrated') IS NULL") == "t"
        assert db.update_databases() == [discard]
        db.restore_snapshot(snapshot, discard)  # repeated after an interruption: already done
        with pytest.raises(runtime.RuntimeFailure, match="copy taken before the update is missing"):
            db.restore_snapshot("ai_shuffle_rollback_" + "c" * 12, "ai_shuffle_failed_" + "c" * 12)
        db.drop_update_database(discard)
        assert db.update_databases() == []
        for name in ("ai_shuffle", "postgres", "ai_shuffle_rollback_x; DROP DATABASE ai_shuffle"):
            with pytest.raises(runtime.RuntimeFailure):
                db.drop_update_database(name)
        with pytest.raises(runtime.RuntimeFailure):
            db.snapshot("ai_shuffle_failed_" + "b" * 12)
        # The application role still reaches only its own database.
        db.snapshot(snapshot)
        with pytest.raises(psycopg2.OperationalError):
            psycopg2.connect(**dict(cfg.get_database_connection(), dbname=snapshot)).close()
        assert db.query("SELECT value FROM kept") == "before"
    finally:
        db.close()


def _signed_release_feed(monkeypatch, updater_module, tmp_path):
    """A test-signed release feed served to the real updater module."""
    import base64
    import hashlib
    import io
    import zipfile
    import httpx
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    key = Ed25519PrivateKey.generate()
    responses = {}

    def publish(revision, sequence):
        entries = {f"ai-shuffle-{revision}/source.txt": revision}
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            for name, value in entries.items():
                archive.writestr(name, value)
        archive = output.getvalue()
        files = [(name.split("/", 1)[1], hashlib.sha256(value.encode()).hexdigest()) for name, value in entries.items()]
        payload = json.dumps({"format": 2, "revision": revision, "sequence": sequence, "version": f"0.{sequence}.0",
                              "sha256": hashlib.sha256(archive).hexdigest(), "size": len(archive),
                              "container": {"sequence": sequence, "runtime": "d" * 64}, "channel": "stable",
                              "tree": hashlib.sha256(updater_module.tree_lines(files)).hexdigest()}).encode()
        responses["/v1/desktop/manifest"] = json.dumps({"payload": base64.b64encode(payload).decode(),
                                                        "signature": base64.b64encode(key.sign(payload)).decode()}).encode()
        responses["/v1/desktop/sources/" + revision] = archive

    client = httpx.Client
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=responses[request.url.path]))
    monkeypatch.setattr(updater_module.httpx, "Client", lambda **kwargs: client(transport=transport, **kwargs))
    public = key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
    return public, publish


def test_real_pg_update_that_crashes_after_migrating_is_rolled_back(settings_factory, tmp_path, pg_enabled,
                                                                     monkeypatch):
    """Acceptance: version A runs, update B migrates the database and then
    crashes on its first start. The supervisor puts the database back, runs A
    again and does not offer B automatically; the worker never stays down."""
    A, B = "a" * 40, "b" * 40
    module = runtime.load_updater()
    # The fixture runs on the host, not the image's /usr/local/bin/python.
    monkeypatch.setattr(module, "IMAGE_PYTHON", sys.executable)
    public, publish = _signed_release_feed(monkeypatch, module, tmp_path)
    marker = tmp_path / "started.log"
    programs = {
        # The previous version: records each start, then serves until stopped.
        A: ('import os, time\nfrom pathlib import Path\n'
            'with Path(os.environ["TEST_MARKER"]).open("a") as out: out.write(os.environ["GIT_SHA"] + "\\n")\n'
            'time.sleep(600)\n'),
        # The new version: migrates the schema, then crashes before it is ready.
        B: ('import subprocess\nsubprocess.run(["psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", "-c", '
            '"CREATE TABLE migrated (value text); UPDATE kept SET value = \'after\';"], check=True)\n'
            'raise SystemExit(3)\n'),
    }

    def build(directory, revision, version=None):
        for name in ("source/app/static/index.html", "complete"):
            (directory / name).parent.mkdir(parents=True, exist_ok=True)
            (directory / name).write_text(revision)
        python = directory / "venv/bin/python"
        python.parent.mkdir(parents=True, exist_ok=True)
        base = Path(module.IMAGE_PYTHON).resolve()
        python.symlink_to(base)
        (directory / "venv/pyvenv.cfg").write_text(f"home = {base.parent}\nexecutable = {base}\n")
        (directory / "source/app/main.py").write_text(programs[revision])

    cfg = settings_factory()
    db = runtime.Database(cfg, port=pg_enabled)
    app = None
    try:
        db.open()
        db.query("CREATE TABLE kept (value text); INSERT INTO kept VALUES ('before');")
        lifecycle = {"active-sessions": {"active_sessions": 0}, "stop-sessions": {"stopped": True}}
        updates = module.ContainerUpdates(tmp_path / "application-updates",
                                          {"revision": "c" * 40, "sequence": 1, "runtime": "d" * 64}, public,
                                          lambda: True, lambda action: lifecycle.get(action, {}), database=db)
        monkeypatch.setattr(updates, "build", build)
        # Version A is the installed update the worker runs today.
        publish(A, 2)
        updates.check(download=True)
        updates.activating()
        updates.running = A
        updates.healthy()
        assert updates.state == {"active": A} and db.update_databases() == []
        env = dict(runtime.client_env(db.url), PATH=os.environ["PATH"], TEST_MARKER=str(marker))
        command, cwd, child_env = updates.launch(env)
        app = runtime.subprocess.Popen(command, cwd=cwd, env=child_env, start_new_session=True)
        # B is released, downloaded and installed by the owner.
        publish(B, 3)
        updates.check(download=True)
        assert updates.state == {"active": A, "pending": B}
        updates.install_requested.set()
        current = {"app": app}

        def restart(process):
            current["app"] = runtime.restart_into_update(process, updates, env)
            return current["app"]

        def relaunch(process):
            current["app"] = runtime.start_current(process, updates, env)
            return current["app"]

        result = {}
        supervisor = threading.Thread(target=lambda: result.update(code=runtime.watch(
            app, db, None, runtime.ReadinessMonitor("http://127.0.0.1:9/health/ready"), updates, restart, relaunch)))
        supervisor.start()
        deadline = runtime.time.monotonic() + 60
        while runtime.time.monotonic() < deadline and supervisor.is_alive():
            if marker.exists() and len(marker.read_text().split()) >= 2:
                break
            runtime.time.sleep(0.1)
        runtime.STOP.set()
        supervisor.join(10)
        app = current["app"]
        assert result.get("code") == 0, "the supervisor kept the worker up"
        assert marker.read_text().split() == [A, A], "the previous version started again"
        assert app.poll() is None, "the previous version is still serving"
        assert updates.state == {"active": A, "failed": [B]}
        assert db.query("SELECT value FROM kept") == "before"
        assert db.query("SELECT to_regclass('migrated') IS NULL") == "t", "the migration was undone"
        assert db.update_databases() == [], "no copies are left behind"
        status = json.loads((updates.root / "status.json").read_text())
        assert status["phase"] == "error" and status["version"] == B[:12] and "0.3.0 failed to start" in status["error"]
        # The rolled-back release is not offered again by an automatic check.
        updates.check()
        assert updates.state == {"active": A, "failed": [B]}
        assert json.loads((updates.root / "status.json").read_text())["phase"] == "error"
    finally:
        runtime.STOP.clear()
        if app is not None and app.poll() is None:
            runtime.stop_group(app, 5)
        db.close()


def test_restore_preflight_and_restore_share_rewound_native_descriptor(monkeypatch, tmp_path, settings_factory):
    archive = tmp_path / "buffered.dump"
    archive.write_bytes(b"PGDMP" + b"synthetic archive bytes" * 300)
    db = Mock(mode="embedded")
    native_reads = []

    def read_native_header(source):
        # pg_restore inherits the raw descriptor, not Python's buffered cursor.
        native_reads.append(os.read(source.fileno(), 5))
        assert native_reads[-1] == b"PGDMP"

    def preflight(args, *, source, timeout):
        assert args == ["pg_restore", "--list"]
        read_native_header(source)

    db.command.side_effect = preflight
    db.restore.side_effect = read_native_header
    monkeypatch.setattr(runtime, "Database", lambda settings: db)
    assert runtime.run(settings_factory(), "restore", str(archive)) == 0
    assert native_reads == [b"PGDMP", b"PGDMP"]
    db.close.assert_called_once()


def test_real_pg_restore_empty_only_and_failure_fence(settings_factory, tmp_path, pg_enabled, monkeypatch):
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    source = runtime.Database(settings_factory(data_dir=source_dir), port=pg_enabled)
    archive = tmp_path / "synthetic.dump"
    try:
        source.open()
        source.query("CREATE TABLE restored_probe (value text); INSERT INTO restored_probe VALUES ('synthetic-only');")
        source.dump(str(archive))
    finally:
        source.close()
    target_dir = tmp_path / "target"
    target_dir.mkdir()
    target_settings = settings_factory(data_dir=target_dir)
    database_type = runtime.Database
    with monkeypatch.context() as patch:
        patch.setattr(runtime, "Database", lambda settings: database_type(settings, port=pg_enabled))
        # Exercise the CLI entry point's header check, native --list preflight,
        # descriptor rewind and actual restore, rather than skipping preflight.
        assert runtime.run(target_settings, "restore", str(archive)) == 0
    target = runtime.Database(target_settings, port=pg_enabled)
    try:
        target.open(purpose="restore")
        assert target.query("SELECT value FROM restored_probe") == "synthetic-only"
        assert not (target.pg / "restore-incomplete").exists()
        with archive.open("rb") as stream:
            with pytest.raises(runtime.RuntimeFailure, match="not verified empty"):
                target.restore(stream)
        target.query("DROP TABLE restored_probe")
        with (tmp_path / "broken.dump").open("wb") as stream:
            stream.write(b"PGDMP-broken")
        with (tmp_path / "broken.dump").open("rb") as stream:
            with pytest.raises(runtime.RuntimeFailure, match="pg_restore failed"):
                target.restore(stream)
        assert (target.pg / "restore-incomplete").exists()
    finally:
        target.close()
    fenced = runtime.Database(settings_factory(data_dir=target_dir), port=pg_enabled)
    try:
        with pytest.raises(runtime.RuntimeFailure, match="restore"):
            fenced.open()
    finally:
        fenced.close()


def _host_counters():
    cpu = [int(value) for value in Path("/proc/stat").read_text().splitlines()[0].split()[1:9]]
    disks = {}
    for line in Path("/proc/diskstats").read_text().splitlines():
        fields = line.split()
        name = fields[2]
        if (Path("/sys/block") / name).exists() and not name.startswith(("loop", "ram")):
            disks[name] = (int(fields[9]), int(fields[12]))  # written sectors, busy milliseconds
    return sum(cpu), cpu[3] + cpu[4], disks


def _host_delta(before, after, seconds):
    ticks = after[0] - before[0]
    return {
        "cpu_busy_percent": 100 * (1 - (after[1] - before[1]) / ticks) if ticks else 0,
        "disks": {name: {"written_bytes": (values[0] - before[2][name][0]) * 512,
                         "busy_percent": (values[1] - before[2][name][1]) / (seconds * 10)}
                  for name, values in after[2].items() if name in before[2]},
    }


def _wait_for_idle():
    import time
    # A bounded, measured guest-idle baseline, not a claim about the physical host.
    for _ in range(30):
        before, started = _host_counters(), time.monotonic()
        time.sleep(2)
        sample = _host_delta(before, _host_counters(), time.monotonic() - started)
        if sample["cpu_busy_percent"] < 10 and all(disk["busy_percent"] < 10 for disk in sample["disks"].values()):
            print(json.dumps({"phase": "guest-idle-baseline", **sample}))
            return
    pytest.fail(f"No guest-idle interval within 60 seconds: {sample}")


def test_checkpoint_host_delta_reports_cpu_and_disk_work():
    before = (100, 80, {"sda": (100, 1000)})
    after = (200, 170, {"sda": (104, 1100)})
    sample = _host_delta(before, after, 2)
    assert sample["cpu_busy_percent"] == pytest.approx(10)
    assert sample["disks"] == {"sda": {"written_bytes": 2048, "busy_percent": 5}}


def _measured_checkpoint(db, phase):
    import re
    import time
    logpath = db.pg / "server.log"
    offset = logpath.stat().st_size
    before = _host_counters()
    started = time.monotonic()
    try:
        db.query("CHECKPOINT", admin=True)
    finally:
        # Only checkpoint timing lines: never SQL, client output or credentials.
        with logpath.open("rb") as stream:
            stream.seek(offset)
            lines = stream.read().decode(errors="replace").splitlines()
        timings = [line[line.index("checkpoint "):] for line in lines
                   if re.search(r"LOG:  checkpoint (starting|complete):", line)]
        seconds = time.monotonic() - started
        print(json.dumps({"phase": phase, "seconds": seconds,
                          "host": _host_delta(before, _host_counters(), seconds),
                          "load_average": os.getloadavg(), "checkpoint_log": timings}))


def test_checkpoint_measurement_keeps_timeout_and_excludes_other_logs(tmp_path, capsys):
    db = Mock(pg=tmp_path)
    log = tmp_path / "server.log"
    log.write_text("previous log entry\n")

    def timeout(*args, **kwargs):
        with log.open("a") as stream:
            stream.write("LOG:  checkpoint starting: immediate force wait\n")
            stream.write("ERROR: synthetic-private-value\n")
        raise runtime.RuntimeFailure("database command interrupted or timed out")

    db.query.side_effect = timeout
    with pytest.raises(runtime.RuntimeFailure, match="timed out"):
        _measured_checkpoint(db, "test-timeout")
    db.query.assert_called_once_with("CHECKPOINT", admin=True)
    output = json.loads(capsys.readouterr().out)
    assert output["seconds"] >= 0
    assert output["checkpoint_log"] == ["checkpoint starting: immediate force wait"]
    assert "synthetic-private-value" not in json.dumps(output)


@pytest.mark.parametrize("writers", [0, 2], ids=["no-added-load", "disk-pressure"])
def test_real_pg_checkpoint_timings(settings_factory, tmp_path, pg_enabled, writers):
    """Opt-in bounded same-filesystem fsync pressure; keep the real 10s budget."""
    if os.environ.get("CONTAINER_RUNTIME_TEST_PRESSURE") != "1":
        pytest.skip("requires explicit disk-pressure measurement opt-in")
    import selectors
    import subprocess
    import sys
    db = runtime.Database(settings_factory(), port=pg_enabled)
    children = []
    writer = """import os, sys, time
from pathlib import Path
path = Path(sys.argv[1])
block = b'x' * (1024 * 1024)
started = time.monotonic()
written = 0
with path.open('wb', buffering=0) as stream:
    while time.monotonic() - started < 60:
        for _ in range(32):
            stream.write(block)
            written += len(block)
        os.fsync(stream.fileno())
        if written == 32 * len(block):
            print('ready', flush=True)
        stream.seek(0)
print(written, flush=True)
"""
    try:
        if writers == 0:
            _wait_for_idle()
        db.open()
        db.query("CREATE TABLE checkpoint_probe AS SELECT i, repeat(md5(i::text), 64) AS value FROM generate_series(1, 32768) i")
        for index in range(writers):
            child = subprocess.Popen([sys.executable, "-c", writer, str(tmp_path / f"pressure-{index}")],
                                     stdout=subprocess.PIPE, text=True, start_new_session=True)
            children.append(child)
            with selectors.DefaultSelector() as ready:
                ready.register(child.stdout, selectors.EVENT_READ)
                assert ready.select(timeout=10), "disk-pressure writer did not complete its first fsync"
                assert child.stdout.readline().strip() == "ready"
        for sample in range(3):
            db.query("UPDATE checkpoint_probe SET value = reverse(value)")
            assert all(child.poll() is None for child in children), "disk-pressure producer exited early"
            _measured_checkpoint(db, f"writers={writers},sample={sample}")
    finally:
        for child in children:
            child.terminate()
        for child in children:
            child.communicate(timeout=5)
        db.close()


def test_real_pg_app_fenced_then_sigkill_recovers_committed_wal(settings_factory, tmp_path, pg_enabled):
    import signal
    import subprocess
    import sys
    cfg = settings_factory()
    db = runtime.Database(cfg, port=pg_enabled)
    child = None
    try:
        db.open()
        db.query("CREATE TABLE crash_probe (value text);")
        _measured_checkpoint(db, "before-crash")
        # Commit after the checkpoint, then kill rather than gracefully stop:
        # the next startup must actually replay WAL, not initialize a new DB.
        db.query("BEGIN; INSERT INTO crash_probe VALUES ('committed-before-sigkill'); COMMIT;")
        retained = {path: path.read_bytes() for path in (
            db.pg / "cluster.json", db.pg / "password", db.mode_file,
        )}
        pid = db.data / "postmaster.pid"
        original_pid = pid.read_bytes()
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
        db.process.kill()
        assert db.process.wait(timeout=5) == -signal.SIGKILL
        assert runtime.watch(child, db, None) == 1
        # The dead database must not release the supervisor's lifetime lock
        # while the app is still being fenced.
        contender = runtime.Database(cfg, port=pg_enabled)
        try:
            with pytest.raises(runtime.RuntimeFailure, match="another supervisor"):
                contender.open()
        finally:
            contender.close()
    finally:
        runtime.shutdown(child, None, db)
    assert child.poll() is not None
    assert pid.read_bytes() == original_pid  # supervisor never removes it
    logpath = db.pg / "server.log"
    previous_log_size = logpath.stat().st_size
    retry = runtime.Database(cfg, port=pg_enabled)
    try:
        retry.open()
        assert retry.identifier == db.identifier
        assert retry.query("SELECT value FROM crash_probe") == "committed-before-sigkill"
        assert int(pid.read_text().splitlines()[0]) == retry.process.pid
        assert all(path.read_bytes() == value for path, value in retained.items())
        recovery_log = logpath.read_bytes()[previous_log_size:]
        assert b"database system was interrupted" in recovery_log
        assert b"redo starts at" in recovery_log
        assert b"redo done at" in recovery_log
    finally:
        retry.close()
    assert not pid.exists()  # only PostgreSQL's normal clean shutdown removes it


def test_real_pg_native_active_pid_refusal_preserves_state(settings_factory, tmp_path, pg_enabled):
    import subprocess
    import sys
    cfg = settings_factory()
    db = runtime.Database(cfg, port=pg_enabled)
    try:
        db.open()
        db.query("CREATE TABLE refusal_probe (value text); INSERT INTO refusal_probe VALUES ('preserved');")
        pid = db.data / "postmaster.pid"
        native_pid_lines = pid.read_text().splitlines(keepends=True)
    finally:
        db.close()
    retained = {path: path.read_bytes() for path in (
        db.pg / "cluster.json", db.pg / "password", db.mode_file,
        db.data / "global" / "pg_control",
    )}
    blocker = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
    retry = runtime.Database(cfg, port=pg_enabled)
    try:
        # Model a live/reused PID with a sibling process (not postgres's parent,
        # which PostgreSQL deliberately exempts). No manually launched database
        # outside the supervisor lock is supported by this container contract.
        native_pid_lines[0] = f"{blocker.pid}\n"
        runtime.write_private(pid, "".join(native_pid_lines))
        original_pid = pid.read_bytes()
        before = pid.stat()
        previous_log_size = (db.pg / "server.log").stat().st_size
        with pytest.raises(runtime.RuntimeFailure, match="exited before authenticated readiness"):
            retry.open()
        assert retry.process is not None and retry.process.returncode != 0
        assert blocker.poll() is None
        assert pid.read_bytes() == original_pid
        assert (pid.stat().st_ino, pid.stat().st_mtime_ns) == (before.st_ino, before.st_mtime_ns)
        assert all(path.read_bytes() == value for path, value in retained.items())
        refusal_log = (db.pg / "server.log").read_bytes()[previous_log_size:]
        assert b'lock file "postmaster.pid" already exists' in refusal_log
        assert not (db.pg / "initializing").exists()
    finally:
        try:
            retry.close()
        finally:
            runtime.stop_group(blocker, 5)
    assert pid.read_bytes() == original_pid  # cleanup did not repair it either
    # Once the conflicting process has exited, only PostgreSQL may replace its
    # own PID file; the same retained identity, credentials and data must work.
    recovered = runtime.Database(cfg, port=pg_enabled)
    try:
        recovered.open()
        assert recovered.identifier == db.identifier
        assert recovered.query("SELECT value FROM refusal_probe") == "preserved"
    finally:
        recovered.close()


def test_real_pg_wrong_retained_password_never_resets(settings_factory, tmp_path, pg_enabled):
    db = runtime.Database(settings_factory(), port=pg_enabled)
    try:
        db.open()
    finally:
        db.close()
    runtime.write_private(db.pg / "password", "f" * 96, replace=True)
    retry = runtime.Database(settings_factory(), port=pg_enabled)
    retry.wait_ready = lambda **kwargs: retry.query()
    try:
        with pytest.raises(runtime.RuntimeFailure, match="psql failed"):
            retry.open()
        assert (retry.pg / "password").read_text() == "f" * 96
    finally:
        retry.close()


# --- Hand-over to the active update's supervisor (#1821) ---------------------

PRODUCTION = {"format": 2, "revision": "a" * 40, "sequence": 5, "runtime": "r", "provides": ["base-1", "source-supervisor"]}


@pytest.fixture
def image_here(tmp_path, monkeypatch):
    """This file running as /app/docker/container_runtime.py of an image."""
    here = tmp_path / "app" / "docker"
    here.mkdir(parents=True)
    (here / "application-update-key.pem").write_bytes(b"key")
    monkeypatch.setattr(runtime, "__file__", str(here / "container_runtime.py"))
    for name in ("SHUFFLE_HANDED_OVER", "SHUFFLE_SOURCE_SUPERVISOR", "SHUFFLE_IMAGE_DIR", "SHUFFLE_HANDOVER"):
        monkeypatch.delenv(name, raising=False)

    def image(record):
        (here / "image.json").write_text(json.dumps(record))
    image(PRODUCTION)
    return here, image


def updater_with(target=None, error=None):
    def handover_target(root, image, key):
        assert key == b"key"
        if error:
            raise error
        return target
    module = type("Updater", (), {"handover_target": staticmethod(handover_target)})
    return lambda: module


def test_handover_starts_the_verified_update_with_a_trial_marker(settings_factory, image_here, tmp_path, monkeypatch):
    here, _ = image_here
    settings = settings_factory()
    (tmp_path / "application-updates").mkdir()
    python, script = tmp_path / "venv/bin/python", tmp_path / "source/docker/container_runtime.py"
    monkeypatch.setattr(runtime, "load_updater", updater_with(("b" * 40, python, script)))
    monkeypatch.setenv("PYTHONPATH", "/somewhere")
    execve = Mock()
    assert runtime.handover(settings, execve=execve) is True
    path, argv, env = execve.call_args.args
    assert (path, argv) == (str(python), [str(python), str(script), "run"])
    assert env["SHUFFLE_HANDED_OVER"] == "b" * 40 and env["SHUFFLE_IMAGE_DIR"] == str(here)
    assert "PYTHONPATH" not in env and env["PYTHONNOUSERSITE"] == "1"
    marker = tmp_path / "application-updates" / runtime.HANDOVER_TRIAL
    assert json.loads(marker.read_text()) == {"revision": "b" * 40}
    # The handed-over supervisor reads image.json and the key from the image.
    monkeypatch.setenv("SHUFFLE_HANDED_OVER", "b" * 40)
    monkeypatch.setenv("SHUFFLE_IMAGE_DIR", str(here))
    assert runtime.image_dir() == here
    # ...and never hands over again.
    assert runtime.handover(settings, execve=execve) is False and execve.call_count == 1


@pytest.mark.parametrize("record", [
    dict(PRODUCTION, provides=["base-1"]),  # an image without the hand-over
    dict(PRODUCTION, sequence=0),  # a development image
    dict(PRODUCTION, revision="dev"),
])
def test_images_without_the_handover_run_their_own_supervisor(settings_factory, image_here, monkeypatch, record):
    _, image = image_here
    image(record)
    monkeypatch.setattr(runtime, "load_updater", updater_with(error=AssertionError("not consulted")))
    execve = Mock()
    assert runtime.handover(settings_factory(), execve=execve) is False
    execve.assert_not_called()


def test_handover_can_be_switched_off(settings_factory, image_here, monkeypatch):
    monkeypatch.setenv("SHUFFLE_SOURCE_SUPERVISOR", "0")
    monkeypatch.setattr(runtime, "load_updater", updater_with(error=AssertionError("not consulted")))
    assert runtime.handover(settings_factory(), execve=Mock()) is False


@pytest.mark.parametrize("error", [None, RuntimeError("boom"), type("UpdateError", (Exception,), {})("blocked")])
def test_no_target_or_any_failure_falls_back_to_the_image_supervisor(settings_factory, image_here, monkeypatch, error, capsys):
    monkeypatch.setattr(runtime, "load_updater", updater_with(None, error))
    execve = Mock()
    assert runtime.handover(settings_factory(), execve=execve) is False
    execve.assert_not_called()
    if error is not None:
        assert "running this image's supervisor" in capsys.readouterr().err


def test_confirm_handover_clears_only_its_own_marker(tmp_path, monkeypatch):
    marker = tmp_path / runtime.HANDOVER_TRIAL
    marker.write_text(json.dumps({"revision": "c" * 40}))
    monkeypatch.setattr(runtime, "_handover_confirmed", False)
    monkeypatch.setenv("SHUFFLE_HANDED_OVER", "b" * 40)
    runtime.confirm_handover(tmp_path)
    assert marker.exists()
    monkeypatch.setattr(runtime, "_handover_confirmed", False)
    marker.write_text(json.dumps({"revision": "b" * 40}))
    runtime.confirm_handover(tmp_path)
    assert not marker.exists()


def test_confirm_handover_is_a_no_op_for_the_image_supervisor(tmp_path, monkeypatch):
    marker = tmp_path / runtime.HANDOVER_TRIAL
    marker.write_text(json.dumps({"revision": "b" * 40}))
    monkeypatch.setattr(runtime, "_handover_confirmed", False)
    monkeypatch.delenv("SHUFFLE_HANDED_OVER", raising=False)
    runtime.confirm_handover(tmp_path)
    assert marker.exists()


def test_chrome_falls_back_to_the_image_command_without_scripts_beside_it(image_here):
    assert runtime.chrome_command({"A": "1"}) == (["chrome-supervisor"], {"A": "1"}, None)
