"""
Busy-write guard — engine backstop (prod-readiness Phase 2.7).

A pooled SDK client that is mid-turn must NEVER get a second prompt written
into its stdin: the CLI dies with "Fatal error in message reader" (exit 1),
the whole in-flight turn is lost (messages persist only at turn end), and
in-process subagents die with it.

These tests pin the three reuse gates:

- ``stream_to_websocket`` (the chat path) refuses a busy pooled client with a
  clear error event and never calls ``client.query()``.
- ``stream_query`` (REST) does the same.
- ``_run_background_query`` refuses and surfaces the error on the v2 session
  event log (a ``turn.aborted`` terminal — its only error surface since the
  live-store/sync-engine broadcasts were deleted in the V2 Phase-1 cutover).

And the claim semantics:

- ``is_streaming`` is SET at the reuse-decision point (before the first await
  after the decision), so two near-simultaneous entrants can't both pass —
  exactly one proceeds, the other fails safe.
- ``is_streaming`` is cleared on every exit path (exception, cancel).

No Postgres or real SDK client is required — every boundary is mocked,
mirroring tests/core/test_interrupt_send_now.py.
"""

import asyncio
import gc
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import anyio
import pytest

import app.core.query_engine as qe
import app.core.query_background as qb
from app.core.provider import session_admission
from app.core.provider.harness_adapter import HarnessAdapter
from app.core.provider.events import AssistantMessage, TextBlock
from app.core.provider.session_admission import SessionBusyError, SessionReplacedError
from app.core.session_pool import SessionState
from app.db import session_events as se
from app.db import sessions as sessions_db
from tests.core.engine_harness import StubPool, collect, mock_adapter


# ─── helpers ────────────────────────────────────────────────────────────────


def _fake_state(**overrides) -> SessionState:
    adapter = mock_adapter()

    async def _empty_response():
        if False:  # pragma: no cover — empty async generator
            yield None

    adapter.receive_events = MagicMock(side_effect=lambda: _empty_response())
    state = SessionState(adapter=adapter, is_connected=True)
    for key, value in overrides.items():
        setattr(state, key, value)
    return state


def _chat_patches(stack: ExitStack, pool: StubPool):
    """Patch every stream_to_websocket boundary up to (and past) the reuse gate."""
    stack.enter_context(patch.object(
        qe, "get_profile", return_value={"id": "claude-code", "config": {}}
    ))

    async def _fake_run_sync(fn, *a, **k):
        return {"id": "row-1"}

    stack.enter_context(patch.object(qe, "run_sync", new=AsyncMock(side_effect=_fake_run_sync)))
    options = MagicMock()
    options.include_partial_messages = False
    options.cwd = None
    stack.enter_context(patch("app.core.sdk_options.build_options_from_profile", return_value=(options, {})
    ))
    stack.enter_context(patch.object(qe, "get_session_pool", return_value=pool))
    stack.enter_context(patch.object(qe, "is_subprocess_alive", return_value=True))
    stack.enter_context(patch.object(qe, "requires_reconnect", return_value=False))
    stack.enter_context(patch.object(qe, "mark_subprocess_oom_killable", new=MagicMock()))
    stack.enter_context(patch.object(qe, "resolve_note_references", side_effect=lambda p: p))
    stack.enter_context(patch.object(qe, "_persist_turn_results", new=AsyncMock()))
    stack.enter_context(patch.object(qe, "push_memory", new=AsyncMock()))
    stack.enter_context(patch.object(qe, "sync_engine", new=_fake_sync_engine()))
    return options


def _fake_sync_engine():
    # V2: the broadcast_stream_* seams are deleted with the streaming
    # broadcast path — sync_engine's surviving surface here is message-added.
    sync = MagicMock()
    sync.broadcast_message_added = AsyncMock()
    return sync


def _claim_window_state(set_model_side_effect=None) -> SessionState:
    """A reusable pooled state whose spawn model differs from this turn's.

    That makes the post-claim ``adapter.set_model`` await the FIRST suspension
    point after the reuse-decision claim — the observation (and failure
    injection) point for the claim-window tests below.
    """
    state = _fake_state()
    # aih_provider_id must be a REAL value on both sides: the set_model hot-swap
    # is gated on "provider unchanged" (PA13), and two bare MagicMock attributes
    # never compare equal — which would silently skip the swap under test.
    state.connect_options = MagicMock(
        permission_mode="default", model="old-model", aih_provider_id=None
    )
    state.adapter.set_permission_mode = AsyncMock()
    state.adapter.set_model = AsyncMock(side_effect=set_model_side_effect)
    return state


def _claim_window_options(options) -> None:
    """Point the turn's options at a different model (and same perm mode)."""
    options.model = "new-model"
    options.permission_mode = "default"
    options.aih_provider_id = None  # same provider → the model change hot-swaps


async def test_native_chat_admission_passes_its_lane_to_eviction(tmp_path):
    from app.core.session_pool import SessionPool
    pool = StubPool()
    busy_pool = SessionPool(max_size=1)
    busy_pool.put("busy-one", _fake_state(is_streaming=True))
    busy_pool.put("busy-two", _fake_state(is_streaming=True))
    lanes = []
    async def evict(lane):
        lanes.append(lane)
        return await busy_pool.evict_if_needed(lane)
    pool.evict_if_needed = evict
    with ExitStack() as stack:
        options = _chat_patches(stack, pool)
        options.cwd = str(tmp_path / "missing-cwd-stops-before-connect")
        stack.enter_context(patch.object(qe, "memory_leg_for_options", return_value="native"))
        # The missing CWD stops at the next boundary, before adapter creation.
        stack.enter_context(patch("app.core.hook_service.attach_hooks_to_options", return_value=None))
        await collect(qe._stream_to_websocket_impl("fixture", "new-native", "claude-code"))
    assert lanes == ["native"]
    assert "busy-one" in busy_pool and "busy-two" in busy_pool


# ─── stream_to_websocket (chat gate) ────────────────────────────────────────


async def test_chat_gate_refuses_busy_client_and_never_writes_stdin():
    """A query for a session whose pooled client is mid-turn yields a busy
    error and returns — client.query() is never called on the busy client."""
    state = _fake_state(is_streaming=True)
    pool = StubPool(state)

    with ExitStack() as stack:
        _chat_patches(stack, pool)
        events = await collect(qe._stream_to_websocket_impl("hi", "sess-busy", "claude-code"))

    assert any(e.get("type") == "error" and "busy" in e.get("message", "").lower()
               for e in events), events
    state.adapter.send_query.assert_not_awaited()
    # The rejection must not clear the ACTIVE turn's flag.
    assert state.is_streaming is True


