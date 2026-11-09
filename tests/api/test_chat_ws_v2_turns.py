"""V2 turns cutover — ``ChatWebSocketHandler`` delegation (Phase 2 A6b / D4+D5).

With ``AI_SHUFFLE_V2_TURNS`` ON (the default), the WS chat handlers no longer
drive the legacy in-memory busy-guard / ``queued_messages`` / drain machinery.
They delegate to the durable ``turns`` table via ``turn_manager``:

* ``handle_query``            -> ``turn_manager.submit_async(source='user')`` (+ F5
                                 ``query_queued`` ack when a turn is already live);
* ``handle_queue_message``    -> ``turn_manager.submit_async(source='queue')``;
* ``handle_remove_queued``    -> ``turn_manager.remove_queued`` (queued->aborted);
* ``handle_clear_queued``     -> ``remove_queued`` for every user/queue chip;
* ``handle_stop``             -> ``turn_manager.interrupt_async(mode='stop')`` +
                                 all-background-task teardown;
* ``handle_interrupt_for_query`` -> ``turn_manager.interrupt_async(mode='send_now')``
                                 then ``interrupt_confirmed``;
* ``handle_load_session``     -> ``queuedMessages`` sourced from the turns view.

These run against a REAL turns table (``mock_db`` pg-shim) — the assertions read
durable turn state and the ``queue_updated`` wire frames, NOT mocked seams. The
heavy WS collaborators (sync engine, cloud relay, run offload) are patched;
``turn_manager`` / ``turns`` / ``database`` are real.

The legacy (flag-OFF) rollback path stays covered by test_busy_write_guard_ws.py,
test_chat_ws_handler_queue.py and test_chat_ws_queue_race_gaps.py.
"""

import asyncio
from contextlib import ExitStack, contextmanager
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest

import app.api.chat_ws_handler as cwh
from app.core.config import settings
from app.db import database
from app.db import turns as turns_db
from tests.helpers import sent_frames


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

        # run offload -> synchronous passthrough so DB access made through
        # ``run_sync`` stays on the loop thread (mock_db is a single-connection
        # SQLite shim). ``turn_manager.submit_async`` still hops to a worker
        # thread of its own (the shim serializes across threads) and fires its
        # kick back on the loop, which is what test_chat_ws_offload.py asserts.
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


def _queue_frames(handler):
    return [
        c.args[0]
        for c in handler.send_json.call_args_list
        if c.args
        and isinstance(c.args[0], dict)
        and c.args[0].get("event_type") == "queue_updated"
    ]


# ═══ _v2_queue_view — B3 wire shape ══════════════════════════════════════════


async def test_nonblocking_question_ws_ack_hydration_and_authorization(mock_db, monkeypatch):
    from app.core.user_question_handler import user_question_handler
    monkeypatch.setattr("app.core.turn_manager._kick", lambda sid: None)
    monkeypatch.setattr("app.core.turn_claim.notify_turns_changed", lambda sid: None)
    monkeypatch.setattr("app.core.interaction_wait.notify_changed", lambda sid: None)
    sid = _make_session("question-session")
    await user_question_handler.post_question(
        request_id="ws-question", tool_use_id="tu", session_id=sid,
        questions=[{"question": "Color?"}], broadcast_func=AsyncMock(),
    )
    handler = _make_handler()
    handler.current_session_id = sid
    await handler.handle_get_pending_questions({})
    pending = sent_frames(handler, "pending_questions")[0]["questions"]
    assert pending[0]["request_id"] == "ws-question" and pending[0]["nonblocking"] is True
    data = {"request_id": "ws-question", "session_id": sid, "answers": {"Color?": "blue"}}
    handler._authorize_mobile_session = AsyncMock(return_value=False)
    await handler.handle_user_question_response(data)
    assert turns_db.get_queued_turns(sid) == []
    assert sent_frames(handler, "user_question_response_ack") == []
    handler._authorize_mobile_session = AsyncMock(return_value=True)
    await handler.handle_user_question_response(data)
    await handler.handle_user_question_response(data)
    assert [f["success"] for f in sent_frames(handler, "user_question_response_ack")] == [True, False]
    assert len(turns_db.get_queued_turns(sid)) == 1


