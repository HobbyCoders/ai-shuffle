"""
``load_session`` queue reconciliation + queue-drain-once lock for
``ChatWebSocketHandler`` — Phase 6.1 STEPS 3 & 4 of
docs/STREAMING_UNIFICATION_PLAN.md.

Part A — drain-on-load port (STEP 3, closure-canonical):
The live ``/ws/chat`` closure runs ``drain_next_queued_if_idle`` at the end of
``load_session`` (websocket.py, log_prefix="load-session") — the recovery for
"Send later" messages whose drain was dropped because the originating socket
died (the user reloaded). The class had NO equivalent; the port routes the
recovery through the class's own drain body (``_process_next_queued``) with the
closure's exact gating, pinned here against the oracle
(tests/api/test_ws_closure_characterization.py, section G):

* idle session + leftover queued message -> popped, persisted (slash commands
  classified role=command), turn spawned, post-pop queue broadcast,
* streaming session -> the drain must NOT fire (a turn owns the session),
* stop / interrupt-pending latches -> no drain, and the latch SURVIVES the
  load (a user Stop must not be un-done by a reload),
* drain failures are swallowed (the load itself already succeeded),
* like the closure's drain, delivery is socket-independent — a dead origin
  socket still drains (the turn streams via live store + sync_engine).

Conscious class-model difference vs the closure's ``_drain_reserved``: the
closure ALSO direct-sends a ``sync``/``queue_updated`` frame on the origin
socket (it may be unregistered mid-stream); the class drain broadcasts via
``sync_engine`` only — at load time this device was JUST registered by
``load_session``, so the broadcast reaches it.

Part A also pins ``queuedMessages`` parity on the ``history`` frame (plan
step 3 "keep queuedMessages" — a class-only key the closure never had).

Part B — the queue-drain-once lock (STEP 4):
A message queued WHILE a turn is streaming must drain EXACTLY once, at turn
end, via the class's pre-close ``handed_off_to_queued`` model — never while
the turn is still streaming, and never a second time from the drain-on-load
port (the DB pop is atomic; the check-and-register under
``_active_sessions_lock`` inside ``_process_next_queued`` is the
serialization point). The mid-stream test drives the REAL ``run_query``
pipeline (mirroring tests/api/test_chat_ws_drain_pump.py) with mocked
boundaries and a gated fake stream.

Harness mirrors test_chat_ws_handler_relay.py / test_chat_ws_handler_watch.py:
fake WebSocket, mocked collaborators, no Postgres / SDK required.
"""

import asyncio
import json
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.websockets import WebSocketState

import app.api.chat_ws_handler as cwh
import app.api.websocket as ws_mod


# ─── helpers ─────────────────────────────────────────────────────────────────


class _FakeWebSocket:
    """Minimal Starlette-WebSocket stand-in: records outbound frames decoded."""

    def __init__(self):
        self.client_state = WebSocketState.CONNECTED
        self.sent: list = []

    async def send_text(self, text: str):
        self.sent.append(json.loads(text))


class _FakeLiveTask:
    """Stands in for a live (not done) asyncio.Task in _active_chat_sessions."""

    def __init__(self, done: bool = False):
        self._done = done

    def done(self) -> bool:
        return self._done


