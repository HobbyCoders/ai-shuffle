"""
Headless regression pins for the route flip — Phase 6.1 STEP 6 of
docs/STREAMING_UNIFICATION_PLAN.md.

Steps 1-5 folded the closure's relay branches, ``watch_session`` and
drain-on-load into ``ChatWebSocketHandler`` — code every HEADLESS consumer
(``HeadlessChatHandler``: background /sessions/with-worktree queries, cloud
offload, cluster peer ingress) now also traverses. The per-handler
"headless never relays / never watches" pins live in
tests/api/test_chat_ws_handler_relay.py (section 8 + the source guard) and
tests/api/test_chat_ws_handler_watch.py (section A3). This file pins the
remaining seams none of those cover:

* ``launch_headless_query`` itself — task registration in
  ``_active_chat_sessions`` (so /stop + reconnect-attach see it), argument
  passthrough to ``run_query``, and cancel-before-respawn of an existing
  query for the same session.
* ``HeadlessChatHandler.__init__`` deliberately SKIPS ``super().__init__``
  and mirrors the parent's fields by hand — a field added to the parent
  during this refactor (or step 7's deletion pass) would silently break
  every headless path with an AttributeError. Pin exact field parity.
* The headless ``send_json`` contract run_query depends on: silent success
  (True) with no socket, no ``_seq`` mutation, connection stays healthy.
* Headless handlers can never reach ``handle_load_session`` /
  ``handle_watch_session`` (the drain-on-load + watcher registration added
  in steps 2-3): the dispatch table is empty AND neither headless
  entry-point module ever invokes them (source pin backing the claim in
  chat_ws_handler.py's drain-on-load comment).
* The cluster ingress seam (app/api/cluster.py cluster_queue_message →
  handle_queue_message): the REAL handler, fed the exact payload shape the
  endpoint builds, against a session the registry calls REMOTE — must store
  locally with the relayed profile/project, attempt the idle drain, and
  never consult the relay (test_cluster_queue_message.py pins the endpoint
  with a fake handler; test_chat_ws_handler_relay.py pins the handler with
  a generic payload; this closes the seam between them).

No Postgres / SDK required.
"""

import asyncio
import inspect
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import app.api.chat_ws_handler as cwh
import app.api.cluster as cluster_mod
import app.api.websocket as ws_mod
import app.core.cloud_relay as cloud_relay_mod
import app.core.headless_query_runner as hqr
from app.core.headless_query_runner import HeadlessChatHandler, launch_headless_query


# ─── harness ─────────────────────────────────────────────────────────────────


class _Harness:
    """Patches the module-global control planes launch/queue paths touch."""

    def __enter__(self):
        self._stack = ExitStack()
        s = self._stack

        self.registry: dict = {}
        self.stopped: set = set()
        s.enter_context(patch.object(ws_mod, "_active_chat_sessions", new=self.registry))
        s.enter_context(patch.object(ws_mod, "_active_sessions_lock", new=asyncio.Lock()))
        s.enter_context(patch.object(cwh, "_stopped_sessions", new=self.stopped))

        self.session_row = {"id": "sess-h", "project_id": "proj-1", "worktree_id": None}
        self.db = s.enter_context(patch.object(cwh, "database"))
        self.db.get_session = MagicMock(side_effect=lambda sid: dict(self.session_row))
        self.db.get_queued_messages = MagicMock(return_value=[])

        async def _fake_run_sync(fn, *a, **k):
            return fn(*a, **k)

        s.enter_context(patch.object(cwh, "run_sync", new=AsyncMock(side_effect=_fake_run_sync)))

        self.sync = s.enter_context(patch.object(cwh, "sync_engine"))
        self.sync.broadcast_queue_updated = AsyncMock()

        self.is_remote = s.enter_context(
            patch.object(cloud_relay_mod, "is_remote_session", MagicMock(return_value=False))
        )
        self.relay_queue_message = s.enter_context(
            patch.object(cloud_relay_mod, "relay_queue_message", AsyncMock())
        )

        return self

    def __exit__(self, *exc):
        self._stack.close()
        return False


LAUNCH_ARGS = dict(
    prompt="do the thing",
    session_id="sess-h",
    profile_id="claude-code",
    project_id="proj-1",
    user_id="admin",
    api_user_id=None,
    worktree_id="wt-9",
    overrides={"model": "opus"},
)


# ═══ 1. launch_headless_query — registration + argument passthrough ═══════════


