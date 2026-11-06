"""Disposable PostgreSQL must not allocate POSIX DSM, even on forced teardown."""
import json
import os
import signal
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

pytest.importorskip("fcntl", reason="Linux container PostgreSQL supervisor")

from tests.db import schema_lifecycle_helpers as lifecycle
from tests.smoke.test_app_boot import _load_container_runtime


@pytest.mark.parametrize("disposable", [False, True])
def test_dsm_setting_is_explicit_and_applies_to_initdb_and_retained_start(tmp_path, monkeypatch, disposable):
    runtime = _load_container_runtime()
    db = runtime.Database(SimpleNamespace(effective_data_dir=tmp_path), disposable=disposable)
    db.pg.mkdir()
    commands = []
    db.command = lambda args, **kwargs: commands.append(args) or "16.1"
    db.control_identifier = lambda: "test-id"
    db.start = Mock()
    db.wait_ready = Mock()
    db.query = Mock()
    db.initialize()
    init = next(args for args in commands if "-D" in args and args[0] == "initdb")
    assert ("dynamic_shared_memory_type=mmap" in init) is disposable
    # Exercise the real start() on a retained cluster as well, not just initdb.
    del db.start
    spawn = Mock()
    monkeypatch.setattr(runtime.subprocess, "Popen", spawn)
    try:
        db.start()
        args = spawn.call_args.args[0]
        assert ("dynamic_shared_memory_type=mmap" in args) is disposable
    finally:
        db.pg_log.close()
        db.private_runtime.cleanup()


@pytest.mark.parametrize("free,total,warns", [(5, 100, True), (10, 100, True), (11, 100, False)])
def test_low_shm_warning_is_read_only_and_explains_isolation(monkeypatch, capsys, free, total, warns):
    runtime = _load_container_runtime()
    monkeypatch.setattr(runtime.shutil, "disk_usage", lambda path: SimpleNamespace(free=free, total=total))
    runtime.warn_disposable_shm_pressure()
    output = capsys.readouterr().err
    assert ("/dev/shm" in output) is warns
    if warns:
        assert "mmap" in output
        assert "not removed" in output


def test_missing_shm_mount_does_not_block_start(monkeypatch):
    runtime = _load_container_runtime()
    monkeypatch.setattr(runtime.shutil, "disk_usage", Mock(side_effect=FileNotFoundError))
    runtime.warn_disposable_shm_pressure()


@pytest.mark.skipif(sys.platform != "linux", reason="/dev/shm and /proc lifecycle check")
@pytest.mark.parametrize("stop", ["close", "ttl", "guardian", "restart"])
def test_real_disposable_stop_leaves_no_postgres_shm(tmp_path, monkeypatch, stop, record_property):
    from app.preview_run import free_port, supervise
    from scripts.devloop import guardian

    bindir = lifecycle.pg16_bindir()
    if bindir is None:
        pytest.skip("PostgreSQL 16 server binaries not found")
    monkeypatch.setenv("PATH", bindir + os.pathsep + os.environ.get("PATH", ""))
    runtime = _load_container_runtime()
    runtime.STOP.clear()
    scratch = tmp_path / "ai-shuffle-preview-shm"
    scratch.mkdir()
    (scratch / guardian.SCRATCH_MARKER).write_text(json.dumps(
        {"pid": 999001, "start": "owned-test", "uid": os.getuid()}))
    data = scratch / "data"
    data.mkdir()
    class ThrowawayDatabase(runtime.Database):
        def command(self, args, **kwargs):
            # Durability is irrelevant to DSM ownership; skip the same costly
            # sync/checkpoint work as the shared private_postgres test helper.
            if args[0] == "initdb" and "-D" in args:
                args = [*args, "--no-sync", "-c", "fsync=off"]
            return super().command(args, **kwargs)

    db = ThrowawayDatabase(SimpleNamespace(effective_data_dir=data),
                           port=free_port(), disposable=True)
    shm = Path("/dev/shm")
    before = set(shm.glob("PostgreSQL.*"))
    owned_segments = set()

    def check_running():
        assert db.query("SHOW dynamic_shared_memory_type", admin=True) == "mmap"
        # Exercise the DSM-producing path: a forced parallel aggregate, not
        # just a serializer or a seeded setting. Verify workers were launched.
        plan = db.query("SET max_parallel_workers_per_gather=2; SET min_parallel_table_scan_size=0; "
                        "SET parallel_setup_cost=0; SET parallel_tuple_cost=0; "
                        "EXPLAIN ANALYZE SELECT sum(x) FROM shm_probe", admin=True)
        assert "Workers Launched:" in plan and "Workers Launched: 0" not in plan
        # Attribute names to this cluster rather than asserting global equality:
        # another disposable test may legitimately start on the same host.
        maps = Path(f"/proc/{db.process.pid}/maps").read_text()
        for entry in maps.splitlines():
            path = entry.split()[-1]
            if path.startswith("/dev/shm/PostgreSQL."):
                owned_segments.add(Path(path))
        assert not owned_segments, maps

    try:
        db.open()
        db.query("CREATE TABLE shm_probe AS SELECT generate_series(1, 100000) AS x; "
                 "ANALYZE shm_probe", admin=True)
        check_running()
        if stop == "ttl":
            # Reuse the open, real cluster under the actual preview TTL lease.
            monkeypatch.setattr(db, "open", lambda: None)
            supervise([sys.executable, "-c", "import time; time.sleep(60)"],
                      {"PATH": os.environ["PATH"]}, db, ttl=.1, owner_pid=os.getpid())
        elif stop == "guardian":
            # Actual guardian ownership checks and SIGKILL fallback. mmap must
            # remain safe even when Python/PG clean shutdown cannot run.
            guardian.cleanup(tmp_path / "absent-status.json", scratch, 999001, "owned-test")
            db.process.wait(timeout=5)
            assert db.process.returncode == -signal.SIGKILL
        else:
            db.close()
            if stop == "restart":
                # Pause/wake reopens a retained cluster, not a new initdb.
                db = ThrowawayDatabase(SimpleNamespace(effective_data_dir=data),
                                       port=free_port(), disposable=True)
                db.open()
                check_running()
                db.close()
        after = set(shm.glob("PostgreSQL.*"))
        record_property("postgres_shm_before_count", len(before))
        record_property("postgres_shm_after_count", len(after))
        print(f"{stop}: PostgreSQL.* count before={len(before)} after={len(after)}")
        assert not (after & owned_segments)
        # No private DSM files live in the shared mount, including after a kill.
        assert not owned_segments
    finally:
        db.close()
