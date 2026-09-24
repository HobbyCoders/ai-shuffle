"""Tests for the wakeup model override (pending_wakeups.model).

A ScheduleWakeup wake used to fire on the profile's DEFAULT model even when the
scheduling turn ran with a per-query model override — re-priming the prompt cache
on the wrong model. The scheduling turn's effective model is now persisted on the
wakeup row and replayed as the wake TURN's ``overrides={"model": ...}``.

Idle-watchdog wakes (``source="idle_watchdog"``) stay model-less by design.
"""

from tests.conftest import async_twin
import json
import time
from unittest.mock import patch

import pytest

from app.core.config import settings


@pytest.fixture(autouse=True)
def _reset_watchdog_state():
    from app.core import wakeup_service
    wakeup_service._watchdog_cooldown.clear()
    yield
    wakeup_service._watchdog_cooldown.clear()
    for task in list(wakeup_service._timers.values()):
        task.cancel()
    wakeup_service._timers.clear()


def _usage(total, max_tokens=200_000):
    """A raw SDK context-usage payload with an explicit ~92% autocompact edge."""
    return {
        "totalTokens": total,
        "maxTokens": max_tokens,
        "isAutoCompactEnabled": True,
        "autoCompactThreshold": int(max_tokens * 0.92),
    }


# ---------------------------------------------------------------------------
# persistence
# ---------------------------------------------------------------------------

def test_create_wakeup_persists_model(mock_db):
    from app.db import wakeups as wdb

    iid = settings.effective_instance_id
    now = time.time()
    returned = wdb.create_wakeup(id="wm", session_id="s1", prompt="p",
                                 fire_at=now, instance_id=iid, model="haiku")
    assert returned["model"] == "haiku"

    rows = {r["id"]: r for r in wdb.get_pending_wakeups(iid)}
    assert rows["wm"]["model"] == "haiku"


def test_create_wakeup_model_defaults_to_null(mock_db):
    from app.db import wakeups as wdb

    iid = settings.effective_instance_id
    returned = wdb.create_wakeup(id="wn", session_id="s1", prompt="p",
                                 fire_at=time.time(), instance_id=iid)
    assert returned["model"] is None

    rows = {r["id"]: r for r in wdb.get_pending_wakeups(iid)}
    assert rows["wn"]["model"] is None


async def test_schedule_persists_model(mock_db, monkeypatch):
    from app.core import wakeup_service
    from app.db import wakeups as wdb

    monkeypatch.setattr(wakeup_service, "_arm", lambda row, delay: None)

    row = await wakeup_service.schedule(
        session_id="s1", profile_id="pr", project_id="pj",
        prompt="hello", delay_seconds=120, model="haiku",
    )

    assert row["model"] == "haiku"
    persisted = {
        r["id"]: r
        for r in wdb.get_pending_wakeups(settings.effective_instance_id)
    }
    assert persisted[row["id"]]["model"] == "haiku"


# ---------------------------------------------------------------------------
# firing: the override replay
# ---------------------------------------------------------------------------

def _capture_submit(monkeypatch):
    """Spy on ``turn_manager.submit`` — the seam a wake now fires through."""
    from app.core import turn_manager

    calls = []

    def _fake_submit(session_id, source, **kwargs):
        calls.append({"session_id": session_id, "source": source, **kwargs})
        return f"turn-{len(calls)}"

    monkeypatch.setattr(turn_manager, "submit", _fake_submit)
    monkeypatch.setattr(turn_manager, "submit_async", async_twin(_fake_submit))
    return calls


async def test_fire_with_model_passes_override(mock_db, monkeypatch):
    from app.core import wakeup_service
    from app.db import wakeups as wdb

    calls = _capture_submit(monkeypatch)

    wdb.create_wakeup(id="w1", session_id="s1", prompt="p1", fire_at=time.time(),
                      instance_id=settings.effective_instance_id,
                      profile_id="pr", project_id="pj", model="haiku")
    row = {"id": "w1", "session_id": "s1", "prompt": "p1", "profile_id": "pr",
           "project_id": "pj", "source": "schedule_wakeup", "model": "haiku"}

    await wakeup_service._fire(row)

    assert len(calls) == 1
    assert calls[0]["source"] == "wakeup"
    assert calls[0]["overrides"] == {"model": "haiku"}
    assert calls[0]["prompt"] == "p1"
    assert calls[0]["session_id"] == "s1"
    assert calls[0]["profile_id"] == "pr"
    assert calls[0]["project_id"] == "pj"