async def test_launch_registers_task_and_passes_run_query_args():
    with _Harness() as h:
        with patch.object(HeadlessChatHandler, "run_query", new_callable=AsyncMock) as rq:
            handler = await launch_headless_query(**LAUNCH_ARGS)
            assert isinstance(handler, HeadlessChatHandler), (
                "launch returns the handler so socketless callers (automation "
                "runs) can await handler.query_task and read the sniffed turn "
                "outcome off it"
            )
            assert h.registry.get("sess-h") is handler.query_task, (
                "headless query must land in _active_chat_sessions so /stop and "
                "reconnect-attach work identically to a WS-launched query"
            )
            await handler.query_task

    rq.assert_awaited_once_with(
        prompt="do the thing",
        session_id="sess-h",
        profile_id="claude-code",
        project_id="proj-1",
        overrides={"model": "opus"},
        worktree_id="wt-9",
        deferred_delivery=False,
    )


async def test_launch_cancels_existing_query_before_respawn():
    """A second launch for the same session must cancel the in-flight task
    (no double-spawn) and take over the registry slot."""
    with _Harness() as h:
        started = asyncio.Event()

        async def _forever():
            started.set()
            await asyncio.Event().wait()

        old = asyncio.create_task(_forever())
        await started.wait()
        h.registry["sess-h"] = old

        with patch.object(HeadlessChatHandler, "run_query", new_callable=AsyncMock):
            handler = await launch_headless_query(**LAUNCH_ARGS)
            assert old.cancelled(), "pre-existing query task must be cancelled"
            assert h.registry.get("sess-h") is handler.query_task
            await handler.query_task


# ═══ 2. __init__ field parity — the subclass skips super().__init__ ═══════════


def test_headless_init_mirrors_every_parent_field():
    """HeadlessChatHandler.__init__ deliberately does NOT call the parent
    __init__ and re-creates its fields by hand. Any field added to
    ChatWebSocketHandler.__init__ (steps 1-5 of the flip, or step 7 cleanup)
    MUST be mirrored there, or every headless consumer breaks at runtime with
    an AttributeError deep inside run_query."""
    parent = cwh.ChatWebSocketHandler(
        websocket=object(),  # type: ignore[arg-type]
        device_id="dev-parity",
        user_id="admin",
        api_user_id=None,
    )
    headless = HeadlessChatHandler(user_id="admin", api_user_id=None)

    # Headless-ONLY additions (deliberate): terminal-frame capture for
    # socketless callers — run_query reports the turn outcome only via
    # direct send_json frames, so the handler sniffs done/error/interrupted
    # before the websocket-None short-circuit swallows them. Automation
    # runs read these to stamp cost/failure onto the run row.
    HEADLESS_ONLY_FIELDS = {
        "last_turn_metadata",
        "last_turn_error",
        "last_turn_interrupted",
    }

    missing = set(vars(parent)) - set(vars(headless))
    extra = set(vars(headless)) - set(vars(parent)) - HEADLESS_ONLY_FIELDS
    assert not missing, f"HeadlessChatHandler.__init__ is missing parent fields: {sorted(missing)}"
    assert not extra, f"HeadlessChatHandler.__init__ grew fields the parent lacks: {sorted(extra)}"

    assert headless.websocket is None
    assert headless.device_id.startswith("headless-")
    assert headless.connection_healthy is True


# ═══ 3. send_json headless contract — silent success, no socket, no _seq ══════


async def test_headless_send_json_silently_succeeds_without_socket():
    """run_query aborts wherever send_json returns False — headless must
    report success so the query keeps streaming into the live store, and it
    must not stamp ``_seq`` (there is no wire to sequence)."""
    handler = HeadlessChatHandler(user_id="admin", api_user_id=None)
    frame = {"type": "start", "session_id": "sess-h"}

    ok = await handler.send_json(frame)

    assert ok is True
    assert "_seq" not in frame, "no websocket — the frame must not be sequence-stamped"
    assert handler._ws_seq == 0
    assert handler.connection_healthy is True


# ═══ 4. drain-on-load / watch_session are unreachable headless ════════════════


def test_headless_dispatch_table_is_empty():
    """The headless handler never runs the WS dispatch loop; its _handlers
    table is empty, so load_session (drain-on-load, step 3) and watch_session
    (step 2) can never be dispatched on a headless instance."""
    handler = HeadlessChatHandler(user_id="admin", api_user_id=None)
    assert handler._handlers == {}


def test_headless_entry_points_never_invoke_load_or_watch():
    """Source pin backing the drain-on-load comment in chat_ws_handler.py
    ("HeadlessChatHandler never issues load_session ... headless_query_runner
    / cluster relay only reach run_query and handle_queue_message"): neither
    headless entry-point module may grow a handle_load_session /
    handle_watch_session call."""
    for mod in (hqr, cluster_mod):
        src = inspect.getsource(mod)
        assert "handle_load_session" not in src, f"{mod.__name__} must not issue load_session"
        assert "handle_watch_session" not in src, f"{mod.__name__} must not issue watch_session"

    # cluster ingress reaches the class ONLY via handle_queue_message + run_query
    cluster_src = inspect.getsource(cluster_mod)
    assert "handle_queue_message" in cluster_src
    assert "launch_headless_query" in cluster_src


