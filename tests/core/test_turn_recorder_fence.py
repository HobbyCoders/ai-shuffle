"""Fence-violation handling in TurnRecorder.

A superseded runner used to log one WARNING per rejected batch for the rest
of the turn (920 lines in 3.5 minutes in the 2026-08-22 incident). Now the
first violation flips the recorder to superseded: later events are dropped
and counted, the owner hook fires once, and close() logs a single WARNING.
"""

import asyncio
import logging
from unittest.mock import patch

import pytest

from app.core import turn_recorder as tr
from app.db import session_events as session_events_db


def _recorder() -> tr.TurnRecorder:
    return tr.TurnRecorder("sess-1", "cmd-1", "run-1", 7, "runner-a")


def _fence_reject(*_args, **_kwargs):
    raise session_events_db.FenceViolation("run-1", 7)


def _user_event(i: int) -> dict:
    return {"type": "stream_delta", "delta": {"type": "text_delta", "text": f"t{i}"}}


@pytest.mark.asyncio
async def test_first_violation_supersedes_and_logs_once(caplog):
    caplog.set_level(logging.WARNING, logger="app.core.turn_recorder")
    rec = _recorder()
    fired = []
    rec.on_superseded = lambda: fired.append(True)

    with patch.object(session_events_db, "append_events", side_effect=_fence_reject) as append:
        await rec.start("hello")  # 2 events in one rejected batch
        assert rec.superseded is True
        assert fired == [True]
        assert rec.superseded_dropped == 2

        # Everything after the first violation is dropped without touching the DB.
        for i in range(50):
            await rec.on_event(_user_event(i))
        await rec.on_event({"type": "done"})
        assert append.call_count == 1
        assert rec.superseded_dropped == 2 + 51
        assert fired == [True]  # hook fired exactly once

        with patch.object(tr.runs_db, "release_run", return_value=None), \
                patch.object(tr, "_notify_run_change", lambda *_: None):
            await rec.close()
        assert append.call_count == 1  # close appends nothing under a lost fence

    fence_lines = [r for r in caplog.records if "fence violation" in r.getMessage()]
    assert len(fence_lines) == 1
    assert fence_lines[0].levelno == logging.WARNING
    assert "53 event(s) dropped" in fence_lines[0].getMessage()


@pytest.mark.asyncio
async def test_violation_stops_the_heartbeat():
    rec = _recorder()

    async def _forever():
        await asyncio.sleep(3600)

    rec._renew_task = asyncio.create_task(_forever())
    with patch.object(session_events_db, "append_events", side_effect=_fence_reject):
        await rec._append([{"type": "x"}])
    assert rec.superseded is True
    assert rec._renew_task is None


@pytest.mark.asyncio
async def test_hook_exception_does_not_escape():
    rec = _recorder()

    def _boom():
        raise RuntimeError("owner hook broke")

    rec.on_superseded = _boom
    with patch.object(session_events_db, "append_events", side_effect=_fence_reject):
        await rec._append([{"type": "x"}])
    assert rec.superseded is True


@pytest.mark.asyncio
async def test_healthy_recorder_unchanged(caplog):
    caplog.set_level(logging.WARNING, logger="app.core.turn_recorder")
    rec = _recorder()
    with patch.object(session_events_db, "append_events", return_value=[1, 2]) as append:
        await rec.start("hello")
        assert rec.superseded is False
        assert rec.superseded_dropped == 0
        assert append.call_count == 1
    assert not [r for r in caplog.records if "fence violation" in r.getMessage()]


@pytest.mark.asyncio
async def test_session_runtime_hook_cancels_pump_and_interrupts():
    from app.core import session_runtime as sr

    runtime = sr.SessionRuntime.__new__(sr.SessionRuntime)
    runtime.session_id = "sess-1"
    runtime.turn_id = "turn-1"
    runtime._abort_reason = None

    async def _pump():
        await asyncio.sleep(3600)

    pump = asyncio.create_task(_pump())
    runtime._pump_task = pump

    interrupted = []

    async def _fake_interrupt(sid):
        interrupted.append(sid)

    with patch("app.core.query_session_lifecycle.interrupt_session", new=_fake_interrupt):
        runtime._on_recorder_superseded()
        await asyncio.sleep(0)
        with pytest.raises(asyncio.CancelledError):
            await pump
        await asyncio.sleep(0)

    assert runtime._abort_reason == "fence_superseded"
    assert interrupted == ["sess-1"]
