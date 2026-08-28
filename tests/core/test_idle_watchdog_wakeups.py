"""Tests for #598: durable wakeups + idle-at-edge watchdog + latch hardening.

Covers the three deliverable test areas:
  * the watchdog trigger condition (idle, over-fraction, correctly gated);
  * wakeup persistence round-trip (create / list / claim / rehydrate / re-arm);
  * the auto-continue latch fail-closed fallback paths in compact_continue.
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


def _age_session(session_id, seconds):
    """Backdate a session's ``updated_at`` by ``seconds`` (last-activity clock)."""
    from datetime import datetime, timedelta, timezone

    from app.db.base import get_cursor, get_db
    from app.db.compat import ph

    stamp = (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()
    _ph = ph()
    with get_db() as conn:
        get_cursor(conn).execute(
            f"UPDATE sessions SET updated_at = {_ph} WHERE id = {_ph}",
            (stamp, session_id),
        )


@pytest.fixture(autouse=True)
def _reset_watchdog_state():
    from app.core import wakeup_service
    wakeup_service._watchdog_cooldown.clear()
    yield
    wakeup_service._watchdog_cooldown.clear()
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
                      instance_id=iid, source="idle_watchdog")

    pending = wdb.get_pending_wakeups(iid)
    assert {p["id"] for p in pending} == {"w1", "w2"}
    # oldest fire first
    assert pending[0]["id"] == "w1"
    assert wdb.session_has_pending("s1")
    assert wdb.session_has_pending("s2", source="idle_watchdog")
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
                      instance_id=iid, source="idle_watchdog")

    assert wdb.delete_session_wakeups("s1", source="idle_watchdog") == 1
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
        prompt="nudge", delay_seconds=600, source="idle_watchdog",
    )

    rows = [r for r in wdb.get_pending_wakeups(settings.effective_instance_id)
            if r["session_id"] == "s-mix"]
    assert {r["source"] for r in rows} == {"schedule_wakeup", "idle_watchdog"}

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
# idle-at-edge watchdog trigger condition
# ---------------------------------------------------------------------------

async def test_watchdog_nudges_idle_edge_session(mock_db, monkeypatch):
    from app.core import wakeup_service
    from app.db import database

    sid = "wd-edge"
    database.create_session(session_id=sid, profile_id="default")
    database.set_session_context_usage(session_id=sid, data=_usage(180_000))  # 90%

    scheduled = []

    async def _fake_schedule(**kwargs):
        scheduled.append(kwargs)
        return {"id": "x", "fire_at": 0}

    monkeypatch.setattr(wakeup_service, "schedule", _fake_schedule)

    nudged = await wakeup_service.scan_once()

    assert sid in nudged
    assert len(scheduled) == 1
    assert scheduled[0]["session_id"] == sid
    assert scheduled[0]["source"] == "idle_watchdog"
    assert scheduled[0]["delay_seconds"] == 0.0
    # watchdog nudges stay model-less by design: they fire on the profile
    # default, unlike a ScheduleWakeup wake which replays its turn's model.
    assert scheduled[0].get("model") is None


async def test_watchdog_skips_below_threshold(mock_db, monkeypatch):
    from app.core import wakeup_service
    from app.db import database

    sid = "wd-low"
    database.create_session(session_id=sid, profile_id="default")
    database.set_session_context_usage(session_id=sid, data=_usage(100_000))  # 50%

    scheduled = []
    monkeypatch.setattr(wakeup_service, "schedule",
                        lambda **kw: scheduled.append(kw))

    nudged = await wakeup_service.scan_once()

    assert nudged == []
    assert scheduled == []


async def test_watchdog_skips_session_with_active_turn(mock_db, monkeypatch):
    from app.core import wakeup_service
    from app.db import database
    from app.db import turns as turns_db

    sid = "wd-busy"
    database.create_session(session_id=sid, profile_id="default")
    database.set_session_context_usage(session_id=sid, data=_usage(190_000))
    # a live (queued) turn ⇒ not idle
    turns_db.enqueue_turn(sid, "user", prompt="do work")

    scheduled = []
    monkeypatch.setattr(wakeup_service, "schedule",
                        lambda **kw: scheduled.append(kw))

    nudged = await wakeup_service.scan_once()

    assert nudged == []
    assert scheduled == []


