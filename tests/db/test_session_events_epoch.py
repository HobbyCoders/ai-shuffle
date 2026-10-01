"""Log epoch, tool.use lookup, and the short compaction idle window.

The epoch lets a resuming SSE client tell that the log it has a cursor into
was cleared or rewritten. It must change on every clear and must NOT change
for anything that keeps the same log (appends, delta compaction).
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.db import database
from app.db import session_events as se
from app.db.base import get_db


@pytest.fixture(autouse=True)
def _reset_rotation():
    se._compaction_cursor = ""
    yield
    se._compaction_cursor = ""


def _deltas(n, turn="t1"):
    return [
        {"type": "assistant.text_delta", "payload": {"text": f"x{i}"}, "turn_id": turn}
        for i in range(n)
    ]


def test_empty_log_has_the_empty_epoch(mock_db):
    assert se.get_log_epoch("nobody") == se.EMPTY_LOG_EPOCH


def test_epoch_is_stable_across_appends(mock_db):
    se.append_events("s1", [{"type": "turn.started"}])
    epoch = se.get_log_epoch("s1")
    se.append_events("s1", _deltas(3))

    assert epoch != se.EMPTY_LOG_EPOCH
    assert se.get_log_epoch("s1") == epoch


def test_epoch_changes_when_the_session_is_cleared(mock_db):
    database.create_session("s1", "default")
    se.append_events("s1", [{"type": "turn.started"}])
    before = se.get_log_epoch("s1")

    database.clear_session_conversation("s1")
    assert se.get_log_epoch("s1") == se.EMPTY_LOG_EPOCH

    se.append_events("s1", [{"type": "turn.started"}])
    after = se.get_log_epoch("s1")
    assert after not in (before, se.EMPTY_LOG_EPOCH)


def test_compacting_a_leading_delta_run_keeps_the_epoch(mock_db):
    """Compaction moves the run's first id onto the survivor, so the epoch of a
    log that starts with a delta run survives it."""
    se.append_events("s1", _deltas(5))
    se.append_events("s1", [{"type": "turn.completed", "turn_id": "t1"}])
    epoch = se.get_log_epoch("s1")

    assert se.compact_session_deltas("s1", keep_tail_events=0) == (1, 4)
    assert se.get_log_epoch("s1") == epoch


def test_has_event_at(mock_db):
    se.append_events("s1", _deltas(3))
    se.append_events("s1", [{"type": "turn.completed", "turn_id": "t1"}])
    se.compact_session_deltas("s1", keep_tail_events=0)

    assert [se.has_event_at("s1", n) for n in (1, 2, 3, 4, 5)] == [
        False, False, True, True, False,
    ]


def test_get_tool_use_seqs(mock_db):
    se.append_events("s1", [
        {"type": "tool.input_delta", "payload": {"tool_use_id": "a", "partial_json": "{"},
         "turn_id": "t1"},
        {"type": "tool.use", "payload": {"tool_use_id": "a", "name": "Read", "input": {}},
         "turn_id": "t1"},
        {"type": "tool.use", "payload": {"tool_use_id": "b", "name": "Read", "input": {}},
         "turn_id": "t2"},
        {"type": "tool.use", "payload": {"name": "no-id"}, "turn_id": "t2"},
    ])

    assert se.get_tool_use_seqs("s1") == {("t1", "a"): 2, ("t2", "b"): 3}
    assert se.get_tool_use_seqs("s1", 2) == {("t2", "b"): 3}


def _mk_session(session_id, *, idle):
    with get_db() as conn:
        conn.cursor().execute(
            "INSERT INTO sessions (id, title, status, updated_at) "
            "VALUES (%s, %s, %s, %s)",
            (session_id, session_id, "completed",
             (datetime.now(timezone.utc) - idle).isoformat()),
        )


def test_hour_window_compacts_recently_idle_sessions(mock_db):
    assert se.DELTA_COMPACTION_IDLE_HOURS <= 1
    _mk_session("idle-2h", idle=timedelta(hours=2))
    _mk_session("idle-10m", idle=timedelta(minutes=10))
    se.append_events("idle-2h", _deltas(10))
    se.append_events("idle-10m", _deltas(10))

    totals = se.compact_idle_session_deltas(
        older_than_hours=se.DELTA_COMPACTION_IDLE_HOURS, keep_tail_events=0
    )

    assert totals == {"sessions": 1, "runs": 1, "rows": 9}
    assert len(se.get_events("idle-2h")) == 1
    assert len(se.get_events("idle-10m")) == 10


def test_hour_window_still_skips_active_runs_and_the_tail(mock_db):
    from app.db import runs

    _mk_session("claimed", idle=timedelta(hours=2))
    _mk_session("short", idle=timedelta(hours=2))
    se.append_events("claimed", _deltas(10))
    se.append_events("short", _deltas(10))
    runs.claim_run("claimed", "runnerA", 60)

    totals = se.compact_idle_session_deltas(older_than_hours=1, keep_tail_events=0)
    assert totals["rows"] == 9  # only "short"
    assert len(se.get_events("claimed")) == 10
    # The default retained tail protects a session with less history than it.
    se._compaction_cursor = ""
    _mk_session("tail", idle=timedelta(hours=2))
    se.append_events("tail", _deltas(10))
    assert se.compact_idle_session_deltas(older_than_hours=1)["rows"] == 0


def test_maintenance_uses_the_hour_window(mock_db, monkeypatch):
    seen = {}

    def fake(*args, **kwargs):
        seen.update(kwargs, args=args)
        return {"sessions": 0, "runs": 0, "rows": 0}

    monkeypatch.setattr(se, "compact_idle_session_deltas", fake)
    database.run_database_maintenance()

    assert seen.get("older_than_hours") == se.DELTA_COMPACTION_IDLE_HOURS
