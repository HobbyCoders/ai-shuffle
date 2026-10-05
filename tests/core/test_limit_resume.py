"""Resume when a usage limit resets (#1831).

Covers the reset-time extraction (structured windows first, the error text
as the fallback, with its time zone), the schedule (once per limit per chat,
staggered, never for a stopped, archived or moved chat), the fire (push back
while still limited, compact before the continuation, a worker continued for
its parent) and the cancels (Cancel, an owner message, the durable row across
a restart).
"""

from __future__ import annotations

import json
import random
import time
from datetime import datetime, timezone
from typing import Any, Dict, List
from unittest.mock import AsyncMock, patch

import pytest

from app.core import limit_resume as lr
from app.core.config import settings


# A fixed "now": 2026-10-04 18:00 America/New_York (22:00 UTC).
NOW = datetime(2026, 10, 4, 22, 0, tzinfo=timezone.utc).timestamp()


@pytest.fixture(autouse=True)
def _existing_resume_profile(mock_db):
    from app.db import database

    database.create_profile("pr", "Resume agent", None, {})


@pytest.fixture(autouse=True)
def _reset_wakeup_state():
    from app.core import wakeup_service

    yield
    for task in list(wakeup_service._timers.values()):
        task.cancel()
    wakeup_service._timers.clear()
    wakeup_service._retry_attempts.clear()


# ─── Reset time: the error text ─────────────────────────────────────────────


def test_text_with_a_time_and_zone_resolves_in_that_zone():
    info = lr.reset_from_text(
        "You've hit your session limit · resets 11:10pm (America/New_York)", now=NOW,
    )
    assert info is not None and info.source == "text" and info.tz == "America/New_York"
    # 23:10 New York (EDT, UTC-4) is 03:10 UTC the next day.
    assert info.at == datetime(2026, 10, 5, 3, 10, tzinfo=timezone.utc).timestamp()


def test_text_time_already_past_today_means_tomorrow():
    info = lr.reset_from_text("limit · resets 9am (America/New_York)", now=NOW)
    assert info is not None
    assert info.at == datetime(2026, 10, 5, 13, 0, tzinfo=timezone.utc).timestamp()


def test_text_with_a_date():
    info = lr.reset_from_text("weekly limit · resets Oct 6, 3pm (Europe/Berlin)", now=NOW)
    assert info is not None
    assert info.at == datetime(2026, 10, 6, 13, 0, tzinfo=timezone.utc).timestamp()


def test_text_without_a_zone_uses_the_local_zone(monkeypatch):
    monkeypatch.setattr(lr, "_local_zone", lambda: timezone.utc)
    info = lr.reset_from_text("resets 11:10pm", now=NOW)
    assert info is not None and info.tz is None
    assert info.at == datetime(2026, 10, 4, 23, 10, tzinfo=timezone.utc).timestamp()


def test_text_with_an_epoch():
    info = lr.reset_from_text("Claude AI usage limit reached|1791172200", now=NOW)
    assert info is not None and info.at == 1791172200.0


@pytest.mark.parametrize("text", [
    "", "rate limited, try later", "resets 3", "resets 25:00", "resets 13pm",
    # An epoch far in the past or months ahead is not believed.
    "usage limit reached|1600000000", "usage limit reached|1900000000",
])
def test_text_without_a_usable_reset(text):
    assert lr.reset_from_text(text, now=NOW) is None


# ─── Reset time: structured windows first ───────────────────────────────────


def _claude(monkeypatch, windows):
    monkeypatch.setattr(lr, "_claude_windows", lambda: windows)


def test_structured_claude_window_wins_over_the_text(monkeypatch):
    reset = NOW + 3600
    _claude(monkeypatch, [
        {"type": "five_hour", "utilization": 0.0, "resets_at": str(int(reset)),
         "observed_at": int((NOW - 30) * 1000)},
        {"type": "seven_day", "utilization": 40.0, "resets_at": str(int(NOW + 86400))},
    ])
    info = lr.find_reset(lr.CLAUDE_KEY, "resets 11:10pm (America/New_York)", now=NOW)
    assert info is not None and info.source == "structured"
    assert info.at == reset and info.windows == ("five_hour",)


