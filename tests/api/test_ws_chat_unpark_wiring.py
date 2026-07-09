"""
Regression tests for the background-agent "unpark" wiring on the LIVE browser
route — ``app/api/websocket.py``'s inline ``/ws/chat`` ``run_query``.

WHY THIS FILE EXISTS (the bug it guards against): the park→wake machinery
(PR #394 fast-path stash + #398 between-turns drain pump) originally lived ONLY
on the class-based ``ChatWebSocketHandler`` (used by the headless runner). The
live browser endpoint ``/ws/chat`` is a SEPARATE, inline ``run_query`` that was
never migrated to the class — so for real browser sessions a background sub-agent
would complete but the parent never unparked. Every existing unit test exercised
the headless class, so they all passed while the browser stayed broken.

The fix extracted the machinery into module-level ``*_impl`` functions in
``chat_ws_handler`` and wired the handler through them.

NOTE (Phase 6.1 step 7): ``/ws/chat`` no longer runs a separate inline
``run_query`` closure — it is a thin shim over ``ChatWebSocketHandler``, the
sole owner of the unpark machinery. The old BINDING and SOURCE-WIRING guards
that inspected the inline closure's source were therefore removed (there is no
second copy left to drift). What remains here is the behavioral contract that
matters at runtime, driven directly against the shared ``*_impl`` functions:

1. LATCH CONTRACT: an in-stream terminal notification for a genuine background
   task spends the one-unpark-per-task token (``mark_unparked``) so a later
   drain-pump replay of the same completion delivers NOTHING.
2. SLOW-PATH CONTRACT: a terminal notification arriving on the pooled client
   AFTER the turn ended is caught by ``_drain_notifications_impl`` and unparks
   the parent via websocket-style collaborators (a ``_deliver_unpark`` closure
   capturing a connection-local ``run_query`` + ``set_query_task``).

Plus a source tripwire that the unified ``run_query`` finally block guards
cleanup against a successor turn (``superseded_by_successor``) and does not
resurrect the deleted post-turn fast-path follow-up.

No Postgres or real SDK client is required — every boundary is mocked.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.api.chat_ws_handler as cwh
import app.api.websocket as ws_mod
import app.api.ws_cleanup as ws_cleanup
from app.api.ws_event_handlers import MutableStreamState
from app.core.task_registry import task_registry
from claude_agent_sdk import TaskNotificationMessage


# ---------------------------------------------------------------------------
# 1. Binding + source-wiring guards for the LIVE inline route — REMOVED at
#    Phase 6.1 step 7 (closure deletion).
#
# ``test_ws_chat_imports_shared_unpark_machinery`` and
# ``test_ws_chat_run_query_source_wires_unpark`` asserted that the inline
# ``/ws/chat`` closure bound / called the shared ``*_impl`` unpark machinery.
# That closure is gone — ``/ws/chat`` is now a thin shim over
# ``ChatWebSocketHandler``, which owns the ONLY unpark implementation (the
# machinery is defined in chat_ws_handler itself, so there is no longer a
# second copy that could drift). The runtime unpark contract is exercised
# end-to-end by the LATCH and SLOW-path tests below (they drive
# ``cwh._drain_notifications_impl`` / ``_latch_terminal_notification``
# directly). The class run_query's pump/latch/stop_drain_pump wiring is
# additionally pinned by the finally-guard test below.
# ---------------------------------------------------------------------------


def test_run_query_finally_guards_cleanup_against_successor():
    """The unified run_query ``finally`` block must NOT cleanup_session() a live
    session that a SUCCESSOR turn now owns.

    THE BUG THIS GUARDS: a turn's finally runs CONCURRENTLY with a successor
    turn's startup whenever the between-turns drain pump delivers an unpark
    follow-up inside the finally's window (the pump batches for ~1.5s; the
    finally spends up to ~1s in heartbeat teardown plus DB fetches — longest
    exactly when the origin socket died to a page refresh). The finally's
    teardown heuristics check only STORE state (``has_session &&
    is_streaming`` / ``remaining_devices == 0``), so when the follow-up's
    ``start_streaming`` wins the race, the parent read the successor's
    streaming state as its own dangling one and called ``cleanup_session()``:
    the follow-up's live session was deleted out from under it, its registry
    tasks reaped to "killed", and the unpark_pending latch cleared. The
    follow-up turn then streamed into a void — the durable transcript kept
    growing (new messages appeared on refresh) while ``/live`` reported
    not-streaming forever, so the page never re-entered streaming.

    The fix: every cleanup_session() heuristic in a run_query finally checks
    ``superseded_by_successor(session_id)`` (ownership via
    ``_active_chat_sessions``) and skips teardown when another task has
    replaced this turn as the session's active turn.
    """
    import inspect

    src = inspect.getsource(cwh.ChatWebSocketHandler.run_query)
    label = "chat_ws_handler.run_query"
    occurrences = src.count("await superseded_by_successor(session_id)")
    assert occurrences >= 2, (
        f"{label}: expected the ownership guard on BOTH finally cleanup "
        f"heuristics (streaming-never-ended AND no-devices), found "
        f"{occurrences} — an unguarded cleanup_session() will race the "
        f"drain pump's unpark follow-up and wipe its live session"
    )


@pytest.mark.asyncio
async def test_superseded_by_successor_ownership_semantics():
    """The ownership predicate itself: False when WE are the registered turn
    or nothing is registered; True only when ANOTHER task replaced us."""
    session_id = "unpark-ownership-test"
    try:
        # Nothing registered → not superseded (normal solo-turn teardown).
        assert not await ws_cleanup.superseded_by_successor(session_id)

        # We are the registered turn → not superseded.
        async with ws_cleanup._active_sessions_lock:
            ws_cleanup._active_chat_sessions[session_id] = asyncio.current_task()
        assert not await ws_cleanup.superseded_by_successor(session_id)

        # A different task replaced us (the unpark follow-up) → superseded.
        async def _sleeper():
            await asyncio.sleep(30)

        successor = asyncio.get_running_loop().create_task(_sleeper())
        try:
            async with ws_cleanup._active_sessions_lock:
                ws_cleanup._active_chat_sessions[session_id] = successor
            assert await ws_cleanup.superseded_by_successor(session_id)
        finally:
            successor.cancel()
    finally:
        ws_cleanup._active_chat_sessions.pop(session_id, None)


def test_post_turn_fast_path_stays_deleted():
    """Regression tripwire for the 2026-07-02 bug: the app-side post-turn
    follow-up for IN-STREAM completions (stash → _maybe_unpark_followup) was a
    double delivery — the CLI already injects mid-turn completions into the
    model's context before the turn ends. Neither handler may resurrect it."""
    import inspect

    for mod in (cwh, ws_mod):
        assert not hasattr(mod, "_maybe_unpark_followup_impl")
        assert not hasattr(mod, "_stash_terminal_notification")
    # The unified run_query must not resurrect the post-turn fast-path follow-up.
    assert "_maybe_unpark_followup" not in inspect.getsource(
        cwh.ChatWebSocketHandler.run_query
    )
    # The stash's carrier field is gone too.
    assert not hasattr(MutableStreamState(), "pending_task_notifications")


