"""SessionRuntime × computer-use: Stop halts, new turns re-arm.

The chat Stop button drives ``SessionRuntime.interrupt(mode="stop")``; steering
uses ``mode="send_now"``. Only a real Stop may halt the session's computer-use
control (and drop its access grants); the session's NEXT turn must re-arm it so
a fresh agent can act again.
"""

from contextlib import contextmanager
from unittest.mock import AsyncMock, patch

import pytest

from app.core.session_runtime import SessionRuntime
from app.mcp.computer_use import abort, access


@pytest.fixture(autouse=True)
def _clean_cu_state():
    abort.resume("s1")
    abort.resume("s2")
    access.revoke("s1")
    access.revoke("s2")
    yield
    abort.resume("s1")
    abort.resume("s2")
    access.revoke("s1")
    access.revoke("s2")


def _runtime(sid="s1", tid="t1") -> SessionRuntime:
    rt = SessionRuntime.__new__(SessionRuntime)
    rt.session_id = sid
    rt.turn_id = tid
    rt._interrupt_driving = False
    rt._interrupt_requested_at = None
    rt._abort_reason = None
    rt._drain_after_cancel = False
    rt._pump_task = None
    rt.source = "user"
    rt.turn = {"command_id": None}
    rt.api_user_id = None
    return rt


@contextmanager
def _interrupt_stubs():
    """Stub every side effect interrupt() performs beyond computer-use.

    ``interrupt_session`` is imported lazily from query_session_lifecycle, so
    it is patched at its SOURCE module. The settle wait sees a terminal turn
    immediately (mocked ``get_turn``), so no escalation ever runs.
    """
    from app.core.session_runtime import permission_handler as _ph_singleton

    with patch.object(
        _ph_singleton, "cancel_all_requests", new=AsyncMock()
    ), patch(
        "app.core.session_runtime.record_interrupting", new=AsyncMock()
    ), patch(
        "app.core.query_session_lifecycle.interrupt_session", new=AsyncMock()
    ), patch(
        "app.core.query_session_lifecycle.stop_all_background_tasks",
        new=AsyncMock(return_value=[]),
    ), patch(
        "app.core.session_runtime.turns_db"
    ) as turns_mod:
        turns_mod.get_turn.return_value = {"state": "aborted"}
        turns_mod.TERMINAL_STATES = ("completed", "aborted", "parked")
        yield


class TestStopHaltsComputerUse:
    @pytest.mark.asyncio
    async def test_stop_halt_and_revoke(self):
        access.grant("s1", apps=["Notepad"])
        rt = _runtime()
        with _interrupt_stubs():
            outcome = await rt.interrupt("stop")
        assert outcome == "aborted"
        assert abort.is_stopped("s1")
        assert not access.is_granted("s1")

    @pytest.mark.asyncio
    async def test_other_sessions_unaffected(self):
        access.grant("s2", apps=["Calc"])
        rt = _runtime(sid="s1", tid="t1")
        with _interrupt_stubs():
            await rt.interrupt("stop")
        assert not abort.is_stopped("s2")
        assert access.is_granted("s2")

    @pytest.mark.asyncio
    async def test_send_now_does_not_halt(self):
        access.grant("s1", apps=["Notepad"])
        rt = _runtime()
        with _interrupt_stubs():
            await rt.interrupt("send_now")
        assert not abort.is_stopped("s1")
        assert access.is_granted("s1")


class _Bail(Exception):
    """Sentinel: aborts execute() right AFTER the re-arm seam under test."""


async def _sync_to_thread(fn, *args, **kwargs):
    """Awaitable stand-in for asyncio.to_thread running the fn inline."""
    if callable(fn):
        return fn(*args, **kwargs)
    return None


class TestNewTurnRearms:
    """execute() calls abort.resume(sid) right after registering as the live
    runtime — the exact seam a Stop-halted session passes through on its next
    turn. Everything after ``_reset_started_this_turn`` is never reached: that
    bare call raises the _Bail sentinel, which propagates out of execute() and
    is caught here (the lease release in the finally is mocked away)."""

    @pytest.mark.asyncio
    async def test_execute_rearms_session(self):
        abort.request_stop("s1")
        assert abort.is_stopped("s1")

        rt = _runtime(sid="s1", tid="t2")
        def check_rearmed_then_exit(*args):
            assert not abort.is_stopped("s1")
            raise _Bail()
        with patch(
            "app.core.session_runtime.asyncio.to_thread", new=_sync_to_thread
        ), patch(
            "app.core.session_runtime.placement", create=True
        ) as placement, patch(
            "app.core.session_runtime.runs_db"
        ) as runs_mod, patch(
            "app.core.session_runtime.turns_db"
        ) as turns_mod, patch(
            "app.core.session_runtime._notify_run_change"
        ), patch(
            "app.core.session_runtime._live_runtimes", {}
        ), patch(
            "app.core.session_runtime._broadcast_queue_after_start", new=AsyncMock()
        ), patch.object(
            SessionRuntime, "_maybe_pin_placement", new=AsyncMock()
        ), patch.object(
            SessionRuntime, "_restore_working_set", new=AsyncMock()
        ), patch(
            "app.core.session_runtime.stop_idle_reader", new=AsyncMock()
        ):
            placement.resolve_placement.return_value = None
            runs_mod.claim_run.return_value = {"id": "r1", "fence_token": 1}
            turns_mod.start_turn.return_value = {"id": "t2"}
            turns_mod.get_live_turn.return_value = None
            # The re-arm runs between start_turn and _reset_started_this_turn.
            with patch.object(
                SessionRuntime,
                "_reset_started_this_turn",
                side_effect=check_rearmed_then_exit,
            ):
                with pytest.raises(_Bail):
                    await rt.execute()

        # Finishing (or failing) the turn releases input and fences late workers.
        assert abort.is_stopped("s1")
