"""Bug C regression (/live path): postgres_only + empty store must serve the
legacy session_messages transcript instead of an empty chat.

Pre-cutover sessions hold their FULL transcript in the legacy table (shared
across installs) and have ZERO session_store_entries rows when the bulk
backfill script was never run. The /live endpoint's original hard-cut returned
``messages: []`` for them in production (a 210-row session rendered empty).

The rescue must fire ONLY when the store yields nothing: for post-cutover
sessions the legacy table holds user-rows-only, and serving it over a populated
store was the "assistant replies vanish on reopen" bug.

Same direct-call harness as test_live_endpoint_park_revival.py.
"""

from unittest.mock import AsyncMock, MagicMock, patch

from app.api.sessions import get_live_session
from app.core.config import settings
from app.core.live_session_store import live_session_store

LEGACY_MSGS = [
    {"id": 1, "role": "user", "content": "old question", "type": "text"},
    {"id": 2, "role": "assistant", "content": "old answer", "type": "text"},
]
STORE_MSGS = [
    {"id": "u9", "role": "assistant", "content": "from store", "type": "text"},
]


def _owner_path_patches(session):
    return [
        patch("app.api.sessions.require_session", new=AsyncMock(return_value=session)),
        patch("app.api.sessions.enrich_session_worktree_meta", new=MagicMock(return_value={})),
        patch("app.core.cloud_relay.is_remote_session", new=MagicMock(return_value=False)),
    ]


async def _poll_live(session, store_result, legacy_result, store_side_effect=None):
    patches = _owner_path_patches(session)
    for p in patches:
        p.start()
    try:
        with patch.object(settings, "session_store_mode", "postgres_only"), \
             patch.object(live_session_store, "has_session", return_value=False), \
             patch("app.core.message_resolver.resolve_working_dir", return_value="/workspace"), \
             patch("app.core.cost_utils.attach_cost_metadata"), \
             patch("app.api.sessions.parse_session_history_via_store",
                   return_value=store_result, side_effect=store_side_effect), \
             patch("app.core.db_message_loader.load_messages_from_db",
                   return_value=legacy_result) as legacy_loader:
            result = await get_live_session(
                request=MagicMock(), session_id=session["id"],
                since_update_id=0, token="tok",
            )
    finally:
        for p in patches:
            p.stop()
    return result, legacy_loader


async def test_postgres_only_empty_store_serves_legacy_transcript():
    session = {"id": "legacy-sess", "title": "Old", "sdk_session_id": "sdk-old-1"}

    result, legacy_loader = await _poll_live(
        session, store_result=([], {}), legacy_result=(list(LEGACY_MSGS), True)
    )

    assert isinstance(result, dict)
    contents = [m["content"] for m in result["messages"]]
    assert contents == ["old question", "old answer"]
    legacy_loader.assert_called_once()


async def test_postgres_only_populated_store_never_consults_legacy_db():
    session = {"id": "stored-sess", "title": "New", "sdk_session_id": "sdk-new-1"}

    result, legacy_loader = await _poll_live(
        session,
        store_result=(list(STORE_MSGS), {}),
        legacy_result=([{"id": 1, "role": "user", "content": "LEGACY TRAP"}], True),
    )

    assert [m["content"] for m in result["messages"]] == ["from store"]
    legacy_loader.assert_not_called()


async def test_postgres_only_raised_store_parse_serves_empty_not_legacy():
    """A RAISED store parse (transient DB error) proves nothing about the
    store's contents. Falling through to the legacy table would render a
    post-cutover session as the user-rows-only legacy view (the "vanishing
    replies" trap). Serve an empty transcript instead — a transient blank
    beats a wrong one."""
    session = {"id": "flaky-sess", "title": "New", "sdk_session_id": "sdk-new-2"}

    result, legacy_loader = await _poll_live(
        session,
        store_result=None,
        store_side_effect=RuntimeError("db pool exhausted"),
        legacy_result=([{"id": 1, "role": "user", "content": "LEGACY TRAP"}], True),
    )

    assert result["messages"] == []
    legacy_loader.assert_not_called()