async def test_watchdog_skips_session_with_pending_wakeup(mock_db, monkeypatch):
    from app.core import wakeup_service
    from app.db import database
    from app.db import wakeups as wdb

    sid = "wd-armed"
    database.create_session(session_id=sid, profile_id="default")
    database.set_session_context_usage(session_id=sid, data=_usage(190_000))
    wdb.create_wakeup(id="already", session_id=sid, prompt="p", fire_at=time.time(),
                      instance_id=settings.effective_instance_id)

    scheduled = []
    monkeypatch.setattr(wakeup_service, "schedule",
                        lambda **kw: scheduled.append(kw))

    nudged = await wakeup_service.scan_once()

    assert nudged == []
    assert scheduled == []


async def test_watchdog_skips_autocompact_disabled(mock_db, monkeypatch):
    """Fluid's native legs trim instead of autocompacting, so there is no
    compaction edge to nudge past — the watchdog must not burn a nudge turn."""
    from app.core import wakeup_service
    from app.db import database

    sid = "wd-noac"
    database.create_session(session_id=sid, profile_id="default")
    # fluid-native shape: no autoCompactThreshold key, 90% (over the ~85% gate)
    database.set_session_context_usage(session_id=sid, data={
        "totalTokens": 180_000,
        "maxTokens": 200_000,
        "isAutoCompactEnabled": False,
    })

    scheduled = []
    monkeypatch.setattr(wakeup_service, "schedule",
                        lambda **kw: scheduled.append(kw))

    nudged = await wakeup_service.scan_once()

    assert nudged == []
    assert scheduled == []


async def test_watchdog_nudges_when_autocompact_flag_absent(mock_db, monkeypatch):
    from app.core import wakeup_service
    from app.db import database

    sid = "wd-legacy"
    database.create_session(session_id=sid, profile_id="default")
    # absent flag ⇒ assume enabled (backward compat with pre-existing snapshots)
    database.set_session_context_usage(session_id=sid, data={
        "totalTokens": 180_000,
        "maxTokens": 200_000,
    })

    scheduled = []

    async def _fake_schedule(**kwargs):
        scheduled.append(kwargs)
        return {"id": "x", "fire_at": 0}

    monkeypatch.setattr(wakeup_service, "schedule", _fake_schedule)

    nudged = await wakeup_service.scan_once()

    assert sid in nudged
    assert len(scheduled) == 1


async def test_watchdog_cooldown_blocks_immediate_renudge(mock_db, monkeypatch):
    from app.core import wakeup_service
    from app.db import database

    sid = "wd-cool"
    database.create_session(session_id=sid, profile_id="default")
    database.set_session_context_usage(session_id=sid, data=_usage(190_000))

    scheduled = []

    async def _fake_schedule(**kwargs):
        scheduled.append(kwargs)
        return {"id": "x", "fire_at": 0}

    monkeypatch.setattr(wakeup_service, "schedule", _fake_schedule)

    first = await wakeup_service.scan_once()
    # fake schedule created no real pending row, so only the cooldown can block it
    second = await wakeup_service.scan_once()

    assert first == [sid]
    assert second == []
    assert len(scheduled) == 1


# ---------------------------------------------------------------------------
# #793: usage-aware re-nudge latch + lifetime cap
# ---------------------------------------------------------------------------

def _clock(monkeypatch, wakeup_service, start=1_000.0):
    """Patch wakeup_service's monotonic clock; returns a setter for "now"."""
    state = {"now": start}
    monkeypatch.setattr(wakeup_service.time, "monotonic", lambda: state["now"])
    return state


