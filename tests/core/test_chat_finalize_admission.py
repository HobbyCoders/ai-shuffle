"""A chat still owns session-wide cleanup after its engine stream ends."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from app.core import session_runtime
from app.core.provider import session_admission as admission
from app.db import turns
from tests.core.test_session_runtime import _runtime_env

SID = "chat-finalize-admission"


def pump(**kwargs):
    async def events(lease):
        admission.turn_ended(lease)
        yield {"type": "done", "metadata": {}}

    lease = kwargs.get("admission_lease")
    return events(lease) if lease is not None else admission.stream_turn(SID, events)


async def test_direct_successor_waits_until_chat_finalization_has_finished(mock_db):
    turn = turns.enqueue_turn(SID, "user", prompt="hello")
    finalizing, finish_cleanup = asyncio.Event(), asyncio.Event()
    attempted, admitted = asyncio.Event(), asyncio.Event()

    async def persist(*args, **kwargs):
        finalizing.set()
        await finish_cleanup.wait()

    async def direct_turn():
        attempted.set()
        lease = await admission.acquire(SID)
        try:
            admitted.set()
        finally:
            admission.close(lease)

    with _runtime_env(pump=pump), patch(
        "app.core.turn_finalize.persist_turn_end_state", side_effect=persist,
    ):
        runtime = asyncio.create_task(session_runtime.SessionRuntime(turn).execute())
        successor = None
        try:
            await asyncio.wait_for(finalizing.wait(), 5)
            successor = asyncio.create_task(direct_turn())
            await asyncio.wait_for(attempted.wait(), 5)
            assert not admitted.is_set(), "a successor can start workers before the previous turn tears them down"
        finally:
            finish_cleanup.set()
            assert await asyncio.wait_for(runtime, 5) == "completed"
            if successor is not None:
                await asyncio.wait_for(successor, 5)
        assert admitted.is_set()
    assert SID not in admission._holders


async def test_busy_direct_turn_keeps_chat_queued_without_session_cleanup(mock_db):
    turn = turns.enqueue_turn(SID, "user", prompt="hello")
    direct = await admission.acquire(SID)
    try:
        with _runtime_env(pump=pump) as env, patch(
            "app.core.provider_agent_supervisor.teardown_workers", new=AsyncMock(),
        ) as teardown:
            outcome = await session_runtime.SessionRuntime(turn).execute()
        assert outcome is None
        assert turns.get_turn(turn["id"])["state"] == "queued"
        teardown.assert_not_awaited()
        env["recorder"].start.assert_not_awaited()
    finally:
        admission.close(direct)


@pytest.mark.parametrize("cancel", [False, True], ids=["error", "cancelled"])
async def test_chat_releases_admission_when_pump_ends_abnormally(mock_db, cancel):
    entered = asyncio.Event()
    turn = turns.enqueue_turn(SID, "user", prompt="hello")

    async def failed_pump(**kwargs):
        entered.set()
        if cancel:
            await asyncio.Event().wait()
        raise RuntimeError("pump failed")
        yield  # turn the helper into an async iterator

    with _runtime_env(pump=failed_pump):
        runtime = asyncio.create_task(session_runtime.SessionRuntime(turn).execute())
        await asyncio.wait_for(entered.wait(), 5)
        if cancel:
            runtime.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(runtime, 5)
        else:
            assert await asyncio.wait_for(runtime, 5) == "aborted"
    assert SID not in admission._holders
