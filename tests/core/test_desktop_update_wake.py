"""A wake is a bounded hint; the existing bridge still consumes each command."""
import asyncio

import pytest
from app.core import application_updates as updates


@pytest.fixture
def bridge(monkeypatch):
    monkeypatch.setattr(updates, "allowed", lambda: True)
    monkeypatch.setattr(updates, "control_root", lambda: None)
    for name, value in [("_desktop_request", None), ("_desktop_followup", None),
                        ("_desktop_channel_request", None), ("_desktop_wake_waiter", None),
                        ("_desktop_wake_generation", 0), ("_desktop_state", {}), ("_desktop_seen", 0)]:
        monkeypatch.setattr(updates, name, value, raising=False)
    updates.desktop_exchange({"phase": "available"})


@pytest.mark.asyncio
async def test_enqueued_web_action_wakes_without_a_heartbeat_tick_and_is_consumed_once(bridge):
    waiter = asyncio.create_task(updates.desktop_update_wake(0))
    await asyncio.sleep(0)
    updates.enqueue("download")
    assert await asyncio.wait_for(waiter, 1) == {"generation": 1}
    assert updates.desktop_exchange({"phase": "available"})["action"] == "download"
    assert updates.desktop_exchange({"phase": "available"}) == {}


@pytest.mark.asyncio
async def test_one_waiter_limit_cancellation_and_timeout_leave_no_subscription(bridge):
    waiter = asyncio.create_task(updates.desktop_update_wake(0))
    await asyncio.sleep(0)
    with pytest.raises(ValueError, match="already"):
        await updates.desktop_update_wake(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert updates._desktop_wake_waiter is None
    assert await updates.desktop_update_wake(0, timeout=0) == {"generation": 0}
    assert updates._desktop_wake_waiter is None


@pytest.mark.asyncio
async def test_wake_cannot_extend_expiry_or_take_an_action_away_from_a_busy_shell(bridge, monkeypatch):
    updates.enqueue("download")
    assert await updates.desktop_update_wake(0) == {"generation": 1}
    expires = updates._desktop_request["expires_at"]
    assert updates.desktop_exchange({"phase": "checking"}) == {}
    assert updates._desktop_request["expires_at"] == expires
    monkeypatch.setattr(updates.time, "time", lambda: expires + 1)
    assert updates.desktop_exchange({"phase": "available"}) == {}


@pytest.mark.asyncio
async def test_channel_request_wakes_the_same_control_path(bridge, monkeypatch):
    monkeypatch.setattr(updates, "channel_status", lambda: {"editable": True, "channel": "stable"})
    updates.set_channel("nightly")
    assert await updates.desktop_update_wake(0) == {"generation": 1}
    reply = updates.desktop_exchange({"phase": "idle", "channel_config": {"editable": True, "channel": "stable"}})
    assert reply["action"] == "set_channel" and reply["channel"] == "nightly"
