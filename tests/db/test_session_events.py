"""Unit tests for app/db/session_events.py — the append-only event log."""

import pytest

from app.db import session_events as se
from app.db import runs


def test_seqs_start_at_one_and_are_contiguous(mock_db):
    fs1, ls1 = se.append_events("s1", [{"type": "a"}, {"type": "b"}])
    assert (fs1, ls1) == (1, 2)
    fs2, ls2 = se.append_events("s1", [{"type": "c"}])
    assert (fs2, ls2) == (3, 3)
    fs3, ls3 = se.append_events("s1", [{"type": "d"}, {"type": "e"}])
    assert (fs3, ls3) == (4, 5)

    events = se.get_events("s1")
    assert [e["seq"] for e in events] == [1, 2, 3, 4, 5]
    assert [e["event_type"] for e in events] == ["a", "b", "c", "d", "e"]


def test_compaction_prefix_keeps_newest_100_at_boundary(mock_db):
    se.append_events("s1", [{"type": "system.compaction_boundary", "payload": {"index": i}}
                             for i in range(102)])
    se.append_events("s1", [{"type": "other"}])
    rows = se.get_compaction_events_before("s1", 102)
    assert len(rows) == 100
    assert [r["seq"] for r in rows] == list(range(2, 102))
    assert len(se.get_compaction_events_before("s1", 101)) == 100
    assert len(se.get_compaction_events_before("s1", 100)) == 99


def test_per_session_independent_seqs(mock_db):
    se.append_events("s1", [{"type": "a"}])
    fs, ls = se.append_events("s2", [{"type": "z"}])
    assert (fs, ls) == (1, 1)
    assert se.get_max_seq("s1") == 1
    assert se.get_max_seq("s2") == 1


def test_append_with_valid_fence_token(mock_db):
    run = runs.claim_run("s1", "runnerA", 60)
    fs, ls = se.append_events(
        "s1", [{"type": "a"}], run_id=run["id"], fence_token=run["fence_token"]
    )
    assert (fs, ls) == (1, 1)
    ev = se.get_events("s1")[0]
    assert ev["run_id"] == run["id"]


def test_stale_token_raises_fence_violation(mock_db):
    run = runs.claim_run("s1", "runnerA", 60)
    with pytest.raises(se.FenceViolation):
        se.append_events(
            "s1", [{"type": "a"}], run_id=run["id"],
            fence_token=run["fence_token"] + 1,
        )
    # Nothing was written.
    assert se.get_max_seq("s1") == 0


def test_non_active_run_raises_fence_violation(mock_db):
    run = runs.claim_run("s1", "runnerA", 60)
    assert runs.release_run(run["id"], run["fence_token"]) is True
    with pytest.raises(se.FenceViolation):
        se.append_events(
            "s1", [{"type": "a"}], run_id=run["id"],
            fence_token=run["fence_token"],
        )


def test_missing_run_raises_fence_violation(mock_db):
    with pytest.raises(se.FenceViolation):
        se.append_events(
            "s1", [{"type": "a"}], run_id="does-not-exist", fence_token=1
        )


def test_run_id_without_fence_token_raises_value_error(mock_db):
    run = runs.claim_run("s1", "runnerA", 60)
    with pytest.raises(ValueError):
        se.append_events("s1", [{"type": "a"}], run_id=run["id"])


def test_empty_events_raises_value_error(mock_db):
    with pytest.raises(ValueError):
        se.append_events("s1", [])
    assert se.get_max_seq("s1") == 0


def test_get_events_from_seq_and_limit(mock_db):
    se.append_events("s1", [{"type": f"e{i}"} for i in range(10)])
    # from_seq slices (seq strictly greater than from_seq).
    tail = se.get_events("s1", from_seq=7)
    assert [e["seq"] for e in tail] == [8, 9, 10]
    # limit caps the batch.
    capped = se.get_events("s1", from_seq=0, limit=3)
    assert [e["seq"] for e in capped] == [1, 2, 3]


def test_payload_roundtrips_as_dict(mock_db):
    se.append_events("s1", [{"type": "a", "payload": {"k": "v", "n": 1}}])
    ev = se.get_events("s1")[0]
    assert ev["payload"] == {"k": "v", "n": 1}


def test_get_max_seq_absent_is_zero(mock_db):
    assert se.get_max_seq("never-seen") == 0