async def test_chat_gate_uses_current_pool_entry_after_async_setup():
    old_state = _fake_state()
    current_state = _fake_state()
    pool = StubPool(old_state)
    setup_started = asyncio.Event()
    finish_setup = asyncio.Event()

    async def _slow_checkout(*args, **kwargs):
        setup_started.set()
        await finish_setup.wait()

    with ExitStack() as stack:
        _chat_patches(stack, pool)
        # Any await between the pool read and the reuse decision will do;
        # the pending-checkout wait is one.
        stack.enter_context(patch("app.core.query_options.ensure_pending_checkout", new=_slow_checkout))
        query = asyncio.create_task(collect(qe._stream_to_websocket_impl(
            "hi", "sess-replaced", "claude-code",
            overrides={"provider_id": "provider-a"},
        )))
        try:
            await asyncio.wait_for(setup_started.wait(), timeout=10)
            pool.state = current_state
        finally:
            finish_setup.set()
            events = await asyncio.wait_for(query, timeout=10)

    old_state.adapter.send_query.assert_not_awaited()
    current_state.adapter.send_query.assert_awaited_once()
    assert any(e.get("type") == "done" for e in events), events


async def test_chat_gate_claims_is_streaming_at_decision_point():
    """is_streaming is set BEFORE the first await after the reuse decision
    (previously it was set ~350 lines later, letting two entrants both pass)."""
    observed = []

    async def _observe(_model):
        observed.append(state.is_streaming)

    state = _claim_window_state(set_model_side_effect=_observe)
    state.is_streaming = False
    pool = StubPool(state)

    with ExitStack() as stack:
        options = _chat_patches(stack, pool)
        _claim_window_options(options)
        await collect(qe._stream_to_websocket_impl("hi", "sess-claim", "claude-code"))

    assert observed == [True], (
        "is_streaming must already be claimed when the first await after the "
        f"reuse decision runs — observed {observed}"
    )


async def test_chat_gate_two_concurrent_entrants_exactly_one_proceeds():
    """Entrant A holds the client mid-turn; entrant B arriving concurrently is
    rejected with a busy error and never writes into A's stdin."""
    state = _fake_state()
    pool = StubPool(state)
    a_blocked = asyncio.Event()
    a_release = asyncio.Event()

    async def _blocking_query(prompt):
        a_blocked.set()
        await a_release.wait()

    state.adapter.send_query = AsyncMock(side_effect=_blocking_query)

    with ExitStack() as stack:
        _chat_patches(stack, pool)

        a_events: list = []

        async def _consume_a():
            async for ev in qe._stream_to_websocket_impl("first", "sess-race", "claude-code"):
                a_events.append(ev)

        task_a = asyncio.create_task(_consume_a())
        await asyncio.wait_for(a_blocked.wait(), timeout=5.0)
        assert state.is_streaming is True

        # B arrives while A is mid-turn.
        b_events = await collect(qe._stream_to_websocket_impl("second", "sess-race", "claude-code"))

        a_release.set()
        await asyncio.wait_for(task_a, timeout=5.0)

    assert any(e.get("type") == "error" and "busy" in e.get("message", "").lower()
               for e in b_events), b_events
    # Exactly one prompt was ever written into the CLI's stdin.
    assert state.adapter.send_query.await_count == 1
    # A's turn completed normally and released the claim.
    assert any(e.get("type") == "done" for e in a_events), a_events
    assert state.is_streaming is False


async def test_chat_is_streaming_cleared_on_exception():
    state = _fake_state()
    pool = StubPool(state)
    state.adapter.send_query = AsyncMock(side_effect=RuntimeError("boom"))

    with ExitStack() as stack:
        _chat_patches(stack, pool)
        events = await collect(qe._stream_to_websocket_impl("hi", "sess-exc", "claude-code"))

    assert any(e.get("type") == "error" for e in events), events
    assert state.is_streaming is False


async def test_chat_is_streaming_cleared_on_cancel():
    state = _fake_state()
    pool = StubPool(state)

    async def _cancelled_response():
        raise asyncio.CancelledError()
        yield None  # pragma: no cover

    state.adapter.receive_events = MagicMock(
        side_effect=lambda: _cancelled_response()
    )

    with ExitStack() as stack:
        _chat_patches(stack, pool)
        events = await collect(qe._stream_to_websocket_impl("hi", "sess-cancel", "claude-code"))

    assert any(e.get("type") == "interrupted" for e in events), events
    assert state.is_streaming is False


# ─── stream_query (REST gate) ───────────────────────────────────────────────


def _rest_patches(stack: ExitStack, pool: StubPool):
    stack.enter_context(patch.object(
        qe, "get_profile", return_value={"id": "claude-code", "config": {}}
    ))

    async def _fake_run_sync(fn, *a, **k):
        return {"id": "row-1"}

    stack.enter_context(patch.object(qe, "run_sync", new=AsyncMock(side_effect=_fake_run_sync)))
    options = MagicMock()
    options.cwd = None
    stack.enter_context(patch("app.core.sdk_options.build_options_from_profile", return_value=(options, {})
    ))
    stack.enter_context(patch.object(qe, "get_session_pool", return_value=pool))
    stack.enter_context(patch.object(qe, "is_subprocess_alive", return_value=True))
    stack.enter_context(patch.object(qe, "requires_reconnect", return_value=False))
    sync = _fake_sync_engine()
    stack.enter_context(patch.object(qe, "sync_engine", new=sync))
    return options


async def test_rest_gate_refuses_busy_client_and_never_writes_stdin():
    state = _fake_state(is_streaming=True)
    pool = StubPool(state)

    with ExitStack() as stack:
        _rest_patches(stack, pool)
        events = await collect(qe.stream_query("hi", "claude-code", session_id="sess-busy"))

    assert any(e.get("type") == "error" and "busy" in e.get("message", "").lower()
               for e in events), events
    state.adapter.send_query.assert_not_awaited()
    assert state.is_streaming is True  # active turn's claim untouched


# ─── _run_background_query gate ─────────────────────────────────────────────


