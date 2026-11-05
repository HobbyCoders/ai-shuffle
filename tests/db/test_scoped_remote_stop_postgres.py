"""Actual PostgreSQL producer/receiver boundaries for generation-scoped Stop."""
import threading

import pytest

from tests.db.postgres_harness import requires_real_pg
from tests.db.test_event_write_lock_timeout_postgres import cluster, session_id, connect  # noqa: F401

pytestmark = requires_real_pg


@pytest.fixture
def child(session_id, monkeypatch):
    from app.core import remote_task
    from app.db import base, remote_tasks, turns
    monkeypatch.setattr(remote_task, "_require_worker_member", lambda _: None)  # signed auth tested separately
    turn = turns.enqueue_turn(session_id, "provider_agent", prompt="original remote work", profile_id="default")
    remote_tasks.init_schema()
    with base.get_db() as conn:
        remote_tasks.record_child(session_id, "parent", conn=conn, input_order=turn["insert_order"])
    return turn, {"generation": session_id, "epoch": 1}


def test_claim_rejects_an_actual_new_message_generation(child, session_id):
    from app.core import scoped_remote_stop
    from app.db import remote_tasks
    _, scope = child
    newer = remote_tasks.reserve_message_scope("new-message", session_id, "parent")
    assert newer["epoch"] > scope["epoch"]
    assert scoped_remote_stop.claim(session_id, scope, "parent") == {"accepted": False}
    assert remote_tasks.get_child(session_id)["stop_requested_at"] is None


def test_repeatable_stop_receipt_and_update_spare_concurrent_logical_resume(child, session_id, monkeypatch):
    from app.core import scoped_remote_stop, turn_manager
    from app.db import stop_receipts, turns
    queued, scope = child
    old = turns.start_turn(queued["id"], None, "old-owner")
    snapshot, resume = threading.Event(), threading.Event()
    original = turns._stop_receipt
    def gate(cursor, sid):
        snapshot.set()
        assert resume.wait(5)
        return original(cursor, sid)
    monkeypatch.setattr(turns, "_stop_receipt", gate)
    results, errors = [], []
    def stop():
        try:
            results.append(scoped_remote_stop.claim(session_id, scope, "parent"))
        except Exception as exc:
            errors.append(exc)
    thread = threading.Thread(target=stop)
    thread.start()
    try:
        assert snapshot.wait(5)
        turns.park_turn(old["id"])
        new_id = turn_manager.submit(session_id, "user", prompt="a new human instruction", no_kick=True)
        new = turns.get_turn(new_id)
        started = turns.start_turn(new_id, None, "new-owner", can_resume=lambda *_: True)
        assert started["id"] == old["id"]
        assert started["payload"]["accepted_input_order"] == new["insert_order"]
    finally:
        resume.set()
        thread.join(5)
    assert not thread.is_alive() and not errors
    [claimed] = results
    assert claimed["accepted"] and claimed["receipt"]["input_cutoff"] == old["insert_order"]
    assert claimed["turn_id"] == old["id"]
    assert turns.interrupt_turn(session_id, "stop", True, old["id"],
        source="user_stop", stop_receipt=claimed["receipt"]) is None
    current = turns.get_turn(old["id"])
    assert current["state"] == "running" and current["instance_id"] == "new-owner"
    assert current["payload"]["accepted_input_order"] == new["insert_order"]
    pending = stop_receipts.pending("old-owner", turn_id=old["id"])
    assert len(pending) == 1 and pending[0]["input_cutoff"] == old["insert_order"]
    assert stop_receipts.pending("new-owner", turn_id=old["id"]) == []


def test_pending_message_retry_cannot_replace_a_newer_incarnation(child, session_id):
    from app.db import remote_tasks
    first = remote_tasks.reserve_message_scope("pending-old", session_id, "parent")
    second = remote_tasks.reserve_message_scope("pending-new", session_id, "parent")
    assert second["epoch"] > first["epoch"]
    with pytest.raises(ValueError, match="superseded"):
        remote_tasks.reserve_message_scope("pending-old", session_id, "parent")
    assert remote_tasks.get_child(session_id)["scope_generation"] == "pending-new"
    assert remote_tasks.message_answer("pending-old")["answer"] == {"_scope_pending": True}
    answer = {"status": "queued", "stop_scope": second}
    assert remote_tasks.record_message("pending-new", worker_session_id=session_id,
        from_device_id="parent", direction="child", answer=answer) is True
    assert remote_tasks.message_answer("pending-new")["answer"] == answer


def test_stale_projection_cannot_pair_new_generation_with_old_admission(child, session_id):
    from app.db import background_tasks, turns
    old, scope = child
    snapshot = {"task_id": "projection", "session_id": session_id, "kind": "agent", "status": "running",
                "started_at": "2021-01-01T00:00:00Z", "remote_device_id": "owner",
                "turn_order": old["insert_order"], "remote_stop_scope": scope}
    background_tasks.upsert_background_task(snapshot)
    newer = turns.enqueue_turn(session_id, "user", prompt="new parent intent", profile_id="default")
    scope2 = {"generation": "new-message", "epoch": scope["epoch"] + 1}
    background_tasks.upsert_background_task({**snapshot, "turn_order": newer["insert_order"], "remote_stop_scope": scope2})
    background_tasks.upsert_background_task(snapshot)
    row = background_tasks.get_background_task("projection", session_id)
    assert row["turn_order"] == newer["insert_order"]
    assert row["remote_stop_scope"] == scope2