async def test_nonblocking_question_ws_forwards_to_owner_without_local_answer(mock_db):
    from app.network import control
    handler = _make_handler()
    handler._network_owner = AsyncMock(return_value="owner-device")
    handler._authorize_mobile_session = AsyncMock(return_value=True)
    with patch.object(control, "answer_question", AsyncMock(return_value={"success": True})) as forwarded, \
            patch.object(cwh.user_question_handler, "respond", AsyncMock()) as local:
        await handler.handle_user_question_response({
            "request_id": "q-remote", "session_id": "remote", "answers": {"Q": "A"},
        })
    local.assert_not_awaited()
    assert forwarded.await_args.args == ("owner-device",)
    assert forwarded.await_args.kwargs["answered_by_device"] == "dev-1"
    assert sent_frames(handler, "user_question_response_ack")[0]["success"] is True


@pytest.mark.parametrize("failure", ["RunnerUnreachable", "ControlRefused"])
async def test_undelivered_question_answer_acks_retryable(mock_db, failure):
    """The UI closes the card on the tap; only this ack brings it back."""
    from app.network import control
    handler = _make_handler()
    handler._network_owner = AsyncMock(return_value="owner-device")
    handler._authorize_mobile_session = AsyncMock(return_value=True)
    with patch.object(control, "answer_question", AsyncMock(side_effect=getattr(control, failure)("down"))):
        await handler.handle_user_question_response({
            "request_id": "q-remote", "session_id": "remote", "answers": {}, "skipped": ["Q"],
        })
    assert sent_frames(handler, "user_question_response_ack") == [{
        "type": "user_question_response_ack", "session_id": "remote", "request_id": "q-remote",
        "success": False, "reason": "relay_offline",
    }]


