"""Delegated leg hardening against the claude CLI's control protocol.

Driven against ``fake_claude.py``: a permission request the CLI withdraws
(``control_cancel_request``), PreToolUse answers that fail closed, the hook
events the loop raises itself (Notification, SessionEnd), the CLI version
log line, and the bundled bridge's ``alwaysLoad``.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from fluid.engine import delegated
from fluid.engine.delegated import DelegatedLoop
from fluid.engine.delegated_proto import HookCallbackRequest, parse_control_cancel
from fluid.events.model import (
    HookEvent,
    PermissionRequest,
    RateLimit,
    ToolResult,
    TurnComplete,
)
from fluid.events.ops import RespondPermission, UserTurn
from fluid.hooks.bus import (
    NOTIFICATION,
    PRE_TOOL_USE,
    SESSION_END,
    HookBus,
    HookDecision,
)
from fluid.store.base import MemoryStore
from fluid.tools.mcp.bundled_server import bundled_server_config
from fluid.tools.registry import ToolCtx

FAKE = str(Path(__file__).parent.parent / "fixtures" / "fake_claude.py")


def _fake_loop(scenario: str, tmp_path, **kwargs: Any) -> DelegatedLoop:
    return DelegatedLoop(
        store=MemoryStore(), model="claude-fake", claude_cmd=[sys.executable, FAKE],
        env={"FAKE_CLAUDE_SCENARIO": scenario}, cwd=str(tmp_path), **kwargs,
    )


def _spy_on_frames(loop: DelegatedLoop) -> list[dict]:
    frames: list[dict] = []
    original = loop._write_frame

    async def spy(frame: dict) -> None:
        frames.append(frame)
        await original(frame)

    loop._write_frame = spy  # type: ignore[method-assign]
    return frames


class _Recorder:
    def __init__(self, *events: str) -> None:
        self.bus = HookBus()
        self.seen: list[tuple[str, dict[str, Any]]] = []
        for event in events:
            self.bus.on(event, self._record)

    async def _record(self, event: str, payload: dict[str, Any]) -> HookDecision | None:
        self.seen.append((event, dict(payload)))
        return None


def test_parse_control_cancel() -> None:
    assert parse_control_cancel({"type": "control_cancel_request", "request_id": "r1"}) == "r1"
    assert parse_control_cancel({"type": "control_cancel_request"}) is None
    assert parse_control_cancel({"type": "control_request", "request_id": "r1"}) is None
    assert parse_control_cancel(None) is None


# ─── control_cancel_request while parked on a permission ────────────────────


async def test_a_withdrawn_permission_ends_the_wait_and_the_turn_carries_on(tmp_path) -> None:
    """Nobody answers the prompt. The CLI withdraws it (a PermissionRequest
    hook in its own settings answered first) and streams on: the wait ends at
    once instead of after PERMISSION_TIMEOUT, no reply goes back for the
    withdrawn request, and the frames read during the wait are mapped in
    order. Notification hooks fire on parking; SessionEnd once on close."""
    rec = _Recorder(NOTIFICATION, SESSION_END)
    loop = _fake_loop("v", tmp_path, hooks=rec.bus)
    frames = _spy_on_frames(loop)
    try:
        started = time.monotonic()
        await loop.submit(UserTurn(content="clean up"))
        await asyncio.wait_for(loop.wait_turn(), timeout=10)
        elapsed = time.monotonic() - started
        events = loop.bus.log(loop.session_id)
        perm = next(e for e in events if isinstance(e, PermissionRequest))
        # The host's late answer finds nothing pending and is dropped.
        await loop.submit(RespondPermission(request_id=perm.request_id, decision="allow"))
    finally:
        await loop.aclose()
        await loop.aclose()

    assert elapsed < 5
    assert loop._pending_permissions == {}
    assert not [
        f for f in frames
        if f.get("type") == "control_response"
        and f["response"].get("request_id") == "cli_req_withdrawn"
    ]
    kinds = [type(e) for e in events]
    assert kinds.index(PermissionRequest) < kinds.index(RateLimit) < kinds.index(ToolResult)
    result = next(e for e in events if isinstance(e, ToolResult))
    assert result.output == "cleaned" and not result.is_error
    assert isinstance(events[-1], TurnComplete)
    assert not [e for e in events if isinstance(e, HookEvent) and e.level == "error"]

    assert [event for event, _ in rec.seen] == [NOTIFICATION, SESSION_END]
    notification = rec.seen[0][1]
    assert notification["notification_type"] == "permission_prompt"
    assert notification["name"] == "Bash"
    assert notification["tool_use_id"] == "toolu_withdrawn_1"
    assert rec.seen[1][1]["reason"] == "other"


async def test_a_cancel_already_read_ahead_is_found_in_the_backlog(tmp_path) -> None:
    loop = DelegatedLoop(store=MemoryStore(), model="m")

    class _Stdout:
        async def readline(self) -> bytes:
            await asyncio.sleep(3600)
            return b""

    class _Proc:
        stdout = _Stdout()

    loop._proc = _Proc()  # type: ignore[assignment]
    loop._stdout_backlog = [
        b'{"type":"assistant"}\n',
        b'{"type":"control_cancel_request","request_id":"other"}\n',
        b'{"type":"control_cancel_request","request_id":"cli_1"}\n',
        b'{"type":"result"}\n',
    ]
    outcome = await asyncio.wait_for(loop._watch_parked_stdout("cli_1"), timeout=1)
    loop._proc = None
    assert outcome == "cancelled"
    assert loop._stdout_backlog == [
        b'{"type":"assistant"}\n',
        b'{"type":"control_cancel_request","request_id":"other"}\n',
        b'{"type":"result"}\n',
    ]


# ─── PreToolUse fails closed ─────────────────────────────────────────────────


class _BrokenBus(HookBus):
    async def emit(self, event: str, payload: dict[str, Any]) -> HookDecision:
        if event == PRE_TOOL_USE:
            raise RuntimeError("guard crashed")
        return await super().emit(event, payload)


async def _answer_pre_tool_use(loop: DelegatedLoop) -> dict:
    await loop._ensure_session()
    written: list[dict] = []

    async def capture(frame: dict) -> None:
        written.append(frame)

    loop._write_frame = capture  # type: ignore[method-assign]
    await loop._answer_hook_request(HookCallbackRequest(
        cli_request_id="cli_hook_1", callback_id="fluid_PreToolUse",
        input={"hook_event_name": "PreToolUse", "tool_name": "Bash",
               "tool_input": {"command": "curl -X POST x"}, "tool_use_id": "t1"},
        tool_use_id="t1",
    ))
    (frame,) = written
    assert frame["response"]["subtype"] == "success"
    return frame["response"]["response"]["hookSpecificOutput"]


async def test_a_raising_pre_tool_use_hook_answers_with_a_deny(tmp_path) -> None:
    loop = DelegatedLoop(
        store=MemoryStore(), model="m", hooks=_BrokenBus(), ctx=ToolCtx(cwd=str(tmp_path)),
    )
    output = await _answer_pre_tool_use(loop)
    assert output["permissionDecision"] == "deny"
    assert "RuntimeError: guard crashed" in output["permissionDecisionReason"]
    (event,) = [e for e in loop.bus.log(loop.session_id) if isinstance(e, HookEvent)]
    assert event.level == "deny" and event.tool_name == "Bash"


async def test_a_pre_tool_use_hook_past_its_budget_answers_with_a_deny(
    tmp_path, monkeypatch,
) -> None:
    monkeypatch.setattr(delegated, "PRE_TOOL_USE_HOOK_BUDGET", 0.05)
    bus = HookBus()

    async def slow(_event: str, _payload: dict) -> None:
        await asyncio.sleep(5)

    bus.on(PRE_TOOL_USE, slow)
    loop = DelegatedLoop(store=MemoryStore(), model="m", hooks=bus, ctx=ToolCtx(cwd=str(tmp_path)))
    output = await asyncio.wait_for(_answer_pre_tool_use(loop), timeout=2)
    assert output["permissionDecision"] == "deny"
    assert "did not decide within" in output["permissionDecisionReason"]


async def test_a_raising_guard_denies_the_cli_call_end_to_end(tmp_path) -> None:
    """The fake CLI asks the PreToolUse callback before running Bash; the
    crashed guard's deny reaches it and comes back as the tool's error. The
    callback is registered with an explicit timeout above the loop's budget."""
    loop = _fake_loop("u", tmp_path, hooks=_BrokenBus())
    frames = _spy_on_frames(loop)
    try:
        await loop.submit(UserTurn(content="run it"))
        await asyncio.wait_for(loop.wait_turn(), timeout=10)
        events = loop.bus.log(loop.session_id)
    finally:
        await loop.aclose()

    (result,) = [e for e in events if isinstance(e, ToolResult)]
    assert result.is_error and "guard crashed" in result.output
    init = next(
        f for f in frames
        if f.get("type") == "control_request" and f["request"]["subtype"] == "initialize"
    )
    hooks = init["request"]["hooks"]
    assert hooks["PreToolUse"][0]["timeout"] == delegated.PRE_TOOL_USE_CLI_TIMEOUT
    assert delegated.PRE_TOOL_USE_HOOK_BUDGET < delegated.PRE_TOOL_USE_CLI_TIMEOUT
    assert "timeout" not in hooks["PostToolUse"][0]


