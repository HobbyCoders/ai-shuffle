"""The permission-mode matrix on non-Claude (native) providers.

Mode x leg x watched/unwatched, for a call the runtime asked about:

* Ask (``default``) prompts when watched; unwatched, the native call waits
  and fluid denies it after ``permission_timeout``.
* A stored legacy ``auto`` is Ask on the native leg — for the engine AND for
  the app's approver (``effective_permission_mode``). The delegated Claude CLI
  keeps ``auto``.
* Accept Edits approves every call except the computer-use access request,
  watched or not (``mode_approves_without_prompt``).
* Bypass approves everything, watched or not.
"""

import asyncio
import dataclasses
from contextlib import ExitStack
from unittest.mock import patch

import pytest

import app.core.query_engine as qe
from app.core.permission_handler import permission_handler
from app.core.provider.harness_adapter import HarnessAdapter
from app.core.provider.types import PermissionRequestEvent, SessionOptions
from app.core.session_modes import (
    COMPUTER_USE_REQUEST_ACCESS,
    effective_permission_mode,
    mode_approves_without_prompt,
)
from fluid.events import model as lm
from fluid.events.ops import RespondPermission, SetPermissionMode
from tests.core.test_query_engine_permission_flows import (
    SID,
    _allowed,
    _callback,
    _ctx,
    _next_prompt,
    _prompt_count,
)

FSID = "ses_matrix"


@pytest.fixture(autouse=True)
def _clean_rules():
    permission_handler.clear_session_rules(SID)
    with patch("app.db.database.get_permission_rules", return_value=[]):
        yield
    permission_handler.clear_session_rules(SID)


# ─── the rule itself ──────────────────────────────────────────────────────


@pytest.mark.parametrize("mode, delegated, expected", [
    ("auto", False, "default"),
    ("auto", True, "auto"),
    ("acceptEdits", False, "acceptEdits"),
    ("bypassPermissions", False, "bypassPermissions"),
    ("default", False, "default"),
    (None, False, None),
])
def test_effective_permission_mode(mode, delegated, expected):
    assert effective_permission_mode(mode, delegated=delegated) == expected


@pytest.mark.parametrize("mode, tool, approves", [
    ("default", "Write", False),
    ("plan", "Write", False),
    (None, "Write", False),
    ("acceptEdits", "Write", True),
    ("acceptEdits", "Bash", True),
    ("acceptEdits", COMPUTER_USE_REQUEST_ACCESS, False),
    ("auto", "Bash", True),  # only ever reached on the delegated leg
    ("bypassPermissions", "Bash", True),
    ("bypassPermissions", COMPUTER_USE_REQUEST_ACCESS, True),
])
def test_mode_approves_without_prompt(mode, tool, approves):
    assert mode_approves_without_prompt(mode, tool) is approves


# ─── watched: the app's approver ──────────────────────────────────────────


async def test_legacy_auto_prompts_on_a_native_leg(tmp_path):
    """A stored ``auto`` runs as Ask on a native leg, so the approver asks."""
    cb, broadcast, bounds = await _callback(tmp_path, session_row={"permission_mode": "auto"})
    with patch.object(qe, "sync_engine", new=bounds.sync_engine):
        call = asyncio.create_task(cb("Bash", {"command": "rm x"}, _ctx(delegated=False)))
        prompt = await _next_prompt(broadcast)
        assert prompt["tool_name"] == "Bash"
        await permission_handler.respond(prompt["request_id"], SID, "deny")
        assert not _allowed(await asyncio.wait_for(call, 5))


async def test_legacy_auto_keeps_approving_on_the_claude_cli(tmp_path):
    cb, broadcast, _ = await _callback(tmp_path, session_row={"permission_mode": "auto"})
    assert _allowed(await cb("Bash", {"command": "rm x"}, _ctx(delegated=True)))
    assert _prompt_count(broadcast) == 0


async def test_accept_edits_approves_other_tools_but_asks_for_screen_control(tmp_path):
    cb, broadcast, bounds = await _callback(
        tmp_path, session_row={"permission_mode": "acceptEdits"},
    )
    assert _allowed(await cb("Write", {"file_path": "/x"}, _ctx(delegated=False)))
    assert _allowed(await cb("Bash", {"command": "ls"}, _ctx(delegated=False)))
    assert _prompt_count(broadcast) == 0
    with patch.object(qe, "sync_engine", new=bounds.sync_engine):
        call = asyncio.create_task(cb(COMPUTER_USE_REQUEST_ACCESS, {"apps": []}, _ctx()))
        prompt = await _next_prompt(broadcast)
        await permission_handler.respond(prompt["request_id"], SID, "deny")
        assert not _allowed(await asyncio.wait_for(call, 5))


