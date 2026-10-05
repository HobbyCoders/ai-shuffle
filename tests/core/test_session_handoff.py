"""Agent-queued hand-off to another worker, run when the session is idle."""
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from app.core import session_handoff as sh

DIRECTORY = {"runners": [
    {"device_id": "dev-unraid", "device_name": "Unraid", "online": True, "is_self": True},
    {"device_id": "dev-gaming", "device_name": "DesktopWorker", "online": True, "is_self": False},
    {"device_id": "dev-laptop", "device_name": "Laptop", "online": False, "is_self": False},
]}


@pytest.fixture(autouse=True)
def _clean():
    sh._pending.clear()
    sh._running.clear()
    sh._waiting.clear()
    with patch.object(sh, "_workers", return_value=DIRECTORY):
        yield
    sh._pending.clear()
    sh._waiting.clear()


def test_resolves_by_name_or_device_id_and_refuses_offline_or_self():
    assert sh.resolve_worker("desktopworker")["device_id"] == "dev-gaming"
    assert sh.resolve_worker("dev-gaming")["device_name"] == "DesktopWorker"
    with pytest.raises(ValueError, match="offline"):
        sh.resolve_worker("Laptop")
    with pytest.raises(ValueError, match="Unraid is this worker"):
        sh.resolve_worker("Unraid")
    with pytest.raises(ValueError, match=r"Unknown worker 'Desktop'.*DesktopWorker \(online\)"):
        sh.resolve_worker("Desktop")
    with pytest.raises(ValueError, match="Name the worker"):
        sh.resolve_worker("  ")


def test_request_queues_and_cancel_drops_it():
    result = sh.request("s1", "DesktopWorker", "  tests pass;\n next: wire the UI ")
    assert result["queued"] and result["worker"] == "DesktopWorker"
    assert sh.pending_for("s1").note == "tests pass; next: wire the UI"
    assert sh.cancel("s1") and sh.pending_for("s1") is None


async def test_runs_the_move_when_idle_and_records_the_outcome():
    sh.request("s1", "DesktopWorker", "halfway")
    move = AsyncMock()
    with patch.object(sh, "_busy", return_value=False), \
            patch.object(sh, "_record", AsyncMock()) as record, \
            patch("app.api.sessions.move_session_to", move), \
            patch("app.db.database.get_session", return_value={"id": "s1"}):
        outcome = await sh.run_pending("s1")
    move.assert_awaited_once_with("s1", {"id": "s1"}, "dev-gaming")
    assert outcome.startswith("Handed off to DesktopWorker")
    texts = [call.args[1] for call in record.await_args_list]
    assert texts[0] == "Handing off to DesktopWorker. Note: halfway"
    assert sh.pending_for("s1") is None


async def test_a_refused_move_is_reported_not_raised():
    sh.request("s1", "DesktopWorker")
    with patch.object(sh, "_busy", return_value=False), \
            patch.object(sh, "_record", AsyncMock()), \
            patch("app.api.sessions.move_session_to",
                  AsyncMock(side_effect=HTTPException(409, "project isn't checked out on DesktopWorker"))), \
            patch("app.db.database.get_session", return_value={"id": "s1"}):
        outcome = await sh.run_pending("s1")
    assert outcome == "Hand-off to DesktopWorker failed: project isn't checked out on DesktopWorker"


async def test_gives_up_when_the_session_stays_busy():
    sh.request("s1", "DesktopWorker")
    with patch.object(sh, "_busy", return_value=True), \
            patch.object(sh, "IDLE_TIMEOUT_S", 0), \
            patch.object(sh, "_record", AsyncMock()), \
            patch("app.api.sessions.move_session_to", AsyncMock()) as move:
        outcome = await sh.run_pending("s1")
    assert "still busy" in outcome
    move.assert_not_awaited()


async def test_nothing_queued_is_a_no_op():
    assert await sh.run_pending("nobody") is None


# #1819: the owner's move of a running or parked chat queues the same hand-off.

class _Busy:
    """A session that stays busy until ``idle`` is set."""
    def __init__(self):
        self.idle = False

    def __call__(self, _sid):
        return {"running": not self.idle, "tasks": []}


async def _settle(predicate, rounds=200):
    import asyncio
    for _ in range(rounds):
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return False