# ─── The tool policy fails closed too ────────────────────────────────────────


async def _pre_tool_use_answer(loop: DelegatedLoop) -> dict:
    """The whole PreToolUse answer for a Bash call ({} = no opinion)."""
    await loop._ensure_session()
    written: list[dict] = []

    async def capture(frame: dict) -> None:
        written.append(frame)

    loop._write_frame = capture  # type: ignore[method-assign]
    await loop._answer_hook_request(HookCallbackRequest(
        cli_request_id="cli_hook_1", callback_id="fluid_PreToolUse",
        input={"hook_event_name": "PreToolUse", "tool_name": "Bash",
               "tool_input": {"command": "curl -X POST x"}, "tool_use_id": "t1"},
        tool_use_id="t1",
    ))
    (frame,) = written
    assert frame["response"]["subtype"] == "success"
    return frame["response"]["response"]


def _raising_policy(name: str, tool_input: dict) -> str | None:
    raise RuntimeError("policy crashed")


async def test_a_raising_tool_policy_denies_the_call_in_bypass_mode(tmp_path, caplog) -> None:
    loop = DelegatedLoop(
        store=MemoryStore(), model="m", permission_mode="bypassPermissions",
        tool_policy=_raising_policy, ctx=ToolCtx(cwd=str(tmp_path)),
    )
    caplog.set_level(logging.ERROR, logger="fluid.engine.delegated")
    answer = await _pre_tool_use_answer(loop)
    output = answer["hookSpecificOutput"]
    assert output["permissionDecision"] == "deny"
    assert "RuntimeError" in output["permissionDecisionReason"]
    assert any(r.levelno == logging.ERROR and "tool policy failed" in r.getMessage()
               for r in caplog.records)