# ---------------------------------------------------------------------------
# Helpers shared by the contract tests
# ---------------------------------------------------------------------------

def _terminal_event(output_file, *, task_id="task-1", uuid="uuid-1", status="completed"):
    return {
        "type": "task_notification",
        "task_id": task_id,
        "status": status,
        "summary": "did the thing",
        "tool_use_id": "tu-1",
        "output_file": str(output_file),
        "uuid": uuid,
        "usage": {"total_tokens": 42, "tool_uses": 3, "duration_ms": 1000},
    }


class _RecordingRunQuery:
    """A connection-local run_query stand-in: records the args of each follow-up
    turn and returns a trivial awaited-once coroutine (so asyncio.create_task has
    something real to schedule)."""

    def __init__(self):
        self.calls = []

    def __call__(self, prompt, session_id, profile_id, project_id, overrides=None, worktree_id=None, deferred_delivery=False):
        self.calls.append({
            "prompt": prompt,
            "session_id": session_id,
            "profile_id": profile_id,
            "project_id": project_id,
            "overrides": overrides,
            "worktree_id": worktree_id,
            "deferred_delivery": deferred_delivery,
        })

        async def _noop():
            return None

        return _noop()


def _make_ws_deliver_unpark(run_query, set_query_task):
    """Build the SAME _deliver_unpark closure shape websocket.py constructs."""

    async def _deliver_unpark(*, pending, session_id, profile_id, project_id,
                              worktree_id, ctx, final_metadata=None):
        return await cwh._deliver_unpark_impl(
            pending=pending,
            session_id=session_id,
            profile_id=profile_id,
            project_id=project_id,
            worktree_id=worktree_id,
            ctx=ctx,
            final_metadata=final_metadata,
            run_query=run_query,
            set_query_task=set_query_task,
        )

    return _deliver_unpark


