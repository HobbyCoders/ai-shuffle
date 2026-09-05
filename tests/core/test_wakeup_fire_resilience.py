"""A claimed wake must survive a submit that fails (2026-09-04 audit).

``_fire`` claims a wakeup by DELETING its row and only then submits the turn.
Before this fix everything after the claim was best-effort: an unresolvable
profile dropped the wake with a warning, and a ``submit_async`` that raised or
refused dropped it with nothing at all. The audited session armed a 120s retry
wake, nothing ever ran, and the agent sat parked for 15.6 minutes until a human
typed "Resume".

The claim stays destructive (it is what stops a rehydrated timer and a peer
install double-firing), so the guarantee here is the round trip: a failed
submit puts the SAME row back with a backed-off ``fire_at`` and re-arms it, and
a wake that is ultimately given up on says so on the session.
"""

import asyncio
import time
from unittest.mock import patch

import pytest

from app.core.config import settings
from tests.conftest import async_twin


@pytest.fixture(autouse=True)
def _reset_wakeup_state():
    from app.core import wakeup_service

    wakeup_service._retry_attempts.clear()
    yield
    for task in list(wakeup_service._timers.values()):
        task.cancel()
    wakeup_service._timers.clear()
    wakeup_service._retry_attempts.clear()


def _row(wakeup_id="w1", session_id="s1", source="schedule_wakeup"):
    return {
        "id": wakeup_id,
        "session_id": session_id,
        "prompt": "continue",
        "profile_id": "pr",
        "project_id": "pj",
        "source": source,
        "model": None,
        "reason": "retry",
        "instance_id": settings.effective_instance_id,
    }


def _persist(row, fire_at=None):
    from app.db import wakeups as wdb

    wdb.create_wakeup(
        id=row["id"],
        session_id=row["session_id"],
        prompt=row["prompt"],
        fire_at=fire_at if fire_at is not None else time.time(),
        profile_id=row["profile_id"],
        project_id=row["project_id"],
        reason=row["reason"],
        model=row["model"],
        source=row["source"],
        instance_id=settings.effective_instance_id,
    )


def _pending(wakeup_id):
    from app.db import wakeups as wdb

    rows = {
        r["id"]: r
        for r in wdb.get_pending_wakeups(settings.effective_instance_id)
    }
    return rows.get(wakeup_id)


def _capture_arm(monkeypatch):
    from app.core import wakeup_service

    armed = []
    monkeypatch.setattr(
        wakeup_service, "_arm", lambda row, delay: armed.append((row, delay))
    )
    return armed


def _submit_raising(monkeypatch, exc=None):
    from app.core import turn_manager

    def _boom(session_id, source, **kwargs):
        raise exc or RuntimeError("Server is at capacity — session pool at hard cap (8/8)")

    monkeypatch.setattr(turn_manager, "submit", _boom)
    monkeypatch.setattr(turn_manager, "submit_async", async_twin(_boom))


def _submit_refusing(monkeypatch):
    """A submit that returns no turn id — a refusal, not an exception."""
    from app.core import turn_manager

    def _refuse(session_id, source, **kwargs):
        return None

    monkeypatch.setattr(turn_manager, "submit", _refuse)
    monkeypatch.setattr(turn_manager, "submit_async", async_twin(_refuse))


def _capture_submit(monkeypatch):
    from app.core import turn_manager

    calls = []

    def _fake(session_id, source, **kwargs):
        calls.append({"session_id": session_id, "source": source, **kwargs})
        return f"turn-{len(calls)}"

    monkeypatch.setattr(turn_manager, "submit", _fake)
    monkeypatch.setattr(turn_manager, "submit_async", async_twin(_fake))
    return calls


# ── the happy path is unchanged ──────────────────────────────────────────────


async def test_successful_submit_consumes_the_row(mock_db, monkeypatch):
    from app.core import wakeup_service

    calls = _capture_submit(monkeypatch)
    row = _row("w-ok")
    _persist(row)

    await wakeup_service._fire(row)

    assert len(calls) == 1 and calls[0]["source"] == "wakeup"
    assert _pending("w-ok") is None
    assert wakeup_service._retry_attempts == {}


# ── submit raises / refuses → the row comes back ─────────────────────────────


async def test_submit_raise_reinserts_and_rearms_the_row(mock_db, monkeypatch):
    from app.core import wakeup_service

    _submit_raising(monkeypatch)
    armed = _capture_arm(monkeypatch)
    row = _row("w-raise")
    _persist(row)

    before = time.time()
    await wakeup_service._fire(row)

    persisted = _pending("w-raise")
    assert persisted is not None, "a raising submit must not consume the wake"
    assert persisted["prompt"] == "continue"
    assert persisted["profile_id"] == "pr"
    assert persisted["source"] == "schedule_wakeup"
    assert persisted["fire_at"] >= before
    assert len(armed) == 1 and armed[0][0]["id"] == "w-raise"
    assert armed[0][1] == pytest.approx(wakeup_service._RETRY_BASE_SECONDS)
    assert wakeup_service._retry_attempts["w-raise"] == 1


async def test_submit_refusal_reinserts_and_rearms_the_row(mock_db, monkeypatch):
    from app.core import wakeup_service

    _submit_refusing(monkeypatch)
    armed = _capture_arm(monkeypatch)
    row = _row("w-refuse")
    _persist(row)

    await wakeup_service._fire(row)

    assert _pending("w-refuse") is not None
    assert len(armed) == 1
    assert wakeup_service._retry_attempts["w-refuse"] == 1


