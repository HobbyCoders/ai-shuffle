"""Unattended runs never wait on a person for a guarded generation tool.

An automation in Ask mode put ``generateImage`` to a permission prompt nobody
could answer, so the run hung until its timeout and the tool's own unattended
skip (``skipped/no_stored_choice``) never happened (#776). Only unattended,
background-only sources go straight to generation admission. Model-routing
attendance is broader: API, Resume, connectors, voice and unknown sources may
have a person present, so they retain Ask. Idle generation denies immediately.

The permission callback and the generation tools read ONE per-turn context
(``generation_resolver.turn_context``), stamped when the turn claims its
pooled client. A pooled Claude CLI client keeps the tool servers built at
connect, so an interactive connect reused by a wake-up turn (or the reverse)
must never let the callback and admission disagree.

Drives the real ``can_use_tool`` closure a turn wires, as in
tests/core/test_query_engine_permission_flows.py.
"""

import asyncio
import json
from contextlib import ExitStack
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.core.connectors.manager  # noqa: F401 — see test_query_engine_permission_flows
import app.core.query_engine as qe
from app.core import generation_resolver as gr
from app.core.generation_resolver import RunContext, admission_gates_unattended, bind_turn_context
from app.core.permission_handler import permission_handler
from app.mcp import ai_tools_sdk as sdk
from tests.core.engine_harness import collect
from tests.core.query_engine_flow_harness import RecordingPool, engine_patches, frames_of
from tests.core.test_generation_resolver import PRICE, FakeBackend, candidate

SID = "qe-unattended-generation"
CLI_NAME = "mcp__fluid_bundled__mcp__ai-tools__generateImage"
AUTOMATION = RunContext(session_id=SID, unattended=True, owner=True, turn_source="automation")
WAKEUP = RunContext(session_id=SID, unattended=True, owner=True, turn_source="wakeup")
VOICE = RunContext(session_id=SID, unattended=True, owner=True, turn_source="voice")
CONNECTOR = RunContext(session_id=SID, unattended=True, owner=True, turn_source="connector")
TYPED = RunContext(session_id=SID, unattended=False, owner=True, turn_source="user")


@pytest.fixture(autouse=True)
def _clean():
    gr._TURN_CONTEXTS.clear()
    permission_handler.clear_session_rules(SID)
    with patch("app.db.database.get_permission_rules", return_value=[]):
        yield
    permission_handler.clear_session_rules(SID)
    gr._TURN_CONTEXTS.clear()


def _bind(context):
    bind_turn_context(SimpleNamespace(aih_generation_context=context))


def _allowed(result) -> bool:
    return type(result).__name__ == "PermissionResultAllow"


def _ctx():
    return SimpleNamespace(tool_use_id="t", agent_id=None)


async def _callback(tmp_path, *, mode="default"):
    broadcast = AsyncMock()
    with ExitStack() as stack:
        bounds = engine_patches(stack, cwd=str(tmp_path / "missing"),
                                session_row={"permission_mode": mode},
                                profile={"id": "claude-code", "config": {}})
        events = await collect(qe._stream_to_websocket_impl(
            "hi", SID, "claude-code", overrides={"permission_mode": mode, "_permission_explicit": True},
            broadcast_func=broadcast, user_id="user-1",
        ))
    assert frames_of(events, "error")[0]["message"].startswith("Working directory does not exist")
    return bounds.options.can_use_tool, broadcast


def _prompts(broadcast):
    return [c.args[0] for c in broadcast.await_args_list
            if c.args and c.args[0].get("type") == "permission_request"]


async def _asks(cb, broadcast, name):
    call = asyncio.create_task(cb(name, {"prompt": "a tree"}, _ctx()))
    try:
        async def first_prompt():
            while not _prompts(broadcast):
                await asyncio.sleep(0.005)
            return _prompts(broadcast)[0]
        prompt = await asyncio.wait_for(first_prompt(), 15)
        assert prompt["tool_name"] == name
        await permission_handler.respond(prompt["request_id"], SID, "deny")
        assert not _allowed(await asyncio.wait_for(call, 5))
    finally:
        call.cancel()


# ─── the permission callback ────────────────────────────────────────────────


@pytest.mark.parametrize("source", [
    "automation", "wakeup", "auto_continue", "unpark", "failover", "agent_message",
])
@pytest.mark.parametrize("name", [CLI_NAME, "mcp__ai-tools__generateVideo", "mcp__ai-tools__runFalModel",
                                  "mcp__ai-tools__searchModels", "GenerateNativeImage"])
async def test_unattended_sources_go_to_admission_without_a_prompt(tmp_path, source, name):
    from app.core.query_options import _is_interactive_run

    cb, broadcast = await _callback(tmp_path)
    unattended = not _is_interactive_run({"turn_source": source}, {"id": SID})
    assert unattended
    _bind(replace(AUTOMATION, turn_source=source, unattended=unattended))
    with patch.object(permission_handler, "request_permission", new=AsyncMock(
        side_effect=AssertionError("Unattended generation must not ask a person"),
    )):
        result = await asyncio.wait_for(cb(name, {"prompt": "a tree"}, _ctx()), 5)
    assert _allowed(result) and result.updated_input == {"prompt": "a tree"}
    assert _prompts(broadcast) == []