def _patch_deliver_boundaries(stack):
    """Patch the I/O boundaries _deliver_unpark_impl touches + isolate the
    _active_chat_sessions registry it writes into."""
    from contextlib import ExitStack
    assert isinstance(stack, ExitStack)

    async def _fake_run_sync(fn, *a, **k):
        return None

    stack.enter_context(patch.object(cwh, "run_sync", new=AsyncMock(side_effect=_fake_run_sync)))

    sync = stack.enter_context(patch.object(cwh, "sync_engine"))
    sync.broadcast_message_added = AsyncMock()

    store = stack.enter_context(patch.object(cwh, "live_session_store"))
    store.end_streaming = AsyncMock()

    # Isolate the registry the impl writes the handoff into (it imports it from
    # app.api.websocket at call time, so patch there).
    fresh_sessions = {}
    stack.enter_context(patch.object(ws_mod, "_active_chat_sessions", new=fresh_sessions))
    stack.enter_context(patch.object(ws_mod, "_active_sessions_lock", new=asyncio.Lock()))
    return fresh_sessions


# ---------------------------------------------------------------------------
# 3. LATCH contract — in-stream completion latches; the pump never re-delivers
# ---------------------------------------------------------------------------

def test_latch_spends_unpark_token_for_in_stream_completion():
    """A genuine background task coming to rest MID-TURN (started this turn AND
    launched in the background) is latched — the CLI delivered it to the model
    already, so the app records mark_unparked and fires nothing itself. Other
    events are no-ops."""
    task_registry.reset_all()
    sid = "sess-1"
    task_registry.mark_started_this_turn(sid, "task-1")
    task_registry.mark_background_launch(sid, "tu-1")

    # Non-notification / non-terminal events never latch.
    cwh._latch_terminal_notification({"type": "text", "content": "hi"}, sid)
    cwh._latch_terminal_notification(_terminal_event("x", status="in_progress"), sid)
    assert task_registry.was_unparked(sid, "task-1") is False

    cwh._latch_terminal_notification(_terminal_event("x"), sid)
    assert task_registry.was_unparked(sid, "task-1") is True
    task_registry.reset_all()


@pytest.mark.asyncio
async def test_latched_completion_is_not_redelivered_by_pump(tmp_path):
    """THE 2026-07-02 regression, end to end: the pooled-client buffer replays a
    mid-turn completion to the between-turns drain pump; the pump's mark_unparked
    gate must find the token spent by the in-stream latch and deliver NOTHING —
    no stale post-turn <task-notification> follow-up."""
    from contextlib import ExitStack

    task_registry.reset_all()
    ws_cleanup._drain_pumps.clear()
    out = tmp_path / "out.txt"
    out.write_text("ALREADY REPORTED RESULT", encoding="utf-8")

    sid = "sess-1"
    # Genuine background task: tracked, started this turn, backgrounded launch.
    task_registry.start_task(sid, "task-1", "agent", "bg work")
    task_registry.mark_started_this_turn(sid, "task-1")
    task_registry.mark_background_launch(sid, "tu-1")

    # Its terminal notification arrives IN-STREAM → latch only.
    cwh._latch_terminal_notification(_terminal_event(out), sid)
    assert task_registry.was_unparked(sid, "task-1") is True

    # Between turns, the same completion replays on the pooled client.
    client = _FakeClient(
        [_sdk_notif("task-1", uuid="u-replay", output_file=str(out))], hang=False
    )
    state = _fake_state(client)
    run_query = _RecordingRunQuery()
    deliver = _make_ws_deliver_unpark(run_query, lambda t: None)
    pool = MagicMock()
    pool.peek = MagicMock(return_value=state)

    with ExitStack() as stack:
        _patch_deliver_boundaries(stack)
        stack.enter_context(patch("app.core.session_pool.get_session_pool", return_value=pool))
        await cwh._drain_notifications_impl(
            sid,
            "claude-code",
            None,
            None,
            device_id="dev-1",
            user_id="admin",
            send_json=AsyncMock(return_value=True),
            deliver_unpark=deliver,
        )
        await asyncio.sleep(0)

    try:
        # Drained the replay, delivered nothing.
        assert client.received == 1
        assert run_query.calls == []
    finally:
        task_registry.reset_all()
        ws_cleanup._drain_pumps.clear()


