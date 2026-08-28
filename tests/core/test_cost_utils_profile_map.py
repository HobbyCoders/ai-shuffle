"""Unit tests for the per-session profile-map cache (app.core.cost_utils).

The profile map resolves a user message's profile id from the DB user rows.
It must preserve ORDER so a session whose profile changed mid-conversation —
possibly repeating the same prompt text — pairs each message to its own
profile rather than collapsing onto the last write.
"""

from unittest.mock import patch

import app.core.cost_utils as cost_utils


def _user_rows(db_rows):
    """Patch the narrow user-row read with rows given in DB-row form.

    ``cost_utils`` no longer runs ``SELECT * FROM session_messages`` per cache
    miss — it reads only ``(content, profile_id, metadata)`` for the session's
    USER rows. These tests still express their fixtures as full DB rows, so
    this converts them the same way the projection would.
    """
    return patch(
        "app.core.cost_utils._load_user_message_rows",
        return_value=[
            (
                r.get("content", ""),
                r.get("profile_id"),
                r.get("metadata") if isinstance(r.get("metadata"), dict) else None,
            )
            for r in db_rows
            if r.get("role") == "user"
        ],
    )


def _clear_cache():
    with cost_utils._profile_map_cache_lock:
        cost_utils._profile_map_cache.clear()


def test_get_cached_profile_map_builds_ordered_lists():
    """Rows are grouped by content into ordered profile-id lists."""
    _clear_cache()
    rows = [
        {"role": "user", "content": "hi", "profile_id": "p1"},
        {"role": "assistant", "content": "reply", "profile_id": None},
        {"role": "user", "content": "hi", "profile_id": "p2"},
        {"role": "user", "content": "bye", "profile_id": "p3"},
    ]
    with _user_rows(rows):
        profile_map = cost_utils.get_cached_profile_map("sess-1")

    assert profile_map == {"hi": ["p1", "p2"], "bye": ["p3"]}
    _clear_cache()


def test_build_profile_queues_pops_in_order():
    """Consuming a content's queue yields the ids in creation order."""
    _clear_cache()
    rows = [
        {"role": "user", "content": "repeat", "profile_id": "first"},
        {"role": "user", "content": "repeat", "profile_id": "second"},
    ]
    with _user_rows(rows):
        queues = cost_utils.build_profile_queues("sess-2")

    q = queues["repeat"]
    assert q.popleft() == "first"
    assert q.popleft() == "second"
    assert len(q) == 0
    _clear_cache()


def test_update_profile_map_appends_preserving_order():
    """Incremental writes append (not overwrite) so duplicates are kept."""
    _clear_cache()
    with _user_rows([]):
        cost_utils.get_cached_profile_map("sess-3")  # seed empty cache entry

    cost_utils.update_profile_map("sess-3", "same", "a")
    cost_utils.update_profile_map("sess-3", "same", "b")

    assert cost_utils.get_cached_profile_map("sess-3") == {"same": ["a", "b"]}
    _clear_cache()


def test_attach_turn_profile_ids_carries_turn_profile_to_all_rows():
    """The turn's profile (from its user row) flows onto that turn's
    assistant/thinking/tool rows; a mid-session switch re-attributes only the
    later turn. Both snake_case and camelCase keys are written."""
    _clear_cache()
    db_rows = [
        {"role": "user", "content": "q1", "profile_id": "p-a"},
        {"role": "user", "content": "q2", "profile_id": "p-b"},
    ]
    # The transcript carries a profile only on user rows; the rest inherit.
    transcript = [
        {"role": "user", "content": "q1", "type": "text"},
        {"role": "assistant", "content": "a1", "type": "text"},
        {"role": "assistant", "content": "t1", "type": "thinking"},
        {"role": "user", "content": "q2", "type": "text"},
        {"role": "assistant", "content": "a2", "type": "text"},
    ]
    with _user_rows(db_rows):
        result = cost_utils.attach_turn_profile_ids("sess-4", transcript)

    assert result is transcript  # mutated in place, returned for chaining
    assert [m["profile_id"] for m in transcript] == [
        "p-a", "p-a", "p-a", "p-b", "p-b",
    ]
    assert transcript[0]["profileId"] == "p-a"
    _clear_cache()


def test_attach_turn_profile_ids_leaves_unresolved_turn_untouched():
    """A user row with no DB profile match (unresolved) leaves its turn's rows
    unstamped rather than mislabelling them with a neighbouring turn."""
    _clear_cache()
    db_rows = [{"role": "user", "content": "known", "profile_id": "p-a"}]
    transcript = [
        {"role": "user", "content": "unknown", "type": "text"},
        {"role": "assistant", "content": "a0", "type": "text"},
        {"role": "user", "content": "known", "type": "text"},
        {"role": "assistant", "content": "a1", "type": "text"},
    ]
    with _user_rows(db_rows):
        cost_utils.attach_turn_profile_ids("sess-5", transcript)

    assert "profile_id" not in transcript[0]
    assert "profile_id" not in transcript[1]
    assert transcript[2]["profile_id"] == "p-a"
    assert transcript[3]["profile_id"] == "p-a"
    _clear_cache()


def test_attach_turn_profile_ids_both_keys_false_writes_snake_only():
    """When both_keys=False only the snake_case profile_id is written."""
    _clear_cache()
    db_rows = [{"role": "user", "content": "q", "profile_id": "p-a"}]
    transcript = [{"role": "user", "content": "q", "type": "text"}]
    with _user_rows(db_rows):
        cost_utils.attach_turn_profile_ids("sess-6", transcript, both_keys=False)

    assert transcript[0]["profile_id"] == "p-a"
    assert "profileId" not in transcript[0]
    _clear_cache()