# ═══ 5. cluster ingress seam — real handler, exact endpoint payload ═══════════


async def test_cluster_ingress_payload_stores_locally_and_never_relays():
    """cluster_queue_message (app/api/cluster.py) builds
    {"session_id", "content", "profile", "project"} and calls the REAL
    HeadlessChatHandler.handle_queue_message. Even when the shared-DB row
    says the session is remote (the relay loop scenario: install A already
    relayed to us, the owner), the ingress side must store the message in
    the local queue with the relayed profile/project, attempt the idle
    drain, and never relay back out."""
    with _Harness() as h:
        h.is_remote.return_value = True  # would loop the relay if the guard broke
        h.stopped.add("sess-h")  # pre-seed so the latch-clear below is observable

        handler = HeadlessChatHandler(user_id="admin", api_user_id=None)
        handler.current_session_id = "sess-h"
        with patch.object(handler, "_process_next_queued", new_callable=AsyncMock) as drain:
            await handler.handle_queue_message({
                "session_id": "sess-h",
                "content": "queued from peer",
                "profile": "claude-code",
                "project": "proj-1",
            })

    h.is_remote.assert_not_called()
    h.relay_queue_message.assert_not_awaited()
    h.db.add_queued_message.assert_called_once_with(
        session_id="sess-h",
        content="queued from peer",
        profile_id="claude-code",
        project_id="proj-1",
    )
    h.sync.broadcast_queue_updated.assert_awaited_once()
    drain.assert_awaited_once_with("sess-h")
    assert "sess-h" not in h.stopped, "queueing clears the stopped latch (local path ran)"


# ═══ 6. terminal-frame capture — the socketless turn-outcome seam ═════════════
# run_query reports the turn outcome ONLY as direct send_json frames (done /
# error / interrupted). With no socket those frames would vanish in the
# websocket-None short-circuit, so the headless handler sniffs them first —
# automation runs await handler.query_task and read the outcome off these
# fields to stamp cost/failure onto the run row.


async def test_done_frame_captures_metadata():
    handler = HeadlessChatHandler(user_id="admin", api_user_id=None)
    ok = await handler.send_json(
        {"type": "done", "metadata": {"total_cost_usd": 1.5, "tokens_in": 3}}
    )
    assert ok is True  # websocket-None short-circuit still reports success
    assert handler.last_turn_metadata == {"total_cost_usd": 1.5, "tokens_in": 3}
    assert handler.last_turn_error is None


async def test_error_frame_captures_error():
    handler = HeadlessChatHandler(user_id="admin", api_user_id=None)
    await handler.send_json({"type": "error", "message": "Error: boom"})
    assert handler.last_turn_error == "Error: boom"


async def test_successor_done_clears_prior_error():
    """A failover/auto-continue successor turn reuses the handler; its clean
    completion must win over the earlier turn's error."""
    handler = HeadlessChatHandler(user_id="admin", api_user_id=None)
    await handler.send_json({"type": "error", "message": "Error: limit"})
    await handler.send_json({"type": "done", "metadata": {"total_cost_usd": 0.2}})
    assert handler.last_turn_error is None
    assert handler.last_turn_metadata == {"total_cost_usd": 0.2}


async def test_interrupted_frame_sets_flag():
    handler = HeadlessChatHandler(user_id="admin", api_user_id=None)
    await handler.send_json({"type": "interrupted", "message": "stopped"})
    assert handler.last_turn_interrupted is True


async def test_non_terminal_frames_do_not_touch_outcome_fields():
    handler = HeadlessChatHandler(user_id="admin", api_user_id=None)
    await handler.send_json({"type": "chunk", "content": "hi"})
    assert handler.last_turn_metadata == {}
    assert handler.last_turn_error is None
    assert handler.last_turn_interrupted is False


async def test_launch_deferred_delivery_passes_through():
    """Automation runs are server-initiated turns: deferred_delivery=True must
    reach run_query (the wake-turn contract — watching devices stay registered
    so the turn streams live instead of surfacing on reload)."""
    with _Harness():
        with patch.object(HeadlessChatHandler, "run_query", new_callable=AsyncMock) as rq:
            handler = await launch_headless_query(**LAUNCH_ARGS, deferred_delivery=True)
            await handler.query_task

    assert rq.call_args.kwargs["deferred_delivery"] is True
