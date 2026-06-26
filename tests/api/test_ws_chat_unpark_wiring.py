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
``chat_ws_handler`` and wired BOTH handlers through them. These tests assert:

1. BINDING GUARD: ``websocket.py`` references the SAME shared impl objects as
   ``chat_ws_handler`` — i.e. the live route uses the shared machinery, not a
   divergent (or absent) copy.
2. SOURCE-WIRING GUARD: the live ``run_query`` actually invokes the four call
   sites the fix added (the precise thing that was missing).
3. FAST-PATH CONTRACT: stash → maybe_unpark → deliver → ONE follow-up
   ``run_query`` carrying a ``<task-notification>`` prompt with the sub-agent's
   full result — driven through the shared impls with collaborators shaped
   EXACTLY as ``websocket.py`` constructs them (a ``_deliver_unpark`` closure
   capturing a connection-local ``run_query`` + ``set_query_task``).
4. SLOW-PATH CONTRACT: a terminal notification arriving on the pooled client
   AFTER the turn ended is caught by ``_drain_notifications_impl`` and unparks
   the parent via the same websocket-style collaborators.

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


SHARED_SYMBOLS = (
    "_stash_terminal_notification",
    "_deliver_unpark_impl",
    "_maybe_unpark_followup_impl",
    "_drain_notifications_impl",
    "_start_drain_pump_impl",
)


# ---------------------------------------------------------------------------
# 1. Binding guard — the live route imports the SHARED machinery
# ---------------------------------------------------------------------------

def test_ws_chat_imports_shared_unpark_machinery():
    """websocket.py must bind the EXACT impl objects from chat_ws_handler.

    If the live route ever drifts back to a private/absent copy (the original
    bug), these identities break.
    """
    for name in SHARED_SYMBOLS:
        assert hasattr(ws_mod, name), f"websocket.py missing shared symbol {name}"
        assert getattr(ws_mod, name) is getattr(cwh, name), (
            f"websocket.py.{name} is not chat_ws_handler.{name} — the live route "
            f"is using a divergent copy of the unpark machinery"
        )


# ---------------------------------------------------------------------------
# 2. Source-wiring guard — the inline run_query invokes the machinery
# ---------------------------------------------------------------------------

def test_ws_chat_run_query_source_wires_unpark():
    """The inline /ws/chat run_query must contain the four call sites the fix
    added. This is the precise wiring that was missing (machinery present in the
    codebase, never called from the live path)."""
    import inspect

    src = inspect.getsource(ws_mod.chat_websocket)
    # FAST path: in-loop stash of terminal notifications.
    assert "_stash_terminal_notification(" in src
    # FAST path: post-loop follow-up turn.
    assert "_maybe_unpark_followup_impl(" in src
    # SLOW path: between-turns drain pump started on completion.
    assert "_start_drain_pump_impl(" in src
    # Single-reader invariant: each turn cancels the pump before reading.
    assert "stop_drain_pump(session_id)" in src


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
# 3. FAST-path contract — stash → maybe_unpark → deliver → follow-up run_query
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fast_path_stash_then_unpark_spawns_followup(tmp_path):
    from contextlib import ExitStack

    task_registry.reset_all()
    out = tmp_path / "out.txt"
    out.write_text("FULL SUBAGENT RESULT TEXT", encoding="utf-8")

    stream_state = MutableStreamState()
    sid = "sess-1"

    # Genuine FAST path: the agent's TaskStartedMessage arrived THIS turn. The
    # real SDK lifecycle records this via _record_task_message; here we call the
    # stash directly, so emulate the start. Without it, _stash_terminal_notification
    # now (correctly) suppresses the notification as a prior-turn orphan.
    task_registry.mark_started_this_turn(sid, "task-1")
    # The parent ALSO launched it in the background (tool_use_id "tu-1"); without
    # this the stash suppresses it as a synchronous sub-agent (result already inline).
    task_registry.mark_background_launch(sid, "tu-1")

    # FAST path: a sub-agent terminal notification arrives in-stream → stash it.
    cwh._stash_terminal_notification(stream_state, _terminal_event(out), sid)
    # A non-terminal / non-notification event is a no-op (should not stash).
    cwh._stash_terminal_notification(stream_state, {"type": "text", "content": "hi"}, sid)
    cwh._stash_terminal_notification(
        stream_state, _terminal_event(out, status="in_progress"), sid
    )
    assert len(stream_state.pending_task_notifications) == 1

    run_query = _RecordingRunQuery()
    set_calls = []
    deliver = _make_ws_deliver_unpark(run_query, lambda t: set_calls.append(t))
    ctx = MagicMock()

    with ExitStack() as stack:
        fresh_sessions = _patch_deliver_boundaries(stack)
        handed_off = await cwh._maybe_unpark_followup_impl(
            stream_state=stream_state,
            session_id=sid,
            profile_id="claude-code",
            project_id=None,
            worktree_id=None,
            ctx=ctx,
            deliver_unpark=deliver,
        )
        # Let the created follow-up coroutine settle.
        await asyncio.sleep(0)

        try:
            assert handed_off is True
            # Stash cleared after handoff.
            assert stream_state.pending_task_notifications == []
            # EXACTLY ONE follow-up run_query with the synthesized prompt.
            assert len(run_query.calls) == 1
            synth = run_query.calls[0]["prompt"]
            assert "<task-notification>" in synth
            assert "FULL SUBAGENT RESULT TEXT" in synth  # full result off disk
            assert "task-1" in synth and "tu-1" in synth
            assert run_query.calls[0]["session_id"] == sid
            # The unpark follow-up MUST be marked deferred_delivery so a dead
            # origin socket (parked / reconnected client) falls through to
            # sync_engine + polling delivery instead of aborting — the parked-
            # session UI-update bug. (Mirrors the "Send later" queue-drain fix.)
            assert run_query.calls[0]["deferred_delivery"] is True
            # Follow-up registered in the (isolated) active-sessions registry.
            assert sid in fresh_sessions
            # set_query_task was called with the follow-up task.
            assert len(set_calls) == 1
        finally:
            task_registry.reset_all()


@pytest.mark.asyncio
async def test_fast_path_no_stash_no_followup(tmp_path):
    """A turn with no stashed notifications hands off nothing (normal completion)."""
    from contextlib import ExitStack

    task_registry.reset_all()
    stream_state = MutableStreamState()
    run_query = _RecordingRunQuery()
    deliver = _make_ws_deliver_unpark(run_query, lambda t: None)

    with ExitStack() as stack:
        _patch_deliver_boundaries(stack)
        handed_off = await cwh._maybe_unpark_followup_impl(
            stream_state=stream_state,
            session_id="sess-1",
            profile_id="claude-code",
            project_id=None,
            worktree_id=None,
            ctx=MagicMock(),
            deliver_unpark=deliver,
        )

    assert handed_off is False
    assert run_query.calls == []
    task_registry.reset_all()


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
    out = tmp_path / "out.txt"
    out.write_text("SLOW RESULT", encoding="utf-8")

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
