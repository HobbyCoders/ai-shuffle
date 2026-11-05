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