async def test_background_gate_refuses_busy_client_and_never_writes_stdin(mock_db):
    state = _fake_state(is_streaming=True)
    pool = StubPool(state)
    # v2 session — the event log is the background path's only error surface.
    sessions_db.create_session("sess-busy", "default")

    sync = _fake_sync_engine()

    async def _fake_run_sync(fn, *a, **k):
        return {"id": "row-1"}

    with ExitStack() as stack:
        stack.enter_context(patch.object(qb, "run_sync", new=AsyncMock(side_effect=_fake_run_sync)))
        stack.enter_context(patch.object(qb, "sync_engine", new=sync))
        options = MagicMock()
        stack.enter_context(patch("app.core.sdk_options.build_options_from_profile", return_value=(options, {})
        ))
        stack.enter_context(patch.object(qb, "get_session_pool", return_value=pool))
        stack.enter_context(patch.object(qb, "is_subprocess_alive", return_value=True))
        stack.enter_context(patch.object(qb, "requires_reconnect", return_value=False))

        await qb._run_background_query(
            "sess-busy", "hello", {"id": "claude-code"}, None, None, None, None, None,
        )

    state.adapter.send_query.assert_not_awaited()
    assert state.is_streaming is True  # active turn's claim untouched
    # The failure is surfaced on the session event log: the refused turn
    # recorded its start + user message, then a turn.aborted terminal.
    rows = se.get_events("sess-busy")
    types = [r["event_type"] for r in rows]
    assert types == ["turn.started", "message.user", "turn.aborted"], types
    aborted = rows[-1]["payload"]
    assert aborted["reason"] == "error"
    assert "busy" in (aborted.get("message") or "").lower()


# ─── Claim-to-try windows: a failure between the claim and the main try must ─
# ─── clear is_streaming, or the pooled client is bricked forever (busy error ─
# ─── on every future query; evict_idle skips streaming states).              ─


async def test_chat_claim_window_exception_clears_flag():
    """stream_to_websocket: a BaseException raised AFTER the decision-point claim
    but BEFORE the main try/finally (here: the post-claim set_model await) must
    clear is_streaming and propagate — never strand the claim. KeyboardInterrupt
    stands in for the non-Exception class the guard exists for: the setter's own
    ``except Exception`` swallows ordinary errors, so only a BaseException can
    still reach — and must be released by — the claim guard."""
    state = _claim_window_state(set_model_side_effect=KeyboardInterrupt("claim boom"))
    pool = StubPool(state)

    with ExitStack() as stack:
        options = _chat_patches(stack, pool)
        _claim_window_options(options)
        with pytest.raises(KeyboardInterrupt):
            await collect(qe._stream_to_websocket_impl("hi", "sess-window", "claude-code"))

    assert state.is_streaming is False, "claim must be released on claim-window failure"
    assert state.streaming_started_at is None
    state.adapter.send_query.assert_not_awaited()


async def test_chat_claim_window_cancel_clears_flag():
    """Same window, CancelledError (except Exception would MISS it — the guard
    must be BaseException-wide): the claim is released and the cancel propagates."""
    state = _claim_window_state(set_model_side_effect=asyncio.CancelledError())
    pool = StubPool(state)

    with ExitStack() as stack:
        options = _chat_patches(stack, pool)
        _claim_window_options(options)
        with pytest.raises(asyncio.CancelledError):
            await collect(qe._stream_to_websocket_impl("hi", "sess-window", "claude-code"))

    assert state.is_streaming is False, "claim must be released on cancellation"
    assert state.streaming_started_at is None
    state.adapter.send_query.assert_not_awaited()


async def test_rest_claim_window_exception_clears_flag():
    """stream_query: a failure in the post-claim window (V2: the stream_start
    sync-log write is the first await after the 'Mark as streaming' claim — the
    old broadcast_stream_start seam is deleted) clears the flag and propagates
    as before."""
    state = _fake_state()
    pool = StubPool(state)

    async def _run_sync_boom(fn, *a, **k):
        if k.get("event_type") == "stream_start":
            raise RuntimeError("sync-log boom")
        return {"id": "row-1"}

    with ExitStack() as stack:
        _rest_patches(stack, pool)
        stack.enter_context(
            patch.object(qe, "run_sync", new=AsyncMock(side_effect=_run_sync_boom))
        )
        with pytest.raises(RuntimeError, match="sync-log boom"):
            await collect(qe.stream_query("hi", "claude-code", session_id="sess-window"))

    assert state.is_streaming is False, "claim must be released on claim-window failure"
    assert state.streaming_started_at is None
    state.adapter.send_query.assert_not_awaited()


async def test_background_claim_window_exception_clears_flag():
    """_run_background_query: a failure between the reuse-decision claim and the
    main try (here: the post-claim set_model await) clears the flag and
    propagates — the pooled client stays reusable."""
    state = _claim_window_state(set_model_side_effect=KeyboardInterrupt("claim boom"))
    pool = StubPool(state)

    sync = _fake_sync_engine()

    async def _fake_run_sync(fn, *a, **k):
        return {"id": "row-1"}

    with ExitStack() as stack:
        stack.enter_context(patch.object(qb, "run_sync", new=AsyncMock(side_effect=_fake_run_sync)))
        stack.enter_context(patch.object(qb, "sync_engine", new=sync))
        options = MagicMock()
        _claim_window_options(options)
        stack.enter_context(patch("app.core.sdk_options.build_options_from_profile", return_value=(options, {})
        ))
        stack.enter_context(patch.object(qb, "get_session_pool", return_value=pool))
        stack.enter_context(patch.object(qb, "is_subprocess_alive", return_value=True))
        stack.enter_context(patch.object(qb, "requires_reconnect", return_value=False))

        with pytest.raises(KeyboardInterrupt):
            await qb._run_background_query(
                "sess-window", "hello", {"id": "claude-code"}, None, None, None, None, None,
            )

    assert state.is_streaming is False, "claim must be released on claim-window failure"
    assert state.streaming_started_at is None
    state.adapter.send_query.assert_not_awaited()


# ─── Mid-session permission_mode switch is applied in place on pooled reuse ───
# ─── (issue #608). requires_reconnect() keeps permission_mode off the respawn ─
# ─── list, so the change must be pushed into the live subprocess via           ─
# ─── client.set_permission_mode() — the callback_refs update alone only gates  ─
# ─── the app-level can_use_tool callback, not the CLI's internal auto-approval. ─


