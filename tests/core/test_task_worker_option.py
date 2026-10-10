"""Task and Swarm ``worker``: offered only with other workers, validated by the
shared resolver, refused by name; omitted or this worker runs here as before."""
import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import app.core.task_dispatch as td
from app.core.provider_agent import WorkerOutcome
from app.network import membership, runners

SELF = {"device_id": "dev-gaming", "device_name": "DesktopWorker", "online": True, "is_self": True,
        "capabilities": {"os": "windows", "remote_task": 1}}
UNRAID = {"device_id": "dev-unraid", "device_name": "Unraid", "online": True, "is_self": False,
          "platform": "linux", "capabilities": {"os": "linux", "has_display": False, "docker": True,
                                                "claude_cli_signed_in": True, "remote_task": 1}}
OLD = {"device_id": "dev-old", "device_name": "OldBox", "online": True, "is_self": False,
       "capabilities": {"os": "linux"}}
LAPTOP = {"device_id": "dev-laptop", "device_name": "Laptop", "online": False, "is_self": False,
          "capabilities": {"os": "darwin", "has_display": True, "remote_task": 1}}
LISTING = {"runners": [SELF, UNRAID, OLD, LAPTOP]}


class _Ctx:
    device_id = "dev-gaming"

    @staticmethod
    def member(device_id):
        return SimpleNamespace(kind=membership.KIND_WORKER, status=membership.STATUS_ACTIVE)


@pytest.fixture()
def network(monkeypatch):
    monkeypatch.setattr(runners.engine, "context", lambda: _Ctx())
    monkeypatch.setattr(runners, "directory", lambda: LISTING)


@pytest.fixture()
def caller(mock_db):
    from app.db import database

    database.create_profile("root-agent", "Root Agent", None, {"model": "opus"})
    database.create_profile("helper", "Helper", None, {"model": "sonnet"})
    database.create_session(session_id="s-root", profile_id="root-agent", title="root")
    return database


def _dispatch(args, session_id="s-root"):
    calls = []

    async def fake_worker(**kwargs):
        calls.append(kwargs)
        return WorkerOutcome(ok=True, session_id="w1", report="done")

    with patch("app.core.provider_agent.dispatch_worker", side_effect=fake_worker):
        payload = asyncio.run(td.dispatch_task_tool(
            {"agent": "helper", "prompt": "go", "mode": "sync", **args}, caller_session_id=session_id))
    return payload, calls


# ─── dispatch ────────────────────────────────────────────────────────────────


def test_omitted_worker_runs_here_without_touching_the_resolver(caller, monkeypatch):
    def _never(*a, **k):
        raise AssertionError("no worker named, so no resolution")
    monkeypatch.setattr(runners, "resolve_worker", _never)
    payload, calls = _dispatch({})
    assert payload["ok"] is True and payload["report_head"] == "done"
    assert len(calls) == 1 and "worker" not in calls[0]


@pytest.mark.parametrize("pick", ["DesktopWorker", "desktopworker", "dev-gaming"])
def test_naming_this_worker_runs_here(caller, network, pick):
    payload, calls = _dispatch({"worker": pick})
    assert payload["ok"] is True and len(calls) == 1


@pytest.mark.parametrize("pick, error", [
    ("Nope", "Unknown worker 'Nope'. Workers on this account: DesktopWorker (this worker), Unraid (online), "
             "OldBox (online), Laptop (offline)."),
    ("Laptop", "Laptop is offline; nothing was dispatched. Ask the user to bring it online, or omit "
               "worker to run it here."),
    ("OldBox", "OldBox runs an older release that cannot run remote subagents; nothing was dispatched. "
               "Ask the owner to update it, or omit worker to run it here."),
])
def test_other_picks_are_refused_by_name_before_any_worker(caller, network, pick, error):
    payload, calls = _dispatch({"worker": pick})
    assert payload == {"ok": False, "error": error}
    assert calls == []


@pytest.mark.parametrize("pick", ["Unraid", "dev-unraid"])
def test_a_valid_remote_pick_goes_to_the_remote_dispatch_not_the_local_one(caller, network, monkeypatch, pick):
    from app.core import remote_task

    seen = []

    async def fake_remote(**kwargs):
        seen.append(kwargs)
        return {"ok": True, "mode": "remote", "task_id": "w-remote"}
    monkeypatch.setattr(remote_task, "dispatch_remote", fake_remote)
    payload, calls = _dispatch({"worker": pick})
    assert payload == {"ok": True, "mode": "remote", "task_id": "w-remote"}
    assert calls == []
    [kwargs] = seen
    assert kwargs["worker"]["device_id"] == "dev-unraid"
    assert kwargs["profile_id"] == "helper" and kwargs["caller_session_id"] == "s-root"
    assert kwargs["root_session_id"] == "s-root" and kwargs["depth"] == 1
    assert kwargs["prompt"] == "go" and kwargs["mode"] == "sync"


