"""Actual PostgreSQL slot transactions and v92 -> v93 startup migration."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
import threading

import pytest
from app.db import database, model_providers
from app.db.schema_postgres import create_postgres_schema
from tests.db.schema_lifecycle_helpers import pg16_bindir, pg_connect, private_postgres


@pytest.fixture
def cluster(tmp_path_factory):
    bindir = pg16_bindir()
    if bindir is None:
        pytest.skip("requires PostgreSQL 16 server binaries")
    with private_postgres(bindir, tmp_path_factory.mktemp("provider-slots-pg")) as descriptor:
        yield descriptor


@pytest.fixture
def pg(cluster, monkeypatch):
    @contextmanager
    def get_db():
        with closing(pg_connect(cluster)) as conn:
            conn.autocommit = False
            with conn:
                conn.cursor().execute("SET LOCAL statement_timeout = '10s'")
                yield conn

    with get_db() as conn:
        create_postgres_schema(conn.cursor())
    monkeypatch.setattr(model_providers, "get_db", get_db)
    monkeypatch.setattr(database, "get_db", get_db)
    yield get_db


def create(id, enabled=True):
    return database.create_model_provider(id, id, "openai", "https://api.openai.com/v1", enabled=enabled)


def test_parallel_activation_is_atomic_on_postgres(pg):
    create("a")
    create("b", False)
    barrier = threading.Barrier(8)
    def activate(i):
        barrier.wait(timeout=10)
        database.set_model_provider_enabled("a" if i % 2 else "b", True)
        # Each statement sees exactly one committed winner, never half a switch.
        assert len(database.get_enabled_model_providers()) == 1
    with ThreadPoolExecutor(max_workers=8) as pool:
        for future in [pool.submit(activate, i) for i in range(8)]:
            future.result(timeout=15)
    assert len(database.get_enabled_model_providers()) == 1


def test_actual_v92_startup_deduplicates_enabled_accounts(pg):
    create("first", False)
    create("later", False)
    with pg() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE model_providers SET enabled = TRUE, created_at = %s WHERE id = 'first'", ("2025-01-01",))
        cursor.execute("UPDATE model_providers SET enabled = TRUE, created_at = %s WHERE id = 'later'", ("2026-01-01",))
        cursor.execute("UPDATE schema_version SET version = 92")
    database.init_database()
    assert [r["id"] for r in database.get_enabled_model_providers()] == ["first"]
    assert len(database.get_all_model_providers()) == 2
    with pg() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT version FROM schema_version")
        assert cursor.fetchone()["version"] == database.SCHEMA_VERSION
    database.set_model_provider_enabled("later", True)
    database.init_database()
    assert [r["id"] for r in database.get_enabled_model_providers()] == ["later"]
