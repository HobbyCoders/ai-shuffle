"""Unit tests for app/db/interactions.py — pending interaction state machine."""

import pytest

from app.db import interactions as ix


@pytest.fixture
def test_db():
    from tests.pg_shim import new_sqlite_connection
    conn = new_sqlite_connection()
    yield conn
    conn.close()


@pytest.fixture
def mock_db(test_db):
    from tests.pg_shim import route_db_to
    with route_db_to(test_db):
        yield test_db


def test_create_lists_as_pending(mock_db):
    created = ix.create_interaction("s1", "permission", {"tool": "Bash"}, 60)
    assert created["state"] == "pending"
    assert created["payload"] == {"tool": "Bash"}
    pending = ix.get_pending_interactions("s1")
    assert len(pending) == 1
    assert pending[0]["id"] == created["id"]
    assert pending[0]["kind"] == "permission"


def test_answer_once_true_second_false(mock_db):
    created = ix.create_interaction("s1", "permission", {}, 60)
    assert ix.answer_interaction(created["id"], {"allow": True}, "device-1") is True
    assert ix.answer_interaction(created["id"], {"allow": False}) is False
    row = ix.get_interaction(created["id"])
    assert row["state"] == "answered"
    assert row["answer"] == {"allow": True}
    assert row["answered_by_device"] == "device-1"
    # No longer listed as pending.
    assert ix.get_pending_interactions("s1") == []


def test_answer_after_expiry_false(mock_db):
    created = ix.create_interaction("s1", "permission", {}, -1)
    assert ix.answer_interaction(created["id"], {"allow": True}) is False
    # Expired interactions are not listed as pending.
    assert ix.get_pending_interactions("s1") == []


def test_cancel_pending(mock_db):
    created = ix.create_interaction("s1", "permission", {}, 60)
    assert ix.cancel_interaction(created["id"]) is True
    assert ix.get_interaction(created["id"])["state"] == "cancelled"
    # Cancelling again (no longer pending) is a no-op.
    assert ix.cancel_interaction(created["id"]) is False


def test_expire_due_only_flips_overdue_pendings(mock_db):
    live = ix.create_interaction("s1", "a", {}, 60)
    overdue = ix.create_interaction("s1", "b", {}, -1)
    count = ix.expire_due_interactions()
    assert count == 1
    assert ix.get_interaction(live["id"])["state"] == "pending"
    assert ix.get_interaction(overdue["id"])["state"] == "expired"


def test_get_interaction_missing_returns_none(mock_db):
    assert ix.get_interaction("nope") is None
