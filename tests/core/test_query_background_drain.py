"""
Bug D regression: the REST fire-and-forget background path must enforce the same
single-reader invariant as the WebSocket turn loop.

`_run_background_query` reuses the pooled SDK client and reads its message stream
(`client.query()` + `receive_response()`). A between-turns "drain pump" (the R2
reader, `receive_messages()`) may be reading that SAME client when the background
query arrives. Two coroutines reading one anyio message stream loses/corrupts
messages, so the background path MUST cancel any live drain pump BEFORE it touches
the pooled client's message stream — exactly what every WS turn does via
stop_drain_pump() at turn start.

Ordering nuance (F3): the stale-turn early gate performs a read-only
``get_session_pool().peek(session_id)`` BEFORE stop_drain_pump — deliberately.
The pump is the only thing that can clear ``stale_turn_undrained``, so the
refusal must happen while the pump is still running (stopping first would orphan
the flag forever). A peek never touches the client's message stream, so the
single-reader invariant is untouched.

These tests pin that ordering: peek (non-reader, stale gate) → stop_drain_pump →
pool acquisition (the first place this turn becomes a reader).
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.core.query_background import _run_background_query


def _preamble_patches(stop_mock, *, pool_side_effect):
    """Patch every collaborator between function entry and the pool acquisition.

    `get_session_pool` is given `pool_side_effect` so the caller controls both
    the early stale-gate peek and the pool-acquisition boundary — the latter is
    the first place this turn would become a reader of the pooled client, so
    halting there lets us assert the pump was already stopped without mocking
    the entire streaming loop.
    """
    sync_mock = MagicMock()
    sync_mock.broadcast_message_added = AsyncMock()
    oauth_mock = MagicMock()
    oauth_mock.ensure_fresh_credentials = AsyncMock()
    return [
        patch("app.core.query_background.run_sync", new=AsyncMock(return_value={"id": "msg-1"})),
        patch("app.core.query_background.sync_engine", new=sync_mock),
        patch("app.core.query_background.direct_oauth", new=oauth_mock),
        patch(
            "app.core.sdk_options.build_options_from_profile",
            new=MagicMock(return_value=(MagicMock(), {})),
        ),
        patch("app.core.query_background.get_session_pool", side_effect=pool_side_effect),
        # The function-local `from app.api.ws_cleanup import stop_drain_pump` resolves
        # against the ws_cleanup module, so that is where the patch must land.
        patch("app.api.ws_cleanup.stop_drain_pump", new=stop_mock),
    ]


def _peek_then_halt(calls=None):
    """get_session_pool side effect: first call (stale-gate peek) returns a pool
    whose peek() finds no pooled state; the second call (pool acquisition — the
    reader boundary) raises the halt sentinel."""
    state = {"n": 0}

    def _pool():
        state["n"] += 1
        if state["n"] == 1:
            if calls is not None:
                calls.append(("peek", None))
            pool = MagicMock()
            pool.peek = MagicMock(return_value=None)
            return pool
        if calls is not None:
            calls.append(("pool", None))
        raise RuntimeError("__halt_at_pool__")

    return _pool


async def test_run_background_query_stops_drain_pump_before_pool_access():
    """The pump is cancelled before the pooled client is acquired (reader boundary)."""
    stop_mock = AsyncMock()

    patches = _preamble_patches(stop_mock, pool_side_effect=_peek_then_halt())
    for p in patches:
        p.start()
    try:
        with pytest.raises(RuntimeError, match="__halt_at_pool__"):
            await _run_background_query(
                "sess-1", "hello",
                {"id": "profile-1"}, None, None, None, None, None,
            )
    finally:
        for p in patches:
            p.stop()

    # The pump-stop ran (proving it precedes pool acquisition, which raised the
    # sentinel) and targeted the correct session.
    stop_mock.assert_awaited_once_with("sess-1")


async def test_run_background_query_pump_stop_precedes_pool_call_order():
    """Order is: stale-gate peek (non-reader) → stop_drain_pump → pool acquisition."""
    calls = []
    stop_mock = AsyncMock(side_effect=lambda sid: calls.append(("stop", sid)))

    patches = _preamble_patches(stop_mock, pool_side_effect=_peek_then_halt(calls))
    for p in patches:
        p.start()
    try:
        with pytest.raises(RuntimeError, match="__halt_at_pool__"):
            await _run_background_query(
                "sess-9", "hi",
                {"id": "profile-1"}, None, None, None, None, None,
            )
    finally:
        for p in patches:
            p.stop()

    assert calls == [("peek", None), ("stop", "sess-9"), ("pool", None)]
