"""Owner-only, reload-safe opt-in over the normal limit-stop/wakeup path."""
import asyncio
import time
from unittest.mock import AsyncMock

import pytest

from app.core import limit_resume as lr, turn_scheduler, wakeup_service
from app.core.task_registry import task_registry
from app.db import database, session_events, turns, wakeups
from app.db.base import get_db


@pytest.fixture
def stopped_chat(mock_db, monkeypatch):
    database.create_profile("once-agent", "Agent", None, {})
    database.create_session("once-chat", "once-agent", is_active=True)
    monkeypatch.setattr(lr, "session_leg", lambda session: "delegated")
    monkeypatch.setattr(lr, "structured_reset", lambda key, now=None: None)
    monkeypatch.setattr(lr, "still_blocked_until", lambda *a, **kw: None)
    monkeypatch.setattr("app.core.account_failover.attempt_failover", AsyncMock())
    task_registry.clear_user_stop("once-chat")
    ended = time.time() - 3600
    reset = ended + 7200
    terminal = {"api_error_status": 429, "is_error": True,
                "errors": [f"Claude AI usage limit reached|{int(reset)}"]}
    turn = turns.enqueue_turn("once-chat", "user", profile_id="once-agent", prompt="work")
    with get_db() as conn:
        conn.cursor().execute("UPDATE turns SET state='completed', started_at=%s, ended_at=%s WHERE id=%s",
                              (ended - 60, ended, turn["id"]))
    session_events.append_events("once-chat", [{"type": "turn.completed", "turn_id": turn["id"],
                                               "payload": terminal}])
    turn = turns.get_turn(turn["id"])
    yield turn, terminal, reset
    task_registry.clear_user_stop("once-chat")
    for task in list(wakeup_service._timers.values()):
        task.cancel()
    wakeup_service._timers.clear()
    wakeup_service._retry_attempts.clear()


async def test_global_off_stop_publishes_reset_and_arm_survives_reload(stopped_chat):
    turn, terminal, reset = stopped_chat
    await turn_scheduler._maybe_failover("once-chat", turn, terminal)
    before = await lr.state("once-chat")
    assert before["limit_stopped"]["reset_at"] == int(reset)
    assert before["limit_stopped"]["can_arm"] is True
    assert before["armed"] is False
    armed = await lr.arm("once-chat", turn["id"])
    assert armed["armed"] is True
    assert armed["resume_at"] >= reset + lr.GRACE_SECONDS - 1
    assert armed["pending"]["compact"] is True
    assert armed["pending"]["once"] is True
    # No in-memory decision cache: the durable wake and handled log are enough.
    lr._locks.clear()
    assert await lr.state("once-chat") == armed
    assert await lr.arm("once-chat", turn["id"]) == armed
    assert len(wakeups.get_session_wakeups("once-chat", lr.LIMIT_RESUME_SOURCE)) == 1
    assert lr.get_settings() == lr.DEFAULTS
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT payload FROM session_events WHERE session_id=%s AND event_type='system.note'",
                    ("once-chat",))
        notes = [row["payload"] for row in cur.fetchall()]
    from app.db.base import parse_jsonb
    data = [parse_jsonb(note)["data"] for note in notes if parse_jsonb(note).get("subtype") == lr.SYSTEM_SUBTYPE]
    assert data[0]["status"] == "available" and data[0]["reset_at"] == int(reset)
    assert data[-1]["status"] == "scheduled" and data[-1]["armed"] is True


async def test_simultaneous_arms_use_one_wakeup(stopped_chat):
    turn, _, _ = stopped_chat
    first, second = await asyncio.gather(lr.arm("once-chat", turn["id"]), lr.arm("once-chat", turn["id"]))
    assert first == second
    assert len(wakeups.get_session_wakeups("once-chat", lr.LIMIT_RESUME_SOURCE)) == 1


async def test_cancel_is_durable_and_prevents_rearming_same_limit(stopped_chat):
    turn, _, _ = stopped_chat
    await lr.arm("once-chat", turn["id"])
    assert await lr.cancel("once-chat") is True
    assert await lr.cancel("once-chat") is False
    state = await lr.state("once-chat")
    assert state["armed"] is False and state["resume_at"] is None
    assert state["limit_stopped"]["reason"] == "already_handled"
    with pytest.raises(ValueError, match="already handled"):
        await lr.arm("once-chat", turn["id"])


async def test_unknown_reset_is_explicit_and_refuses_arm(stopped_chat):
    turn, _, _ = stopped_chat
    terminal = {"api_error_status": 429, "errors": ["Usage limit reached"]}
    session_events.append_events("once-chat", [{"type": "turn.completed", "turn_id": turn["id"],
                                               "payload": terminal}])
    await turn_scheduler._maybe_failover("once-chat", turn, terminal)
    state = await lr.state("once-chat")
    assert state["limit_stopped"]["reset_at"] is None
    assert state["limit_stopped"]["reset_known"] is False
    assert state["limit_stopped"]["reason"] == "reset_unknown"
    with pytest.raises(ValueError, match="Reset time unknown.*manually"):
        await lr.arm("once-chat", turn["id"])
    assert lr.pending("once-chat") is None


