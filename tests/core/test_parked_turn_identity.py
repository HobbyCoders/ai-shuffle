"""Parked identity regressions adapted from the review's standalone probe."""
import pytest

from app.core import provider_agent, session_orchestration, session_runtime, task_dispatch
from app.db import database, turns


def _park(sid, source, payload, **session_fields):
    database.create_session(session_id=sid, profile_id="default", **session_fields)
    first = turns.enqueue_turn(
        sid, source, prompt="work", payload=payload, profile_id="default",
        command_id="orchestration:start:x" if source == "agent_message" else None,
    )
    assert turns.start_turn(first["id"])
    assert turns.park_turn(first["id"], ["monitor"])
    return first


def _wake(sid, source, payload=None):
    queued = turns.enqueue_turn(sid, source, prompt="next", payload=payload or {}, profile_id="default")
    resumed = turns.start_turn(queued["id"], can_resume=session_runtime.SessionRuntime._can_resume_parked)
    return queued, resumed


@pytest.mark.parametrize("source", ["user", "queue", "api"])
def test_human_parked_wake_resets_budget_not_launch_identity(mock_db, source):
    identity = {
        "task_report_device": "starter-device", "task_report": {"owner_session_id": "owner"},
        "task_start_fingerprint": "task-start", "task_checkout_mode": "none",
        "orchestration_sender": "starter", "orchestration_fingerprint": "start",
        "orchestration_report_device": "starter-device", "model_override": {"model": "sonnet"},
        "loop_guard_origin": "automation",
    }
    message = {"peer_message_content": "old message", "peer_sender_label": "starter",
               "forwarded_by_device": "starter-device", "orchestration_reply_to": "old-message"}
    old_chain = {"orchestration_root": "used-up-root", "orchestration_depth": 2,
                 "orchestration_remote_origin": "starter-device"}
    first = _park("human", "agent_message", {**identity, **old_chain, **message})
    queued, resumed = _wake("human", source, {**old_chain, **message})
    assert resumed["id"] == first["id"]
    payload = resumed["payload"]
    assert payload["orchestration_root"] == queued["id"]
    assert not {"orchestration_depth", "orchestration_remote_origin", *message} & payload.keys()
    assert {key: payload.get(key) for key in identity} == identity
    context = session_orchestration._context("human")
    assert context["payload"]["orchestration_root"] == queued["id"]
    assert "orchestration_remote_origin" not in context["payload"]
    # A later automatic wake must not resurrect the original peer's origin or
    # report the owner's new job as the starter's work.
    assert turns.park_turn(first["id"], ["next-monitor"])
    _, automatic = _wake("human", "unpark")
    assert automatic["payload"]["orchestration_root"] == queued["id"]
    from app.core import loop_guard
    assert loop_guard.turn_origin(automatic) == source
    settled = turns.settle_turn(automatic["id"], "completed")
    origin = session_orchestration._origin_turn("human", settled)
    assert origin is not None and origin["id"] == first["id"]
    assert session_orchestration.build_started_report("human", settled, "completed") is None


@pytest.mark.parametrize("remote", [False, True])
@pytest.mark.parametrize("later_turn", [None, "auto_continue", "failover"])
@pytest.mark.parametrize("wake_source", ["unpark", "away"])
def test_started_report_survives_parked_wake(mock_db, monkeypatch, remote, later_turn, wake_source):
    from app.core import assistant_service

    database.create_session("home", "default")
    monkeypatch.setattr(assistant_service, "get_primary_session_id", lambda: "home")
    monkeypatch.setattr(assistant_service, "is_primary_session", lambda sid: sid == "home")
    payload = {"orchestration_sender": "home", "orchestration_root": "root", "orchestration_depth": 1}
    if remote:
        payload.update(orchestration_remote_origin="starter-device", orchestration_report_device="starter-device")
    first = _park("reporting", "agent_message", payload)
    if wake_source == "away":
        from app.db import interactions
        question = interactions.create_interaction("reporting", "question", {"request_id": "continue"}, None)
        assert interactions.answer_question_and_enqueue(
            question, {"Continue?": "Yes"}, None, "Continue with Yes", first, human_input=False,
        )
        [answer] = turns.get_queued_turns("reporting")
        assert answer["payload"]["question_answer_human_input"] is False
        resumed = turns.start_turn(answer["id"], can_resume=session_runtime.SessionRuntime._can_resume_parked)
    else:
        _, resumed = _wake("reporting", "unpark")
    assert resumed["id"] == first["id"]
    settled = turns.settle_turn(first["id"], "completed")
    if later_turn:
        continuation = turns.enqueue_turn("reporting", later_turn, parent_turn_id=first["id"])
        assert turns.start_turn(continuation["id"])
        settled = turns.settle_turn(continuation["id"], "completed")
    origin = session_orchestration._origin_turn("reporting", settled)
    assert origin is not None and origin["id"] == first["id"]
    built = session_orchestration.build_started_report("reporting", settled, "completed")
    assert built is not None
    home, device, frame = built
    assert (home, device) == ((None, "starter-device") if remote else ("home", None))
    assert frame["task_id"] == "reporting"
    assert frame["agent_message_turn_id"] == settled["id"]


def test_worker_detection_survives_automatic_wakes_not_human_input(mock_db):
    provider_agent.reset_worker_sessions()
    first = _park("worker", "provider_agent", {}, is_worker=True)
    for _ in range(2):
        _, resumed = _wake("worker", "unpark")
        assert resumed["id"] == first["id"]
        assert provider_agent.is_worker_turn("worker")
        assert provider_agent.is_provider_agent_worker("worker")
        assert turns.park_turn(first["id"], ["monitor"])
    _, human = _wake("worker", "user")
    assert human["id"] == first["id"]
    assert not provider_agent.is_worker_turn("worker")
    assert turns.park_turn(first["id"], ["monitor"])
    _wake("worker", "unpark")
    assert not provider_agent.is_worker_turn("worker")


def test_local_worker_chain_survives_wake_without_process_registry(mock_db):
    provider_agent.reset_worker_sessions()
    database.create_session("owner", "default")
    _park("local-parent", "provider_agent", {}, is_worker=True, parent_session_id="owner")
    _wake("local-parent", "unpark")
    first = _park("local-worker", "provider_agent", {}, is_worker=True, parent_session_id="local-parent")
    _, resumed = _wake("local-worker", "unpark")
    assert resumed["id"] == first["id"]
    assert task_dispatch._first_turn_source("local-worker") == "provider_agent"
    assert task_dispatch.caller_chain("local-worker")[1:] == ("owner", 2)
    assert task_dispatch._chain_agent_ids("local-worker") == ["default", "default", "default"]
