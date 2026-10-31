"""The delegated tool boundary is armed per process, never by message text."""
from pathlib import Path

import pytest

from fluid.engine.delegated import DelegatedLoop, TOOL_OUTPUT_GUARD_MESSAGE
from fluid.engine.delegated_proto import HookCallbackRequest
from fluid.store.base import MemoryStore


def armed_loop(tmp_path):
    loop = DelegatedLoop(store=MemoryStore(), model="fixture")
    loop._tool_output_guard_required = True
    loop._tool_output_guard_nonce = "process-one"
    loop._tool_output_guard_receipt = str(tmp_path / "host-receipt")
    return loop


def call(name="Read", **extra):
    return HookCallbackRequest(
        cli_request_id="request", callback_id="fluid_PreToolUse", tool_use_id="toolu_1",
        input={"tool_name": name, "tool_input": {}, **extra},
    )


@pytest.mark.parametrize("name", ["Read", "Bash", "mcp__fixture__report"])
@pytest.mark.parametrize("agent", [None, "child-agent"])
async def test_missing_guard_denies_main_and_subagent_tools(tmp_path, name, agent):
    loop = armed_loop(tmp_path)
    reply = await loop._guarded_pre_tool_use(call(name, agent_id=agent, guard_ready=True))
    assert reply["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert reply["hookSpecificOutput"]["permissionDecisionReason"] == TOOL_OUTPUT_GUARD_MESSAGE


@pytest.mark.parametrize("content", ["", "process-zero", "process-one\n", "process-one" + "x" * 300])
async def test_only_the_exact_process_receipt_counts(tmp_path, content):
    loop = armed_loop(tmp_path)
    Path(loop._tool_output_guard_receipt).write_text(content, encoding="utf-8")
    assert not loop._check_tool_output_guard()
    assert (await loop._guarded_pre_tool_use(call()))["hookSpecificOutput"]["permissionDecision"] == "deny"


async def test_ready_guard_keeps_the_existing_policy_and_is_revoked_on_stop(tmp_path):
    loop = armed_loop(tmp_path)
    Path(loop._tool_output_guard_receipt).write_text(loop._tool_output_guard_nonce, encoding="utf-8")
    loop.tool_policy = lambda name, args: "host policy still applies"
    reply = await loop._guarded_pre_tool_use(call())
    assert loop._tool_output_guard_ready
    assert "host policy still applies" in reply["hookSpecificOutput"]["permissionDecisionReason"]
    await loop._stop_process()
    assert not loop._tool_output_guard_ready
    assert loop._tool_output_guard_nonce == ""
    assert loop._tool_output_guard_receipt is None