class _Harness:
    """Patches every boundary load_session + the queue-drain machinery reach.

    Exposes the same surface as the sibling harnesses so assertions mirror the
    closure oracle 1:1:
      .sync / .db / .store / .perm / .uq / .resolver
      .registry / .stopped / .interrupt_pending    (control-plane sets)
      .queue / .popped                             (in-memory queued-message DB)
    """

    def __enter__(self):
        self._stack = ExitStack()
        s = self._stack

        self.queue: list = []   # the server-side queued_messages table
        self.popped: list = []  # every message actually popped (drained)
        self.registry: dict = {}
        self.stopped: set = set()
        self.interrupt_pending: set = set()

        # --- sync engine ------------------------------------------------------
        self.sync = s.enter_context(patch.object(cwh, "sync_engine"))
        self.sync.is_device_watching_session = MagicMock(return_value=False)
        self.sync.is_session_streaming = MagicMock(return_value=False)
        self.sync.get_device_count = MagicMock(return_value=1)
        self.sync._connections = {}
        for name in (
            "register_device",
            "unregister_device",
            "broadcast_session_opened",
            "broadcast_stream_end",
            "broadcast_queue_updated",
            "broadcast_message_added",
            "broadcast_session_updated",
        ):
            setattr(self.sync, name, AsyncMock())

        # --- database (queue-aware: pop/add/requeue mutate self.queue) --------
        self.session_row = {
            "sdk_session_id": None,
            "title": "t",
            "worktree_id": None,
            "project_id": None,
            "total_tokens_in": 0,
            "total_tokens_out": 0,
        }

        def _get_session(sid):
            if self.session_row is None:
                return None
            row = dict(self.session_row)
            row.setdefault("id", sid)
            return row

        def _pop(sid):
            if self.queue:
                msg = self.queue.pop(0)
                self.popped.append(msg)
                return msg
            return None

        def _add_queued(*, session_id, content, profile_id=None, project_id=None):
            self.queue.append({
                "id": f"q{len(self.queue) + 1}",
                "content": content,
                "profile_id": profile_id,
                "project_id": project_id,
            })

        def _requeue_front(*, session_id, content, profile_id=None, project_id=None):
            self.queue.insert(0, {
                "id": "q-requeued",
                "content": content,
                "profile_id": profile_id,
                "project_id": project_id,
            })

        self.db = s.enter_context(patch.object(cwh, "database"))
        self.db.get_session = MagicMock(side_effect=_get_session)
        self.db.get_session_by_sdk_id = MagicMock(return_value=None)
        self.db.get_worktree = MagicMock(return_value=None)
        self.db.get_queued_messages = MagicMock(side_effect=lambda sid: list(self.queue))
        self.db.count_queued_messages = MagicMock(side_effect=lambda sid: len(self.queue))
        self.db.pop_next_queued_message = MagicMock(side_effect=_pop)
        self.db.add_queued_message = MagicMock(side_effect=_add_queued)
        self.db.requeue_message_at_front = MagicMock(side_effect=_requeue_front)
        self.db.add_session_message = MagicMock()

        async def _fake_run_sync(fn, *a, **k):
            return fn(*a, **k)

        s.enter_context(
            patch.object(cwh, "run_sync", new=AsyncMock(side_effect=_fake_run_sync))
        )

        # --- message resolver / working dir / cost metadata --------------------
        self.resolver = s.enter_context(patch.object(cwh, "message_resolver"))
        self.resolver.resolve = MagicMock(
            return_value=SimpleNamespace(messages=[], source="db", usage=None)
        )
        self.resolver.resolve_for_streaming_init = MagicMock(return_value=[])
        s.enter_context(patch.object(cwh, "resolve_working_dir", return_value="/tmp"))
        s.enter_context(
            patch.object(cwh, "enrich_session_worktree_meta", MagicMock(return_value=None))
        )
        s.enter_context(patch("app.core.cost_utils.attach_cost_metadata", MagicMock()))
        s.enter_context(
            patch("app.core.cost_utils.get_cached_profile_map", MagicMock(return_value={}))
        )

        # --- live session store -------------------------------------------------
        self.store = s.enter_context(patch.object(cwh, "live_session_store"))
        self.store.get_session_dict = MagicMock(return_value=None)
        self.store.has_session = MagicMock(return_value=False)
        self.store.is_streaming = MagicMock(return_value=False)
        for name in (
            "start_streaming",
            "add_user_message",
            "end_streaming",
            "cleanup_session",
            "discard_empty_user_turn",
        ):
            setattr(self.store, name, AsyncMock())

        # --- permission / question handlers -------------------------------------
        self.perm = s.enter_context(patch.object(cwh, "permission_handler"))
        self.perm.get_pending_requests = MagicMock(return_value=[])
        self.perm.get_queue_count = MagicMock(return_value=0)
        self.uq = s.enter_context(patch.object(cwh, "user_question_handler"))
        self.uq.get_pending_questions = MagicMock(return_value=[])

        # --- webhooks / connectors / cloud ---------------------------------------
        s.enter_context(patch.object(cwh, "dispatch_session_complete", new=AsyncMock()))
        s.enter_context(patch.object(cwh, "dispatch_session_error", new=AsyncMock()))
        self.conn = s.enter_context(patch.object(cwh, "connector_manager"))
        self.conn.dispatch_event = AsyncMock()
        s.enter_context(
            patch("app.core.cloud_relay.is_remote_session", MagicMock(return_value=False))
        )

        # --- module-global control planes ----------------------------------------
        s.enter_context(patch.object(ws_mod, "_active_chat_sessions", new=self.registry))
        s.enter_context(patch.object(ws_mod, "_active_sessions_lock", new=asyncio.Lock()))
        s.enter_context(patch.object(cwh, "_stopped_sessions", new=self.stopped))
        s.enter_context(patch.object(cwh, "_interrupt_pending_sessions", new=self.interrupt_pending))

        return self

    def __exit__(self, *exc):
        self._stack.close()
        return False

    def make_handler(self):
        """Real handler on a fake CONNECTED websocket (frames recorded)."""
        self.ws = _FakeWebSocket()
        return cwh.ChatWebSocketHandler(
            websocket=self.ws,  # type: ignore[arg-type]
            device_id="dev-1",
            user_id="admin",
            api_user_id=None,
        )

    def make_load_handler(self):
        """Handler with run_query STUBBED — for load-path unit tests (the spawn
        is recorded, never executed)."""
        handler = self.make_handler()
        handler.run_query = AsyncMock(return_value=None)
        return handler

    def make_streaming_handler(self):
        """Handler whose REAL run_query is wrapped in a prompt-recording spy —
        for the mid-stream drain-once integration test. send_json is stubbed
        (frame shapes are Part A's job)."""
        handler = self.make_handler()
        handler.send_json = AsyncMock(return_value=True)
        real_run_query = handler.run_query  # bound method, captured pre-wrap
        prompts: list = []

        async def _spy(prompt, *a, **k):
            prompts.append(prompt)
            return await real_run_query(prompt, *a, **k)

        handler.run_query = _spy
        return handler, prompts


