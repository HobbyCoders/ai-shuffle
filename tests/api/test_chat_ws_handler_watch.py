"""
``watch_session`` port + deferred-permissions parity for ``ChatWebSocketHandler``
— Phase 6.1 STEP 2 of docs/STREAMING_UNIFICATION_PLAN.md.

Part A — watch_session (closure-canonical, ported verbatim):
The frontend sends ``watch_session`` on EVERY session switch
(session-switcher.ts), so the closure's exact contract is pinned here against
the class, mirroring the oracle tests in
tests/api/test_ws_closure_characterization.py (section H):

* happy path: sync_engine registration + current_session_id binding,
* queued-message restore frame (``sync``/``queue_updated``) only when nonempty,
* the dead-vs-live duplicate-socket ASYMMETRY: a DEAD old socket is replaced
  (page-refresh recovery); a LIVE old socket skips re-registration (duplicate
  tab) but still binds current_session_id,
* (V2 A7: the headless websocket=None watcher-guard test was removed with
  headless_query_runner.py; the guard itself remains in the class.)

Part B — deferred-permissions shapes where the CLASS is the intended winner
(plan section "Divergences where the class is the intended winner"). These pin
the class contract the frontend already expects (permission-handler.ts /
PermissionQueue.svelte / types/websocket.ts) so the flip commit can consciously
replace the closure's ``requests``-shaped reply:

* ``get_pending_permissions`` replies ``pending`` + ``deferred`` (closure: ``requests``),
* ``permission_response`` supports decision="defer" (remember/pattern stripped),
* ``requeue_deferred_permissions`` -> ``permission_queue_update`` frame.

Harness mirrors tests/api/test_chat_ws_handler_relay.py: fake WebSocket,
mocked collaborators, no Postgres / SDK required.
"""

import asyncio
import json
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.websockets import WebSocketState

import app.api.chat_ws_handler as cwh


# ─── helpers ─────────────────────────────────────────────────────────────────


class _FakeWebSocket:
    """Minimal Starlette-WebSocket stand-in: records outbound frames decoded."""

    def __init__(self):
        self.client_state = WebSocketState.CONNECTED
        self.sent: list = []

    async def send_text(self, text: str):
        self.sent.append(json.loads(text))


class _HandlerHarness:
    """Patches every boundary the watch/permission handlers reach.

    Surface mirrors the relay harness so assertions read 1:1 against the
    closure oracle:
      .sync  — sync_engine (is_device_watching_session / _connections /
               register_device / unregister_device)
      .db    — database (get_queued_messages)
      .perm  — permission_handler (respond / get_pending_requests /
               get_deferred_requests / requeue_deferred / get_queue_count)
    """

    def __enter__(self):
        self._stack = ExitStack()
        s = self._stack

        # --- sync engine ------------------------------------------------------
        self.sync = s.enter_context(patch.object(cwh, "sync_engine"))
        self.sync.is_device_watching_session = MagicMock(return_value=False)
        self.sync._connections = {}
        for name in ("register_device", "unregister_device"):
            setattr(self.sync, name, AsyncMock())

        # --- database -----------------------------------------------------------
        self.db = s.enter_context(patch.object(cwh, "database"))
        self.db.get_queued_messages = MagicMock(return_value=[])
        # Permission handlers now consult the session row to decide whether to
        # relay to a remote owner. These watch/permission tests exercise the
        # LOCAL path, so return a legacy row (instance_id=None => local) keyed
        # by the requested id — is_remote_session() then never relays.
        self.db.get_session = MagicMock(
            side_effect=lambda sid: {"id": sid, "instance_id": None}
        )

        async def _fake_run_sync(fn, *a, **k):
            return fn(*a, **k)

        s.enter_context(
            patch.object(cwh, "run_sync", new=AsyncMock(side_effect=_fake_run_sync))
        )

        # --- permission handler ---------------------------------------------------
        self.perm = s.enter_context(patch.object(cwh, "permission_handler"))
        self.perm.respond = AsyncMock(return_value={"decision": "allow"})
        self.perm.get_pending_requests = MagicMock(return_value=[])
        self.perm.get_deferred_requests = MagicMock(return_value=[])
        self.perm.requeue_deferred = AsyncMock(return_value=[])
        self.perm.get_queue_count = MagicMock(return_value=0)

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


WATCH = {"type": "watch_session", "session_id": "sess-1"}


# ═══ A1. watch_session — happy path ════════════════════════════════════════════


