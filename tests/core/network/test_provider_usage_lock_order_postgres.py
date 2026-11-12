"""A synced key replacement and a usage write take the same lock order.

Against a disposable PostgreSQL, never the app DB: the in-memory harness has
no row locks, so it cannot show a deadlock.
"""
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, nullcontext

import pytest

from app.db import provider_usage_snapshots
from app.db.schema_postgres import create_postgres_schema
from app.network import engine, policy
from tests.db.schema_lifecycle_helpers import pg16_bindir, pg_connect, private_postgres

PROVIDER = "pg-usage-lock"


@pytest.fixture(scope="module")
def cluster(tmp_path_factory):
    bindir = pg16_bindir()
    if bindir is None:
        pytest.skip("requires PostgreSQL 16 server binaries")
    with private_postgres(bindir, tmp_path_factory.mktemp("usage-lock-pg")) as descriptor:
        with closing(pg_connect(descriptor)) as conn, conn:
            create_postgres_schema(conn.cursor())
        yield descriptor


def provider(base_url):
    return {"id": PROVIDER, "name": "Kimi", "provider_type": "kimi", "base_url": base_url,
            "auth_mode": "api_key", "enabled": True}


def waiting_on_lock(conn, pid):
    cursor = conn.cursor()
    cursor.execute("SELECT wait_event_type FROM pg_stat_activity WHERE pid = %s", (pid,))
    row = cursor.fetchone()
    return bool(row) and row["wait_event_type"] == "Lock"


def test_synced_endpoint_change_waits_for_an_in_flight_usage_write(cluster, monkeypatch):
    coll = policy.collection("model_providers")
    with closing(pg_connect(cluster)) as setup:
        engine.materialize(setup.cursor(), coll, PROVIDER, provider("https://api.moonshot.ai/anthropic"))
        cursor = setup.cursor()
        cursor.execute("SELECT revision FROM model_providers WHERE id = %s", (PROVIDER,))
        revision = cursor.fetchone()["revision"]
        cursor.execute("INSERT INTO provider_usage_snapshots (provider_id, provider_type, windows, updated_at) "
                       "VALUES (%s, 'kimi_balance', '[]', now())", (PROVIDER,))

    with closing(pg_connect(cluster)) as poll, closing(pg_connect(cluster)) as sync, \
            closing(pg_connect(cluster)) as watch, ThreadPoolExecutor(max_workers=1) as pool:
        poll.autocommit = sync.autocommit = False
        for conn in (poll, sync):
            conn.cursor().execute("SET lock_timeout TO 10000")
        sync_cursor = sync.cursor()
        sync_cursor.execute("SELECT pg_backend_pid() AS pid")
        sync_pid = sync_cursor.fetchone()["pid"]
        # The poller's guard: the provider row is locked, the snapshot not yet written.
        poll.cursor().execute("SELECT revision FROM model_providers WHERE id = %s FOR UPDATE", (PROVIDER,))

        def apply_replacement():
            row = engine.materialize(sync_cursor, coll, PROVIDER, provider("https://api.moonshot.ai/v2"))
            sync.commit()
            return row

        future = pool.submit(apply_replacement)
        deadline = time.monotonic() + 5
        while not waiting_on_lock(watch, sync_pid):
            assert time.monotonic() < deadline, "the sync write never reached the provider lock"
            time.sleep(0.05)
        # Finish the guarded write while the sync waits. If the sync held the
        # snapshot row, PostgreSQL would abort one side as a deadlock here.
        monkeypatch.setattr(provider_usage_snapshots, "get_db", lambda: nullcontext(poll))
        written = provider_usage_snapshots.upsert_provider_usage_snapshot(
            PROVIDER, "kimi_balance", None, [], {"balance_remaining": 1.0, "currency": "USD"},
            {"source": "kimi_balance"}, int(time.time() * 1000),
            expected_provider_revision=(PROVIDER, revision))
        poll.commit()
        assert written is True
        row = future.result(timeout=15)

    assert row["base_url"] == "https://api.moonshot.ai/v2"
    with closing(pg_connect(cluster)) as check:
        cursor = check.cursor()
        cursor.execute("SELECT 1 FROM provider_usage_snapshots WHERE provider_id = %s", (PROVIDER,))
        # The reading belonged to the old endpoint; the sync removed it after it landed.
        assert cursor.fetchone() is None


def test_first_arrival_locks_nothing_and_inserts(cluster):
    coll = policy.collection("model_providers")
    record = {**provider("https://api.moonshot.ai/anthropic"), "id": "pg-usage-new"}
    with closing(pg_connect(cluster)) as conn:
        conn.autocommit = False
        row = engine.materialize(conn.cursor(), coll, "pg-usage-new", record)
        conn.commit()
    assert row["id"] == "pg-usage-new"



def test_a_generic_edit_keeps_the_guard_stored_when_it_writes(cluster, monkeypatch):
    # The generic PATCH on an Antigravity row keeps the reserved guard key as
    # stored at write time: it takes the slot lock, then the provider row FOR
    # UPDATE (the same order as before, then the snapshot), so a synced guard
    # restore holding the row makes it wait and then wins. No new lock pair:
    # a usage writer takes the provider row, then its snapshot.
    from app.core import antigravity
    from app.db import model_providers

    coll = policy.collection("model_providers")
    row_id = "pg-antigravity-guard"
    record = {"id": row_id, "name": "Antigravity", "provider_type": "antigravity",
              "base_url": "", "auth_mode": "oauth-personal", "enabled": True}
    key = antigravity.GUARD_OVERRIDE_KEY
    with closing(pg_connect(cluster)) as setup:
        engine.materialize(setup.cursor(), coll, row_id, record)
        setup.cursor().execute("UPDATE model_providers SET extra_env_json = %s WHERE id = %s",
                               ('{"%s": "1"}' % key, row_id))

    with closing(pg_connect(cluster)) as sync, closing(pg_connect(cluster)) as edit, \
            closing(pg_connect(cluster)) as watch, ThreadPoolExecutor(max_workers=1) as pool:
        sync.autocommit = edit.autocommit = False
        for conn in (sync, edit):
            conn.cursor().execute("SET lock_timeout TO 10000")
        edit_cursor = edit.cursor()
        edit_cursor.execute("SELECT pg_backend_pid() AS pid")
        edit_pid = edit_cursor.fetchone()["pid"]
        # The synced restore: row locked, guard key gone, not yet committed.
        sync.cursor().execute("SELECT 1 FROM model_providers WHERE id = %s FOR UPDATE", (row_id,))
        sync.cursor().execute("UPDATE model_providers SET extra_env_json = '{}' WHERE id = %s", (row_id,))
        monkeypatch.setattr(model_providers, "get_db", lambda: nullcontext(edit))

        def stale_edit():
            saved = model_providers.update_model_provider(
                row_id, keep_env_keys=(key,), extra_env_json={"OTHER_SETTING": "1", key: "1"})
            edit.commit()
            return saved

        future = pool.submit(stale_edit)
        deadline = time.monotonic() + 5
        while not waiting_on_lock(watch, edit_pid):
            assert time.monotonic() < deadline, "the edit never waited for the provider row"
            time.sleep(0.05)
        sync.commit()
        saved = future.result(timeout=15)

    assert saved["extra_env_json"] == {"OTHER_SETTING": "1"}