def _shapes(ws):
    """Outbound frames with the class's ``_seq`` diagnostic key stripped."""
    out = []
    for f in ws.sent:
        f = dict(f)
        f.pop("_seq", None)
        out.append(f)
    return out


def _frames(ws, ftype):
    return [f for f in _shapes(ws) if f.get("type") == ftype]


def _errors(ws):
    return _frames(ws, "error")


LOAD = {"type": "load_session", "session_id": "sess-1"}


def _queued(content, profile_id=None, project_id=None):
    return {"id": "q1", "content": content, "profile_id": profile_id, "project_id": project_id}


# ═══ A1. drain-on-load — the recovery the closure had and the class was missing ═


async def test_load_session_drains_leftover_queued_message():
    """Oracle parity (test_load_session_drains_queued_message_on_load): loading
    an idle session with a leftover queued message pops it, persists it
    (slash commands classified role=command), spawns the turn, and broadcasts
    the post-pop queue. The history frame still shows the PRE-drain queue
    (queuedMessages is read before the drain runs)."""
    with _Harness() as h:
        handler = h.make_load_handler()
        h.queue.append(_queued("/compact now"))
        await handler.handle_load_session(dict(LOAD))
        await asyncio.sleep(0)  # retire the create_task'd (stubbed) run_query

    # history carries the pre-drain queue (plan step 3: keep queuedMessages)
    hist = _frames(h.ws, "history")[0]
    assert hist["queuedMessages"] == [_queued("/compact now")]
    # popped exactly once, persisted as a command row (closure-identical shape)
    h.db.pop_next_queued_message.assert_called_once_with("sess-1")
    mk = h.db.add_session_message.call_args.kwargs
    assert mk["role"] == "command"
    assert mk["content"] == "/compact now"
    assert mk["metadata"] == {"command_name": "/compact", "command_args": "now"}
    assert mk["profile_id"] == "claude-code"
    # exactly ONE turn spawned for it, registered as the session's active turn
    handler.run_query.assert_called_once()
    assert handler.run_query.call_args.args == (
        "/compact now", "sess-1", "claude-code", None, None, None
    )
    assert "sess-1" in h.registry
    # post-pop queue broadcast (class model: sync_engine broadcast; the closure
    # additionally direct-sent the frame — see module docstring)
    assert h.sync.broadcast_queue_updated.await_args.args == ("sess-1", [])