def test_peer_queue_legacy_rows_keep_attribution_separate_from_user_controls(mock_db):
    from fluid.context.peer import format_peer_message
    _make_session("legacy-peer")
    prompt = format_peer_message("sender", "Pending old message").strip().replace(
        "another agent in this session", "another top-level session", 1)
    peer = turns_db.enqueue_turn("legacy-peer", "agent_message", prompt=prompt,
                                 payload={"orchestration_sender": "sender"})
    human = turns_db.enqueue_turn("legacy-peer", "queue", prompt="user send later")
    assert [r["id"] for r in cwh._v2_queue_view("legacy-peer")] == [human["id"]]
    peers = cwh._v2_peer_queue_view("legacy-peer")
    assert peers == [{"id": peer["id"], "content": "Pending old message", "sender_label": "sender",
                      "sender_session_id": "sender", "created_at": peer["created_at"]}]


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
            "profile": "default",
            "project": None,
        })

    queued = turns_db.get_queued_turns("sess-1")
    assert [(t["source"], t["prompt"]) for t in queued] == [("user", "hello world")]
    assert sent_frames(handler, "query_queued") == []
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
    from app.db import runs
    lease = runs.claim_run("sess-1", "test-runner", 60)
    turns_db.start_turn(live, run_id=lease["id"])

    with _Patches() as p:
        handler = _make_handler()
        await handler.handle_query({
            "prompt": "second while busy",
            "session_id": "sess-1",
            "profile": "default",
            "project": None,
        })

    # live turn untouched
    assert turns_db.get_live_turn("sess-1")["id"] == live
    # the follow-up is queued (not live)
    queued = turns_db.get_queued_turns("sess-1")
    assert [(t["source"], t["prompt"]) for t in queued] == [("user", "second while busy")]
    # F5 ack + pending-message frame
    assert sent_frames(handler, "query_queued") == [
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
    from app.db import runs
    lease = runs.claim_run("sess-1", "test-runner", 60)
    turns_db.start_turn(live, run_id=lease["id"])

    with _Patches() as p:
        handler = _make_handler()
        await handler.handle_query({
            "prompt": "second while busy",
            "session_id": "sess-1",
            "profile": "default",
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
            "profile": "default",
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
    from app.db import runs
    lease = runs.claim_run("sess-1", "test-runner", 60)
    turns_db.start_turn(live, run_id=lease["id"])

    with _Patches():
        with patch(
            "app.core.session_runtime.try_steer_pending",
            new=AsyncMock(return_value=1),
        ) as steer:
            handler = _make_handler()
            await handler.handle_query({
                "prompt": "second while busy",
                "session_id": "sess-1",
                "profile": "default",
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
                "profile": "default",
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
            "profile": "default",
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
             patch.object(
                 cwh.turn_manager, "submit_async", AsyncMock(return_value="turn-x")
             ) as submit:
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
    created = sent_frames(handler, "session_created")
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
        with patch.object(
            cwh.turn_manager, "submit_async", AsyncMock(return_value="turn-x")
        ):
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

    session_id = sent_frames(handler, "session_created")[0]["session_id"]
    row = database.get_session(session_id)
    assert row["model"] == "sonnet"
    assert row["effort"] == "high"
    assert row["permission_mode"] == "plan"
    assert row["context_window"] == 200_000


async def test_handle_query_ignores_an_unoffered_window_on_new_session(mock_db):
    """#1894: a window the model does not offer (Sonnet offers 200K and 1M)
    is never stamped as a new chat's pin, which would be enforced as a ceiling."""
    with _Patches():
        with patch.object(
            cwh.turn_manager, "submit_async", AsyncMock(return_value="turn-x")
        ):
            handler = _make_handler()
            await handler.handle_query({
                "prompt": "new session with an unoffered window",
                "session_id": None,
                "profile": "default",
                "project": None,
                "overrides": {"model": "sonnet", "context_window": 150_000},
            })

    session_id = sent_frames(handler, "session_created")[0]["session_id"]
    row = database.get_session(session_id)
    assert row["model"] == "sonnet"
    assert row["context_window"] is None


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


def _withdrawal_receipts(session_id):
    from app.db import session_events

    return [(e["command_id"], e["payload"]) for e in session_events.get_events(session_id)
            if e["event_type"] == "queued_message.withdrawn"]


@pytest.mark.parametrize("by", ["message_id", "client_message_id", "clear"])
async def test_a_withdrawal_reaches_every_page_through_the_session_log(mock_db, by):
    """Page 1 withdraws B; page 2 (which sent B) learns it from the session log,
    which every page's feed reads, live or on reconnect. The receipt carries
    B's command id and no words (#1114, review round 3 S2)."""
    _make_session("sess-1")
    b_cmd, c_cmd = "client-" + "b" * 32, "client-" + "c" * 32
    b = turns_db.enqueue_turn("sess-1", "user", prompt="B words", command_id=b_cmd)["id"]
    c = turns_db.enqueue_turn("sess-1", "queue", prompt="C words", command_id=c_cmd)["id"]

    with _Patches():
        page_1 = _make_handler()
        if by == "clear":
            await page_1.handle_clear_queued_messages({"session_id": "sess-1"})
        else:
            await page_1.handle_remove_queued_message(
                {"session_id": "sess-1", by: b if by == "message_id" else b_cmd})

    withdrawn = [(b_cmd, {"message_id": b})] + ([(c_cmd, {"message_id": c})] if by == "clear" else [])
    assert _withdrawal_receipts("sess-1") == withdrawn
    from app.db import session_events
    # A content-free receipt has no transcript row: legacy history can still be imported.
    assert session_events.can_backfill_events("sess-1")
    # A repeated × is answered from the receipt, and writes no second one.
    with _Patches():
        await _make_handler().handle_remove_queued_message({"session_id": "sess-1", "client_message_id": b_cmd})
    assert _withdrawal_receipts("sess-1") == withdrawn


def test_every_queued_send_that_will_not_run_gets_a_receipt(mock_db):
    """The receipt is written by the one guarded queued -> aborted transition, so
    Stop's queue and the scheduler's removals send it too. A send that ran, a peer
    message and a row with no command id get none."""
    _make_session("sess-1")
    cmd = "client-" + "d" * 32
    stopped = turns_db.enqueue_turn("sess-1", "user", prompt="stopped words", command_id=cmd)["id"]
    no_command = turns_db.enqueue_turn("sess-1", "user", prompt="legacy")["id"]
    peer = turns_db.enqueue_turn("sess-1", "agent_message", prompt="peer", command_id="peer-cmd")["id"]
    ran = turns_db.enqueue_turn("sess-1", "user", prompt="ran", command_id="client-" + "e" * 32)["id"]
    assert turns_db.start_turn(ran) is not None

    turns_db.stop_queued_turns("sess-1", [stopped, no_command])
    turns_db.remove_queued_turn(peer)
    assert turns_db.remove_queued_turn(ran) is None

    assert _withdrawal_receipts("sess-1") == [(cmd, {"message_id": stopped})]


# ═══ handle_stop — interrupt(stop) ════════════════════════════════════════════


async def test_handle_stop_interrupts_live_turn_and_stops_background(mock_db):
    """Stop interrupts the live turn and explicitly tears down all owned tasks."""
    _make_session("sess-1")
    live = turns_db.enqueue_turn("sess-1", "user", prompt="p")["id"]
    from app.db import runs
    lease = runs.claim_run("sess-1", "test-runner", 60)
    turns_db.start_turn(live, run_id=lease["id"])

    with _Patches():
        with patch(
            "app.core.query_session_lifecycle.stop_all_background_tasks",
            new=AsyncMock(),
        ) as stop_bg:
            handler = _make_handler()
            await handler.handle_stop(
                {"session_id": "sess-1", "stop_background": True}
            )

    row = turns_db.get_turn(live)
    assert row["state"] == "interrupting"
    assert row["stop_background"] in (True, 1)
    # STOP SEMANTICS (round 7): Stop kills ALL of the session's work — shells,
    # serving tasks and watchers included — and quiesces the session so nothing
    # wakes it until the user's next message. See handle_stop's note.
    stop_bg.assert_awaited_once_with("sess-1", user_stop=True, scope=ANY,
                                     receipt=row["payload"]["stop_receipt"])


async def test_handle_stop_without_live_turn_quiesces_queued_work(mock_db):
    from app.core.task_registry import task_registry
    _make_session("sess-1")
    queued = turns_db.enqueue_turn("sess-1", "unpark", prompt="pending")
    with _Patches():
        handler = _make_handler()
        await handler.handle_stop({"session_id": "sess-1"})
    assert turns_db.get_turn(queued["id"])["state"] == "aborted"
    assert task_registry.is_user_stopped("sess-1")
    from app.core import turn_scheduler
    task_registry.start_task("sess-1", "stopped-child", "agent", "Child", tool_use_id="launch")
    task_registry.mark_background_launch("sess-1", "launch")
    await turn_scheduler.on_task_terminal("sess-1", {
        "task_id": "stopped-child", "status": "completed", "summary": "late report"})
    assert await turn_scheduler.on_agent_message("sess-1", "peer", "wake") is None
    assert turns_db.get_queued_turns("sess-1") == []


async def test_handle_stop_with_current_turn_id_interrupts(mock_db):
    """A stop carrying the CURRENT running turn's id interrupts it (turn-scoped
    stop, #585)."""
    _make_session("sess-1")
    live = turns_db.enqueue_turn("sess-1", "user", prompt="p")["id"]
    from app.db import runs
    lease = runs.claim_run("sess-1", "test-runner", 60)
    turns_db.start_turn(live, run_id=lease["id"])

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

    with _Patches(), patch(
        "app.core.query_session_lifecycle.stop_all_background_tasks",
        new=AsyncMock(),
    ) as stop_bg:
        handler = _make_handler()
        await handler.handle_stop(
            {"session_id": "sess-1", "turn_id": n}
        )

    stop_bg.assert_not_awaited()
    # Successor untouched (still running); the stale target stays completed.
    assert turns_db.get_turn(n1)["state"] == "running"
    assert turns_db.get_turn(n)["state"] == "completed"
    # The Stop reached nothing, and the page is told so instead of nothing (#1109).
    [missed] = sent_frames(handler, "stop_result")
    assert missed["session_id"] == "sess-1" and missed["turn_id"] == n and missed["stopped"] is False
    assert missed["message"]


@contextmanager
def _captured_parked_stop(session_id, turn_id):
    from app.core.query_session_lifecycle import capture_stop_scope
    from app.core.task_registry import task_registry

    task_registry.start_task(session_id, "captured-task", "shell", "Job", tool_use_id="launch")
    captured = []
    def capture(sid, **kwargs):
        assert sid == session_id
        assert turns_db.get_turn(turn_id)["state"] == "parked"  # BEFORE the interrupt
        scope = capture_stop_scope(sid, **kwargs)
        assert [task["id"] for task in scope.active] == ["captured-task"]
        assert scope.runs["captured-task"] is not None
        captured.append(scope)
        return scope
    with patch("app.core.query_session_lifecycle.capture_stop_scope", side_effect=capture):
        yield captured


@pytest.mark.parametrize("newer", ["queued", "cancelled", "started", "completed"])
async def test_handle_stop_on_a_parked_turn_ends_its_work(mock_db, newer):
    """A turn parked on background work is the chat's current turn: Stop aimed
    at it must reach that work, or the job finishes and wakes the chat anyway."""
    _make_session("sess-1")
    parked, successor = _park_with_newer_queue_history(newer)

    with _Patches(), _captured_parked_stop("sess-1", parked) as captured, patch(
        "app.core.query_session_lifecycle.stop_all_background_tasks",
        new=AsyncMock(return_value=[]),
    ) as stop_bg:
        handler = _make_handler()
        await handler.handle_stop({"session_id": "sess-1", "turn_id": parked})

    if newer in ("started", "completed"):
        stop_bg.assert_not_awaited()
        assert turns_db.get_turn(successor)["state"] == ("running" if newer == "started" else "completed")
        assert [f["stopped"] for f in sent_frames(handler, "stop_result")] == [False]
    else:
        [scope] = captured
        # Parked cleanup captures its cutoff at admission, not after an await,
        # while retaining THIS pre-interrupt local task-incarnation snapshot.
        assert turns_db.STOP_RECEIPT_KEY not in turns_db.get_turn(parked)["payload"]
        stop_bg.assert_awaited_once()
        receipt = stop_bg.await_args.kwargs["receipt"]
        assert receipt["input_cutoff"] == turns_db.get_turn(successor)["insert_order"]
        assert receipt["queued"] == ([successor] if newer == "queued" else [])
        assert [row["id"] for row in receipt["parks"]] == [parked]
        assert receipt["cleanup_guard"]["turn"]["id"] == parked
        assert receipt["cleanup_guard"]["turn"]["run_id"] == "run-1"
        stop_bg.assert_awaited_once_with("sess-1", user_stop=True, scope=scope, receipt=receipt)
        assert stop_bg.await_args.kwargs["scope"] is scope
        # The parked turn's work was stopped: no "nothing was stopped" notice.
        assert sent_frames(handler, "stop_result") == []


async def test_handle_stop_for_a_parked_turn_spares_a_later_turn(mock_db):
    """A delayed Stop for a parked turn that has since been followed by a wake
    still spares the wake, like any other Stop for a turn that has passed."""
    _make_session("sess-1")
    parked = turns_db.enqueue_turn("sess-1", "user", prompt="first")["id"]
    turns_db.start_turn(parked, run_id="run-1")
    turns_db.park_turn(parked)
    wake = turns_db.enqueue_turn("sess-1", "queue", prompt="wake")["id"]
    turns_db.start_turn(wake, run_id="run-2")

    with _Patches(), patch(
        "app.core.query_session_lifecycle.stop_all_background_tasks",
        new=AsyncMock(),
    ) as stop_bg:
        await _make_handler().handle_stop({"session_id": "sess-1", "turn_id": parked})

    stop_bg.assert_not_awaited()
    assert turns_db.get_turn(wake)["state"] == "running"


def _park_with_newer_queue_history(newer):
    parked = turns_db.enqueue_turn("sess-1", "user", prompt="first")["id"]
    turns_db.start_turn(parked, run_id="run-1")
    successor = turns_db.enqueue_turn("sess-1", "queue", prompt="second")["id"]
    if newer == "cancelled":
        turns_db.remove_queued_turn(successor)
    turns_db.park_turn(parked)
    if newer in ("started", "completed"):
        turns_db.start_turn(successor, run_id="run-2")
        if newer == "completed":
            turns_db.settle_turn(successor, "completed")
    # Queue/history consumers must still see Q; only Stop ignores unstarted Q.
    assert turns_db.get_latest_run_turn("sess-1")["id"] == successor
    return parked, successor


@pytest.mark.parametrize("newer", ["queued", "cancelled", "started", "completed"])
async def test_stop_relayed_to_the_owning_worker_ends_a_parked_turns_work(mock_db, newer):
    """The same rule when another worker's page relays the Stop to the owner."""
    from app.network import control

    _make_session("sess-1")
    parked, successor = _park_with_newer_queue_history(newer)

    with patch.object(control, "_require_owned"), _captured_parked_stop("sess-1", parked) as captured, patch(
        "app.core.query_session_lifecycle.stop_all_background_tasks",
        new=AsyncMock(return_value=[]),
    ) as stop_bg:
        reply = await control._interrupt(
            {"session_id": "sess-1", "mode": "stop", "stop_background": True, "turn_id": parked},
            "other-device",
        )

    if newer in ("started", "completed"):
        stop_bg.assert_not_awaited()
        assert reply["interrupted"] is False
        assert turns_db.get_turn(successor)["state"] == ("running" if newer == "started" else "completed")
    else:
        [scope] = captured
        # Parked cleanup captures its cutoff at admission, not after an await,
        # while retaining THIS pre-interrupt local task-incarnation snapshot.
        assert turns_db.STOP_RECEIPT_KEY not in turns_db.get_turn(parked)["payload"]
        stop_bg.assert_awaited_once()
        receipt = stop_bg.await_args.kwargs["receipt"]
        assert receipt["input_cutoff"] == turns_db.get_turn(successor)["insert_order"]
        assert receipt["queued"] == ([successor] if newer == "queued" else [])
        assert [row["id"] for row in receipt["parks"]] == [parked]
        assert receipt["cleanup_guard"]["turn"]["id"] == parked
        assert receipt["cleanup_guard"]["turn"]["run_id"] == "run-1"
        stop_bg.assert_awaited_once_with("sess-1", user_stop=True, scope=scope, receipt=receipt)
        assert stop_bg.await_args.kwargs["scope"] is scope
        assert reply["interrupted"] is True


def _turn_being_stopped(busy):
    """A leased turn an earlier Stop already moved to interrupting/draining."""
    from app.db import runs
    live = turns_db.enqueue_turn("sess-1", "user", prompt="p")["id"]
    lease = runs.claim_run("sess-1", "test-runner", 60)
    turns_db.start_turn(live, run_id=lease["id"])
    turns_db.interrupt_turn("sess-1", "stop", turn_id=live)
    if busy == "draining":
        turns_db.escalate_to_draining(live)
    assert turns_db.get_turn(live)["state"] == busy
    return live


@pytest.mark.parametrize("busy", ["interrupting", "draining"])
async def test_a_repeated_stop_while_the_turn_is_still_stopping_says_nothing(mock_db, busy):
    """Review round 1 (S1): pressing Stop again while the first Stop is still
    interrupting or draining T must not tell the page T "already finished" —
    that cleared the page's wait and recovery while T was still busy."""
    _make_session("sess-1")
    live = _turn_being_stopped(busy)

    with _Patches(), patch(
        "app.core.query_session_lifecycle.stop_all_background_tasks", new=AsyncMock(),
    ):
        handler = _make_handler()
        await handler.handle_stop({"session_id": "sess-1", "turn_id": live})

    assert turns_db.get_turn(live)["state"] == busy
    assert sent_frames(handler, "stop_result") == []


@pytest.mark.parametrize("busy", ["interrupting", "draining"])
async def test_a_repeated_relayed_stop_while_the_turn_is_still_stopping_is_not_finished(mock_db, busy):
    """The owner reports the same thing to a relaying page: not finished."""
    from app.network import control

    _make_session("sess-1")
    live = _turn_being_stopped(busy)
    with patch.object(control, "_require_owned"), patch(
        "app.core.query_session_lifecycle.stop_all_background_tasks", new=AsyncMock(return_value=[]),
    ):
        reply = await control._interrupt(
            {"session_id": "sess-1", "mode": "stop", "stop_background": True, "turn_id": live}, "other-device",
        )

    assert reply["finished"] is False
    assert turns_db.get_turn(live)["state"] == busy


async def test_a_relayed_stop_for_an_ended_turn_is_finished(mock_db):
    from app.network import control

    _make_session("sess-1")
    done = turns_db.enqueue_turn("sess-1", "user", prompt="p")["id"]
    turns_db.start_turn(done, run_id="run-1")
    turns_db.settle_turn(done, "completed")
    with patch.object(control, "_require_owned"), patch(
        "app.core.query_session_lifecycle.stop_all_background_tasks", new=AsyncMock(return_value=[]),
    ):
        reply = await control._interrupt(
            {"session_id": "sess-1", "mode": "stop", "stop_background": True, "turn_id": done}, "other-device",
        )

    assert reply["interrupted"] is False and reply["finished"] is True


@pytest.mark.parametrize("reply, missed", [
    ({"interrupted": False, "finished": True, "turn_id": None}, True),
    # Not interrupted but not ended either: an earlier Stop is still at work.
    ({"interrupted": False, "finished": False, "turn_id": None}, False),
    ({"interrupted": True, "finished": False, "turn_id": "t-live"}, False),
    ({"interrupted": False, "turn_id": None}, False),  # an older owner that does not say
])
async def test_relayed_stop_tells_the_page_when_it_reached_nothing(mock_db, reply, missed):
    """A Stop relayed to the owning worker that matched no turn and no parked
    work must not end silently on the page that pressed it (#1109)."""
    from app.network import control

    _make_session("sess-1")
    with _Patches(), patch.object(control, "interrupt", new=AsyncMock(return_value=reply)) as relay:
        handler = _make_handler()
        handler._network_owner = AsyncMock(return_value="owner-device")
        handler._v2_broadcast_queue = AsyncMock()
        await handler.handle_stop({"session_id": "sess-1", "turn_id": "t-old"})

    relay.assert_awaited_once()
    frames = sent_frames(handler, "stop_result")
    assert frames == ([{"type": "stop_result", "session_id": "sess-1", "turn_id": "t-old", "stopped": False,
                        "message": frames[0]["message"]}] if missed else [])


# ═══ handle_interrupt_for_query — interrupt(send_now) + confirm ══════════════


async def test_interrupt_for_query_send_now_confirms(mock_db, monkeypatch):
    """The two-phase send-now flips the live turn running->interrupting with
    mode=send_now and confirms (here the drain wait times out fast — the turn is
    kept alive by no runtime — and the confirm is sent unconditionally)."""
    monkeypatch.setattr(cwh, "_INTERRUPT_DRAIN_TIMEOUT_SECONDS", 0.2)
    _make_session("sess-1")
    live = turns_db.enqueue_turn("sess-1", "user", prompt="p")["id"]
    from app.db import runs
    lease = runs.claim_run("sess-1", "test-runner", 60)
    turns_db.start_turn(live, run_id=lease["id"])

    with _Patches():
        handler = _make_handler()
        await handler.handle_interrupt_for_query({"session_id": "sess-1"})

    row = turns_db.get_turn(live)
    assert row["state"] == "interrupting"
    assert row["interrupt_mode"] == "send_now"
    assert sent_frames(handler, "interrupt_confirmed") == [
        {"type": "interrupt_confirmed", "session_id": "sess-1"}
    ]


async def test_pending_interrupt_while_parked_preserves_queue_and_kicks(mock_db):
    from app.core import turn_manager

    _make_session("sess-1")
    live = turns_db.enqueue_turn("sess-1", "user", prompt="background work")
    turns_db.start_turn(live["id"], None, "runner")
    queued = turns_db.enqueue_turn("sess-1", "user", prompt="answer now")
    turns_db.park_turn(live["id"], ["task-still-running"])
    kicked = []
    turn_manager.register_kick(kicked.append)
    try:
        with _Patches(), patch(
            "app.core.query_session_lifecycle.stop_all_background_tasks", new=AsyncMock(),
        ) as stop_bg:
            handler = _make_handler()
            await handler.handle_interrupt_for_query({
                "session_id": "sess-1", "turn_id": live["id"],
            })
    finally:
        turn_manager.register_kick(None)
    stop_bg.assert_not_awaited()
    assert kicked == ["sess-1"]
    assert turns_db.get_turn(queued["id"])["state"] == "queued"
    assert turns_db.get_turn(live["id"])["state"] == "parked"
    assert not sent_frames(handler, "interrupt_failed")
    assert sent_frames(handler, "interrupt_confirmed")


async def test_pending_interrupt_stale_turn_spares_successor(mock_db):
    _make_session("sess-1")
    old = turns_db.enqueue_turn("sess-1", "user", prompt="old")
    turns_db.start_turn(old["id"], None, "runner")
    turns_db.park_turn(old["id"], ["task-still-running"])
    successor = turns_db.enqueue_turn("sess-1", "user", prompt="answer now")
    turns_db.start_turn(successor["id"], None, "runner")
    with _Patches():
        handler = _make_handler()
        await asyncio.wait_for(handler.handle_interrupt_for_query({
            "session_id": "sess-1", "turn_id": old["id"],
        }), 1)
    assert turns_db.get_turn(successor["id"])["state"] == "running"
    assert sent_frames(handler, "interrupt_confirmed")


async def test_interrupt_for_query_idle_confirms_immediately(mock_db):
    """No live turn -> the drain predicate is satisfied at once and the confirm
    is sent without waiting."""
    _make_session("sess-1")
    with _Patches():
        handler = _make_handler()
        await handler.handle_interrupt_for_query({"session_id": "sess-1"})

    assert sent_frames(handler, "interrupt_confirmed") == [
        {"type": "interrupt_confirmed", "session_id": "sess-1"}
    ]
