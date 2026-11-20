"""Race the Claude selection writers against a second PostgreSQL transaction."""
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from threading import Event

import pytest

from app.core import claude_accounts
from app.core import claude_auth_mode as auth
from app.db import base
from app.db import database as db
from app.db import settings as db_settings
from app.network import engine
from tests.core.network import test_transcript_op_retention_postgres as peer_postgres
from tests.core.network.harness import exchange
from tests.db.schema_lifecycle_helpers import pg16_bindir, pg_connect, private_postgres

cluster = peer_postgres.cluster
pg_workers = peer_postgres.pg_workers


@pytest.fixture(scope="module")
def choice_cluster(tmp_path_factory):
    bindir = pg16_bindir()
    if bindir is None:
        pytest.skip("requires disposable PostgreSQL 16 server binaries")
    with private_postgres(bindir, tmp_path_factory.mktemp("claude-choice-pg")) as descriptor:
        with closing(pg_connect(descriptor)) as conn:
            conn.cursor().execute("""CREATE TABLE system_settings (
                key TEXT PRIMARY KEY, value TEXT, revision TEXT, updated_at TIMESTAMPTZ
            )""")
        yield descriptor


@pytest.mark.parametrize("initial", ["K1", None, "K2"])
def test_subscription_activation_conflict_rolls_back_and_retry_keeps_latest_pin(
    choice_cluster, initial, monkeypatch,
):
    def connect():
        conn = pg_connect(choice_cluster)
        conn.cursor().execute("SET statement_timeout = '5s'")
        conn.autocommit = False
        return conn

    monkeypatch.setattr(base, "get_connection", connect)
    monkeypatch.setattr(base, "_close_connection", lambda conn: conn.close())
    local = "claude_subscriptions:local-fixture"
    shared = auth.SELECTION_SETTING
    db.delete_system_setting(shared)
    db.set_system_setting(auth.setting_key(), auth.MODE_SUBSCRIPTION)
    db.set_system_setting(local, "before")
    local_revision = db.get_system_setting_revision(local)
    if initial is not None:
        db.set_system_setting(shared, json.dumps({
            "mode": "subscription", "subscription": "old", "key_id": initial,
        }))
    # Even an identical-value rewrite is a different row revision.
    incoming = json.dumps({"mode": "subscription", "subscription": "old", "key_id": "K2"})
    local_written, peer_committed = Event(), Event()
    write = db_settings._write_system_setting

    def pause_after_local_write(conn, key, value):
        write(conn, key, value)
        if key == local:
            local_written.set()
            assert peer_committed.wait(5)

    def activate():
        auth.publish_subscription("new", local_subscription=(local, "after"))

    with monkeypatch.context() as interleave, ThreadPoolExecutor(max_workers=1) as pool:
        interleave.setattr(db_settings, "_write_system_setting", pause_after_local_write)
        request = pool.submit(activate)
        try:
            assert local_written.wait(5)
            # Read-committed observer cannot see the uncommitted local write.
            assert db.get_system_setting(local) == "before"
            db.set_system_setting(shared, incoming)
            peer_revision = db.get_system_setting_revision(shared)
        finally:
            peer_committed.set()
        with pytest.raises(claude_accounts.AccountError) as error:
            request.result(timeout=5)
        assert error.value.code == "selection_changed"
    assert db.get_system_setting(local) == "before"
    assert db.get_system_setting_revision(local) == local_revision
    assert db.get_system_setting(shared) == incoming
    assert db.get_system_setting_revision(shared) == peer_revision

    activate()
    assert db.get_system_setting(local) == "after"
    assert auth.shared_selection() == {"mode": "subscription", "subscription": "new", "key_id": "K2"}


def test_signed_peer_choice_lands_inside_activation_transaction(pg_workers, monkeypatch):
    receiver, producer = pg_workers("receiver"), pg_workers("producer")
    shared, local = auth.SELECTION_SETTING, "claude_cli_logins:receiver-fixture"
    with receiver.active():
        engine.create_network("Claude choices")
        auth.set_mode(auth.MODE_API_KEY, key_id="K1")
        assert engine.scan_all(collections=["system_settings"], include_catalog=False, strict=True) >= 1
        db.set_system_setting(local, "before")
        before = db.get_system_setting_snapshot(local)
    peer_postgres.admit(receiver, producer)

    with producer.active():
        assert auth.shared_selection()["key_id"] == "K1"
        auth.update_shared_key("K2")
        assert engine.scan_all(collections=["system_settings"], include_catalog=False, strict=True) >= 1

    write = db_settings._write_system_setting
    received = None

    def deliver_during_activation(conn, key, value):
        nonlocal received
        write(conn, key, value)
        if key == local:
            assert db.get_system_setting_snapshot(local) == before
            result = exchange(producer, receiver)
            assert ("system_settings", shared, "upsert") in result.touched
            assert result.applied >= 1 and result.rejected == result.retry == 0
            received = db.get_system_setting_snapshot(shared)
            assert auth.shared_selection()["key_id"] == "K2"

    with receiver.active():
        assert auth.shared_selection()["key_id"] == "K1"
        with monkeypatch.context() as pause:
            pause.setattr(db_settings, "_write_system_setting", deliver_during_activation)
            with pytest.raises(claude_accounts.AccountError, match="choice changed"):
                auth.publish_subscription("verified-sub", local_subscription=(local, "after"))
        assert db.get_system_setting_snapshot(local) == before
        assert db.get_system_setting_snapshot(shared) == received
        auth.publish_subscription("verified-sub", local_subscription=(local, "after"))
        assert db.get_system_setting(local) == "after"
        assert auth.shared_selection() == {"mode": "api_key", "subscription": "verified-sub", "key_id": "K2"}
