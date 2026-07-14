"""
Frame-by-frame characterization of the ``/ws/chat`` route — the behavioral
ORACLE for Phase 6.1 of docs/STREAMING_UNIFICATION_PLAN.md.

Written at STEP 0 against the original nested-closure implementation
(``app.api.websocket.chat_websocket``); since the STEP 5 route flip the same
transcripts drive the unified ``ChatWebSocketHandler`` through the flipped
endpoint, proving the class serves the closure's contract.

Design rules (these carried the suite across the flip):

* Every test drives the route through its PUBLIC entry point
  (``ws_mod.chat_websocket(websocket, token=..., device_id=...)``) with a fake
  WebSocket feeding inbound frames — exactly the contract the route exposes.
  The endpoint is now a thin shim (accept/auth/device_id prologue +
  ``ChatWebSocketHandler.run()``), so these transcripts exercise the class.
* Anything that reaches into implementation INTERNALS is confined to:
    - ``_ClosureHarness`` (the mock/patch surface — module-global registries
      and collaborator boundaries; each mock is mirrored into BOTH
      app.api.websocket and app.api.chat_ws_handler, because the class
      resolves its collaborators from its own module globals), and
    - the tests in the "FLIP-ADAPT" section at the bottom (dispatch-table
      inventory checks, now pointed at the handler class).
* All heavy collaborators are mocked (SDK, DB, sync engine, cloud relay);
  ``run_query`` coroutines are intercepted un-started, so no streaming
  machinery actually runs. What is asserted is the *observable* contract:
  outbound frames (exact shapes), collaborator calls, and the module-global
  registry side effects.

Closure-vs-class divergences that were PINNED pre-flip and consciously
resolved to the CLASS shape at flip time (details at each pinning test):
* every outbound frame now carries a monotonic ``_seq`` diagnostic —
  stripped AND verified strictly increasing by ``_FakeWebSocket``;
* ``session_created`` carries ``title`` (class-canonical per plan);
* ``get_pending_permissions`` replies ``pending`` + ``deferred`` (was
  ``requests``);
* the drain-on-load queue reconcile broadcasts via sync_engine only (the
  closure also direct-sent a sync/queue_updated ack) — see
  test_chat_ws_handler_queue.py for why that is equivalent at load time;
* ``history`` may carry ``queuedMessages`` (class-only key, additive).

NOT characterizable in this sandbox (feeds the live gate — see plan "Test
split"): real seed/history parity under postgres_only, real SDK streaming
(run_query internals), real interrupt/Send-now two-phase drain against a live
CLI, cross-install relay actually reaching a second install, and ping-loop /
60s-receive-timeout wall-clock behavior.
"""

import asyncio
import inspect
import json
import uuid
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.websockets import WebSocketState

import app.api.chat_ws_handler as cwh_mod
import app.api.websocket as ws_mod
import app.core.cloud_relay as cloud_relay_mod
import pytest

InstallOffline = cloud_relay_mod.InstallOffline


@pytest.fixture(autouse=True)
def _pin_legacy_turns_path(monkeypatch):
    """A6b (v2 turns cutover): pin the LEGACY (flag-off) handler path. This is
    the closure/handler oracle for query/stop/interrupt/queue/remove/clear; under
    ``AI_SHUFFLE_V2_TURNS`` (default ON) those delegate to ``turn_manager``. The
    legacy path still SHIPS behind the flag as the PR-A rollback path and is
    pinned here so the oracle stays green; the v2 turns-backed delegation is
    covered in tests/api/test_chat_ws_v2_turns.py."""
    from app.core.config import settings
    monkeypatch.setattr(settings, "v2_turns", False)


# ═══ Harness (FLIP-ADAPT: the only place allowed to know closure internals) ══


