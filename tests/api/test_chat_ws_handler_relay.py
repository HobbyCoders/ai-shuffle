"""
Cloud/cluster relay parity for ``ChatWebSocketHandler`` — Phase 6.1 STEP 1 of
docs/STREAMING_UNIFICATION_PLAN.md.

The live ``/ws/chat`` closure (app/api/websocket.py) relays SIX inbound msg
types to the install that owns a remote session (``is_remote_session``):
query, stop, interrupt_for_query, user_question_response,
get_pending_questions, queue_message. STEP 1 ports those branches into the
class; these tests prove the class now matches the closure's observable
contract — the relay tests in tests/api/test_ws_closure_characterization.py
are the oracle (same inputs, same forwarded payloads, same outbound frames).

Class-specific requirements pinned here on top of the closure oracle:

* Every relay branch is guarded by ``self.websocket is not None`` so
  ``HeadlessChatHandler`` NEVER relays (plan step 6) — the headless section
  proves ``is_remote_session`` is never even consulted.
* Outbound frames go through the class's ``send_json`` and therefore carry
  the ``_seq`` diagnostic key (asserted once; shape comparisons strip it).
* ``permission_response`` / ``get_pending_permissions`` deliberately gain NO
  relay branch (known gap, parity with the closure) — source-level guard.

Harness mirrors test_ws_closure_characterization.py / test_busy_write_guard_ws:
fake WebSocket, mocked collaborators, run_query/_safety_cancel coroutines
intercepted un-started. No Postgres / SDK required.
"""

import asyncio
import inspect
import json
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.websockets import WebSocketState

import app.api.chat_ws_handler as cwh
import app.api.websocket as ws_mod
import app.core.cloud_relay as cloud_relay_mod
from app.core.headless_query_runner import HeadlessChatHandler

InstallOffline = cloud_relay_mod.InstallOffline


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
        self.cancel_calls = 0

    def done(self) -> bool:
        return self._done

    def cancel(self) -> None:
        self.cancel_calls += 1


class _SelectiveCreateTask:
    """Intercepts run_query / _safety_cancel spawns WITHOUT running them;
    schedules everything else for real."""

    _INTERCEPT = ("run_query", "_safety_cancel")

    def __init__(self):
        self.coros = []
        self.names = []

    def __call__(self, coro):
        name = getattr(coro, "__qualname__", "").rsplit(".", 1)[-1]
        if name not in self._INTERCEPT:
            return asyncio.ensure_future(coro)
        self.coros.append(coro)
        self.names.append(name)
        task = MagicMock()
        task.done.return_value = True
        return task

    def spawns(self, name: str) -> int:
        return self.names.count(name)

    def close_all(self):
        for c in self.coros:
            try:
                c.close()
            except Exception:
                pass


