"""Lossless delta compaction for ``session_events`` (audit B6).

``session_events`` has never been pruned: a streamed answer costs one row per
~100 ms chunk and they accumulate forever. The retention pass is a COMPACTION,
not a deletion — it performs exactly the merge the SSE replay already performs
at read time, so the bytes a client receives are unchanged.

The load-bearing assertion in this file is
``test_replay_output_is_byte_identical_after_compaction``: everything else is a
guard rail around it.
"""

from datetime import datetime, timedelta, timezone

import pytest

import app.api.events as events_mod
from app.db.base import get_db
from app.db import session_events as se


@pytest.fixture(autouse=True)
def _reset_rotation():
    """The driver's round-robin cursor is module state — isolate each test."""
    se._compaction_cursor = ""
    yield
    se._compaction_cursor = ""


def _iso(days_ago):
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def _mk_session(session_id, *, idle_days):
    with get_db() as conn:
        conn.cursor().execute(
            "INSERT INTO sessions (id, title, status, updated_at) "
            "VALUES (%s, %s, %s, %s)",
            (session_id, session_id, "completed", _iso(idle_days)),
        )


def _replay_frames(session_id, from_seq=0):
    """Exactly what the SSE backfill would put on the wire from ``from_seq``."""
    pending = [None]
    rows = se.get_events(session_id, from_seq, 10_000)
    frames = [
        events_mod._frame(row) for row in events_mod._coalesce_deltas(rows, pending)
    ]
    frames += [events_mod._frame(row) for row in events_mod._flush_pending(pending)]
    return frames


def _deltas(n, text="x", turn="t1"):
    return [
        {"type": "assistant.text_delta", "payload": {"text": f"{text}{i}"}, "turn_id": turn}
        for i in range(n)
    ]


def test_compactable_types_match_the_replay_coalescer():
    """The pass is only lossless because it merges precisely what the replay
    merges. Pin the pairing so the two lists cannot drift apart."""
    assert set(se.COMPACTABLE_DELTA_TYPES) == set(events_mod._COALESCE_TYPES)


def test_replay_output_is_byte_identical_after_compaction(mock_db):
    se.append_events("s1", [{"type": "turn.started", "turn_id": "t1"}])
    se.append_events("s1", _deltas(20))
    se.append_events("s1", [{"type": "turn.completed", "turn_id": "t1"}])

    before = _replay_frames("s1")
    runs, removed = se.compact_session_deltas("s1", keep_tail_events=0)

    assert (runs, removed) == (1, 19)
    assert _replay_frames("s1") == before


def test_a_resumed_client_below_the_run_sees_identical_frames(mock_db):
    se.append_events("s1", [{"type": "turn.started", "turn_id": "t1"}])
    se.append_events("s1", _deltas(10))
    se.append_events("s1", [{"type": "turn.completed", "turn_id": "t1"}])

    before = {seq: _replay_frames("s1", seq) for seq in (0, 1, 11, 12)}
    se.compact_session_deltas("s1", keep_tail_events=0)

    for seq, frames in before.items():
        assert _replay_frames("s1", seq) == frames, f"from_seq={seq}"


def test_the_surviving_row_carries_the_first_envelope_at_the_last_seq(mock_db):
    se.append_events("s1", _deltas(4))
    rows_before = se.get_events("s1")
    se.compact_session_deltas("s1", keep_tail_events=0)
    rows_after = se.get_events("s1")

    assert len(rows_after) == 1
    survivor = rows_after[0]
    assert survivor["seq"] == rows_before[-1]["seq"]      # last seq
    assert survivor["id"] == rows_before[0]["id"]         # first envelope
    assert survivor["created_at"] == rows_before[0]["created_at"]
    assert survivor["payload"]["text"] == "x0x1x2x3"      # joined text


def test_seqs_are_never_reused_and_appends_continue(mock_db):
    se.append_events("s1", _deltas(5))
    assert se.get_max_seq("s1") == 5

    se.compact_session_deltas("s1", keep_tail_events=0)

    assert se.get_max_seq("s1") == 5  # the counter is untouched
    first, last = se.append_events("s1", [{"type": "turn.completed"}])
    assert (first, last) == (6, 6)
    assert [r["seq"] for r in se.get_events("s1")] == [5, 6]


def test_the_retained_tail_is_never_touched(mock_db):
    se.append_events("s1", _deltas(30))

    runs, removed = se.compact_session_deltas("s1", keep_tail_events=25)

    # Only seqs <= 5 are eligible; the live/resumable tail keeps every row.
    assert (runs, removed) == (1, 4)
    assert [r["seq"] for r in se.get_events("s1")] == [5] + list(range(6, 31))


def test_runs_are_not_merged_across_a_non_delta_row(mock_db):
    se.append_events("s1", _deltas(3, turn="t1"))
    se.append_events("s1", [{"type": "tool.use", "turn_id": "t1"}])
    se.append_events("s1", _deltas(3, text="y", turn="t1"))

    before = _replay_frames("s1")
    runs, removed = se.compact_session_deltas("s1", keep_tail_events=0)

    assert (runs, removed) == (2, 4)
    assert [r["event_type"] for r in se.get_events("s1")] == [
        "assistant.text_delta", "tool.use", "assistant.text_delta",
    ]
    assert _replay_frames("s1") == before


