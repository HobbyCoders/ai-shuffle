"""Unattended runs never wait on a person for a guarded generation tool.

An automation in Ask mode put ``generateImage`` to a permission prompt nobody
could answer, so the run hung until its timeout and the tool's own unattended
skip (``skipped/no_stored_choice``) never happened. The guarded generation
tools now go straight to their admission in unattended runs; every other tool,
and every interactive run, still asks. Above-threshold spend never confirms
itself unattended (tests/core/test_generation_resolver.py
``test_unattended_run_cannot_approve_its_own_spend``).

Drives the real ``can_use_tool`` closure a turn wires, as in
tests/core/test_query_engine_permission_flows.py.
"""

import asyncio
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import app.core.connectors.manager  # noqa: F401 — see test_query_engine_permission_flows
import app.core.query_engine as qe
from app.core.generation_resolver import RunContext, admission_gates_unattended
from app.core.permission_handler import permission_handler
from app.mcp import ai_tools_sdk as sdk
from tests.core.engine_harness import collect
from tests.core.query_engine_flow_harness import engine_patches, frames_of

SID = "qe-unattended-generation"
CLI_NAME = "mcp__fluid_bundled__mcp__ai-tools__generateImage"


@pytest.fixture(autouse=True)
def _clean_rules():
    permission_handler.clear_session_rules(SID)
    with patch("app.db.database.get_permission_rules", return_value=[]):
        yield
    permission_handler.clear_session_rules(SID)


def _allowed(result) -> bool:
    return type(result).__name__ == "PermissionResultAllow"


async def _callback(tmp_path, context, *, mode="default"):
    broadcast = AsyncMock()
    with ExitStack() as stack:
        bounds = engine_patches(stack, cwd=str(tmp_path / "missing"),
                                session_row={"permission_mode": mode},
                                profile={"id": "claude-code", "config": {}})
        bounds.options.aih_generation_context = context
        events = await collect(qe._stream_to_websocket_impl(
            "hi", SID, "claude-code", overrides={"permission_mode": mode, "_permission_explicit": True},
            broadcast_func=broadcast, user_id="user-1",
        ))
    assert frames_of(events, "error")[0]["message"].startswith("Working directory does not exist")
    return bounds.options.can_use_tool, broadcast


def _prompts(broadcast):
    return [c.args[0] for c in broadcast.await_args_list
            if c.args and c.args[0].get("type") == "permission_request"]


@pytest.mark.parametrize("name", [CLI_NAME, "mcp__ai-tools__generateVideo", "mcp__ai-tools__runFalModel",
                                  "GenerateNativeImage"])
async def test_unattended_guarded_generation_goes_to_admission_without_a_prompt(tmp_path, name):
    cb, broadcast = await _callback(tmp_path, RunContext(session_id=SID, unattended=True))
    result = await asyncio.wait_for(cb(name, {"prompt": "a tree"}, SimpleNamespace(tool_use_id="t", agent_id=None)), 5)
    assert _allowed(result) and result.updated_input == {"prompt": "a tree"}
    assert _prompts(broadcast) == []


async def test_a_saved_deny_rule_still_wins_unattended(tmp_path):
    permission_handler._add_session_rule_idempotent(SID, CLI_NAME, None, "deny")
    cb, broadcast = await _callback(tmp_path, RunContext(session_id=SID, unattended=True))
    result = await asyncio.wait_for(cb(CLI_NAME, {"prompt": "a tree"}, SimpleNamespace(tool_use_id="t", agent_id=None)), 5)
    assert not _allowed(result)


@pytest.mark.parametrize("context,name", [
    (RunContext(session_id=SID, unattended=False, owner=True), CLI_NAME),  # the owner is here to answer
    (RunContext(session_id=SID, unattended=True), "mcp__ai-tools__editImage"),  # not admission-gated
    (RunContext(session_id=SID, unattended=True), "mcp__other__generateImage"),  # not our server
    (None, CLI_NAME),  # no run context: nothing is known, so it asks
])
async def test_everything_else_still_asks(tmp_path, context, name):
    cb, broadcast = await _callback(tmp_path, context)
    call = asyncio.create_task(cb(name, {"prompt": "a tree"}, SimpleNamespace(tool_use_id="t", agent_id=None)))
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


async def test_unattended_generate_with_no_stored_choice_skips_once_admitted():
    # What the run reaches now instead of a hung prompt.
    tools = {t.name: t for t in sdk._create_unified_tools(None, RunContext(session_id=SID, unattended=True))}
    response = await tools["generateImage"].handler({"prompt": "a tree"})
    import json
    output = json.loads(response["content"][0]["text"])
    assert output["status"] == "skipped" and output["reason"] == "no_stored_choice"


def test_predicate_needs_a_real_unattended_run_context():
    from unittest.mock import MagicMock
    assert admission_gates_unattended(CLI_NAME, RunContext(unattended=True))
    assert not admission_gates_unattended(CLI_NAME, RunContext(unattended=False))
    # A mock or missing context is never read as unattended.
    assert not admission_gates_unattended(CLI_NAME, MagicMock(unattended=True))
    assert not admission_gates_unattended(CLI_NAME, None)