# ---------------------------------------------------------------------------
# 4. SLOW-path contract — drain pump catches a late notification and unparks
# ---------------------------------------------------------------------------

class _FakeClient:
    def __init__(self, messages, *, hang=False):
        self._messages = list(messages)
        self._hang = hang
        self.received = 0

    async def receive_messages(self):
        for msg in self._messages:
            self.received += 1
            yield msg
        if self._hang:
            await asyncio.Event().wait()


def _fake_state(client, *, is_streaming=False, alive=True):
    state = MagicMock()
    state.is_streaming = is_streaming
    # Real SessionState defaults this to False; a bare MagicMock attribute is
    # truthy and would trip the pump's stale-turn hold (F2) in every test.
    state.stale_turn_undrained = False
    state.client = client
    adapter = MagicMock()
    adapter.is_alive = MagicMock(return_value=alive)
    state.adapter = adapter
    return state


def _sdk_notif(task_id="task-1", *, uuid="u-1", output_file="", status="completed"):
    return TaskNotificationMessage(
        subtype="task_notification",
        data={},
        task_id=task_id,
        status=status,
        output_file=output_file,
        summary="done it",
        uuid=uuid,
        session_id="sess-1",
        tool_use_id="tu-1",
        usage=None,
    )


@pytest.mark.asyncio
async def test_slow_path_drain_impl_unparks_via_ws_collaborators(tmp_path):
    from contextlib import ExitStack

    task_registry.reset_all()
    ws_cleanup._drain_pumps.clear()
    # The sub-agent's output_file is its JSONL transcript; the unpark prompt now
    # extracts the FINAL assistant message from it (not a raw dump), so write a
    # transcript whose final assistant text is the result under assertion.
    import json as _json
    out = tmp_path / "agent-slow.jsonl"
    out.write_text(
        _json.dumps({
            "type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "SLOW RESULT"}]},
            "uuid": "a1", "timestamp": "t1",
        }),
        encoding="utf-8",
    )

    # The agent task is "running" from the parent turn, launched in the background
    # (its launch tool_use_id is the notification's tool_use_id "tu-1").
    task_registry.start_task("sess-1", "task-1", "agent", "bg work")
    task_registry.mark_background_launch("sess-1", "tu-1")

    client = _FakeClient([_sdk_notif("task-1", uuid="u-1", output_file=str(out))], hang=True)
    state = _fake_state(client)

    run_query = _RecordingRunQuery()
    deliver = _make_ws_deliver_unpark(run_query, lambda t: None)
    send_json = AsyncMock(return_value=True)

    pool = MagicMock()
    pool.peek = MagicMock(return_value=state)

    with ExitStack() as stack:
        _patch_deliver_boundaries(stack)
        stack.enter_context(patch("app.core.session_pool.get_session_pool", return_value=pool))
        await cwh._drain_notifications_impl(
            "sess-1",
            "claude-code",
            None,
            None,
            device_id="dev-1",
            user_id="admin",
            send_json=send_json,
            deliver_unpark=deliver,
        )
        await asyncio.sleep(0)

    try:
        # complete_task ran (indicator unfreezes).
        assert task_registry.get_active("sess-1") == []
        # The unpark spawned a follow-up run_query with the full result.
        assert len(run_query.calls) == 1
        synth = run_query.calls[0]["prompt"]
        assert "<task-notification>" in synth
        assert "SLOW RESULT" in synth
        # Pump removed itself from the registry on exit.
        assert "sess-1" not in ws_cleanup._drain_pumps
    finally:
        task_registry.reset_all()
        ws_cleanup._drain_pumps.clear()
