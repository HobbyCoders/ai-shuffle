"""V2 turns cutover — ``ChatWebSocketHandler`` delegation (Phase 2 A6b / D4+D5).

With ``AI_SHUFFLE_V2_TURNS`` ON (the default), the WS chat handlers no longer
drive the legacy in-memory busy-guard / ``queued_messages`` / drain machinery.
They delegate to the durable ``turns`` table via ``turn_manager``:

* ``handle_query``            -> ``turn_manager.submit(source='user')`` (+ F5
                                 ``query_queued`` ack when a turn is already live);
* ``handle_queue_message``    -> ``turn_manager.submit(source='queue')``;
* ``handle_remove_queued``    -> ``turn_manager.remove_queued`` (queued->aborted);
* ``handle_clear_queued``     -> ``remove_queued`` for every user/queue chip;
* ``handle_stop``             -> ``turn_manager.interrupt(mode='stop')`` (+ the
                                 ``stop_background`` opt-in gate);
* ``handle_interrupt_for_query`` -> ``turn_manager.interrupt(mode='send_now')``
                                 then ``interrupt_confirmed``;
* ``handle_load_session``     -> ``queuedMessages`` sourced from the turns view.

These run against a REAL turns table (``mock_db`` pg-shim) — the assertions read
durable turn state and the ``queue_updated`` wire frames, NOT mocked seams. The
heavy WS collaborators (sync engine, cloud relay, run offload) are patched;
``turn_manager`` / ``turns`` / ``database`` are real.

The legacy (flag-OFF) rollback path stays covered by test_busy_write_guard_ws.py,
test_chat_ws_handler_queue.py and test_chat_ws_queue_race_gaps.py.
"""

from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.api.chat_ws_handler as cwh
import app.api.websocket as ws_mod
from app.core.config import settings
from app.db import database
from app.db import turns as turns_db


def _make_session(session_id="sess-1", *, title="t", profile_id="default"):
    # The implicit-pin placement gate only considers approved, online runners,
    # so the self install needs a registry row before any turn is submitted.
    database.register_install(
        settings.effective_instance_id, "test-runner", None, "local", "1", {},
        approve_new=True,
    )
    database.create_session(session_id=session_id, profile_id=profile_id, title=title)
    return session_id


class _Patches:
    """Patch the WS-only collaborators; keep turn_manager / turns / database real
    so the handlers exercise the durable turns table end-to-end."""

    def __enter__(self):
        self._stack = ExitStack()
        s = self._stack

        self.sync = s.enter_context(patch.object(cwh, "sync_engine"))
        for name in (
            "register_device",
            "unregister_device",
            "broadcast_message_added",
            "broadcast_queue_updated",
            "broadcast_session_updated",
            "broadcast_user_session_created",
            "broadcast_user_session_updated",
        ):
            setattr(self.sync, name, AsyncMock())

        # run offload -> synchronous passthrough so all DB access stays on the
        # loop thread (mock_db is a single-connection SQLite shim). turn_manager
        # .submit is invoked DIRECTLY (never via run_sync) so its kick can grab
        # the running loop.
        async def _run_sync(fn, *a, **k):
            return fn(*a, **k)

        s.enter_context(patch.object(cwh, "run_sync", new=AsyncMock(side_effect=_run_sync)))
        return self

    def __exit__(self, *exc):
        self._stack.close()
        return False


def _make_handler():
    handler = cwh.ChatWebSocketHandler(
        websocket=MagicMock(),
        device_id="dev-1",
        user_id="admin",
        api_user_id=None,
    )
    handler.send_json = AsyncMock(return_value=True)
    return handler


def _sent(handler, ftype):
    return [
        c.args[0]
        for c in handler.send_json.call_args_list
        if c.args and isinstance(c.args[0], dict) and c.args[0].get("type") == ftype
    ]


def _queue_frames(handler):
    return [
        c.args[0]
        for c in handler.send_json.call_args_list
        if c.args
        and isinstance(c.args[0], dict)
        and c.args[0].get("event_type") == "queue_updated"
    ]


