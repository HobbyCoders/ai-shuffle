"""Container lifecycle guards. No application startup or real configured database."""
import importlib.util
import json
import os
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
    monkeypatch.setattr(runtime, "watch", lambda app, db, chrome, monitor=None: seen.update(url=monitor.url) or 0)
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
        db.query("CHECKPOINT", admin=True)
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