def test_a_stale_exhausted_window_is_ignored(monkeypatch):
    _claude(monkeypatch, [
        {"type": "seven_day_overage_included", "utilization": 0.0,
         "resets_at": str(int(NOW + 5 * 86400)), "observed_at": int((NOW - 3 * 86400) * 1000)},
    ])
    assert lr.structured_reset(lr.CLAUDE_KEY, now=NOW) is None
    info = lr.find_reset(lr.CLAUDE_KEY, "resets 11:10pm (America/New_York)", now=NOW)
    assert info is not None and info.source == "text"


def test_structured_provider_windows_use_percent_used(monkeypatch):
    monkeypatch.setattr(lr, "_provider_windows", lambda pid: [
        {"type": "five_hour", "utilization": 100.0, "resets_at": str(int(NOW + 600))},
        {"type": "seven_day", "utilization": 99.0, "resets_at": str(int(NOW + 9000))},
    ])
    info = lr.structured_reset("prov-1", now=NOW)
    assert info is not None and info.at == NOW + 600 and info.windows == ("five_hour",)


def test_still_blocked_reads_only_the_windows_that_stopped_the_turn(monkeypatch):
    _claude(monkeypatch, [
        {"type": "five_hour", "utilization": 100.0, "resets_at": str(int(NOW - 10))},
        {"type": "seven_day_opus", "utilization": 0.0, "resets_at": str(int(NOW + 7200))},
    ])
    assert lr.still_blocked_until(lr.CLAUDE_KEY, ["five_hour"], now=NOW) is None
    assert lr.still_blocked_until(lr.CLAUDE_KEY, ["seven_day_opus"], now=NOW) == NOW + 7200
    assert lr.still_blocked_until(lr.CLAUDE_KEY, [], now=NOW) is None


# ─── Staggering ─────────────────────────────────────────────────────────────


def test_fire_time_is_after_the_reset_with_a_bounded_random_delay():
    reset = NOW + 3600
    for seed in range(20):
        at = lr.plan_fire_at(reset, now=NOW, taken=[], rng=random.Random(seed))
        assert reset + lr.GRACE_SECONDS <= at <= reset + lr.GRACE_SECONDS + lr.JITTER_SECONDS


def test_several_chats_due_together_are_staggered():
    reset = NOW + 3600
    rng = random.Random(0)
    taken: List[float] = []
    for _ in range(4):
        taken.append(lr.plan_fire_at(reset, now=NOW, taken=taken, rng=rng))
    gaps = [b - a for a, b in zip(sorted(taken), sorted(taken)[1:])]
    assert all(gap >= lr.STAGGER_SECONDS - lr.JITTER_SECONDS for gap in gaps)
    assert sorted(taken)[-1] >= reset + lr.GRACE_SECONDS + 3 * lr.STAGGER_SECONDS
    # A resume due hours later is not pushed back by these.
    far = lr.plan_fire_at(NOW + 5 * 3600, now=NOW, taken=taken, rng=random.Random(1))
    assert far <= NOW + 5 * 3600 + lr.GRACE_SECONDS + lr.JITTER_SECONDS


# ─── Scheduling ─────────────────────────────────────────────────────────────


