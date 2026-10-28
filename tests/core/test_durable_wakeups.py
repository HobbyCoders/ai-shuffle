"""Tests for #598: durable wakeups.

Wakeup persistence round-trip (create / list / claim / rehydrate / re-arm),
firing through the turn queue, and supersession. The idle-at-edge watchdog
these tests once covered is retired (tests/core/test_compact_then_continue.py
pins that it no longer sends ``/compact`` turns).
"""

from tests.conftest import async_twin
import time

import pytest

from app.core.config import settings


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _usage(total, max_tokens=200_000):
    """A raw SDK context-usage payload with an explicit ~92% autocompact edge."""
    return {
        "totalTokens": total,
        "maxTokens": max_tokens,
        "isAutoCompactEnabled": True,
        "autoCompactThreshold": int(max_tokens * 0.92),
    }


@pytest.fixture(autouse=True)
def _existing_wake_profile(mock_db):
    from app.db import database

    database.create_profile("pr", "Wake agent", None, {})


@pytest.fixture(autouse=True)
def _reset_wakeup_timers():
    from app.core import wakeup_service
    yield
    for task in list(wakeup_service._timers.values()):
        task.cancel()
    wakeup_service._timers.clear()


# ---------------------------------------------------------------------------
# wakeup persistence round-trip
# ---------------------------------------------------------------------------

def test_wakeup_persistence_round_trip(mock_db):
    from app.db import wakeups as wdb

    iid = settings.effective_instance_id
    now = time.time()
    wdb.create_wakeup(id="w1", session_id="s1", prompt="p1", fire_at=now - 10,
                      instance_id=iid, source="schedule_wakeup")
    wdb.create_wakeup(id="w2", session_id="s2", prompt="p2", fire_at=now + 1000,
                      instance_id=iid, source="other_producer")

    pending = wdb.get_pending_wakeups(iid)
    assert {p["id"] for p in pending} == {"w1", "w2"}
    # oldest fire first
    assert pending[0]["id"] == "w1"
    assert wdb.session_has_pending("s1")
    assert wdb.session_has_pending("s2", source="other_producer")
    assert not wdb.session_has_pending("s2", source="schedule_wakeup")

    # claim is a one-shot: wins once, then loses (idempotent double-fire guard)
    assert wdb.claim_wakeup("w1") is True
    assert wdb.claim_wakeup("w1") is False
    assert not wdb.session_has_pending("s1")

    # scoped to this install
    assert wdb.get_pending_wakeups("some-other-install") == []


def test_delete_session_wakeups_scoped_by_source(mock_db):
    from app.db import wakeups as wdb

    iid = settings.effective_instance_id
    now = time.time()
    wdb.create_wakeup(id="a", session_id="s1", prompt="p", fire_at=now,
                      instance_id=iid, source="schedule_wakeup")
    wdb.create_wakeup(id="b", session_id="s1", prompt="p", fire_at=now,
                      instance_id=iid, source="other_producer")

    assert wdb.delete_session_wakeups("s1", source="other_producer") == 1
    remaining = wdb.get_pending_wakeups(iid)
    assert {r["id"] for r in remaining} == {"a"}
    assert wdb.delete_session_wakeups("s1") == 1
    assert wdb.get_pending_wakeups(iid) == []


async def test_rehydrate_rearms_pending(mock_db, monkeypatch):
    from app.core import wakeup_service
    from app.db import wakeups as wdb

    iid = settings.effective_instance_id
    now = time.time()
    wdb.create_wakeup(id="w1", session_id="s1", prompt="p1", fire_at=now - 10,
                      instance_id=iid)
    wdb.create_wakeup(id="w2", session_id="s2", prompt="p2", fire_at=now + 9_999,
                      instance_id=iid)

    armed = []
    monkeypatch.setattr(wakeup_service, "_arm",
                        lambda row, delay: armed.append((row["id"], delay)))

    n = await wakeup_service.rehydrate_pending()

    assert n == 2
    by = dict(armed)
    # overdue → armed immediately (stagger index 0 == 0s)
    assert by["w1"] == 0.0
    # future → keeps its remaining delay
    assert by["w2"] > 9_000


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


