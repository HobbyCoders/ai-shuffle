"""The delegated tool boundary is armed per process, never by message text."""
import logging
import sys
from pathlib import Path

import pytest

from fluid.engine import delegated_guard_policy
from fluid.engine.delegated import DelegatedLoop
from fluid.engine.delegated_proto import HookCallbackRequest
from fluid.events.model import HookEvent, TurnComplete
from fluid.events.ops import UserTurn
from fluid.store.base import MemoryStore


FAKE = str(Path(__file__).parents[1] / "fixtures" / "fake_claude.py")


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
@pytest.mark.parametrize("version", ["2.0.0", "2.1.314", None])
async def test_missing_guard_warns_but_keeps_main_and_subagent_tools_running(
    tmp_path, name, agent, version,
):
    assert delegated_guard_policy.UNAVAILABLE_GUARD_POLICY == "warn"
    loop = armed_loop(tmp_path)
    loop.cli_version = version
    assert await loop._guarded_pre_tool_use(call(name, agent_id=agent, guard_ready=True)) == {}
    assert not loop._tool_output_guard_ready


@pytest.mark.parametrize("content", ["", "process-zero", "process-one\n", "process-one" + "x" * 300])
async def test_only_the_exact_process_receipt_counts(tmp_path, content):
    loop = armed_loop(tmp_path)
    Path(loop._tool_output_guard_receipt).write_text(content, encoding="utf-8")
    assert not loop._check_tool_output_guard()
    assert await loop._guarded_pre_tool_use(call()) == {}


async def test_ready_guard_keeps_the_existing_policy_and_is_revoked_on_stop(tmp_path):
    loop = armed_loop(tmp_path)
    receipt = Path(loop._tool_output_guard_receipt)
    receipt.write_text(loop._tool_output_guard_nonce, encoding="utf-8")
    loop.tool_policy = lambda name, args: "host policy still applies"
    reply = await loop._guarded_pre_tool_use(call())
    assert loop._tool_output_guard_ready
    assert "host policy still applies" in reply["hookSpecificOutput"]["permissionDecisionReason"]
    await loop._stop_process()
    assert not loop._tool_output_guard_ready
    assert loop._tool_output_guard_nonce == ""
    assert loop._tool_output_guard_receipt is None
    assert not receipt.exists()


@pytest.mark.parametrize(("policy", "version", "denied"), [
    ("deny", "2.0.0", True), ("warn", "2.1.314", False),
    ("split", "2.1.285", False), ("split", "2.1.286", True),
    ("split", "2.1.314", True), ("split", None, True), ("split", "unknown", True),
])
def test_unavailable_behavior_is_a_single_separate_switch(
    tmp_path, monkeypatch, policy, version, denied,
):
    monkeypatch.setattr(delegated_guard_policy, "UNAVAILABLE_GUARD_POLICY", policy)
    loop = armed_loop(tmp_path)
    loop.cli_version = version
    assert loop._missing_output_guard_denies() is denied


@pytest.mark.parametrize("version", ["2.1.285", "2.1.314", None])
async def test_warns_once_in_chat_and_logs_error_without_bypassing_policy(
    tmp_path, caplog, version,
):
    loop = armed_loop(tmp_path)
    loop.cli_version = version
    await loop._ensure_session()
    loop.tool_policy = lambda name, args: "host deny" if name == "Bash" else None
    with caplog.at_level(logging.ERROR, logger="fluid.engine.delegated"):
        assert await loop._guarded_pre_tool_use(call("Read")) == {}
        second = await loop._guarded_pre_tool_use(call("Bash"))
        await loop._stop_process()
        assert await loop._guarded_pre_tool_use(call("Read")) == {}
    assert second["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "host deny" in second["hookSpecificOutput"]["permissionDecisionReason"]
    warnings = [e for e in loop.bus.log(loop.session_id)
                if isinstance(e, HookEvent) and e.level == "warning"]
    assert len(warnings) == 1
    assert "Tools will continue" in warnings[0].message
    assert len([r for r in caplog.records if r.levelno == logging.ERROR]) == 1


async def test_guard_warning_reporting_failure_never_blocks_the_chat(tmp_path, monkeypatch, caplog):
    loop = armed_loop(tmp_path)

    async def broken_report(*args):
        raise RuntimeError("fixture event store unavailable")

    monkeypatch.setattr(loop, "_hook_event", broken_report)
    with caplog.at_level(logging.ERROR, logger="fluid.engine.delegated"):
        assert await loop._guarded_pre_tool_use(call()) == {}
        assert await loop._guarded_pre_tool_use(call()) == {}
    assert len([r for r in caplog.records if r.levelno == logging.ERROR]) == 1


@pytest.mark.parametrize("guidance_mod", [True, False])
async def test_close_removes_session_guard_copy_and_receipt(
    tmp_path, guidance_mod,
):
    loop = DelegatedLoop(
        store=MemoryStore(), model="fixture", claude_cmd=[sys.executable, FAKE],
        cwd=str(tmp_path), claude_mod=guidance_mod,
        env={"FAKE_CLAUDE_VERSION": "claude 2.1.314 (fixture)"},
    )
    try:
        await loop.submit(UserTurn(content="hi"))
        await loop.wait_turn()
        assert isinstance(loop.bus.log(loop.session_id)[-1], TurnComplete)
        session_dir = Path(loop._tmpdir)
        receipt = Path(loop._tool_output_guard_receipt)
        assert receipt.is_file()
        assert (session_dir / "tool-output-guard" / "hooks" / "register.ts").is_file()
        assert (session_dir / "claude-mod").is_dir() is guidance_mod
        await loop._stop_process()
        assert not receipt.exists()
    finally:
        await loop.aclose()
    assert not session_dir.exists()