class _Env:
    """Patches around on_limit_stop / fire: one local delegated chat."""

    def __init__(self, monkeypatch, *, session=None, switches=None, leg="delegated"):
        self.session = session if session is not None else {
            "id": "s1", "profile_id": "pr", "project_id": "pj",
            "instance_id": settings.effective_instance_id, "owner_device_id": None,
        }
        self.recorded: List[tuple] = []
        self.handled: set = set()
        self.archived: set = set()
        from app.core import archive_guard
        from app.db import database

        monkeypatch.setattr(database, "get_session", lambda sid: dict(self.session, id=sid)
                            if self.session else None)
        monkeypatch.setattr(lr, "session_leg", lambda session: leg)
        monkeypatch.setattr(lr, "get_settings", lambda: dict(
            lr.DEFAULTS, **(switches if switches is not None else {"resume_on_reset": True})))
        monkeypatch.setattr(lr, "handled_limit_keys", lambda sid: set(self.handled))
        monkeypatch.setattr(archive_guard, "is_archived_async",
                            AsyncMock(side_effect=lambda sid: sid in self.archived))

        async def record(sid, status, data):
            self.recorded.append((sid, status, dict(data)))
            if data.get("limit_key"):
                self.handled.add(data["limit_key"])

        monkeypatch.setattr(lr, "_record", record)


TERMINAL = {"api_error_status": 429, "is_error": True,
            "errors": ["You've hit your session limit · resets 11:10pm (America/New_York)"]}
TURN = {"id": "t1", "profile_id": "pr", "project_id": "pj", "overrides": {"model": "opus"}}


def _no_structured(monkeypatch):
    monkeypatch.setattr(lr, "structured_reset", lambda key, now=None: None)


async def test_schedules_one_durable_resume_and_records_it(monkeypatch):
    env = _Env(monkeypatch)
    _no_structured(monkeypatch)
    started = time.time()
    result = await lr.on_limit_stop("s1", TURN, TERMINAL)
    assert result is not None
    from app.db import wakeups as wdb

    [row] = wdb.get_session_wakeups("s1", lr.LIMIT_RESUME_SOURCE)
    meta = lr.meta_of(row)
    assert meta["provider"] == lr.CLAUDE_KEY and meta["compact"] is True
    assert meta["reset_source"] == "text" and meta["tz"] == "America/New_York"
    assert row["model"] == "opus" and row["prompt"] == lr.RESUME_PROMPT
    assert row["fire_at"] >= meta["reset_at"] + lr.GRACE_SECONDS - 1
    assert meta["reset_at"] > started
    [(sid, status, data)] = env.recorded
    assert sid == "s1" and status == "scheduled" and data["resume_at"] == row["fire_at"]


async def test_the_same_limit_is_handled_once(monkeypatch):
    env = _Env(monkeypatch)
    _no_structured(monkeypatch)
    assert await lr.on_limit_stop("s1", TURN, TERMINAL) is not None
    # The resume ran into the same limit again: not scheduled a second time.
    assert await lr.on_limit_stop("s1", TURN, TERMINAL) is None
    assert [s for _, s, _ in env.recorded] == ["scheduled"]


@pytest.mark.parametrize("case", ["off", "stopped", "archived", "moved", "worker_without_owner"])
async def test_never_scheduled_when_it_must_not_resume(monkeypatch, case):
    from app.core.task_registry import task_registry
    from app.db import wakeups as wdb

    session = {"id": "s1", "profile_id": "pr", "instance_id": settings.effective_instance_id}
    switches = {"resume_on_reset": case != "off"}
    if case == "moved":
        session["instance_id"] = "another-install"
    if case == "worker_without_owner":
        session["parent_session_id"] = "parent"
    env = _Env(monkeypatch, session=session, switches=switches)
    _no_structured(monkeypatch)
    if case == "archived":
        env.archived.add("s1")
    if case == "stopped":
        task_registry.mark_user_stopped("s1")
    try:
        assert await lr.on_limit_stop("s1", TURN, TERMINAL) is None
    finally:
        task_registry.clear_user_stop("s1")
    assert wdb.get_session_wakeups("s1", lr.LIMIT_RESUME_SOURCE) == []
    assert env.recorded == []


