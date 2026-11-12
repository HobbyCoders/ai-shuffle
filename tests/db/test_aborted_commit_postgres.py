"""An aborted transaction is never reported as committed (#1452).

PostgreSQL answers COMMIT of a transaction that an earlier error aborted with
ROLLBACK, and psycopg2 raises nothing. A block that caught a database error
without a savepoint used to leave ``get_db``/``transaction`` normally and run
its post-commit callbacks for work that never landed.
"""
from __future__ import annotations

from types import SimpleNamespace

import psycopg2
import psycopg2.errors
import psycopg2.extensions
import pytest

from app.db import base
from tests.db.test_commit_deadline_postgres import private_db, private_pool  # noqa: F401


@pytest.fixture
def probe(private_pool):  # noqa: F811
    with base.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("CREATE TABLE IF NOT EXISTS aborted_probe (id int PRIMARY KEY, v text)")
        cursor.execute("TRUNCATE aborted_probe")
        cursor.execute("INSERT INTO aborted_probe VALUES (1, 'before')")
    return private_pool


def _value() -> str:
    with base.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT v FROM aborted_probe WHERE id = 1")
        return cursor.fetchone()["v"]


def _swallow_error(cursor) -> None:
    try:
        cursor.execute("SELECT * FROM aborted_probe_missing_table")
    except psycopg2.errors.UndefinedTable:
        pass


@pytest.mark.parametrize("manager", [base.get_db, base.transaction])
def test_caught_error_without_savepoint_raises_and_skips_callbacks(probe, manager) -> None:
    ran = []
    with pytest.raises(psycopg2.errors.InFailedSqlTransaction):
        with manager() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE aborted_probe SET v = 'after' WHERE id = 1")
            base.after_commit(conn, lambda: ran.append("callback"))
            _swallow_error(cursor)
    assert ran == []
    assert _value() == "before"
    # The slot went back clean and stays usable.
    with base.get_db() as conn:
        assert conn.info.transaction_status == psycopg2.extensions.TRANSACTION_STATUS_IDLE


def test_explicit_commit_of_aborted_transaction_raises(probe) -> None:
    ran = []
    with pytest.raises(psycopg2.errors.InFailedSqlTransaction):
        with base.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE aborted_probe SET v = 'after' WHERE id = 1")
            base.after_commit(conn, lambda: ran.append("callback"))
            _swallow_error(cursor)
            conn.commit()
    assert ran == []
    assert _value() == "before"


def test_savepoint_keeps_the_transaction_and_its_callbacks(probe) -> None:
    ran = []
    with base.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE aborted_probe SET v = 'after' WHERE id = 1")
        base.after_commit(conn, lambda: ran.append("callback"))
        base.execute_savepoint(cursor, "SAVEPOINT sp_probe")
        try:
            cursor.execute("SELECT * FROM aborted_probe_missing_table")
        except psycopg2.errors.UndefinedTable:
            base.execute_savepoint(cursor, "ROLLBACK TO SAVEPOINT sp_probe")
        base.execute_savepoint(cursor, "RELEASE SAVEPOINT sp_probe")
    assert ran == ["callback"]
    assert _value() == "after"


def test_explicit_rollback_drops_callbacks_queued_before_it(probe) -> None:
    ran = []
    with base.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE aborted_probe SET v = 'discarded' WHERE id = 1")
        base.after_commit(conn, lambda: ran.append("discarded"))
        conn.rollback()
        cursor.execute("UPDATE aborted_probe SET v = 'after' WHERE id = 1")
        base.after_commit(conn, lambda: ran.append("committed"))
    assert ran == ["committed"]
    assert _value() == "after"


def test_mirror_blob_bookkeeping_failure_keeps_the_apply_transaction(probe, monkeypatch) -> None:
    from app.network import blobs, engine

    def failing_note_missing(hash_value, size, holder, *, _cursor=None):
        _cursor.execute("SELECT * FROM aborted_probe_missing_table")

    monkeypatch.setattr(blobs, "note_missing", failing_note_missing)
    with base.get_db() as conn:
        cursor = conn.cursor()
        engine._after_mirror(cursor, SimpleNamespace(name="artifacts"),
                             {"hash": "ab" * 32, "size": 1, "owner_device_id": "peer"})
        cursor.execute("UPDATE aborted_probe SET v = 'after' WHERE id = 1")
    assert _value() == "after"
