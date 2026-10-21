"""Regressions for update waiting-state follow-ups."""
import asyncio
import time
from types import SimpleNamespace

import pytest

from app.core import update_workers as fleet, desktop_lifecycle
from tests.test_update_workers import setup, busy, updates, B
from tests.test_update_waiting_safety import service


@pytest.mark.parametrize("mode", ["stop", "when_idle"])
@pytest.mark.parametrize("peer", [False, True])
async def test_inflight_stop_accepts_duplicate_without_starting_another_restart(busy, setup, monkeypatch, mode, peer):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []
    async def stop(sid, **kwargs):
        calls.append(sid)
        entered.set()
        await release.wait()
        busy.clear()
    monkeypatch.setattr(desktop_lifecycle, "stop_session_work", stop)
    monkeypatch.setattr(fleet, "STOP_WAIT_SECONDS", 0)
    monkeypatch.setattr(fleet.engine, "context", lambda: SimpleNamespace(member=lambda _: SimpleNamespace(kind="worker", status="active")))
    await fleet._local_action("install", "first-stop", "stop")
    await asyncio.wait_for(entered.wait(), 2)
    try:
        if peer:
            result = await fleet.peer_action({"operation": "action", "action": "install", "mode": mode,
                                               "request_id": "duplicate", "expires_at": time.time() + 10}, "peer")
        else:
            result = await fleet._local_action("install", "duplicate", mode)
        assert result["accepted"] is True
        assert result["install_state"] == "stopping"
        assert "already in progress" in result["reason"]
        assert len(fleet._stop_tasks) == 1
        assert fleet._deferred is None
    finally:
        release.set()
        await asyncio.gather(*list(fleet._stop_tasks))
        if fleet._deferred:
            task = fleet._deferred["task"]
            fleet.local_action("cancel", "cleanup")
            await asyncio.gather(task, return_exceptions=True)
    assert calls == ["s1"]
    assert updates._desktop_request["action"] == "install", "the original stop must still finish"


def test_retained_idle_download_has_actionable_role_without_installing(setup):
    row = {"phase": "idle", "supported": True, "allow_updates": True, "staged_version": B,
           "version": "a" * 12, "running_version": "a" * 12, "wait_reason": "updater_idle"}
    assert fleet.classify(row, True) == {"role": "available", "label": "Check for updates before restarting"}
    assert fleet.classify({**row, "request_pending": True}, True) == {
        "role": "preparing", "label": "Request queued; waiting for the updater"}
    updates.desktop_exchange({"phase": "idle", "staged_version": B})
    assert fleet.local_action("install", "not-ready")["accepted"] is False
    assert updates._desktop_request is None


@pytest.mark.parametrize("mode", [None, "stop", "when_idle"])
async def test_mixed_staged_and_retained_idle_fleet_checks_before_restart(setup, monkeypatch, mode):
    from unittest.mock import AsyncMock
    values, _ = setup
    values[updates.ALL_KEY] = "true"
    rows = [
        {"device_id": "self", "device_name": "Local", "is_self": True,
         "phase": "ready", "supported": True, "allow_updates": True, "staged_version": B},
        {"device_id": "peer", "device_name": "Peer", "is_self": False,
         "phase": "idle", "supported": True, "allow_updates": True,
         "staged_version": B, "wait_reason": "updater_idle"},
    ]
    for row in rows:
        row.update(fleet.classify(row, True))
    plan = fleet.plan(rows)
    assert plan["action"] == "check"
    assert plan["ready"] is False
    assert plan["available"] is False
    assert "Check for updates" in plan["held"] and "Peer" in plan["held"]
    monkeypatch.setattr(fleet, "snapshot", AsyncMock(return_value={"workers": rows, "plan": plan}))
    act = AsyncMock(return_value={"accepted": True})
    monkeypatch.setattr(fleet, "_act", act)
    if mode:
        with pytest.raises(fleet.UpdateNotSent, match="Check for updates.*Peer"):
            await fleet._send("install", mode)
        act.assert_not_awaited()
    else:
        result = await fleet._send("install")
        assert result["sent"] == "check"
        assert "Peer" in result["held"]
        assert act.await_count == 1
        assert act.await_args.args[:2] == (rows[1], "check")
    assert updates._desktop_request is None
    assert fleet.progress.load() is None


@pytest.mark.parametrize("age,session,since_delta,expected", [
    (0, "chat", -10, "still_busy_after_stop"),
    (300, "chat", -1000, "still_busy_after_stop"),
    (660, "chat", -1000, "running_work"),
    (10, "chat", 0, "running_work"),
    (0, "different-chat", -10, "running_work"),
])
def test_failed_stop_reason_applies_only_to_recent_same_work(service, monkeypatch, age, session, since_delta, expected):
    from datetime import datetime, timezone
    service("node preview.js --port 5173")
    now = time.time()
    rows = desktop_lifecycle.busy_work()
    rows[0]["task_details"][0]["started_at"] = datetime.fromtimestamp(now + since_delta, timezone.utc).isoformat()
    monkeypatch.setattr(desktop_lifecycle, "busy_work", lambda: rows)
    monkeypatch.setattr(fleet, "_stop_result", {"accepted": False, "completed_at": now - age,
                                              "busy": [{"session_id": session}]})
    assert fleet.waiting_status({"phase": "ready", "staged_version": B})["wait_reason"] == expected
