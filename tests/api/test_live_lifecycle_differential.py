"""Differential lock on the /live wire contract across every lifecycle mode.

Phase 1 of the lifecycle-state unification introduces a DERIVED view
(``app.core.lifecycle_state.compute``) that ``get_live_session`` destructures
instead of computing ``activeTasks`` / ``unpark_pending`` inline. Phase-1
discipline is DERIVATION BEFORE ENFORCEMENT: the refactor must be
byte-identical on the wire — same field names, same values, same per-path
attachment — with ZERO reader flips.

These tests were written FIRST, against the PRE-refactor endpoint, and encode
its exact behavior (the expected values below were captured from main's
behavior, not from the new module). They drive REAL writer sequences — the
live store's ``start_streaming``/``end_streaming``, the task registry's
``start_task``/``complete_task``/``mark_unpark_pending``, a real
``queued_messages`` row — through the real ``get_live_session`` function, and
assert the exact wire triple ``(is_streaming, activeTasks, unpark_pending)``
in every mode. Any behavioral drift introduced by the derived view fails
here.

Modes covered (the four lifecycle states plus the delta-poll paths):
  idle            — no live session, no tasks, no latch, no queue
  streaming       — live store is_streaming=True (with and without tasks)
  parked_with_work— turn ended with a running background agent
  waking          — unpark latch set / queued-messages keep-alive
  delta polls     — lightweight-body (parked) vs 304 (idle)
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.responses import Response

from app.api.sessions import get_live_session
from app.core.config import settings
from app.core.live_session_store import live_session_store
from app.core.task_registry import task_registry


@pytest.fixture(autouse=True)
def _pin_preserve_background_flag():
    """Pin the effective ``preserve_background_on_interrupt`` flag to False for
    every test in this file, independent of ambient env vars or any other
    (properly torn-down) test's mock state. This suite asserts the FULL /live
    response body byte-for-byte — including this field — so it must not depend
    on suite ordering. See tests/conftest.py's forced
    ``PRESERVE_BACKGROUND_ON_INTERRUPT`` env override for the actual source fix
    this pin backstops.
    """
    with patch.object(settings, "preserve_background_on_interrupt", False), \
            patch("app.core.cli_interrupt_patch.is_patch_active", return_value=False):
        yield


def _owner_path_patches(session):
    """Force the LOCAL owner path: not a remote/relayed session, no worktree DB."""
    return [
        patch("app.api.sessions.require_session", new=AsyncMock(return_value=session)),
        patch("app.api.sessions.enrich_session_worktree_meta", new=MagicMock(return_value={})),
        patch("app.core.cloud_relay.is_remote_session", new=MagicMock(return_value=False)),
    ]


async def _call_live(session, since_update_id=0):
    patches = _owner_path_patches(session)
    for p in patches:
        p.start()
    try:
        return await get_live_session(
            request=MagicMock(),
            session_id=session["id"],
            since_update_id=since_update_id,
            token="tok",
        )
    finally:
        for p in patches:
            p.stop()


def _triple(result):
    """The wire triple this suite pins: (is_streaming, activeTasks, unpark_pending)."""
    return (result["is_streaming"], result["activeTasks"], result["unpark_pending"])


# =============================================================================
# idle
# =============================================================================

async def test_idle_full_body_exact(mock_db):
    """A genuinely idle session: the FULL response body, field for field."""
    sid = "lifediff-idle"
    session = {"id": sid, "title": "Idle"}

    result = await _call_live(session)

    assert result == {
        "session_id": sid,
        "is_streaming": False,
        "last_update_id": 0,
        "messages": [],
        "usage": {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_tokens": 0,
            "cache_read_tokens": 0,
        },
        "activeTasks": [],
        "unpark_pending": False,
        "preserve_background_on_interrupt": False,
        "session_meta": {"title": "Idle"},
    }


# =============================================================================
# streaming
# =============================================================================

async def test_streaming_triple(mock_db):
    sid = "lifediff-streaming"
    session = {"id": sid, "title": "S"}
    await live_session_store.start_streaming(sid)
    try:
        result = await _call_live(session)
    finally:
        await live_session_store.end_streaming(sid)
        live_session_store.reset_all()

    assert _triple(result) == (True, [], False)


async def test_streaming_with_running_task_triple(mock_db):
    sid = "lifediff-streaming-task"
    session = {"id": sid, "title": "S"}
    await live_session_store.start_streaming(sid)
    task_registry.start_task(sid, "t1", "agent", "bg research", tool_use_id="toolu_1")
    try:
        result = await _call_live(session)
    finally:
        await live_session_store.end_streaming(sid)
        live_session_store.reset_all()
        task_registry.clear_session(sid)

    is_streaming, tasks, unpark = _triple(result)
    assert is_streaming is True
    assert unpark is False
    assert len(tasks) == 1
    t = tasks[0]
    assert t["id"] == "t1"
    assert t["subject"] == "bg research"
    assert t["status"] == "running"
    assert t["kind"] == "agent"
    assert t["toolUseId"] == "toolu_1"


# =============================================================================
# parked_with_work
# =============================================================================

async def test_parked_with_work_triple(mock_db):
    """Turn ended with a background agent still running: is_streaming=False but
    activeTasks keeps the poller parked."""
    sid = "lifediff-parked"
    session = {"id": sid, "title": "P"}
    await live_session_store.start_streaming(sid)
    task_registry.start_task(sid, "t1", "agent", "bg work")
    await live_session_store.end_streaming(sid)
    try:
        result = await _call_live(session)
    finally:
        live_session_store.reset_all()
        task_registry.clear_session(sid)

    is_streaming, tasks, unpark = _triple(result)
    assert is_streaming is False
    assert unpark is False
    assert [(t["id"], t["status"]) for t in tasks] == [("t1", "running")]


# =============================================================================
# waking
# =============================================================================

async def test_waking_unpark_latch_triple(mock_db):
    """Background task completed, unpark follow-up committed but not yet
    streaming: unpark_pending=True and the retained completed row is visible
    (the completion -> unpark hand-off bridge)."""
    sid = "lifediff-waking"
    session = {"id": sid, "title": "W"}
    await live_session_store.start_streaming(sid)
    task_registry.start_task(sid, "t1", "agent", "bg work")
    task_registry.complete_task(sid, "t1", "completed")
    # Mirrors _deliver_unpark_impl's order: end_streaming FIRST (it clears the
    # latch as a backstop), then mark_unpark_pending.
    await live_session_store.end_streaming(sid)
    task_registry.mark_unpark_pending(sid)
    try:
        result = await _call_live(session)
    finally:
        live_session_store.reset_all()
        task_registry.clear_unpark_pending(sid)
        task_registry.clear_session(sid)

    is_streaming, tasks, unpark = _triple(result)
    assert is_streaming is False
    assert unpark is True
    # get_visible surfaces the just-completed task for the short hand-off window.
    assert [(t["id"], t["status"]) for t in tasks] == [("t1", "completed")]


async def test_waking_queued_keepalive_triple(mock_db):
    """No latch, but an undrained queued message exists (REAL row): the
    queued-messages keep-alive reuses unpark_pending as the channel."""
    from app.db import database

    sid = "lifediff-queued"
    session = {"id": sid, "title": "Q"}
    database.create_session(session_id=sid, profile_id="default")
    database.add_queued_message(sid, "queued follow-up")

    result = await _call_live(session)

    assert _triple(result) == (False, [], True)


async def test_unpark_latch_short_circuits_queue_count(mock_db):
    """When the latch is already set the queued-messages count must NOT run
    (verbatim keep-alive ordering from sessions.py)."""
    sid = "lifediff-latch-first"
    session = {"id": sid, "title": "L"}
    task_registry.mark_unpark_pending(sid)

    def _must_not_run(*a, **k):  # pragma: no cover - failure path
        raise AssertionError("count_queued_messages must not run when latched")

    try:
        with patch("app.api.sessions.database.count_queued_messages", _must_not_run):
            result = await _call_live(session)
    finally:
        task_registry.clear_unpark_pending(sid)

    assert _triple(result) == (False, [], True)


async def test_queue_count_failure_is_swallowed(mock_db):
    """A failing queued-messages count degrades to unpark_pending=False —
    never an error, never True."""
    sid = "lifediff-queue-err"
    session = {"id": sid, "title": "E"}

    with patch(
        "app.api.sessions.database.count_queued_messages",
        side_effect=RuntimeError("db down"),
    ):
        result = await _call_live(session)

    assert _triple(result) == (False, [], False)


# =============================================================================
# delta polls
# =============================================================================

async def test_parked_delta_poll_lightweight_body_exact(mock_db):
    """Delta poll, no message updates, running task: the exact lightweight body."""
    sid = "lifediff-delta-parked"
    session = {"id": sid, "title": "DP"}
    await live_session_store.start_streaming(sid)
    task_registry.start_task(sid, "t1", "agent", "bg work")
    await live_session_store.end_streaming(sid)
    cursor = live_session_store.get_session_dict(sid)["last_update_id"]
    try:
        result = await _call_live(session, since_update_id=cursor)
    finally:
        live_session_store.reset_all()
        task_registry.clear_session(sid)

    assert not isinstance(result, Response)
    tasks = result["activeTasks"]
    assert [(t["id"], t["status"]) for t in tasks] == [("t1", "running")]
    assert result == {
        "session_id": sid,
        "is_streaming": False,
        "last_update_id": cursor,
        "messages": [],
        "initial_message_count": cursor,
        "activeTasks": tasks,
        "unpark_pending": False,
        "preserve_background_on_interrupt": False,
        "session_meta": {"title": "DP"},
    }


async def test_idle_delta_poll_304(mock_db):
    """Delta poll, no updates, no tasks, no latch: a bare 304."""
    sid = "lifediff-delta-idle"
    session = {"id": sid, "title": "DI"}
    await live_session_store.start_streaming(sid)
    await live_session_store.end_streaming(sid)
    cursor = live_session_store.get_session_dict(sid)["last_update_id"]
    try:
        result = await _call_live(session, since_update_id=cursor)
    finally:
        live_session_store.reset_all()

    assert isinstance(result, Response)
    assert result.status_code == 304


async def test_streaming_delta_poll_full_snapshot_carries_triple(mock_db):
    """A parked poller's stale cursor during a NEW streaming turn gets the full
    snapshot with the triple attached (the live_data attachment path)."""
    sid = "lifediff-delta-stream"
    session = {"id": sid, "title": "DS"}
    await live_session_store.start_streaming(sid)
    first_cursor = live_session_store.get_session_dict(sid)["last_update_id"]
    await live_session_store.end_streaming(sid)
    # Second turn: the parked poller's cursor predates turn_start_update_id.
    await live_session_store.start_streaming(sid)
    task_registry.start_task(sid, "t2", "agent", "second turn work")
    try:
        result = await _call_live(session, since_update_id=first_cursor)
    finally:
        await live_session_store.end_streaming(sid)
        live_session_store.reset_all()
        task_registry.clear_session(sid)

    assert not isinstance(result, Response)
    is_streaming, tasks, unpark = _triple(result)
    assert is_streaming is True
    assert unpark is False
    assert [(t["id"], t["status"]) for t in tasks] == [("t2", "running")]
