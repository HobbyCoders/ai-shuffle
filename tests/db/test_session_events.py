"""Unit tests for app/db/session_events.py — the append-only event log."""

import pytest

from app.db import session_events as se
from app.db import runs


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