def _reuse_state_with_opts(perm_mode="default", model="m") -> SessionState:
    """A reusable pooled state whose spawn snapshot has the given perm_mode/model,
    with the SDK-client runtime setters stubbed as AsyncMocks."""
    state = _fake_state()
    state.connect_options = MagicMock(permission_mode=perm_mode, model=model)
    state.adapter.set_permission_mode = AsyncMock()
    state.adapter.set_model = AsyncMock()
    return state


def _bind_session_permission(stack: ExitStack, permission_mode: str) -> None:
    """Serve the session row with this stored permission binding.

    The WS path resolves the turn's mode from the session's current binding
    (``session_modes.turn_permission_mode``), not from the built options, and
    reconciles the live client with it just before the send.
    """
    from app.db import database

    async def _fake_run_sync(fn, *a, **k):
        if fn == database.get_session:
            return {"id": "row-1", "permission_mode": permission_mode}
        return {"id": "row-1"}

    stack.enter_context(patch.object(qe, "run_sync", new=AsyncMock(side_effect=_fake_run_sync)))


async def test_chat_applies_permission_mode_on_reuse_without_respawn():
    """WS path: a reused client whose spawn permission_mode differs from the
    session's current binding gets set_permission_mode(new) awaited, and the
    pool entry is NOT torn down (no respawn — that would kill in-flight
    background tasks)."""
    state = _reuse_state_with_opts(perm_mode="default", model="m")
    pool = StubPool(state)

    with ExitStack() as stack:
        options = _chat_patches(stack, pool)
        _bind_session_permission(stack, "acceptEdits")
        options.model = "m"  # unchanged → set_model must NOT fire
        events = await collect(qe._stream_to_websocket_impl("hi", "sess-perm", "claude-code"))

    state.adapter.set_permission_mode.assert_awaited_once_with("acceptEdits")
    state.adapter.set_model.assert_not_awaited()
    assert state.connect_options.permission_mode == "acceptEdits"
    assert pool.state is state, "reuse must not evict/replace the pooled client"
    assert not any(e.get("type") == "error" for e in events), events
    assert state.is_streaming is False  # empty turn completed, claim released


async def test_chat_permission_mode_unchanged_does_not_call_setter():
    """No spurious setter call when the binding is identical to spawn."""
    state = _reuse_state_with_opts(perm_mode="acceptEdits", model="m")
    pool = StubPool(state)

    with ExitStack() as stack:
        options = _chat_patches(stack, pool)
        _bind_session_permission(stack, "acceptEdits")
        options.model = "m"
        await collect(qe._stream_to_websocket_impl("hi", "sess-noop", "claude-code"))

    state.adapter.set_permission_mode.assert_not_awaited()
    state.adapter.send_query.assert_awaited_once()


async def test_chat_permission_mode_setter_failure_aborts_the_send_without_stranding():
    """A raising set_permission_mode must not send the query under the client's
    old (possibly looser) mode: the turn ends with an error frame instead. The
    exception goes through the normal turn-error cleanup, so the is_streaming
    claim is released rather than stranded, and the uncertain client is evicted.
    """
    state = _reuse_state_with_opts(perm_mode="bypassPermissions", model="m")
    state.adapter.set_permission_mode = AsyncMock(side_effect=RuntimeError("setter boom"))
    pool = StubPool(state)

    with ExitStack() as stack:
        options = _chat_patches(stack, pool)
        _bind_session_permission(stack, "default")
        options.model = "m"
        events = await collect(qe._stream_to_websocket_impl("hi", "sess-perm-boom", "claude-code"))

    state.adapter.set_permission_mode.assert_awaited_once_with("default")
    state.adapter.send_query.assert_not_awaited()
    assert any(e.get("type") == "error" for e in events), events
    assert state.is_streaming is False  # claim released by the error path
    assert pool.state is None, "a client with a refused permission switch must be evicted"


async def test_rest_applies_permission_mode_on_reuse_without_respawn():
    """REST path (stream_query): same in-place apply. Short-circuit the turn body
    right after the reuse block (the stream_start sync-log write is the first main
    await) to assert the apply ran before it, without harnessing a full turn."""
    state = _reuse_state_with_opts(perm_mode="default", model="m")
    pool = StubPool(state)

    async def _run_sync_boom(fn, *a, **k):
        if k.get("event_type") == "stream_start":
            raise RuntimeError("sync-log boom")
        return {"id": "row-1"}

    with ExitStack() as stack:
        options = _rest_patches(stack, pool)
        options.permission_mode = "plan"
        options.model = "m"
        stack.enter_context(patch.object(qe, "run_sync", new=AsyncMock(side_effect=_run_sync_boom)))
        with pytest.raises(RuntimeError, match="sync-log boom"):
            await collect(qe.stream_query("hi", "claude-code", session_id="sess-perm-rest"))

    state.adapter.set_permission_mode.assert_awaited_once_with("plan")
    state.adapter.set_model.assert_not_awaited()
    assert pool.state is state, "reuse must not evict/replace the pooled client"


async def test_background_applies_permission_mode_on_reuse_without_respawn(mock_db):
    """Background path: same in-place apply. Short-circuit right after the reuse
    block via _store_background_task (first statement after the 'Mark as
    streaming' claim, before the query) to assert the apply ran, without a turn."""
    state = _reuse_state_with_opts(perm_mode="default", model="m")
    pool = StubPool(state)
    sessions_db.create_session("sess-perm-bg", "default")

    async def _fake_run_sync(fn, *a, **k):
        return {"id": "row-1"}

    with ExitStack() as stack:
        stack.enter_context(patch.object(qb, "run_sync", new=AsyncMock(side_effect=_fake_run_sync)))
        stack.enter_context(patch.object(qb, "sync_engine", new=_fake_sync_engine()))
        options = MagicMock()
        options.permission_mode = "plan"
        options.model = "m"
        stack.enter_context(patch("app.core.sdk_options.build_options_from_profile", return_value=(options, {})))
        stack.enter_context(patch.object(qb, "get_session_pool", return_value=pool))
        stack.enter_context(patch.object(qb, "is_subprocess_alive", return_value=True))
        stack.enter_context(patch.object(qb, "requires_reconnect", return_value=False))
        stack.enter_context(patch.object(
            qb, "_store_background_task", side_effect=RuntimeError("stop-after-apply")
        ))

        with pytest.raises(RuntimeError, match="stop-after-apply"):
            await qb._run_background_query(
                "sess-perm-bg", "hello", {"id": "claude-code"}, None, None, None, None, None,
            )

    state.adapter.set_permission_mode.assert_awaited_once_with("plan")
    state.adapter.set_model.assert_not_awaited()
    assert pool.state is state, "reuse must not evict/replace the pooled client"
    assert state.is_streaming is False  # claim-guard released it on the raise


