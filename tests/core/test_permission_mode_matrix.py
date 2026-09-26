"""The permission-mode matrix on non-Claude (native) providers.

Mode x leg x watched/unwatched, for a call the runtime asked about:

* Ask (``default``) prompts when watched; unwatched, the native call waits
  and fluid denies it after ``permission_timeout``.
* A stored legacy ``auto`` is Ask on the native leg — for the engine AND for
  the app's approver (``effective_permission_mode``). The delegated Claude CLI
  keeps ``auto``.
* Accept Edits approves the edit-class tools (``ACCEPT_EDITS_TOOLS``: Edit,
  Write, MultiEdit, NotebookEdit) and nothing else, watched or not
  (``mode_approves_without_prompt``): Bash, ``run_python``, WebFetch and the
  computer-use access request prompt exactly as under Ask. The native engine
  keeps asking for every write-class call (the app sends fluid the policy, not
  the mode, so fluid's own ``acceptEdits`` allow rules are never installed);
  the approver allows an edit only after the user's saved rules, so a
  remembered Deny on Write still wins under Accept Edits.
* Bypass approves everything, watched or not.
"""

import asyncio
import dataclasses
from contextlib import ExitStack
from unittest.mock import patch

import pytest
from fluid.events import model as lm
from fluid.events.ops import RespondPermission, SetPermissionMode
from fluid.permissions.modes import ACCEPT_EDITS_TOOLS as FLUID_ACCEPT_EDITS_TOOLS
from fluid.tools.registry import ToolSpec

import app.core.query_engine as qe
from app.core.permission_handler import permission_handler
from app.core.provider.harness_adapter import HarnessAdapter
from app.core.provider.types import PermissionRequestEvent, SessionOptions
from app.core.session_modes import (
    ACCEPT_EDITS_TOOLS,
    COMPUTER_USE_REQUEST_ACCESS,
    effective_permission_mode,
    mode_approves_without_prompt,
)
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
    ("acceptEdits", "Edit", True),
    ("acceptEdits", "MultiEdit", True),
    ("acceptEdits", "NotebookEdit", True),
    ("acceptEdits", "Bash", False),
    ("acceptEdits", "run_python", False),
    ("acceptEdits", "WebFetch", False),
    ("acceptEdits", "save_skill", False),
    ("acceptEdits", "mcp__builtins__Monitor", False),
    ("acceptEdits", COMPUTER_USE_REQUEST_ACCESS, False),
    ("auto", "Bash", True),  # only ever reached on the delegated leg
    ("bypassPermissions", "Bash", True),
    ("bypassPermissions", "run_python", True),
    ("bypassPermissions", COMPUTER_USE_REQUEST_ACCESS, True),
])
def test_mode_approves_without_prompt(mode, tool, approves):
    assert mode_approves_without_prompt(mode, tool) is approves


def test_the_edit_class_set_is_fluids_and_names_only_file_editors():
    """One table: the app re-exports fluid's set rather than spelling it again,
    and it holds exactly Claude Code's file-edit tools."""
    assert ACCEPT_EDITS_TOOLS is FLUID_ACCEPT_EDITS_TOOLS
    assert ACCEPT_EDITS_TOOLS == {"Edit", "Write", "MultiEdit", "NotebookEdit"}
    assert not {"Bash", "run_python", "WebFetch", COMPUTER_USE_REQUEST_ACCESS} & ACCEPT_EDITS_TOOLS


# ─── the native engine: what fluid decides before the approver sees it ────


def _write_spec(name):
    return ToolSpec(name=name, description="", input_schema={}, risk="write")


def _engine_verdicts(mode):
    engine = HarnessAdapter()._build_permissions(SessionOptions(model="gpt-5", permission_mode=mode))
    return {
        name: engine.evaluate(_write_spec(name), {"file_path": "/x", "command": "rm x", "code": "1"})
        for name in ("Edit", "Write", "MultiEdit", "NotebookEdit", "Bash", "run_python", "WebFetch", COMPUTER_USE_REQUEST_ACCESS)
    }


@pytest.mark.parametrize("mode, expected", [
    ("default", "ask"),
    ("acceptEdits", "ask"),
    (None, "ask"),
    ("bypassPermissions", "allow"),
])
def test_native_engine_asks_for_every_write_short_of_bypass(mode, expected):
    """Accept Edits installs no allow rules in fluid: the approver, which sees
    the saved rules, is what lets an edit through."""
    assert set(_engine_verdicts(mode).values()) == {expected}