async def test_watch_session_registers_and_binds_current_session():
    """Oracle: watch_session registers the device for sync broadcasts and binds
    current_session_id (proven both directly and via a follow-up
    get_pending_permissions, exactly like the closure oracle)."""
    with _HandlerHarness() as h:
        handler = h.make_handler()
        await handler.handle_watch_session(dict(WATCH))
        await handler.handle_get_pending_permissions({})

    # register_device awaited once with (device, session, THIS socket)
    h.sync.register_device.assert_awaited_once()
    assert h.sync.register_device.await_args.args == ("dev-1", "sess-1", h.ws)
    assert handler.current_session_id == "sess-1"
    pend = _frames(h.ws, "pending_permissions")
    assert pend and pend[0]["session_id"] == "sess-1", (
        "watch_session must bind current_session_id"
    )


async def test_watch_session_is_in_dispatch_table():
    """The run() loop dispatches via _handlers — the port is dead code unless
    the message type is registered."""
    with _HandlerHarness() as h:
        handler = h.make_handler()

    bound = handler._handlers.get("watch_session")
    assert bound is not None and bound.__func__ is cwh.ChatWebSocketHandler.handle_watch_session


async def test_watch_session_missing_session_id_is_noop():
    """Closure guard: no session_id => nothing happens (no register, no frame)."""
    with _HandlerHarness() as h:
        handler = h.make_handler()
        await handler.handle_watch_session({"type": "watch_session"})

    h.sync.register_device.assert_not_awaited()
    assert handler.current_session_id is None
    assert h.ws.sent == []


async def test_watch_session_restores_nonempty_queue():
    """Oracle: nonempty server-side queue is mirrored back as the same
    sync/queue_updated frame the queue_message handlers emit — and it goes
    through the class send_json, so the raw frame carries _seq."""
    with _HandlerHarness() as h:
        h.db.get_queued_messages.return_value = [{"id": "q1", "content": "later"}]
        await h.make_handler().handle_watch_session(dict(WATCH))

    assert _queue_frames(h.ws) == [{
        "type": "sync",
        "event_type": "queue_updated",
        "session_id": "sess-1",
        "data": {"queued_messages": [{"id": "q1", "content": "later"}]},
    }]
    assert h.ws.sent[0]["_seq"] == 1


async def test_watch_session_empty_queue_sends_no_frame():
    with _HandlerHarness() as h:
        await h.make_handler().handle_watch_session(dict(WATCH))

    assert _queue_frames(h.ws) == []


async def test_watch_session_queue_load_failure_still_registers_silently():
    """Closure parity: a DB failure loading the queue is swallowed — the watch
    registration stands, and neither a queue frame nor an error frame is sent."""
    with _HandlerHarness() as h:
        h.db.get_queued_messages.side_effect = RuntimeError("db down")
        handler = h.make_handler()
        await handler.handle_watch_session(dict(WATCH))

    h.sync.register_device.assert_awaited_once()
    assert handler.current_session_id == "sess-1"
    assert _queue_frames(h.ws) == []
    assert _errors(h.ws) == []


async def test_watch_session_switch_unregisters_old_session():
    """Switching sessions unregisters the device from the previous one first."""
    with _HandlerHarness() as h:
        handler = h.make_handler()
        handler.current_session_id = "sess-old"
        await handler.handle_watch_session(dict(WATCH))

    h.sync.unregister_device.assert_awaited_once()
    assert h.sync.unregister_device.await_args.args == ("dev-1", "sess-old")
    h.sync.register_device.assert_awaited_once()
    assert handler.current_session_id == "sess-1"


async def test_watch_session_rewatch_same_session_does_not_unregister():
    """Re-watching the already-current session must NOT unregister it (the
    closure only unregisters when current != new)."""
    with _HandlerHarness() as h:
        handler = h.make_handler()
        handler.current_session_id = "sess-1"
        await handler.handle_watch_session(dict(WATCH))

    h.sync.unregister_device.assert_not_awaited()
    h.sync.register_device.assert_awaited_once()


# ═══ A2. watch_session — the pinned dead-vs-live duplicate asymmetry ═══════════


async def test_watch_session_duplicate_live_ws_skips_reregistration():
    """Oracle: same device_id watching from another LIVE socket (duplicate
    tab): the watch is skipped (no re-register — re-registering would evict
    the live tab and cause a reconnect loop) but the session is still bound
    as current."""
    with _HandlerHarness() as h:
        h.sync.is_device_watching_session.return_value = True
        other_ws = SimpleNamespace(client_state=WebSocketState.CONNECTED)
        h.sync._connections = {
            "sess-1": {"dev-1": SimpleNamespace(websocket=other_ws)}
        }
        handler = h.make_handler()
        await handler.handle_watch_session(dict(WATCH))
        await handler.handle_get_pending_permissions({})

    h.sync.register_device.assert_not_awaited()
    # the skip path binds current_session_id BEFORE returning (closure parity)
    assert handler.current_session_id == "sess-1"
    pend = _frames(h.ws, "pending_permissions")
    assert pend and pend[0]["session_id"] == "sess-1"