async def test_owner_move_is_queued_while_running_and_runs_when_the_turn_ends():
    busy = _Busy()
    move = AsyncMock()
    with patch.object(sh, "busy_state", busy), patch.object(sh, "POLL_S", 0.01), \
            patch.object(sh, "IDLE_TIMEOUT_S", 0), \
            patch.object(sh, "_record", AsyncMock()) as record, \
            patch("app.api.sessions.move_session_to", move), \
            patch("app.db.database.get_session", return_value={"id": "s1"}):
        result = await sh.queue_move("s1", "dev-gaming")
        assert result == {"queued": True, "worker": "DesktopWorker"}
        assert record.await_args_list[0].args[2] == {"status": "queued", "worker": "DesktopWorker", "by": "owner"}
        # The owner's move outlasts the agent's timeout: still waiting, not dropped.
        assert not await _settle(lambda: move.await_count, rounds=10)
        assert sh.pending_for("s1").by == "owner"
        state = await sh.owner_op("s1", "status")
        assert state["pending"] == {"worker": "DesktopWorker", "target_id": "dev-gaming", "by": "owner"}
        assert state["busy"]["running"] is True
        busy.idle = True
        assert await _settle(lambda: move.await_count)
    move.assert_awaited_once_with("s1", {"id": "s1"}, "dev-gaming")
    assert sh.pending_for("s1") is None


async def test_owner_move_of_an_idle_chat_runs_at_once():
    busy = _Busy()
    busy.idle = True
    move = AsyncMock()
    with patch.object(sh, "busy_state", busy), \
            patch.object(sh, "_record", AsyncMock()), \
            patch("app.api.sessions.move_session_to", move), \
            patch("app.db.database.get_session", return_value={"id": "s1"}):
        await sh.queue_move("s1", "DesktopWorker")
        assert await _settle(lambda: move.await_count)


async def test_cancel_stops_a_waiting_move_and_says_so():
    busy = _Busy()
    move = AsyncMock()
    with patch.object(sh, "busy_state", busy), patch.object(sh, "POLL_S", 0.01), \
            patch.object(sh, "_record", AsyncMock()) as record, \
            patch("app.api.sessions.move_session_to", move):
        await sh.queue_move("s1", "DesktopWorker")
        assert await _settle(lambda: "s1" in sh._waiting)
        assert await sh.owner_op("s1", "cancel") == {"cancelled": True}
        assert record.await_args_list[-1].args[2] == {"status": "cancelled", "worker": "DesktopWorker", "by": "owner"}
        assert await _settle(lambda: "s1" not in sh._running)
        busy.idle = True
        assert (await sh.owner_op("s1", "status"))["pending"] is None
    move.assert_not_awaited()


async def test_cancel_with_nothing_queued_still_clears_the_chat():
    with patch.object(sh, "_record", AsyncMock()) as record:
        assert await sh.cancel_move("s1") == {"cancelled": False}
    assert record.await_args.args[2]["status"] == "cancelled"


async def test_a_second_queued_move_is_refused_until_the_first_is_cancelled():
    with patch.object(sh, "busy_state", _Busy()), patch.object(sh, "_record", AsyncMock()):
        await sh.queue_move("s1", "DesktopWorker")
        with pytest.raises(ValueError, match="already queued"):
            await sh.queue_move("s1", "DesktopWorker")
        sh.cancel("s1")


async def test_stop_and_move_stops_the_work_first():
    stop = AsyncMock()
    with patch.object(sh, "busy_state", _Busy()), patch.object(sh, "_record", AsyncMock()), \
            patch("app.core.desktop_lifecycle.stop_session_work", stop):
        await sh.queue_move("s1", "DesktopWorker", stop=True)
        sh.cancel("s1")
    stop.assert_awaited_once_with("s1", source="session_handoff")


async def test_route_asks_the_owning_worker():
    from app.network import control
    call = AsyncMock(return_value={"queued": True, "worker": "DesktopWorker"})
    with patch.object(control, "owner_of", return_value="dev-unraid"), patch.object(control, "call", call):
        await sh.route({"id": "s1"}, "queue", "dev-gaming", stop=True)
    call.assert_awaited_once_with("dev-unraid", "session-move",
                                  {"session_id": "s1", "op": "queue", "target": "dev-gaming", "stop": True})


def test_busy_state_names_running_tasks():
    from app.core.task_registry import task_registry
    with patch("app.core.streaming_status.has_active_run", return_value=False), \
            patch.object(task_registry, "get_active", return_value=[{"subject": "Run tests"}, {"agent": "explore"}]):
        assert sh.busy_state("s1") == {"running": False, "tasks": ["Run tests", "explore"]}
