"""Update downtime telemetry, without starting an app or a configured database."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

pytest.importorskip("fcntl", reason="The container supervisor is Linux-only")
_spec = importlib.util.spec_from_file_location(
    "snapshot_timing_runtime", Path(__file__).resolve().parents[2] / "docker" / "container_runtime.py",
)
assert _spec is not None and _spec.loader is not None
runtime = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(runtime)
SNAPSHOT = "ai_shuffle_rollback_" + "b" * 12


@pytest.fixture
def db(tmp_path):
    database = runtime.Database(SimpleNamespace(effective_data_dir=tmp_path))
    database.admin_script = Mock()
    database.query = Mock(side_effect=["4294967296", "4294967296"])
    return database


def test_snapshot_brackets_one_copy_with_sizes_and_separate_copy_duration(db, monkeypatch, capsys):
    monkeypatch.setattr(runtime.time, "monotonic", Mock(side_effect=[10.0, 11.0, 14.0, 15.0]))
    db.snapshot(SNAPSHOT)
    output = capsys.readouterr().err
    assert "database snapshot starting strategy=FILE_COPY" in output
    assert "source_database_bytes=4294967296" in output
    assert "database snapshot complete" in output
    assert "duration_seconds=5.000" in output
    assert "copy_seconds=3.000" in output
    assert "copied_database_bytes=4294967296" in output
    assert "attempts=1" in output
    assert db.query.call_args_list == [
        (("SELECT pg_database_size('ai_shuffle')",), {"admin": True}),
        ((f"SELECT pg_database_size('{SNAPSHOT}')",), {"admin": True}),
    ]
    scripts = [call.args[0] for call in db.admin_script.call_args_list]
    # Ratchet: telemetry must not add another copy or change its strategy.
    assert len(scripts) == 2
    assert sum("CREATE DATABASE" in sql for sql in scripts) == 1
    assert "STRATEGY FILE_COPY" in scripts[-1]
    assert db.admin_script.call_args_list[-1].kwargs == {"timeout": runtime.SNAPSHOT_TIMEOUT}


@pytest.mark.parametrize("failure", [runtime.RuntimeFailure("private diagnostic"), ValueError("private diagnostic")])
def test_size_telemetry_failure_cannot_block_or_invalidate_a_snapshot(db, monkeypatch, capsys, failure):
    db.query.side_effect = failure
    db.snapshot(SNAPSHOT)
    assert db.admin_script.call_count == 2
    output = capsys.readouterr().err
    assert "database snapshot complete" in output
    assert "source_database_bytes=unknown" in output
    assert "copied_database_bytes=unknown" in output
    assert "private diagnostic" not in output


def test_failed_snapshot_is_bracketed_without_claiming_copied_bytes(db, monkeypatch, capsys):
    monkeypatch.setattr(runtime, "STOP", SimpleNamespace(is_set=lambda: False))
    db.admin_script.side_effect = [None] + [runtime.RuntimeFailure("private diagnostic")] * 3
    with pytest.raises(runtime.RuntimeFailure):
        db.snapshot(SNAPSHOT)
    output = capsys.readouterr().err
    assert "database snapshot failed" in output
    assert "attempts=3" in output
    assert "copied_database_bytes=unknown" in output
    assert "database snapshot complete" not in output
    assert "private diagnostic" not in output
    assert db.query.call_count == 1


def test_snapshot_retry_measures_all_attempts(db, monkeypatch, capsys):
    monkeypatch.setattr(runtime, "STOP", SimpleNamespace(is_set=lambda: False))
    monkeypatch.setattr(runtime.time, "monotonic", Mock(side_effect=[10.0, 11.0, 13.0, 14.0, 17.0, 18.0]))
    db.admin_script.side_effect = [None, runtime.RuntimeFailure("retry"), None]
    db.snapshot(SNAPSHOT)
    output = capsys.readouterr().err
    assert "database snapshot complete" in output
    assert "attempts=2" in output
    assert "copy_seconds=5.000" in output
    assert "duration_seconds=8.000" in output


@pytest.mark.parametrize("failure", [False, True])
def test_spawn_logs_bracket_popen_without_command_environment_or_paths(monkeypatch, capsys, failure):
    monkeypatch.setattr(runtime, "isolation", lambda: SimpleNamespace(ACTIVE=None))
    monkeypatch.setattr(runtime.time, "monotonic", Mock(side_effect=[10.0, 10.125]))
    child = SimpleNamespace(pid=123)
    spawn = Mock(side_effect=OSError("private diagnostic")) if failure else Mock(return_value=child)
    monkeypatch.setattr(runtime.subprocess, "Popen", spawn)
    command, cwd, env = ["private command"], "private path", {"PRIVATE": "private value"}
    if failure:
        with pytest.raises(OSError):
            runtime.start_app(command, cwd, env)
    else:
        assert runtime.start_app(command, cwd, env) is child
    spawn.assert_called_once_with(command, cwd=cwd, env=env, start_new_session=True)
    output = capsys.readouterr().err
    assert "application child spawn starting" in output
    assert "duration_seconds=0.125" in output
    assert ("application child spawn failed" if failure else "application child spawned pid=123") in output
    assert "private" not in output


@pytest.fixture
def stderr_fault(monkeypatch):
    """A real print/write failure, isolated from pytest's own stderr."""
    def fail_on(fragment):
        writes = []

        def write(text):
            writes.append(text)
            if fragment in text:
                raise OSError("diagnostic sink unavailable")
            return len(text)

        stream = SimpleNamespace(write=write, flush=lambda: None)
        monkeypatch.setattr(runtime, "sys", SimpleNamespace(stderr=stream))
        return writes

    return fail_on