async def test_no_reset_time_says_so_and_schedules_nothing(monkeypatch):
    env = _Env(monkeypatch)
    _no_structured(monkeypatch)
    terminal = {"api_error_status": 429, "errors": ["usage limit reached"]}
    assert await lr.on_limit_stop("s1", TURN, terminal) is None
    assert [s for _, s, _ in env.recorded] == ["no_reset"]


async def test_a_resume_that_hits_the_limit_again_is_not_resumed_again(monkeypatch):
    """The resume is the one try: if the continuation is stopped by a usage
    limit again (a new reset minute, so a new limit key), nothing more is
    scheduled and the chat says so."""
    from app.db import wakeups as wdb

    env = _Env(monkeypatch)
    _no_structured(monkeypatch)
    resumed = dict(TURN, payload={lr.PAYLOAD_KEY: {"limit_key": "claude:1"}})
    assert await lr.on_limit_stop("s1", resumed, TERMINAL) is None
    assert wdb.get_session_wakeups("s1", lr.LIMIT_RESUME_SOURCE) == []
    [(sid, status, data)] = env.recorded
    assert sid == "s1" and status == "no_reset"
    assert "will not resume by itself" in data["content"]


async def test_compact_first_only_on_a_leg_that_compacts(monkeypatch):
    _Env(monkeypatch, leg="voice")
    _no_structured(monkeypatch)
    # Voice has no provider key at all: nothing scheduled.
    assert await lr.on_limit_stop("s1", TURN, TERMINAL) is None


# ─── Firing ─────────────────────────────────────────────────────────────────


def _wake_row(meta: Dict[str, Any], session_id="s1"):
    base = {"provider": lr.CLAUDE_KEY, "leg": "delegated", "windows": ["five_hour"],
            "limit_key": "claude:1", "compact": True, "owner": None, "pushbacks": 0}
    base.update(meta)
    return {"id": "wake-x", "session_id": session_id, "prompt": lr.RESUME_PROMPT,
            "profile_id": "pr", "project_id": "pj", "model": "opus",
            "reason": json.dumps(base), "source": lr.LIMIT_RESUME_SOURCE}


async def test_fire_compacts_then_continues_in_queue_order(monkeypatch):
    env = _Env(monkeypatch)
    monkeypatch.setattr(lr, "still_blocked_until", lambda *a, **k: None)
    submit = AsyncMock(side_effect=["turn-compact", "turn-resume"])
    with patch("app.core.turn_manager.submit_async", submit), \
            patch("app.core.wakeup_service._session_api_user_id", lambda sid: None):
        turn_id = await lr.fire(_wake_row({}))
    assert turn_id == "turn-resume"
    prompts = [c.kwargs["prompt"] for c in submit.await_args_list]
    assert prompts == [lr.COMPACT_PROMPT, lr.RESUME_PROMPT]
    assert all(c.args[1] == "wakeup" for c in submit.await_args_list)
    assert all(c.kwargs["overrides"] == {"model": "opus"} for c in submit.await_args_list)
    # Both turns are marked as the resume, so the usage-limit hold lets them
    # through and a limit they hit is not resumed again.
    assert all(lr.PAYLOAD_KEY in c.kwargs["payload"] for c in submit.await_args_list)
    assert [s for _, s, _ in env.recorded] == ["resumed"]
    assert env.recorded[0][2]["compacted_first"] is True


async def test_fire_without_compact_only_continues(monkeypatch):
    _Env(monkeypatch)
    monkeypatch.setattr(lr, "still_blocked_until", lambda *a, **k: None)
    submit = AsyncMock(return_value="turn-resume")
    with patch("app.core.turn_manager.submit_async", submit), \
            patch("app.core.wakeup_service._session_api_user_id", lambda sid: None):
        await lr.fire(_wake_row({"compact": False}))
    assert [c.kwargs["prompt"] for c in submit.await_args_list] == [lr.RESUME_PROMPT]


