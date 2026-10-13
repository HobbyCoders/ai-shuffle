"""
Tests for turn-end context-usage capture in ``app.core.query_persistence``.

``capture_context_usage`` self-peeks the session pool for the live adapter, runs
the SDK ``get_context_usage()`` control call (via the shared
``shielded_get_context_usage`` detach+shield+timeout pattern the GET endpoint
uses) after a turn's results are persisted, and writes the wrapped payload to
``sessions.context_usage_json``. It is strictly best-effort: any timeout or
exception must be swallowed so it never fails or delays the turn.
"""

import asyncio
import contextlib

from unittest.mock import AsyncMock, MagicMock, patch

from app.core import query_persistence as qp


@contextlib.contextmanager
def _patch_pool(state):
    """Patch the pool source so ``peek()`` returns ``state``.

    ``capture_context_usage`` lazy-imports ``get_session_pool`` from
    ``app.core.session_pool`` at call time, so patching the source binding
    governs it.
    """
    mock_pool = MagicMock()
    mock_pool.peek.return_value = state
    with patch("app.core.session_pool.get_session_pool", return_value=mock_pool):
        yield


def _make_state(adapter, *, is_connected=True):
    state = MagicMock()
    state.is_connected = is_connected
    state.adapter = adapter
    return state


async def test_capture_writes_column_on_success():
    """A successful control call persists the raw payload via the DB helper."""
    usage = {
        "categories": [{"name": "system", "tokens": 1200}],
        "totalTokens": 1200,
        "maxTokens": 200000,
    }
    adapter = MagicMock()
    adapter.get_context_usage = AsyncMock(return_value=usage)

    with _patch_pool(_make_state(adapter)), \
            patch.object(qp.database, "set_session_context_usage") as mock_set:
        await qp.capture_context_usage("sess-1")

    mock_set.assert_called_once_with(session_id="sess-1", data=usage)


async def test_capture_skips_write_when_unsupported():
    """A None result (SDK method unavailable) must not write the column."""
    adapter = MagicMock()
    adapter.get_context_usage = AsyncMock(return_value=None)

    with _patch_pool(_make_state(adapter)), \
            patch.object(qp.database, "set_session_context_usage") as mock_set:
        await qp.capture_context_usage("sess-1")

    mock_set.assert_not_called()


async def test_capture_swallows_timeout():
    """A control call that hangs past the bound is swallowed, no write, no raise."""
    async def _hang():
        await asyncio.sleep(2)

    adapter = MagicMock()
    adapter.get_context_usage = _hang

    with _patch_pool(_make_state(adapter)), \
            patch.object(qp.database, "set_session_context_usage") as mock_set, \
            patch.object(qp, "SDK_CONTEXT_USAGE_TIMEOUT_SECONDS", 0.05):
        # Must not raise.
        await qp.capture_context_usage("sess-1")

    mock_set.assert_not_called()


async def test_capture_swallows_control_call_exception():
    """An exception from the control call is swallowed, no write, no raise."""
    adapter = MagicMock()
    adapter.get_context_usage = AsyncMock(side_effect=RuntimeError("boom"))

    with _patch_pool(_make_state(adapter)), \
            patch.object(qp.database, "set_session_context_usage") as mock_set:
        await qp.capture_context_usage("sess-1")

    mock_set.assert_not_called()


async def test_capture_swallows_db_write_exception():
    """A failure while persisting is swallowed and never propagates."""
    adapter = MagicMock()
    adapter.get_context_usage = AsyncMock(return_value={"totalTokens": 5})

    with _patch_pool(_make_state(adapter)), \
            patch.object(
                qp.database, "set_session_context_usage",
                side_effect=RuntimeError("db down"),
            ):
        # Must not raise despite the DB write blowing up.
        await qp.capture_context_usage("sess-1")


async def test_capture_noop_when_not_pooled():
    """No pooled state short-circuits without touching the DB."""
    with _patch_pool(None), \
            patch.object(qp.database, "set_session_context_usage") as mock_set:
        await qp.capture_context_usage("sess-1")

    mock_set.assert_not_called()


async def test_capture_noop_when_not_connected():
    """A pooled but disconnected state short-circuits without touching the DB."""
    adapter = MagicMock()
    adapter.get_context_usage = AsyncMock(return_value={"totalTokens": 5})

    with _patch_pool(_make_state(adapter, is_connected=False)), \
            patch.object(qp.database, "set_session_context_usage") as mock_set:
        await qp.capture_context_usage("sess-1")

    mock_set.assert_not_called()


async def test_capture_noop_when_adapter_none():
    """A missing adapter short-circuits without touching the DB."""
    with _patch_pool(_make_state(None)), \
            patch.object(qp.database, "set_session_context_usage") as mock_set:
        await qp.capture_context_usage("sess-1")

    mock_set.assert_not_called()


async def test_schedule_runs_capture_detached():
    """``schedule_context_usage_capture`` fires the capture on a tracked task."""
    usage = {"totalTokens": 7}
    adapter = MagicMock()
    adapter.get_context_usage = AsyncMock(return_value=usage)

    with _patch_pool(_make_state(adapter)), \
            patch.object(qp.database, "set_session_context_usage") as mock_set:
        qp.schedule_context_usage_capture("sess-1")
        # The detached task holds a strong reference in the module set.
        assert qp._context_usage_capture_tasks
        # Await the tracked task to completion (the capture writes via run_sync,
        # which hops a thread executor, so a bare sleep(0) is not enough).
        task = next(iter(qp._context_usage_capture_tasks))
        await task

    mock_set.assert_called_once_with(session_id="sess-1", data=usage)
    # Done-callback discards the task from the tracking set.
    assert not qp._context_usage_capture_tasks
