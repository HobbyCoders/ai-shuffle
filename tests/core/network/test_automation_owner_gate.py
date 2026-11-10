"""An automation runs only on the worker that owns it, whatever starts it (#1397).

Two in-process workers share one automation, owned by worker A. Its
connector lives on worker B, so B is where the connector's trigger arrives.
Every start path goes through the real ``AutomationManager`` entry points;
only the run itself (``_execute_run``) is a stand-in that records the call.
"""
import asyncio

import pytest

from app.core import automation_manager as manager_mod
from app.core.automation_manager import AutomationManager
from app.db import database
from app.network import engine, ownership
from tests.core.network.harness import converge, join

AUTOMATION = "auto-owner-1397"
CONNECTOR = "conn-owner-1397"


@pytest.fixture
def pair(workers):
    a, b = workers("worker-a"), workers("worker-b")
    with a.active():
        engine.create_network("Home", device_name="Worker A")
    join(a, b, name="Worker B")
    with a.active():
        database.create_connector(CONNECTOR, "webhook", "Hook", {"mode": "inbound", "inbound_path": "h"},
                                  [], is_active=True)
        ownership.assign("connectors", CONNECTOR, b.device_id)  # the connector lives on B ...
        database.create_automation(
            automation_id=AUTOMATION, name="Triage", trigger_type="connector_trigger",
            trigger_config={"trigger_id": "new-issue", "connector_id": CONNECTOR},
            prompt="triage it", profile_id="default", enabled=True,
            triggers=[
                {"type": "connector_trigger", "config": {"trigger_id": "new-issue", "connector_id": CONNECTOR}},
                {"type": "connector_event", "config": {"event_type": "message_received",
                                                       "connector_id": CONNECTOR}},
                {"type": "webhook", "config": {}},
            ],
        )
        ownership.assign("automations", AUTOMATION, a.device_id)  # ... the automation belongs to A
        engine.scan_all()
    converge([a, b])
    return a, b


class _Recorder:
    """A manager whose runs are recorded instead of executed."""

    def __init__(self, monkeypatch):
        self.manager = AutomationManager()
        self.runs = []
        self.tasks = []

        async def execute_run(automation, run_id, rendered_prompt, trigger_data, **_kw):
            self.runs.append(trigger_data["trigger"])

        real_create = self.manager._create_tracked_task

        def create(coro, *, name):
            task = real_create(coro, name=name)
            self.tasks.append(task)
            return task

        monkeypatch.setattr(self.manager, "_execute_run", execute_run)
        monkeypatch.setattr(self.manager, "_create_tracked_task", create)

    async def settle(self):
        while self.tasks:
            batch, self.tasks = self.tasks, []
            await asyncio.gather(*batch)


async def _fire_event_paths(rec):
    """Connector trigger, connector event and webhook: the paths with no caller to answer."""
    await rec.manager.handle_connector_trigger(CONNECTOR, "new-issue", {"title": "bug"})
    await rec.settle()
    await rec.manager.handle_connector_event("message_received", {"connector_id": CONNECTOR})
    await rec.settle()
    await rec.manager.trigger_webhook(AUTOMATION, {"x": 1}, None)
    await rec.settle()


async def test_a_trigger_on_a_worker_that_does_not_own_the_automation_starts_nothing(pair, monkeypatch, caplog):
    a, b = pair
    rec = _Recorder(monkeypatch)
    with b.active():  # B holds the connector, A owns the automation
        assert database.get_automation(AUTOMATION)["owner_device_id"] == a.device_id
        with caplog.at_level("WARNING", logger=manager_mod.logger.name):
            await _fire_event_paths(rec)
        assert rec.runs == []
        # Dropped on purpose, not half-started: no run row on B to count against its budget.
        assert database.get_automation_runs(AUTOMATION) == []
        dropped = [r.getMessage() for r in caplog.records if AUTOMATION in r.getMessage()]
        assert len(dropped) == 3 and all(a.device_id in m and "Worker A" in m for m in dropped)


