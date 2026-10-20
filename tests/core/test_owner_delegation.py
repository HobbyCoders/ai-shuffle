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


def test_direct_parent_not_routing_root_is_assignment_authority(runtime):
    worker(runtime, "root", "parent")
    worker(runtime, "root", "child")  # UI routing copy
    worker(runtime, "parent", "child")
    assert agent_messaging.message_provenance("child", "parent") == "parent"
    for sender in ("root", "unknown", "foreign", "child"):
        assert agent_messaging.message_provenance("child", sender) == "peer"