async def test_late_arm_uses_turn_end_for_structured_reading(stopped_chat, monkeypatch):
    turn, _, reset = stopped_chat
    seen = []
    def structured(key, now=None):
        seen.append(now)
        return lr.ResetInfo(at=reset, source="structured", windows=("five_hour",))
    monkeypatch.setattr(lr, "structured_reset", structured)
    await lr.arm("once-chat", turn["id"])
    assert seen and all(t == lr._epoch(turn["ended_at"]) for t in seen)


@pytest.mark.parametrize("change", ["stop", "settle", "archive", "handoff", "newer_failed", "membership"])
async def test_invalidation_hides_arm_and_blocks_claimed_wake(stopped_chat, monkeypatch, change):
    turn, _, _ = stopped_chat
    await lr.arm("once-chat", turn["id"])
    [row] = wakeups.get_session_wakeups("once-chat", lr.LIMIT_RESUME_SOURCE)
    if change == "stop":
        task_registry.mark_user_stopped("once-chat")
    elif change in ("settle", "archive"):
        with get_db() as conn:
            sql = "is_active=FALSE" if change == "settle" else "status='archived'"
            conn.cursor().execute("UPDATE sessions SET " + sql + " WHERE id=%s", ("once-chat",))
    elif change == "handoff":
        monkeypatch.setattr("app.core.session_handoff.pending_for", lambda sid: object())
    elif change == "membership":
        # A settle/unsettle revision still invalidates the old opt-in.
        fence = lr._once_fence("once-chat")
        monkeypatch.setattr(lr, "_once_fence", lambda sid: {**fence, "membership": {"local_revision": 2}})
    else:
        newer = turns.enqueue_turn("once-chat", "wakeup", profile_id="once-agent", prompt="new run")
        with get_db() as conn:
            conn.cursor().execute("UPDATE turns SET state='completed', started_at=%s, ended_at=%s WHERE id=%s",
                                  (time.time() - 2, time.time(), newer["id"]))
        session_events.append_events("once-chat", [{"type": "turn.completed", "turn_id": newer["id"],
                                                   "payload": {"is_error": True, "api_error_status": 500}}])
    assert (await lr.state("once-chat"))["armed"] is False
    submit = AsyncMock()
    monkeypatch.setattr("app.core.turn_manager.submit_async", submit)
    assert await lr.fire(row) is None
    submit.assert_not_awaited()
    with pytest.raises(ValueError):
        await lr.arm("once-chat", turn["id"])


async def test_cancel_after_claim_prevents_the_continuation(stopped_chat, monkeypatch):
    turn, _, _ = stopped_chat
    await lr.arm("once-chat", turn["id"])
    [row] = wakeups.get_session_wakeups("once-chat", lr.LIMIT_RESUME_SOURCE)
    assert wakeups.claim_wakeup(row["id"]) is True
    assert await lr.cancel("once-chat") is True
    submit = AsyncMock()
    monkeypatch.setattr("app.core.turn_manager.submit_async", submit)
    assert await lr.fire(row) is None
    submit.assert_not_awaited()


@pytest.mark.parametrize("claimed", [True, False])
async def test_once_cancel_authorizes_before_any_mutation(stopped_chat, claimed):
    turn, _, _ = stopped_chat
    await lr.arm("once-chat", turn["id"])
    [row] = wakeups.get_session_wakeups("once-chat", lr.LIMIT_RESUME_SOURCE)
    if claimed:
        assert wakeups.claim_wakeup(row["id"]) is True

    def denied():
        raise PermissionError("Owner access required")

    with pytest.raises(PermissionError, match="Owner access required"):
        await lr.cancel("once-chat", authorize_once=denied)
    assert lr._last_decision("once-chat")["status"] == "scheduled"
    assert bool(lr.pending("once-chat")) is not claimed
    assert await lr.cancel("once-chat", authorize_once=lambda: None) is True


async def test_stale_turn_id_cannot_arm_latest_stop(stopped_chat):
    with pytest.raises(ValueError, match="no longer the last"):
        await lr.arm("once-chat", "older-turn")
    assert lr.pending("once-chat") is None


async def test_once_wake_keeps_existing_compact_before_resume(stopped_chat, monkeypatch):
    turn, _, _ = stopped_chat
    await lr.arm("once-chat", turn["id"])
    [row] = wakeups.get_session_wakeups("once-chat", lr.LIMIT_RESUME_SOURCE)
    assert wakeups.claim_wakeup(row["id"]) is True
    submit = AsyncMock(side_effect=["compact-turn", "resume-turn"])
    monkeypatch.setattr("app.core.turn_manager.submit_async", submit)
    assert await lr.fire(row) == "resume-turn"
    assert [call.kwargs["prompt"] for call in submit.await_args_list] == [lr.COMPACT_PROMPT, lr.RESUME_PROMPT]
    assert (await lr.state("once-chat"))["armed"] is False
    with pytest.raises(ValueError, match="already handled"):
        await lr.arm("once-chat", turn["id"])
