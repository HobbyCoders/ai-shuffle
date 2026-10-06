"""Bug C regression (/live path): postgres_only + empty store must serve the
legacy session_messages transcript instead of an empty chat.

Pre-cutover sessions hold their FULL transcript in the legacy table (shared
across installs) and have ZERO session_store_entries rows when the bulk
backfill script was never run. The /live endpoint's original hard-cut returned
``messages: []`` for them in production (a 210-row session rendered empty).

The rescue must fire ONLY when the store yields nothing: for post-cutover
sessions the legacy table holds user-rows-only, and serving it over a populated
store was the "assistant replies vanish on reopen" bug.

V2: /live serves the persisted transcript plus lifecycle fields; live render
deltas ride the session event feed (SSE). The lifecycle snapshot (activeTasks /
unpark_pending from ``lifecycle_state.compute``) is stubbed here and its
surfacing on the response body is asserted alongside the transcript checks.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from app.api.sessions import get_live_session

LEGACY_MSGS = [
    {"id": 1, "role": "user", "content": "old question", "type": "text"},
    {"id": 2, "role": "assistant", "content": "old answer", "type": "text"},
]
STORE_MSGS = [
    {"id": "u9", "role": "assistant", "content": "from store", "type": "text"},
]


def _lifecycle_stub(active_tasks=None, unpark_pending=False):
    """Minimal LifecycleSnapshot stand-in: only the fields /live destructures."""
    return SimpleNamespace(
        active_tasks=list(active_tasks or []),
        unpark_pending=unpark_pending,
        drift=[],
    )


def _owner_path_patches(session, lifecycle=None):
    return [
        patch("app.api.sessions.require_transcript_session", new=AsyncMock(return_value=session)),
        patch("app.api.sessions.enrich_session_worktree_meta", new=MagicMock(return_value={})),
        patch("app.core.lifecycle_state.compute",
              new=AsyncMock(return_value=lifecycle or _lifecycle_stub())),
        patch("app.core.streaming_status.has_active_run", new=MagicMock(return_value=False)),
    ]


async def _poll_live(session, store_result, legacy_result, store_side_effect=None,
                     lifecycle=None, include_messages=True, store_probe=None):
    patches = _owner_path_patches(session, lifecycle=lifecycle)
    for p in patches:
        p.start()
    try:
        with patch("app.core.message_resolver.resolve_working_dir", return_value="/workspace"), \
             patch("app.core.cost_utils.attach_cost_metadata"), \
             patch("app.core.cost_utils.attach_user_message_metadata"), \
             patch("app.api.sessions.parse_session_history_via_store",
                   return_value=store_result, side_effect=store_side_effect) as store, \
             patch("app.core.db_message_loader.load_messages_from_db",
                   return_value=legacy_result) as legacy_loader:
            if store_probe is not None:
                store_probe.append(store)
            result = await get_live_session(
                request=MagicMock(), session_id=session["id"], token="tok",
                include_messages=include_messages,
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


async def test_live_surfaces_lifecycle_fields_on_response_body():
    """/live attaches the lifecycle snapshot (activeTasks / unpark_pending) and
    the runs-table busy truth to every local response body (replacement-level
    coverage for the retired park-revival delta-poll suite; the field SEMANTICS
    are pinned in tests/core/test_lifecycle_state.py)."""
    session = {"id": "parked-sess", "title": "Parked", "sdk_session_id": "sdk-park-1"}
    lifecycle = _lifecycle_stub(
        active_tasks=[{"id": "task-1", "status": "running", "kind": "agent"}],
        unpark_pending=True,
    )

    result, _ = await _poll_live(
        session, store_result=([], {}), legacy_result=([], False),
        lifecycle=lifecycle,
    )

    assert isinstance(result, dict)
    assert result["activeTasks"][0]["id"] == "task-1"
    assert result["unpark_pending"] is True
    assert result["is_streaming"] is False
    assert result["last_update_id"] == 0


# ---------------------------------------------------------------------------
# include_messages=false — metadata-only snapshot (session-open cost)
# ---------------------------------------------------------------------------
# The v2 switch-in fetches /live purely for lifecycle/usage/worktree metadata:
# the SSE feed owns the transcript, so the message list this endpoint builds is
# discarded the moment it arrives. On a long session that is megabytes of JSON
# assembled per open. `include_messages=false` skips the whole transcript read.


async def test_include_messages_false_ships_no_message_bodies():
    session = {"id": "meta-sess", "title": "Meta", "sdk_session_id": "sdk-meta-1"}
    probe: list = []

    result, legacy_loader = await _poll_live(
        session,
        store_result=(list(STORE_MSGS), {}),
        legacy_result=(list(LEGACY_MSGS), True),
        include_messages=False,
        store_probe=probe,
    )

    # No bodies, and neither transcript source was even consulted.
    assert result["messages"] == []
    probe[0].assert_not_called()
    legacy_loader.assert_not_called()


async def test_include_messages_false_keeps_the_metadata_fields():
    """Everything the switch-in actually reads must survive the opt-out."""
    session = {"id": "meta-sess-2", "title": "Meta Two", "sdk_session_id": "sdk-meta-2",
               "total_tokens_out": 40, "cache_creation_tokens": 5,
               "cache_read_tokens": 6, "last_input_tokens": 30}
    lifecycle = _lifecycle_stub(
        active_tasks=[{"id": "task-9", "status": "running", "kind": "agent"}],
        unpark_pending=True,
    )

    result, _ = await _poll_live(
        session, store_result=(list(STORE_MSGS), {}), legacy_result=([], False),
        lifecycle=lifecycle, include_messages=False,
    )

    assert result["session_id"] == "meta-sess-2"
    assert result["session_meta"]["title"] == "Meta Two"
    assert result["usage"] == {
        "input_tokens": 30, "output_tokens": 40,
        "cache_creation_tokens": 5, "cache_read_tokens": 6,
    }
    assert result["activeTasks"][0]["id"] == "task-9"
    assert result["unpark_pending"] is True
    assert result["is_streaming"] is False


async def test_metadata_only_snapshot_includes_hidden_worker_run_binding():
    session = {"id": "hidden-worker", "hidden": True, "model": "gpt-5.6-terra",
               "provider_id": "openai", "effort": "high", "context_window": 200_000}
    probe = []
    result, legacy = await _poll_live(
        session, store_result=([], {}), legacy_result=([], False),
        include_messages=False, store_probe=probe,
    )
    assert result["session_meta"]["model"] == "gpt-5.6-terra"
    assert result["session_meta"]["provider_id"] == "openai"
    assert result["session_meta"]["effort"] == "high"
    assert result["session_meta"]["context_window"] == 200_000
    probe[0].assert_not_called()
    legacy.assert_not_called()


async def test_include_messages_defaults_to_the_full_transcript():
    """The param is opt-out only: every existing caller keeps its wire shape."""
    session = {"id": "default-sess", "title": "Default", "sdk_session_id": "sdk-def-1"}

    result, _ = await _poll_live(
        session, store_result=(list(STORE_MSGS), {}), legacy_result=([], False),
    )

    assert [m["content"] for m in result["messages"]] == ["from store"]


async def test_metadata_marks_a_subagent_chat_and_not_a_main_chat():
    """The UI keeps a subagent's chat to reading and messaging from this flag."""
    for row, expected in (({"is_worker": True, "hidden": True}, True), ({"is_worker": None}, False), ({}, False)):
        result, _ = await _poll_live(
            {"id": "s", **row}, store_result=([], {}), legacy_result=([], False),
            include_messages=False, store_probe=[],
        )
        assert result["session_meta"]["is_worker"] is expected


def test_session_response_carries_is_worker_and_coalesces_null():
    from app.core.models import Session
    base = {"id": "s", "profile_id": "p", "created_at": "2026-10-05T00:00:00", "updated_at": "2026-10-05T00:00:00"}
    assert Session(**base, is_worker=True).is_worker is True
    assert Session(**base, is_worker=None).is_worker is False
    assert Session(**base).is_worker is False