async def test_watchdog_holds_when_no_decrease_after_cooldown(mock_db, monkeypatch, caplog):
    from app.core import wakeup_service
    from app.db import database

    sid = "wd-no-decrease"
    database.create_session(session_id=sid, profile_id="default")
    database.set_session_context_usage(session_id=sid, data=_usage(190_000))  # 95%

    scheduled = []

    async def _fake_schedule(**kwargs):
        scheduled.append(kwargs)
        return {"id": "x", "fire_at": 0}

    monkeypatch.setattr(wakeup_service, "schedule", _fake_schedule)
    clock = _clock(monkeypatch, wakeup_service)

    first = await wakeup_service.scan_once()
    assert first == [sid]

    # cooldown elapses but the persisted fraction never dropped (deferred
    # compaction that never landed) — same 95% snapshot.
    clock["now"] += wakeup_service.settings.idle_watchdog_cooldown_seconds + 1

    with caplog.at_level("INFO", logger="app.core.wakeup_service"):
        second = await wakeup_service.scan_once()

    assert second == []
    assert len(scheduled) == 1
    assert any(
        "no decrease observed" in r.message for r in caplog.records
    )


async def test_watchdog_rearms_after_decrease_and_cooldown(mock_db, monkeypatch):
    from app.core import wakeup_service
    from app.db import database

    sid = "wd-decrease"
    database.create_session(session_id=sid, profile_id="default")
    database.set_session_context_usage(session_id=sid, data=_usage(190_000))  # 95%

    scheduled = []

    async def _fake_schedule(**kwargs):
        scheduled.append(kwargs)
        return {"id": "x", "fire_at": 0}

    monkeypatch.setattr(wakeup_service, "schedule", _fake_schedule)
    clock = _clock(monkeypatch, wakeup_service)

    first = await wakeup_service.scan_once()
    assert first == [sid]

    # cooldown elapses AND compaction visibly landed — fraction dropped by
    # more than the 0.02 epsilon (95% -> 90%).
    clock["now"] += wakeup_service.settings.idle_watchdog_cooldown_seconds + 1
    database.set_session_context_usage(session_id=sid, data=_usage(180_000))  # 90%

    second = await wakeup_service.scan_once()

    assert second == [sid]
    assert len(scheduled) == 2


async def test_watchdog_lifetime_cap_mutes_after_three(mock_db, monkeypatch, caplog):
    from app.core import wakeup_service
    from app.db import database

    sid = "wd-cap"
    database.create_session(session_id=sid, profile_id="default")
    # 95% -> 92% -> 89% -> 86%: each step clears both cooldown and the 0.02
    # decrease gate while staying above the 85% fraction_gate.
    fractions = [190_000, 184_000, 178_000, 172_000]
    database.set_session_context_usage(session_id=sid, data=_usage(fractions[0]))

    scheduled = []

    async def _fake_schedule(**kwargs):
        scheduled.append(kwargs)
        return {"id": "x", "fire_at": 0}

    monkeypatch.setattr(wakeup_service, "schedule", _fake_schedule)
    clock = _clock(monkeypatch, wakeup_service)

    results = []
    with caplog.at_level("INFO", logger="app.core.wakeup_service"):
        for i, total in enumerate(fractions):
            if i > 0:
                clock["now"] += wakeup_service.settings.idle_watchdog_cooldown_seconds + 1
                database.set_session_context_usage(session_id=sid, data=_usage(total))
            results.append(await wakeup_service.scan_once())

    # first three sweeps each land a nudge (lifetime cap == 3); the fourth
    # (also off cooldown, also decreased) is muted and skipped.
    assert results == [[sid], [sid], [sid], []]
    assert len(scheduled) == 3

    latch = wakeup_service._watchdog_cooldown[sid]
    assert latch.count == 3
    assert latch.muted is True

    cap_logs = [r for r in caplog.records if "lifetime nudge cap" in r.message]
    assert len(cap_logs) == 1
    assert "muting" in cap_logs[0].message


# ---------------------------------------------------------------------------
# stale-candidate recency gate
# ---------------------------------------------------------------------------

def test_idle_edge_candidates_excludes_stale_sessions(mock_db):
    from app.db import database
    from app.db import wakeups as wdb

    iid = settings.effective_instance_id
    database.create_session(session_id="fresh", profile_id="default")
    database.set_session_context_usage(session_id="fresh", data=_usage(190_000))
    database.create_session(session_id="stale", profile_id="default")
    database.set_session_context_usage(session_id="stale", data=_usage(190_000))
    _age_session("stale", 60 * 60 * 24 * 30)  # a month untouched

    gated = wdb.idle_edge_candidates(iid, max_idle_seconds=86_400)
    assert {c["id"] for c in gated} == {"fresh"}

    # gate off (None or 0) → the legacy "any active session, forever" behavior
    assert {c["id"] for c in wdb.idle_edge_candidates(iid)} == {"fresh", "stale"}
    assert {c["id"] for c in wdb.idle_edge_candidates(iid, max_idle_seconds=0)} == {
        "fresh", "stale",
    }


