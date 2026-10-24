"""Long-operation budgets on disposable PostgreSQL; no configured worker database."""
from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Any, Iterator

import psycopg2
import pytest

from app.db import base, database, mobile_tools, sql_timeouts
from tests.db.test_commit_deadline_postgres import private_db  # noqa: F401


@pytest.fixture
def budget_pool(
    private_db: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> Iterator[base._RetainingThreadedConnectionPool]:
    params = {k: private_db[k] for k in ("host", "port", "user", "password")}
    params["dbname"] = private_db["database"]
    pool = base._RetainingThreadedConnectionPool(
        1, 3, reserved=0,
        options="-c statement_timeout=20ms -c lock_timeout=20ms "
                "-c idle_in_transaction_session_timeout=200ms", **params,
    )
    monkeypatch.setattr(base, "_pg_pool", pool)
    monkeypatch.setattr(base, "get_connection", base._get_pg_connection)
    # VACUUM deliberately checks out directly, outside get_db's transaction.
    monkeypatch.setattr(database, "get_connection", base._get_pg_connection)
    try:
        yield pool
    finally:
        pool.closeall()


def test_startup_migration_outlives_ordinary_deadline_without_leaking(
    budget_pool: base._RetainingThreadedConnectionPool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_migrations = database._run_column_migrations
    ran = []

    def slow_migrations(cursor: Any) -> None:
        for setting in ("statement_timeout", "lock_timeout",
                        "idle_in_transaction_session_timeout"):
            cursor.execute(f"SHOW {setting}")
            assert cursor.fetchone()[setting] == "0"
        cursor.execute("SELECT pg_sleep(0.08)")
        real_migrations(cursor)
        ran.append(True)

    monkeypatch.setenv("FORCE_DB_MIGRATIONS", "1")
    monkeypatch.setattr(database, "_run_column_migrations", slow_migrations)

    def slow_mobile_migration(cursor: Any) -> None:
        sql_timeouts.migration_timeouts(cursor)
        cursor.execute("SELECT pg_sleep(0.08)")
        ran.append("mobile")

    monkeypatch.setattr(mobile_tools, "migration_timeouts", slow_mobile_migration)
    with database.boot_init_lock():
        database.init_database()
        mobile_tools.init_schema()
        # The dedicated boot-lock connection must survive idle startup too.
        time.sleep(0.25)
    assert ran == [True, "mobile"]
    with base.get_db() as conn:
        cur = conn.cursor()
        for setting, expected in (("statement_timeout", "20ms"), ("lock_timeout", "20ms"),
                                  ("idle_in_transaction_session_timeout", "200ms")):
            cur.execute(f"SHOW {setting}")
            assert cur.fetchone()[setting] == expected
    with pytest.raises(psycopg2.errors.QueryCanceled):
        with base.get_db() as conn:
            conn.cursor().execute("SELECT pg_sleep(0.08)")
    assert not budget_pool._used


@pytest.mark.parametrize("fail_vacuum", [False, True])
def test_maintenance_uses_longer_bounded_budgets_and_restores_on_error(
    budget_pool: base._RetainingThreadedConnectionPool, monkeypatch: pytest.MonkeyPatch,
    fail_vacuum: bool,
) -> None:
    database.init_database()
    real_analyze = database.analyze_timeouts
    seen = []

    def slow_analyze(cursor: Any) -> None:
        real_analyze(cursor)
        cursor.execute("SHOW statement_timeout")
        assert cursor.fetchone()["statement_timeout"] == "5min"
        cursor.execute("SELECT pg_sleep(0.08)")
        seen.append("analyze")

    @contextmanager
    def slow_vacuum(cursor: Any) -> Iterator[None]:
        with sql_timeouts.vacuum_timeouts(cursor):
            for setting, expected in (("statement_timeout", "15min"), ("lock_timeout", "30s")):
                cursor.execute(f"SHOW {setting}")
                assert cursor.fetchone()[setting] == expected
            seen.append("vacuum")
            if fail_vacuum:
                cursor.execute("SET statement_timeout = '1ms'")
            cursor.execute("SELECT pg_sleep(0.08)")
            yield

    monkeypatch.setattr(database, "analyze_timeouts", slow_analyze)
    monkeypatch.setattr(database, "vacuum_timeouts", slow_vacuum)
    results = database.run_database_maintenance()
    assert seen == ["analyze", "vacuum"]
    assert results["analyze"] == "ok"
    if fail_vacuum:
        assert results["vacuum"].startswith("error:")
    else:
        assert results["vacuum"] == "ok"
    with base.get_db() as conn:
        assert conn.autocommit is False
        cur = conn.cursor()
        for setting in ("statement_timeout", "lock_timeout"):
            cur.execute(f"SHOW {setting}")
            assert cur.fetchone()[setting] == "20ms"
    assert not budget_pool._used


def test_shim_accepts_migration_budget_scope(mock_db: Any) -> None:
    database.init_database()
