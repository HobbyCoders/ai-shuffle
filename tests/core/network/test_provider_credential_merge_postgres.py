"""Credential merge SQL against a disposable PostgreSQL, never the app DB."""
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime

import pytest

from app.db.schema_postgres import create_postgres_schema
from app.network import engine, policy
from tests.db.schema_lifecycle_helpers import pg16_bindir, pg_connect, private_postgres


@pytest.fixture(scope="module")
def cluster(tmp_path_factory):
    bindir = pg16_bindir()
    if bindir is None:
        pytest.skip("requires PostgreSQL 16 server binaries")
    with private_postgres(bindir, tmp_path_factory.mktemp("credential-merge-pg")) as descriptor:
        with closing(pg_connect(descriptor)) as conn, conn:
            create_postgres_schema(conn.cursor())
        yield descriptor


def bundle(provider, stamp, generation):
    return {"id": provider, "name": "Original", "provider_type": "openai", "base_url": "https://example.com",
            "auth_mode": "chatgpt", "codex_last_refresh": stamp, "codex_access_token": "access-" + generation,
            "codex_refresh_token": "refresh-" + generation, "codex_account_id": "account-" + generation,
            "codex_id_token_expires_at": stamp, "needs_reauth": False}


@pytest.mark.parametrize("losing_row", [False, True])
def test_timestamp_comparison_and_atomic_write(cluster, losing_row):
    coll = policy.collection("model_providers")
    provider = "pg-losing" if losing_row else "pg-winning"
    # Lexically later, but chronologically older. PostgreSQL must compare
    # instants, not strings (the in-memory network harness cannot prove this).
    old = bundle(provider, "2026-10-19T14:00:00+04:00", "old")
    newer = bundle(provider, "2026-10-19T12:00:00+00:00", "new")
    with closing(pg_connect(cluster)) as initial:
        engine.materialize(initial.cursor(), coll, provider, old)
    started = threading.Event()

    def incoming_write():
        with closing(pg_connect(cluster)) as conn:
            conn.cursor().execute("SET statement_timeout TO 10000")
            started.set()
            if losing_row:
                assert engine._merge_losing_provider_credentials(
                    conn.cursor(), coll, provider, newer, {"deleted": False})
                return engine._read_row(conn.cursor(), coll, provider)
            return engine.materialize(conn.cursor(), coll, provider, {**old, "name": "Edited"})

    with closing(pg_connect(cluster)) as rotating, ThreadPoolExecutor(max_workers=1) as pool:
        rotating.autocommit = False
        # Hold a real row lock while the incoming operation starts. The
        # guarded write must evaluate against the committed row, not a stale
        # read from before the rotation.
        rotating.cursor().execute("SELECT id FROM model_providers WHERE id = %s FOR UPDATE", (provider,))
        if not losing_row:
            engine.materialize(rotating.cursor(), coll, provider, newer)
        future = pool.submit(incoming_write)
        try:
            assert started.wait(5)
        finally:
            rotating.commit()
        row = future.result(timeout=15)
    assert row["codex_refresh_token"] == "refresh-new"
    assert row["codex_access_token"] == "access-new"
    assert row["codex_account_id"] == "account-new"
    assert row["codex_last_refresh"] == datetime(2026, 10, 19, 12, tzinfo=UTC)
    assert row["name"] == ("Original" if losing_row else "Edited")
