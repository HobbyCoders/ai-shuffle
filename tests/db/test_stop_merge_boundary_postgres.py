"""Real PostgreSQL interleavings for Stop's wake-merge boundary."""
from uuid import uuid4

import pytest

from tests.db.postgres_harness import requires_real_pg
from tests.db.test_event_write_lock_timeout_postgres import (
    cluster, session_id,  # noqa: F401 - private-cluster fixtures
)

pytestmark = requires_real_pg


@pytest.mark.parametrize("caller_owned", [False, True])
def test_stop_between_wake_filter_and_insert_cancels_entire_merge(session_id, monkeypatch, caller_owned):
    from app.db import base, turns, wakeups

    old = wakeups.create_wakeup(id=uuid4().hex, session_id=session_id,
                                prompt="OLD instruction", source="held_peer_promotion", fire_at=1000)
    real_cursor = wakeups.get_cursor
    stopped = []

    class StopBeforeInsert:
        def __init__(self, cursor):
            self.cursor = cursor

        def execute(self, sql, params=None):
            if "INSERT INTO pending_wakeups" in sql and not stopped:
                stopped.append(turns.read_stop_receipt(session_id))
            return self.cursor.execute(sql, params)

        def __getattr__(self, name):
            return getattr(self.cursor, name)

    monkeypatch.setattr(wakeups, "get_cursor", lambda conn: StopBeforeInsert(real_cursor(conn)))

    def merge(conn=None):
        return wakeups.merge_session_wakeup(
            id=uuid4().hex, session_id=session_id, prompt="NEW instruction",
            source="held_peer_promotion", fire_at=2000, _conn=conn,
        )

    if caller_owned:
        with base.get_db() as conn:
            merged, replaced = merge(conn)
    else:
        merged, replaced = merge()

    assert replaced == [old["id"]]
    assert stopped[0]["stop_seq"] == old["stop_seq"] + 1
    assert merged["stop_seq"] == old["stop_seq"]
    assert wakeups.claim_wakeup(merged["id"]) is False


def test_merge_after_stop_does_not_keep_old_text(session_id):
    from app.db import turns, wakeups

    old = wakeups.create_wakeup(id=uuid4().hex, session_id=session_id,
                                prompt="OLD instruction", source="held_peer_promotion", fire_at=1000)
    receipt = turns.read_stop_receipt(session_id)
    merged, replaced = wakeups.merge_session_wakeup(
        id=uuid4().hex, session_id=session_id, prompt="NEW instruction",
        source="held_peer_promotion", fire_at=2000,
    )
    assert replaced == [old["id"]]
    assert merged["prompt"] == "NEW instruction"
    assert merged["stop_seq"] == receipt["stop_seq"]
    assert wakeups.claim_wakeup(merged["id"]) is True


@pytest.mark.parametrize("caller_owned", [False, True])
def test_older_merge_leaves_newer_stop_generation_wake(session_id, monkeypatch, caller_owned):
    from app.db import base, turns, wakeups

    real = wakeups.get_cursor
    future = []

    class NewWakeBeforeSelect:
        def __init__(self, cursor):
            self.cursor = cursor
        def execute(self, sql, params=None):
            if "SELECT * FROM pending_wakeups" in sql and not future:
                turns.read_stop_receipt(session_id)
                future.append(wakeups.create_wakeup(id=uuid4().hex, session_id=session_id,
                              prompt="NEW after Stop", source="held_peer_promotion", fire_at=1000))
            return self.cursor.execute(sql, params)
        def __getattr__(self, name):
            return getattr(self.cursor, name)

    monkeypatch.setattr(wakeups, "get_cursor", lambda conn: NewWakeBeforeSelect(real(conn)))
    def merge(conn=None):
        return wakeups.merge_session_wakeup(id=uuid4().hex, session_id=session_id,
                       prompt="OLD merge", source="held_peer_promotion", fire_at=2000, _conn=conn)
    if caller_owned:
        with base.get_db() as conn:
            merged, replaced = merge(conn)
    else:
        merged, replaced = merge()
    assert future[0]["stop_seq"] > merged["stop_seq"]
    assert future[0]["id"] not in replaced
    assert "NEW after Stop" not in merged["prompt"]
    assert wakeups.claim_wakeup(merged["id"]) is False
    assert wakeups.claim_wakeup(future[0]["id"]) is True


@pytest.mark.parametrize("compact_first", [False, True])
def test_claimed_wake_keeps_stop_sequence_through_durable_admission(session_id, compact_first):
    from app.db import turns, wakeups

    armed = wakeups.create_wakeup(id=uuid4().hex, session_id=session_id,
                                 prompt="cancelled", source="limit_resume", fire_at=1)
    assert wakeups.claim_wakeup(armed["id"])
    turns.read_stop_receipt(session_id)
    admitted = turns.enqueue_turn(session_id, "wakeup", prompt="cancelled", profile_id="default",
                                 payload={turns.WAKE_STOP_SEQ_KEY: armed["stop_seq"],
                                          "compact_first": compact_first})
    assert admitted["state"] == "aborted"
    assert turns.get_queued_turns(session_id) == []
    assert turns.start_turn(admitted["id"], None, "peer-runner") is None
    fresh = wakeups.create_wakeup(id=uuid4().hex, session_id=session_id,
                                 prompt="new", source="limit_resume", fire_at=2)
    assert wakeups.claim_wakeup(fresh["id"])
    new = turns.enqueue_turn(session_id, "wakeup", prompt="new", profile_id="default",
                             payload={turns.WAKE_STOP_SEQ_KEY: fresh["stop_seq"]})
    assert turns.start_turn(new["id"], None, "peer-runner") is not None
