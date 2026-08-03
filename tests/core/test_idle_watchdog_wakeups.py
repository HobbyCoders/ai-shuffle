"""Tests for #598: durable wakeups + idle-at-edge watchdog + latch hardening.

Covers the three deliverable test areas:
  * the watchdog trigger condition (idle, over-fraction, correctly gated);
  * wakeup persistence round-trip (create / list / claim / rehydrate / re-arm);
  * the auto-continue latch fail-closed fallback paths in compact_continue.
"""

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
def _reset_watchdog_state():
    from app.core import wakeup_service
    wakeup_service._watchdog_cooldown.clear()
    yield
    wakeup_service._watchdog_cooldown.clear()


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


async def test_fire_claims_row_and_skips_double_fire(mock_db, monkeypatch):
    from app.core import wakeup_service
    from app.db import wakeups as wdb
    import app.core.account_failover as af

    calls = []

    async def _fake_exec(**kwargs):
        calls.append(kwargs)
        return {"session_id": kwargs.get("session_id"), "metadata": {}}

    monkeypatch.setattr(af, "execute_query_with_failover", _fake_exec)

    wdb.create_wakeup(id="w1", session_id="s1", prompt="p1", fire_at=time.time(),
                      instance_id=settings.effective_instance_id,
                      profile_id="pr", project_id="pj")
    row = {"id": "w1", "session_id": "s1", "prompt": "p1",
           "profile_id": "pr", "project_id": "pj", "source": "schedule_wakeup"}

    await wakeup_service._fire(row)
    assert len(calls) == 1
    assert calls[0]["prompt"] == "p1"
    assert calls[0]["session_id"] == "s1"

    # row already claimed/deleted → no second query fires
    await wakeup_service._fire(row)
    assert len(calls) == 1


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