def test_a_remote_pick_is_refused_at_once_when_the_root_tree_is_full(caller, network, monkeypatch):
    """A remote child has no local admission queue, so a full worker budget
    refuses it before anything is sent."""
    from app.core import remote_task
    from app.core.config import settings

    monkeypatch.setattr(settings, "task_tool_max_workers", 2)
    monkeypatch.setattr(td, "_live_worker_count_for_root", lambda root: 2)

    async def never(**kwargs):
        raise AssertionError("a full tree must not reach the remote worker")
    monkeypatch.setattr(remote_task, "dispatch_remote", never)
    payload, calls = _dispatch({"worker": "Unraid"})
    assert payload["ok"] is False and "Worker budget reached (2 live workers" in payload["error"]
    assert calls == []


def test_file_agents_cannot_run_on_another_worker(caller, network):
    registry = {"reviewer": {"name": "reviewer", "prompt": "Review.", "tools": ["Read"]}}
    with patch("app.core.provider_agent.dispatch_worker") as local:
        payload = asyncio.run(td.dispatch_task_tool(
            {"agent": "reviewer", "prompt": "go", "worker": "Unraid"},
            caller_session_id="s-root", agent_registry=registry))
    assert payload["ok"] is False and "defined in a file or plugin" in payload["error"]
    local.assert_not_called()


def test_a_remote_child_cannot_hop_to_another_worker(caller, network, monkeypatch):
    from app.core import remote_task
    monkeypatch.setattr(remote_task, "remote_child_in_chain", lambda sid: True)
    payload, calls = _dispatch({"worker": "Unraid"})
    assert payload["ok"] is False and "its own subagents run only here" in payload["error"]
    assert calls == []


def test_not_networked_pick_is_refused(caller, monkeypatch):
    monkeypatch.setattr(runners.engine, "context", lambda: None)
    payload, calls = _dispatch({"worker": "Unraid"})
    assert payload["ok"] is False and "not part of a worker network" in payload["error"]
    assert calls == []


def test_api_key_sessions_cannot_name_another_worker(caller, network):
    from app.db import api_users
    api_users.create_api_user("key-1", "Key", "hash", profile_ids=["root-agent", "helper"])
    caller.create_session(session_id="s-key", profile_id="root-agent", title="key", api_user_id="key-1")
    from app.core import api_user_scope
    with patch.object(api_user_scope, "worker_refusal", return_value=None):
        payload, calls = _dispatch({"worker": "Unraid"}, "s-key")
    assert payload == {"ok": False, "error": "Only the owner's chats can run a subagent on another worker."}
    assert calls == []


def test_non_owner_principal_cannot_name_another_worker(caller, network, monkeypatch):
    from app.db import mobile_tool_identity
    monkeypatch.setattr(mobile_tool_identity, "read", lambda sid: {"kind": "paired", "owner": "x"})
    payload, _ = _dispatch({"worker": "Unraid"})
    assert payload["error"] == "Only the owner's chats can run a subagent on another worker."


def _remote_ok(monkeypatch):
    from app.core import remote_task
    seen = []

    async def fake_remote(**kwargs):
        seen.append(kwargs)
        return {"ok": True, "mode": "remote", "task_id": "w-remote"}
    monkeypatch.setattr(remote_task, "dispatch_remote", fake_remote)
    return seen


def test_the_owners_paired_device_can_name_another_worker(caller, network, monkeypatch):
    """A chat the owner runs from a signed-in phone or browser is a paired
    principal with owner authority, not the admin; it is still the owner (#2218)."""
    from app.core import mobile_tool_identity as identity
    from app.db import mobile_tool_identity
    paired = {"kind": "paired", "owner": "account:owner-1", "principal_id": "pcl-1",
              "account_id": "owner-1"}
    monkeypatch.setattr(mobile_tool_identity, "read", lambda sid: paired)
    monkeypatch.setattr(identity, "_owner_authority", lambda value: value is paired)
    seen = _remote_ok(monkeypatch)
    payload, calls = _dispatch({"worker": "Unraid"})
    assert payload == {"ok": True, "mode": "remote", "task_id": "w-remote"}
    assert calls == [] and len(seen) == 1


def test_a_paired_device_without_owner_authority_is_refused(caller, network, monkeypatch):
    from app.core import mobile_tool_identity as identity
    from app.db import mobile_tool_identity
    paired = {"kind": "paired", "owner": "account:other-1", "principal_id": "pcl-2",
              "account_id": "other-1"}
    monkeypatch.setattr(mobile_tool_identity, "read", lambda sid: paired)
    monkeypatch.setattr(identity, "_owner_authority", lambda value: False)
    seen = _remote_ok(monkeypatch)
    payload, _ = _dispatch({"worker": "Unraid"})
    assert payload["error"] == "Only the owner's chats can run a subagent on another worker."
    assert seen == []