def _stored_payload(conn, session_id):
    return conn.execute(
        "SELECT payload FROM session_events WHERE session_id = ? ORDER BY seq",
        (session_id,),
    ).fetchone()[0]


def test_illegal_code_points_are_scrubbed_from_payloads(mock_db):
    """Postgres text/json columns reject U+0000 and lone UTF-16 surrogates, so
    an event carrying either (a model writing '\\u0000' into tool input, say)
    used to abort the INSERT and drop the event. Both writers sanitize to
    U+FFFD first."""
    dirty = "a\x00b\ud800c"
    clean = "a�b�c"

    se.append_events("s1", [{"type": "a", "payload": {"text": dirty, dirty: 1}}])
    assert se.append_backfill_events(
        "s2", [{"type": "b", "payload": {"nested": [{"text": dirty}]}}]
    ) == (1, 1)

    for session_id in ("s1", "s2"):
        raw = _stored_payload(mock_db, session_id)
        assert "\\u0000" not in raw
        assert "\\ud800" not in raw
        assert "\\ufffd" in raw

    live = se.get_events("s1")[0]["payload"]
    assert live["text"] == clean
    # Dict keys are scrubbed too — a NUL in a key breaks the INSERT just as hard.
    assert list(live) == ["text", clean]
    assert se.get_events("s2")[0]["payload"]["nested"][0]["text"] == clean


def test_legal_payload_content_is_untouched(mock_db):
    se.append_events("s1", [{"type": "a", "payload": {"text": "plain ✓", "n": 1}}])
    assert se.get_events("s1")[0]["payload"] == {"text": "plain ✓", "n": 1}


def test_copy_session_events_preserves_turn_structure(mock_db):
    se.append_events("src", [
        {"type": "turn.started", "turn_id": "turn-a", "payload": {"profile_id": "p"}},
        {"type": "message.user", "turn_id": "turn-a", "payload": {"content": "hi"}},
        {"type": "tool.use", "turn_id": "turn-a", "payload": {"name": "Read"}},
        {"type": "turn.completed", "turn_id": "turn-a", "payload": {}},
        {"type": "turn.started", "turn_id": "turn-b", "payload": {"profile_id": "p"}},
        {"type": "message.user", "turn_id": "turn-b", "payload": {"content": "again"}},
    ])
    n = se.copy_session_events("src", "dst")
    assert n == 6

    copied = se.get_events("dst")
    assert [e["seq"] for e in copied] == [1, 2, 3, 4, 5, 6]
    assert [e["turn_id"] for e in copied] == [
        "turn-a", "turn-a", "turn-a", "turn-a", "turn-b", "turn-b",
    ]
    assert [e["event_type"] for e in copied] == [
        "turn.started", "message.user", "tool.use",
        "turn.completed", "turn.started", "message.user",
    ]
    assert copied[1]["payload"] == {"content": "hi"}
    assert {e["run_id"] for e in copied} == {None}
    assert {e["session_id"] for e in copied} == {"dst"}
    # Source is untouched and dest counter lets a later append continue.
    assert se.get_max_seq("src") == 6
    assert se.get_max_seq("dst") == 6
    fs, ls = se.append_events("dst", [{"type": "turn.completed", "turn_id": "turn-b"}])
    assert (fs, ls) == (7, 7)


def test_copy_session_events_empty_source_is_noop(mock_db):
    assert se.copy_session_events("missing", "dst") == 0
    assert se.get_max_seq("dst") == 0


def test_copy_session_events_refuses_to_overwrite(mock_db):
    se.append_events("src", [{"type": "a", "turn_id": "t1"}])
    se.append_events("dst", [{"type": "already", "turn_id": "keep"}])
    assert se.copy_session_events("src", "dst") == 0
    dest = se.get_events("dst")
    assert [e["event_type"] for e in dest] == ["already"]
    assert dest[0]["turn_id"] == "keep"


def test_copy_session_events_same_id_raises(mock_db):
    se.append_events("s1", [{"type": "a"}])
    with pytest.raises(ValueError):
        se.copy_session_events("s1", "s1")


def test_copy_session_events_rewrites_payload_session_id(mock_db):
    se.append_events("src", [{
        "type": "a",
        "payload": {"session_id": "src", "text": "x"},
    }])
    se.copy_session_events("src", "dst")
    assert se.get_events("dst")[0]["payload"] == {"session_id": "dst", "text": "x"}
    assert se.get_events("src")[0]["payload"]["session_id"] == "src"