class _HandlerHarness:
    """Patches every boundary the relay-touched class handlers reach.

    Exposes the same surface as the closure harness so the assertions mirror
    the oracle 1:1:
      .sync / .db / .session_row / .cloud / .uq / .interrupt_session
      .registry / .stopped / .interrupt_pending   (control-plane sets)
      .created                                    (intercepted spawns)
    """

    def __enter__(self):
        self._stack = ExitStack()
        s = self._stack

        self.created = _SelectiveCreateTask()
        self.registry: dict = {}
        self.stopped: set = set()
        self.interrupt_pending: set = set()

        s.enter_context(patch.object(cwh.asyncio, "create_task", new=self.created))

        # --- sync engine ------------------------------------------------------
        self.sync = s.enter_context(patch.object(cwh, "sync_engine"))
        for name in (
            "register_device",
            "unregister_device",
            "broadcast_user_session_created",
            "broadcast_session_updated",
            "broadcast_message_added",
            "broadcast_queue_updated",
        ):
            setattr(self.sync, name, AsyncMock())

        # --- database -----------------------------------------------------------
        self.session_row = {
            "sdk_session_id": None,
            "title": "t",
            "worktree_id": None,
            "project_id": None,
        }

        def _get_session(sid):
            if self.session_row is None:
                return None
            row = dict(self.session_row)
            row.setdefault("id", sid)
            return row

        self.db = s.enter_context(patch.object(cwh, "database"))
        self.db.get_session = MagicMock(side_effect=_get_session)
        self.db.get_queued_messages = MagicMock(return_value=[])
        self.db.pop_next_queued_message = MagicMock(return_value=None)

        async def _fake_run_sync(fn, *a, **k):
            return fn(*a, **k)

        s.enter_context(
            patch.object(cwh, "run_sync", new=AsyncMock(side_effect=_fake_run_sync))
        )

        # --- question handler ---------------------------------------------------
        self.uq = s.enter_context(patch.object(cwh, "user_question_handler"))
        self.uq.get_pending_questions = MagicMock(return_value=[])
        self.uq.respond = AsyncMock(return_value=True)

        # --- permission handler -------------------------------------------------
        self.ph = s.enter_context(patch.object(cwh, "permission_handler"))
        self.ph.get_pending_requests = MagicMock(return_value=[])
        self.ph.get_deferred_requests = MagicMock(return_value=[])
        self.ph.respond = AsyncMock(return_value={"resolved": True})

        # --- cloud relay (class imports inline: `from app.core import cloud_relay`)
        self.cloud = SimpleNamespace(
            is_remote_session=s.enter_context(
                patch.object(cloud_relay_mod, "is_remote_session", MagicMock(return_value=False))
            ),
            relay_message=s.enter_context(
                patch.object(cloud_relay_mod, "relay_message", AsyncMock())
            ),
            relay_interrupt=s.enter_context(
                patch.object(cloud_relay_mod, "relay_interrupt", AsyncMock())
            ),
            relay_question_response=s.enter_context(
                patch.object(cloud_relay_mod, "relay_question_response", AsyncMock(return_value=True))
            ),
            relay_get_pending_questions=s.enter_context(
                patch.object(cloud_relay_mod, "relay_get_pending_questions", AsyncMock(return_value=[]))
            ),
            relay_queue_message=s.enter_context(
                patch.object(cloud_relay_mod, "relay_queue_message", AsyncMock())
            ),
            relay_permission_response=s.enter_context(
                patch.object(cloud_relay_mod, "relay_permission_response", AsyncMock(return_value=True))
            ),
            relay_get_pending_permissions=s.enter_context(
                patch.object(
                    cloud_relay_mod,
                    "relay_get_pending_permissions",
                    AsyncMock(return_value={"pending": [], "deferred": []}),
                )
            ),
        )

        # --- inline-imported helpers --------------------------------------------
        self.interrupt_session = s.enter_context(
            patch("app.core.query_engine.interrupt_session", AsyncMock(return_value=True))
        )

        # --- module-global control-plane registries -------------------------------
        s.enter_context(patch.object(ws_mod, "_active_chat_sessions", new=self.registry))
        s.enter_context(patch.object(ws_mod, "_active_sessions_lock", new=asyncio.Lock()))
        s.enter_context(patch.object(cwh, "_stopped_sessions", new=self.stopped))
        s.enter_context(patch.object(cwh, "_interrupt_pending_sessions", new=self.interrupt_pending))

        return self

    def __exit__(self, *exc):
        try:
            self.created.close_all()
        finally:
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

    def make_headless(self):
        """The real HeadlessChatHandler (websocket=None)."""
        return HeadlessChatHandler(user_id="admin", api_user_id=None)


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


def _queue_frames(ws):
    return [
        f for f in _shapes(ws)
        if f.get("type") == "sync" and f.get("event_type") == "queue_updated"
    ]


# ═══ 1. query — relay site 1 ═══════════════════════════════════════════════════


async def test_query_remote_session_relays_and_confirms():
    """Oracle: prompt relayed with (session_row, prompt, profile, overrides),
    device registered for the mirror stream, cloud_forwarded sent, NOTHING runs
    locally (no persist, no run_query). The 'claude-code' default-profile
    sentinel is NOT a real profile row — it must never cross the relay (the
    owner would die on an unlogged "Profile not found"); it is stripped to
    None so the owner falls back to the session's own profile."""
    with _HandlerHarness() as h:
        h.cloud.is_remote_session.return_value = True
        handler = h.make_handler()
        await handler.handle_query({
            "prompt": "remote hello",
            "session_id": "sess-r",
            "profile": "claude-code",
            "overrides": {"model": "opus"},
        })

    args = h.cloud.relay_message.await_args.args
    assert args[0]["id"] == "sess-r"
    assert args[1:] == ("remote hello", None, {"model": "opus"})
    assert _frames(h.ws, "cloud_forwarded") == [
        {"type": "cloud_forwarded", "session_id": "sess-r"}
    ]
    h.sync.register_device.assert_awaited_once()
    assert h.sync.register_device.await_args.args[:2] == ("dev-1", "sess-r")
    assert handler.current_session_id == "sess-r"
    h.db.add_session_message.assert_not_called()
    assert h.created.spawns("run_query") == 0


async def test_query_remote_relays_real_profile_verbatim():
    """An explicitly selected profile is forwarded unchanged."""
    with _HandlerHarness() as h:
        h.cloud.is_remote_session.return_value = True
        await h.make_handler().handle_query({
            "prompt": "remote hello",
            "session_id": "sess-r",
            "profile": "default-ai",
        })

    args = h.cloud.relay_message.await_args.args
    assert args[1:] == ("remote hello", "default-ai", None)


