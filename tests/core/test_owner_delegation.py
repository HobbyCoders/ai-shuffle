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


@pytest.mark.parametrize("restore_root", [False, True])
async def test_parent_provenance_survives_restart(peers, runtime, monkeypatch, restore_root):
    from app.core import task_persistence, task_registry as registries
    worker(runtime, "root", "parent")
    worker(runtime, "root", "child")
    worker(runtime, "parent", "child")
    await task_persistence.flush()
    restored = registries.TaskRegistry()
    if restore_root:
        # Reconcile marks local running rows killed, then only root is hydrated.
        # These are snapshots of real start_task-produced records, not new edges.
        restored.hydrate_rows("root", [
            {"task_id": task["id"], "kind": "agent", "status": "killed"}
            for task in runtime[0].get_all("root", limit=None)
        ])
    monkeypatch.setattr(registries, "task_registry", restored)
    assert await agent_messaging.message_provenance_async("child", "parent") == "parent"
    assert await agent_messaging.message_provenance_async("child", "root") == "parent"
    assert await agent_messaging.message_provenance_async("child", "unknown") == "peer"
    [main] = await agent_messaging.peer_rows_async("child")
    assert main["worker_session_id"] == "parent"


def test_remote_parent_and_routing_coordinator_are_verified(runtime, monkeypatch):
    monkeypatch.setattr("app.core.remote_task.remote_parent", lambda sid: {
        "owner_session_id": "direct", "parent_session_id": "routing", "device_id": "remote",
        "worker_name": "Worker",
    } if sid == "child" else None)
    assert agent_messaging.message_provenance("child", "direct") == "parent"
    assert agent_messaging.message_provenance("child", "routing") == "parent"
    assert agent_messaging.message_provenance("child", "unknown") == "peer"
    [main] = agent_messaging.peer_rows("child")
    assert main["name"] == "main" and main["worker_session_id"] == "direct"


def test_all_task_ancestors_but_not_siblings_are_assignment_authority(runtime):
    worker(runtime, "root", "parent")
    worker(runtime, "root", "child")  # UI routing copy
    worker(runtime, "parent", "child")
    worker(runtime, "ancestor", "root")  # an ancestor without a child routing copy
    worker(runtime, "root", "sibling")
    worker(runtime, "unrelated", "fork")
    for sender in ("parent", "root", "ancestor"):
        assert agent_messaging.message_provenance("child", sender) == "parent"
    for sender in ("sibling", "fork", "unknown", "foreign", "child"):
        assert agent_messaging.message_provenance("child", sender) == "peer"

async def test_persisted_parent_rows_are_instance_scoped(peers, runtime, monkeypatch):
    from app.core import task_persistence, task_registry as registries
    from app.core.config import settings
    with monkeypatch.context() as foreign:
        foreign.setattr(settings, "instance_id", "foreign-install")
        worker(runtime, "foreign-parent", "child")
        await task_persistence.flush()
    monkeypatch.setattr(registries, "task_registry", registries.TaskRegistry())
    assert agent_messaging._persisted_parents("child") == []
    assert agent_messaging.message_provenance("child", "foreign-parent") == "peer"


@pytest.fixture
async def lineage_threads(monkeypatch):
    """Fail deterministically if SQL or mutable host state crosses its thread."""
    import threading
    from app.core import remote_task, task_registry as registries
    loop_thread = threading.get_ident()
    original = agent_messaging._persisted_parents
    reads = []

    def off_loop(child):
        assert threading.get_ident() != loop_thread, "SQL ran on the event loop"
        reads.append(child)
        return original(child)

    def on_loop(function):
        def guarded(*args, **kwargs):
            assert threading.get_ident() == loop_thread, "loop-owned state read in a thread"
            return function(*args, **kwargs)
        return guarded

    monkeypatch.setattr(agent_messaging, "_persisted_parents", off_loop)
    for name in ("get_all", "get_task", "sessions_with_task"):
        monkeypatch.setattr(registries.task_registry, name,
                            on_loop(getattr(registries.task_registry, name)))
    monkeypatch.setattr(agent_messaging.swarm, "peer_rows", on_loop(agent_messaging.swarm.peer_rows))
    monkeypatch.setattr(remote_task, "remote_parent", on_loop(remote_task.remote_parent))
    return reads


async def test_async_roster_and_delivery_thread_boundaries(peers, runtime, lineage_threads):
    from fluid.tools.registry import ToolCtx
    reads = lineage_threads
    engine = worker(runtime, "parent", "child")
    await agent_messaging.make_roster(lambda: "parent")({}, ToolCtx())
    assert reads
    reads.clear()
    outcome = await agent_messaging.make_message(lambda: "parent")(
        {"to": "child", "message": "Updated scope"}, ToolCtx(),
    )
    assert not outcome.is_error, outcome.text
    assert engine.submit.await_args.args[0].provenance == "parent"
    assert "child" in reads