async def test_watchdog_skips_stale_session(mock_db, monkeypatch):
    from app.core import wakeup_service
    from app.db import database

    sid = "wd-stale"
    database.create_session(session_id=sid, profile_id="default")
    database.set_session_context_usage(session_id=sid, data=_usage(190_000))
    _age_session(sid, settings.idle_watchdog_max_idle_seconds + 3_600)

    scheduled = []
    monkeypatch.setattr(wakeup_service, "schedule",
                        lambda **kw: scheduled.append(kw))

    assert await wakeup_service.scan_once() == []
    assert scheduled == []


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
# auto-continue latch: fail-closed fallback paths (#557 interplay)
# ---------------------------------------------------------------------------

def test_usage_over_threshold_pure():
    from app.core import compact_continue

    assert compact_continue.usage_over_threshold(None) == (False, None)
    assert compact_continue.usage_over_threshold({"maxTokens": 1000}) == (False, None)
    # autocompact disabled → never a boundary deferral (returns total, not over)
    assert compact_continue.usage_over_threshold(
        {"totalTokens": 999, "maxTokens": 1000, "isAutoCompactEnabled": False}
    ) == (False, 999)
    over, total = compact_continue.usage_over_threshold(_usage(190_000))
    assert over is True and total == 190_000


def test_usage_fraction_pure():
    from app.core import compact_continue

    assert compact_continue.usage_fraction(_usage(100_000, 200_000)) == 0.5
    assert compact_continue.usage_fraction({"totalTokens": 0, "maxTokens": 0}) is None
    assert compact_continue.usage_fraction(None) is None


class _RaisingAdapter:
    async def get_context_usage(self):
        raise RuntimeError("control call wedged")


class _StatePooled:
    is_connected = True

    def __init__(self, adapter):
        self.adapter = adapter


def _patch_pool(monkeypatch, peek_result):
    class _Pool:
        def peek(self, _sid):
            return peek_result

    monkeypatch.setattr("app.core.session_pool.get_session_pool", lambda: _Pool())


async def test_latch_falls_back_to_snapshot_when_unpooled(mock_db, monkeypatch):
    from app.core import compact_continue
    from app.db import database

    sid = "latch-unpooled"
    database.create_session(session_id=sid, profile_id="default")
    database.set_session_context_usage(session_id=sid, data=_usage(190_000))
    _patch_pool(monkeypatch, None)  # session not pooled

    over, total = await compact_continue.context_over_threshold(sid)

    assert over is True
    assert total == 190_000


async def test_latch_falls_back_to_snapshot_when_live_raises(mock_db, monkeypatch):
    from app.core import compact_continue
    from app.db import database

    sid = "latch-raise"
    database.create_session(session_id=sid, profile_id="default")
    database.set_session_context_usage(session_id=sid, data=_usage(190_000))
    _patch_pool(monkeypatch, _StatePooled(_RaisingAdapter()))

    over, total = await compact_continue.context_over_threshold(sid)

    assert over is True
    assert total == 190_000


async def test_latch_declines_when_no_live_and_no_snapshot(mock_db, monkeypatch):
    from app.core import compact_continue
    from app.db import database

    sid = "latch-none"
    database.create_session(session_id=sid, profile_id="default")  # no snapshot
    _patch_pool(monkeypatch, None)

    over, total = await compact_continue.context_over_threshold(sid)

    assert over is False
    assert total is None


async def test_latch_snapshot_fallback_under_threshold_declines(mock_db, monkeypatch):
    from app.core import compact_continue
    from app.db import database

    sid = "latch-under"
    database.create_session(session_id=sid, profile_id="default")
    database.set_session_context_usage(session_id=sid, data=_usage(50_000))  # 25%
    _patch_pool(monkeypatch, None)

    over, total = await compact_continue.context_over_threshold(sid)

    assert over is False
    assert total == 50_000
