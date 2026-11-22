"""The "x" on one running task of a chat that runs on another worker.

The chat's Stop was forwarded to the worker that runs the chat, but the "x"
on a single task was not: the viewing worker tried to stop it on its own
read-only mirror, had nothing to stop, and answered "Couldn't stop the task".
Two in-process workers over the real signed control endpoint.
"""
import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest

from app.api import sessions as sessions_api
from app.api import task_stop_owner
from app.core import query_engine
from app.core.task_registry import task_registry
from app.db import database
from app.network import catalog, control, engine, transport
from tests.core.network.harness import _ACTIVE, converge, join
from tests.core.network.test_transport import ServedBy

SESSION = "s-owned-there"
STOP_PATH = "/api/v1/network/peer/control/" + task_stop_owner.ACTION


@pytest.fixture
def pair(workers, monkeypatch):
    owner, mirror = workers("owner"), workers("mirror")
    with owner.active():
        engine.create_network("Home", device_name="ServerWorker")
    join(owner, mirror, name="Laptop")
    with owner.active():
        database.create_session(SESSION, "default", title="Runs on ServerWorker", is_active=True)
        catalog.publish_owned()
    converge([owner, mirror])
    served = ServedBy(owner, "http://owner.local:8000")
    real_async = httpx.AsyncClient

    def client(*args, **kwargs):
        kwargs["transport"] = served.transport()
        return real_async(**kwargs)
    monkeypatch.setattr(httpx, "AsyncClient", client)
    transport._links[owner.device_id] = transport.PeerLink(owner.device_id, [served.base_url])

    # Which worker ran the stop: the registry is one per process here, so a
    # stop on the mirror would also find the owner's row.
    ran_on = []
    real_stop = query_engine.stop_session_task

    async def spy(session_id, task_id, **kwargs):
        ran_on.append(_ACTIVE.get().name)
        return await real_stop(session_id, task_id, **kwargs)
    monkeypatch.setattr(query_engine, "stop_session_task", spy)

    async def require_session(request, session_id):
        return database.get_session(session_id)
    monkeypatch.setattr(sessions_api, "require_session", require_session)
    try:
        yield owner, mirror, served, ran_on
    finally:
        task_registry.clear_session(SESSION)


def press_x(task_id):
    """The endpoint the "x" calls, as the viewing worker answers it."""
    return asyncio.run(sessions_api.stop_session_background_task(None, SESSION, task_id, token="t"))


def test_the_x_on_a_mirrored_chat_stops_the_task_where_it_runs(pair):
    owner, mirror, served, ran_on = pair
    with owner.active():
        task_registry.start_task(SESSION, "shell-1", "shell", "npm run dev")
    with mirror.active():
        assert control.owner_of(database.get_session(SESSION)) == owner.device_id
        result = press_x("shell-1")
    assert result["ok"] is True, result
    assert result["session_id"] == SESSION and result["task_id"] == "shell-1"
    assert ran_on == ["owner"]
    assert ("POST", STOP_PATH) in served.requests
    with owner.active():
        assert task_registry.get_task(SESSION, "shell-1")["status"] == "killed"


def test_a_chat_on_this_worker_is_stopped_here(pair):
    owner, mirror, served, ran_on = pair
    with owner.active():
        task_registry.start_task(SESSION, "shell-2", "shell", "sleep 900")
        result = press_x("shell-2")
    assert result["ok"] is True, result
    assert ran_on == ["owner"]
    assert ("POST", STOP_PATH) not in served.requests


def test_an_owner_that_cannot_be_reached_is_named(pair, monkeypatch):
    owner, mirror, served, ran_on = pair
    monkeypatch.setattr(control, "call", AsyncMock(
        side_effect=control.RunnerUnreachable("Couldn't reach ServerWorker from Laptop (connection refused).")))
    with mirror.active():
        result = press_x("shell-3")
    assert result["ok"] is False
    assert result["message"] == ("Couldn't stop the task. "
                                 "Couldn't reach ServerWorker from Laptop (connection refused).")
    assert ran_on == []  # never "stopped" on the mirror


def test_an_older_owner_is_asked_to_update(pair, monkeypatch):
    owner, mirror, served, ran_on = pair
    original = control._LAZY_HANDLERS.pop(task_stop_owner.ACTION)
    try:
        with mirror.active():
            result = press_x("shell-4")
    finally:
        control._LAZY_HANDLERS[task_stop_owner.ACTION] = original
    assert result["ok"] is False
    assert "ServerWorker needs an update to stop a single task" in result["message"]
    assert ran_on == []


def test_the_owner_refuses_a_stop_for_a_chat_it_does_not_run(pair):
    owner, mirror, served, ran_on = pair
    with mirror.active():
        with pytest.raises(PermissionError, match="does not own"):
            asyncio.run(control.handle(task_stop_owner.ACTION, {"session_id": SESSION, "task_id": "x"},
                                       from_device=owner.device_id))
        with pytest.raises(ValueError, match="Invalid task id"):
            asyncio.run(control.handle(task_stop_owner.ACTION, {"session_id": SESSION, "task_id": ""},
                                       from_device=owner.device_id))
    assert ran_on == []
