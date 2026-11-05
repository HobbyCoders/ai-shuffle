"""Stop identity must survive claim, parked absorption, and runtime replacement."""
import json

import pytest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from tests.core.test_event_write_lock_timeout import (
    SID, _session, real_worker, _detached_work_done,  # noqa: F401 - focused fixtures
)


@pytest.mark.parametrize("consumer", ["schedule_wakeup", "held_peer_promotion", "limit_resume", "compact_resume", "worker_resume"])
async def test_claimed_wake_cannot_enter_a_turn_after_stop(monkeypatch, consumer):
    from app.core import limit_resume, turn_manager, wakeup_service
    from app.db import base, turns, wakeups

    meta = {"limit_key": "test", "compact": consumer == "compact_resume", "leg": "delegated",
            **({"owner": "parent"} if consumer == "worker_resume" else {})}
    source = "limit_resume" if consumer in {"compact_resume", "worker_resume"} else consumer
    row = wakeups.create_wakeup(id=uuid4().hex, session_id=SID, prompt="cancelled wake",
                                profile_id="default", source=source, fire_at=1, reason=json.dumps(meta))
    monkeypatch.setattr(limit_resume, "_refused", AsyncMock(return_value=None))
    monkeypatch.setattr(limit_resume, "still_blocked_until", lambda *_args, **_kw: None)
    monkeypatch.setattr(limit_resume, "_record", AsyncMock())
    # Claim succeeds; the Stop occurs during the real consumer's next await.
    stopped = False
    async def resolve(_row):
        nonlocal stopped
        if not stopped:
            turns.read_stop_receipt(SID)
            stopped = True
        return "default"

    monkeypatch.setattr(wakeup_service, "_resolve_profile_id", resolve)
    monkeypatch.setattr(turn_manager, "_kick", lambda _sid: None)
    await wakeup_service._fire(row)
    if source == "limit_resume":
        assert all(call.args[1] != limit_resume._STATUS_RESUMED for call in limit_resume._record.await_args_list)
    with base.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM turns WHERE session_id=%s", (SID,))
        admitted = [turns.get_turn(r["id"]) for r in cursor.fetchall()]
    assert len(admitted) == (2 if consumer == "compact_resume" else 1)
    for turn in admitted:
        assert turn["payload"].get("wake_stop_seq") == row["stop_seq"]
        assert turn["state"] == "aborted"
        assert turns.start_turn(turn["id"], None, "peer-runner") is None
    assert turns.get_live_turn(SID) is None
    # A genuinely new wake is not blocked by an older cancelled instruction.
    fresh = wakeups.create_wakeup(id=uuid4().hex, session_id=SID, prompt="new wake",
                                  profile_id="default", source="schedule_wakeup", fire_at=2)
    await wakeup_service._fire(fresh)
    queued = turns.get_queued_turns(SID)
    assert len(queued) == 1 and queued[0]["payload"]["wake_stop_seq"] == fresh["stop_seq"]
    assert turns.start_turn(queued[0]["id"], None, "peer-runner") is not None


async def test_parked_absorption_retains_older_stop_cleanup_authority(real_worker):
    from app.core import session_runtime as sr
    from app.core.task_registry import task_registry
    from app.db import turns

    accepted = turns.enqueue_turn(SID, "user", prompt="old", profile_id="default")
    started = turns.start_turn(accepted["id"], None, "old-runner")
    old = sr.SessionRuntime(started)
    stopped = turns.interrupt_turn(SID, "stop", stop_background=True, source="user_stop")
    turns.park_turn(accepted["id"], ["worker-a"])
    new_input = turns.enqueue_turn(SID, "user", prompt="new", profile_id="default",
                                   payload={turns.PERSON_INPUT_KEY: True})
    absorbed = turns.start_turn(new_input["id"], None, "new-runner", can_resume=lambda *_args: True)
    assert absorbed["id"] == started["id"]
    assert absorbed["payload"]["accepted_input_id"] == new_input["id"]
    successor = sr.SessionRuntime(absorbed)
    sr._live_runtimes[SID] = successor
    signal = AsyncMock()
    try:
        with patch("app.core.query_session_lifecycle.interrupt_session", signal):
            await old.interrupt("stop", escalation_timeout=.01)
            await _detached_work_done()
        assert real_worker.torn_down is True
        assert task_registry.get_task(SID, "worker-a")["status"] == "killed"
        signal.assert_not_awaited()
        assert successor.admission_order == new_input["insert_order"]
    finally:
        sr._live_runtimes.pop(SID, None)


@pytest.mark.parametrize("retry", ["failure", "limit_pushback"])
async def test_retry_does_not_rearm_cancelled_text_into_a_new_generation(monkeypatch, retry):
    from app.core import wakeup_service
    from app.db import base, turns, wakeups

    old = wakeups.create_wakeup(id=uuid4().hex, session_id=SID, prompt="old retry",
                                profile_id="default", source="limit_resume", fire_at=1)
    assert wakeups.claim_wakeup(old["id"])
    turns.read_stop_receipt(SID)
    fresh = wakeups.create_wakeup(id=uuid4().hex, session_id=SID, prompt="genuinely new",
                                  profile_id="default", source="limit_resume", fire_at=2)
    monkeypatch.setattr(wakeup_service, "_arm", lambda *_args: None)
    if retry == "failure":
        await wakeup_service._rearm_after_failed_submit(old, "transient failure")
    else:
        await wakeup_service.schedule(session_id=SID, profile_id="default", project_id=None,
                    prompt=old["prompt"], delay_seconds=20, source="limit_resume",
                    created_at=old["created_at"], stop_seq=old["stop_seq"])
    with base.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, prompt, stop_seq FROM pending_wakeups WHERE session_id=%s", (SID,))
        rows = list(cursor.fetchall())
    retried = next(r for r in rows if r["prompt"] == "old retry")
    assert retried["stop_seq"] == old["stop_seq"] < fresh["stop_seq"]
    assert wakeups.claim_wakeup(retried["id"]) is False
    assert wakeups.claim_wakeup(fresh["id"]) is True