async def test_query_remote_omitted_profile_relays_none():
    """No profile key at all → the handler's internal 'claude-code' default
    must not leak across the relay either."""
    with _HandlerHarness() as h:
        h.cloud.is_remote_session.return_value = True
        await h.make_handler().handle_query({
            "prompt": "remote hello",
            "session_id": "sess-r",
        })

    args = h.cloud.relay_message.await_args.args
    assert args[1:] == ("remote hello", None, None)


async def test_query_remote_offline_error_frame():
    with _HandlerHarness() as h:
        h.cloud.is_remote_session.return_value = True
        h.cloud.relay_message.side_effect = InstallOffline("worker down")
        await h.make_handler().handle_query({"prompt": "x", "session_id": "sess-r"})

    assert _errors(h.ws) == [
        {"type": "error", "message": "Device offline — cannot send: worker down"}
    ]
    assert _frames(h.ws, "cloud_forwarded") == []
    assert h.created.spawns("run_query") == 0


async def test_query_remote_failure_error_frame():
    with _HandlerHarness() as h:
        h.cloud.is_remote_session.return_value = True
        h.cloud.relay_message.side_effect = RuntimeError("boom")
        await h.make_handler().handle_query({"prompt": "x", "session_id": "sess-r"})

    assert _errors(h.ws) == [
        {"type": "error", "message": "Could not reach owning device: boom"}
    ]
    assert h.created.spawns("run_query") == 0


async def test_query_local_session_does_not_relay():
    with _HandlerHarness() as h:
        await h.make_handler().handle_query({
            "prompt": "local hello",
            "session_id": "sess-l",
            "profile": "claude-code",
        })

    h.cloud.is_remote_session.assert_called_once()
    h.cloud.relay_message.assert_not_awaited()
    assert _frames(h.ws, "cloud_forwarded") == []
    assert h.created.spawns("run_query") == 1


# ═══ 2. stop — relay site 2 ════════════════════════════════════════════════════


async def test_stop_remote_relays_interrupt_and_skips_local_paths():
    """Oracle quirk PINNED: remote stop returns BEFORE the _stopped_sessions
    latch add — a remote stop never latches locally, never interrupts locally,
    and never arms the safety-cancel timer."""
    with _HandlerHarness() as h:
        h.cloud.is_remote_session.return_value = True
        h.registry["sess-r"] = _FakeLiveTask()
        await h.make_handler().handle_stop({"session_id": "sess-r"})

    h.cloud.relay_interrupt.assert_awaited_once()
    assert h.cloud.relay_interrupt.await_args.args[0]["id"] == "sess-r"
    h.interrupt_session.assert_not_awaited()
    assert h.stopped == set(), "relay branch returns BEFORE the stopped latch"
    assert h.created.spawns("_safety_cancel") == 0
    assert h.ws.sent == [], "stop is silent"


async def test_stop_remote_offline_is_silent():
    with _HandlerHarness() as h:
        h.cloud.is_remote_session.return_value = True
        h.cloud.relay_interrupt.side_effect = InstallOffline("gone")
        await h.make_handler().handle_stop({"session_id": "sess-r"})

    assert _errors(h.ws) == [], "offline stop is a silent skip — nothing to stop here"
    assert h.stopped == set()


async def test_stop_remote_failure_is_silent():
    with _HandlerHarness() as h:
        h.cloud.is_remote_session.return_value = True
        h.cloud.relay_interrupt.side_effect = RuntimeError("boom")
        await h.make_handler().handle_stop({"session_id": "sess-r"})

    assert _errors(h.ws) == []
    h.interrupt_session.assert_not_awaited()


async def test_stop_local_does_not_relay():
    with _HandlerHarness() as h:
        await h.make_handler().handle_stop({"session_id": "sess-1"})

    h.cloud.relay_interrupt.assert_not_awaited()
    assert "sess-1" in h.stopped
    h.interrupt_session.assert_awaited_once_with("sess-1")


# ═══ 3. interrupt_for_query — relay site 3 ═════════════════════════════════════


async def test_interrupt_for_query_remote_confirms():
    """Remote interrupt_for_query relays then confirms so the frontend
    proceeds to fire the (also relayed) replacement query — the local SDK
    interrupt and the interrupt-pending latch are never touched."""
    with _HandlerHarness() as h:
        h.cloud.is_remote_session.return_value = True
        await h.make_handler().handle_interrupt_for_query({"session_id": "sess-r"})

    h.cloud.relay_interrupt.assert_awaited_once()
    h.interrupt_session.assert_not_awaited()
    assert h.interrupt_pending == set()
    assert _frames(h.ws, "interrupt_confirmed") == [
        {"type": "interrupt_confirmed", "session_id": "sess-r"}
    ]


async def test_interrupt_for_query_remote_offline_fails():
    with _HandlerHarness() as h:
        h.cloud.is_remote_session.return_value = True
        h.cloud.relay_interrupt.side_effect = InstallOffline("gone")
        await h.make_handler().handle_interrupt_for_query({"session_id": "sess-r"})

    assert _frames(h.ws, "interrupt_failed") == [{
        "type": "interrupt_failed",
        "session_id": "sess-r",
        "message": "Device offline: gone",
    }]