class _FakeWebSocket:
    """Minimal Starlette-WebSocket stand-in (mirrors test_busy_write_guard_ws).

    Feeds ``inbound`` frames one per ``receive_json`` and raises
    ``WebSocketDisconnect`` when exhausted; records every outbound frame
    (decoded) in ``sent`` and any server-initiated close in ``closed``.
    """

    def __init__(self, inbound):
        self._inbound = list(inbound)
        self.client_state = WebSocketState.CONNECTED
        self.sent: list = []
        self.cookies = {}
        self.accepted = False
        self.closed = None  # (code, reason)
        self._last_seq = 0

    async def accept(self):
        self.accepted = True

    async def receive_json(self):
        if self._inbound:
            return self._inbound.pop(0)
        self.client_state = WebSocketState.DISCONNECTED
        raise ws_mod.WebSocketDisconnect(code=1000)

    async def send_text(self, text: str):
        frame = json.loads(text)
        # Post-flip pin: the unified handler stamps EVERY outbound frame with a
        # monotonically increasing `_seq` diagnostic (class-canonical, plan
        # line "class-canonical behaviors"). Verify it here once, then strip it
        # so every frame-shape assertion in this file stays byte-exact.
        seq = frame.pop("_seq", None)
        assert isinstance(seq, int), f"outbound frame missing _seq: {frame}"
        assert seq > self._last_seq, (
            f"_seq must be strictly increasing: got {seq} after {self._last_seq}"
        )
        self._last_seq = seq
        self.sent.append(frame)

    async def close(self, code: int = 1000, reason: str = ""):
        self.client_state = WebSocketState.DISCONNECTED
        self.closed = (code, reason)


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
    schedules everything else for real (ping-loop teardown etc.)."""

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


class _RateStub:
    def __init__(self):
        self.allow = True
        self.calls = 0

    def check(self) -> bool:
        self.calls += 1
        return self.allow


_ASYNC_SYNC_ENGINE_METHODS = (
    "register_user_device",
    "unregister_user_device",
    "register_device",
    "unregister_device",
    "broadcast_user_session_created",
    "broadcast_session_updated",
    "broadcast_message_added",
    "broadcast_queue_updated",
    "broadcast_session_opened",
    "broadcast_session_closed",
    "broadcast_tabs_updated",
)


class _ClosureHarness:
    """Patches every boundary the /ws/chat route touches, then drives it.

    Post-flip (STEP 5): the route is a shim over ChatWebSocketHandler, which
    resolves its collaborators from app.api.chat_ws_handler module globals —
    so every mock is installed into BOTH ws_mod and cwh_mod (same objects).
    Names the class imports inline at call time (ping_loop and the registries
    from app.api.websocket, cloud_relay, interrupt_session, cost_utils,
    app.db.base.transaction) are patched at their source, which covers both.

    Exposes the mocks so tests configure inputs and assert on side effects:
      .auth            authenticate_websocket (AsyncMock -> (ok, api_user))
      .sync            sync_engine (broadcast_* are AsyncMocks)
      .has_active_run / .user_streaming_sessions
                       app.core.streaming_status (durable runs-table busy view)
      .db              app.db.database as seen by the route
      .session_row     template row db.get_session returns (None => not found)
      .cloud           cloud_relay boundary (is_remote_session + relay_*)
      .interrupt_session  app.core.query_engine.interrupt_session
      .perm / .uq      permission_handler / user_question_handler
      .load_messages_from_db  ONE input for history content (post-flip it
                       feeds a message_resolver.resolve stub returning
                       source="db", so test bodies are unchanged)
      .registry / .stopped / .interrupt_pending / .draining
                       the module-global control sets, per-test instances
      .created         the selective create_task recorder
      .rate            per-connection rate limiter stub
    """

    def __enter__(self):
        self._stack = ExitStack()
        s = self._stack

        self.created = _SelectiveCreateTask()
        self.registry: dict = {}
        self.stopped: set = set()
        self.interrupt_pending: set = set()
        self.draining: set = set()
        self.rate = _RateStub()

        self.auth = AsyncMock(return_value=(True, None))
        s.enter_context(patch.object(ws_mod, "authenticate_websocket", new=self.auth))

        async def _noop_ping(*a, **k):
            return None

        # handler.run() imports ping_loop from app.api.websocket at call time.
        s.enter_context(patch.object(ws_mod, "ping_loop", new=_noop_ping))
        _rate_factory = lambda **kw: self.rate  # noqa: E731
        s.enter_context(
            patch.object(ws_mod, "WebSocketRateLimiter", new=_rate_factory)
        )
        s.enter_context(
            patch.object(cwh_mod, "WebSocketRateLimiter", new=_rate_factory)
        )
        # ws_mod.asyncio IS the asyncio module, so this also intercepts the
        # class's create_task calls (run_query/_safety_cancel by qualname).
        s.enter_context(patch.object(ws_mod.asyncio, "create_task", new=self.created))

        # --- sync engine -----------------------------------------------------
        self.sync = s.enter_context(patch.object(ws_mod, "sync_engine"))
        s.enter_context(patch.object(cwh_mod, "sync_engine", new=self.sync))
        for name in _ASYNC_SYNC_ENGINE_METHODS:
            setattr(self.sync, name, AsyncMock())
        self.sync.get_device_count = MagicMock(return_value=1)
        self.sync.is_device_watching_session = MagicMock(return_value=False)
        self.sync._connections = {}

        # --- durable runs-table busy view (v2 core streaming truth) -----------
        # The route reads busy truth via ``app.core.streaming_status`` (module-
        # attribute access at call time), so patching the source module covers
        # every flipped site. Defaults mirror an idle runs table.
        self.has_active_run = MagicMock(return_value=False)
        s.enter_context(
            patch("app.core.streaming_status.has_active_run", new=self.has_active_run)
        )
        self.user_streaming_sessions = MagicMock(return_value=[])
        s.enter_context(
            patch(
                "app.core.streaming_status.get_user_streaming_sessions",
                new=self.user_streaming_sessions,
            )
        )

        # (V2: the live session store is deleted — history is served from the
        # persisted transcript and busy truth from app.core.streaming_status.)

        # --- database ---------------------------------------------------------
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

        self.db = s.enter_context(patch.object(ws_mod, "database"))
        s.enter_context(patch.object(cwh_mod, "database", new=self.db))
        self.db.get_session = MagicMock(side_effect=_get_session)
        self.db.get_session_by_sdk_id = MagicMock(return_value=None)
        self.db.get_queued_messages = MagicMock(return_value=[])
        self.db.pop_next_queued_message = MagicMock(return_value=None)
        self.db.get_worktree = MagicMock(return_value=None)
        self.db.get_project = MagicMock(return_value=None)
        self.db.get_git_repository = MagicMock(return_value=None)

        async def _fake_run_sync(fn, *a, **k):
            return fn(*a, **k)

        _run_sync = AsyncMock(side_effect=_fake_run_sync)
        s.enter_context(patch.object(ws_mod, "run_sync", new=_run_sync))
        s.enter_context(patch.object(cwh_mod, "run_sync", new=_run_sync))
        s.enter_context(patch.object(ws_mod, "cleanup_session_handlers", new=AsyncMock()))
        # New-session persistence runs inside a DB transaction (atomic
        # session+message, class-canonical) — stub the context manager.
        s.enter_context(patch("app.db.base.transaction", new=MagicMock()))

        # --- permission / question handlers -----------------------------------
        self.perm = s.enter_context(patch.object(ws_mod, "permission_handler"))
        s.enter_context(patch.object(cwh_mod, "permission_handler", new=self.perm))
        self.perm.get_pending_requests = MagicMock(return_value=[])
        self.perm.get_deferred_requests = MagicMock(return_value=[])
        self.perm.respond = AsyncMock(return_value={"ok": True})
        self.uq = s.enter_context(patch.object(ws_mod, "user_question_handler"))
        s.enter_context(patch.object(cwh_mod, "user_question_handler", new=self.uq))
        self.uq.get_pending_questions = MagicMock(return_value=[])
        self.uq.respond = AsyncMock(return_value=True)

        # --- cloud relay (imported inline as `from app.core import cloud_relay`)
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
        )

        # --- inline-imported helpers -------------------------------------------
        self.interrupt_session = s.enter_context(
            patch("app.core.query_engine.interrupt_session", AsyncMock(return_value=True))
        )
        self.load_messages_from_db = s.enter_context(
            patch("app.core.db_message_loader.load_messages_from_db", MagicMock(return_value=([], False)))
        )
        s.enter_context(patch("app.core.cost_utils.attach_cost_metadata", MagicMock()))
        s.enter_context(patch("app.core.cost_utils.get_cached_profile_map", MagicMock(return_value={})))

        # --- message resolution (class load path) ------------------------------
        # The unified handler loads history through message_resolver.resolve();
        # wire it through the SAME load_messages_from_db mock (source="db",
        # no usage) so test bodies keep configuring one history input.
        self.message_resolver = s.enter_context(
            patch.object(cwh_mod, "message_resolver")
        )

        def _resolve(session_id, sdk_session_id, working_dir, force_jsonl=False):
            messages, _found = self.load_messages_from_db(session_id)
            return SimpleNamespace(messages=messages, source="db", usage=None)

        self.message_resolver.resolve = MagicMock(side_effect=_resolve)
        s.enter_context(
            patch.object(cwh_mod, "resolve_working_dir", new=MagicMock(return_value="/tmp"))
        )

        # --- module-global control-plane registries ----------------------------
        # The class imports _active_chat_sessions/_active_sessions_lock from
        # app.api.websocket at call time (patching ws_mod covers it); the
        # stop/interrupt latches are bound in BOTH module namespaces at import
        # time, so mirror the same per-test set instances into cwh_mod.
        s.enter_context(patch.object(ws_mod, "_active_chat_sessions", new=self.registry))
        s.enter_context(patch.object(ws_mod, "_active_sessions_lock", new=asyncio.Lock()))
        s.enter_context(patch.object(ws_mod, "_stopped_sessions", new=self.stopped))
        s.enter_context(patch.object(cwh_mod, "_stopped_sessions", new=self.stopped))
        s.enter_context(patch.object(ws_mod, "_interrupt_pending_sessions", new=self.interrupt_pending))
        s.enter_context(patch.object(cwh_mod, "_interrupt_pending_sessions", new=self.interrupt_pending))
        s.enter_context(patch.object(ws_mod, "_draining_sessions", new=self.draining))

        return self

    def __exit__(self, *exc):
        try:
            self.created.close_all()
        finally:
            self._stack.close()
        return False

    async def run(self, frames, device_id="dev-1", token="tok"):
        """Drive the route end-to-end over the given inbound frames."""
        ws = _FakeWebSocket(frames)
        self.ws = ws
        await ws_mod.chat_websocket(ws, token=token, device_id=device_id)
        return ws


def _frames(ws, ftype):
    return [f for f in ws.sent if f.get("type") == ftype]


def _queue_frames(ws):
    return [
        f for f in ws.sent
        if f.get("type") == "sync" and f.get("event_type") == "queue_updated"
    ]


def _errors(ws):
    return _frames(ws, "error")


WATCH = {"type": "watch_session", "session_id": "sess-1"}


# ═══ A. Accept / auth prologue ════════════════════════════════════════════════


async def test_auth_failure_closes_4001_and_sends_nothing():
    """Failed auth: socket is accepted (so a close reason can be sent), then
    closed with 4001/"Authentication failed"; nothing else happens."""
    with _ClosureHarness() as h:
        h.auth.return_value = (False, None)
        ws = await h.run([])

    assert ws.accepted is True
    assert ws.closed == (4001, "Authentication failed")
    assert ws.sent == []
    h.sync.register_user_device.assert_not_awaited()


async def test_auth_success_sends_streaming_snapshot_first():
    """Successful connect ALWAYS sends the user_streaming_sessions snapshot as
    the first frame — even when empty (stale-entry reconciliation)."""
    with _ClosureHarness() as h:
        ws = await h.run([])

    assert ws.closed is None
    assert ws.sent[0] == {
        "type": "user_streaming_sessions",
        "sessions": [],
        "session_ids": [],
    }
    h.sync.register_user_device.assert_awaited_once()
    args = h.sync.register_user_device.await_args.args
    assert args[0] == "admin"  # cookie/admin auth binds the constant user id
    assert args[1] == "dev-1"


async def test_streaming_snapshot_carries_sessions_and_ids():
    with _ClosureHarness() as h:
        # Connect-time snapshot is sourced from the durable runs-table join.
        h.user_streaming_sessions.return_value = [
            {"session_id": "s1", "profile": "p"}
        ]
        ws = await h.run([])

    assert ws.sent[0] == {
        "type": "user_streaming_sessions",
        "sessions": [{"session_id": "s1", "profile": "p"}],
        "session_ids": ["s1"],
    }


async def test_missing_device_id_generated_and_api_user_binds_user_id():
    """No device_id => server mints a UUID; an API user's id becomes the
    user-level broadcast key (instead of the "admin" constant)."""
    with _ClosureHarness() as h:
        h.auth.return_value = (True, {"id": "api-7", "name": "svc"})
        await h.run(
            [{"type": "tabs_update", "tabs_data": {"open": ["a"]}}],
            device_id=None,
        )

    args = h.sync.register_user_device.await_args.args
    assert args[0] == "api-7"
    uuid.UUID(args[1])  # generated device_id must be a valid UUID
    kw = h.sync.broadcast_tabs_updated.await_args.kwargs
    assert kw["user_id"] == "api-7"
    assert kw["tabs_data"] == {"open": ["a"]}


# ═══ B. Rate limiting ═════════════════════════════════════════════════════════


async def test_rate_limited_message_gets_error_and_is_not_dispatched():
    with _ClosureHarness() as h:
        h.rate.allow = False
        ws = await h.run([WATCH])

    assert _errors(ws) == [
        {"type": "error", "error": "Rate limited: too many messages. Please slow down."}
    ]
    h.sync.register_device.assert_not_awaited()


async def test_pong_bypasses_rate_limiter():
    """pong keepalives are exempt: the limiter is never even consulted."""
    with _ClosureHarness() as h:
        h.rate.allow = False
        ws = await h.run([{"type": "pong"}])

    assert h.rate.calls == 0
    assert _errors(ws) == []


# ═══ C. query — local dispatch ════════════════════════════════════════════════


async def test_query_empty_prompt_error():
    with _ClosureHarness() as h:
        ws = await h.run([{"type": "query", "prompt": "   ", "session_id": None}])

    assert _errors(ws) == [{"type": "error", "message": "Empty prompt"}]
    assert h.created.spawns("run_query") == 0


async def test_query_new_session_mints_session_and_spawns_run_query():
    """New-session happy path: create_session (title from prompt),
    session_created frame, device registration, user message persisted +
    broadcast excluding the sender, exactly one run_query registered.

    CONSCIOUS flip edit (plan: class-canonical): session_created now carries
    ``title`` so the frontend can show it without a round-trip.

    CONSCIOUS flip edit (V2 Phase 1): session_created now also carries
    ``v2_stream`` (the flag the session was just created with) so the client
    knows whether to attach the typed event feed. The Phase-1 deletion forces
    every new chat session onto the V2 path, so this always stamps True."""
    with _ClosureHarness() as h:
        ws = await h.run([{
            "type": "query",
            "prompt": "hello there",
            "session_id": None,
            "profile": "claude-code",
            "project": None,
        }])

    ck = h.db.create_session.call_args.kwargs
    assert ck["title"] == "hello there"
    assert ck["profile_id"] == "claude-code"
    assert ck["worktree_id"] is None
    sid = ck["session_id"]

    created_frames = _frames(ws, "session_created")
    assert created_frames == [{
        "type": "session_created",
        "session_id": sid,
        "title": "hello there",
        "worktree_id": None,
        "v2_stream": True,
    }], "no request_id given => key must be OMITTED (adoption-token contract)"

    h.sync.broadcast_user_session_created.assert_awaited_once()
    h.sync.register_device.assert_awaited_once()
    assert h.sync.register_device.await_args.args[:2] == ("dev-1", sid)

    mk = h.db.add_session_message.call_args.kwargs
    assert (mk["session_id"], mk["role"], mk["content"]) == (sid, "user", "hello there")
    bk = h.sync.broadcast_message_added.await_args.kwargs
    assert bk["source_device_id"] == "dev-1", "sender must be excluded"

    assert h.created.spawns("run_query") == 1
    assert sid in h.registry, "spawned turn must be registered"


async def test_query_new_session_echoes_request_id_adoption_token():
    with _ClosureHarness() as h:
        ws = await h.run([{
            "type": "query",
            "prompt": "adopt me",
            "session_id": None,
            "request_id": "tok-123",
        }])

    frame = _frames(ws, "session_created")[0]
    assert frame["request_id"] == "tok-123"


async def test_query_existing_session_backfills_empty_title():
    """An existing session with a blank title gets a title derived from the
    prompt (update_session + broadcast_session_updated)."""
    with _ClosureHarness() as h:
        h.session_row["title"] = ""
        ws = await h.run([{
            "type": "query",
            "prompt": "derive this title",
            "session_id": "sess-1",
            "profile": "claude-code",
        }])

    uk = h.db.update_session.call_args.kwargs
    assert uk == {"session_id": "sess-1", "title": "derive this title"}
    h.sync.broadcast_session_updated.assert_awaited_once()
    assert h.created.spawns("run_query") == 1
    assert _frames(ws, "session_created") == [], "existing session mints nothing"


async def test_query_new_session_unknown_worktree_errors():
    with _ClosureHarness() as h:
        ws = await h.run([{
            "type": "query",
            "prompt": "hi",
            "session_id": None,
            "worktree_id": "wt-missing",
        }])

    assert _errors(ws) == [
        {"type": "error", "message": "Worktree not found: wt-missing"}
    ]
    h.db.create_session.assert_not_called()
    assert h.created.spawns("run_query") == 0


# ═══ D. query — cloud relay (closure-canonical; relay site 1) ═════════════════


async def test_query_remote_session_relays_and_confirms():
    """A session owned by another install: prompt is relayed, this device is
    registered for the mirror stream, cloud_forwarded is sent, and NOTHING
    runs locally (no persist, no run_query)."""
    with _ClosureHarness() as h:
        h.cloud.is_remote_session.return_value = True
        ws = await h.run([{
            "type": "query",
            "prompt": "remote hello",
            "session_id": "sess-r",
            "profile": "claude-code",
            "overrides": {"model": "opus"},
        }])

    args = h.cloud.relay_message.await_args.args
    assert args[0]["id"] == "sess-r"
    assert args[1:] == ("remote hello", None, {"model": "opus"}), \
        "the 'claude-code' sentinel must be stripped before crossing the relay"
    assert _frames(ws, "cloud_forwarded") == [
        {"type": "cloud_forwarded", "session_id": "sess-r"}
    ]
    h.sync.register_device.assert_awaited_once()
    h.db.add_session_message.assert_not_called()
    assert h.created.spawns("run_query") == 0


async def test_query_remote_offline_error_frame():
    with _ClosureHarness() as h:
        h.cloud.is_remote_session.return_value = True
        h.cloud.relay_message.side_effect = InstallOffline("worker down")
        ws = await h.run([{"type": "query", "prompt": "x", "session_id": "sess-r"}])

    assert _errors(ws) == [
        {"type": "error", "message": "Device offline — cannot send: worker down"}
    ]
    assert _frames(ws, "cloud_forwarded") == []


async def test_query_remote_failure_error_frame():
    with _ClosureHarness() as h:
        h.cloud.is_remote_session.return_value = True
        h.cloud.relay_message.side_effect = RuntimeError("boom")
        ws = await h.run([{"type": "query", "prompt": "x", "session_id": "sess-r"}])

    assert _errors(ws) == [
        {"type": "error", "message": "Could not reach owning device: boom"}
    ]


# ═══ E. stop ══════════════════════════════════════════════════════════════════


async def test_stop_local_marks_stopped_and_interrupts_sdk():
    """stop: session is latched user-stopped (suppresses post-turn queue
    drain) and the SDK-level interrupt fires. No frames are emitted."""
    with _ClosureHarness() as h:
        ws = await h.run([{"type": "stop", "session_id": "sess-1"}])

    assert "sess-1" in h.stopped
    h.interrupt_session.assert_awaited_once_with("sess-1")
    assert len(ws.sent) == 1, "stop is silent (only the connect snapshot)"
    assert h.created.spawns("_safety_cancel") == 0, "no live task => no safety timer"


async def test_stop_local_live_task_spawns_safety_cancel():
    with _ClosureHarness() as h:
        h.registry["sess-1"] = _FakeLiveTask()
        await h.run([{"type": "stop", "session_id": "sess-1"}])

    assert h.created.spawns("_safety_cancel") == 1
    assert h.registry["sess-1"].cancel_calls == 0, "never bare-cancelled inline"


async def test_stop_remote_relays_interrupt_and_skips_local_paths():
    """Relay site 2: remote stop relays the interrupt; the local SDK interrupt
    and the user-stopped latch are both skipped."""
    with _ClosureHarness() as h:
        h.cloud.is_remote_session.return_value = True
        ws = await h.run([{"type": "stop", "session_id": "sess-r"}])

    h.cloud.relay_interrupt.assert_awaited_once()
    h.interrupt_session.assert_not_awaited()
    assert h.stopped == set(), "relay branch continues BEFORE the stopped latch"
    assert len(ws.sent) == 1


async def test_stop_remote_offline_is_silent():
    with _ClosureHarness() as h:
        h.cloud.is_remote_session.return_value = True
        h.cloud.relay_interrupt.side_effect = InstallOffline("gone")
        ws = await h.run([{"type": "stop", "session_id": "sess-r"}])

    assert _errors(ws) == [], "offline stop is a silent skip — nothing to stop here"


# ═══ F. interrupt_for_query (two-phase Send-now) ══════════════════════════════


async def test_interrupt_for_query_without_session_fails():
    with _ClosureHarness() as h:
        ws = await h.run([{"type": "interrupt_for_query"}])

    assert _frames(ws, "interrupt_failed") == [{
        "type": "interrupt_failed",
        "session_id": None,
        "message": "No active session",
    }]


async def test_interrupt_for_query_local_confirms_and_clears_latch():
    with _ClosureHarness() as h:
        ws = await h.run([{"type": "interrupt_for_query", "session_id": "sess-1"}])

    h.interrupt_session.assert_awaited_once_with("sess-1")
    assert _frames(ws, "interrupt_confirmed") == [
        {"type": "interrupt_confirmed", "session_id": "sess-1"}
    ]
    assert h.interrupt_pending == set(), "pending latch must be cleared in finally"


async def test_interrupt_for_query_local_failure_sends_interrupt_failed():
    with _ClosureHarness() as h:
        h.interrupt_session.side_effect = RuntimeError("sdk broke")
        ws = await h.run([{"type": "interrupt_for_query", "session_id": "sess-1"}])

    assert _frames(ws, "interrupt_failed") == [{
        "type": "interrupt_failed",
        "session_id": "sess-1",
        "message": "sdk broke",
    }]
    assert h.interrupt_pending == set()


async def test_interrupt_for_query_remote_confirms():
    """Relay site 3: remote interrupt_for_query relays then confirms so the
    frontend proceeds to fire the (also relayed) replacement query."""
    with _ClosureHarness() as h:
        h.cloud.is_remote_session.return_value = True
        ws = await h.run([{"type": "interrupt_for_query", "session_id": "sess-r"}])

    h.cloud.relay_interrupt.assert_awaited_once()
    h.interrupt_session.assert_not_awaited()
    assert _frames(ws, "interrupt_confirmed") == [
        {"type": "interrupt_confirmed", "session_id": "sess-r"}
    ]


async def test_interrupt_for_query_remote_offline_fails():
    with _ClosureHarness() as h:
        h.cloud.is_remote_session.return_value = True
        h.cloud.relay_interrupt.side_effect = InstallOffline("gone")
        ws = await h.run([{"type": "interrupt_for_query", "session_id": "sess-r"}])

    assert _frames(ws, "interrupt_failed") == [{
        "type": "interrupt_failed",
        "session_id": "sess-r",
        "message": "Device offline: gone",
    }]


async def test_interrupt_for_query_remote_error_fails():
    with _ClosureHarness() as h:
        h.cloud.is_remote_session.return_value = True
        h.cloud.relay_interrupt.side_effect = RuntimeError("boom")
        ws = await h.run([{"type": "interrupt_for_query", "session_id": "sess-r"}])

    assert _frames(ws, "interrupt_failed") == [{
        "type": "interrupt_failed",
        "session_id": "sess-r",
        "message": "Could not reach owning device: boom",
    }]


# ═══ G. load_session ══════════════════════════════════════════════════════════


async def test_load_session_not_found():
    with _ClosureHarness() as h:
        h.session_row = None
        ws = await h.run([{"type": "load_session", "session_id": "ghost"}])

    assert _frames(ws, "session_not_found") == [
        {"type": "session_not_found", "session_id": "ghost"}
    ]
    h.sync.register_device.assert_not_awaited()


async def test_load_session_sdk_id_fallback_uses_canonical_id():
    """Loading by an SDK session UUID falls back to get_session_by_sdk_id and
    everything downstream uses the canonical AI Shuffle id."""
    with _ClosureHarness() as h:
        h.session_row = None
        h.db.get_session_by_sdk_id.return_value = {
            "id": "real-id", "sdk_session_id": "sdk-uuid", "title": "t",
            "worktree_id": None, "project_id": None,
        }
        ws = await h.run([{"type": "load_session", "session_id": "sdk-uuid"}])

    hist = _frames(ws, "history")[0]
    assert hist["session_id"] == "real-id"
    assert h.sync.register_device.await_args.args[1] == "real-id"


async def test_load_session_idle_history_frame_shape():
    """Idle session: pinned history frame — exactly type/session_id/session/
    messages/isStreaming (no streamingUsage / activeBlockType / pending* keys),
    messages from the DB-first loader, session_opened broadcast follows."""
    with _ClosureHarness() as h:
        h.load_messages_from_db.return_value = (
            [{"role": "user", "content": "hi"}], True
        )
        ws = await h.run([{"type": "load_session", "session_id": "sess-1"}])

    hist = _frames(ws, "history")[0]
    assert set(hist.keys()) == {"type", "session_id", "session", "messages", "isStreaming"}
    assert hist["session_id"] == "sess-1"
    assert hist["messages"] == [{"role": "user", "content": "hi"}]
    assert hist["isStreaming"] is False
    assert hist["session"]["id"] == "sess-1"
    h.sync.broadcast_session_opened.assert_awaited_once()
    assert h.sync.broadcast_session_opened.await_args.kwargs["is_new"] is False


async def test_load_session_drains_queued_message_on_load():
    """THE drain-on-load recovery (closure-canonical, ported into the class in
    step 3): loading an idle session with a leftover queued message pops it,
    persists it (slash-commands classified as role=command), reconciles the
    queue, and spawns the turn.

    CONSCIOUS flip edit (already pinned in test_chat_ws_handler_queue.py): the
    closure's drain ALSO direct-sent a sync/queue_updated ack on the origin
    socket; the class's _process_next_queued reconciles via
    sync_engine.broadcast_queue_updated ONLY — at load time this device was
    JUST registered by load_session, so the broadcast reaches it. The
    reconcile must still happen AFTER the history frame was delivered (the
    ordering the closure pinned)."""
    with _ClosureHarness() as h:
        h.db.pop_next_queued_message.return_value = {
            "content": "/compact now", "profile_id": None, "project_id": None,
        }
        frames_at_broadcast: list = []
        h.sync.broadcast_queue_updated.side_effect = (
            lambda *a, **k: frames_at_broadcast.append(
                [f.get("type") for f in h.ws.sent]
            )
        )
        ws = await h.run([{"type": "load_session", "session_id": "sess-1"}])

    h.db.pop_next_queued_message.assert_called_once_with("sess-1")
    mk = h.db.add_session_message.call_args.kwargs
    assert mk["role"] == "command"
    assert mk["content"] == "/compact now"
    assert mk["metadata"] == {"command_name": "/compact", "command_args": "now"}
    assert mk["profile_id"] == "claude-code"
    assert h.created.spawns("run_query") == 1
    assert "sess-1" in h.registry
    # Post-pop queue reconcile (now []) broadcast via sync_engine, AFTER the
    # history frame went out on this socket.
    assert h.sync.broadcast_queue_updated.await_args.args == ("sess-1", [])
    assert frames_at_broadcast and "history" in frames_at_broadcast[-1]
    # class model: no direct sync/queue_updated frame on the origin socket
    assert _queue_frames(ws) == []


async def test_load_session_streaming_late_join_serves_persisted_history():
    """Late-joining device on a live turn (V2): busy truth is the durable runs
    table; history is the PERSISTED transcript (the live tail arrives via the
    session event feed — the live-store message override, streamingUsage and
    activeBlockType keys are deleted with the store) — and the drain-on-load
    recovery must NOT fire while streaming."""
    with _ClosureHarness() as h:
        h.has_active_run.return_value = True  # lease-valid active run
        h.load_messages_from_db.return_value = (
            [{"role": "assistant", "content": "partial"}], True
        )
        ws = await h.run([{"type": "load_session", "session_id": "sess-1"}])

    hist = _frames(ws, "history")[0]
    assert hist["isStreaming"] is True
    assert hist["messages"] == [{"role": "assistant", "content": "partial"}]
    assert set(hist.keys()) == {
        "type", "session_id", "session", "messages", "isStreaming"
    }, "no live-store override keys (streamingUsage/activeBlockType) in V2"
    h.db.pop_next_queued_message.assert_not_called()


async def test_load_session_pending_task_gap_reports_streaming():
    """A spawned turn that has not claimed its run yet (SDK init gap): the
    _active_chat_sessions pending task still marks the session streaming.
    (The old stale-streaming broadcast_stream_end cleanup is deleted with the
    legacy maps — a stale runs-table claim self-heals via the run-lease TTL,
    so load reports busy truth as-is and never force-clears it.)"""
    with _ClosureHarness() as h:
        h.registry["sess-1"] = _FakeLiveTask()
        ws = await h.run([{"type": "load_session", "session_id": "sess-1"}])

    assert _frames(ws, "history")[0]["isStreaming"] is True
    h.db.pop_next_queued_message.assert_not_called()


async def test_load_session_includes_pending_permissions_and_questions():
    with _ClosureHarness() as h:
        h.perm.get_pending_requests.return_value = [{"request_id": "p1"}]
        h.uq.get_pending_questions.return_value = [{"id": "q1"}]
        ws = await h.run([{"type": "load_session", "session_id": "sess-1"}])

    hist = _frames(ws, "history")[0]
    assert hist["pendingPermissions"] == [{"request_id": "p1"}]
    assert hist["pendingQuestions"] == [{"id": "q1"}]


async def test_load_session_duplicate_tab_same_device_rejected():
    with _ClosureHarness() as h:
        h.sync.is_device_watching_session.return_value = True
        other_ws = SimpleNamespace(client_state=WebSocketState.CONNECTED)
        h.sync._connections = {
            "sess-1": {"dev-1": SimpleNamespace(websocket=other_ws)}
        }
        ws = await h.run([{"type": "load_session", "session_id": "sess-1"}])

    assert _frames(ws, "session_already_open") == [{
        "type": "session_already_open",
        "session_id": "sess-1",
        "message": "This session is already open in another tab on this device.",
    }]
    assert _frames(ws, "history") == []


# ═══ H. watch_session (closure-canonical; class has NO equivalent) ════════════


async def test_watch_session_registers_and_binds_current_session():
    """watch_session registers the device for sync broadcasts and binds
    current_session_id (proven via a follow-up get_pending_permissions)."""
    with _ClosureHarness() as h:
        ws = await h.run([WATCH, {"type": "get_pending_permissions"}])

    h.sync.register_device.assert_awaited_once()
    assert h.sync.register_device.await_args.args[:2] == ("dev-1", "sess-1")
    pend = _frames(ws, "pending_permissions")
    assert pend and pend[0]["session_id"] == "sess-1", (
        "watch_session must bind current_session_id"
    )


async def test_watch_session_restores_nonempty_queue():
    with _ClosureHarness() as h:
        h.db.get_queued_messages.return_value = [{"id": "q1", "content": "later"}]
        ws = await h.run([WATCH])

    assert _queue_frames(ws) == [{
        "type": "sync",
        "event_type": "queue_updated",
        "session_id": "sess-1",
        "data": {"queued_messages": [{"id": "q1", "content": "later"}]},
    }]


async def test_watch_session_empty_queue_sends_no_frame():
    with _ClosureHarness() as h:
        ws = await h.run([WATCH])

    assert _queue_frames(ws) == []


async def test_watch_session_duplicate_live_ws_skips_reregistration():
    """Same device_id watching from another LIVE socket (duplicate tab): the
    watch is skipped (no re-register, avoiding a reconnect loop) but the
    session is still bound as current."""
    with _ClosureHarness() as h:
        h.sync.is_device_watching_session.return_value = True
        other_ws = SimpleNamespace(client_state=WebSocketState.CONNECTED)
        h.sync._connections = {
            "sess-1": {"dev-1": SimpleNamespace(websocket=other_ws)}
        }
        ws = await h.run([WATCH, {"type": "get_pending_permissions"}])

    h.sync.register_device.assert_not_awaited()
    pend = _frames(ws, "pending_permissions")
    assert pend and pend[0]["session_id"] == "sess-1"


async def test_watch_session_dead_old_ws_is_replaced():
    """Same device_id but the old socket is dead (page refresh): the new
    socket takes over — registration proceeds."""
    with _ClosureHarness() as h:
        h.sync.is_device_watching_session.return_value = True
        other_ws = SimpleNamespace(client_state=WebSocketState.DISCONNECTED)
        h.sync._connections = {
            "sess-1": {"dev-1": SimpleNamespace(websocket=other_ws)}
        }
        await h.run([WATCH])

    h.sync.register_device.assert_awaited_once()


# ═══ I. close_session ═════════════════════════════════════════════════════════


async def test_close_session_unregisters_and_confirms():
    with _ClosureHarness() as h:
        ws = await h.run([
            WATCH,
            {"type": "close_session"},
            {"type": "get_pending_permissions"},
        ])

    assert _frames(ws, "session_closed") == [
        {"type": "session_closed", "session_id": "sess-1"}
    ]
    h.sync.broadcast_session_closed.assert_awaited_once()
    h.sync.unregister_device.assert_awaited_once()  # close only; no finally repeat
    assert _frames(ws, "pending_permissions") == [], (
        "current_session_id must be cleared by close_session"
    )


# ═══ J. permission_response ═══════════════════════════════════════════════════


async def test_permission_response_valid_acks_with_result():
    with _ClosureHarness() as h:
        h.perm.respond.return_value = {"decision": "allow"}
        ws = await h.run([{
            "type": "permission_response",
            "request_id": "r1",
            "session_id": "sess-1",
            "decision": "allow",
            "remember": "session",
            "pattern": "Bash(*)",
        }])

    kw = h.perm.respond.await_args.kwargs
    assert kw["request_id"] == "r1"
    assert kw["session_id"] == "sess-1"
    assert kw["decision"] == "allow"
    assert kw["remember"] == "session"
    assert kw["pattern"] == "Bash(*)"
    assert kw["broadcast_func"] is not None
    assert _frames(ws, "permission_response_ack") == [{
        "type": "permission_response_ack",
        "session_id": "sess-1",
        "request_id": "r1",
        "result": {"decision": "allow"},
    }]


async def test_permission_response_invalid_errors():
    with _ClosureHarness() as h:
        ws = await h.run([{
            "type": "permission_response",
            "request_id": "r1",
            "session_id": "sess-1",
            # decision missing
        }])

    assert _errors(ws) == [{"type": "error", "message": "Invalid permission response"}]
    h.perm.respond.assert_not_awaited()


# ═══ K. get_pending_permissions — class `pending` + `deferred` shape ══════════


async def test_get_pending_permissions_returns_pending_and_deferred():
    """CONSCIOUS flip edit: the closure replied with key ``requests``; the
    unified handler replies ``pending`` + ``deferred`` (surfacing deferred
    requests the closure silently dropped). The pre-flip pin on ``requests``
    existed exactly to force this change to be a reviewed edit — this is that
    edit (plan step 5)."""
    with _ClosureHarness() as h:
        h.perm.get_pending_requests.return_value = [{"request_id": "p1", "tool": "Bash"}]
        h.perm.get_deferred_requests.return_value = [{"request_id": "d1", "tool": "Edit"}]
        ws = await h.run([WATCH, {"type": "get_pending_permissions"}])

    assert _frames(ws, "pending_permissions") == [{
        "type": "pending_permissions",
        "session_id": "sess-1",
        "pending": [{"request_id": "p1", "tool": "Bash"}],
        "deferred": [{"request_id": "d1", "tool": "Edit"}],
    }]


async def test_get_pending_permissions_without_session_sends_nothing():
    with _ClosureHarness() as h:
        ws = await h.run([{"type": "get_pending_permissions"}])

    assert _frames(ws, "pending_permissions") == []
    assert _errors(ws) == []


# ═══ L. user_question_response ════════════════════════════════════════════════


async def test_user_question_response_local_ack_success():
    with _ClosureHarness() as h:
        ws = await h.run([{
            "type": "user_question_response",
            "request_id": "q1",
            "session_id": "sess-1",
            "answers": {"choice": "A"},
        }])

    kw = h.uq.respond.await_args.kwargs
    assert kw == {"request_id": "q1", "session_id": "sess-1", "answers": {"choice": "A"}}
    assert _frames(ws, "user_question_response_ack") == [{
        "type": "user_question_response_ack",
        "session_id": "sess-1",
        "request_id": "q1",
        "success": True,
    }], "success ack must NOT carry a reason key"


async def test_user_question_response_local_not_pending_reason():
    with _ClosureHarness() as h:
        h.uq.respond.return_value = False
        ws = await h.run([{
            "type": "user_question_response",
            "request_id": "q1",
            "session_id": "sess-1",
            "answers": {"choice": "A"},
        }])

    assert _frames(ws, "user_question_response_ack") == [{
        "type": "user_question_response_ack",
        "session_id": "sess-1",
        "request_id": "q1",
        "success": False,
        "reason": "not_pending",
    }]


async def test_user_question_response_missing_answers_errors():
    with _ClosureHarness() as h:
        ws = await h.run([{
            "type": "user_question_response",
            "request_id": "q1",
            "session_id": "sess-1",
            "answers": {},
        }])

    assert _errors(ws) == [
        {"type": "error", "message": "Invalid user question response"}
    ]


async def test_user_question_response_remote_relays():
    """Relay site 4: the pending question lives on the OWNING install; the
    answer is relayed and the local handler is never consulted."""
    with _ClosureHarness() as h:
        h.cloud.is_remote_session.return_value = True
        ws = await h.run([{
            "type": "user_question_response",
            "request_id": "q1",
            "session_id": "sess-r",
            "answers": {"choice": "B"},
        }])

    args = h.cloud.relay_question_response.await_args.args
    assert args[1:] == ("q1", {"choice": "B"})
    h.uq.respond.assert_not_awaited()
    ack = _frames(ws, "user_question_response_ack")[0]
    assert ack["success"] is True
    assert "reason" not in ack


async def test_user_question_response_remote_offline_reason():
    """Offline relay => success False + reason 'relay_offline' so the frontend
    KEEPS the modal (retryable) instead of dismissing a stale one."""
    with _ClosureHarness() as h:
        h.cloud.is_remote_session.return_value = True
        h.cloud.relay_question_response.side_effect = InstallOffline("gone")
        ws = await h.run([{
            "type": "user_question_response",
            "request_id": "q1",
            "session_id": "sess-r",
            "answers": {"choice": "B"},
        }])

    assert _frames(ws, "user_question_response_ack") == [{
        "type": "user_question_response_ack",
        "session_id": "sess-r",
        "request_id": "q1",
        "success": False,
        "reason": "relay_offline",
    }]


# ═══ M. get_pending_questions ═════════════════════════════════════════════════


async def test_get_pending_questions_local_shape_no_stale():
    with _ClosureHarness() as h:
        h.uq.get_pending_questions.return_value = [{"id": "q1"}]
        ws = await h.run([WATCH, {"type": "get_pending_questions"}])

    assert _frames(ws, "pending_questions") == [{
        "type": "pending_questions",
        "session_id": "sess-1",
        "questions": [{"id": "q1"}],
    }], "local reply must not carry the stale marker"


async def test_get_pending_questions_remote_success_is_authoritative():
    """Relay site 5: a SUCCESSFUL relay (even empty) replaces the local set
    and omits `stale` so old clients keep clearing stale modals."""
    with _ClosureHarness() as h:
        h.uq.get_pending_questions.return_value = [{"id": "local-only"}]
        h.cloud.is_remote_session.return_value = True
        h.cloud.relay_get_pending_questions.return_value = []
        ws = await h.run([WATCH, {"type": "get_pending_questions"}])

    frame = _frames(ws, "pending_questions")[0]
    assert frame["questions"] == []
    assert "stale" not in frame


async def test_get_pending_questions_remote_offline_marks_stale():
    """FAILED relay keeps the local (non-authoritative) set and flags the
    reply stale so the frontend leaves an open modal untouched."""
    with _ClosureHarness() as h:
        h.uq.get_pending_questions.return_value = [{"id": "local-q"}]
        h.cloud.is_remote_session.return_value = True
        h.cloud.relay_get_pending_questions.side_effect = InstallOffline("gone")
        ws = await h.run([WATCH, {"type": "get_pending_questions"}])

    assert _frames(ws, "pending_questions") == [{
        "type": "pending_questions",
        "session_id": "sess-1",
        "questions": [{"id": "local-q"}],
        "stale": True,
    }]


# ═══ N. pong ══════════════════════════════════════════════════════════════════


async def test_pong_is_a_noop():
    with _ClosureHarness() as h:
        ws = await h.run([{"type": "pong"}])

    assert len(ws.sent) == 1, "only the connect snapshot — pong produces nothing"


# ═══ O. queue_message ═════════════════════════════════════════════════════════


async def test_queue_message_empty_content_errors():
    with _ClosureHarness() as h:
        ws = await h.run([{"type": "queue_message", "content": "  ", "session_id": "sess-1"}])

    assert _errors(ws) == [{"type": "error", "message": "Empty queue content"}]
    h.db.add_queued_message.assert_not_called()


async def test_queue_message_without_session_errors():
    with _ClosureHarness() as h:
        ws = await h.run([{"type": "queue_message", "content": "later"}])

    assert _errors(ws) == [{"type": "error", "message": "No session for queue"}]


async def test_queue_message_local_persists_broadcasts_and_drains_idle():
    """Local queue_message: persisted, broadcast + direct queue_updated ack,
    and (idle session) the drain fires immediately."""
    with _ClosureHarness() as h:
        h.db.get_queued_messages.return_value = [{"id": "q1", "content": "later"}]
        ws = await h.run([{
            "type": "queue_message",
            "content": "later",
            "session_id": "sess-1",
            "profile": "claude-code",
            "project": None,
        }])

    qk = h.db.add_queued_message.call_args.kwargs
    assert qk == {
        "session_id": "sess-1",
        "content": "later",
        "profile_id": "claude-code",
        "project_id": None,
    }
    h.sync.broadcast_queue_updated.assert_awaited()
    assert _queue_frames(ws)[0] == {
        "type": "sync",
        "event_type": "queue_updated",
        "session_id": "sess-1",
        "data": {"queued_messages": [{"id": "q1", "content": "later"}]},
    }
    h.db.pop_next_queued_message.assert_called_once_with("sess-1")


async def test_queue_message_busy_session_skips_drain():
    with _ClosureHarness() as h:
        h.registry["sess-1"] = _FakeLiveTask()
        ws = await h.run([{
            "type": "queue_message", "content": "later", "session_id": "sess-1",
        }])

    h.db.add_queued_message.assert_called_once()
    assert _queue_frames(ws), "queue ack still sent"
    h.db.pop_next_queued_message.assert_not_called()
    assert h.registry["sess-1"].cancel_calls == 0


async def test_queue_message_remote_relays_and_reads_back():
    """Relay site 6: remote queue_message relays the queue op (drain happens
    on the OWNER), never persists locally, and reads the shared-DB queue back
    for this device's UI."""
    with _ClosureHarness() as h:
        h.cloud.is_remote_session.return_value = True
        h.db.get_queued_messages.return_value = [{"id": "qr", "content": "later"}]
        ws = await h.run([{
            "type": "queue_message",
            "content": "later",
            "session_id": "sess-r",
            "profile": "claude-code",
            "project": "proj-1",
        }])

    args = h.cloud.relay_queue_message.await_args.args
    assert args[1:] == ("later", None, "proj-1"), \
        "the 'claude-code' sentinel must be stripped before crossing the relay"
    h.db.add_queued_message.assert_not_called()
    h.db.pop_next_queued_message.assert_not_called()
    assert _queue_frames(ws) == [{
        "type": "sync",
        "event_type": "queue_updated",
        "session_id": "sess-r",
        "data": {"queued_messages": [{"id": "qr", "content": "later"}]},
    }]
    assert _errors(ws) == []


