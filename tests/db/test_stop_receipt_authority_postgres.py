"""Cleanup authority is not owned by the mutable logical turn it outlives."""
import time

import pytest

from tests.db.postgres_harness import requires_real_pg
from tests.db.test_event_write_lock_timeout_postgres import cluster, session_id, connect  # noqa: F401

pytestmark = requires_real_pg


def _started(session_id, owner="old-owner"):
    from app.db import turns
    queued = turns.enqueue_turn(session_id, "user", prompt="old", profile_id="default")
    return turns.start_turn(queued["id"], None, owner)


def test_receipt_publish_never_waits_for_locked_turn(connect, session_id):
    from app.db import stop_receipts, turns
    old = _started(session_id)
    blocked = connect()
    try:
        blocked.cursor().execute("UPDATE turns SET prompt='uncommitted' WHERE id=%s", (old["id"],))
        start = time.monotonic()
        receipt = turns.read_stop_receipt(session_id)
        assert time.monotonic() - start < 1
        rows = stop_receipts.pending("old-owner", turn_id=old["id"])
        assert len(rows) == 1 and rows[0]["receipt"]["stop_seq"] == receipt["stop_seq"]
    finally:
        blocked.rollback()
        blocked.close()


def test_receipt_and_stop_sequence_rollback_together(session_id):
    from app.db import base, stop_receipts, turns
    old = _started(session_id)
    with pytest.raises(RuntimeError, match="rollback"):
        with base.get_db() as conn:
            turns._stop_receipt(conn.cursor(), session_id)
            raise RuntimeError("rollback")
    assert stop_receipts.pending("old-owner", turn_id=old["id"]) == []
    with base.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT stop_seq FROM session_stops WHERE session_id=%s", (session_id,))
        assert cursor.fetchone() is None


def test_repeated_stops_survive_foreign_logical_turn_absorption_and_clear(session_id):
    from app.db import sessions, stop_receipts, turns
    old = _started(session_id)
    first = turns.read_stop_receipt(session_id)
    turns.park_turn(old["id"])
    queued = turns.enqueue_turn(session_id, "user", prompt="new", profile_id="default",
                                payload={turns.PERSON_INPUT_KEY: True})
    resumed = turns.start_turn(queued["id"], None, "new-owner", can_resume=lambda *_args: True)
    assert resumed["id"] == old["id"] and resumed["instance_id"] == "new-owner"
    assert resumed["payload"]["accepted_input_order"] == queued["insert_order"]
    second = turns.read_stop_receipt(session_id)
    sessions.clear_session_conversation(session_id)
    rows_a = stop_receipts.pending("old-owner", turn_id=old["id"])
    rows_b = stop_receipts.pending("new-owner", turn_id=old["id"])
    assert len(rows_a) == len(rows_b) == 1
    assert rows_a[0]["stop_seq"] == first["stop_seq"] < rows_b[0]["stop_seq"] == second["stop_seq"]
    sessions.delete_session(session_id)
    assert turns.get_turn(old["id"]) is None
    assert stop_receipts.pending("old-owner", turn_id=old["id"])[0]["id"] == rows_a[0]["id"]
    assert stop_receipts.pending("new-owner", turn_id=old["id"])[0]["id"] == rows_b[0]["id"]


async def test_restart_replay_after_deletion_records_unknown_cleanup_durably(session_id, monkeypatch):
    from app.core import stop_receipts as consumer
    from app.core.task_registry import task_registry
    from app.db import base, sessions, stop_receipts, turns
    old = _started(session_id)
    with base.get_db() as conn:
        conn.cursor().execute("INSERT INTO background_tasks "
            "(task_id,session_id,instance_id,kind,status,started_at,remote_device_id) "
            "VALUES ('legacy',%s,'remote','agent','running',NOW(),'remote')", (session_id,))
    turns.read_stop_receipt(session_id)
    pending = stop_receipts.pending("old-owner", turn_id=old["id"])[0]
    sessions.delete_session(session_id)
    task_registry.clear_session(session_id)
    try:
        from unittest.mock import AsyncMock
        from app.core import query_session_lifecycle
        attempted = AsyncMock(wraps=query_session_lifecycle.stop_all_background_tasks)
        monkeypatch.setattr(query_session_lifecycle, "stop_all_background_tasks", attempted)
        result = await consumer.consume(pending)
        assert await consumer.consume(pending) == result
        attempted.assert_awaited_once()
        assert any(r.get("incomplete") and r.get("task_id") == "legacy" for r in result)
        assert stop_receipts.pending("old-owner", turn_id=old["id"]) == []
        with base.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT consumed_at,result FROM turn_stop_receipts WHERE id=%s", (pending["id"],))
            row = cursor.fetchone()
            assert row["consumed_at"] is not None and row["result"]["incomplete"] is True
    finally:
        task_registry.clear_session(session_id)