async def test_fire_pushes_back_while_the_limit_is_still_in_force(monkeypatch):
    env = _Env(monkeypatch)
    later = time.time() + 1800
    monkeypatch.setattr(lr, "still_blocked_until", lambda *a, **k: later)
    submit = AsyncMock()
    with patch("app.core.turn_manager.submit_async", submit):
        assert await lr.fire(_wake_row({})) is None
    submit.assert_not_awaited()
    from app.db import wakeups as wdb

    [row] = wdb.get_session_wakeups("s1", lr.LIMIT_RESUME_SOURCE)
    assert lr.meta_of(row)["pushbacks"] == 1
    assert row["fire_at"] >= later + lr.GRACE_SECONDS - 1
    assert [s for _, s, _ in env.recorded] == ["deferred"]


async def test_fire_stops_pushing_back_after_the_cap(monkeypatch):
    _Env(monkeypatch)
    monkeypatch.setattr(lr, "still_blocked_until", lambda *a, **k: time.time() + 60)
    submit = AsyncMock(return_value="turn-resume")
    with patch("app.core.turn_manager.submit_async", submit), \
            patch("app.core.wakeup_service._session_api_user_id", lambda sid: None):
        await lr.fire(_wake_row({"pushbacks": lr.MAX_PUSHBACKS, "compact": False}))
    submit.assert_awaited_once()


@pytest.mark.parametrize("case", ["stopped", "archived", "moved"])
async def test_fire_drops_a_chat_that_must_not_resume(monkeypatch, case):
    from app.core.task_registry import task_registry

    session = {"id": "s1", "instance_id": settings.effective_instance_id}
    if case == "moved":
        session["instance_id"] = "another-install"
    env = _Env(monkeypatch, session=session)
    if case == "archived":
        env.archived.add("s1")
    if case == "stopped":
        task_registry.mark_user_stopped("s1")
    submit = AsyncMock()
    try:
        with patch("app.core.turn_manager.submit_async", submit):
            assert await lr.fire(_wake_row({})) is None
    finally:
        task_registry.clear_user_stop("s1")
    submit.assert_not_awaited()
    statuses = [s for _, s, _ in env.recorded]
    assert statuses == ([] if case == "moved" else ["dropped"])


async def test_fire_continues_a_paused_worker_for_its_parent(monkeypatch):
    env = _Env(monkeypatch)
    monkeypatch.setattr(lr, "still_blocked_until", lambda *a, **k: None)
    on_message = AsyncMock(return_value="turn-w2")
    with patch("app.core.turn_scheduler.on_agent_message", on_message):
        turn_id = await lr.fire(_wake_row({"owner": "parent-1", "compact": False}, session_id="w1"))
    assert turn_id == "turn-w2"
    call = on_message.await_args
    assert call.args[:3] == ("w1", lr.RESUME_SENDER, lr.WORKER_RESUME_TEXT)
    assert call.kwargs["reply_to"] == "parent-1"
    assert lr.PAYLOAD_KEY in call.kwargs["extra_payload"]
    assert sorted((sid, s) for sid, s, _ in env.recorded) == [
        ("parent-1", "resumed"), ("w1", "resumed")]
    parent_row = next(d for sid, _, d in env.recorded if sid == "parent-1")
    assert parent_row["task_id"] == "w1"


def test_a_resumed_workers_report_carries_one_note_to_its_parent():
    assert lr.report_note({lr.PAYLOAD_KEY: {"limit_key": "k"}}) == lr.WORKER_REPORT_NOTE
    assert lr.report_note({"agent_message_reply_to": "p"}) is None
    assert lr.report_note(None) is None


# ─── Cancel, owner messages, restart ────────────────────────────────────────


