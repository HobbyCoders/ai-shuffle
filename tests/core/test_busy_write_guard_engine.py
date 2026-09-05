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
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.core.query_engine as qe
import app.core.query_background as qb
from app.core.session_pool import SessionState
from app.db import session_events as se
from app.db import sessions as sessions_db


# ─── helpers ────────────────────────────────────────────────────────────────


def _fake_state(**overrides) -> SessionState:
    adapter = MagicMock()
    adapter.is_alive = MagicMock(return_value=True)
    adapter.send_query = AsyncMock()

    async def _empty_response():
        if False:  # pragma: no cover — empty async generator
            yield None

    adapter.receive_events = MagicMock(side_effect=lambda: _empty_response())
    state = SessionState(adapter=adapter, is_connected=True)
    for key, value in overrides.items():
        setattr(state, key, value)
    return state


class _StubPool:
    def __init__(self, state=None):
        self.state = state

    def get(self, session_id):
        return self.state

    def peek(self, session_id):
        return self.state

    def put(self, session_id, state):
        self.state = state

    def admission_check(self, lane="delegated"):
        return None

    async def evict_if_needed(self):
        return None

    async def remove(self, session_id):
        self.state = None

    async def remove_dead(self, session_id):
        self.state = None

    async def remove_broken(self, session_id):
        self.state = None


def _chat_patches(stack: ExitStack, pool: _StubPool):
    """Patch every stream_to_websocket boundary up to (and past) the reuse gate."""
    stack.enter_context(patch.object(
        qe, "get_profile", return_value={"id": "claude-code", "config": {}}
    ))

    async def _fake_run_sync(fn, *a, **k):
        return {"id": "row-1"}

    stack.enter_context(patch.object(qe, "run_sync", new=AsyncMock(side_effect=_fake_run_sync)))
    oauth = MagicMock()
    oauth.ensure_fresh_credentials = AsyncMock()
    stack.enter_context(patch.object(qe, "direct_oauth", new=oauth))
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


async def _collect(agen):
    events = []
    async for ev in agen:
        events.append(ev)
    return events


# ─── stream_to_websocket (chat gate) ────────────────────────────────────────


async def test_chat_gate_refuses_busy_client_and_never_writes_stdin():
    """A query for a session whose pooled client is mid-turn yields a busy
    error and returns — client.query() is never called on the busy client."""
    state = _fake_state(is_streaming=True)
    pool = _StubPool(state)

    with ExitStack() as stack:
        _chat_patches(stack, pool)
        events = await _collect(qe.stream_to_websocket("hi", "sess-busy", "claude-code"))

    assert any(e.get("type") == "error" and "busy" in e.get("message", "").lower()
               for e in events), events
    state.adapter.send_query.assert_not_awaited()
    # The rejection must not clear the ACTIVE turn's flag.
    assert state.is_streaming is True


async def test_chat_gate_uses_current_pool_entry_after_async_setup():
    old_state = _fake_state()
    current_state = _fake_state()
    pool = _StubPool(old_state)
    setup_started = asyncio.Event()
    finish_setup = asyncio.Event()

    async def _refresh_credentials(*args, **kwargs):
        setup_started.set()
        await finish_setup.wait()

    with ExitStack() as stack:
        _chat_patches(stack, pool)
        qe.direct_oauth.ensure_fresh_credentials.side_effect = _refresh_credentials
        query = asyncio.create_task(_collect(qe.stream_to_websocket(
            "hi", "sess-replaced", "claude-code",
            overrides={"provider_id": "provider-a"},
        )))
        try:
            await asyncio.wait_for(setup_started.wait(), timeout=2)
            pool.state = current_state
        finally:
            finish_setup.set()
            events = await asyncio.wait_for(query, timeout=2)

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
    pool = _StubPool(state)

    with ExitStack() as stack:
        options = _chat_patches(stack, pool)
        _claim_window_options(options)
        await _collect(qe.stream_to_websocket("hi", "sess-claim", "claude-code"))

    assert observed == [True], (
        "is_streaming must already be claimed when the first await after the "
        f"reuse decision runs — observed {observed}"
    )


async def test_chat_gate_two_concurrent_entrants_exactly_one_proceeds():
    """Entrant A holds the client mid-turn; entrant B arriving concurrently is
    rejected with a busy error and never writes into A's stdin."""
    state = _fake_state()
    pool = _StubPool(state)
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
            async for ev in qe.stream_to_websocket("first", "sess-race", "claude-code"):
                a_events.append(ev)

        task_a = asyncio.create_task(_consume_a())
        await asyncio.wait_for(a_blocked.wait(), timeout=5.0)
        assert state.is_streaming is True

        # B arrives while A is mid-turn.
        b_events = await _collect(qe.stream_to_websocket("second", "sess-race", "claude-code"))

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
    pool = _StubPool(state)
    state.adapter.send_query = AsyncMock(side_effect=RuntimeError("boom"))

    with ExitStack() as stack:
        _chat_patches(stack, pool)
        events = await _collect(qe.stream_to_websocket("hi", "sess-exc", "claude-code"))

    assert any(e.get("type") == "error" for e in events), events
    assert state.is_streaming is False


