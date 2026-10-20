"""Receiver-owned continuation, progress receipts and bounded lease coordination."""
import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.core import session_handoff as sh, wakeup_service
from app.db import database, wakeups
from app.db.base import get_db
from app.network import control, engine, handoff, runners


def test_agent_resume_wake_is_stable_and_manual_moves_never_wake(mock_db, monkeypatch):
    database.create_session("resume-chat", "default")
    queued = {"requested_by": "agent", "target_id": "target", "label": "Target", "note": "Do the next step",
              "requested_at": time.time(), "handoff_id": "move-id"}
    monkeypatch.setattr(sh, "_moving", {"resume-chat": sh.PendingHandoff(
        "target", "Target", queued["note"], queued["requested_at"], handoff_id="move-id")})
    monkeypatch.setattr(runners, "find", lambda _: {"capabilities": {"move_wakes": 3, "handoff_target_resume": 1}})
    with patch.object(sh, "resume_plan", return_value="send"):
        first = handoff._resume_wake("resume-chat", queued, "target")
        second = handoff._resume_wake("resume-chat", queued, "target")
    assert first["id"] == second["id"] == "handoff-resume-move-id"
    assert "Do the next step" in first["prompt"]
    assert first["fire_at"] <= time.time()
    assert handoff._moving_wakes({"wakes": [first, {"id": "ordinary"}],
                                  "resume_wake_id": first["id"]}) == {"wake_ids": ["ordinary"]}
    assert handoff._resume_wake("resume-chat", {**queued, "requested_by": "owner"}, "target") is None
    monkeypatch.setattr(runners, "find", lambda _: {"capabilities": {"move_wakes": 3}})
    assert handoff._resume_wake("resume-chat", queued, "target") is None


@pytest.mark.asyncio
async def test_target_commit_arms_resume_without_source_and_human_cannot_drop_it(mock_db, monkeypatch):
    database.create_session("resume-chat", "default")
    wake = {"id": "handoff-resume-move-id", "source": "handoff_resume", "prompt": "Keep the note",
            "fire_at": time.time(), "profile_id": "default"}
    handoff._stage_wakes("resume-chat", "source", {"token": "lease", "wakes": [wake]}, None)
    offline = AsyncMock(side_effect=control.RunnerUnreachable("offline"))
    monkeypatch.setattr(control, "call", offline)
    arm = []
    monkeypatch.setattr(wakeup_service, "arm_persisted", arm.append)
    await handoff._accept_and_arm("resume-chat", "target", None, "lease")
    assert len(arm) == 1
    offline.assert_not_awaited()
    # This is the cancellation path a held human turn takes on admission.
    from app.core.turn_manager import _supersede_wakeups

    _supersede_wakeups("resume-chat")
    assert wakeups.get_session_wakeups("resume-chat")[0]["id"] == wake["id"]
    submit = AsyncMock(return_value="turn-resume")
    monkeypatch.setattr(wakeup_service, "_session_user_stopped", lambda _: False)
    monkeypatch.setattr(wakeup_service, "_resolve_profile_id", AsyncMock(return_value="default"))
    monkeypatch.setattr(wakeup_service, "_session_api_user_id", lambda _: None)
    monkeypatch.setattr("app.core.archive_guard.is_archived_async", AsyncMock(return_value=False))
    monkeypatch.setattr("app.core.turn_manager.submit_async", submit)
    await wakeup_service._fire(arm[0])
    await wakeup_service._fire(arm[0])
    submit.assert_awaited_once()
    assert submit.await_args.kwargs["prompt"] == "Keep the note"
    assert submit.await_args.kwargs["command_id"] == wake["id"]
    await handoff._accept_and_arm("resume-chat", "target", None, "lease")
    assert wakeups.get_session_wakeups("resume-chat") == []


@pytest.mark.asyncio
@pytest.mark.parametrize("capable", [False, True])
async def test_moved_sends_legacy_resume_only_for_older_target(mock_db, monkeypatch, capable):
    from app.db import session_moves

    pending = sh.PendingHandoff("target", "Target", "note", time.time())
    database.create_session("resume-chat", "default")
    session_moves.save("resume-chat", target_id="target", label="Target", note="note", requested_by="agent",
                       requested_at=pending.requested_at, expires_at=pending.expires_at, handoff_id=pending.handoff_id)
    session_moves.set_resume_exported("resume-chat", pending.handoff_id, "target", capable)
    monkeypatch.setattr(sh, "resume_plan", lambda *args: "send")
    # Later capability changes must not alter the persisted export decision.
    monkeypatch.setattr(handoff, "target_resume", lambda _: not capable)
    monkeypatch.setattr(sh, "_tell", AsyncMock())
    monkeypatch.setattr(sh, "_resume_turn", lambda *args: {"prompt": "legacy"})
    deliver = AsyncMock()
    monkeypatch.setattr(sh, "deliver_held", deliver)
    await sh._moved("resume-chat", pending, "target")
    assert deliver.await_args.args[3] == (None if capable else {"prompt": "legacy"})