async def test_native_hot_swap_sends_the_policy_and_installs_no_edit_allows():
    """The real NativeLoop: a switch to Accept Edits leaves the engine asking
    for Edit (fluid's own mode rules stay out), Bypass stops it asking."""
    from fluid.engine.native import NativeLoop
    from fluid.permissions.engine import PermissionEngine
    from fluid.store.base import MemoryStore

    perms = HarnessAdapter()._build_permissions(SessionOptions(model="gpt-5", permission_mode="default"))
    assert isinstance(perms, PermissionEngine)
    loop = NativeLoop(provider=None, store=MemoryStore(), model="m", permissions=perms)
    adapter = HarnessAdapter()
    adapter._engine = loop
    adapter._connected = True
    adapter._is_delegated = False
    adapter._options = SessionOptions(model="gpt-5", permission_mode="default")

    await adapter.set_permission_mode("acceptEdits")
    assert adapter.permission_mode == "acceptEdits"
    assert perms.approval_policy == "on_write"
    assert perms.rules == []
    assert perms.evaluate(_write_spec("Edit"), {"file_path": "/x"}) == "ask"

    await adapter.set_permission_mode("bypassPermissions")
    assert perms.approval_policy == "never_ask"
    assert perms.evaluate(_write_spec("Bash"), {"command": "rm x"}) == "allow"

    await adapter.set_permission_mode("default")
    assert perms.approval_policy == "on_write"
    assert perms.evaluate(_write_spec("Edit"), {"file_path": "/x"}) == "ask"


def _saved_deny(tool, pattern=None):
    """An agent-level remembered Deny rule, as the DB returns it."""
    return [{"id": "r1", "profile_id": "default", "tool_name": tool, "tool_pattern": pattern, "decision": "deny"}]


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


async def test_accept_edits_approves_edits_and_asks_for_everything_else(tmp_path):
    cb, broadcast, bounds = await _callback(
        tmp_path, session_row={"permission_mode": "acceptEdits"},
    )
    assert _allowed(await cb("Write", {"file_path": "/x"}, _ctx(delegated=False)))
    assert _allowed(await cb("Edit", {"file_path": "/x"}, _ctx(delegated=False)))
    assert _prompt_count(broadcast) == 0
    with patch.object(qe, "sync_engine", new=bounds.sync_engine):
        asked = [
            ("Bash", {"command": "ls"}),
            ("run_python", {"code": "print(1)"}),
            ("WebFetch", {"url": "https://example.com"}),
            (COMPUTER_USE_REQUEST_ACCESS, {"apps": []}),
        ]
        for seen, (tool, tool_input) in enumerate(asked):
            call = asyncio.create_task(cb(tool, tool_input, _ctx(delegated=False)))
            prompt = await _next_prompt(broadcast, seen=seen)
            assert prompt["tool_name"] == tool
            await permission_handler.respond(prompt["request_id"], SID, "deny")
            assert not _allowed(await asyncio.wait_for(call, 5))
    assert _prompt_count(broadcast) == len(asked)


async def test_a_saved_deny_beats_accept_edits(tmp_path):
    """Remembered Deny on Write: Write is denied, Edit is still auto-allowed;
    a patterned rule denies only the files it names."""
    cb, broadcast, _ = await _callback(tmp_path, session_row={"permission_mode": "acceptEdits"})
    with patch("app.db.database.get_permission_rules", return_value=_saved_deny("Write")):
        assert not _allowed(await cb("Write", {"file_path": "/x"}, _ctx(delegated=False)))
        assert _allowed(await cb("Edit", {"file_path": "/x"}, _ctx(delegated=False)))
    with patch("app.db.database.get_permission_rules", return_value=_saved_deny("Write", "*.lock")):
        assert not _allowed(await cb("Write", {"file_path": "/x/a.lock"}, _ctx(delegated=False)))
        assert _allowed(await cb("Write", {"file_path": "/x/a.py"}, _ctx(delegated=False)))
    assert _prompt_count(broadcast) == 0


async def test_a_session_deny_beats_accept_edits(tmp_path):
    cb, broadcast, _ = await _callback(tmp_path, session_row={"permission_mode": "acceptEdits"})
    permission_handler._add_session_rule_idempotent(SID, "Edit", None, "deny")
    assert not _allowed(await cb("Edit", {"file_path": "/x"}, _ctx(delegated=False)))
    assert _allowed(await cb("Write", {"file_path": "/x"}, _ctx(delegated=False)))
    assert _prompt_count(broadcast) == 0


async def test_a_saved_deny_does_not_touch_bypass(tmp_path):
    cb, broadcast, _ = await _callback(tmp_path, session_row={"permission_mode": "bypassPermissions"})
    with patch("app.db.database.get_permission_rules", return_value=_saved_deny("Write")):
        assert _allowed(await cb("Write", {"file_path": "/x"}, _ctx(delegated=False)))
    assert _prompt_count(broadcast) == 0


async def test_accept_edits_asks_on_the_claude_cli_leg_too(tmp_path):
    """The CLI approves file edits itself under acceptEdits and asks the app
    about the rest; the app answers those as Ask does, so the mode means the
    same thing on both legs."""
    cb, broadcast, bounds = await _callback(
        tmp_path, session_row={"permission_mode": "acceptEdits"},
    )
    with patch.object(qe, "sync_engine", new=bounds.sync_engine):
        call = asyncio.create_task(cb("Bash", {"command": "ls"}, _ctx(delegated=True)))
        prompt = await _next_prompt(broadcast)
        assert prompt["tool_name"] == "Bash"
        await permission_handler.respond(prompt["request_id"], SID, "allow")
        assert _allowed(await asyncio.wait_for(call, 5))