# ─── The chat's lease: the gates above only see a client that is already pooled ─


class _PausedConnect(HarnessAdapter):
    """The real ``connect_with_sdk_options``, stopping at one of its steps."""

    def __init__(self, world: "_ConnectWorld"):
        super().__init__()
        self.world = world

    async def connect(self, options):
        self.world.connects.append(self)
        self._connected = True
        await self.world.step("connect")

    async def prepare_background_completions(self, *_args, **_kwargs):
        await self.world.step("prepare")

    async def reconcile_user_questions(self):
        await self.world.step("reconcile")

    def is_alive(self):
        return True

    async def set_permission_mode(self, mode):
        return None

    async def send_query(self, prompt):
        self.world.sent.append(prompt)

    async def receive_events(self):
        for frame in self.world.frames_first:
            yield frame
        await self.world.step("turn")

    async def disconnect(self):
        self.world.disconnected.append(self)
        await self.world.step("disconnect")


class _ConnectWorld:
    def __init__(self, stop_at: str | None = None):
        self.stop_at = stop_at
        self.reached, self.proceed = asyncio.Event(), asyncio.Event()
        self.connects: list = []
        self.sent: list = []
        self.disconnected: list = []
        self.frames_first: list = []

    async def step(self, name: str) -> None:
        if name == self.stop_at:
            self.reached.set()
            await self.proceed.wait()

    def adapter(self):
        return _PausedConnect(self)

    def patches(self, stack: ExitStack, route: str, pool: StubPool):
        if route == "background":
            stack.enter_context(patch.object(qb, "run_sync", new=AsyncMock(return_value={"id": "row-1"})))
            stack.enter_context(patch.object(qb, "sync_engine", new=_fake_sync_engine()))
            stack.enter_context(patch.object(qb, "get_session_pool", return_value=pool))
            stack.enter_context(patch.object(qb, "get_profile", return_value={"id": "claude-code", "config": {}}))
            options = MagicMock()
            stack.enter_context(patch(
                "app.core.sdk_options.build_options_from_profile", return_value=(options, None),
            ))
        else:
            options = (_chat_patches if route == "chat" else _rest_patches)(stack, pool)
        # What a chat with no stored SDK id connects with: nothing stable to key on.
        options.session_id, options.resume_session_id = None, None
        options.include_partial_messages = False
        stack.enter_context(patch("app.core.provider.factory.make_adapter", side_effect=self.adapter))
        return options


def _start(route: str, prompt: str, session_id: str):
    """One request on ``route``; returns a future of its frames."""
    if route == "chat":
        return asyncio.ensure_future(collect(qe._stream_to_websocket_impl(prompt, session_id, "claude-code")))
    if route == "rest":
        return asyncio.ensure_future(collect(qe.stream_query(prompt, "claude-code", session_id=session_id)))

    async def background():
        started = await qb.start_background_query(prompt, "claude-code", session_id=session_id)
        await qb._pending_background_tasks[started["session_id"]]
        return []

    return asyncio.ensure_future(background())


async def _second_is_refused(route: str, session_id: str) -> None:
    if route == "background":
        with pytest.raises(SessionBusyError):
            await qb.start_background_query("two", "claude-code", session_id=session_id)
        return
    frames = await _start(route, "two", session_id)
    assert [e["message"] for e in frames if e.get("type") == "error"] == [str(SessionBusyError())]
    assert not any(e.get("type") == "done" for e in frames)


@pytest.mark.parametrize("route", ["chat", "rest", "background"])
@pytest.mark.parametrize("step", ["connect", "prepare", "reconcile", "turn"])
async def test_second_request_is_refused_at_every_step_of_the_first_turn(mock_db, route, step):
    """Two requests, no SDK id, the real connect boundary. Whichever step the first
    has reached, the second never connects a second engine."""
    sessions_db.create_session("sess-race", "default")
    world = _ConnectWorld(step)
    with ExitStack() as stack:
        world.patches(stack, route, StubPool())
        first = _start(route, "one", "sess-race")
        await asyncio.wait_for(world.reached.wait(), 10)

        await _second_is_refused(route, "sess-race")

        assert len(world.connects) == 1, "the refused request must never reach its claim"
        assert "two" not in " ".join(world.sent)
        world.proceed.set()
        await asyncio.wait_for(first, 10)
    assert "sess-race" not in session_admission._holders


async def test_background_conversation_then_immediate_continue_of_its_returned_id(mock_db):
    """The reported race: the caller gets the id back and continues it straight away."""
    world = _ConnectWorld("connect")
    with ExitStack() as stack:
        world.patches(stack, "background", StubPool())
        started = await qb.start_background_query("one", "claude-code")
        new_id = started["session_id"]
        assert new_id in session_admission._holders, "held before the caller has the id"

        await _second_is_refused("rest", new_id)
        await _second_is_refused("background", new_id)
        await asyncio.wait_for(world.reached.wait(), 10)
        assert len(world.connects) == 1

        world.proceed.set()
        await asyncio.wait_for(qb._pending_background_tasks[new_id], 10)
    await asyncio.sleep(0)
    assert new_id not in session_admission._holders


@pytest.mark.parametrize("route", ["chat", "background"])
@pytest.mark.parametrize("step", ["connect", "prepare", "reconcile"])
async def test_a_connect_cancelled_anywhere_in_its_boundary_lets_the_chat_go(mock_db, route, step):
    sessions_db.create_session("sess-cancel", "default")
    world = _ConnectWorld(step)
    with ExitStack() as stack:
        world.patches(stack, route, StubPool())
        first = _start(route, "one", "sess-cancel")
        await asyncio.wait_for(world.reached.wait(), 10)
        assert "sess-cancel" in session_admission._holders

        (qb._pending_background_tasks["sess-cancel"] if route == "background" else first).cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(first, 10)
        await asyncio.sleep(0)

        assert "sess-cancel" not in session_admission._holders
        world.stop_at = None
        retry = await asyncio.wait_for(_start(route, "two", "sess-cancel"), 10)
    assert "two" in " ".join(world.sent), retry


