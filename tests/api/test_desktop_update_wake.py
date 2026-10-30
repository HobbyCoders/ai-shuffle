"""Cancellation and auth on the desktop's read-only wake control route."""
import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from app.api import internal
from app.core import application_updates as updates


@pytest.fixture
def wake(monkeypatch):
    monkeypatch.setenv("SHUFFLE_DESKTOP_LIFECYCLE_TOKEN", "synthetic-control-token")
    monkeypatch.setattr(updates, "_desktop_wake_waiter", None)
    monkeypatch.setattr(updates, "_desktop_wake_generation", 0)


def request(ingress="local", authorization="Bearer synthetic-control-token", receive=None):
    return SimpleNamespace(scope={"shuffle.ingress": ingress}, headers={"authorization": authorization}, receive=receive)


@pytest.mark.asyncio
@pytest.mark.parametrize("ingress,authorization", [("remote", "Bearer synthetic-control-token"), ("local", "wrong"), ("local", "")])
async def test_wake_uses_the_existing_desktop_auth_boundary(wake, ingress, authorization):
    with pytest.raises(HTTPException) as caught:
        await internal.desktop_update_wake(internal.DesktopUpdateWake(generation=0), request(ingress, authorization))
    assert caught.value.status_code == 403
    assert updates._desktop_wake_waiter is None


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["disconnect", "shutdown-cancel"])
async def test_disconnect_and_shutdown_cancel_all_wait_tasks_and_free_the_slot(wake, monkeypatch, ending):
    messages = asyncio.Queue()
    registered = asyncio.Event()
    original = updates.desktop_update_wake

    async def wait(generation):
        registered.set()
        return await original(generation)

    monkeypatch.setattr(updates, "desktop_update_wake", wait)
    before = asyncio.all_tasks()
    task = asyncio.create_task(internal.desktop_update_wake(internal.DesktopUpdateWake(generation=0), request(receive=messages.get)))
    await registered.wait()
    assert updates._desktop_wake_waiter is not None
    if ending == "disconnect":
        messages.put_nowait({"type": "http.disconnect"})
        assert (await task).status_code == 204
    else:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert updates._desktop_wake_waiter is None
    assert not (asyncio.all_tasks() - before)
    assert await original(0, timeout=0) == {"generation": 0}
