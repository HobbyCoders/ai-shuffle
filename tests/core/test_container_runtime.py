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
    monitor = runtime.ReadinessMonitor("http://127.0.0.1:8000/health/ready", probe=lambda url: answers.pop(0), failures=4, startup_failures=6)
    answers[:] = [True, False, False, False, True, False, False, False, False]
    assert [monitor.check() for _ in range(8)] == [False] * 8
    assert monitor.failures == 3
    assert monitor.check() is True
    assert monitor.failures == 4
    assert runtime.READY_PROBE_FAILURES * runtime.READY_PROBE_INTERVAL == 120, "an unhealthy worker is restarted after two minutes"
    assert runtime.READY_PROBE_STARTUP_FAILURES >= runtime.READY_PROBE_FAILURES


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
    monkeypatch.setattr(runtime, "watch", lambda app, db, chrome, monitor=None, updates=None, restart=None, relaunch=None: seen.update(url=monitor.url) or 0)
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
    monkeypatch.setattr(runtime, "watch", lambda *args: 0)
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
    monkeypatch.setattr(runtime, "watch", lambda *args: 0)
    monkeypatch.setattr(runtime, "shutdown", Mock())
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
    monkeypatch.setattr(runtime, "load_settings", lambda: cfg)
    monkeypatch.setattr(runtime, "drop_privileges", lambda settings: order.append("drop"))
    monkeypatch.setattr(runtime, "harden_process_introspection", lambda settings: order.append("harden"))
    monkeypatch.setattr(runtime, "run", lambda settings, action, filename=None: order.append(action) or 0)
    assert runtime.main(["run"]) == 0
    assert order == ["drop", "harden", "run"]


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
    monkeypatch.setattr(runtime.pwd, "getpwnam", lambda _: SimpleNamespace(pw_uid=1000, pw_gid=1000, pw_name="appuser", pw_dir=str(tmp_path)))
    chown = Mock()
    monkeypatch.setattr(runtime.os, "chown", chown)
    monkeypatch.setattr(runtime.os, "initgroups", Mock())
    monkeypatch.setattr(runtime.os, "setgid", Mock())
    monkeypatch.setattr(runtime.os, "setuid", lambda value: uid.__setitem__(0, value))
    runtime.drop_privileges(settings_factory(data_dir=data, workspace_dir=workspace))
    chown.assert_called_once_with(data, 1000, 1000)
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
    public, publish = _signed_release_feed(monkeypatch, module, tmp_path)
    marker = tmp_path / "started.log"
    programs = {
        # The previous version: records each start, then serves until stopped.
        A: '#!/bin/sh\necho "$GIT_SHA" >> "$TEST_MARKER"\nexec sleep 600\n',
        # The new version: migrates the schema, then crashes before it is ready.
        B: ('#!/bin/sh\npsql -X -q -v ON_ERROR_STOP=1 -c "CREATE TABLE migrated (value text);'
            ' UPDATE kept SET value = \'after\';" || exit 9\nexit 3\n'),
    }

    def build(directory, revision, version=None):
        for name in ("source/app/static/index.html", "complete"):
            (directory / name).parent.mkdir(parents=True, exist_ok=True)
            (directory / name).write_text(revision)
        python = directory / "venv/bin/python"
        python.parent.mkdir(parents=True, exist_ok=True)
        python.write_text(programs[revision])
        python.chmod(0o755)

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