# ═══ _v2_queue_view — B3 wire shape ══════════════════════════════════════════


def test_v2_queue_view_filters_sources_and_numbers_sequentially(mock_db):
    """Only user/queue queued turns render as chips, with sequential integer
    positions and id<-turn id / content<-prompt (the float turns.position never
    leaks). Internal sources (failover/auto_continue/unpark) are excluded."""
    _make_session("sess-1")
    t_user = turns_db.enqueue_turn("sess-1", "user", prompt="first")["id"]
    turns_db.enqueue_turn("sess-1", "failover", prompt="internal retry")
    t_queue = turns_db.enqueue_turn("sess-1", "queue", prompt="second")["id"]

    view = cwh._v2_queue_view("sess-1")

    assert [r["id"] for r in view] == [t_user, t_queue]
    assert [r["content"] for r in view] == ["first", "second"]
    assert [r["position"] for r in view] == [0, 1]
    assert all(r["session_id"] == "sess-1" for r in view)


# ═══ handle_query — submit(user) ═════════════════════════════════════════════


async def test_handle_query_idle_submits_user_turn_no_queued_ack(mock_db):
    """An idle-session query enqueues a durable source=user turn, persists the
    user message, broadcasts message_added, and sends NO query_queued (nothing
    was live to queue behind)."""
    _make_session("sess-1")
    with _Patches() as p:
        handler = _make_handler()
        await handler.handle_query({
            "prompt": "hello world",
            "session_id": "sess-1",
            "profile": "claude-code",
            "project": None,
        })

    queued = turns_db.get_queued_turns("sess-1")
    assert [(t["source"], t["prompt"]) for t in queued] == [("user", "hello world")]
    assert _sent(handler, "query_queued") == []
    p.sync.broadcast_message_added.assert_awaited_once()
    # user message persisted to the surviving session_messages dual-write
    rows = mock_db.execute(
        "SELECT role, content FROM session_messages WHERE session_id='sess-1'"
    ).fetchall()
    assert [(r["role"], r["content"]) for r in rows] == [("user", "hello world")]


async def test_handle_query_busy_queues_behind_live_turn_and_acks(mock_db):
    """A query for a session with a live turn enqueues a source=user turn BEHIND
    the live turn (never cancels it) and sends the F5 query_queued ack + a
    queue_updated frame carrying the new pending message."""
    _make_session("sess-1")
    live = turns_db.enqueue_turn("sess-1", "user", prompt="live turn")["id"]
    turns_db.start_turn(live, run_id="run-1")

    with _Patches() as p:
        handler = _make_handler()
        await handler.handle_query({
            "prompt": "second while busy",
            "session_id": "sess-1",
            "profile": "claude-code",
            "project": None,
        })

    # live turn untouched
    assert turns_db.get_live_turn("sess-1")["id"] == live
    # the follow-up is queued (not live)
    queued = turns_db.get_queued_turns("sess-1")
    assert [(t["source"], t["prompt"]) for t in queued] == [("user", "second while busy")]
    # F5 ack + pending-message frame
    assert _sent(handler, "query_queued") == [
        {"type": "query_queued", "session_id": "sess-1"}
    ]
    frames = _queue_frames(handler)
    assert frames, "a queue_updated frame must reach this (streaming) socket"
    assert [m["content"] for m in frames[-1]["data"]["queued_messages"]] == [
        "second while busy"
    ]
    p.sync.broadcast_queue_updated.assert_awaited()


async def test_handle_query_busy_defers_the_transcript_record(mock_db):
    """The model has not seen this prompt yet, so nothing is written to the
    transcript at send time — it renders as a pending message instead, and the
    turn row carries the marker that says who owes the record."""
    _make_session("sess-1")
    live = turns_db.enqueue_turn("sess-1", "user", prompt="live turn")["id"]
    turns_db.start_turn(live, run_id="run-1")

    with _Patches() as p:
        handler = _make_handler()
        await handler.handle_query({
            "prompt": "second while busy",
            "session_id": "sess-1",
            "profile": "claude-code",
            "project": None,
        })

    rows = mock_db.execute(
        "SELECT role, content FROM session_messages WHERE session_id='sess-1'"
    ).fetchall()
    assert rows == []
    p.sync.broadcast_message_added.assert_not_awaited()
    queued = turns_db.get_queued_turns("sess-1")
    assert queued[0]["payload"]["deferred_user_record"] is True


