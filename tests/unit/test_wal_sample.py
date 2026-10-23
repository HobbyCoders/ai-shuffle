"""Read-only WAL sampling uses fresh transactions and never resets counters."""

import json
from decimal import Decimal
from unittest.mock import Mock

import pytest

from scripts.diagnostics import wal_sample


class FakeConnection:
    def __init__(self, version):
        self.server_version = version
        self.queries = []
        self.rows = iter([
            (Decimal(9007199254741000), 10, 1.5, "wal-reset"),
            (2, 3, 20.0, 4.0, "checkpoint-reset"),
            (Decimal(9007199254742024), 16, 3.5, "wal-reset"),
            (3, 5, 27.0, 9.0, "checkpoint-reset"),
        ])
        self.rollbacks = 0
        self.sessions = []

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def execute(self, query):
        # Deliberately fail any mutation or function call, not just named resets.
        assert query.startswith("SELECT ")
        assert "(" not in query and "reset(" not in query.lower()
        self.queries.append(query)

    def fetchone(self):
        return next(self.rows)

    def set_session(self, **kwargs):
        self.sessions.append(kwargs)

    def rollback(self):
        self.rollbacks += 1


@pytest.mark.parametrize("version,view", [
    (140000, "pg_stat_bgwriter"), (160000, "pg_stat_bgwriter"),
    (170000, "pg_stat_checkpointer"), (180000, "pg_stat_checkpointer"),
])
def test_two_samples_are_read_only_fresh_deltas(monkeypatch, version, view):
    conn = FakeConnection(version)
    sleeps = []
    monkeypatch.setattr(wal_sample.time, "sleep", lambda seconds: sleeps.append(seconds))
    result = wal_sample.sample(conn, 5)
    assert result == {
        "elapsed_s": result["elapsed_s"], "wal_bytes": 1024, "wal_fpi": 6,
        "wal_sync_ms": 2, "checkpoints_timed": 1, "checkpoints_requested": 2,
        "checkpoints": 3, "checkpoint_write_ms": 7, "checkpoint_sync_ms": 5,
    }
    assert result["elapsed_s"] >= 0
    assert sleeps == [5]
    assert conn.sessions == [{"readonly": True}]
    assert conn.rollbacks == 2
    assert len(conn.queries) == 4
    assert all(view in query for query in conn.queries[1::2])
    assert all("pg_stat_wal" in query for query in conn.queries[::2])


@pytest.mark.parametrize("key", ["wal_stats_reset", "checkpoint_stats_reset", "wal_bytes"])
def test_external_resets_are_rejected(key):
    before = dict.fromkeys(wal_sample.COUNTERS, 10)
    before.update(wal_stats_reset="a", checkpoint_stats_reset="b")
    after = {**before, key: 0 if key == "wal_bytes" else "changed"}
    with pytest.raises(ValueError):
        wal_sample.compute_deltas(before, after)


def test_failed_sample_ends_read_transaction():
    conn = FakeConnection(170000)
    conn.rows = iter([])
    with pytest.raises(StopIteration):
        wal_sample.read_sample(conn)
    assert conn.rollbacks == 1


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf"])
def test_invalid_interval(value):
    with pytest.raises(SystemExit) as exc:
        wal_sample.main(["--interval", value])
    assert exc.value.code == 2


def test_cli_uses_app_connection_helper_and_closes(monkeypatch, capsys):
    import psycopg2

    from app.core.config import settings

    conn = FakeConnection(170000)
    conn.close = Mock()
    connect = Mock(return_value=conn)
    helper = Mock(return_value={"dbname": "test-managed-db"})
    monkeypatch.setattr(type(settings), "get_database_connection", helper)
    monkeypatch.setattr(psycopg2, "connect", connect)
    monkeypatch.setattr(wal_sample.time, "sleep", lambda _: None)
    assert wal_sample.main(["--interval", "1"]) == 0
    helper.assert_called_once_with()
    connect.assert_called_once_with(dbname="test-managed-db")
    conn.close.assert_called_once_with()
    assert json.loads(capsys.readouterr().out)["wal_bytes"] == 1024


def test_connection_failure_does_not_print_private_details(monkeypatch, capsys):
    import psycopg2

    from app.core.config import settings

    monkeypatch.setattr(type(settings), "get_database_connection", lambda self: {})
    monkeypatch.setattr(psycopg2, "connect", Mock(side_effect=RuntimeError("private-detail-marker")))
    with pytest.raises(SystemExit) as exc:
        wal_sample.main([])
    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert "private-detail-marker" not in captured.err + captured.out
    assert "Could not sample managed PostgreSQL statistics" in captured.err