async def test_a_saved_deny_rule_still_wins_unattended(tmp_path):
    permission_handler._add_session_rule_idempotent(SID, CLI_NAME, None, "deny")
    cb, broadcast = await _callback(tmp_path)
    _bind(AUTOMATION)
    result = await asyncio.wait_for(cb(CLI_NAME, {"prompt": "a tree"}, _ctx()), 5)
    assert not _allowed(result)


@pytest.mark.parametrize("turn,name", [
    (TYPED, CLI_NAME),  # the owner is here to answer
    (AUTOMATION, "mcp__ai-tools__editImage"),  # not admission-gated
    (AUTOMATION, "mcp__other__generateImage"),  # not our server
])
async def test_every_other_turn_and_tool_still_asks(tmp_path, turn, name):
    cb, broadcast = await _callback(tmp_path)
    if turn is not None:
        _bind(turn)
    await _asks(cb, broadcast, name)


async def test_the_callback_reads_the_current_turn_not_an_earlier_one(tmp_path):
    cb, broadcast = await _callback(tmp_path)
    _bind(AUTOMATION)
    _bind(TYPED)  # the next turn on the same pooled client is typed by the owner
    await _asks(cb, broadcast, CLI_NAME)


@pytest.mark.parametrize("source,mode", [
    ("api", None), ("resume", None), ("connector", None), ("voice", None),
    ("user", "voice"), ("automation", "voice"), ("wakeup", "voice"),
    ("unknown", None), (None, None),
])
async def test_person_attended_and_unknown_sources_keep_the_ask_prompt(tmp_path, source, mode):
    from app.core.agent_types import PermissionResultDeny
    from app.core.query_options import _generation_context, _is_interactive_run

    cb, broadcast = await _callback(tmp_path)
    row = {"id": SID, "mode": mode}
    unattended = not _is_interactive_run({"turn_source": source}, row)
    assert unattended  # model-routing attendance alone cannot suppress Ask
    _bind(_generation_context({}, row, None, session_id=SID,
                              unattended=unattended, turn_source=source))
    with patch.object(permission_handler, "request_permission", new=AsyncMock(
        return_value=PermissionResultDeny(message="fixture owner's reply"),
    )) as request:
        result = await asyncio.wait_for(cb(CLI_NAME, {"prompt": "a tree"}, _ctx()), 5)
    request.assert_awaited_once()
    assert not _allowed(result)


@pytest.mark.parametrize("name", [CLI_NAME, "GenerateNativeImage", "mcp__ai-tools__runFalModel",
                                  "mcp__ai-tools__searchModels"])
@pytest.mark.parametrize("mode", ["default", "bypassPermissions"])
async def test_a_late_permission_call_denies_without_an_unanswerable_prompt(tmp_path, name, mode):
    cb, broadcast = await _callback(tmp_path, mode=mode)
    _bind(AUTOMATION)
    gr.clear_turn_context(SID, expected=AUTOMATION)
    with patch.object(permission_handler, "request_permission", new=AsyncMock(
        side_effect=AssertionError("Idle generation must not open an Ask prompt"),
    )):
        result = await asyncio.wait_for(cb(name, {"prompt": "a tree"}, _ctx()), 5)
    assert not _allowed(result)
    assert "no active turn" in result.message.lower()
    assert _prompts(broadcast) == []


def test_predicate_needs_this_sessions_unattended_turn():
    assert admission_gates_unattended(CLI_NAME, AUTOMATION, session_id=SID)
    assert not admission_gates_unattended(CLI_NAME, AUTOMATION, session_id="another-session")
    assert not admission_gates_unattended(CLI_NAME, VOICE, session_id=SID)
    assert not admission_gates_unattended(CLI_NAME, replace(AUTOMATION, unattended=False), session_id=SID)
    # A mock or missing context is never read as unattended.
    assert not admission_gates_unattended(CLI_NAME, MagicMock(unattended=True, turn_source="automation",
                                                              session_id=SID), session_id=SID)
    assert not admission_gates_unattended(CLI_NAME, None, session_id=SID)


async def test_a_reused_pooled_client_stamps_this_turns_context(tmp_path):
    from tests.core.test_query_engine_turn_lifecycle import _pooled_state, _result

    state = _pooled_state([_result()])
    async def during_turn(*args, **kwargs):
        assert gr.turn_context(SID) is WAKEUP

    state.adapter.send_query.side_effect = during_turn
    with ExitStack() as stack:
        bounds = engine_patches(stack, pool=RecordingPool(state), cwd=str(tmp_path))
        bounds.options.aih_generation_context = WAKEUP
        events = await collect(qe._stream_to_websocket_impl("hi", SID, "claude-code"))
    assert frames_of(events, "done")
    bounds.make_adapter.assert_not_called()  # the pooled client was reused
    state.adapter.send_query.assert_awaited_once()
    assert gr.turn_context(SID) is None