async def test_interrupt_for_query_remote_error_fails():
    with _HandlerHarness() as h:
        h.cloud.is_remote_session.return_value = True
        h.cloud.relay_interrupt.side_effect = RuntimeError("boom")
        await h.make_handler().handle_interrupt_for_query({"session_id": "sess-r"})

    assert _frames(h.ws, "interrupt_failed") == [{
        "type": "interrupt_failed",
        "session_id": "sess-r",
        "message": "Could not reach owning device: boom",
    }]


async def test_interrupt_for_query_local_does_not_relay():
    with _HandlerHarness() as h:
        await h.make_handler().handle_interrupt_for_query({"session_id": "sess-1"})

    h.cloud.relay_interrupt.assert_not_awaited()
    h.interrupt_session.assert_awaited_once_with("sess-1")
    assert _frames(h.ws, "interrupt_confirmed") == [
        {"type": "interrupt_confirmed", "session_id": "sess-1"}
    ]
    assert h.interrupt_pending == set(), "pending latch must be cleared in finally"


# ═══ 4. user_question_response — relay site 4 ══════════════════════════════════


async def test_user_question_response_remote_relays():
    """The pending question lives on the OWNING install; the answer is relayed
    and the local handler is never consulted. Success ack carries no reason."""
    with _HandlerHarness() as h:
        h.cloud.is_remote_session.return_value = True
        await h.make_handler().handle_user_question_response({
            "request_id": "q1",
            "session_id": "sess-r",
            "answers": {"choice": "B"},
        })

    args = h.cloud.relay_question_response.await_args.args
    assert args[0]["id"] == "sess-r"
    assert args[1:] == ("q1", {"choice": "B"})
    h.uq.respond.assert_not_awaited()
    assert _frames(h.ws, "user_question_response_ack") == [{
        "type": "user_question_response_ack",
        "session_id": "sess-r",
        "request_id": "q1",
        "success": True,
    }], "success ack must NOT carry a reason key"


async def test_user_question_response_remote_offline_reason():
    """Offline relay => success False + reason 'relay_offline' so the frontend
    KEEPS the modal (retryable) instead of dismissing a stale one."""
    with _HandlerHarness() as h:
        h.cloud.is_remote_session.return_value = True
        h.cloud.relay_question_response.side_effect = InstallOffline("gone")
        await h.make_handler().handle_user_question_response({
            "request_id": "q1",
            "session_id": "sess-r",
            "answers": {"choice": "B"},
        })

    assert _frames(h.ws, "user_question_response_ack") == [{
        "type": "user_question_response_ack",
        "session_id": "sess-r",
        "request_id": "q1",
        "success": False,
        "reason": "relay_offline",
    }]


async def test_user_question_response_remote_error_reason():
    with _HandlerHarness() as h:
        h.cloud.is_remote_session.return_value = True
        h.cloud.relay_question_response.side_effect = RuntimeError("boom")
        await h.make_handler().handle_user_question_response({
            "request_id": "q1",
            "session_id": "sess-r",
            "answers": {"choice": "B"},
        })

    ack = _frames(h.ws, "user_question_response_ack")[0]
    assert ack["success"] is False
    assert ack["reason"] == "relay_offline"


async def test_user_question_response_remote_not_pending_reason():
    """Owner authoritatively says "no such pending question" => reason
    'not_pending' so the frontend dismisses the stale modal."""
    with _HandlerHarness() as h:
        h.cloud.is_remote_session.return_value = True
        h.cloud.relay_question_response.return_value = False
        await h.make_handler().handle_user_question_response({
            "request_id": "q1",
            "session_id": "sess-r",
            "answers": {"choice": "B"},
        })

    ack = _frames(h.ws, "user_question_response_ack")[0]
    assert ack["success"] is False
    assert ack["reason"] == "not_pending"


async def test_user_question_response_local_success_no_reason():
    with _HandlerHarness() as h:
        await h.make_handler().handle_user_question_response({
            "request_id": "q1",
            "session_id": "sess-1",
            "answers": {"choice": "A"},
        })

    h.cloud.relay_question_response.assert_not_awaited()
    kw = h.uq.respond.await_args.kwargs
    assert kw == {"request_id": "q1", "session_id": "sess-1", "answers": {"choice": "A"}}
    assert _frames(h.ws, "user_question_response_ack") == [{
        "type": "user_question_response_ack",
        "session_id": "sess-1",
        "request_id": "q1",
        "success": True,
    }]


