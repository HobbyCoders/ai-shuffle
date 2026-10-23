"""Composer-only tuning must not change implicit child/home defaults."""
import asyncio
from types import SimpleNamespace
import pytest
from app.core import model_effort as me, session_orchestration as orch
from app.core.provider import harness_adapter as ha
from app.core.provider.types import SessionOptions
from app.db import database, model_providers
from tests.core.test_provider_agent import dispatch_env  # noqa: F401
from tests.core.test_remote_task import net, kicks, world, body_for, start_on_target, child_turns  # noqa: F401
from tests.core.test_session_orchestration import peers  # noqa: F401


@pytest.fixture
def sol(monkeypatch):
    from app.core import preference_identity, task_dispatch
    monkeypatch.setattr(task_dispatch, "_credential_gap", lambda _: None)
    row = {"id": "sol", "name": "Test subscription", "provider_type": "chatgpt",
           "enabled": True, "auth_mode": "chatgpt", "default_model": "gpt-6.1-sol",
           "models_json": [{"id": "gpt-6.1-sol", "reasoning_capabilities": {
               "version": 1, "effort_levels": ["low", "medium", "high", "xhigh"], "default_effort": "low"}}]}
    monkeypatch.setattr(model_providers, "get_model_provider", lambda *a, **kw: row)
    monkeypatch.setattr(database, "get_model_provider", lambda *a, **kw: row)
    monkeypatch.setattr(ha, "_lookup_provider_row", lambda _: row)
    monkeypatch.setattr(preference_identity, "principal_preference_identity", lambda _: ("account", "owner"))
    return row


def test_clamp_and_remembered_null_preserve_vendor_default(mock_db, sol):
    cap = me.effort_capabilities("gpt-6.1-sol", sol)
    assert cap["initial_effort"] == "high" and cap["default_effort"] == "low"
    assert me.clamp_effort(None, "gpt-6.1-sol", sol) == "low"
    assert me.resolve_child_effort("gpt-6.1-sol", "sol") == "low"
    database.set_user_preference("account", "owner", me.effort_preference_key("gpt-6.1-sol"), None)
    assert me.resolve_child_effort("gpt-6.1-sol", "sol") == "low"
    assert me.resolve_child_effort("gpt-6.1-sol", "sol", parent="medium") == "medium"
    opts = SessionOptions(model="gpt-6.1-sol", extra={"provider_id": "sol"})
    assert ha.model_default_effort(opts) == "low"


@pytest.mark.parametrize("remembered", [False, True])
def test_task_and_delegated_worker_producer_stays_low(dispatch_env, sol, remembered):
    from app.core import provider_agent as pa
    if remembered:
        database.set_user_preference("account", "owner", me.effort_preference_key("gpt-6.1-sol"), None)
    result = asyncio.run(pa.dispatch_worker(parent_session_id="root", context_session_id="caller",
        profile_id="child", prompt="offline", provider_id="sol", model_id="gpt-6.1-sol"))
    assert result.ok
    assert dispatch_env.create_kwargs["effort"] == "low"
    assert dispatch_env.tm.submitted[0]["overrides"]["effort"] == "low"


def test_remote_task_receiver_stays_low(world, sol):
    answer = start_on_target(world, body_for(world, model_id="gpt-6.1-sol", provider_id="sol"))
    row = database.get_session(answer["worker_session_id"])
    [turn] = child_turns(answer["worker_session_id"])
    assert row["effort"] == turn["overrides"]["effort"] == "low"


def test_local_and_remote_orchestration_producers_stay_low(peers, sol, monkeypatch):
    from app.network import engine, membership
    database.set_session_run_binding("caller", model="prov:sol:gpt-6.1-sol")
    args = {"title": "offline", "prompt": "offline", "request_id": "sol-local"}
    result = orch._queue("caller", args, "start")
    assert database.get_session(result["session_id"])["effort"] == "low"
    sender = database.get_session("caller")
    command = "orchestration:start:" + "a" * 64
    body = orch._remote_body(sender, {**args, "request_id": command}, {},
        {"orchestration_depth": 1, "peer_message_content": "offline",
         "orchestration_sender": "caller", "peer_sender_label": "Caller"},
        "offline", command, "f" * 64, permission_mode="default")
    assert body["effort"] == "low"
    body.update(effort=None, session_id=orch._remote_session_id(command))
    member = SimpleNamespace(kind=membership.KIND_WORKER, status=membership.STATUS_ACTIVE, name="Test")
    monkeypatch.setattr(engine, "context", lambda: SimpleNamespace(device_id="here", member=lambda _: member))
    remote = orch.accept_remote_start(body, "elsewhere")
    assert database.get_session(remote["session_id"])["effort"] == "low"