async def test_chat_is_streaming_cleared_on_cancel():
    state = _fake_state()
    pool = _StubPool(state)

    async def _cancelled_response():
        raise asyncio.CancelledError()
        yield None  # pragma: no cover

    state.adapter.receive_events = MagicMock(
        side_effect=lambda: _cancelled_response()
    )

    with ExitStack() as stack:
        _chat_patches(stack, pool)
        events = await _collect(qe.stream_to_websocket("hi", "sess-cancel", "claude-code"))

    assert any(e.get("type") == "interrupted" for e in events), events
    assert state.is_streaming is False


# ─── stream_query (REST gate) ───────────────────────────────────────────────


def _rest_patches(stack: ExitStack, pool: _StubPool):
    stack.enter_context(patch.object(
        qe, "get_profile", return_value={"id": "claude-code", "config": {}}
    ))

    async def _fake_run_sync(fn, *a, **k):
        return {"id": "row-1"}

    stack.enter_context(patch.object(qe, "run_sync", new=AsyncMock(side_effect=_fake_run_sync)))
    oauth = MagicMock()
    oauth.ensure_fresh_credentials = AsyncMock()
    stack.enter_context(patch.object(qe, "direct_oauth", new=oauth))
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
    pool = _StubPool(state)

    with ExitStack() as stack:
        _rest_patches(stack, pool)
        events = await _collect(qe.stream_query("hi", "claude-code", session_id="sess-busy"))

    assert any(e.get("type") == "error" and "busy" in e.get("message", "").lower()
               for e in events), events
    state.adapter.send_query.assert_not_awaited()
    assert state.is_streaming is True  # active turn's claim untouched


# ─── _run_background_query gate ─────────────────────────────────────────────


async def test_background_gate_refuses_busy_client_and_never_writes_stdin(mock_db):
    state = _fake_state(is_streaming=True)
    pool = _StubPool(state)
    # v2 session — the event log is the background path's only error surface.
    sessions_db.create_session("sess-busy", "default")

    sync = _fake_sync_engine()

    async def _fake_run_sync(fn, *a, **k):
        return {"id": "row-1"}

    with ExitStack() as stack:
        stack.enter_context(patch.object(qb, "run_sync", new=AsyncMock(side_effect=_fake_run_sync)))
        stack.enter_context(patch.object(qb, "sync_engine", new=sync))
        oauth = MagicMock()
        oauth.ensure_fresh_credentials = AsyncMock()
        stack.enter_context(patch.object(qb, "direct_oauth", new=oauth))
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
    pool = _StubPool(state)

    with ExitStack() as stack:
        options = _chat_patches(stack, pool)
        _claim_window_options(options)
        with pytest.raises(KeyboardInterrupt):
            await _collect(qe.stream_to_websocket("hi", "sess-window", "claude-code"))

    assert state.is_streaming is False, "claim must be released on claim-window failure"
    assert state.streaming_started_at is None
    state.adapter.send_query.assert_not_awaited()


async def test_chat_claim_window_cancel_clears_flag():
    """Same window, CancelledError (except Exception would MISS it — the guard
    must be BaseException-wide): the claim is released and the cancel propagates."""
    state = _claim_window_state(set_model_side_effect=asyncio.CancelledError())
    pool = _StubPool(state)

    with ExitStack() as stack:
        options = _chat_patches(stack, pool)
        _claim_window_options(options)
        with pytest.raises(asyncio.CancelledError):
            await _collect(qe.stream_to_websocket("hi", "sess-window", "claude-code"))

    assert state.is_streaming is False, "claim must be released on cancellation"
    assert state.streaming_started_at is None
    state.adapter.send_query.assert_not_awaited()


async def test_rest_claim_window_exception_clears_flag():
    """stream_query: a failure in the post-claim window (V2: the stream_start
    sync-log write is the first await after the 'Mark as streaming' claim — the
    old broadcast_stream_start seam is deleted) clears the flag and propagates
    as before."""
    state = _fake_state()
    pool = _StubPool(state)

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
            await _collect(qe.stream_query("hi", "claude-code", session_id="sess-window"))

    assert state.is_streaming is False, "claim must be released on claim-window failure"
    assert state.streaming_started_at is None
    state.adapter.send_query.assert_not_awaited()


