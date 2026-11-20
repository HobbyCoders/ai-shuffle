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


@pytest.mark.parametrize("changed", ["scalar", "local", "shared", "40P01", "40001"])
def test_legacy_confirmation_cas_rolls_back_a_prepared_migration(choice_cluster, changed, monkeypatch):
    def connect():
        conn = pg_connect(choice_cluster)
        conn.cursor().execute("SET statement_timeout = '5s'")
        conn.autocommit = False
        return conn

    monkeypatch.setattr(base, "get_connection", connect)
    monkeypatch.setattr(base, "_close_connection", lambda conn: conn.close())
    monkeypatch.setattr(claude_accounts, "_encrypt", lambda value: value)
    monkeypatch.setattr(claude_accounts, "_require_storage", lambda: None)
    local, shared = auth.setting_key(), auth.SELECTION_SETTING
    scalar, saved = auth.API_KEY_SETTING, claude_accounts.API_LIST_SETTING
    db.delete_system_setting(shared)
    db.delete_system_setting(saved)
    db.set_system_setting(local, auth.MODE_API_KEY)
    db.set_system_setting(scalar, "fixture-first-1111")
    revision = auth.summary()["api_key_confirmation"]
    assert revision
    update = {
        "scalar": (scalar, "fixture-different-1111"),
        "local": (local, auth.MODE_OFF),
        "shared": (shared, json.dumps({"mode": auth.MODE_SUBSCRIPTION})),
    }.get(changed)
    before = db_settings.get_system_setting_snapshots(scalar, local, shared, saved)
    original_write = db_settings._write_system_setting_if_unchanged
    original_commit = db_settings.write_system_settings_if_unchanged

    def commit(writes, *, guards):
        assert saved in writes and scalar in guards  # Migration prepared, but not committed.
        if changed == "scalar":
            db.set_system_setting(*update)  # After matching the rendered revision, before taking the scalar lock.
        return original_commit(writes, guards=guards)

    def write(conn, key, value, expected):
        if update is None and key == local:
            # Raise the real PG error in the still-open transaction after the
            # list INSERT, rather than simulating a deadlock with a lock race.
            conn.cursor().execute(f"DO $$ BEGIN RAISE SQLSTATE '{changed}'; END $$")
        if update is not None and key == update[0] and changed != "scalar":
            assert db.get_system_setting(saved) is None  # Migration not visible yet.
            db.set_system_setting(*update)  # On a different connection, before the local/shared CAS.
        return original_write(conn, key, value, expected)

    monkeypatch.setattr(db_settings, "_write_system_setting_if_unchanged", write)
    monkeypatch.setattr(db_settings, "write_system_settings_if_unchanged", commit)
    with pytest.raises(claude_accounts.AccountError, match="API key or mode changed") as error:
        auth.confirm_legacy_api_key(revision)
    assert error.value.status == 409
    assert error.value.code == "confirmation_changed"
    if update is not None:
        before[update[0]] = db.get_system_setting_snapshot(update[0])
        assert before[update[0]].value == update[1]
    assert db_settings.get_system_setting_snapshots(scalar, local, shared, saved) == before


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