async def test_a_raising_tool_policy_beats_a_hooks_allow(tmp_path) -> None:
    bus = HookBus()

    async def approve(_event: str, _payload: dict) -> HookDecision:
        return HookDecision(permission_decision="allow", note="looks fine")

    bus.on(PRE_TOOL_USE, approve)
    loop = DelegatedLoop(
        store=MemoryStore(), model="m", hooks=bus, tool_policy=_raising_policy,
        ctx=ToolCtx(cwd=str(tmp_path)),
    )
    output = (await _pre_tool_use_answer(loop))["hookSpecificOutput"]
    assert output["permissionDecision"] == "deny"
    assert "RuntimeError" in output["permissionDecisionReason"]


async def test_an_async_tool_policy_is_awaited(tmp_path) -> None:
    assert DelegatedLoop.supports_async_tool_policy is True

    async def refuse(name: str, tool_input: dict) -> str | None:
        await asyncio.sleep(0)
        return "no"

    async def allow(name: str, tool_input: dict) -> str | None:
        await asyncio.sleep(0)
        return None

    async def crash(name: str, tool_input: dict) -> str | None:
        await asyncio.sleep(0)
        raise ValueError("rule store down")

    def loop_with(policy) -> DelegatedLoop:
        return DelegatedLoop(
            store=MemoryStore(), model="m", permission_mode="bypassPermissions",
            tool_policy=policy, ctx=ToolCtx(cwd=str(tmp_path)),
        )

    refused = (await _pre_tool_use_answer(loop_with(refuse)))["hookSpecificOutput"]
    assert refused == {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                       "permissionDecisionReason": "no"}
    assert await _pre_tool_use_answer(loop_with(allow)) == {}
    crashed = (await _pre_tool_use_answer(loop_with(crash)))["hookSpecificOutput"]
    assert crashed["permissionDecision"] == "deny"
    assert "ValueError" in crashed["permissionDecisionReason"]


# ─── CLI version log ─────────────────────────────────────────────────────────


async def test_the_cli_version_is_logged_once_per_spawn(tmp_path, caplog) -> None:
    loop = _fake_loop("a", tmp_path)
    caplog.set_level(logging.INFO, logger="fluid.engine.delegated")
    try:
        await loop.submit(UserTurn(content="hi"))
        await loop.wait_turn()
        await loop.submit(UserTurn(content="again"))
        await loop.wait_turn()
    finally:
        await loop.aclose()
    lines = [r.getMessage() for r in caplog.records if "claude CLI started" in r.getMessage()]
    assert len(lines) == 1
    assert "version=claude 2.0.0 (fake-fluid)" in lines[0]
    assert "leg=delegated" in lines[0] and loop.session_id in lines[0]


# ─── bundled bridge stays loaded next to ToolSearch ──────────────────────────


@pytest.mark.parametrize("token", [None, "secret"])
def test_the_bundled_bridge_is_always_loaded(token) -> None:
    assert bundled_server_config("/tmp/b.sock", token)["alwaysLoad"] is True