async def test_load_session_empty_queue_drain_is_noop():
    """No leftover queue: the drain probes (pop returns None) and nothing is
    spawned; history omits the queuedMessages key entirely."""
    with _Harness() as h:
        handler = h.make_load_handler()
        await handler.handle_load_session(dict(LOAD))

    hist = _frames(h.ws, "history")[0]
    assert "queuedMessages" not in hist
    handler.run_query.assert_not_called()
    h.db.add_session_message.assert_not_called()


async def test_load_session_streaming_turn_skips_drain():
    """Oracle parity (test_load_session_streaming_uses_live_store_and_skips_drain):
    a live turn owns the session — the drain-on-load recovery must NOT fire,
    and the message stays queued for the turn-end drain."""
    with _Harness() as h:
        handler = h.make_load_handler()
        h.queue.append(_queued("later msg"))
        h.registry["sess-1"] = _FakeLiveTask()
        h.store.get_session_dict.return_value = {
            "messages": [{"role": "assistant", "content": "partial"}],
            "usage": {"input_tokens": 5},
            "active_block_type": "text",
        }
        await handler.handle_load_session(dict(LOAD))

    hist = _frames(h.ws, "history")[0]
    assert hist["isStreaming"] is True
    assert hist["messages"] == [{"role": "assistant", "content": "partial"}]
    h.db.pop_next_queued_message.assert_not_called()
    handler.run_query.assert_not_called()
    assert [m["content"] for m in h.queue] == ["later msg"]


async def test_load_session_stopped_latch_skips_drain_and_survives():
    """Closure parity: drain_next_queued_if_idle bails on the _stopped_sessions
    latch WITHOUT clearing it — a reload must not un-do a user Stop. The
    message stays queued (it drains when the user explicitly queues/sends)."""
    with _Harness() as h:
        handler = h.make_load_handler()
        h.queue.append(_queued("later msg"))
        h.stopped.add("sess-1")
        await handler.handle_load_session(dict(LOAD))

    h.db.pop_next_queued_message.assert_not_called()
    handler.run_query.assert_not_called()
    assert "sess-1" in h.stopped, "load_session must not clear a user Stop"
    assert [m["content"] for m in h.queue] == ["later msg"]
    # the load itself still succeeded — history delivered, queue visible
    assert _frames(h.ws, "history")[0]["queuedMessages"] == [_queued("later msg")]


async def test_load_session_interrupt_pending_skips_drain():
    """Closure parity: an in-flight interrupt_for_query owns the next turn —
    the drain must not race it (the frontend sends the real query itself)."""
    with _Harness() as h:
        handler = h.make_load_handler()
        h.queue.append(_queued("later msg"))
        h.interrupt_pending.add("sess-1")
        await handler.handle_load_session(dict(LOAD))

    h.db.pop_next_queued_message.assert_not_called()
    handler.run_query.assert_not_called()
    assert "sess-1" in h.interrupt_pending
    assert [m["content"] for m in h.queue] == ["later msg"]


async def test_load_session_drain_failure_is_swallowed():
    """Closure parity: a drain failure is logged and swallowed — the load
    already succeeded (history sent); no error frame, no raised exception."""
    with _Harness() as h:
        handler = h.make_load_handler()
        h.db.pop_next_queued_message = MagicMock(side_effect=RuntimeError("db down"))
        await handler.handle_load_session(dict(LOAD))

    assert _frames(h.ws, "history"), "history must have been delivered before the drain"
    assert _errors(h.ws) == []


async def test_load_session_drain_is_socket_independent():
    """Closure parity (drain_next_queued_if_idle's documented contract): the
    drain intentionally does NOT bail on an unhealthy socket — delivery rides
    on the live store + sync_engine broadcasts. A load whose history send
    already failed still drains the leftover message."""
    with _Harness() as h:
        handler = h.make_load_handler()
        h.queue.append(_queued("later msg"))
        # every direct send fails (send_json swallows and returns False)
        h.ws.send_text = AsyncMock(side_effect=RuntimeError("socket dead"))
        await handler.handle_load_session(dict(LOAD))
        await asyncio.sleep(0)

    h.db.pop_next_queued_message.assert_called_once_with("sess-1")
    handler.run_query.assert_called_once()
    assert handler.run_query.call_args.args[0] == "later msg"


