"""Container lifecycle guards. No application startup or real configured database."""
import json
import os
from pathlib import Path
from unittest.mock import Mock

import pytest

from docker import container_runtime as runtime
from app.core.config import Settings


@pytest.fixture
def settings_factory(monkeypatch, tmp_path):
    for name in list(os.environ):
        if name.startswith("POSTGRES_") or name in {"DATABASE_URL", "DATABASE_MODE", "AI_SHUFFLE_ENV_FILE"}:
            monkeypatch.delenv(name)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    return lambda **kw: Settings(_env_file=None, **kw)


def test_fresh_defaults_to_embedded(settings_factory):
    assert runtime.select_mode(settings_factory()) == "embedded"


@pytest.mark.parametrize("field,value", [("postgres_password", "synthetic"), ("postgres_port", 5432), ("postgres_host", ""), ("postgres_user", "ai_shuffle")])
def test_explicit_components_select_external(settings_factory, field, value):
    assert runtime.select_mode(settings_factory(**{field: value})) == "external"


def test_embedded_conflict_and_invalid_mode(settings_factory):
    with pytest.raises(runtime.RuntimeFailure, match="conflict"):
        runtime.select_mode(settings_factory(database_mode="embedded", postgres_password="synthetic"))
    with pytest.raises(runtime.RuntimeFailure, match="DATABASE_MODE"):
        runtime.select_mode(settings_factory(database_mode="typo"))


def test_env_file_settings_and_url_precedence(settings_factory, tmp_path, monkeypatch):
    envfile = tmp_path / "synthetic-config"
    envfile.write_text("POSTGRES_PASSWORD=from-file\nDATABASE_URL=postgres://REDACTED@db:5544/name\nDATABASE_MODE=auto\n")
    monkeypatch.setenv("AI_SHUFFLE_ENV_FILE", str(envfile))
    cfg = runtime.load_settings()
    assert runtime.select_mode(cfg) == "external"
    assert cfg.get_database_url() == "postgres://REDACTED@db:5544/name"


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


def test_external_never_inspects_managed_storage(settings_factory, tmp_path, monkeypatch):
    forbidden = tmp_path / "postgres"
    forbidden.symlink_to("/definitely/not/a/cluster")
    db = runtime.Database(settings_factory(postgres_password="synthetic"))
    monkeypatch.setattr(db, "query", Mock(return_value="1"))
    db.open()
    db.close()
    assert forbidden.is_symlink()
    assert json.loads((tmp_path / "database-mode.json").read_text())["mode"] == "external"


def test_dump_never_initializes_and_restore_external_is_forbidden(settings_factory, tmp_path):
    with pytest.raises(runtime.RuntimeFailure, match="retained"):
        runtime.Database(settings_factory()).open(purpose="dump")
    assert not (tmp_path / "postgres").exists()
    with pytest.raises(runtime.RuntimeFailure, match="external restore"):
        runtime.Database(settings_factory(postgres_password="synthetic")).open(purpose="restore")


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


def test_failed_external_launch_is_recorded_without_pg_storage(settings_factory, tmp_path, monkeypatch):
    db = runtime.Database(settings_factory(postgres_password="synthetic"))
    monkeypatch.setattr(db, "query", Mock(side_effect=runtime.RuntimeFailure("authentication refused")))
    with pytest.raises(runtime.RuntimeFailure, match="authentication refused"):
        db.open()
    assert json.loads((tmp_path / "database-mode.json").read_text())["mode"] == "external"
    assert not (tmp_path / "postgres").exists()
    with pytest.raises(runtime.RuntimeFailure, match="external"):
        runtime.Database(settings_factory()).open()


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
    db.url = "postgresql://REDACTED@localhost/test"
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
        env.update(AI_SHUFFLE_ENV_FILE="/dev/null", DATA_DIR=str(tmp_path), PYTHONPATH=str(Path(__file__).resolve().parents[2]))
        streamed = subprocess.run([sys.executable, str(Path(runtime.__file__)), "dump", "--file", "-"], env=env, capture_output=True, timeout=20)
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


def test_real_pg_restore_empty_only_and_failure_fence(settings_factory, tmp_path, pg_enabled):
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
    target = runtime.Database(settings_factory(data_dir=target_dir), port=pg_enabled)
    try:
        target.open(purpose="restore")
        with archive.open("rb") as stream:
            target.restore(stream)
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