async def test_handle_query_idle_still_records_immediately(mock_db):
    """The idle path is unchanged: nothing is deferred when the model reads the
    prompt right away."""
    _make_session("sess-1")

    with _Patches() as p:
        handler = _make_handler()
        await handler.handle_query({
            "prompt": "nobody is busy",
            "session_id": "sess-1",
            "profile": "claude-code",
            "project": None,
        })

    rows = mock_db.execute(
        "SELECT role, content FROM session_messages WHERE session_id='sess-1'"
    ).fetchall()
    assert [(r["role"], r["content"]) for r in rows] == [("user", "nobody is busy")]
    p.sync.broadcast_message_added.assert_awaited_once()
    queued = turns_db.get_queued_turns("sess-1")
    assert "deferred_user_record" not in queued[0]["payload"]


async def test_handle_query_busy_tries_to_steer_the_running_turn(mock_db):
    """After the ack, the handler asks the live turn to take the message NOW
    rather than at the next turn."""
    _make_session("sess-1")
    live = turns_db.enqueue_turn("sess-1", "user", prompt="live turn")["id"]
    turns_db.start_turn(live, run_id="run-1")

    with _Patches():
        with patch(
            "app.core.session_runtime.try_steer_pending",
            new=AsyncMock(return_value=1),
        ) as steer:
            handler = _make_handler()
            await handler.handle_query({
                "prompt": "second while busy",
                "session_id": "sess-1",
                "profile": "claude-code",
                "project": None,
            })

    steer.assert_awaited_once_with("sess-1")


async def test_handle_query_idle_never_steers(mock_db):
    _make_session("sess-1")

    with _Patches():
        with patch(
            "app.core.session_runtime.try_steer_pending", new=AsyncMock(),
        ) as steer:
            handler = _make_handler()
            await handler.handle_query({
                "prompt": "nobody is busy",
                "session_id": "sess-1",
                "profile": "claude-code",
                "project": None,
            })

    steer.assert_not_awaited()


async def test_handle_query_command_persisted_as_command_role(mock_db):
    """A slash command on an existing session persists role=command with parsed
    metadata (parity with the legacy classification)."""
    _make_session("sess-1")
    with _Patches():
        handler = _make_handler()
        await handler.handle_query({
            "prompt": "/compact now",
            "session_id": "sess-1",
            "profile": "claude-code",
            "project": None,
        })

    rows = mock_db.execute(
        "SELECT role, content FROM session_messages WHERE session_id='sess-1'"
    ).fetchall()
    assert [(r["role"], r["content"]) for r in rows] == [("command", "/compact now")]
    assert [(t["source"], t["prompt"]) for t in turns_db.get_queued_turns("sess-1")] == [
        ("user", "/compact now")
    ]


async def test_handle_query_ignores_legacy_worktree_id_key(mock_db):
    """A NEW-session query that still carries a legacy ``worktree_id`` key is
    accepted as an ordinary session create: the key is IGNORED — no worktree
    lookup runs, the created session has no legacy worktree columns, and the
    turn is submitted WITHOUT a worktree_id kwarg (Q9-1 intake deletion)."""
    with _Patches():
        with patch.object(cwh.database, "get_worktree", MagicMock()) as get_wt, \
             patch.object(cwh.turn_manager, "submit", MagicMock()) as submit:
            handler = _make_handler()
            await handler.handle_query({
                "prompt": "new session please",
                "session_id": None,
                "profile": "default",
                "project": None,
                "worktree_id": "wt-legacy-1",
            })

    # the legacy key triggered NO worktree resolution
    get_wt.assert_not_called()

    # a session was created and echoed WITHOUT a worktree_id key
    created = _sent(handler, "session_created")
    assert len(created) == 1
    new_sid = created[0]["session_id"]
    assert "worktree_id" not in created[0]

    # persisted session carries no legacy worktree columns
    sess = database.get_session(new_sid)
    assert sess.get("worktree_id") is None
    assert sess.get("worktree_branch") is None
    assert sess.get("worktree_path") is None
    # a user-initiated new-chat session is created in the Active group
    assert bool(sess.get("is_active")) is True

    # submit invoked without a worktree_id kwarg
    submit.assert_called_once()
    assert "worktree_id" not in submit.call_args.kwargs