@pytest.mark.parametrize("route", ["chat", "rest", "background"])
async def test_a_connect_that_fails_lets_the_chat_go(mock_db, route):
    sessions_db.create_session("sess-fail", "default")
    world = _ConnectWorld()
    with ExitStack() as stack:
        world.patches(stack, route, StubPool())
        with patch.object(_PausedConnect, "connect", new=AsyncMock(side_effect=RuntimeError("no"))):
            await asyncio.wait_for(_start(route, "one", "sess-fail"), 10)
        assert "sess-fail" not in session_admission._holders


async def test_a_background_task_cancelled_before_its_first_step_lets_the_chat_go(mock_db):
    world = _ConnectWorld()
    with ExitStack() as stack:
        world.patches(stack, "background", StubPool())
        started = await qb.start_background_query("one", "claude-code")
        qb._pending_background_tasks[started["session_id"]].cancel()
        await asyncio.sleep(0.05)
    assert started["session_id"] not in session_admission._holders


async def test_a_background_start_that_fails_before_its_task_lets_the_chat_go(mock_db):
    with patch.object(qb, "get_profile", return_value=None):
        with pytest.raises(ValueError):
            await qb.start_background_query("one", "nobody", session_id="sess-noprofile")
    assert "sess-noprofile" not in session_admission._holders


async def test_failover_retry_of_a_failed_stream_is_not_refused_by_its_own_first_try(mock_db):
    """``stream_to_websocket`` runs the route again in the same task."""
    world = _ConnectWorld()
    with ExitStack() as stack:
        world.patches(stack, "chat", StubPool())
        for prompt in ("one", "two"):
            await collect(qe._stream_to_websocket_impl(prompt, "sess-again", "claude-code"))
    assert sum("one" in text or "two" in text for text in world.sent) == 2


@pytest.mark.parametrize("route", ["chat", "rest", "background"])
async def test_a_rewind_during_connect_stops_the_turn_before_it_starts(mock_db, route):
    sessions_db.create_session("sess-rw", "default")
    world = _ConnectWorld("reconcile")
    with ExitStack() as stack:
        world.patches(stack, route, StubPool())
        turn = _start(route, "one", "sess-rw")
        await asyncio.wait_for(world.reached.wait(), 10)

        rewind = await session_admission.begin_rewind("sess-rw")
        stopping = session_admission.take_for_rewind(rewind)
        world.proceed.set()
        await asyncio.wait_for(turn, 10)
        await asyncio.sleep(0)

        assert stopping.exited.is_set()
        assert world.sent == [], "a stopped turn must not send its prompt"
        assert world.disconnected, "its engine is closed, not left running"
        session_admission.close(rewind)


@pytest.mark.parametrize("route", ["chat", "rest", "background", "headless"])
async def test_a_turn_a_rewind_stopped_before_its_connect_never_claims(mock_db, route):
    """A rewind can give up waiting on a turn stuck in its preparation. If that
    turn then connected, its claim would outrank the rewind and whatever follows."""
    sessions_db.create_session("sess-early", "default")
    world = _ConnectWorld()
    rewind = None

    async def rewound_meanwhile(*_args, **_kwargs):
        nonlocal rewind
        rewind = await session_admission.begin_rewind("sess-early")
        session_admission.take_for_rewind(rewind)

    with ExitStack() as stack:
        if route == "headless":
            _headless_patches(stack, world)
        else:
            world.patches(stack, route, StubPool())
        stack.enter_context(patch.object(qe, "reconcile_memory", new=rewound_meanwhile))
        stack.enter_context(patch.object(qb, "reconcile_memory", new=rewound_meanwhile))
        if route == "headless":
            with pytest.raises(SessionReplacedError, match="rewound"):
                await asyncio.wait_for(_headless("one", "sess-early"), 10)
        else:
            await asyncio.wait_for(_start(route, "one", "sess-early"), 10)
    assert world.connects == [], "a stopped turn must not reach its claim"
    assert world.sent == []
    assert session_admission._holders["sess-early"] is rewind, "the rewind keeps the chat"
    session_admission.close(rewind)


@pytest.mark.parametrize("route", ["chat", "rest", "background"])
async def test_a_turn_another_install_took_over_reports_it_and_never_says_done(mock_db, route):
    sessions_db.create_session("sess-lost", "default")
    world = _ConnectWorld()

    async def replaced(self):
        raise SessionReplacedError()
        yield  # pragma: no cover

    with ExitStack() as stack:
        world.patches(stack, route, StubPool())
        with patch.object(_PausedConnect, "receive_events", new=replaced):
            frames = await asyncio.wait_for(_start(route, "one", "sess-lost"), 10)
    if route == "background":
        await asyncio.sleep(0)
        last = se.get_events("sess-lost")[-1]
        assert last["event_type"] == "turn.aborted"
        assert last["payload"].get("message") == SessionReplacedError.ANOTHER_COPY
    else:
        assert [e["message"] for e in frames if e.get("type") == "error"] == [SessionReplacedError.ANOTHER_COPY]
        assert not any(e.get("type") == "done" for e in frames)
    assert "sess-lost" not in session_admission._holders


@pytest.mark.parametrize("step", ["connect", "prepare", "reconcile"])
async def test_a_rest_client_leaving_during_connect_holds_the_chat_until_the_turn_is_over(mock_db, step):
    """A client leaving cancels only its own wait: the turn goes on, as it does once running."""
    sessions_db.create_session("sess-left", "default")
    world = _ConnectWorld(step)
    with ExitStack() as stack:
        world.patches(stack, "rest", StubPool())
        reader = _start("rest", "one", "sess-left")
        await asyncio.wait_for(world.reached.wait(), 10)
        reader.cancel()
        with pytest.raises(asyncio.CancelledError):
            await reader

        await _second_is_refused("rest", "sess-left")

        world.proceed.set()
        await _until_free("sess-left")
    assert "one" in " ".join(world.sent), "the turn the client left still ran"


async def _until_free(session_id: str) -> None:
    for _ in range(1000):
        if session_id not in session_admission._holders:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the chat was never freed")


def _words():
    return AssistantMessage(content=[TextBlock(text="partial")], model="m")


