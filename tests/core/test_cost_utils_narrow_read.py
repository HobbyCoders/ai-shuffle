"""The per-session metadata caches read a narrow projection, not ``SELECT *``.

Finding B2: every cache miss on a session open ran
``get_session_messages`` — a full ``SELECT * FROM session_messages`` for the
session (all assistant rows, all tool payloads) — and did it TWICE, once for
the profile map and once for the user-metadata map.
"""

from unittest.mock import patch

import pytest

import app.core.cost_utils as cost_utils
from app.db import database


@pytest.fixture
def seeded_session(mock_db):
    """A session with two user rows (one carrying metadata) and one assistant row."""
    cost_utils.invalidate_profile_cache()
    database.create_session(session_id="narrow-1", profile_id="default", title="t")
    database.add_session_message(
        "narrow-1", "user", "first", profile_id="p-a",
        metadata={"automation_id": "auto-1"},
    )
    database.add_session_message("narrow-1", "assistant", "answer" * 5000)
    database.add_session_message("narrow-1", "user", "second", profile_id="p-b")
    yield mock_db
    cost_utils.invalidate_profile_cache()


def _traced(raw_conn, fn):
    statements = []
    raw_conn.set_trace_callback(statements.append)
    try:
        result = fn()
    finally:
        raw_conn.set_trace_callback(None)
    return result, statements


def test_profile_map_is_built_from_the_narrow_projection(seeded_session):
    result, statements = _traced(
        seeded_session, lambda: cost_utils.get_cached_profile_map("narrow-1")
    )

    assert result == {"first": ["p-a"], "second": ["p-b"]}
    reads = [s for s in statements if "session_messages" in s]
    assert len(reads) == 1, reads
    assert "SELECT *" not in reads[0]
    assert "role = 'user'" in reads[0]


def test_one_read_fills_both_caches(seeded_session):
    """A miss on either map fills both — a session open pays for one read."""
    cost_utils.get_cached_profile_map("narrow-1")

    _, statements = _traced(
        seeded_session, lambda: cost_utils.get_cached_user_metadata_map("narrow-1")
    )
    assert [s for s in statements if "session_messages" in s] == []


def test_metadata_map_matches_the_rows(seeded_session):
    assert cost_utils.get_cached_user_metadata_map("narrow-1") == {
        "first": [{"automation_id": "auto-1"}],
        "second": [None],
    }


def test_select_star_helper_is_no_longer_used(seeded_session):
    """The wide read must not be reachable from the cache-miss path."""
    with patch("app.db.database.get_session_messages",
               side_effect=AssertionError("SELECT * on the session-open path")):
        assert cost_utils.get_cached_profile_map("narrow-1") == {
            "first": ["p-a"], "second": ["p-b"],
        }


def test_wide_read_is_still_the_fallback(mock_db):
    """A failed narrow read degrades to the old path rather than losing data."""
    cost_utils.invalidate_profile_cache()
    rows = [{"role": "user", "content": "hi", "profile_id": "p1", "metadata": None}]
    with patch("app.db.base.get_db", side_effect=RuntimeError("no pool")), \
            patch("app.db.database.get_session_messages", return_value=rows):
        assert cost_utils.get_cached_profile_map("fallback-1") == {"hi": ["p1"]}
    cost_utils.invalidate_profile_cache()


def test_oversized_maps_are_served_but_not_cached(mock_db):
    """Bounded cache: entry COUNT was capped, entry SIZE was not."""
    cost_utils.invalidate_profile_cache()
    rows = [
        (f"content-{i}", f"p-{i}", None)
        for i in range(cost_utils._MAX_CACHED_MAP_VALUES + 1)
    ]
    with patch("app.core.cost_utils._load_user_message_rows", return_value=rows):
        built = cost_utils.get_cached_profile_map("huge-1")

    assert len(built) == cost_utils._MAX_CACHED_MAP_VALUES + 1  # served in full
    with cost_utils._profile_map_cache_lock:
        assert "huge-1" not in cost_utils._profile_map_cache  # but not retained
    cost_utils.invalidate_profile_cache()


def test_concurrent_append_during_a_read_does_not_raise(seeded_session):
    """The cached map is mutated by the write path while readers iterate it.

    Without a snapshot under the cache lock this races into
    "dictionary changed size during iteration" mid-request.
    """
    import threading

    cost_utils.get_cached_profile_map("narrow-1")  # prime the cache entry

    errors = []

    def writer():
        try:
            for i in range(2000):
                cost_utils.update_profile_map("narrow-1", f"c{i}", "p")
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    thread = threading.Thread(target=writer, daemon=True)
    thread.start()
    try:
        while thread.is_alive():
            cost_utils.build_profile_queues("narrow-1")
    except Exception as exc:  # pragma: no cover - failure path
        errors.append(exc)
    thread.join(timeout=10)

    assert not errors, errors