async def test_load_session_not_found_never_drains():
    """A ghost session id short-circuits before registration — the drain must
    not run against a session that does not exist."""
    with _Harness() as h:
        handler = h.make_load_handler()
        h.session_row = None
        await handler.handle_load_session({"type": "load_session", "session_id": "ghost"})

    assert _frames(h.ws, "session_not_found") == [
        {"type": "session_not_found", "session_id": "ghost"}
    ]
    h.db.pop_next_queued_message.assert_not_called()


async def test_load_session_duplicate_tab_never_drains():
    """The duplicate-tab rejection returns before registration — no drain (the
    other tab's handler owns the session's queue lifecycle)."""
    with _Harness() as h:
        h.sync.is_device_watching_session.return_value = True
        other_ws = SimpleNamespace(client_state=WebSocketState.CONNECTED)
        h.sync._connections = {
            "sess-1": {"dev-1": SimpleNamespace(websocket=other_ws)}
        }
        handler = h.make_load_handler()
        h.queue.append(_queued("later msg"))
        await handler.handle_load_session(dict(LOAD))

    assert _frames(h.ws, "session_already_open")
    h.db.pop_next_queued_message.assert_not_called()
    handler.run_query.assert_not_called()


# ═══ A2. drain-vs-new-turn race — the busy guard inside _process_next_queued ═══


async def test_load_drain_preempted_by_new_turn_requeues_not_double_runs():
    """A turn starts in the window between load_session's is_streaming check
    and the drain (emulated via the broadcast_session_opened side effect —
    the last await before the drain). The drain's busy guard under
    _active_sessions_lock must re-queue the popped message at the front
    instead of launching a competing query — the message is never lost and
    never double-run (this is the class's equivalent of the closure's
    _draining_sessions reservation)."""
    with _Harness() as h:
        handler = h.make_load_handler()
        h.queue.append(_queued("later msg"))

        async def _turn_started(*a, **k):
            h.registry["sess-1"] = _FakeLiveTask()

        h.sync.broadcast_session_opened = AsyncMock(side_effect=_turn_started)
        await handler.handle_load_session(dict(LOAD))
        await asyncio.sleep(0)

    # popped, then re-queued at the front — never spawned
    h.db.pop_next_queued_message.assert_called_once_with("sess-1")
    handler.run_query.assert_not_called()
    rq = h.db.requeue_message_at_front.call_args.kwargs
    assert rq["session_id"] == "sess-1"
    assert rq["content"] == "later msg"
    assert [m["content"] for m in h.queue] == ["later msg"], "message must never be lost"


# ═══ B. STEP 4 — mid-stream queue drains EXACTLY once, at turn end ══════════════


def _gated_stream(*turns):
    """A stream_to_websocket stand-in. Each call consumes the next
    ``(mid_stream, gate)`` pair from ``turns``: first chunk, signal
    ``mid_stream`` (so the test can interleave), hold the turn open until
    ``gate`` is set, then finish. Extra calls beyond ``turns`` complete
    immediately."""
    pending = list(turns)

    async def _gen(*args, **kwargs):
        yield {"type": "chunk", "content": "hi"}
        if pending:
            mid_stream, gate = pending.pop(0)
            mid_stream.set()
            await gate.wait()
        yield {"type": "done", "metadata": {}}
    return _gen