def _reader(route: str, prompt: str, session_id: str):
    if route == "chat":
        return qe._stream_to_websocket_impl(prompt, session_id, "claude-code")
    return qe.stream_query(prompt, "claude-code", session_id=session_id)


@pytest.mark.parametrize("route, leaves", [("chat", "closed"), ("rest", "closed"), ("rest", "cancelled")])
async def test_a_client_that_leaves_never_frees_a_running_engine_and_its_turn_is_saved(mock_db, route, leaves):
    """The engine runs on, so the chat stays held until the turn ends and has been saved."""
    sessions_db.create_session("sess-drop", "default")
    world = _ConnectWorld("turn")
    world.frames_first = [_words()]
    saved = AsyncMock()
    with ExitStack() as stack:
        world.patches(stack, route, StubPool())
        stack.enter_context(patch.object(qe, "_persist_turn_results", new=saved))
        reader = _reader(route, "one", "sess-drop")
        await asyncio.wait_for(reader.__anext__(), 10)
        if leaves == "closed":
            await reader.aclose()
            await asyncio.wait_for(world.reached.wait(), 10)
        else:
            await asyncio.wait_for(world.reached.wait(), 10)
            while True:  # read what the turn produced, then wait for more
                waiting = asyncio.ensure_future(reader.__anext__())
                await asyncio.sleep(0.01)
                if not waiting.done():
                    break
            waiting.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiting

        assert "sess-drop" in session_admission._holders, "the engine is still running"
        saved.assert_not_awaited()
        await _second_is_refused(route, "sess-drop")
        assert world.disconnected == [], "a refused message removes nothing"

        world.proceed.set()
        await _until_free("sess-drop")
        saved.assert_awaited_once()
        assert saved.await_args.kwargs.get("interrupted") in (None, False)

        world.stop_at = None
        frames = await asyncio.wait_for(_start(route, "two", "sess-drop"), 10)
    assert "two" in " ".join(world.sent), frames
    assert not [e for e in frames if e.get("type") == "error"], frames


async def test_a_reader_that_fell_behind_is_cut_off_and_the_turn_still_saves(mock_db, monkeypatch):
    sessions_db.create_session("sess-slow", "default")
    monkeypatch.setattr(session_admission, "_READER_BUFFER_BYTES", 1)
    world = _ConnectWorld("turn")
    world.frames_first = [_words() for _ in range(4)]
    saved = AsyncMock()
    with ExitStack() as stack:
        world.patches(stack, "rest", StubPool())
        stack.enter_context(patch.object(qe, "_persist_turn_results", new=saved))
        reader = _reader("rest", "one", "sess-slow")
        frames = [await asyncio.wait_for(reader.__anext__(), 10)]
        await asyncio.wait_for(world.reached.wait(), 10)
        async for frame in reader:  # it stopped reading for a while, and catches up here
            frames.append(frame)
        assert frames[-1] == {"type": "error", "message": session_admission._FELL_BEHIND_MESSAGE}
        assert "sess-slow" in session_admission._holders, "the engine is still running"
        saved.assert_not_awaited()

        world.proceed.set()
        await _until_free("sess-slow")
        saved.assert_awaited_once()
        assert saved.await_args.kwargs.get("interrupted") in (None, False)

        world.stop_at = None
        monkeypatch.setattr(session_admission, "_READER_BUFFER_BYTES", 1024 * 1024)
        frames = await asyncio.wait_for(_start("rest", "two", "sess-slow"), 10)
    assert "two" in " ".join(world.sent), frames
    assert frames[-1].get("type") == "done", frames


async def test_a_failure_after_the_client_left_still_lets_the_chat_go(mock_db):
    sessions_db.create_session("sess-drop-fail", "default")
    world = _ConnectWorld("turn")
    world.frames_first = [_words()]

    async def fails(*_args, **_kwargs):
        raise RuntimeError("database gone")

    with ExitStack() as stack:
        world.patches(stack, "chat", StubPool())
        stack.enter_context(patch.object(qe, "_persist_turn_results", new=fails))
        reader = _reader("chat", "one", "sess-drop-fail")
        await asyncio.wait_for(reader.__anext__(), 10)
        await reader.aclose()
        await asyncio.wait_for(world.reached.wait(), 10)
        world.proceed.set()
        await _until_free("sess-drop-fail")


@pytest.mark.parametrize("route", ["chat", "rest", "background"])
async def test_an_engine_still_running_refuses_the_next_message_without_being_removed(mock_db, route):
    """The engine's own "a turn is already running" reads as a broken client, so a
    request has to be refused before it gets that far."""
    sessions_db.create_session("sess-mid", "default")
    state = _fake_state(is_streaming=False, sdk_session_id="sdk-mid")
    state.adapter.engine = MagicMock(turn_running=True)
    pool = StubPool(state)
    world = _ConnectWorld()
    with ExitStack() as stack:
        world.patches(stack, route, pool)
        if route == "background":
            await qb.start_background_query("two", "claude-code", session_id="sess-mid")
            await asyncio.wait_for(qb._pending_background_tasks["sess-mid"], 10)
        else:
            await _second_is_refused(route, "sess-mid")
    assert pool.peek("sess-mid") is state, "not removed as broken"
    assert world.connects == []
    state.adapter.send_query.assert_not_called()


@pytest.mark.parametrize("route", ["chat", "rest", "background"])
async def test_a_message_sent_on_hearing_the_end_waits_for_the_route_and_is_never_refused(mock_db, route):
    """Everything the route still does happens before any successor exists."""
    from app.core.turn_recorder import TurnRecorder

    sessions_db.create_session("sess-end", "default")
    world = _ConnectWorld()
    next_turns: list = []
    real_on_event = TurnRecorder.on_event

    async def next_message():
        next_turns.append(asyncio.ensure_future(session_admission.acquire("sess-end")))
        await asyncio.sleep(0.01)

    async def on_event(self, event):
        if event.get("type") == "done":
            await next_message()
            assert not next_turns[-1].done(), "the old route is still running"
        await real_on_event(self, event)

    with ExitStack() as stack:
        world.patches(stack, route, StubPool())
        stack.enter_context(patch.object(TurnRecorder, "on_event", new=on_event))
        if route == "background":
            await asyncio.wait_for(_start(route, "one", "sess-end"), 10)
        else:
            async for event in _reader(route, "one", "sess-end"):
                if event.get("type") == "done":
                    await next_message()
                    # REST relays through a queue, so its route can be over already.
                    assert route == "rest" or not next_turns[-1].done(), "still running"
    assert len(next_turns) == 1
    session_admission.close(await asyncio.wait_for(next_turns[0], 5))