async def test_handle_query_persists_context_window_on_new_session(mock_db):
    with _Patches():
        with patch.object(cwh.turn_manager, "submit", MagicMock()):
            handler = _make_handler()
            await handler.handle_query({
                "prompt": "new session with standard context",
                "session_id": None,
                "profile": "default",
                "project": None,
                "overrides": {
                    "model": "sonnet",
                    "effort": "high",
                    "permission_mode": "plan",
                    "context_window": 200_000,
                },
            })

    session_id = _sent(handler, "session_created")[0]["session_id"]
    row = database.get_session(session_id)
    assert row["model"] == "sonnet"
    assert row["effort"] == "high"
    assert row["permission_mode"] == "plan"
    assert row["context_window"] == 200_000


# ═══ handle_queue_message — submit(queue) ════════════════════════════════════


async def test_handle_queue_message_submits_queue_turn_and_broadcasts(mock_db):
    """queue_message enqueues a durable source=queue turn and re-broadcasts the
    queue from the turns table."""
    _make_session("sess-1")
    with _Patches() as p:
        handler = _make_handler()
        await handler.handle_queue_message(
            {"session_id": "sess-1", "content": "later work", "profile": "claude-code"}
        )

    queued = turns_db.get_queued_turns("sess-1")
    assert [(t["source"], t["prompt"]) for t in queued] == [("queue", "later work")]
    # transport context rides the turn at parity with handle_message, so a
    # drained queue turn keeps the attention bump + AskUserQuestion fanout
    assert queued[0]["payload"].get("user_id") == "admin"
    frames = _queue_frames(handler)
    assert frames and [m["content"] for m in frames[-1]["data"]["queued_messages"]] == [
        "later work"
    ]
    p.sync.broadcast_queue_updated.assert_awaited()


# ═══ handle_remove_queued_message / handle_clear_queued_messages ═════════════


async def test_handle_remove_queued_message_aborts_turn(mock_db):
    """remove_queued_message aborts the queued turn (kept as history) and the
    queue view re-sources to empty."""
    _make_session("sess-1")
    tid = turns_db.enqueue_turn("sess-1", "user", prompt="drop me")["id"]

    with _Patches():
        handler = _make_handler()
        await handler.handle_remove_queued_message(
            {"session_id": "sess-1", "message_id": tid}
        )

    assert turns_db.get_turn(tid)["state"] == "aborted"
    assert turns_db.get_turn(tid)["stop_reason"] == "removed_from_queue"
    assert cwh._v2_queue_view("sess-1") == []


async def test_handle_clear_queued_messages_aborts_only_user_chips(mock_db):
    """clear_queued_messages aborts every user/queue chip but leaves internal
    (failover/…) queued turns untouched."""
    _make_session("sess-1")
    t_user = turns_db.enqueue_turn("sess-1", "user", prompt="a")["id"]
    t_queue = turns_db.enqueue_turn("sess-1", "queue", prompt="b")["id"]
    t_internal = turns_db.enqueue_turn("sess-1", "failover", prompt="retry")["id"]

    with _Patches():
        handler = _make_handler()
        await handler.handle_clear_queued_messages({"session_id": "sess-1"})

    assert turns_db.get_turn(t_user)["state"] == "aborted"
    assert turns_db.get_turn(t_queue)["state"] == "aborted"
    assert turns_db.get_turn(t_internal)["state"] == "queued"
    assert cwh._v2_queue_view("sess-1") == []