async def test_watch_session_duplicate_live_ws_skips_queue_restore():
    """The live-duplicate skip path returns BEFORE the queue restore — the
    other (live) tab already has the queue; sending it here would go to the
    wrong socket's UI state."""
    with _HandlerHarness() as h:
        h.sync.is_device_watching_session.return_value = True
        other_ws = SimpleNamespace(client_state=WebSocketState.CONNECTED)
        h.sync._connections = {
            "sess-1": {"dev-1": SimpleNamespace(websocket=other_ws)}
        }
        h.db.get_queued_messages.return_value = [{"id": "q1", "content": "later"}]
        await h.make_handler().handle_watch_session(dict(WATCH))

    assert _queue_frames(h.ws) == []
    h.db.get_queued_messages.assert_not_called()


async def test_watch_session_dead_old_ws_is_replaced():
    """Oracle: same device_id but the old socket is dead (page refresh): the
    new socket takes over — registration proceeds."""
    with _HandlerHarness() as h:
        h.sync.is_device_watching_session.return_value = True
        other_ws = SimpleNamespace(client_state=WebSocketState.DISCONNECTED)
        h.sync._connections = {
            "sess-1": {"dev-1": SimpleNamespace(websocket=other_ws)}
        }
        await h.make_handler().handle_watch_session(dict(WATCH))

    h.sync.register_device.assert_awaited_once()


async def test_watch_session_same_ws_rewatch_proceeds():
    """Closure: when the existing registration IS this very websocket, the
    re-watch proceeds normally (registration refresh, queue restore)."""
    with _HandlerHarness() as h:
        h.sync.is_device_watching_session.return_value = True
        handler = h.make_handler()
        h.sync._connections = {
            "sess-1": {"dev-1": SimpleNamespace(websocket=h.ws)}
        }
        h.db.get_queued_messages.return_value = [{"id": "q1", "content": "later"}]
        await handler.handle_watch_session(dict(WATCH))

    h.sync.register_device.assert_awaited_once()
    assert _queue_frames(h.ws), "same-socket re-watch still restores the queue"


# NOTE (V2 A7): the headless-never-watches test is GONE — A7 deleted
# headless_query_runner.py, so no ChatWebSocketHandler is built with
# websocket=None. The ``self.websocket is None`` early-return guard in
# handle_watch_session remains in the class.


# ═══ B1. get_pending_permissions — class shape is canonical ════════════════════
#
# Plan: the closure replies with key ``requests``; the CLASS replies with
# ``pending`` + ``deferred`` and that is what the frontend expects
# (types/websocket.ts PendingPermissionsMessage, PermissionQueue.svelte).
# These pins let the flip commit consciously replace the closure shape.


async def test_get_pending_permissions_replies_pending_plus_deferred():
    with _HandlerHarness() as h:
        h.perm.get_pending_requests.return_value = [{"request_id": "p1"}]
        h.perm.get_deferred_requests.return_value = [{"request_id": "d1"}]
        handler = h.make_handler()
        handler.current_session_id = "sess-1"
        await handler.handle_get_pending_permissions({})

    h.perm.get_pending_requests.assert_called_once_with("sess-1")
    h.perm.get_deferred_requests.assert_called_once_with("sess-1")
    assert _frames(h.ws, "pending_permissions") == [{
        "type": "pending_permissions",
        "session_id": "sess-1",
        "pending": [{"request_id": "p1"}],
        "deferred": [{"request_id": "d1"}],
    }], "class shape is pending+deferred — NOT the closure's `requests` key"


async def test_get_pending_permissions_carries_deferred_even_when_empty():
    """The ``deferred`` key is always present (frontend treats a missing key
    as an older backend) — pin that an empty deferred queue still ships []."""
    with _HandlerHarness() as h:
        h.perm.get_pending_requests.return_value = [{"request_id": "p1"}]
        handler = h.make_handler()
        handler.current_session_id = "sess-1"
        await handler.handle_get_pending_permissions({})

    frame = _frames(h.ws, "pending_permissions")[0]
    assert frame["deferred"] == []


async def test_get_pending_permissions_without_session_is_silent():
    with _HandlerHarness() as h:
        await h.make_handler().handle_get_pending_permissions({})

    assert h.ws.sent == []
    h.perm.get_pending_requests.assert_not_called()