async def test_queue_message_remote_offline_error_after_queue_frame():
    """Offline relay: the queue is STILL read back (reconciling away the
    frontend's optimistic entry) and THEN an error frame follows."""
    with _ClosureHarness() as h:
        h.cloud.is_remote_session.return_value = True
        h.cloud.relay_queue_message.side_effect = InstallOffline("np")
        ws = await h.run([{
            "type": "queue_message", "content": "later", "session_id": "sess-r",
        }])

    assert _queue_frames(ws), "read-back frame must still be sent"
    assert _errors(ws) == [
        {"type": "error", "message": "Device offline — cannot queue: np"}
    ]
    types = [(f.get("type"), f.get("event_type")) for f in ws.sent]
    assert types.index(("sync", "queue_updated")) < types.index(("error", None))


# ═══ P. remove / clear queued messages ════════════════════════════════════════


async def test_remove_queued_message():
    with _ClosureHarness() as h:
        h.db.get_queued_messages.return_value = [{"id": "rest"}]
        ws = await h.run([{
            "type": "remove_queued_message",
            "message_id": "m1",
            "session_id": "sess-1",
        }])

    assert h.db.remove_queued_message.call_args.args == ("m1",)
    h.sync.broadcast_queue_updated.assert_awaited_once()
    assert _queue_frames(ws) == [{
        "type": "sync",
        "event_type": "queue_updated",
        "session_id": "sess-1",
        "data": {"queued_messages": [{"id": "rest"}]},
    }]


