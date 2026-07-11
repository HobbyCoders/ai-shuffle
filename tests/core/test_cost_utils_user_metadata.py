"""Unit tests for the per-session user-message metadata cache (app.core.cost_utils).

PR #560 follow-up: transcript reads (SDK store / JSONL) rebuild user messages
from the SDK payload, which never carried the DB row's metadata — so an
automation run's {automation_id, automation_run_id} badge was lost on the read
path. The cache pairs each transcript user message with its DB row positionally
(per-content queues, same pattern as the profile map) and merges the metadata
back in.
"""

from unittest.mock import patch

import app.core.cost_utils as cost_utils


def _clear_caches():
    cost_utils.invalidate_profile_cache()


AUTO_META = {"automation_id": "auto-1", "automation_run_id": "run-1"}


def test_get_cached_user_metadata_map_builds_ordered_lists():
    """Every DB user row contributes an entry — metadata-less rows as None —
    so the queues stay positionally aligned with the rows."""
    _clear_caches()
    rows = [
        {"role": "user", "content": "hi", "metadata": AUTO_META},
        {"role": "assistant", "content": "reply", "metadata": {"ignored": True}},
        {"role": "user", "content": "hi", "metadata": None},
        {"role": "user", "content": "bye", "metadata": {"k": "v"}},
    ]
    with patch("app.db.database.get_session_messages", return_value=rows):
        meta_map = cost_utils.get_cached_user_metadata_map("sess-m1")

    assert meta_map == {"hi": [AUTO_META, None], "bye": [{"k": "v"}]}
    _clear_caches()


def test_update_user_metadata_map_appends_when_primed():
    _clear_caches()
    with patch("app.db.database.get_session_messages", return_value=[]):
        cost_utils.get_cached_user_metadata_map("sess-m2")  # prime empty entry

    cost_utils.update_user_metadata_map("sess-m2", "same", AUTO_META)
    cost_utils.update_user_metadata_map("sess-m2", "same", None)

    assert cost_utils.get_cached_user_metadata_map("sess-m2") == {
        "same": [AUTO_META, None]
    }
    _clear_caches()


def test_update_user_metadata_map_noop_when_not_primed():
    """An unprimed session must not grow a partial (misaligned) cache entry."""
    _clear_caches()
    cost_utils.update_user_metadata_map("sess-m3", "hi", AUTO_META)

    with cost_utils._user_meta_cache_lock:
        assert "sess-m3" not in cost_utils._user_meta_cache
    _clear_caches()


def test_attach_merges_db_metadata_onto_transcript_user_rows():
    """The automation badge lands on the transcript user message; assistant
    rows and no-metadata user rows are untouched."""
    _clear_caches()
    db_rows = [
        {"role": "user", "content": "do it", "metadata": AUTO_META},
        {"role": "user", "content": "plain", "metadata": None},
    ]
    transcript = [
        {"role": "user", "content": "do it", "type": "text"},
        {"role": "assistant", "content": "done", "type": "text"},
        {"role": "user", "content": "plain", "type": "text"},
    ]
    with patch("app.db.database.get_session_messages", return_value=db_rows):
        result = cost_utils.attach_user_message_metadata(transcript, "sess-m4")

    assert result is transcript  # mutated in place, returned for chaining
    assert transcript[0]["metadata"] == AUTO_META
    assert "metadata" not in transcript[1]
    assert "metadata" not in transcript[2]
    _clear_caches()


def test_attach_transcript_metadata_wins_on_key_conflict():
    """Messages that already carry metadata keep their own keys; DB-only keys
    are merged in underneath (a DB-read message merging itself is a no-op)."""
    _clear_caches()
    db_rows = [
        {"role": "user", "content": "q", "metadata": {"automation_id": "a", "shared": "db"}},
    ]
    transcript = [
        {"role": "user", "content": "q", "metadata": {"shared": "transcript"}},
    ]
    with patch("app.db.database.get_session_messages", return_value=db_rows):
        cost_utils.attach_user_message_metadata(transcript, "sess-m5")

    assert transcript[0]["metadata"] == {"automation_id": "a", "shared": "transcript"}
    _clear_caches()


def test_attach_pairs_repeated_content_positionally():
    """Same prompt sent twice — once by an automation, once by hand — badges
    only the matching occurrence, in creation order."""
    _clear_caches()
    db_rows = [
        {"role": "user", "content": "repeat", "metadata": AUTO_META},
        {"role": "user", "content": "repeat", "metadata": None},
    ]
    transcript = [
        {"role": "user", "content": "repeat", "type": "text"},
        {"role": "user", "content": "repeat", "type": "text"},
    ]
    with patch("app.db.database.get_session_messages", return_value=db_rows):
        cost_utils.attach_user_message_metadata(transcript, "sess-m6")

    assert transcript[0]["metadata"] == AUTO_META
    assert "metadata" not in transcript[1]
    _clear_caches()


def test_invalidate_profile_cache_clears_metadata_cache_too():
    _clear_caches()
    rows = [{"role": "user", "content": "hi", "metadata": AUTO_META}]
    with patch("app.db.database.get_session_messages", return_value=rows):
        cost_utils.get_cached_user_metadata_map("sess-m7")

    cost_utils.invalidate_profile_cache("sess-m7")
    with cost_utils._user_meta_cache_lock:
        assert "sess-m7" not in cost_utils._user_meta_cache

    # And the clear-all form
    with patch("app.db.database.get_session_messages", return_value=rows):
        cost_utils.get_cached_user_metadata_map("sess-m8")
    cost_utils.invalidate_profile_cache()
    with cost_utils._user_meta_cache_lock:
        assert len(cost_utils._user_meta_cache) == 0
