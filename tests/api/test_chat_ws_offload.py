"""W6 — the chat WebSocket handlers must not do blocking work on the event loop.

Every synchronous DB call / JSONL parse made from a WS handler freezes EVERY
other WebSocket, SSE generator and HTTP request served by the same worker for
its whole duration (perf review 2026-08, finding W6). These tests pin the fix by
identity, not by wire shape: each assertion records the thread the blocking
callable actually ran on and requires it to differ from the loop thread.

The companion behavioural coverage (durable turn state + wire frames) lives in
test_chat_ws_v2_turns.py, whose collaborator patching is reused here.
"""

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch


import app.api.chat_ws_handler as cwh
from app.core import turn_manager
from app.db import database
from app.db import turns as turns_db

# The handler-level collaborator patching (sync engine, run offload) and the
# session/handler builders are shared with the behavioural suite.
from tests.api.test_chat_ws_v2_turns import _Patches, _make_handler, _make_session


class _ThreadSpy:
    """Wrap a callable, recording the thread each call ran on."""

    def __init__(self, fn):
        self._fn = fn
        self.threads = []

    def __call__(self, *args, **kwargs):
        self.threads.append(threading.get_ident())
        return self._fn(*args, **kwargs)


# ═══ handle_query / handle_queue_message — turn_manager.submit ═══════════════


async def test_handle_query_submits_off_the_event_loop(mock_db, loop_thread):
    """``submit`` does up to three DB writes; on a send that is the hottest path
    in the app. It must run in ONE worker-thread hop, with the kick left on the
    loop (a kick fired from a worker thread is silently dropped)."""
    _make_session("sess-1")
    spy = _ThreadSpy(turn_manager._submit_blocking)
    kick_threads = []
    turn_manager.register_kick(lambda sid: kick_threads.append(threading.get_ident()))
    try:
        with _Patches():
            with patch.object(turn_manager, "_submit_blocking", spy):
                handler = _make_handler()
                await handler.handle_query({
                    "prompt": "hello world",
                    "session_id": "sess-1",
                    "profile": "default",
                    "project": None,
                })
    finally:
        turn_manager.register_kick(None)

    # the turn really was enqueued...
    assert [t["source"] for t in turns_db.get_queued_turns("sess-1")] == ["user"]
    # ...off the loop thread...
    assert spy.threads and loop_thread not in spy.threads
    # ...and the scheduler kick fired back ON the loop.
    assert kick_threads == [loop_thread]


async def test_handle_queue_message_submits_off_the_event_loop(mock_db, loop_thread):
    _make_session("sess-1")
    live = turns_db.enqueue_turn("sess-1", "user", prompt="live")["id"]
    from app.db import runs
    lease = runs.claim_run("sess-1", "test-runner", 60)
    turns_db.start_turn(live, run_id=lease["id"])

    spy = _ThreadSpy(turn_manager._submit_blocking)
    with _Patches():
        with patch.object(turn_manager, "_submit_blocking", spy):
            handler = _make_handler()
            await handler.handle_queue_message({
                "content": "queued while busy",
                "session_id": "sess-1",
                "profile": "claude-code",
            })

    assert [t["prompt"] for t in turns_db.get_queued_turns("sess-1")] == [
        "queued while busy"
    ]
    assert spy.threads and loop_thread not in spy.threads


# ═══ handle_stop / handle_interrupt_for_query — turn_manager.interrupt ═══════


async def test_handle_stop_interrupts_off_the_event_loop(mock_db, loop_thread):
    """The stop button's durable ``running -> interrupting`` flip is a DB write;
    the interrupt executor still has to fire on the loop."""
    _make_session("sess-1")
    live = turns_db.enqueue_turn("sess-1", "user", prompt="live")["id"]
    from app.db import runs
    lease = runs.claim_run("sess-1", "test-runner", 60)
    turns_db.start_turn(live, run_id=lease["id"])

    spy = _ThreadSpy(turns_db.interrupt_turn)
    executor_threads = []
    turn_manager.register_interrupt_executor(
        lambda sid, mode, turn_id=None: executor_threads.append(threading.get_ident())
    )
    try:
        with _Patches():
            with patch.object(turns_db, "interrupt_turn", spy), \
                 patch(
                     "app.core.query_session_lifecycle.stop_all_background_tasks",
                     new=AsyncMock(),
                 ):
                handler = _make_handler()
                await handler.handle_stop({"session_id": "sess-1"})
    finally:
        turn_manager.register_interrupt_executor(None)

    assert turns_db.get_turn(live)["state"] == "interrupting"
    assert spy.threads and loop_thread not in spy.threads
    assert executor_threads == [loop_thread]


async def test_handle_interrupt_for_query_interrupts_off_the_event_loop(
    mock_db, loop_thread, monkeypatch
):
    # Nothing settles the interrupted turn here, so the handler polls out the
    # whole drain window; keep it short (8s by default).
    monkeypatch.setattr(cwh, "_INTERRUPT_DRAIN_TIMEOUT_SECONDS", 0.1)
    _make_session("sess-1")
    live = turns_db.enqueue_turn("sess-1", "user", prompt="live")["id"]
    from app.db import runs
    lease = runs.claim_run("sess-1", "test-runner", 60)
    turns_db.start_turn(live, run_id=lease["id"])

    spy = _ThreadSpy(turns_db.interrupt_turn)
    with _Patches():
        with patch.object(turns_db, "interrupt_turn", spy):
            handler = _make_handler()
            await handler.handle_interrupt_for_query({"session_id": "sess-1"})

    assert spy.threads and loop_thread not in spy.threads
    assert [
        c.args[0]["type"]
        for c in handler.send_json.call_args_list
        if c.args and isinstance(c.args[0], dict)
    ] == ["interrupt_confirmed"]