async def test_user_question_response_local_not_pending_reason():
    """Closure parity (came in with the relay feature): a LOCAL respond miss
    also acks reason='not_pending' so the frontend dismisses the stale modal."""
    with _HandlerHarness() as h:
        h.uq.respond.return_value = False
        await h.make_handler().handle_user_question_response({
            "request_id": "q1",
            "session_id": "sess-1",
            "answers": {"choice": "A"},
        })

    assert _frames(h.ws, "user_question_response_ack") == [{
        "type": "user_question_response_ack",
        "session_id": "sess-1",
        "request_id": "q1",
        "success": False,
        "reason": "not_pending",
    }]


# ═══ 5. get_pending_questions — relay site 5 ═══════════════════════════════════


async def test_get_pending_questions_remote_success_is_authoritative():
    """A SUCCESSFUL relay (even empty) replaces the local set and omits
    `stale` so old clients keep clearing stale modals."""
    with _HandlerHarness() as h:
        h.uq.get_pending_questions.return_value = [{"id": "local-only"}]
        h.cloud.is_remote_session.return_value = True
        h.cloud.relay_get_pending_questions.return_value = []
        handler = h.make_handler()
        handler.current_session_id = "sess-r"
        await handler.handle_get_pending_questions({})

    frame = _frames(h.ws, "pending_questions")[0]
    assert frame["questions"] == []
    assert "stale" not in frame


async def test_get_pending_questions_remote_offline_marks_stale():
    """FAILED relay keeps the local (non-authoritative) set and flags the
    reply stale so the frontend leaves an open modal untouched."""
    with _HandlerHarness() as h:
        h.uq.get_pending_questions.return_value = [{"id": "local-q"}]
        h.cloud.is_remote_session.return_value = True
        h.cloud.relay_get_pending_questions.side_effect = InstallOffline("gone")
        handler = h.make_handler()
        handler.current_session_id = "sess-r"
        await handler.handle_get_pending_questions({})

    assert _frames(h.ws, "pending_questions") == [{
        "type": "pending_questions",
        "session_id": "sess-r",
        "questions": [{"id": "local-q"}],
        "stale": True,
    }]


async def test_get_pending_questions_remote_error_marks_stale():
    with _HandlerHarness() as h:
        h.uq.get_pending_questions.return_value = [{"id": "local-q"}]
        h.cloud.is_remote_session.return_value = True
        h.cloud.relay_get_pending_questions.side_effect = RuntimeError("boom")
        handler = h.make_handler()
        handler.current_session_id = "sess-r"
        await handler.handle_get_pending_questions({})

    frame = _frames(h.ws, "pending_questions")[0]
    assert frame["questions"] == [{"id": "local-q"}]
    assert frame["stale"] is True


async def test_get_pending_questions_local_no_stale_no_relay():
    with _HandlerHarness() as h:
        h.uq.get_pending_questions.return_value = [{"id": "q1"}]
        handler = h.make_handler()
        handler.current_session_id = "sess-1"
        await handler.handle_get_pending_questions({})

    h.cloud.relay_get_pending_questions.assert_not_awaited()
    assert _frames(h.ws, "pending_questions") == [{
        "type": "pending_questions",
        "session_id": "sess-1",
        "questions": [{"id": "q1"}],
    }], "local reply must not carry the stale marker"


# ═══ 6. queue_message — relay site 6 ═══════════════════════════════════════════


async def test_queue_message_remote_relays_and_reads_back():
    """Remote queue_message relays the queue op (drain happens on the OWNER),
    never persists locally, and reads the shared-DB queue back for this
    device's UI. The local stopped-latch is never touched (parity)."""
    with _HandlerHarness() as h:
        h.cloud.is_remote_session.return_value = True
        h.stopped.add("sess-r")
        h.db.get_queued_messages.return_value = [{"id": "qr", "content": "later"}]
        await h.make_handler().handle_queue_message({
            "content": "later",
            "session_id": "sess-r",
            "profile": "claude-code",
            "project": "proj-1",
        })

    args = h.cloud.relay_queue_message.await_args.args
    assert args[0]["id"] == "sess-r"
    assert args[1:] == ("later", None, "proj-1"), \
        "the 'claude-code' sentinel must be stripped before crossing the relay"
    h.db.add_queued_message.assert_not_called()
    h.db.pop_next_queued_message.assert_not_called()
    assert "sess-r" in h.stopped, "remote queue op must not clear the local latch"
    assert _queue_frames(h.ws) == [{
        "type": "sync",
        "event_type": "queue_updated",
        "session_id": "sess-r",
        "data": {"queued_messages": [{"id": "qr", "content": "later"}]},
    }]
    assert _errors(h.ws) == []