def test_a_mirrored_owner_label_counts_as_the_owner_only_for_the_owners_account(
        caller, network, monkeypatch):
    """A chat moved here from another worker carries a synced label with no
    local principal; its account decides, as the transcript checks do."""
    from app.core import preference_identity
    from app.db import mobile_tool_identity
    label = {"kind": "paired", "owner": "account:owner-1", "principal_id": None,
             "account_id": "owner-1"}
    monkeypatch.setattr(mobile_tool_identity, "read", lambda sid: label)
    monkeypatch.setattr(preference_identity, "local_owner_account", lambda: "owner-1")
    seen = _remote_ok(monkeypatch)
    payload, _ = _dispatch({"worker": "Unraid"})
    assert payload["ok"] is True and len(seen) == 1

    monkeypatch.setattr(preference_identity, "local_owner_account", lambda: "someone-else")
    payload, _ = _dispatch({"worker": "Unraid"})
    assert payload["error"] == "Only the owner's chats can run a subagent on another worker."
    assert len(seen) == 1


def test_background_observation_cannot_name_another_worker(caller, network):
    from app.db import turns
    turns.enqueue_turn("s-root", "assistant_proactive", prompt="look",
                       payload={"_assistant_proactive": {"run": "r1"}})
    payload, calls = _dispatch({"worker": "Unraid"})
    assert payload == {"ok": False, "error": "Background observation cannot run a subagent on another worker."}
    assert calls == []


# ─── schema ──────────────────────────────────────────────────────────────────


def test_task_schema_offers_worker_only_with_other_workers(mock_db, monkeypatch):
    from app.core.agent_provider_models import build_task_contract

    monkeypatch.setattr(runners, "other_workers", lambda: [])
    alone = build_task_contract(None, None)
    assert "worker" not in alone.input_schema["properties"]

    monkeypatch.setattr(runners, "other_workers", lambda: [UNRAID, OLD, LAPTOP])
    shared = build_task_contract(None, None)
    worker = shared.input_schema["properties"]["worker"]
    assert worker["type"] == "string" and worker["maxLength"] == 200
    assert "worker" not in shared.input_schema["required"]
    text = worker["description"]
    assert "Laptop (darwin, display); OldBox (linux, no display, older release that cannot run remote " \
           "subagents); Unraid (linux, no display, Claude CLI signed in)" in text
    # What the merged remote slices do: background only, approvals in the
    # parent, questions and private access held back, messages reach it.
    assert "always runs in the background" in text
    assert "the project must be checked out there" in text
    assert "answers its approval prompts in the parent conversation" in text
    assert "questions and private-access requests are held back and listed in its report" in text
    assert "SendMessage reaches it there" in text
    assert "held back, not granted" not in text
    # Online state stays out so a worker going offline does not rewrite the prefix.
    assert "offline" not in text.replace("unknown or offline", "")
    # The rest of the contract is unchanged.
    assert {k: v for k, v in shared.input_schema["properties"].items() if k != "worker"} \
        == alone.input_schema["properties"]
    assert shared.description == alone.description


def test_swarm_items_follow_the_task_schema():
    import app.core.provider.harness_adapter as ha
    from app.core.agent_provider_models import TaskContract
    from fluid.tools.builtins import register_builtins
    from fluid.tools.builtins.swarm import SPEC as SWARM

    def contract(worker):
        properties = {"agent": {"type": "string"}, "prompt": {"type": "string"}}
        if worker:
            properties["worker"] = worker
        return TaskContract(description="Delegate.", targets=(), input_schema={
            "type": "object", "properties": properties, "required": ["agent", "prompt"]})

    registry = ha.ToolRegistry()
    register_builtins(registry)
    worker = {"type": "string", "maxLength": 200, "description": "Other workers: Unraid."}
    ha._install_task_contract(registry, contract(worker))
    items = registry.get("Swarm").input_schema["properties"]["tasks"]["items"]["properties"]
    assert items["worker"] == worker
    assert registry.get("Swarm").description == SWARM.description

    ha._install_task_contract(registry, contract(None))
    assert registry.get("Swarm").input_schema == SWARM.input_schema
    assert "worker" not in SWARM.input_schema["properties"]["tasks"]["items"]["properties"]


def test_swarm_without_a_swarm_tool_is_left_alone():
    import app.core.provider.harness_adapter as ha
    from app.core.agent_provider_models import TaskContract

    registry = ha.ToolRegistry()
    ha._install_task_contract(registry, TaskContract(description="d", targets=(), input_schema={
        "type": "object", "properties": {"worker": {"type": "string"}}}))
    assert registry.get("Swarm") is None
