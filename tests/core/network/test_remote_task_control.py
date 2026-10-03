"""task-start over the real signed control endpoint between two workers."""
import asyncio

import pytest

from app.core import remote_task, turn_scheduler
from app.db import database
from app.db import network as store
from app.db.base import get_db
from app.network import control, engine, transport
from tests.core.network.harness import converge, join
from tests.core.network.test_session_active_control import connect_owner


@pytest.fixture
def kicks(monkeypatch):
    seen = []
    monkeypatch.setattr(turn_scheduler, "kick", seen.append)
    return seen


def network(workers, monkeypatch):
    """A ("Unraid") runs the child; B ("Windows") runs the parent chat."""
    a, b = workers("unraid"), workers("windows")
    served = connect_owner(monkeypatch, a)
    with a.active():
        engine.create_network("Home", device_name="Unraid")
    join(a, b, name="Windows")
    with b.active():
        database.create_session(session_id="caller", profile_id="default", project_id=None,
                                title="Windows chat", is_active=True, permission_mode="acceptEdits")
        engine.scan_all()
    converge([a, b])
    with b.active():
        store.touch_member(a.device_id, endpoints=[served.base_url])
    transport.note_contact(a.device_id)
    return a, b, served


def dispatch(a, b, **changes):
    fields = dict(
        worker={"device_id": a.device_id, "device_name": "Unraid"},
        caller_row=database.get_session("caller"), caller_session_id="caller", root_session_id="caller",
        depth=1, profile_id="default", agent_name="Default", result_agent="default", provider_id=None,
        model_id="sonnet", effort=None, prompt="Check the disk.", label="Task · Default",
        roster_name=None, mode="background",
    )
    fields.update(changes)
    return asyncio.run(remote_task.dispatch_remote(**fields))


def test_parent_starts_a_child_on_the_other_worker(workers, monkeypatch, kicks):
    a, b, served = network(workers, monkeypatch)
    with b.active():
        result = dispatch(a, b)
    assert result["ok"] is True, result
    assert result["worker"] == {"device_id": a.device_id, "name": "Unraid"}
    assert result["checkout"] == {"mode": "none", "branch": None}
    assert result["permission_mode"] == "acceptEdits"
    assert ("POST", "/api/v1/network/peer/control/task-start") in served.requests
    child = result["worker_session_id"]
    with a.active():
        row = database.get_session(child)
        assert row["owner_device_id"] == a.device_id and row["is_worker"] and row["hidden"]
        assert row["parent_session_id"] == "caller" and row["away_mode"]
        owed = remote_task.report_owed(child)
        assert owed["device_id"] == b.device_id and owed["owner_session_id"] == "caller"
    with b.active():
        # Nothing about the child is recorded on the parent's worker yet (S7).
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) AS n FROM turns WHERE session_id=%s", (child,))
            assert cursor.fetchone()["n"] == 0
    assert kicks == [child]


def test_a_target_refusal_comes_back_by_name(workers, monkeypatch, kicks):
    a, b, _ = network(workers, monkeypatch)
    with b.active():
        result = dispatch(a, b, profile_id="nobody")
    assert result == {"ok": False, "error": (
        "Unraid refused the subagent: The agent 'nobody' does not exist on this worker; nothing was "
        "started; nothing was dispatched.")}
    assert kicks == []


def test_an_older_target_is_refused_as_older(workers, monkeypatch, kicks):
    a, b, _ = network(workers, monkeypatch)
    monkeypatch.setattr(control, "known_action", lambda action: action != remote_task.TASK_START_ACTION)
    with b.active():
        result = dispatch(a, b)
    assert result["ok"] is False and "it runs an older release" in result["error"]
    with a.active():
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) AS n FROM turns WHERE source='provider_agent'")
            assert cursor.fetchone()["n"] == 0