async def test_queue_message_remote_offline_error_after_queue_frame():
    """Offline relay: the queue is STILL read back (reconciling away the
    frontend's optimistic entry) and THEN an error frame follows."""
    with _HandlerHarness() as h:
        h.cloud.is_remote_session.return_value = True
        h.cloud.relay_queue_message.side_effect = InstallOffline("np")
        await h.make_handler().handle_queue_message({
            "content": "later", "session_id": "sess-r",
        })

    assert _queue_frames(h.ws), "read-back frame must still be sent"
    assert _errors(h.ws) == [
        {"type": "error", "message": "Device offline — cannot queue: np"}
    ]
    types = [(f.get("type"), f.get("event_type")) for f in _shapes(h.ws)]
    assert types.index(("sync", "queue_updated")) < types.index(("error", None))


async def test_queue_message_remote_failure_error_frame():
    with _HandlerHarness() as h:
        h.cloud.is_remote_session.return_value = True
        h.cloud.relay_queue_message.side_effect = RuntimeError("boom")
        await h.make_handler().handle_queue_message({
            "content": "later", "session_id": "sess-r",
        })

    assert _errors(h.ws) == [
        {"type": "error", "message": "Could not reach owning device: boom"}
    ]
    h.db.add_queued_message.assert_not_called()


async def test_queue_message_local_does_not_relay():
    with _HandlerHarness() as h:
        h.db.get_queued_messages.return_value = [{"id": "q1", "content": "later"}]
        await h.make_handler().handle_queue_message({
            "content": "later",
            "session_id": "sess-1",
            "profile": "claude-code",
            "project": None,
        })

    h.cloud.relay_queue_message.assert_not_awaited()
    qk = h.db.add_queued_message.call_args.kwargs
    assert qk == {
        "session_id": "sess-1",
        "content": "later",
        "profile_id": "claude-code",
        "project_id": None,
    }
    assert _queue_frames(h.ws), "local queue ack still sent"


# ═══ 7. _seq stamping — relay frames go through the class send_json ════════════


async def test_relay_frames_carry_seq():
    with _HandlerHarness() as h:
        h.cloud.is_remote_session.return_value = True
        await h.make_handler().handle_query({"prompt": "x", "session_id": "sess-r"})

    raw = [f for f in h.ws.sent if f.get("type") == "cloud_forwarded"]
    assert raw and raw[0]["_seq"] == 1


# ═══ 8. Headless — relay must NEVER fire when websocket is None ════════════════
#
# Plan step 6: HeadlessChatHandler (websocket=None) must never relay — even
# for a session the registry says is remote. is_remote_session must not even
# be consulted; the local path runs instead.


async def test_headless_query_never_relays():
    with _HandlerHarness() as h:
        h.cloud.is_remote_session.return_value = True
        handler = h.make_headless()
        await handler.handle_query({
            "prompt": "headless hello",
            "session_id": "sess-h",
            "profile": "claude-code",
        })

    h.cloud.is_remote_session.assert_not_called()
    h.cloud.relay_message.assert_not_awaited()
    assert h.created.spawns("run_query") == 1, "headless query must run locally"


async def test_headless_stop_never_relays():
    with _HandlerHarness() as h:
        h.cloud.is_remote_session.return_value = True
        await h.make_headless().handle_stop({"session_id": "sess-h"})

    h.cloud.is_remote_session.assert_not_called()
    h.cloud.relay_interrupt.assert_not_awaited()
    assert "sess-h" in h.stopped, "headless stop must run the local path"
    h.interrupt_session.assert_awaited_once_with("sess-h")


async def test_headless_interrupt_for_query_never_relays():
    with _HandlerHarness() as h:
        h.cloud.is_remote_session.return_value = True
        await h.make_headless().handle_interrupt_for_query({"session_id": "sess-h"})

    h.cloud.is_remote_session.assert_not_called()
    h.cloud.relay_interrupt.assert_not_awaited()
    h.interrupt_session.assert_awaited_once_with("sess-h")


async def test_headless_user_question_response_never_relays():
    with _HandlerHarness() as h:
        h.cloud.is_remote_session.return_value = True
        await h.make_headless().handle_user_question_response({
            "request_id": "q1",
            "session_id": "sess-h",
            "answers": {"choice": "A"},
        })

    h.cloud.is_remote_session.assert_not_called()
    h.cloud.relay_question_response.assert_not_awaited()
    h.uq.respond.assert_awaited_once()
    # headless must not even fetch the session row for relay
    h.db.get_session.assert_not_called()


async def test_headless_get_pending_questions_never_relays():
    with _HandlerHarness() as h:
        h.cloud.is_remote_session.return_value = True
        handler = h.make_headless()
        handler.current_session_id = "sess-h"
        await handler.handle_get_pending_questions({})

    h.cloud.is_remote_session.assert_not_called()
    h.cloud.relay_get_pending_questions.assert_not_awaited()
    h.uq.get_pending_questions.assert_called_once_with("sess-h")
    h.db.get_session.assert_not_called()


