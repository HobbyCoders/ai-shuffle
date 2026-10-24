"""A call the host denies carries the user's waiting steer in its result (#523).

An owner chat answers a call that waits on the owner's approval with a "not
run yet" deny when the owner sends another message, so that message reaches
the model in the same turn. NativeLoop folds a steer into every tool result,
a denied one included. DelegatedLoop holds a user steer for the next
main-thread PostToolUse hook, which a denied call never reaches, so the deny
itself (the ``can_use_tool`` reply, or a PreToolUse deny) carries it.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from fluid.context.steering import STEER_CLOSE, STEER_OPEN
from fluid.engine.delegated_proto import HookCallbackRequest
from fluid.events import PermissionRequest, RespondPermission, SteerDelivered, ToolResult
from fluid.events.ops import AgentMessage, Interrupt, Steer, UserTurn

from tests.unit.test_delegated import _make_loop, _run_peer_boundary_turn, _user_frames
from tests.unit.test_web_and_steering import _ScriptedProvider, _text_script, _tool_script

PLACEHOLDER = "Not run yet: this call is waiting for the owner's approval."
OWNER_TEXT = "Instead, explain what that command would change"


def _marked(text: str) -> str:
    return f"{STEER_OPEN}\n{text}\n{STEER_CLOSE}"


# ─── NativeLoop ──────────────────────────────────────────────────────────────


async def test_native_denied_call_result_carries_the_waiting_steer() -> None:
    from fluid.engine.native import NativeLoop
    from fluid.permissions.engine import PermissionEngine
    from fluid.store.base import MemoryStore
    from fluid.tools.inproc import create_inproc_server, tool
    from fluid.tools.registry import ToolRegistry

    ran: list[dict] = []

    @tool("push", "Push the branch", {"type": "object"})
    async def push(args: dict) -> dict:
        ran.append(args)
        return {"content": [{"type": "text", "text": "pushed"}]}

    registry = ToolRegistry()
    registry.register_inproc(create_inproc_server("sv", [push]))
    loop = NativeLoop(
        provider=_ScriptedProvider([_tool_script("t1", "mcp__sv__push"), _text_script("ok")]),
        store=MemoryStore(), model="m", registry=registry,
        permissions=PermissionEngine("always_ask"),
    )

    async def answer() -> None:
        while True:
            log = loop.bus.log(loop.session_id) if loop._session is not None else []
            event = next((e for e in log if isinstance(e, PermissionRequest)), None)
            if event is not None:
                break
            await asyncio.sleep(0.01)
        # The host hands the owner's message over first, then lets the
        # waiting call go with the placeholder.
        await loop.submit(Steer(text=OWNER_TEXT, id="q1"))
        await loop.submit(RespondPermission(
            request_id=event.request_id, decision="deny", message=PLACEHOLDER,
        ))

    watcher = asyncio.create_task(answer())
    try:
        await loop.submit(UserTurn(content="push it"))
        await asyncio.wait_for(watcher, 10)
        await asyncio.wait_for(loop.wait_turn(), 10)
        events = loop.bus.log(loop.session_id)
        result = next(e for e in events if isinstance(e, ToolResult))
        receipt = next(e for e in events if isinstance(e, SteerDelivered))
        assert ran == []  # the call never ran
        assert result.is_error and result.output.startswith(PLACEHOLDER)
        assert _marked(OWNER_TEXT) in result.output
        assert receipt.seq > result.seq
        assert receipt.steers == [{"id": "q1", "text": OWNER_TEXT}]
    finally:
        watcher.cancel()
        await loop.aclose()


# ─── DelegatedLoop: the can_use_tool reply ───────────────────────────────────


def _permission_reply(frames: list[dict]) -> dict:
    return next(
        f["response"]["response"] for f in frames
        if f.get("type") == "control_response"
        and f["response"].get("request_id") == "cli_req_peer_1"
    )


async def test_delegated_deny_carries_the_waiting_user_steer() -> None:
    async def on_prompt(loop, event):
        await loop.submit(Steer(text=OWNER_TEXT, id="q1"))
        await loop.submit(RespondPermission(
            request_id=event.request_id, decision="deny", message=PLACEHOLDER,
        ))

    _loop, frames, events, seen = await _run_peer_boundary_turn(on_prompt)

    reply = _permission_reply(frames)
    assert reply["behavior"] == "deny"
    assert reply["message"].startswith(PLACEHOLDER)
    assert reply["message"].count(OWNER_TEXT) == 1 and _marked(OWNER_TEXT) in reply["message"]
    # Delivered once: the failed call's PostToolUse answer carries nothing more.
    assert seen == {"sub": "", "main": ""}
    receipts = [e for e in events if isinstance(e, SteerDelivered)]
    assert [r.steers for r in receipts] == [
        [{"id": "q1", "text": OWNER_TEXT, "tool_use_id": "toolu_peer_1"}],
    ]
    # Never a stdin frame the CLI could treat as a human follow-up (#1786).
    assert len(_user_frames(frames)) == 1


async def test_delegated_allow_leaves_the_steer_for_the_tool_boundary() -> None:
    """Unchanged: an allowed call runs, and its PostToolUse answer carries it."""
    async def on_prompt(loop, event):
        await loop.submit(Steer(text=OWNER_TEXT, id="q1"))
        await loop.submit(RespondPermission(request_id=event.request_id, decision="allow"))

    _loop, frames, events, seen = await _run_peer_boundary_turn(on_prompt)

    assert _permission_reply(frames)["behavior"] == "allow"
    assert seen["main"].count(OWNER_TEXT) == 1
    assert len([e for e in events if isinstance(e, SteerDelivered)]) == 1


async def test_delegated_deny_after_a_stop_carries_nothing() -> None:
    async def on_prompt(loop, event):
        await loop.submit(Steer(text=OWNER_TEXT, id="q1"))
        await loop.submit(Interrupt())
        await loop.submit(RespondPermission(
            request_id=event.request_id, decision="deny", message=PLACEHOLDER,
        ))

    _loop, frames, events, _seen = await _run_peer_boundary_turn(on_prompt)

    assert OWNER_TEXT not in _permission_reply(frames)["message"]
    assert not any(isinstance(e, SteerDelivered) for e in events)


# ─── DelegatedLoop: a PreToolUse deny (Bypass asks the owner there) ──────────


def _armed_loop(pending: list) -> tuple:
    loop = _make_loop("w")
    loop._pending_peers = list(pending)
    loop._turn_peer_ids = {op.id for op in pending if op.id}
    loop._peer_admission_open = True
    loop._active_turn_id = "turn_1"
    mapper = SimpleNamespace(turn_id="turn_1", main_tool_use_ids={"toolu_main"})
    return loop, mapper


def _pre_tool_use(tool_use_id: str, **extra) -> HookCallbackRequest:
    return HookCallbackRequest(
        cli_request_id="cli_hook_1", callback_id="fluid_hook_PreToolUse",
        input={"tool_name": "Bash", "tool_use_id": tool_use_id, **extra}, tool_use_id=tool_use_id,
    )


def _deny(reason: str = PLACEHOLDER) -> dict:
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}


async def test_pre_tool_use_deny_carries_the_waiting_steer_and_peer_in_order() -> None:
    from fluid.context.peer import PEER_OPEN_PREFIX

    steer = Steer(text=OWNER_TEXT, id="q1")
    peer = AgentMessage(sender="reviewer", text="peer note", id="p1", wake=False)
    loop, mapper = _armed_loop([steer, peer])
    output = _deny()

    carried = loop._pre_tool_use_deny_boundary(_pre_tool_use("toolu_main"), output, mapper)

    reason = output["hookSpecificOutput"]["permissionDecisionReason"]
    assert carried == [steer, peer] and loop._pending_peers == []
    assert reason.startswith(PLACEHOLDER)
    assert reason.index(_marked(OWNER_TEXT)) < reason.index(f"{PEER_OPEN_PREFIX} from='reviewer'")


async def test_pre_tool_use_carries_nothing_off_the_main_thread_or_on_an_allow() -> None:
    steer = Steer(text=OWNER_TEXT, id="q1")
    loop, mapper = _armed_loop([steer])

    # A subagent's call, by id or by agent_id.
    assert loop._pre_tool_use_deny_boundary(_pre_tool_use("toolu_sub"), _deny(), mapper) == []
    assert loop._pre_tool_use_deny_boundary(
        _pre_tool_use("toolu_main", agent_id="agent_1"), _deny(), mapper) == []
    # An allow, or no opinion.
    allow = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"}}
    assert loop._pre_tool_use_deny_boundary(_pre_tool_use("toolu_main"), allow, mapper) == []
    assert loop._pre_tool_use_deny_boundary(_pre_tool_use("toolu_main"), {}, mapper) == []
    # A deny that ends the query.
    stopping = {**_deny(), "continue": False}
    assert loop._pre_tool_use_deny_boundary(_pre_tool_use("toolu_main"), stopping, mapper) == []
    # No reader (an answer given outside the running turn).
    assert loop._pre_tool_use_deny_boundary(_pre_tool_use("toolu_main"), _deny(), None) == []
    assert loop._pending_peers == [steer]