# ─── the adapter ──────────────────────────────────────────────────────────


class _Engine:
    def __init__(self, events):
        self._events = [dataclasses.replace(e, seq=i + 1) for i, e in enumerate(events)]
        self.ops: list = []

    async def submit(self, op):
        self.ops.append(op)

    async def events(self, *, since_seq=0):
        for e in self._events:
            if e.seq > since_seq:
                yield e


def _request(name="Write", risk="write"):
    return [
        lm.PermissionRequest(
            session_id=FSID, request_id="perm_1", tool_use_id="tu1",
            name=name, input={"file_path": "/x"}, risk=risk,
        ),
        lm.TurnComplete(session_id=FSID, stop_reason="end_turn", num_model_calls=1),
    ]


def _native(engine, mode):
    adapter = HarnessAdapter()
    adapter._engine = engine
    adapter._connected = True
    adapter._is_delegated = False
    adapter._options = SessionOptions(model="gpt-5", permission_mode=mode)
    return adapter


async def _drain(adapter):
    out = []
    async for ev in adapter.receive_events():
        out.append(ev)
    for _ in range(20):  # let a spawned bridge task finish
        await asyncio.sleep(0)
    return out


async def test_native_set_permission_mode_turns_auto_into_ask():
    engine = _Engine([])
    adapter = _native(engine, "default")
    await adapter.set_permission_mode("auto")
    assert [op.mode for op in engine.ops if isinstance(op, SetPermissionMode)] == ["default"]
    assert adapter.permission_mode == "default"


async def test_bridge_tells_the_approver_which_leg_it_is_on():
    engine = _Engine(_request())
    adapter = _native(engine, "default")
    seen = {}

    async def can_use_tool(name, tool_input, context):
        seen["delegated"] = getattr(context, "delegated", "missing")
        return type("PermissionResultDeny", (), {"message": "no"})()

    adapter._can_use_tool = can_use_tool
    await _drain(adapter)
    assert seen["delegated"] is False


async def test_watched_native_legacy_auto_prompts_end_to_end(tmp_path):
    """The real approver behind the real bridge: stored ``auto`` asks."""
    stack = ExitStack()
    with stack:
        cb, broadcast, bounds = await _callback(
            tmp_path, session_row={"permission_mode": "auto"}, stack=stack,
        )
        stack.enter_context(patch.object(qe, "sync_engine", new=bounds.sync_engine))
        engine = _Engine(_request())
        adapter = _native(engine, "default")
        adapter._can_use_tool = cb
        drained = asyncio.create_task(_drain(adapter))
        prompt = await _next_prompt(broadcast)
        await permission_handler.respond(prompt["request_id"], SID, "deny")
        await asyncio.wait_for(drained, 5)
        for _ in range(50):
            if engine.ops:
                break
            await asyncio.sleep(0.01)
    assert engine.ops[0].decision == "deny"


# ─── unwatched: no approver on the native leg ─────────────────────────────


@pytest.mark.parametrize("mode", ["acceptEdits", "bypassPermissions"])
async def test_unwatched_native_modes_that_approve_allow_at_once(mode):
    engine = _Engine(_request())
    got = await _drain(_native(engine, mode))
    assert not any(isinstance(e, PermissionRequestEvent) for e in got)
    assert len(engine.ops) == 1
    op = engine.ops[0]
    assert isinstance(op, RespondPermission)
    assert (op.request_id, op.decision) == ("perm_1", "allow")


@pytest.mark.parametrize("mode, tool", [
    ("default", "Write"),
    (None, "Write"),
    ("acceptEdits", COMPUTER_USE_REQUEST_ACCESS),
])
async def test_unwatched_native_prompts_are_left_to_the_engine_timeout(mode, tool):
    """Ask (and screen control short of Bypass) is not answered here: the
    request surfaces and fluid denies it after ``permission_timeout``."""
    engine = _Engine(_request(name=tool))
    got = await _drain(_native(engine, mode))
    assert any(isinstance(e, PermissionRequestEvent) for e in got)
    assert engine.ops == []


async def test_unwatched_native_legacy_auto_is_ask():
    engine = _Engine(_request())
    adapter = _native(engine, "acceptEdits")
    await adapter.set_permission_mode("auto")
    engine.ops.clear()
    got = await _drain(adapter)
    assert any(isinstance(e, PermissionRequestEvent) for e in got)
    assert engine.ops == []