def test_runs_are_not_merged_across_turns(mock_db):
    se.append_events("s1", _deltas(3, turn="t1"))
    se.append_events("s1", _deltas(3, text="y", turn="t2"))

    before = _replay_frames("s1")
    runs, removed = se.compact_session_deltas("s1", keep_tail_events=0)

    assert (runs, removed) == (2, 4)
    assert _replay_frames("s1") == before


def test_non_delta_events_are_never_removed(mock_db):
    kept = [
        {"type": "message.user", "turn_id": "t1"},
        {"type": "tool.result", "turn_id": "t1"},
        {"type": "turn.usage", "turn_id": "t1"},
        {"type": "subagent.started", "payload": {"task_id": "k"}, "turn_id": "t1"},
        {"type": "message.assistant", "turn_id": "t1"},
    ]
    se.append_events("s1", kept)

    assert se.compact_session_deltas("s1", keep_tail_events=0) == (0, 0)
    assert [r["event_type"] for r in se.get_events("s1")] == [e["type"] for e in kept]


def test_compaction_is_idempotent(mock_db):
    se.append_events("s1", _deltas(6))
    se.compact_session_deltas("s1", keep_tail_events=0)
    frames = _replay_frames("s1")

    assert se.compact_session_deltas("s1", keep_tail_events=0) == (0, 0)
    assert _replay_frames("s1") == frames


def test_dry_run_reports_without_writing(mock_db):
    se.append_events("s1", _deltas(5))
    before = se.get_events("s1")

    assert se.compact_session_deltas("s1", keep_tail_events=0, dry_run=True) == (1, 4)
    assert se.get_events("s1") == before


def test_unknown_session_is_a_noop(mock_db):
    assert se.compact_session_deltas("nope", keep_tail_events=0) == (0, 0)


def test_max_runs_caps_the_work_and_stays_lossless(mock_db):
    for turn in range(4):
        se.append_events("s1", _deltas(3, turn=f"t{turn}"))

    before = _replay_frames("s1")
    runs, _removed = se.compact_session_deltas("s1", keep_tail_events=0, max_runs=2)

    assert runs == 2
    assert _replay_frames("s1") == before  # partial work is still output-identical


# ---------------------------------------------------------------------------
# The idle-session driver (the maintenance entry point)
# ---------------------------------------------------------------------------


def test_idle_driver_compacts_only_long_idle_sessions(mock_db):
    _mk_session("old", idle_days=30)
    _mk_session("fresh", idle_days=0)
    se.append_events("old", _deltas(10))
    se.append_events("fresh", _deltas(10))
    fresh_before = _replay_frames("fresh")

    totals = se.compact_idle_session_deltas(7, keep_tail_events=0)

    assert totals == {"sessions": 1, "runs": 1, "rows": 9}
    assert len(se.get_events("old")) == 1
    assert _replay_frames("fresh") == fresh_before
    assert len(se.get_events("fresh")) == 10


def test_idle_driver_skips_a_session_with_a_live_run(mock_db):
    """Belt-and-braces on top of the idle window: never touch a claimed session."""
    from app.db import runs

    _mk_session("old", idle_days=30)
    se.append_events("old", _deltas(10))
    runs.claim_run("old", "runnerA", 60)

    assert se.compact_idle_session_deltas(7, keep_tail_events=0)["rows"] == 0
    assert len(se.get_events("old")) == 10


def test_idle_driver_skips_sessions_with_no_sessions_row(mock_db):
    se.append_events("orphan", _deltas(10))

    assert se.compact_idle_session_deltas(7, keep_tail_events=0)["rows"] == 0
    assert len(se.get_events("orphan")) == 10


def test_idle_driver_respects_the_retention_floor(mock_db):
    _mk_session("old", idle_days=30)
    se.append_events("old", _deltas(10))

    # Not enough history past the retained tail -> nothing eligible.
    assert se.compact_idle_session_deltas(7, keep_tail_events=1000)["rows"] == 0
    assert len(se.get_events("old")) == 10


def test_idle_driver_is_disabled_by_a_non_positive_window(mock_db):
    _mk_session("old", idle_days=30)
    se.append_events("old", _deltas(10))

    assert se.compact_idle_session_deltas(0, keep_tail_events=0) == {
        "sessions": 0, "runs": 0, "rows": 0,
    }
    assert len(se.get_events("old")) == 10


def test_idle_driver_rotates_across_passes(mock_db):
    for name in ("a-sess", "b-sess", "c-sess"):
        _mk_session(name, idle_days=30)
        se.append_events(name, _deltas(4))

    first = se.compact_idle_session_deltas(7, keep_tail_events=0, max_sessions=1)
    second = se.compact_idle_session_deltas(7, keep_tail_events=0, max_sessions=1)
    third = se.compact_idle_session_deltas(7, keep_tail_events=0, max_sessions=1)

    assert [first["sessions"], second["sessions"], third["sessions"]] == [1, 1, 1]
    for name in ("a-sess", "b-sess", "c-sess"):
        assert len(se.get_events(name)) == 1


def test_idle_driver_shares_a_caller_transaction(mock_db):
    _mk_session("old", idle_days=30)
    se.append_events("old", _deltas(6))

    with get_db() as conn:
        cur = conn.cursor()
        totals = se.compact_idle_session_deltas(7, keep_tail_events=0, cursor=cur)

    assert totals["rows"] == 5
    assert len(se.get_events("old")) == 1