async def _schedule_row(session_id="s1", fire_in=3600.0, owner=None):
    from app.core import wakeup_service

    meta = {"provider": lr.CLAUDE_KEY, "leg": "delegated", "windows": [], "limit_key": "claude:9",
            "compact": True, "owner": owner, "pushbacks": 0}
    return await wakeup_service.schedule(
        session_id=session_id, profile_id="pr", project_id="pj", prompt=lr.RESUME_PROMPT,
        delay_seconds=fire_in, reason=json.dumps(meta), source=lr.LIMIT_RESUME_SOURCE,
    )


async def test_cancel_removes_the_resume_and_records_it(monkeypatch):
    env = _Env(monkeypatch)
    await _schedule_row()
    assert (await lr.cancel("s1")) is True
    from app.db import wakeups as wdb

    assert wdb.get_session_wakeups("s1", lr.LIMIT_RESUME_SOURCE) == []
    assert [s for _, s, _ in env.recorded] == ["cancelled"]
    assert (await lr.cancel("s1")) is False


async def test_an_owner_message_cancels_the_scheduled_resume(monkeypatch):
    from app.core import turn_manager
    from app.db import wakeups as wdb

    await _schedule_row()
    monkeypatch.setattr("app.db.turns.enqueue_turn", lambda *a, **k: {"id": "t-user"})
    monkeypatch.setattr("app.core.archive_guard.is_archived", lambda sid: False)
    turn_manager.submit("s1", "user", prompt="hello", no_kick=True)
    assert wdb.get_session_wakeups("s1", lr.LIMIT_RESUME_SOURCE) == []


async def test_a_resume_survives_a_restart(monkeypatch):
    from app.core import wakeup_service

    row = await _schedule_row(fire_in=-5)
    # Process restart: in-memory timers are gone, the row is not.
    for task in list(wakeup_service._timers.values()):
        task.cancel()
    wakeup_service._timers.clear()
    fired: List[str] = []

    async def fake_fire(r):
        fired.append(r["session_id"])

    monkeypatch.setattr(lr, "fire", fake_fire)
    monkeypatch.setattr("app.core.archive_guard.is_archived_async", AsyncMock(return_value=False))
    assert await wakeup_service.rehydrate_pending() >= 1
    task = wakeup_service._timers[row["id"]]
    await task
    assert fired == ["s1"]
    from app.db import wakeups as wdb

    # The fire claimed the row: it cannot fire a second time.
    assert wdb.get_session_wakeups("s1", lr.LIMIT_RESUME_SOURCE) == []


async def test_the_park_watchdog_leaves_a_parent_of_a_paused_worker_alone():
    await _schedule_row(session_id="w1", owner="parent-1")
    assert lr.paused_workers_of("parent-1") == ["w1"]
    assert lr.paused_workers_of("someone-else") == []


async def test_cancelling_a_paused_worker_settles_its_row(monkeypatch):
    env = _Env(monkeypatch)
    await _schedule_row(session_id="w1", owner="parent-1")
    settled = AsyncMock()
    monkeypatch.setattr(lr, "_settle_cancelled_worker", settled)
    assert await lr.cancel("w1") is True
    settled.assert_awaited_once_with("parent-1", "w1")
    assert sorted((sid, s) for sid, s, _ in env.recorded) == [
        ("parent-1", "cancelled"), ("w1", "cancelled")]


# ─── The wake-up service routes the row here ────────────────────────────────


async def test_wakeup_fire_hands_a_limit_resume_to_this_module(monkeypatch):
    from app.core import wakeup_service

    row = await _schedule_row(fire_in=3600)
    fire = AsyncMock(return_value="t")
    monkeypatch.setattr(lr, "fire", fire)
    monkeypatch.setattr("app.core.archive_guard.is_archived_async", AsyncMock(return_value=False))
    submit = AsyncMock()
    with patch("app.core.turn_manager.submit_async", submit):
        await wakeup_service._fire(row)
    fire.assert_awaited_once()
    submit.assert_not_awaited()
    assert wakeup_service.LIMIT_RESUME_SOURCE == lr.LIMIT_RESUME_SOURCE