async def test_message_queued_mid_stream_drains_exactly_once_at_turn_end():
    """THE step-4 lock: a message queued WHILE a turn is streaming
      (a) is NOT drained mid-stream (neither by queue_message's idle fast-path
          nor by a load_session issued during the turn — the drain-on-load
          port must respect a live turn),
      (b) drains EXACTLY once when the turn ends (the pre-close
          handed_off_to_queued hand-off spawns the successor),
      (c) is NOT drained a second time by the successor's own turn-end drain,
          its finally-block fallback, or a later reload's drain-on-load
          (the DB pop is atomic — every later probe pops nothing).
    Drives the REAL run_query pipeline with mocked boundaries."""
    with _Harness() as h:
        handler, prompts = h.make_streaming_handler()
        gate = asyncio.Event()
        mid_stream = asyncio.Event()
        # gate the SUCCESSOR turn too, so it is still live (and inspectable in
        # the registry) after the outer task returns
        gate2 = asyncio.Event()
        mid_stream2 = asyncio.Event()

        with patch(
            "app.core.query_engine.stream_to_websocket",
            new=_gated_stream((mid_stream, gate), (mid_stream2, gate2)),
        ), patch("app.api.ws_event_handlers.dispatch_event", new=AsyncMock()):
            # Turn 1 starts (registered exactly like handle_query registers it).
            outer = asyncio.create_task(
                handler.run_query("hello", "sess-1", "claude-code", None, None, None)
            )
            h.registry["sess-1"] = outer
            await asyncio.wait_for(mid_stream.wait(), timeout=5.0)

            # (a1) queue while streaming: stored server-side, NOT drained.
            await handler.handle_queue_message(
                {"content": "queued work", "session_id": "sess-1"}
            )
            assert [m["content"] for m in h.queue] == ["queued work"]
            assert h.db.pop_next_queued_message.call_count == 0

            # (a2) a load_session DURING the stream must not drain either —
            # the drain-on-load port is gated on the live turn.
            await handler.handle_load_session(dict(LOAD))
            assert h.db.pop_next_queued_message.call_count == 0

            # (b) turn ends: pre-close pops the message and hands off to the
            # successor turn before this run_query returns.
            gate.set()
            await asyncio.wait_for(outer, timeout=5.0)
            await asyncio.wait_for(mid_stream2.wait(), timeout=5.0)
            successor = h.registry.get("sess-1")
            assert successor is not None and successor is not outer, (
                "pre-close hand-off must register the successor turn"
            )
            gate2.set()
            await asyncio.wait_for(successor, timeout=5.0)

            # (c) exactly ONE turn ran for the queued message — the successor's
            # own pre-close + finally-fallback probes popped nothing.
            assert prompts == ["hello", "queued work"]
            assert [m["content"] for m in h.popped] == ["queued work"]
            assert h.queue == []

            # A reload AFTER the drain: the drain-on-load port probes an empty
            # queue and must not spawn anything (no double-drain window).
            await handler.handle_load_session(dict(LOAD))
            await asyncio.sleep(0)
            assert prompts == ["hello", "queued work"]
            assert h.popped == [{
                "id": "q1",
                "content": "queued work",
                "profile_id": None,
                "project_id": None,
            }]


async def test_load_session_drain_recovers_orphaned_send_later_end_to_end():
    """The recovery scenario the port exists for: a 'Send later' message is
    left queued on an IDLE session (its origin socket died before draining).
    A fresh load_session drains it through the REAL run_query pipeline —
    one turn, queue emptied, no re-pop by that turn's own drain probes."""
    with _Harness() as h:
        handler, prompts = h.make_streaming_handler()
        h.queue.append(_queued("orphaned send-later"))

        # no mid-stream interleaving needed — the stream runs straight through
        with patch("app.core.query_engine.stream_to_websocket", new=_gated_stream()), \
             patch("app.api.ws_event_handlers.dispatch_event", new=AsyncMock()):
            await handler.handle_load_session(dict(LOAD))
            # the drain registered the recovery turn — let it run to completion
            task = h.registry.get("sess-1")
            assert task is not None
            await asyncio.wait_for(task, timeout=5.0)

        assert prompts == ["orphaned send-later"]
        assert [m["content"] for m in h.popped] == ["orphaned send-later"]
        assert h.queue == []


# ═══ C. Stop-before-output on a queue-drain successor must NOT lose messages ════
# Data-loss regression: two messages queued, turn A ends → pre-close pops msg1 and
# starts successor turn B; a Stop lands while turn B is running before it emits any
# output. The pre-fix bug treated msg1 (an already-committed queued message) as an
# abandoned user-typed draft — orphan-discarding it (user_message_discarded) — and
# left msg2 stranded, so BOTH queued messages vanished silently.


def _discarded_frames(handler):
    """user_message_discarded frames sent via a stubbed AsyncMock send_json."""
    return [
        c.args[0]
        for c in handler.send_json.call_args_list
        if c.args and isinstance(c.args[0], dict)
        and c.args[0].get("type") == "user_message_discarded"
    ]