async def test_bypass_still_approves_everything(tmp_path):
    cb, broadcast, _ = await _callback(
        tmp_path, session_row={"permission_mode": "bypassPermissions"},
    )
    for tool, tool_input in (
        ("Bash", {"command": "ls"}),
        ("run_python", {"code": "print(1)"}),
        (COMPUTER_USE_REQUEST_ACCESS, {"apps": []}),
    ):
        assert _allowed(await cb(tool, tool_input, _ctx(delegated=False)))
    assert _prompt_count(broadcast) == 0


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
    """The native engine is sent the POLICY (Ask is ``on_write``); the adapter
    records the mode."""
    engine = _Engine([])
    adapter = _native(engine, "default")
    await adapter.set_permission_mode("auto")
    assert [op.mode for op in engine.ops if isinstance(op, SetPermissionMode)] == ["on_write"]
    assert adapter.permission_mode == "default"


@pytest.mark.parametrize("mode, sent", [
    ("acceptEdits", "on_write"),
    ("default", "on_write"),
    ("bypassPermissions", "never_ask"),
])
async def test_native_set_permission_mode_never_sends_a_mode_name(mode, sent):
    engine = _Engine([])
    adapter = _native(engine, "default")
    await adapter.set_permission_mode(mode)
    assert [op.mode for op in engine.ops if isinstance(op, SetPermissionMode)] == [sent]
    assert adapter.permission_mode == mode


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


@pytest.mark.parametrize("mode, tool", [
    ("acceptEdits", "Write"),
    ("acceptEdits", "Edit"),
    ("bypassPermissions", "Write"),
    ("bypassPermissions", "Bash"),
    ("bypassPermissions", "run_python"),
])
async def test_unwatched_native_modes_that_approve_allow_at_once(mode, tool):
    engine = _Engine(_request(name=tool))
    got = await _drain(_native(engine, mode))
    assert not any(isinstance(e, PermissionRequestEvent) for e in got)
    assert len(engine.ops) == 1
    op = engine.ops[0]
    assert isinstance(op, RespondPermission)
    assert (op.request_id, op.decision) == ("perm_1", "allow")


@pytest.mark.parametrize("mode, tool", [
    ("default", "Write"),
    (None, "Write"),
    ("acceptEdits", "Bash"),
    ("acceptEdits", "run_python"),
    ("acceptEdits", "WebFetch"),
    ("acceptEdits", COMPUTER_USE_REQUEST_ACCESS),
])
async def test_unwatched_native_prompts_are_left_to_the_engine_timeout(mode, tool):
    """Ask (and, under Accept Edits, everything that is not a file edit —
    screen control included) is not answered here: the request surfaces and
    fluid denies it after ``permission_timeout``."""
    engine = _Engine(_request(name=tool))
    got = await _drain(_native(engine, mode))
    assert any(isinstance(e, PermissionRequestEvent) for e in got)
    assert engine.ops == []


async def test_unwatched_native_accept_edits_honours_a_saved_deny():
    """No approver: a remembered Deny on Write denies the call; Edit is allowed."""
    engine = _Engine([*_request(name="Write")[:1], *_request(name="Edit")])
    engine._events = [dataclasses.replace(e, seq=i + 1) for i, e in enumerate(engine._events)]
    engine._events[1] = dataclasses.replace(engine._events[1], request_id="perm_2", tool_use_id="tu2")
    adapter = _native(engine, "acceptEdits")
    adapter._options.extra = {"profile_id": "default", "app_session_id": SID}
    with patch("app.db.database.get_permission_rules", return_value=_saved_deny("Write")):
        got = await _drain(adapter)
    assert not any(isinstance(e, PermissionRequestEvent) for e in got)
    decisions = {op.request_id: op.decision for op in engine.ops if isinstance(op, RespondPermission)}
    assert decisions == {"perm_1": "deny", "perm_2": "allow"}


async def test_unwatched_native_bypass_ignores_a_saved_deny():
    engine = _Engine(_request(name="Write"))
    adapter = _native(engine, "bypassPermissions")
    adapter._options.extra = {"profile_id": "default", "app_session_id": SID}
    with patch("app.db.database.get_permission_rules", return_value=_saved_deny("Write")):
        await _drain(adapter)
    assert [op.decision for op in engine.ops if isinstance(op, RespondPermission)] == ["allow"]


async def test_unwatched_native_legacy_auto_is_ask():
    engine = _Engine(_request())
    adapter = _native(engine, "acceptEdits")
    await adapter.set_permission_mode("auto")
    engine.ops.clear()
    got = await _drain(adapter)
    assert any(isinstance(e, PermissionRequestEvent) for e in got)
    assert engine.ops == []
