"""v89: the in-app Claude sign-in's storage is removed, and the upgrade notice survives it.

Runs ``_retire_claude_accounts`` against the in-memory SQLite shim after
re-creating what an older build left behind: the ``claude_accounts`` table,
active-account pointers, sealed ``claude_accounts`` envelopes in the sync
tables and the ``usage_snapshots.account_*`` columns.
"""

import json

import pytest

from app.core import claude_relogin_notice as notice
from app.core.config import settings
from app.db import database as db
from app.db.base import column_exists, get_db, table_exists


def _legacy_table(cursor, rows=0):
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS claude_accounts (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, email TEXT, subscription_type TEXT,
            credentials TEXT, credential_fingerprint TEXT, expires_at BIGINT,
            has_refresh_token BOOLEAN DEFAULT TRUE, sync_pending BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP, updated_at TIMESTAMP, last_used_at TIMESTAMP
        )
    """)
    for i in range(rows):
        cursor.execute(
            "INSERT INTO claude_accounts (id, name, credentials) VALUES (%s, %s, %s)",
            (f"acct-{i}", f"Account {i}", "enc:sealed"),
        )


def _legacy_usage_columns(cursor):
    for column in ("account_id", "account_email", "account_name"):
        if not column_exists(cursor, "usage_snapshots", column):
            cursor.execute(f"ALTER TABLE usage_snapshots ADD COLUMN {column} TEXT")


def _envelope(collection, seq):
    return {"collection": collection, "record_id": "r", "op": "upsert", "clock": seq,
            "origin_device_id": "peer", "origin_incarnation": 1, "origin_seq": seq}


def _sync_rows(cursor):
    for seq, collection in ((1, "claude_accounts"), (2, "sessions")):
        cursor.execute(
            "INSERT INTO sync_ops (origin_device_id, incarnation, seq, collection, record_id, op, clock, envelope)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            ("peer", 1, seq, collection, "r", "upsert", seq, json.dumps(_envelope(collection, seq))),
        )
        cursor.execute(
            "INSERT INTO sync_records (collection, record_id, clock, origin_device_id) VALUES (%s, %s, %s, %s)",
            (collection, "r", seq, "peer"),
        )
        cursor.execute(
            "INSERT INTO sync_pending (origin_device_id, incarnation, seq, envelope) VALUES (%s, %s, %s, %s)",
            ("peer", 1, seq + 10, json.dumps(_envelope(collection, seq + 10))),
        )


def _count(cursor, sql, params=()):
    cursor.execute(sql, params)
    row = cursor.fetchone()
    return int(row["n"])


def _notice_key(instance_id):
    return "claude_cli_relogin_notice:" + instance_id


@pytest.fixture
def cursor(mock_db):
    with get_db() as conn:
        yield conn.cursor()


def test_upgrade_records_the_notice_then_removes_everything(cursor):
    _legacy_table(cursor, rows=2)
    _legacy_usage_columns(cursor)
    _sync_rows(cursor)
    db.set_system_setting("claude_active_account:other-install", "acct-1")

    applied = db._retire_claude_accounts(cursor)

    assert not table_exists(cursor, "claude_accounts")
    assert _count(cursor, "SELECT COUNT(*) AS n FROM system_settings WHERE key LIKE 'claude_active_account:%'") == 0
    # Notices for this worker and for the worker that had a pointer, plus the flag.
    assert db.get_system_setting(_notice_key(settings.effective_instance_id)) == "pending"
    assert db.get_system_setting(_notice_key("other-install")) == "pending"
    assert db.get_system_setting(db.CLAUDE_RELOGIN_NEEDED_MARKER) == "1"
    # Only the retired collection is purged from the sync tables.
    for table in ("sync_ops", "sync_records"):
        assert _count(cursor, f"SELECT COUNT(*) AS n FROM {table} WHERE collection = 'claude_accounts'") == 0
        assert _count(cursor, f"SELECT COUNT(*) AS n FROM {table} WHERE collection = 'sessions'") == 1
    cursor.execute("SELECT envelope FROM sync_pending")
    left = [json.loads(r["envelope"]) if isinstance(r["envelope"], str) else r["envelope"]
            for r in cursor.fetchall()]
    assert [e["collection"] for e in left] == ["sessions"]
    for column in ("account_id", "account_email", "account_name"):
        assert not column_exists(cursor, "usage_snapshots", column)
    assert "drop claude_accounts(2 rows)" in applied

    # Idempotent: a second pass finds nothing and changes nothing.
    assert db._retire_claude_accounts(cursor) == []
    assert db.get_system_setting(_notice_key(settings.effective_instance_id)) == "pending"


def test_pointer_alone_is_enough_for_the_notice(cursor):
    db.set_system_setting("claude_active_account:" + settings.effective_instance_id, "acct")
    db._retire_claude_accounts(cursor)
    assert notice.pending() is True


def test_empty_table_on_a_fresh_install_leaves_no_notice(cursor):
    _legacy_table(cursor, rows=0)
    db._retire_claude_accounts(cursor)
    assert not table_exists(cursor, "claude_accounts")
    assert db.get_system_setting(_notice_key(settings.effective_instance_id)) is None
    assert db.get_system_setting(db.CLAUDE_RELOGIN_NEEDED_MARKER) is None
    assert notice.mark_if_needed() is False


def test_a_dismissed_notice_is_never_reopened(cursor):
    db.set_system_setting(_notice_key(settings.effective_instance_id), "dismissed")
    _legacy_table(cursor, rows=1)
    db._retire_claude_accounts(cursor)
    assert db.get_system_setting(_notice_key(settings.effective_instance_id)) == "dismissed"
    assert notice.mark_if_needed() is False


def test_another_worker_sharing_the_database_gets_the_notice_from_the_flag(cursor, monkeypatch):
    _legacy_table(cursor, rows=1)
    db._retire_claude_accounts(cursor)
    monkeypatch.setattr(type(settings), "effective_instance_id", property(lambda self: "second-worker"))
    assert notice.pending() is False
    assert notice.mark_if_needed() is True
    assert notice.pending() is True


def test_the_notice_does_not_read_the_retired_table(cursor, monkeypatch):
    from tests.pg_shim import ShimCursor
    original = ShimCursor.execute

    def execute(self, sql, params=None):
        assert "claude_accounts" not in sql
        return original(self, sql, params)

    monkeypatch.setattr(ShimCursor, "execute", execute)
    assert notice.mark_if_needed() is False


def test_fresh_schema_has_no_claude_account_storage(cursor):
    assert not table_exists(cursor, "claude_accounts")
    for column in ("account_id", "account_email", "account_name"):
        assert not column_exists(cursor, "usage_snapshots", column)


def test_schema_version_bumped_in_both_modules():
    from app.db import schema_postgres
    assert db.SCHEMA_VERSION == schema_postgres.SCHEMA_VERSION == 89