async def test_backoff_grows_with_each_failed_attempt(mock_db, monkeypatch):
    from app.core import wakeup_service

    _submit_raising(monkeypatch)
    armed = _capture_arm(monkeypatch)
    row = _row("w-backoff")
    _persist(row)

    # each failure re-inserts the row, so the next fire claims it again
    for _ in range(3):
        await wakeup_service._fire(row)

    delays = [d for _, d in armed]
    assert delays == [
        wakeup_service._RETRY_BASE_SECONDS,
        wakeup_service._RETRY_BASE_SECONDS * 2,
        wakeup_service._RETRY_BASE_SECONDS * 4,
    ]


async def test_rearmed_wake_actually_fires_later(mock_db, monkeypatch):
    """End to end: submit fails once, the re-armed timer fires, the turn lands."""
    from app.core import turn_manager, wakeup_service

    calls = []
    attempts = {"n": 0}

    def _flaky(session_id, source, **kwargs):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("server busy: session pool at hard cap (8/8)")
        calls.append({"session_id": session_id, "source": source, **kwargs})
        return "turn-2"

    monkeypatch.setattr(turn_manager, "submit", _flaky)
    monkeypatch.setattr(turn_manager, "submit_async", async_twin(_flaky))
    monkeypatch.setattr(wakeup_service, "_RETRY_BASE_SECONDS", 0.01)

    row = _row("w-later")
    _persist(row)

    await wakeup_service._fire(row)
    assert _pending("w-later") is not None and calls == []

    # let the re-armed timer come due
    for _ in range(100):
        await asyncio.sleep(0.01)
        if calls:
            break

    assert len(calls) == 1, "the re-armed wake never fired"
    assert calls[0]["source"] == "wakeup"
    assert calls[0]["prompt"] == "continue"
    assert _pending("w-later") is None
    assert wakeup_service._retry_attempts == {}


# ── giving up is loud ────────────────────────────────────────────────────────


async def test_wake_dropped_after_max_attempts_notifies_the_session(
    mock_db, monkeypatch, caplog
):
    from app.core import wakeup_service

    _submit_raising(monkeypatch)
    armed = _capture_arm(monkeypatch)
    monkeypatch.setattr(wakeup_service, "_RETRY_MAX_ATTEMPTS", 1)

    notes = []

    async def _record(session_id, event):
        notes.append((session_id, event))

    row = _row("w-giveup")
    with patch("app.core.turn_recorder.record_drained_event", new=_record):
        _persist(row)
        await wakeup_service._fire(row)  # attempt 1 → re-armed
        await wakeup_service._fire(row)  # attempt 2 → over the cap → dropped

    assert len(armed) == 1, "no re-arm past the attempt cap"
    assert _pending("w-giveup") is None
    assert len(notes) == 1
    session_id, event = notes[0]
    assert session_id == "s1"
    assert event["type"] == "system"
    assert event["subtype"] == wakeup_service.DROPPED_NOTE_SUBTYPE
    assert "could not be started" in event["data"]["message"]
    assert "hard cap" in event["data"]["reason"]
    assert wakeup_service._retry_attempts == {}


async def test_unresolvable_profile_drop_notifies_the_session(mock_db, monkeypatch):
    from app.core import wakeup_service
    from app.db import database

    calls = _capture_submit(monkeypatch)
    database.create_session(session_id="sess-noprof", profile_id=None)

    notes = []

    async def _record(session_id, event):
        notes.append((session_id, event))

    row = _row("w-noprof", session_id="sess-noprof")
    row["profile_id"] = None
    _persist(row)

    with patch("app.core.turn_recorder.record_drained_event", new=_record):
        await wakeup_service._fire(row)

    assert calls == []
    assert len(notes) == 1
    assert notes[0][1]["subtype"] == wakeup_service.DROPPED_NOTE_SUBTYPE
    assert "profile" in notes[0][1]["data"]["reason"]


async def test_reinsert_failure_is_announced_not_swallowed(mock_db, monkeypatch):
    from app.core import wakeup_service
    from app.db import wakeups as wdb

    _submit_raising(monkeypatch)
    armed = _capture_arm(monkeypatch)
    row = _row("w-noreinsert")
    _persist(row)

    def _explode(**kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(wdb, "create_wakeup", _explode)

    notes = []

    async def _record(session_id, event):
        notes.append((session_id, event))

    with patch("app.core.turn_recorder.record_drained_event", new=_record):
        await wakeup_service._fire(row)

    assert armed == []
    assert len(notes) == 1
    assert notes[0][1]["subtype"] == wakeup_service.DROPPED_NOTE_SUBTYPE


# ── the claim still guarantees one turn ──────────────────────────────────────


async def test_double_fire_race_submits_exactly_one_turn(mock_db, monkeypatch):
    """A rehydrated timer and its original racing the same row: one turn."""
    from app.core import wakeup_service

    calls = _capture_submit(monkeypatch)
    row = _row("w-race")
    _persist(row)

    await asyncio.gather(
        wakeup_service._fire(dict(row)), wakeup_service._fire(dict(row))
    )

    assert len(calls) == 1
    assert _pending("w-race") is None