# ─── Holding automatic turns while the limit is in force ────────────────────


def _held(monkeypatch, errors, *, ended=NOW, status=429):
    from app.core import turn_scheduler
    from app.db import database

    monkeypatch.setattr(lr, "_last_settled_turn", lambda sid: {"id": "t1", "ended_at": ended})
    monkeypatch.setattr(turn_scheduler, "_turn_terminal_payload", lambda sid, tid: {
        "api_error_status": status, "is_error": True, "errors": errors})
    monkeypatch.setattr(database, "get_session", lambda sid: None)
    _no_structured(monkeypatch)


def test_hold_lasts_until_the_stated_reset(monkeypatch):
    _held(monkeypatch, ["You've hit your session limit · resets 11:10pm (America/New_York)"])
    reset = datetime(2026, 10, 5, 3, 10, tzinfo=timezone.utc).timestamp()
    assert lr.hold_until("s1", now=NOW + 60) == reset
    assert lr.hold_until("s1", now=reset + 1) is None


def test_hold_without_a_reset_time_uses_the_fallback(monkeypatch):
    _held(monkeypatch, ["provider returned HTTP 429: Usage limit reached: Rate limited"])
    assert lr.hold_until("s1", now=NOW + 60) == NOW + lr.HOLD_FALLBACK_SECONDS
    assert lr.hold_until("s1", now=NOW + lr.HOLD_FALLBACK_SECONDS + 1) is None


@pytest.mark.parametrize("errors,status", [
    (["provider returned HTTP 429: Rate limit reached. Please try again in 1s."], 429),
    (["You've hit your session limit · resets 11:10pm"], 500),
])
def test_no_hold_without_a_usage_limit(monkeypatch, errors, status):
    _held(monkeypatch, errors, status=status)
    assert lr.hold_until("s1", now=NOW + 60) is None


# ─── Settings ───────────────────────────────────────────────────────────────


def test_settings_default_off_with_compact_on():
    assert lr.get_settings() == {"resume_on_reset": False, "compact_before_resume": True}


def test_settings_update_and_validation():
    out = lr.update_settings(resume_on_reset=True)
    assert out == {"resume_on_reset": True, "compact_before_resume": True}
    assert lr.get_settings()["resume_on_reset"] is True
    with pytest.raises(ValueError):
        lr.update_settings()
    with pytest.raises(ValueError):
        lr.update_settings(resume_on_reset="yes")


def _legacy(key: str, **switches: bool) -> None:
    from app.db import database

    database.set_system_setting(lr.LEGACY_SETTING_PREFIX + key, json.dumps(switches))


@pytest.mark.parametrize("rows,expected", [
    # One provider had resume on: it is on for every provider now.
    ({"claude": {"resume_on_reset": False}, "prov-1": {"resume_on_reset": True}},
     {"resume_on_reset": True, "compact_before_resume": True}),
    # Nobody turned resume on; compact was on (by default) for some provider.
    ({"prov-1": {"resume_on_reset": False, "compact_before_resume": False}},
     {"resume_on_reset": False, "compact_before_resume": True}),
    # Every provider turned compact off.
    ({"claude": {"compact_before_resume": False}},
     {"resume_on_reset": False, "compact_before_resume": False}),
])
def test_per_provider_switches_are_folded_in_once(monkeypatch, rows, expected):
    from app.db import database

    monkeypatch.setattr(lr, "provider_keys", lambda: ["claude"])
    for key, switches in rows.items():
        _legacy(key, **switches)
    assert lr.get_settings() == expected
    # Written once as the global row; the per-provider rows are gone, so they
    # are never read as overrides again.
    assert json.loads(database.get_system_setting(lr.SETTING_KEY)) == expected
    assert lr._legacy_settings() == {}
    _legacy("claude", resume_on_reset=not expected["resume_on_reset"])
    database.invalidate_system_setting_memo()
    assert lr.get_settings() == expected
