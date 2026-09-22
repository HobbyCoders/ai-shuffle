"""Usage lease races and upgrade contracts on disposable PostgreSQL."""

import os
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("ALLOW_REAL_PG") != "1",
                                reason="requires disposable TEST_POSTGRES_DSN")


@pytest.fixture(scope="module", autouse=True)
def pg_schema():
    from app.db import base
    from app.db.schema_postgres import create_postgres_schema
    assert os.environ.get("TEST_POSTGRES_DSN")
    base.init_pool(min_conn=1, max_conn=6)
    try:
        with base.get_db() as conn:
            create_postgres_schema(conn.cursor())
        yield
    finally:
        base.close_pool()


def test_concurrent_connections_grant_one_lease_and_survive_pool_restart(monkeypatch):
    from app.core import usage_polling as polling
    from app.db import base
    monkeypatch.setattr(polling.random, "uniform", lambda *_: 0)
    key = uuid4().hex
    with ThreadPoolExecutor(max_workers=4) as executor:
        leases = list(executor.map(lambda _: polling.claim(key, 1000), range(12)))
    lease, = [value for value in leases if value]
    base.close_pool()
    base.init_pool(min_conn=1, max_conn=6)
    assert not polling.claim(key, 1599)
    polling.finish(key, 1001, 429, 3600, lease=lease)
    assert not polling.claim(key, 4600)
    newer = polling.claim(key, 4601)
    assert newer and newer != lease
    polling.finish(key, 4602, 403, lease=lease)
    polling.finish(key, 4602, 200, lease=newer)
    assert polling.claim(key, 4902)


def test_upgrade_preserves_existing_auth_pause():
    from app.core import usage_polling as polling
    from app.db import base, database
    key = uuid4().hex
    with base.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("ALTER TABLE usage_poll_state DROP COLUMN lease_token")
        cursor.execute("INSERT INTO usage_poll_state (identity, paused) VALUES (%s, TRUE)", (key,))
    database.init_database()
    database.init_database()
    assert not polling.claim(key, 100000)
    polling.resume_identity(key)
    lease = polling.claim(key, 100000)
    assert lease
    polling.finish(key, 100001, 403, lease=lease)
    assert not polling.claim(key, 200000)