async def test_stop_before_output_on_queue_drain_successor_requeues_not_discards(caplog):
    """Repro B — the data loss. Two messages queued during turn A; turn A ends so
    the pre-close drain pops msg1 and hands off to successor turn B. A Stop lands
    while turn B is running BEFORE any output.

    Pre-fix (RED): turn B's finally treats msg1 as an abandoned draft — it emits
    user_message_discarded and calls discard_orphan_first_turn, and msg2 is left
    stranded, so both messages are lost.

    Post-fix (GREEN): msg1 is re-queued at the FRONT (never discarded, no
    user_message_discarded, no orphan-discard DB op), msg2 is still present, and a
    durable ``queue-strand-on-stop`` log records the incident."""
    import logging

    with _Harness() as h:
        handler, prompts = h.make_streaming_handler()
        gate = asyncio.Event()
        mid_stream = asyncio.Event()
        gate2 = asyncio.Event()
        mid_stream2 = asyncio.Event()

        with patch(
            "app.core.query_engine.stream_to_websocket",
            new=_gated_stream((mid_stream, gate), (mid_stream2, gate2)),
        ), patch("app.api.ws_event_handlers.dispatch_event", new=AsyncMock()):
            # Turn A starts and streams.
            outer = asyncio.create_task(
                handler.run_query("hello", "sess-1", "claude-code", None, None, None)
            )
            h.registry["sess-1"] = outer
            await asyncio.wait_for(mid_stream.wait(), timeout=5.0)

            # Two messages queued while streaming.
            await handler.handle_queue_message({"content": "msg1", "session_id": "sess-1"})
            await handler.handle_queue_message({"content": "msg2", "session_id": "sess-1"})
            assert [m["content"] for m in h.queue] == ["msg1", "msg2"]

            # Turn A ends: pre-close pops msg1 and hands off to successor turn B.
            gate.set()
            await asyncio.wait_for(outer, timeout=5.0)
            await asyncio.wait_for(mid_stream2.wait(), timeout=5.0)
            successor = h.registry.get("sess-1")
            assert successor is not None and successor is not outer
            assert [m["content"] for m in h.popped] == ["msg1"]
            assert [m["content"] for m in h.queue] == ["msg2"]

            # A Stop lands while turn B runs, before it emits output.
            h.stopped.add("sess-1")

            with caplog.at_level(logging.WARNING):
                gate2.set()
                await asyncio.wait_for(successor, timeout=5.0)

        # GREEN expectations — msg1 re-queued at front, never discarded.
        assert _discarded_frames(handler) == [], (
            "a committed queued message must never be bounced back as a discarded draft"
        )
        assert not h.db.discard_orphan_first_turn.called, (
            "a queue-drain successor must never wipe the shared SDK transcript"
        )
        h.db.requeue_message_at_front.assert_called_once()
        assert h.db.requeue_message_at_front.call_args.kwargs["content"] == "msg1"
        assert [m["content"] for m in h.queue] == ["msg1", "msg2"], (
            "both queued messages must survive the Stop"
        )
        assert any("queue-strand-on-stop" in r.getMessage() for r in caplog.records), (
            "a durable strand/re-queue log must record the incident"
        )


async def test_stop_stranding_nonempty_queue_emits_durable_log(caplog):
    """Repro A — a Stop that strands a non-empty queue is non-lossy but must not be
    silent. During a streaming turn a message is queued, then Stop lands. The row
    stays (existing by-design behavior — not auto-processed) AND a durable
    ``queue-strand-on-stop`` log is emitted so the strand is diagnosable."""
    import logging

    with _Harness() as h:
        handler, prompts = h.make_streaming_handler()
        gate = asyncio.Event()
        mid_stream = asyncio.Event()

        with patch(
            "app.core.query_engine.stream_to_websocket",
            new=_gated_stream((mid_stream, gate)),
        ), patch("app.api.ws_event_handlers.dispatch_event", new=AsyncMock()):
            outer = asyncio.create_task(
                handler.run_query("hello", "sess-1", "claude-code", None, None, None)
            )
            h.registry["sess-1"] = outer
            await asyncio.wait_for(mid_stream.wait(), timeout=5.0)

            # Queue a message mid-stream, then Stop.
            await handler.handle_queue_message({"content": "later msg", "session_id": "sess-1"})
            assert [m["content"] for m in h.queue] == ["later msg"]
            h.stopped.add("sess-1")

            with caplog.at_level(logging.WARNING):
                gate.set()
                await asyncio.wait_for(outer, timeout=5.0)

        # Row survives, never auto-processed, and the strand is logged.
        assert [m["content"] for m in h.queue] == ["later msg"]
        assert prompts == ["hello"], "a stranded message must not be auto-fired after Stop"
        assert any("queue-strand-on-stop" in r.getMessage() for r in caplog.records)