async def test_fire_claims_row_and_skips_double_fire(mock_db, monkeypatch):
    from app.core import wakeup_service
    from app.db import wakeups as wdb

    calls = _capture_submit(monkeypatch)

    wdb.create_wakeup(id="w1", session_id="s1", prompt="p1", fire_at=time.time(),
                      instance_id=settings.effective_instance_id,
                      profile_id="pr", project_id="pj")
    row = {"id": "w1", "session_id": "s1", "prompt": "p1",
           "profile_id": "pr", "project_id": "pj", "source": "schedule_wakeup"}

    await wakeup_service._fire(row)
    assert len(calls) == 1
    assert calls[0]["source"] == "wakeup"
    assert calls[0]["prompt"] == "p1"
    assert calls[0]["session_id"] == "s1"
    assert calls[0]["profile_id"] == "pr"
    assert calls[0]["project_id"] == "pj"

    # row already claimed/deleted → no second turn is enqueued
    await wakeup_service._fire(row)
    assert len(calls) == 1


async def test_fire_falls_back_to_session_profile(mock_db, monkeypatch):
    """A row with no profile borrows the session's rather than dropping."""
    from app.core import wakeup_service
    from app.db import database, wakeups as wdb

    calls = _capture_submit(monkeypatch)
    database.create_session(session_id="sess-prof", profile_id="default")
    wdb.create_wakeup(id="wp", session_id="sess-prof", prompt="p",
                      fire_at=time.time(),
                      instance_id=settings.effective_instance_id)
    row = {"id": "wp", "session_id": "sess-prof", "prompt": "p",
           "profile_id": None, "project_id": None, "source": "schedule_wakeup"}

    await wakeup_service._fire(row)

    assert len(calls) == 1
    assert calls[0]["profile_id"] == "default"


async def test_fire_drops_wake_with_no_resolvable_profile(mock_db, monkeypatch, caplog):
    from app.core import wakeup_service
    from app.db import database, wakeups as wdb
    from app.db import turns as turns_db

    calls = _capture_submit(monkeypatch)
    database.create_session(session_id="sess-noprof", profile_id=None)
    wdb.create_wakeup(id="wn", session_id="sess-noprof", prompt="p",
                      fire_at=time.time(),
                      instance_id=settings.effective_instance_id)
    row = {"id": "wn", "session_id": "sess-noprof", "prompt": "p",
           "profile_id": None, "project_id": None, "source": "schedule_wakeup"}

    with caplog.at_level("WARNING", logger="app.core.wakeup_service"):
        await wakeup_service._fire(row)

    assert calls == []
    assert turns_db.get_queued_turns("sess-noprof") == []
    assert any("dropping the wake" in r.message for r in caplog.records)


async def test_fire_while_turn_live_queues_instead_of_racing(mock_db):
    """The whole point of the fix: a wake that lands mid-turn QUEUES.

    No spy here — the real ``turn_manager.submit`` runs, so the assertion is on
    the durable ``turns`` table: a second row in ``queued`` behind the live one,
    which the scheduler drains at settlement, rather than a concurrent agent
    loop against the same session.
    """
    from app.core import wakeup_service
    from app.db import database, wakeups as wdb
    from app.db import turns as turns_db

    sid = "sess-live"
    database.create_session(session_id=sid, profile_id="default")
    live = turns_db.enqueue_turn(sid, "user", prompt="do work")
    turns_db.start_turn(live["id"])
    assert turns_db.get_live_turn(sid)["state"] == "running"

    wdb.create_wakeup(id="wl", session_id=sid, prompt="wake up",
                      fire_at=time.time(), profile_id="pr",
                      instance_id=settings.effective_instance_id)
    await wakeup_service._fire(
        {"id": "wl", "session_id": sid, "prompt": "wake up",
         "profile_id": "pr", "project_id": None, "source": "schedule_wakeup"}
    )

    # the user turn is still the ONE live turn; the wake sits behind it
    assert turns_db.get_live_turn(sid)["id"] == live["id"]
    queued = turns_db.get_queued_turns(sid)
    assert len(queued) == 1
    assert queued[0]["source"] == "wakeup"
    assert queued[0]["state"] == "queued"


