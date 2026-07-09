"""
Tests that ``resolve_for_streaming_init`` attaches per-turn profile ids to the
seed transcript it feeds into ``LiveSessionStore.start_streaming``.

Raw JSONL/store transcripts record a profile only on USER rows. If the seed
didn't carry each turn's profile onto its assistant/thinking rows, then while a
new turn streams the historical bubbles would fall back to the session's CURRENT
selector and re-label on a profile switch — the exact bug this guards.

Only the DB boundary and the transcript parser are patched; the walk itself
(cost_utils.attach_turn_profile_ids) runs for real.
"""

from unittest.mock import patch

import app.core.cost_utils as cost_utils
from app.core.message_resolver import MessageResolver


def _clear_profile_cache():
    with cost_utils._profile_map_cache_lock:
        cost_utils._profile_map_cache.clear()


def _run_seed(transcript, db_rows, *, store_patch):
    """Drive resolve_for_streaming_init with the given transcript source patched.

    ``store_patch`` names the branch to feed: "store" (postgres_only path via
    _parse_store_transcript) or "jsonl" (parse_session_history fallback).
    """
    patches = [patch("app.db.database.get_session_messages", return_value=db_rows)]
    if store_patch == "store":
        patches.append(patch(
            "app.core.message_resolver._parse_store_transcript",
            return_value=transcript,
        ))
    else:
        # Force the JSONL fallback: store parse yields nothing so the resolver
        # drops through to parse_session_history.
        patches.append(patch(
            "app.core.message_resolver._parse_store_transcript",
            return_value=None,
        ))
        patches.append(patch(
            "app.core.jsonl_parser.parse_session_history",
            return_value=(transcript, {}),
        ))

    from contextlib import ExitStack
    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        return MessageResolver.resolve_for_streaming_init(
            "sess-seed", sdk_session_id="sdk-1", working_dir="/tmp/x"
        )


def _fixtures():
    db_rows = [
        {"role": "user", "content": "first", "profile_id": "prof-a"},
        {"role": "user", "content": "second", "profile_id": "prof-b"},
    ]
    # The parsed transcript carries a profile on NO row (raw history).
    transcript = [
        {"role": "user", "content": "first", "type": "text"},
        {"role": "assistant", "content": "answer 1", "type": "text"},
        {"role": "user", "content": "second", "type": "text"},
        {"role": "assistant", "content": "answer 2", "type": "text"},
    ]
    return transcript, db_rows


def test_streaming_init_seed_carries_turn_profile_from_store():
    """postgres_only seed branch stamps each turn's assistant rows with the
    profile resolved from that turn's user row (via the DB profile map)."""
    _clear_profile_cache()
    transcript, db_rows = _fixtures()
    seed = _run_seed(transcript, db_rows, store_patch="store")
    assert [m.get("profile_id") for m in seed] == [
        "prof-a", "prof-a", "prof-b", "prof-b",
    ]
    _clear_profile_cache()


def test_streaming_init_seed_carries_turn_profile_from_jsonl():
    """The JSONL fallback branch applies the same per-turn stamping."""
    _clear_profile_cache()
    transcript, db_rows = _fixtures()
    seed = _run_seed(transcript, db_rows, store_patch="jsonl")
    assert [m.get("profile_id") for m in seed] == [
        "prof-a", "prof-a", "prof-b", "prof-b",
    ]
    _clear_profile_cache()