async def test_genuine_first_turn_stop_still_orphan_discards(caplog):
    """Regression guard for #420: a genuine user-typed FIRST turn stopped before any
    output STILL orphan-discards (user_message_discarded emitted + first-turn discard
    DB op). The re-queue fix must fire ONLY for server-initiated queue-drain
    successors, never for a real user draft."""
    with _Harness() as h:
        handler, prompts = h.make_streaming_handler()
        gate = asyncio.Event()
        mid_stream = asyncio.Event()

        with patch(
            "app.core.query_engine.stream_to_websocket",
            new=_gated_stream((mid_stream, gate)),
        ), patch("app.api.ws_event_handlers.dispatch_event", new=AsyncMock()):
            outer = asyncio.create_task(
                handler.run_query("my draft", "sess-1", "claude-code", None, None, None)
            )
            h.registry["sess-1"] = outer
            await asyncio.wait_for(mid_stream.wait(), timeout=5.0)

            # Stop before any output on a brand-new session's first turn.
            h.stopped.add("sess-1")
            gate.set()
            await asyncio.wait_for(outer, timeout=5.0)

        discarded = _discarded_frames(handler)
        assert len(discarded) == 1 and discarded[0]["prompt"] == "my draft", (
            "#420 orphan-discard must still restore the user's typed draft"
        )
        assert h.db.discard_orphan_first_turn.called
        assert not h.db.requeue_message_at_front.called


async def test_stop_before_output_on_followup_queue_drain_requeues_and_cleans_legacy_row(caplog):
    """The PRODUCTION follow-up case (sdk_session_id NOT None): a queue-drain
    successor stopped before output must re-queue msg1 at front AND drop the eager
    ``session_messages`` row the pre-close drain persisted for it (the silent,
    legacy-table-only ``delete_orphan_user_turn`` cleanup) — so re-draining msg1
    later does not leave a duplicate legacy user row. It must NEVER run
    ``discard_orphan_first_turn`` (that wipes the SHARED SDK transcript — real
    history) nor emit ``user_message_discarded`` (that would bounce a committed
    message into the input box)."""
    import logging

    with _Harness() as h:
        # Follow-up session: it already has an SDK transcript.
        h.session_row["sdk_session_id"] = "sdk-existing"
        handler, prompts = h.make_streaming_handler()
        gate = asyncio.Event()
        mid_stream = asyncio.Event()
        gate2 = asyncio.Event()
        mid_stream2 = asyncio.Event()

        with patch(
            "app.core.query_engine.stream_to_websocket",
            new=_gated_stream((mid_stream, gate), (mid_stream2, gate2)),
        ), patch("app.api.ws_event_handlers.dispatch_event", new=AsyncMock()):
            outer = asyncio.create_task(
                handler.run_query("hello", "sess-1", "claude-code", None, None, None)
            )
            h.registry["sess-1"] = outer
            await asyncio.wait_for(mid_stream.wait(), timeout=5.0)

            await handler.handle_queue_message({"content": "msg1", "session_id": "sess-1"})
            await handler.handle_queue_message({"content": "msg2", "session_id": "sess-1"})

            gate.set()
            await asyncio.wait_for(outer, timeout=5.0)
            await asyncio.wait_for(mid_stream2.wait(), timeout=5.0)
            successor = h.registry.get("sess-1")
            assert successor is not None and successor is not outer
            assert [m["content"] for m in h.popped] == ["msg1"]

            h.stopped.add("sess-1")
            with caplog.at_level(logging.WARNING):
                gate2.set()
                await asyncio.wait_for(successor, timeout=5.0)

        # Re-queued at front, legacy row dropped, transcript never wiped, no bounce.
        assert _discarded_frames(handler) == []
        assert not h.db.discard_orphan_first_turn.called, (
            "a follow-up queue-drain successor must NOT wipe the shared transcript"
        )
        assert h.db.delete_orphan_user_turn.called, (
            "the eager session_messages row for msg1 must be dropped so a re-drain "
            "does not duplicate it in the legacy table"
        )
        h.db.requeue_message_at_front.assert_called_once()
        assert h.db.requeue_message_at_front.call_args.kwargs["content"] == "msg1"
        assert [m["content"] for m in h.queue] == ["msg1", "msg2"]
        assert any("queue-strand-on-stop" in r.getMessage() for r in caplog.records)
