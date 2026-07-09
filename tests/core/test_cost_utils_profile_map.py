"""Unit tests for the per-session profile-map cache (app.core.cost_utils).

The profile map resolves a user message's profile id from the DB user rows.
It must preserve ORDER so a session whose profile changed mid-conversation —
possibly repeating the same prompt text — pairs each message to its own
profile rather than collapsing onto the last write.
"""

from unittest.mock import patch

import app.core.cost_utils as cost_utils


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
    with patch("app.db.database.get_session_messages", return_value=rows):
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
    with patch("app.db.database.get_session_messages", return_value=rows):
        queues = cost_utils.build_profile_queues("sess-2")

    q = queues["repeat"]
    assert q.popleft() == "first"
    assert q.popleft() == "second"
    assert len(q) == 0
    _clear_cache()


def test_update_profile_map_appends_preserving_order():
    """Incremental writes append (not overwrite) so duplicates are kept."""
    _clear_cache()
    with patch("app.db.database.get_session_messages", return_value=[]):
        cost_utils.get_cached_profile_map("sess-3")  # seed empty cache entry

    cost_utils.update_profile_map("sess-3", "same", "a")
    cost_utils.update_profile_map("sess-3", "same", "b")

    assert cost_utils.get_cached_profile_map("sess-3") == {"same": ["a", "b"]}
    _clear_cache()