# ─── one long-lived tool server across turns ────────────────────────────────


class _Receipts:
    """The durable receipt store's contract (app.core.generation_confirmations)."""

    def __init__(self):
        self.issued = {}

    async def issue(self, fingerprint):
        token = f"receipt-{len(self.issued)}"
        self.issued[token] = fingerprint
        return token

    async def consume(self, token, fingerprint):
        return self.issued.pop(token, None) == fingerprint


def _server(monkeypatch, connect, entries):
    from app.services import generation_sources

    backend = FakeBackend(entries)
    backend.confirmations = _Receipts()
    monkeypatch.setattr(generation_sources, "Sources", lambda *args, **kw: backend)
    tools = {t.name: t for t in sdk._create_unified_tools(None, connect)}

    async def call(args):
        response = await tools["generateImage"].handler({"prompt": "a tree", **args})
        return json.loads(response["content"][0]["text"])
    return call, backend


EXPENSIVE = replace(candidate(), price={**PRICE, "amount": "0.25", "total": "0.25"})
NAMED = {"source": "fal", "model": EXPENSIVE.model}


def _spent(backend):
    return [c for c in backend.calls if c[0] in ("reserve", "execute")]


async def test_interactive_connect_then_wakeup_turn_never_spends_above_the_threshold(monkeypatch):
    remembered = {"image": EXPENSIVE.choice()}
    call, backend = _server(monkeypatch, TYPED, [EXPENSIVE])
    _bind(replace(TYPED, remembered_choices=remembered))
    asked = await call({})
    assert asked["status"] == "needs-confirmation" and asked["confirmation_token"]
    # The same pooled client now runs a wake-up turn nobody watches, with the
    # same remembered pick: a receipt from the typed turn is never spent here.
    _bind(replace(WAKEUP, remembered_choices=remembered))
    for retry in ({"confirmed": True, "confirmation_token": asked["confirmation_token"]},
                  {**NAMED, "confirmed": True, "confirmation_token": asked["confirmation_token"]}, {}):
        output = await call(retry)
        assert output["status"] == "skipped" and output["reason"] == "needs_confirmation"
        assert "confirmation_token" not in output and "retry" not in output
    assert _spent(backend) == []
    # With nothing stored, it skips instead of offering a card.
    _bind(WAKEUP)
    assert (await call({}))["reason"] == "no_stored_choice"


async def test_unattended_connect_then_typed_turn_gets_its_card_and_approval(monkeypatch):
    call, backend = _server(monkeypatch, WAKEUP, [EXPENSIVE, candidate(account="account-b", model="model-b")])
    _bind(TYPED)
    assert (await call({}))["status"] == "choose"  # not skipped as unattended
    asked = await call(NAMED)
    assert asked["status"] == "needs-confirmation"
    approved = await call({**NAMED, "confirmed": True, "confirmation_token": asked["confirmation_token"]})
    assert approved["status"] == "completed"
    assert _spent(backend) == [("reserve", EXPENSIVE.account), ("execute", EXPENSIVE.account)]


async def test_the_native_alias_runs_under_the_current_turn_too():
    from fluid.tools.registry import ToolCtx

    from app.core.provider import native_generation_aliases as aliases

    backend = FakeBackend([candidate(source="chatgpt", account="plan-a", model="default")])
    with patch.object(aliases, "Sources", lambda *args, **kw: backend):
        _, handler = aliases.make_alias("image", TYPED)
        _bind(WAKEUP)
        output = json.loads((await handler({"prompt": "a tree"}, ToolCtx())).text)
    assert output["status"] == "skipped" and output["reason"] == "no_stored_choice"
    assert backend.calls == []


async def test_a_failed_choices_read_is_unreadable_not_empty():
    def broken():
        raise RuntimeError("store offline")

    context = replace(AUTOMATION, remembered_choices={"image": {"model": "m", "source": "fal"}},
                      choices_loader=broken)
    current = await gr.with_current_choices(context)
    assert current.choices_unreadable is True and current.remembered_choices == {}


@pytest.mark.parametrize("connected,turn,status", [
    (TYPED, WAKEUP, "skipped"),
    (WAKEUP, TYPED, "refused"),
])
async def test_run_fal_invalid_request_label_uses_the_current_turn(monkeypatch, connected, turn, status):
    from app.services import generation_sources
    from app.services.fal_schema import schema_service

    backend = FakeBackend([])
    backend.fal_key = AsyncMock(return_value=None)
    monkeypatch.setattr(generation_sources, "Sources", lambda *args, **kw: backend)
    monkeypatch.setattr(schema_service, "get_schema", AsyncMock(return_value=None))
    tool = next(t for t in sdk._create_unified_tools(None, connected) if t.name == "runFalModel")
    _bind(turn)
    response = await tool.handler({"endpoint_id": "fixture-model", "input": {}})
    output = json.loads(response["content"][0]["text"])
    assert output["reason"] == "invalid_request" and output["status"] == status
    assert backend.calls == []