# ═══ handle_load_session — transcript parse + three DB reads ════════════════


class _LoadSessionPatches:
    """handle_load_session's collaborators, WITHOUT stubbing ``run_sync``.

    The v2-turns suite replaces ``run_sync`` with a synchronous passthrough; here
    the real offload has to run, since it is exactly what is under test.
    """

    def __init__(self, session_id="sess-1", worktree_id="wt-1"):
        self._session_id = session_id
        self._worktree_id = worktree_id

    def __enter__(self):
        self._p = []

        # A linked worktree (so the worktree read actually runs) without the
        # repository/worktree row cascade a real FK-satisfying insert needs.
        row = dict(database.get_session(self._session_id))
        row["worktree_id"] = self._worktree_id
        self._p.append(
            patch.object(
                cwh.database, "get_session", MagicMock(side_effect=lambda _sid: dict(row))
            )
        )

        sync = MagicMock()
        for name in ("register_device", "unregister_device", "broadcast_session_opened"):
            setattr(sync, name, AsyncMock())
        sync.is_device_watching_session = MagicMock(return_value=False)
        self.sync = sync
        self._p.append(patch.object(cwh, "sync_engine", sync))

        self.resolve = _ThreadSpy(
            lambda *a, **k: SimpleNamespace(messages=[], source="jsonl", usage=None)
        )
        resolver = MagicMock()
        resolver.resolve = self.resolve
        self._p.append(patch.object(cwh, "message_resolver", resolver))
        self._p.append(
            patch.object(cwh, "resolve_working_dir", MagicMock(return_value="/tmp"))
        )

        self.attach_cost = _ThreadSpy(lambda *a, **k: None)
        self._p.append(
            patch("app.core.cost_utils.attach_cost_metadata", self.attach_cost)
        )
        self._p.append(
            patch("app.core.cost_utils.attach_turn_profile_ids", MagicMock())
        )

        self.has_active_run = _ThreadSpy(lambda *a, **k: False)
        self._p.append(
            patch("app.core.streaming_status.has_active_run", self.has_active_run)
        )

        self.get_worktree = _ThreadSpy(lambda *a, **k: None)
        self._p.append(patch.object(cwh.database, "get_worktree", self.get_worktree))

        for p in self._p:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in reversed(self._p):
            p.stop()
        return False


async def test_handle_load_session_does_all_blocking_work_off_the_loop(
    mock_db, loop_thread
):
    """Session open parses the whole JSONL transcript and then reads cost
    metadata, busy status and the worktree row — every one of them synchronous.
    None may touch the loop thread."""
    _make_session("sess-1")

    with _LoadSessionPatches() as p:
        handler = _make_handler()
        await handler.handle_load_session({"session_id": "sess-1"})

    history = [
        c.args[0]
        for c in handler.send_json.call_args_list
        if c.args and isinstance(c.args[0], dict) and c.args[0].get("type") == "history"
    ]
    assert len(history) == 1, "the history frame must still be sent"
    assert history[0]["session_id"] == "sess-1"
    assert history[0]["isStreaming"] is False

    for name, spy in (
        ("message_resolver.resolve", p.resolve),
        ("attach_cost_metadata", p.attach_cost),
        ("streaming_status.has_active_run", p.has_active_run),
        ("database.get_worktree", p.get_worktree),
    ):
        assert spy.threads, f"{name} was never called"
        assert loop_thread not in spy.threads, f"{name} blocked the event loop"


async def test_handle_load_session_uses_a_single_thread_hop(mock_db, loop_thread):
    """All four blocking steps share ONE offload — many small hops would trade
    the stall for scheduler churn on every session open."""
    _make_session("sess-1")

    with _LoadSessionPatches() as p:
        handler = _make_handler()
        await handler.handle_load_session({"session_id": "sess-1"})

    threads = (
        p.resolve.threads
        + p.attach_cost.threads
        + p.has_active_run.threads
        + p.get_worktree.threads
    )
    assert len(set(threads)) == 1, f"expected one worker thread, saw {set(threads)}"
    assert loop_thread not in threads


async def test_handle_load_session_still_reports_a_streaming_session(mock_db):
    """The busy flag read off-loop still reaches the history frame."""
    _make_session("sess-1")

    with _LoadSessionPatches() as p:
        p.has_active_run._fn = lambda *a, **k: True
        handler = _make_handler()
        await handler.handle_load_session({"session_id": "sess-1"})

    history = [
        c.args[0]
        for c in handler.send_json.call_args_list
        if c.args and isinstance(c.args[0], dict) and c.args[0].get("type") == "history"
    ]
    assert history and history[0]["isStreaming"] is True


async def test_handle_load_session_offload_runs_concurrently_with_the_loop(mock_db):
    """The point of the offload: while a session open is parsing a transcript,
    other coroutines keep running. A 150 ms 'parse' must not stop a 10 ms ticker."""
    _make_session("sess-1")
    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    def slow_resolve(*a, **k):
        import time as _time
        _time.sleep(0.15)
        return SimpleNamespace(messages=[], source="db", usage=None)

    with _LoadSessionPatches() as p:
        p.resolve._fn = slow_resolve
        task = asyncio.create_task(ticker())
        handler = _make_handler()
        await handler.handle_load_session({"session_id": "sess-1"})
        task.cancel()

    assert ticks >= 5, f"the event loop was stalled during the parse ({ticks} ticks)"