async def test_schedule_replaces_outstanding_wake_for_same_source(mock_db):
    from app.core import wakeup_service
    from app.db import wakeups as wdb

    first = await wakeup_service.schedule(
        session_id="s-dup", profile_id="pr", project_id=None,
        prompt="one", delay_seconds=600, source="schedule_wakeup",
    )
    second = await wakeup_service.schedule(
        session_id="s-dup", profile_id="pr", project_id=None,
        prompt="two", delay_seconds=600, source="schedule_wakeup",
    )

    rows = [r for r in wdb.get_pending_wakeups(settings.effective_instance_id)
            if r["session_id"] == "s-dup"]
    assert len(rows) == 1
    assert rows[0]["id"] == second["id"]
    assert rows[0]["prompt"] == "two"

    # exactly one armed timer, and it is the survivor's
    live = {k: t for k, t in wakeup_service._timers.items() if not t.done()}
    assert set(live) == {second["id"]}
    assert first["id"] not in live

    for task in list(wakeup_service._timers.values()):
        task.cancel()


async def test_schedule_keeps_a_different_source_armed(mock_db):
    from app.core import wakeup_service
    from app.db import wakeups as wdb

    await wakeup_service.schedule(
        session_id="s-mix", profile_id="pr", project_id=None,
        prompt="agent", delay_seconds=600, source="schedule_wakeup",
    )
    await wakeup_service.schedule(
        session_id="s-mix", profile_id="pr", project_id=None,
        prompt="nudge", delay_seconds=600, source="other_producer",
    )

    rows = [r for r in wdb.get_pending_wakeups(settings.effective_instance_id)
            if r["session_id"] == "s-mix"]
    assert {r["source"] for r in rows} == {"schedule_wakeup", "other_producer"}

    for task in list(wakeup_service._timers.values()):
        task.cancel()


async def test_user_turn_supersedes_pending_wakeups(mock_db):
    from app.core import turn_manager, wakeup_service
    from app.db import wakeups as wdb

    sid = "s-supersede"
    row = await wakeup_service.schedule(
        session_id=sid, profile_id="pr", project_id=None,
        prompt="later", delay_seconds=600, source="schedule_wakeup",
    )
    assert wdb.session_has_pending(sid)
    timer = wakeup_service._timers[row["id"]]

    turn_manager.submit(sid, "user", prompt="actually, do this", profile_id="pr")

    assert not wdb.session_has_pending(sid)
    assert row["id"] not in wakeup_service._timers
    timer.cancel()


async def test_server_turn_does_not_supersede_pending_wakeups(mock_db):
    from app.core import turn_manager, wakeup_service
    from app.db import wakeups as wdb

    sid = "s-keep"
    row = await wakeup_service.schedule(
        session_id=sid, profile_id="pr", project_id=None,
        prompt="later", delay_seconds=600, source="schedule_wakeup",
    )

    turn_manager.submit(sid, "automation", prompt="scheduled job", profile_id="pr")

    assert wdb.session_has_pending(sid)
    assert row["id"] in wakeup_service._timers
    wakeup_service._timers[row["id"]].cancel()


async def test_schedule_persists_then_arms(mock_db, monkeypatch):
    from app.core import wakeup_service
    from app.db import wakeups as wdb

    armed = []
    monkeypatch.setattr(wakeup_service, "_arm",
                        lambda row, delay: armed.append((row["id"], delay)))

    row = await wakeup_service.schedule(
        session_id="s1", profile_id="pr", project_id="pj",
        prompt="hello", delay_seconds=120, source="schedule_wakeup",
    )

    # persisted BEFORE arming (durable-first)
    assert wdb.session_has_pending("s1")
    assert armed and armed[0][0] == row["id"] and armed[0][1] == 120


# ---------------------------------------------------------------------------
# session deletion tidies up
# ---------------------------------------------------------------------------

async def test_session_delete_drops_pending_wakeups(mock_db):
    from app.core import wakeup_service
    from app.db import database
    from app.db import wakeups as wdb

    sid = "s-doomed"
    database.create_session(session_id=sid, profile_id="default")
    row = await wakeup_service.schedule(
        session_id=sid, profile_id="pr", project_id=None,
        prompt="later", delay_seconds=600, source="schedule_wakeup",
    )
    assert wdb.session_has_pending(sid)

    database.delete_session(sid)

    assert not wdb.session_has_pending(sid)
    assert row["id"] not in wakeup_service._timers


# ---------------------------------------------------------------------------
# usage arithmetic still shared with the coordinator hand-off
# ---------------------------------------------------------------------------

def test_usage_fraction_pure():
    from app.core import compact_continue

    assert compact_continue.usage_fraction(_usage(100_000, 200_000)) == 0.5
    assert compact_continue.usage_fraction({"totalTokens": 0, "maxTokens": 0}) is None
    assert compact_continue.usage_fraction(None) is None