async def test_a_manual_run_on_a_non_owner_names_the_owner(pair, monkeypatch):
    a, b = pair
    rec = _Recorder(monkeypatch)
    with b.active():
        with pytest.raises(manager_mod.AutomationNotOwnedError) as refused:
            await rec.manager.trigger_manual(AUTOMATION)
        assert "Worker A" in str(refused.value)
        await rec.settle()
        assert rec.runs == []


async def test_the_manual_run_route_on_a_non_owner_answers_409_naming_the_owner(pair):
    from fastapi import HTTPException
    from starlette.requests import Request

    from app.api import automations as automations_api
    a, b = pair
    request = Request({"type": "http", "method": "POST", "headers": [], "path": "/"})
    with b.active():
        with pytest.raises(HTTPException) as refused:
            await automations_api.trigger_automation(AUTOMATION, request, "owner")
    assert refused.value.status_code == 409
    assert "Worker A" in refused.value.detail and a.device_id in refused.value.detail


async def test_every_start_path_runs_on_the_owner(pair, monkeypatch):
    a, _b = pair
    rec = _Recorder(monkeypatch)
    with a.active():
        await _fire_event_paths(rec)
        assert await rec.manager.trigger_manual(AUTOMATION) == {"automation_id": AUTOMATION, "status": "triggered"}
        await rec.settle()
    assert rec.runs == ["connector_trigger", "connector_event", "webhook", "manual"]


async def test_an_approved_run_whose_automation_moved_away_is_cancelled(pair, monkeypatch):
    """The approval lives with the run, on the worker that admitted it. If the
    automation moved to another worker while it waited, it does not run here."""
    a, b = pair
    manager = AutomationManager()
    with a.active():
        database.update_automation(AUTOMATION, requires_approval=True)
        await manager.trigger_manual(AUTOMATION)
        await asyncio.sleep(0)
        for _ in range(50):
            pending = [r for r in database.get_automation_runs(AUTOMATION) if r["status"] == "pending"]
            if pending and not manager._running:
                break
            await asyncio.sleep(0.01)
        run = pending[0]
        approval = next(x for x in database.get_pending_automation_approvals() if x["run_id"] == run["id"])
        database.update_automation_approval(approval["id"], status="approved", decided_by="admin")
        database.update_automation_run(run["id"], status="approved")
        ownership.assign("automations", AUTOMATION, b.device_id)  # moved while it waited
        started = []

        async def no_session(*_a, **_kw):
            started.append(True)
            raise AssertionError("must not start a session for an automation owned elsewhere")

        monkeypatch.setattr(manager, "_run_prompt_as_session", no_session)
        assert await manager.execute_approved_run(approval["id"]) is not None
        for _ in range(50):
            row = database.get_automation_run(run["id"])
            if row["status"] != "approved":
                break
            await asyncio.sleep(0.01)
        assert row["status"] == "cancelled" and "Worker B" in (row.get("error") or "")
        assert started == []


async def test_outside_a_network_every_path_still_runs(workers, monkeypatch):
    solo = workers("worker-solo")
    rec = _Recorder(monkeypatch)
    with solo.active():
        assert not ownership.enrolled()
        database.create_automation(
            automation_id=AUTOMATION, name="Triage", trigger_type="webhook", trigger_config={},
            prompt="triage it", profile_id="default", enabled=True,
            triggers=[
                {"type": "connector_trigger", "config": {"trigger_id": "new-issue", "connector_id": CONNECTOR}},
                {"type": "connector_event", "config": {"event_type": "message_received",
                                                       "connector_id": CONNECTOR}},
                {"type": "webhook", "config": {}},
            ],
        )
        await _fire_event_paths(rec)
        await rec.manager.trigger_manual(AUTOMATION)
        await rec.settle()
    assert rec.runs == ["connector_trigger", "connector_event", "webhook", "manual"]