@pytest.mark.parametrize("fragment", [
    "database snapshot starting",
    "source_database_bytes=",
    "database snapshot complete",
    "",  # a permanently unavailable diagnostic sink
])
def test_snapshot_stderr_failure_cannot_prevent_or_invalidate_the_copy(db, stderr_fault, fragment):
    writes = stderr_fault(fragment)
    db.snapshot(SNAPSHOT)
    assert any(fragment in text for text in writes)
    assert db.admin_script.call_count == 2
    assert db.query.call_count == 2
    assert "CREATE DATABASE" in db.admin_script.call_args.args[0]


def test_failed_snapshot_stderr_failure_does_not_mask_the_original_error(db, monkeypatch, stderr_fault):
    monkeypatch.setattr(runtime, "STOP", SimpleNamespace(is_set=lambda: False))
    original = runtime.RuntimeFailure("snapshot failed")
    db.admin_script.side_effect = [None, original, original, original]
    writes = stderr_fault("database snapshot failed")
    with pytest.raises(runtime.RuntimeFailure) as caught:
        db.snapshot(SNAPSHOT)
    assert caught.value is original
    assert any("database snapshot failed" in text for text in writes)
    assert db.admin_script.call_count == 4
    assert db.query.call_count == 1


@pytest.mark.parametrize("fragment", ["application child spawn starting", "application child spawned", ""])
def test_update_stderr_failure_still_returns_the_live_candidate(monkeypatch, tmp_path, stderr_fault, fragment):
    """Never lose the candidate and let fallback roll back beneath a live writer."""
    writes = stderr_fault(fragment)
    monkeypatch.setattr(runtime, "isolation", lambda: SimpleNamespace(ACTIVE=None))
    current, candidate = SimpleNamespace(pid=122), SimpleNamespace(pid=123, poll=lambda: None)
    spawn = Mock(return_value=candidate)
    monkeypatch.setattr(runtime.subprocess, "Popen", spawn)
    stop = Mock()
    monkeypatch.setattr(runtime, "stop_group", stop)
    command, env = ["candidate"], {"A": "1"}
    updates = SimpleNamespace(
        root=tmp_path,
        lifecycle=Mock(side_effect=[{"active_sessions": 0}, {"stopped": True}]),
        launch=Mock(return_value=(command, tmp_path, env)),
        snapshot_room=Mock(),
        activating=Mock(),
    )

    tracked = runtime.restart_into_update(current, updates, {})

    assert tracked is candidate and tracked.poll() is None
    assert any(fragment in text for text in writes)
    stop.assert_called_once_with(current, 30)
    updates.activating.assert_called_once_with()
    spawn.assert_called_once_with(command, cwd=tmp_path, env=env, start_new_session=True)


@pytest.mark.parametrize("fragment", ["application child spawn starting", "application child spawn failed", ""])
def test_spawn_stderr_failure_does_not_mask_the_original_error(monkeypatch, stderr_fault, fragment):
    writes = stderr_fault(fragment)
    monkeypatch.setattr(runtime, "isolation", lambda: SimpleNamespace(ACTIVE=None))
    original = OSError("candidate could not spawn")
    spawn = Mock(side_effect=original)
    monkeypatch.setattr(runtime.subprocess, "Popen", spawn)
    with pytest.raises(OSError) as caught:
        runtime.start_app(["candidate"], None, {})
    assert caught.value is original
    assert any(fragment in text for text in writes)
    spawn.assert_called_once_with(["candidate"], cwd=None, env={}, start_new_session=True)
