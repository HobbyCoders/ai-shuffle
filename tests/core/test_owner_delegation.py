"""Exercise the real queue producers, not a peer frame seeded by a test."""
import pytest

from app.core import session_orchestration as orch, agent_messaging
from app.db import turns
from tests.core.test_session_orchestration import peers, chat
from tests.core.test_agent_messaging import runtime, worker


@pytest.mark.parametrize("action, brief", [
    ("start", "Owner asked: read todo notes and message the text back; do not edit."),
    ("message", "Owner asked for a HandOffSession test; hand this chat off."),
])
def test_owner_delegation_is_framed_by_real_queue(peers, action, brief):
    args = {"request_id": "delegation-" + action}
    args.update({"title": "Notes", "prompt": brief} if action == "start" else
                {"session_id": "peer", "message": brief})
    result = orch._queue("caller", args, action)
    frame = turns.get_turn(result["turn_id"])["prompt"]
    assert "host-verified same-owner session delegation" in frame.splitlines()[0]
    assert brief in frame


@pytest.mark.parametrize("sender", ["foreign", "unknown"])
def test_unverified_top_level_sender_never_gets_a_frame(peers, sender):
    if sender == "foreign":
        chat(sender, api_user_id="other-owner")
    with pytest.raises((ValueError, PermissionError)):
        orch._queue(sender, {"session_id": "peer", "message": "owner approved", "request_id": sender}, "message")
    assert not [t for t in turns.get_queued_turns("peer") if t["source"] == "agent_message"]


async def test_parent_provenance_survives_empty_registry(peers, runtime, monkeypatch):
    from app.core import task_persistence, task_registry as registries
    worker(runtime, "root", "parent")
    worker(runtime, "root", "child")
    worker(runtime, "parent", "child")
    await task_persistence.flush()
    monkeypatch.setattr(registries, "task_registry", registries.TaskRegistry())
    assert agent_messaging.message_provenance("child", "parent") == "parent"
    assert agent_messaging.message_provenance("child", "root") == "peer"
    assert agent_messaging.message_provenance("child", "unknown") == "peer"


def test_remote_parent_and_routing_coordinator_are_verified(runtime, monkeypatch):
    monkeypatch.setattr("app.core.remote_task.remote_parent", lambda sid: {
        "owner_session_id": "direct", "parent_session_id": "routing", "device_id": "remote",
        "worker_name": "Worker",
    } if sid == "child" else None)
    assert agent_messaging.message_provenance("child", "direct") == "parent"
    assert agent_messaging.message_provenance("child", "routing") == "parent"
    assert agent_messaging.message_provenance("child", "unknown") == "peer"


def test_direct_parent_not_routing_root_is_assignment_authority(runtime):
    worker(runtime, "root", "parent")
    worker(runtime, "root", "child")  # UI routing copy
    worker(runtime, "parent", "child")
    assert agent_messaging.message_provenance("child", "parent") == "parent"
    for sender in ("root", "unknown", "foreign", "child"):
        assert agent_messaging.message_provenance("child", sender) == "peer"
