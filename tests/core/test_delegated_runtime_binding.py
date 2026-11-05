"""The retained delegated MCP server must follow acknowledged pooled model changes."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from app.core import query_options, session_pool, turn_manager
from app.core.provider import harness_adapter
from app.core.session_modes import CALLER_AUTHENTICATED
from app.db import sessions, turns
from tests.core import test_start_model_replay_scope as replay_fixtures

world = replay_fixtures.world
chats = replay_fixtures.chats
remote_wire = replay_fixtures.remote_wire


class _ControlledDelegatedLoop:
    """Only CLI execution/acknowledgment is controlled; host lifecycle is real."""

    def __init__(self, **kwargs):
        self.store = kwargs["store"]
        self.model = kwargs["model"]
        self.session_id = kwargs.get("session_id")
        self.received = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()
        self.fail = False
        self.attempts = []

    async def _ensure_session(self):
        meta = await self.store.get_session(self.session_id) if self.session_id else None
        if meta is None:
            meta = await self.store.create_session(self.session_id)
        self.session_id = meta.session_id
        return meta

    async def submit(self, operation):
        self.attempts.append(operation.model)
        self.received.set()
        await self.release.wait()
        if self.fail:
            raise RuntimeError("controlled CLI switch refusal")
        self.model = operation.model

    async def aclose(self):
        pass


@pytest.fixture
async def runtime(chats, remote_wire, monkeypatch, tmp_path):
    db = chats[0]
    db.update_profile("agent", config={"cwd": str(tmp_path)})
    sessions.create_session("live-default", "agent", title="Default CLI", is_active=True,
                            project_id="task-project", permission_mode="default")
    tid = turn_manager._submit_blocking("live-default", "user", prompt="do the work",
                                       profile_id="agent", caller=CALLER_AUTHENTICATED)
    assert turns.start_turn(tid) is not None
    monkeypatch.setattr(query_options, "_default_provider_model_without_claude", lambda **kwargs: None)
    monkeypatch.setattr(harness_adapter, "_resolve_claude_cmd", lambda: ["controlled-cli"])
    monkeypatch.setattr(harness_adapter, "DelegatedLoop", _ControlledDelegatedLoop)

    def build():
        return query_options.build_options_from_profile(
            profile=db.get_profile("agent"), session_id="live-default",
            overrides={"cwd": str(tmp_path)})[0]

    options = build()
    adapter = harness_adapter.HarnessAdapter()
    await adapter.connect_with_sdk_options(options)
    assert adapter.provider_kind == "delegated"
    state = session_pool.SessionState(adapter=adapter, connect_options=options, is_connected=True)
    server = options.mcp_servers["builtins"]
    start = next(tool for tool in server["instance"].tools if tool.name == "StartSession")
    remote_wire["offline"] = False
    try:
        yield SimpleNamespace(db=db, options=options, adapter=adapter, state=state,
                              server=server, start=start, build=build, tid=tid, prompts=chats[2])
    finally:
        await adapter.disconnect()


def _next_options(runtime, model):
    # The real compatibility init writer/legacy recovery path resolves a new
    # run without making an explicit composer or turn selection.
    runtime.db.update_session("live-default", launched_model=model)
    options = runtime.build()
    assert runtime.db.get_session("live-default")["model"] is None
    assert turns.get_turn(runtime.tid)["overrides"].get("model") is None
    assert options.model == model
    assert options.mcp_servers["builtins"]["instance"] is not runtime.server["instance"]
    return options


async def _child(runtime, worker, request_id):
    args = {"title": "Child", "prompt": "go", "request_id": request_id}
    if worker:
        args["worker"] = worker
    # Invoke the ORIGINAL registered MCP producer, retained by the live adapter.
    assert runtime.adapter._options.mcp_servers["builtins"] is runtime.server
    result = await runtime.start.handler(args)
    assert not result.get("isError"), result
    return runtime.db.get_session(json.loads(result["content"][0]["text"])["session_id"])


@pytest.mark.parametrize("worker", [None, "Other"])
@pytest.mark.parametrize("outcome", ["success", "failure", "noop"])
async def test_delegated_reuse_inherits_only_the_applied_model(runtime, worker, outcome):
    before = runtime.options.model
    requested = before if outcome == "noop" else "opus"
    assert before != "opus"
    replacement = _next_options(runtime, requested)
    assert not session_pool.requires_reconnect(runtime.state, replacement)
    engine = runtime.adapter._engine
    engine.fail = outcome == "failure"
    if outcome == "failure":
        with pytest.raises(RuntimeError, match="controlled CLI switch refusal"):
            await runtime.adapter.set_model(replacement.model)
    elif replacement.model != runtime.options.model:
        await runtime.adapter.set_model(replacement.model)
    applied = requested if outcome == "success" else before
    assert runtime.adapter._engine is engine
    assert runtime.options.model == engine.model == applied
    assert engine.attempts == ([] if outcome == "noop" else [requested])
    # Stale launch metadata must not be mistaken for the actual engine binding.
    runtime.db.update_session("live-default", launched_model="haiku")
    child = await _child(runtime, worker, "after-switch")
    assert (child["model"], child["provider_id"]) == (applied, None)
    assert not runtime.prompts


@pytest.mark.parametrize("worker", [None, "Other"])
async def test_delegated_binding_does_not_publish_switch_intent_before_ack(runtime, worker):
    before = runtime.options.model
    replacement = _next_options(runtime, "opus")
    assert not session_pool.requires_reconnect(runtime.state, replacement)
    engine = runtime.adapter._engine
    engine.release.clear()
    switch = asyncio.create_task(runtime.adapter.set_model(replacement.model))
    try:
        await asyncio.wait_for(engine.received.wait(), 2)
        assert runtime.options.model == engine.model == before
        child = await _child(runtime, worker, "before-ack")
        assert (child["model"], child["provider_id"]) == (before, None)
    finally:
        engine.release.set()
        await switch
    child = await _child(runtime, worker, "after-ack")
    assert (child["model"], child["provider_id"]) == ("opus", None)
    assert not runtime.prompts


def test_delegated_provider_change_still_requires_reconnect(runtime):
    sessions.set_session_run_binding("live-default", model="prov:lane:astra")
    replacement = runtime.build()
    assert session_pool.requires_reconnect(runtime.state, replacement)
    assert "aih_provider_id" in session_pool.reconnect_option_names(runtime.state, replacement)
    assert runtime.options.aih_provider_id is None and replacement.aih_provider_id == "lane"
    assert not runtime.adapter._engine.attempts