async def test_headless_queue_message_never_relays():
    with _HandlerHarness() as h:
        h.cloud.is_remote_session.return_value = True
        await h.make_headless().handle_queue_message({
            "content": "later", "session_id": "sess-h",
        })

    h.cloud.is_remote_session.assert_not_called()
    h.cloud.relay_queue_message.assert_not_awaited()
    # headless queue op must run locally, not relay
    h.db.add_queued_message.assert_called_once()


# ═══ 7b. permission_response — relay site 7 ════════════════════════════════════
#
# The pending permission lives on the OWNING install; the decision must be
# relayed there to unblock its awaiting request_permission() coroutine (else it
# sits until the 300s auto-deny). Mirrors user_question_response.


async def test_permission_response_remote_relays():
    """Remote permission_response relays the decision; the local handler is never
    consulted. Success ack carries no reason."""
    with _HandlerHarness() as h:
        h.cloud.is_remote_session.return_value = True
        await h.make_handler().handle_permission_response({
            "request_id": "p1",
            "session_id": "sess-r",
            "decision": "allow",
            "remember": "session",
            "pattern": "Bash:npm*",
        })

    kw = h.cloud.relay_permission_response.await_args.kwargs
    args = h.cloud.relay_permission_response.await_args.args
    assert args[0]["id"] == "sess-r"
    assert kw == {
        "request_id": "p1",
        "decision": "allow",
        "remember": "session",
        "pattern": "Bash:npm*",
    }
    h.ph.respond.assert_not_awaited()
    assert _frames(h.ws, "permission_response_ack") == [{
        "type": "permission_response_ack",
        "session_id": "sess-r",
        "request_id": "p1",
        "success": True,
    }], "success ack must NOT carry a reason key"


async def test_permission_response_remote_defer_drops_remember_pattern():
    with _HandlerHarness() as h:
        h.cloud.is_remote_session.return_value = True
        await h.make_handler().handle_permission_response({
            "request_id": "p1",
            "session_id": "sess-r",
            "decision": "defer",
            "remember": "session",
            "pattern": "X",
        })

    kw = h.cloud.relay_permission_response.await_args.kwargs
    assert kw == {
        "request_id": "p1",
        "decision": "defer",
        "remember": None,
        "pattern": None,
    }


async def test_permission_response_remote_offline_reason():
    """Offline relay => success False + reason 'relay_offline' so the frontend
    KEEPS the modal (retryable) instead of dismissing a stale one."""
    with _HandlerHarness() as h:
        h.cloud.is_remote_session.return_value = True
        h.cloud.relay_permission_response.side_effect = InstallOffline("gone")
        await h.make_handler().handle_permission_response({
            "request_id": "p1",
            "session_id": "sess-r",
            "decision": "deny",
        })

    assert _frames(h.ws, "permission_response_ack") == [{
        "type": "permission_response_ack",
        "session_id": "sess-r",
        "request_id": "p1",
        "success": False,
        "reason": "relay_offline",
    }]


async def test_permission_response_remote_error_reason():
    with _HandlerHarness() as h:
        h.cloud.is_remote_session.return_value = True
        h.cloud.relay_permission_response.side_effect = RuntimeError("boom")
        await h.make_handler().handle_permission_response({
            "request_id": "p1",
            "session_id": "sess-r",
            "decision": "deny",
        })

    ack = _frames(h.ws, "permission_response_ack")[0]
    assert ack["success"] is False
    assert ack["reason"] == "relay_offline"


async def test_permission_response_remote_not_pending_reason():
    """Owner authoritatively says "no such pending request" => reason
    'not_pending' so the frontend dismisses the stale modal."""
    with _HandlerHarness() as h:
        h.cloud.is_remote_session.return_value = True
        h.cloud.relay_permission_response.return_value = False
        await h.make_handler().handle_permission_response({
            "request_id": "p1",
            "session_id": "sess-r",
            "decision": "allow",
        })

    ack = _frames(h.ws, "permission_response_ack")[0]
    assert ack["success"] is False
    assert ack["reason"] == "not_pending"


async def test_permission_response_local_does_not_relay():
    with _HandlerHarness() as h:
        await h.make_handler().handle_permission_response({
            "request_id": "p1",
            "session_id": "sess-1",
            "decision": "allow",
        })

    h.cloud.relay_permission_response.assert_not_awaited()
    h.ph.respond.assert_awaited_once()
    kw = h.ph.respond.await_args.kwargs
    assert kw["request_id"] == "p1"
    assert kw["session_id"] == "sess-1"
    assert kw["decision"] == "allow"
    assert _frames(h.ws, "permission_response_ack"), "local ack still sent"


# ═══ 7c. get_pending_permissions — relay site 8 ════════════════════════════════


