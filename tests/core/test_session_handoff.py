"""Agent-queued hand-off to another worker, run when the session is idle."""
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from app.core import session_handoff as sh

DIRECTORY = {"runners": [
    {"device_id": "dev-serverworker", "device_name": "Unraid", "online": True, "is_self": True},
    {"device_id": "dev-gaming", "device_name": "DesktopWorker", "online": True, "is_self": False},
    {"device_id": "dev-laptop", "device_name": "Laptop", "online": False, "is_self": False},
]}


@pytest.fixture(autouse=True)
def _clean():
    sh._pending.clear()
    sh._running.clear()
    with patch.object(sh, "_workers", return_value=DIRECTORY):
        yield
    sh._pending.clear()


def test_resolves_by_name_or_device_id_and_refuses_offline_or_self():
    assert sh.resolve_worker("desktopworker")["device_id"] == "dev-gaming"
    assert sh.resolve_worker("dev-gaming")["device_name"] == "DesktopWorker"
    with pytest.raises(ValueError, match="offline"):
        sh.resolve_worker("Laptop")
    with pytest.raises(ValueError, match="No other worker named 'Unraid'"):
        sh.resolve_worker("Unraid")
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