async def test_clear_queued_messages():
    with _ClosureHarness() as h:
        ws = await h.run([{"type": "clear_queued_messages", "session_id": "sess-1"}])

    assert h.db.clear_queued_messages.call_args.args == ("sess-1",)
    h.sync.broadcast_queue_updated.assert_awaited_once_with("sess-1", [])
    assert _queue_frames(ws) == [{
        "type": "sync",
        "event_type": "queue_updated",
        "session_id": "sess-1",
        "data": {"queued_messages": []},
    }]


# ═══ Q. tabs_update ═══════════════════════════════════════════════════════════


async def test_tabs_update_broadcasts_to_user_devices():
    with _ClosureHarness() as h:
        ws = await h.run([{"type": "tabs_update", "tabs_data": {"tabs": [1, 2]}}])

    kw = h.sync.broadcast_tabs_updated.await_args.kwargs
    assert kw == {
        "user_id": "admin",
        "tabs_data": {"tabs": [1, 2]},
        "source_device_id": "dev-1",
    }
    assert len(ws.sent) == 1, "tabs_update sends nothing back on this socket"


# ═══ R. unknown types + inventory (FLIP-ADAPT: source-level checks) ═══════════


async def test_unknown_type_is_ignored_and_loop_continues():
    with _ClosureHarness() as h:
        ws = await h.run([{"type": "definitely_not_a_thing"}, WATCH])

    assert _errors(ws) == [], "unknown types are silently ignored"
    # loop must keep dispatching after the unknown frame
    h.sync.register_device.assert_awaited_once()