@pytest.mark.asyncio
async def test_status_reports_step_and_owner_card_reads_it(monkeypatch):
    member = SimpleNamespace(kind="worker", status="active")
    monkeypatch.setattr(engine, "require_context", lambda: SimpleNamespace(member=lambda _: member))
    monkeypatch.setattr(handoff, "_operations", {})
    monkeypatch.setattr(handoff, "_operation_progress", {})
    monkeypatch.setattr(handoff, "_POLL_WAIT_S", 0)
    ready = asyncio.Event()

    async def work(payload, peer):
        timing = handoff._Timing("import", "progress-chat")
        timing.waiting("install_history.fluid_session_nodes")
        await ready.wait()
        timing.log("committed")
        return {"moved": True}

    monkeypatch.setattr(handoff, "_handle", work)
    receipt = await handoff.handle({"session_id": "progress-chat", "phase": "receive", "deferred": True}, "source")
    await asyncio.sleep(0)
    result = await handoff.handle({"session_id": "progress-chat", "phase": "status", **receipt}, "source")
    assert result["progress"]["step"] == "install_history.fluid_session_nodes"
    assert result["progress"]["elapsed_seconds"] >= result["progress"]["step_elapsed_seconds"] >= 0
    monkeypatch.setitem(handoff._remote_progress, "progress-chat", result["progress"])
    monkeypatch.setattr(sh, "_active", lambda _: sh.PendingHandoff("target", "Target", "", time.time()))
    monkeypatch.setattr(sh, "busy_state", lambda _: {})
    assert sh._owner_state("progress-chat")["pending"]["progress"] == result["progress"]
    ready.set()
    await next(iter(handoff._operations.values()))


@pytest.mark.asyncio
async def test_pending_receipts_extend_900s_deadline_but_not_lease(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(handoff.asyncio, "get_running_loop", lambda: SimpleNamespace(time=lambda: clock[0]))
    monkeypatch.setattr(runners, "find", lambda _: {"capabilities": {"handoff_poll": 1}})
    calls = []

    async def call(device, action, payload, **kwargs):
        calls.append(payload["phase"])
        if payload["phase"] == "receive":
            return {"operation": "op"}
        clock[0] += 500
        if clock[0] < 1500:
            return {"operation": "op", "progress": {"step": "install_history", "elapsed_seconds": clock[0], "step_elapsed_seconds": clock[0]}}
        return {"moved": True}

    monkeypatch.setattr(control, "call", call)
    assert await handoff._call("target", {"session_id": "s", "phase": "receive"}, timeout=900) == {"moved": True}
    assert calls == ["receive", "status", "status", "status"]
    clock[0] = 0

    async def forever(*args, **kwargs):
        clock[0] += 500
        return {"operation": "op"}

    monkeypatch.setattr(control, "call", forever)
    with pytest.raises(control.MaybeApplied):
        await handoff._call("target", {"session_id": "s", "phase": "receive"}, timeout=900)


@pytest.mark.asyncio
async def test_commit_refusal_reason_is_logged_on_both_ends(monkeypatch, caplog):
    reason = "checkout changed during import"
    monkeypatch.setattr(handoff, "_handle_phase", AsyncMock(side_effect=ValueError(reason)))
    with pytest.raises(ValueError, match=reason):
        await handoff._handle({"session_id": "s", "phase": "commit"}, "target")
    monkeypatch.setattr(control, "call", AsyncMock(side_effect=control.ControlRefused(reason)))
    with pytest.raises(control.ControlRefused):
        await handoff._owner_commit("source", {"session_id": "s", "phase": "commit"})
    assert sum(reason in r.message for r in caplog.records) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("staged", [False, True])
async def test_explicit_stop_still_cancels_receiver_resume(mock_db, monkeypatch, staged):
    database.create_session("stopped-chat", "default")
    wake = {"id": "handoff-resume-stopped", "source": "handoff_resume", "prompt": "note",
            "fire_at": time.time(), "profile_id": "default"}
    handoff._stage_wakes("stopped-chat", "source", {"token": "stop-lease", "wakes": [wake]}, None)
    arm = []
    monkeypatch.setattr(wakeup_service, "arm_persisted", arm.append)
    if not staged:
        await handoff._accept_and_arm("stopped-chat", "target", None, "stop-lease")
        assert len(arm) == 1
    wakeup_service.cancel_session_wakeups("stopped-chat")
    await handoff._accept_and_arm("stopped-chat", "target", None, "stop-lease")
    assert wakeups.get_session_wakeups("stopped-chat") == []
    assert len(arm) == (0 if staged else 1)


def test_resume_command_is_idempotent_at_durable_turn_admission(mock_db):
    from app.db import turns

    first = turns.enqueue_turn("resume-chat", "wakeup", prompt="note",
                               command_id="handoff-resume-move-id")
    second = turns.enqueue_turn("resume-chat", "wakeup", prompt="note",
                                command_id="handoff-resume-move-id")
    assert second["id"] == first["id"]
    assert len(turns.get_queued_turns("resume-chat")) == 1