async def test_get_pending_permissions_remote_success_is_authoritative():
    """A SUCCESSFUL relay (even empty) replaces the local sets and omits `stale`
    so old clients keep clearing stale modals."""
    with _HandlerHarness() as h:
        h.ph.get_pending_requests.return_value = [{"request_id": "local-only"}]
        h.cloud.is_remote_session.return_value = True
        h.cloud.relay_get_pending_permissions.return_value = {
            "pending": [{"request_id": "owner-p"}],
            "deferred": [{"request_id": "owner-d"}],
        }
        handler = h.make_handler()
        handler.current_session_id = "sess-r"
        await handler.handle_get_pending_permissions({})

    frame = _frames(h.ws, "pending_permissions")[0]
    assert frame["pending"] == [{"request_id": "owner-p"}]
    assert frame["deferred"] == [{"request_id": "owner-d"}]
    assert "stale" not in frame


async def test_get_pending_permissions_remote_offline_marks_stale():
    """FAILED relay keeps the local (non-authoritative) sets and flags the reply
    stale so the frontend leaves an open modal untouched."""
    with _HandlerHarness() as h:
        h.ph.get_pending_requests.return_value = [{"request_id": "local-p"}]
        h.ph.get_deferred_requests.return_value = [{"request_id": "local-d"}]
        h.cloud.is_remote_session.return_value = True
        h.cloud.relay_get_pending_permissions.side_effect = InstallOffline("gone")
        handler = h.make_handler()
        handler.current_session_id = "sess-r"
        await handler.handle_get_pending_permissions({})

    assert _frames(h.ws, "pending_permissions") == [{
        "type": "pending_permissions",
        "session_id": "sess-r",
        "pending": [{"request_id": "local-p"}],
        "deferred": [{"request_id": "local-d"}],
        "stale": True,
    }]


async def test_get_pending_permissions_remote_error_marks_stale():
    with _HandlerHarness() as h:
        h.ph.get_pending_requests.return_value = [{"request_id": "local-p"}]
        h.cloud.is_remote_session.return_value = True
        h.cloud.relay_get_pending_permissions.side_effect = RuntimeError("boom")
        handler = h.make_handler()
        handler.current_session_id = "sess-r"
        await handler.handle_get_pending_permissions({})

    frame = _frames(h.ws, "pending_permissions")[0]
    assert frame["pending"] == [{"request_id": "local-p"}]
    assert frame["stale"] is True


async def test_get_pending_permissions_local_no_stale_no_relay():
    with _HandlerHarness() as h:
        h.ph.get_pending_requests.return_value = [{"request_id": "p1"}]
        handler = h.make_handler()
        handler.current_session_id = "sess-1"
        await handler.handle_get_pending_permissions({})

    h.cloud.relay_get_pending_permissions.assert_not_awaited()
    assert _frames(h.ws, "pending_permissions") == [{
        "type": "pending_permissions",
        "session_id": "sess-1",
        "pending": [{"request_id": "p1"}],
        "deferred": [],
    }], "local reply must not carry the stale marker"


# ═══ 9. Source-level parity guards ═════════════════════════════════════════════


_RELAYED_HANDLERS = (
    "handle_query",
    "handle_stop",
    "handle_interrupt_for_query",
    "handle_user_question_response",
    "handle_get_pending_questions",
    "handle_queue_message",
    "handle_permission_response",
    "handle_get_pending_permissions",
)


def test_exactly_eight_class_handlers_relay_and_all_are_headless_guarded():
    """Every relay branch consults is_remote_session AND is guarded by the
    websocket-None headless check. The permission handlers gained their relay
    branches (the previously-deferred gap is now closed), joining the six
    question/query/stop/queue relays for eight total."""
    for name in _RELAYED_HANDLERS:
        src = inspect.getsource(getattr(cwh.ChatWebSocketHandler, name))
        assert "is_remote_session" in src, f"{name} lost its relay branch"
        assert "self.websocket is not None" in src, f"{name} lost the headless guard"


# ═══ 10. Headless — permission relays must NEVER fire when websocket is None ════


async def test_headless_permission_response_never_relays():
    with _HandlerHarness() as h:
        h.cloud.is_remote_session.return_value = True
        await h.make_headless().handle_permission_response({
            "request_id": "p1",
            "session_id": "sess-h",
            "decision": "allow",
        })

    h.cloud.is_remote_session.assert_not_called()
    h.cloud.relay_permission_response.assert_not_awaited()
    h.ph.respond.assert_awaited_once()
    h.db.get_session.assert_not_called()


async def test_headless_get_pending_permissions_never_relays():
    with _HandlerHarness() as h:
        h.cloud.is_remote_session.return_value = True
        handler = h.make_headless()
        handler.current_session_id = "sess-h"
        await handler.handle_get_pending_permissions({})

    h.cloud.is_remote_session.assert_not_called()
    h.cloud.relay_get_pending_permissions.assert_not_awaited()
    h.ph.get_pending_requests.assert_called_once_with("sess-h")
    h.db.get_session.assert_not_called()