# ═══ B2. permission_response — decision="defer" (class only) ═══════════════════


async def test_permission_response_defer_strips_remember_and_pattern():
    """decision="defer" moves the request to the deferred queue; remember and
    pattern are meaningless for a defer and the class strips them to None
    before calling permission_handler.respond (closure has no defer at all)."""
    with _HandlerHarness() as h:
        h.perm.respond.return_value = {"decision": "defer"}
        await h.make_handler().handle_permission_response({
            "request_id": "r1",
            "session_id": "sess-1",
            "decision": "defer",
            "remember": "profile",
            "pattern": "Bash(*)",
        })

    kw = h.perm.respond.await_args.kwargs
    assert kw["request_id"] == "r1"
    assert kw["session_id"] == "sess-1"
    assert kw["decision"] == "defer"
    assert kw["remember"] is None, "remember must be stripped on defer"
    assert kw["pattern"] is None, "pattern must be stripped on defer"
    assert kw["broadcast_func"] is not None
    assert _frames(h.ws, "permission_response_ack") == [{
        "type": "permission_response_ack",
        "session_id": "sess-1",
        "request_id": "r1",
        "result": {"decision": "defer"},
    }]


async def test_permission_response_allow_passes_remember_and_pattern():
    """Non-defer decisions pass remember/pattern through untouched (parity
    with the closure oracle's permission_response test)."""
    with _HandlerHarness() as h:
        h.perm.respond.return_value = {"decision": "allow"}
        await h.make_handler().handle_permission_response({
            "request_id": "r1",
            "session_id": "sess-1",
            "decision": "allow",
            "remember": "session",
            "pattern": "Bash(*)",
        })

    kw = h.perm.respond.await_args.kwargs
    assert kw["decision"] == "allow"
    assert kw["remember"] == "session"
    assert kw["pattern"] == "Bash(*)"
    assert _frames(h.ws, "permission_response_ack") == [{
        "type": "permission_response_ack",
        "session_id": "sess-1",
        "request_id": "r1",
        "result": {"decision": "allow"},
    }]


async def test_permission_response_invalid_errors():
    """Missing decision => error frame, permission_handler never consulted."""
    with _HandlerHarness() as h:
        await h.make_handler().handle_permission_response({
            "request_id": "r1",
            "session_id": "sess-1",
        })

    h.perm.respond.assert_not_awaited()
    assert _errors(h.ws) == [
        {"type": "error", "message": "Invalid permission response"}
    ]


async def test_permission_response_falls_back_to_current_session():
    with _HandlerHarness() as h:
        handler = h.make_handler()
        handler.current_session_id = "sess-cur"
        await handler.handle_permission_response({
            "request_id": "r1",
            "decision": "deny",
        })

    kw = h.perm.respond.await_args.kwargs
    assert kw["session_id"] == "sess-cur"


# ═══ B3. requeue_deferred_permissions — class only, frontend sends it ══════════


async def test_requeue_deferred_permissions_frame_shape():
    """requeue moves deferred requests back to pending (re-broadcast via
    broadcast_func) and confirms with a permission_queue_update frame carrying
    the requeued ids + remaining queue count."""
    with _HandlerHarness() as h:
        h.perm.requeue_deferred.return_value = ["r1", "r2"]
        h.perm.get_queue_count.return_value = 3
        await h.make_handler().handle_requeue_deferred_permissions({
            "session_id": "sess-1",
        })

    kw = h.perm.requeue_deferred.await_args.kwargs
    assert kw["session_id"] == "sess-1"
    assert kw["broadcast_func"] is not None
    h.perm.get_queue_count.assert_called_once_with("sess-1")
    assert _frames(h.ws, "permission_queue_update") == [{
        "type": "permission_queue_update",
        "session_id": "sess-1",
        "requeued_ids": ["r1", "r2"],
        "remaining_count": 3,
    }]


async def test_requeue_deferred_falls_back_to_current_session():
    with _HandlerHarness() as h:
        handler = h.make_handler()
        handler.current_session_id = "sess-cur"
        await handler.handle_requeue_deferred_permissions({})

    kw = h.perm.requeue_deferred.await_args.kwargs
    assert kw["session_id"] == "sess-cur"


async def test_requeue_deferred_without_session_errors():
    with _HandlerHarness() as h:
        await h.make_handler().handle_requeue_deferred_permissions({})

    h.perm.requeue_deferred.assert_not_awaited()
    assert _errors(h.ws) == [
        {"type": "error", "message": "No session_id for requeue"}
    ]