async def test_background_claim_window_exception_clears_flag():
    """_run_background_query: a failure between the reuse-decision claim and the
    main try (here: the post-claim set_model await) clears the flag and
    propagates — the pooled client stays reusable."""
    state = _claim_window_state(set_model_side_effect=KeyboardInterrupt("claim boom"))
    pool = _StubPool(state)

    sync = _fake_sync_engine()

    async def _fake_run_sync(fn, *a, **k):
        return {"id": "row-1"}

    with ExitStack() as stack:
        stack.enter_context(patch.object(qb, "run_sync", new=AsyncMock(side_effect=_fake_run_sync)))
        stack.enter_context(patch.object(qb, "sync_engine", new=sync))
        oauth = MagicMock()
        oauth.ensure_fresh_credentials = AsyncMock()
        stack.enter_context(patch.object(qb, "direct_oauth", new=oauth))
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


async def test_chat_applies_permission_mode_on_reuse_without_respawn():
    """WS path: a reused client whose spawn permission_mode differs from this
    turn's gets set_permission_mode(new) awaited, and the pool entry is NOT
    torn down (no respawn — that would kill in-flight background tasks)."""
    state = _reuse_state_with_opts(perm_mode="default", model="m")
    pool = _StubPool(state)

    with ExitStack() as stack:
        options = _chat_patches(stack, pool)
        options.permission_mode = "plan"
        options.model = "m"  # unchanged → set_model must NOT fire
        events = await _collect(qe.stream_to_websocket("hi", "sess-perm", "claude-code"))

    state.adapter.set_permission_mode.assert_awaited_once_with("plan")
    state.adapter.set_model.assert_not_awaited()
    assert pool.state is state, "reuse must not evict/replace the pooled client"
    assert not any(e.get("type") == "error" for e in events), events
    assert state.is_streaming is False  # empty turn completed, claim released


async def test_chat_permission_mode_unchanged_does_not_call_setter():
    """No spurious setter call when permission_mode is identical to spawn."""
    state = _reuse_state_with_opts(perm_mode="plan", model="m")
    pool = _StubPool(state)

    with ExitStack() as stack:
        options = _chat_patches(stack, pool)
        options.permission_mode = "plan"
        options.model = "m"
        await _collect(qe.stream_to_websocket("hi", "sess-noop", "claude-code"))

    state.adapter.set_permission_mode.assert_not_awaited()


async def test_chat_permission_mode_setter_failure_does_not_propagate():
    """A raising set_permission_mode must be swallowed (logged, never raised): an
    exception between the is_streaming claim and the main try/finally would
    strand the flag and brick the pooled client. The turn proceeds normally."""
    state = _reuse_state_with_opts(perm_mode="default", model="m")
    state.adapter.set_permission_mode = AsyncMock(side_effect=RuntimeError("setter boom"))
    pool = _StubPool(state)

    with ExitStack() as stack:
        options = _chat_patches(stack, pool)
        options.permission_mode = "plan"
        options.model = "m"
        events = await _collect(qe.stream_to_websocket("hi", "sess-perm-boom", "claude-code"))

    state.adapter.set_permission_mode.assert_awaited_once_with("plan")
    assert not any(e.get("type") == "error" for e in events), events
    assert state.is_streaming is False  # turn still completed cleanly
    assert pool.state is state


async def test_rest_applies_permission_mode_on_reuse_without_respawn():
    """REST path (stream_query): same in-place apply. Short-circuit the turn body
    right after the reuse block (the stream_start sync-log write is the first main
    await) to assert the apply ran before it, without harnessing a full turn."""
    state = _reuse_state_with_opts(perm_mode="default", model="m")
    pool = _StubPool(state)

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
            await _collect(qe.stream_query("hi", "claude-code", session_id="sess-perm-rest"))

    state.adapter.set_permission_mode.assert_awaited_once_with("plan")
    state.adapter.set_model.assert_not_awaited()
    assert pool.state is state, "reuse must not evict/replace the pooled client"


async def test_background_applies_permission_mode_on_reuse_without_respawn(mock_db):
    """Background path: same in-place apply. Short-circuit right after the reuse
    block via _store_background_task (first statement after the 'Mark as
    streaming' claim, before the query) to assert the apply ran, without a turn."""
    state = _reuse_state_with_opts(perm_mode="default", model="m")
    pool = _StubPool(state)
    sessions_db.create_session("sess-perm-bg", "default")

    async def _fake_run_sync(fn, *a, **k):
        return {"id": "row-1"}

    with ExitStack() as stack:
        stack.enter_context(patch.object(qb, "run_sync", new=AsyncMock(side_effect=_fake_run_sync)))
        stack.enter_context(patch.object(qb, "sync_engine", new=_fake_sync_engine()))
        oauth = MagicMock()
        oauth.ensure_fresh_credentials = AsyncMock()
        stack.enter_context(patch.object(qb, "direct_oauth", new=oauth))
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