# ═══ handle_stop — interrupt(stop) ════════════════════════════════════════════


async def test_handle_stop_interrupts_live_turn(mock_db):
    """A stop flips the live turn running->interrupting (durable intent). The
    query-scoped interrupt already tears down background tasks, so no per-task
    teardown is invoked."""
    _make_session("sess-1")
    live = turns_db.enqueue_turn("sess-1", "user", prompt="p")["id"]
    turns_db.start_turn(live, run_id="run-1")

    with _Patches():
        with patch(
            "app.core.query_engine.stop_all_background_tasks", new=AsyncMock()
        ) as stop_bg:
            handler = _make_handler()
            await handler.handle_stop(
                {"session_id": "sess-1", "stop_background": True}
            )

    assert turns_db.get_turn(live)["state"] == "interrupting"
    stop_bg.assert_not_awaited()


async def test_handle_stop_with_current_turn_id_interrupts(mock_db):
    """A stop carrying the CURRENT running turn's id interrupts it (turn-scoped
    stop, #585)."""
    _make_session("sess-1")
    live = turns_db.enqueue_turn("sess-1", "user", prompt="p")["id"]
    turns_db.start_turn(live, run_id="run-1")

    with _Patches():
        handler = _make_handler()
        await handler.handle_stop(
            {"session_id": "sess-1", "turn_id": live}
        )

    assert turns_db.get_turn(live)["state"] == "interrupting"


async def test_handle_stop_stale_turn_id_spares_successor(mock_db):
    """GitHub #585 regression: turn N settles completed, successor N+1 becomes
    running, then a stop carrying N's (now stale) turn_id is delivered. The
    id=turn_id guard matches zero rows, so N+1 stays running — it is NOT
    interrupted by the stop that was meant for N."""
    _make_session("sess-1")
    n = turns_db.enqueue_turn("sess-1", "user", prompt="first")["id"]
    turns_db.start_turn(n, run_id="run-1")
    turns_db.settle_turn(n, "completed")
    n1 = turns_db.enqueue_turn("sess-1", "queue", prompt="second")["id"]
    turns_db.start_turn(n1, run_id="run-2")
    assert turns_db.get_live_turn("sess-1")["id"] == n1

    with _Patches():
        handler = _make_handler()
        await handler.handle_stop(
            {"session_id": "sess-1", "turn_id": n}
        )

    # Successor untouched (still running); the stale target stays completed.
    assert turns_db.get_turn(n1)["state"] == "running"
    assert turns_db.get_turn(n)["state"] == "completed"


# ═══ handle_interrupt_for_query — interrupt(send_now) + confirm ══════════════


async def test_interrupt_for_query_send_now_confirms(mock_db, monkeypatch):
    """The two-phase send-now flips the live turn running->interrupting with
    mode=send_now and confirms (here the drain wait times out fast — the turn is
    kept alive by no runtime — and the confirm is sent unconditionally)."""
    monkeypatch.setattr(cwh, "_INTERRUPT_DRAIN_TIMEOUT_SECONDS", 0.2)
    _make_session("sess-1")
    live = turns_db.enqueue_turn("sess-1", "user", prompt="p")["id"]
    turns_db.start_turn(live, run_id="run-1")

    with _Patches():
        handler = _make_handler()
        await handler.handle_interrupt_for_query({"session_id": "sess-1"})

    row = turns_db.get_turn(live)
    assert row["state"] == "interrupting"
    assert row["interrupt_mode"] == "send_now"
    assert _sent(handler, "interrupt_confirmed") == [
        {"type": "interrupt_confirmed", "session_id": "sess-1"}
    ]


async def test_interrupt_for_query_idle_confirms_immediately(mock_db):
    """No live turn -> the drain predicate is satisfied at once and the confirm
    is sent without waiting."""
    _make_session("sess-1")
    with _Patches():
        handler = _make_handler()
        await handler.handle_interrupt_for_query({"session_id": "sess-1"})

    assert _sent(handler, "interrupt_confirmed") == [
        {"type": "interrupt_confirmed", "session_id": "sess-1"}
    ]