async def test_a_background_turn_finishes_its_session_cleanup_before_any_successor(mock_db):
    """A successor's permission request can only come after the old cleanup is over."""
    sessions_db.create_session("sess-perm", "default")
    world = _ConnectWorld()
    reached, proceed = asyncio.Event(), asyncio.Event()
    cancelled = AsyncMock()

    async def end_state(_session_id, **_kwargs):
        reached.set()
        await proceed.wait()

    with ExitStack() as stack:
        world.patches(stack, "background", StubPool())
        stack.enter_context(patch.object(qb, "persist_turn_end_state", new=end_state))
        stack.enter_context(patch.object(qb.permission_handler, "cancel_all_requests", new=cancelled))
        first = _start("background", "one", "sess-perm")
        await asyncio.wait_for(reached.wait(), 10)
        cancelled.assert_not_awaited()
        await _second_is_refused("background", "sess-perm")
        proceed.set()
        await asyncio.wait_for(first, 10)
        cancelled.assert_awaited_once_with("sess-perm")
        await asyncio.sleep(0)
        assert "sess-perm" not in session_admission._holders


@pytest.mark.parametrize("second_rewind", [False, True], ids=["alone", "second-rewind-beginning"])
async def test_a_background_turn_a_rewind_outlasted_leaves_its_successors_permissions_alone(
    mock_db, second_rewind,
):
    """A stopped turn can end after the rewind gave up waiting and a new turn began,
    even with another rewind on its way, which may yet be rejected."""
    sessions_db.create_session("sess-late", "default")
    world = _ConnectWorld("turn")
    cancelled, cleared = AsyncMock(), MagicMock()
    with ExitStack() as stack:
        world.patches(stack, "background", StubPool())
        stack.enter_context(patch.object(qb.permission_handler, "cancel_all_requests", new=cancelled))
        stack.enter_context(patch.object(qb.permission_handler, "clear_session_rules", new=cleared))
        first = _start("background", "one", "sess-late")
        await asyncio.wait_for(world.reached.wait(), 10)

        rewind = await session_admission.begin_rewind("sess-late")
        session_admission.take_for_rewind(rewind)
        session_admission.close(rewind)
        successor = await session_admission.acquire("sess-late")
        later = await session_admission.begin_rewind("sess-late") if second_rewind else None

        world.proceed.set()
        await asyncio.wait_for(first, 10)
        cancelled.assert_not_awaited()
        cleared.assert_not_called()
        assert session_admission._holders["sess-late"] is successor
        if later is not None:
            session_admission.close(later)
        session_admission.close(successor)


# ─── Headless ───────────────────────────────────────────────────────────────


def _headless_patches(stack: ExitStack, world: _ConnectWorld) -> None:
    stack.enter_context(patch.object(qe, "get_profile", return_value={"id": "claude-code", "config": {}}))
    stack.enter_context(patch.object(qe, "run_sync", new=AsyncMock(return_value={"id": "row-1", "sdk_session_id": None})))
    options = MagicMock()
    options.session_id, options.resume_session_id, options.cwd = None, None, None
    stack.enter_context(patch("app.core.sdk_options.build_options_from_profile", return_value=(options, None)))
    stack.enter_context(patch("app.core.provider.factory.make_adapter", side_effect=world.adapter))
    stack.enter_context(patch.object(qe, "sync_engine", new=_fake_sync_engine()))


async def _headless(prompt: str, session_id: str):
    return await qe.execute_query(prompt, "claude-code", session_id=session_id)


@pytest.mark.parametrize("step", ["connect", "prepare", "reconcile", "turn", "disconnect"])
async def test_headless_turn_holds_the_chat_through_teardown(mock_db, step):
    world = _ConnectWorld(step)
    with ExitStack() as stack:
        _headless_patches(stack, world)
        first = asyncio.ensure_future(_headless("one", "sess-headless"))
        await asyncio.wait_for(world.reached.wait(), 10)
        with pytest.raises(SessionBusyError):
            await _headless("two", "sess-headless")
        assert len(world.connects) == 1
        world.proceed.set()
        await asyncio.wait_for(asyncio.gather(first, return_exceptions=True), 10)
    assert "sess-headless" not in session_admission._holders


async def test_cancelled_teardown_does_not_leave_the_chat_busy_forever(mock_db):
    """With no pool entry to evict, only the route's own scope can free it."""
    world = _ConnectWorld("disconnect")
    with ExitStack() as stack:
        _headless_patches(stack, world)
        first = asyncio.ensure_future(_headless("one", "sess-teardown"))
        await asyncio.wait_for(world.reached.wait(), 10)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert "sess-teardown" not in session_admission._holders

        world.stop_at = None
        await _headless("two", "sess-teardown")
    assert any("two" in text for text in world.sent)


async def test_a_headless_turn_a_rewind_took_says_it_was_rewound(mock_db):
    world = _ConnectWorld("turn")
    with ExitStack() as stack:
        _headless_patches(stack, world)

        async def replaced(self):
            await self.world.step("turn")
            raise SessionReplacedError()
            yield  # pragma: no cover

        with patch.object(_PausedConnect, "receive_events", new=replaced):
            first = asyncio.ensure_future(_headless("one", "sess-rewound"))
            await asyncio.wait_for(world.reached.wait(), 10)
            rewind = await session_admission.begin_rewind("sess-rewound")
            session_admission.take_for_rewind(rewind)
            world.proceed.set()
            with pytest.raises(SessionReplacedError, match="rewound"):
                await asyncio.wait_for(first, 10)
        session_admission.close(rewind)


async def test_a_rewind_during_a_headless_connect_stops_it_before_it_sends(mock_db):
    world = _ConnectWorld("reconcile")
    with ExitStack() as stack:
        _headless_patches(stack, world)
        first = asyncio.ensure_future(_headless("one", "sess-headless-rw"))
        await asyncio.wait_for(world.reached.wait(), 10)
        rewind = await session_admission.begin_rewind("sess-headless-rw")
        session_admission.take_for_rewind(rewind)
        world.proceed.set()
        with pytest.raises(SessionReplacedError, match="rewound"):
            await asyncio.wait_for(first, 10)
        assert world.sent == []
        session_admission.close(rewind)