# The parity checklist: every inbound type the pre-flip closure dispatched.
# The unified handler must cover ALL of these (plus the class-only types:
# requeue_deferred_permissions, permission_response's "defer" decision).
CLOSURE_INBOUND_TYPES = [
    "query",
    "stop",
    "interrupt_for_query",
    "load_session",
    "watch_session",
    "close_session",
    "permission_response",
    "get_pending_permissions",
    "user_question_response",
    "get_pending_questions",
    "pong",
    "queue_message",
    "remove_queued_message",
    "clear_queued_messages",
    "tabs_update",
]


def _bare_handler():
    """A directly-constructed handler (no I/O) for dispatch-table inventory."""
    return cwh_mod.ChatWebSocketHandler(
        websocket=None,  # type: ignore[arg-type]  # headless-style, inventory only
        device_id="inventory",
        user_id="admin",
        api_user_id=None,
    )


def test_unified_handler_inbound_type_inventory():
    """FLIP-ADAPT (adapted at flip time, per the original pin's instruction):
    the unified handler's dispatch table must remain a SUPERSET of every
    inbound type the closure handled, with exactly the known class-only
    addition. If a type is added/removed this fails, forcing the parity
    checklist above — and the per-type characterization coverage in this
    file — to be updated in the same commit."""
    handlers = _bare_handler()._handlers
    assert set(handlers) >= set(CLOSURE_INBOUND_TYPES)
    assert set(handlers) - set(CLOSURE_INBOUND_TYPES) == {
        "requeue_deferred_permissions"
    }


def test_unified_handler_relay_sites_inventory():
    """FLIP-ADAPT (adapted at flip time): exactly eight msg types consult
    is_remote_session — the six cloud/cluster relay sites ported into the class
    in step 4, PLUS the two permission relays (permission_response,
    get_pending_permissions) that closed the previously-deferred gap. Guards
    against a relay branch being added/dropped without the parity plan
    noticing."""
    handlers = _bare_handler()._handlers
    relayed = {
        msg_type
        for msg_type, method in handlers.items()
        if "is_remote_session" in inspect.getsource(method)
    }
    assert relayed == {
        "query",
        "stop",
        "interrupt_for_query",
        "user_question_response",
        "get_pending_questions",
        "queue_message",
        "permission_response",
        "get_pending_permissions",
    }
