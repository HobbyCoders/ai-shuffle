"""Preview lease teardown owns real subprocesses, never another session's app."""
import os
import sys
import threading
from pathlib import Path
from unittest.mock import Mock

import psutil
import pytest

from app.preview_run import preview_environment, supervise

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux preview ownership")


def test_preview_environment_never_inherits_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", "/not-owned")
    monkeypatch.setenv("DATABASE_URL", "do-not-inherit")
    monkeypatch.setenv("AI_SHUFFLE_ENV_FILE", "/not-owned")
    env = preview_environment(tmp_path, 18880, 18881)
    assert "DATABASE_URL" not in env
    assert env["DATA_DIR"] == str(tmp_path / "data")
    assert Path(env["AI_SHUFFLE_ENV_FILE"]).read_text() == ""
    assert env["HOME"] == str(tmp_path / "home")
    assert env["LOCAL_MODE"] == "true"
    assert env["AI_SHUFFLE_PREVIEW"] == "1"


@pytest.mark.parametrize("reason", ["ttl", "stop", "error", "owner_exit", "pid_reused"])
def test_lease_closes_owned_database_and_worker(tmp_path, reason, monkeypatch):
    record = tmp_path / "pid"
    env = preview_environment(tmp_path, 18880, 18881)
    database = Mock()
    stopped = threading.Event()
    worker = ("import os,time,pathlib; "
              f"pathlib.Path({str(record)!r}).write_text(str(os.getpid())); time.sleep(60)")
    if reason == "stop":
        timer = threading.Timer(.5, stopped.set)
        timer.start()
    if reason in ("owner_exit", "pid_reused"):
        identities = iter(["original", None if reason == "owner_exit" else "reused"])
        monkeypatch.setattr("app.preview_run.process_identity", lambda pid: next(identities))
    if reason == "error":
        database.publish_descriptor.side_effect = RuntimeError("fixture failure")
        with pytest.raises(RuntimeError, match="fixture failure"):
            supervise([sys.executable, "-c", worker], env, database,
                      ttl=.5, owner_pid=os.getpid(), stopped=stopped)
    else:
        supervise([sys.executable, "-c", worker], env, database,
                  ttl=.5 if reason == "ttl" else 30, owner_pid=os.getpid(), stopped=stopped)
        assert record.is_file()
        assert not psutil.pid_exists(int(record.read_text()))
    database.close.assert_called_once()


@pytest.mark.parametrize("ttl,owner", [(0, 2), (14401, 2), (60, 1)])
def test_invalid_lease_never_starts_database(ttl, owner):
    database = Mock()
    with pytest.raises(ValueError):
        supervise([], {}, database, ttl=ttl, owner_pid=owner)
    database.open.assert_not_called()
