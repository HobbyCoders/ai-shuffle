"""Copied wake payloads must not become new peer-message deliveries."""
from unittest.mock import AsyncMock

import pytest

from app.core import session_orchestration as orch, turn_scheduler
from app.core.session_runtime import SessionRuntime
from app.db import interactions, turns
from tests.core.test_session_orchestration import peers  # noqa: F401


def _wake(session_id, first):
    wake = turns.enqueue_turn(session_id, "unpark", profile_id=first["profile_id"], overrides=first["overrides"])
    resumed = turns.start_turn(wake["id"], can_resume=SessionRuntime._can_resume_parked)
    assert resumed["id"] == first["id"]
    return resumed


def _spent_woken_request(monkeypatch):
    request = orch._queue("caller", {"session_id": "peer", "message": "Check", "request_id": "request"}, "message")
    first = turns.start_turn(request["turn_id"])
    monkeypatch.setattr(orch, "MAX_ACTIONS", 0)
    reply = {"session_id": "caller", "message": "Checked", "request_id": "answer"}
    orch._queue("peer", reply, "message")
    assert turns.park_turn(first["id"], ["monitor"])
    return _wake("peer", first), reply


@pytest.mark.parametrize("human_input", [False, True])
def test_question_answer_cannot_copy_spent_delivery_credit(peers, monkeypatch, human_input):
    resumed, reply = _spent_woken_request(monkeypatch)
    context = turns.settle_turn(resumed["id"], "completed")
    for index in range(2):
        question = interactions.create_interaction("peer", "question", {"request_id": f"question-{index}"}, None)
        assert interactions.answer_question_and_enqueue(
            question, {"Continue?": "Yes"}, None, "Continue with Yes", context, human_input=human_input,
        )
        [answer] = turns.get_queued_turns("peer")
        started = turns.start_turn(answer["id"], can_resume=SessionRuntime._can_resume_parked)
        assert started["id"] == answer["id"] != context["id"]
        with pytest.raises(ValueError, match="budget"):
            orch._queue("peer", {**reply, "request_id": f"again-{index}"}, "message")
        # A later compatible wake of the copied answer must not rebind its
        # inherited credit to the answer's own id either.
        assert turns.park_turn(started["id"], ["monitor"])
        started = _wake("peer", started)
        with pytest.raises(ValueError, match="budget"):
            orch._queue("peer", {**reply, "request_id": f"after-wake-{index}"}, "message")
        context = turns.settle_turn(started["id"], "completed")


async def test_failed_wake_recovery_cannot_copy_spent_delivery_credit(peers, monkeypatch):
    resumed, reply = _spent_woken_request(monkeypatch)
    monkeypatch.setattr(turn_scheduler, "_turn_model_call_count", lambda sid, tid: 0)
    monkeypatch.setattr(turn_scheduler, "_drop_pooled_client", AsyncMock())
    monkeypatch.setattr(turn_scheduler, "kick", lambda sid: None)
    settled = turns.settle_turn(resumed["id"], "aborted", stop_reason="error", error="client closed")
    assert await turn_scheduler._maybe_recover_failed_wake("peer", settled)
    [retry] = turns.get_queued_turns("peer")
    resumed = turns.start_turn(retry["id"], can_resume=SessionRuntime._can_resume_parked)
    assert resumed["id"] == retry["id"] != settled["id"]
    with pytest.raises(ValueError, match="budget"):
        orch._queue("peer", {**reply, "request_id": "after-recovery"}, "message")