async def test_fire_without_model_passes_no_overrides(mock_db, monkeypatch):
    from app.core import wakeup_service
    from app.db import wakeups as wdb

    calls = _capture_submit(monkeypatch)

    wdb.create_wakeup(id="w2", session_id="s2", prompt="p2", fire_at=time.time(),
                      instance_id=settings.effective_instance_id,
                      profile_id="pr", project_id="pj")
    row = {"id": "w2", "session_id": "s2", "prompt": "p2", "profile_id": "pr",
           "project_id": "pj", "source": "idle_watchdog", "model": None}

    await wakeup_service._fire(row)

    assert len(calls) == 1
    assert calls[0]["source"] == "wakeup"
    assert calls[0]["overrides"] is None


# ---------------------------------------------------------------------------
# the watchdog stays model-less
# ---------------------------------------------------------------------------

async def test_watchdog_arms_model_less_row(mock_db, monkeypatch):
    from app.core import wakeup_service
    from app.db import database
    from app.db import wakeups as wdb

    sid = "wd-model-less"
    database.create_session(session_id=sid, profile_id="default")
    database.set_session_context_usage(session_id=sid, data=_usage(190_000))

    # let the real schedule() run (so a real row lands) but never fire a query
    monkeypatch.setattr(wakeup_service, "_arm", lambda row, delay: None)

    nudged = await wakeup_service.scan_once()

    assert nudged == [sid]
    rows = [
        r for r in wdb.get_pending_wakeups(settings.effective_instance_id)
        if r["session_id"] == sid
    ]
    assert len(rows) == 1
    assert rows[0]["source"] == "idle_watchdog"
    assert rows[0]["model"] is None


# ---------------------------------------------------------------------------
# the ScheduleWakeup builtin forwards the baked-in model
# ---------------------------------------------------------------------------

def _build_schedule_wakeup_handler(session_id, profile_id, model):
    """Return the real ScheduleWakeup handler from a built builtins server.

    Mirrors ``tests/core/test_task_registry.py::_build_task_list_handler``:
    spy on the lazily-imported ``create_sdk_mcp_server`` to capture the tool
    objects so the handler can be invoked without an MCP transport.
    """
    import claude_agent_sdk as sdk
    import app.mcp.builtin_tools as bt

    captured = {}
    real_create = sdk.create_sdk_mcp_server

    def _spy(name, version, tools):
        captured["tools"] = tools
        return real_create(name=name, version=version, tools=tools)

    with patch.object(sdk, "create_sdk_mcp_server", side_effect=_spy):
        bt.create_builtin_tools_server(
            session_id=session_id, profile_id=profile_id, project_id=None,
            model=model,
        )

    tools = {t.name: t for t in captured["tools"]}
    assert "ScheduleWakeup" in tools, "ScheduleWakeup tool was not registered"
    return tools["ScheduleWakeup"].handler


def _parse_envelope(result):
    return json.loads(result["content"][0]["text"])


async def test_schedule_wakeup_tool_forwards_model(mock_db, monkeypatch):
    from app.core import wakeup_service

    captured = []

    async def _fake_schedule(**kwargs):
        captured.append(kwargs)
        return {"id": "wake-x", "fire_at": 123.0}

    monkeypatch.setattr(wakeup_service, "schedule", _fake_schedule)

    handler = _build_schedule_wakeup_handler("s1", "pr", "haiku")
    payload = _parse_envelope(
        await handler({"delaySeconds": 120, "prompt": "continue"})
    )

    assert payload["success"] is True
    assert payload["model"] == "haiku"
    assert len(captured) == 1
    assert captured[0]["model"] == "haiku"
    assert captured[0]["source"] == "schedule_wakeup"


async def test_schedule_wakeup_tool_without_model(mock_db, monkeypatch):
    from app.core import wakeup_service

    captured = []

    async def _fake_schedule(**kwargs):
        captured.append(kwargs)
        return {"id": "wake-y", "fire_at": 123.0}

    monkeypatch.setattr(wakeup_service, "schedule", _fake_schedule)

    handler = _build_schedule_wakeup_handler("s1", "pr", None)
    await handler({"delaySeconds": 120, "prompt": "continue"})

    assert captured and captured[0]["model"] is None


@pytest.mark.parametrize("mode,expect_notice", [("subscription", True), ("api_key", False)])
async def test_schedule_wakeup_tool_recommends_an_api_key_in_subscription_mode(
    mock_db, monkeypatch, mode, expect_notice
):
    from app.core import claude_auth_mode, loop_guard, wakeup_service

    async def _fake_schedule(**kwargs):
        return {"id": "wake-z", "fire_at": 123.0}

    monkeypatch.setattr(wakeup_service, "schedule", _fake_schedule)
    claude_auth_mode.set_mode(mode)
    handler = _build_schedule_wakeup_handler("s1", "pr", None)
    payload = _parse_envelope(await handler({"delaySeconds": 120, "prompt": "continue"}))
    assert payload["success"] is True
    if expect_notice:
        assert payload["notice"] == loop_guard.SCHEDULED_RUN_GUIDANCE
    else:
        assert "notice" not in payload
