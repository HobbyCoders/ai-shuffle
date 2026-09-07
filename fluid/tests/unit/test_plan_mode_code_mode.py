"""Plan mode has to hold on the code-mode surface, not just in the schemas.

The audited failure (session 5211b152): a code-mode session entered plan mode,
the Plan tool told the model "Write and shell tools have left your surface",
and the model then ran ``from tools.Write import Write; Write(...)`` — which
wrote the file. Plan mode's only enforcement was ``plan_mode_tools`` filtering
the REQUEST SCHEMAS, applied on the non-codemode branch of ``_tools_api``; code
mode reaches its tools through a kernel instead, so nothing filtered anything
and the tool's own result text was a lie.

Enforcement now sits on the surface code mode actually uses, twice over: the
projection (no stub to import, no catalog entry, no docs) and the host-call
gate (a hand-written ``__host_call__`` is refused by name). Both read the flag
live, because the mode ends mid-turn — StopPlanning's approval flips it during
dispatch, and the next call of the same turn may legitimately write.
"""

from __future__ import annotations

import json

from fluid.codemode.bridge import HostBridge
from fluid.codemode.session import CodeModeSession, register_code_mode_tools
from fluid.engine.native import NativeLoop
from fluid.permissions.engine import PermissionEngine
from fluid.providers.base import ProviderCaps, ProviderRequest, StreamChunk
from fluid.store.base import MemoryStore
from fluid.tools.builtins.plan import (
    ENTER_RESULT,
    PLAN_SPEC,
    STOP_SPEC,
    plan_handler,
    stop_handler,
)
from fluid.events import (
    QuestionRequest,
    RespondQuestion,
    ToolResult,
    TurnComplete,
    UserTurn,
)
from fluid.tools.registry import ToolCtx, ToolOutcome, ToolRegistry, ToolSpec

from tests.unit.test_empty_completion_retry import (
    ScriptedProvider,
    _text,
    _tool_call,
)

# What plan mode withholds, and what it must leave alone.
_WRITE_TOOLS = ("Write", "Edit", "Bash")
_READ_TOOLS = ("Read", "Grep", "Glob")

_PLAN_REFUSAL = "plan mode is active"


class _Flag:
    """A plan-mode flag the test flips, standing in for the engine's."""

    def __init__(self, active: bool = True) -> None:
        self.active = active

    def __call__(self) -> bool:
        return self.active


def _registry() -> ToolRegistry:
    async def _ran(input: dict, _ctx: ToolCtx) -> ToolOutcome:
        return ToolOutcome(text="ran")

    registry = ToolRegistry()
    for name, risk in (
        ("Write", "write"),
        ("Edit", "write"),
        ("Bash", "destructive"),
        ("Read", "safe"),
        ("Grep", "safe"),
        ("Glob", "safe"),
    ):
        registry.register_builtin(
            ToolSpec(
                name=name, description=f"{name} things.",
                input_schema={"type": "object", "properties": {}},
                risk=risk, source="builtin",
            ),
            _ran,
        )
    return registry


def _session(flag: _Flag) -> tuple[CodeModeSession, ToolRegistry]:
    registry = _registry()
    session = CodeModeSession(registry)
    register_code_mode_tools(registry, session, plan_mode=flag)
    return session, registry


# ── the projection: no stub, no catalog entry, no docs ────────────────────


def test_projection_drops_write_tools_while_planning_and_restores_them() -> None:
    flag = _Flag(active=True)
    session, _ = _session(flag)

    planning = session._projection()
    assert planning.leaves() == ["Glob", "Grep", "Read"]
    for name in _WRITE_TOOLS:
        assert name not in planning.sources()
        assert planning.docs(name) is None

    flag.active = False  # the user approved the plan
    executing = session._projection()
    assert executing.leaves() == ["Bash", "Edit", "Glob", "Grep", "Read", "Write"]
    for name in _WRITE_TOOLS:
        assert name in executing.sources()


def test_discovery_tools_describe_only_the_planning_surface() -> None:
    """A withheld tool must not be advertised either — the model cannot be
    told to use what it is not allowed to use."""
    flag = _Flag(active=True)
    session, _ = _session(flag)

    listing = session.render_list()
    assert all(name not in listing for name in _WRITE_TOOLS)
    assert all(name in listing for name in _READ_TOOLS)
    assert "Write" not in session.render_search("write a file")
    assert session.render_docs("Write").startswith("No such tool:")

    flag.active = False
    assert "Write" in session.render_list()
    assert "def Write(" in session.render_docs("Write")


# ── the host-call gate: a hand-written call is refused by name ─────────────


def _bridge(flag: _Flag, registry: ToolRegistry) -> HostBridge:
    return HostBridge(
        registry, PermissionEngine("never_ask"), plan_mode=flag
    )


async def test_host_gate_refuses_write_tools_and_allows_read_tools() -> None:
    flag = _Flag(active=True)
    _, registry = _session(flag)
    bridge = _bridge(flag, registry)

    for name in _WRITE_TOOLS:
        refused = await bridge.on_host_call(name, {})
        assert refused.result is None, name
        assert refused.denied is True, name
        assert refused.message and _PLAN_REFUSAL in refused.message, name
        # The refusal names the tool and the way out, so the model does not
        # read it as a missing tool and go hunting for another route.
        assert name in refused.message
        assert "StopPlanning" in refused.message

    for name in _READ_TOOLS:
        allowed = await bridge.on_host_call(name, {})
        assert allowed.result == "ran", name


async def test_the_gate_lifts_the_moment_plan_mode_ends_mid_turn() -> None:
    """The same bridge instance, mid-turn: StopPlanning's approval flips the
    flag during dispatch, and the next in-kernel call may write."""
    flag = _Flag(active=True)
    _, registry = _session(flag)
    bridge = _bridge(flag, registry)

    blocked = await bridge.on_host_call("Write", {})
    assert blocked.result is None

    flag.active = False  # approval lands
    allowed = await bridge.on_host_call("Write", {})
    assert allowed.result == "ran"
    assert allowed.denied is False


async def test_the_gate_covers_a_stub_bound_before_plan_mode_started() -> None:
    """Filtering the projection alone is not enough.

    A cell that imported ``Write`` before the session entered plan mode holds a
    live function object; re-mounting the VFS cannot unbind it, so the call
    still arrives at the host. The gate is what stops it.
    """
    flag = _Flag(active=False)
    _, registry = _session(flag)
    bridge = _bridge(flag, registry)

    assert (await bridge.on_host_call("Write", {})).result == "ran"
    flag.active = True
    refused = await bridge.on_host_call("Write", {})
    assert refused.denied is True and _PLAN_REFUSAL in (refused.message or "")


# ── end to end through the real kernel ────────────────────────────────────


class _FakeLoop:
    """The bits of ``NativeLoop`` that ``CodeModeSession.run_python`` touches."""

    permissions = None
    hooks = None

    def __init__(self) -> None:
        self.ctx = ToolCtx()

    async def _emit(self, _event: object) -> None:
        return None

    async def _await_permission(self, *_args: object) -> tuple[str, None, None]:
        return ("allow", None, None)


async def test_a_planning_cell_can_neither_import_nor_hand_call_write() -> None:
    flag = _Flag(active=True)
    session, _ = _session(flag)
    try:
        imported = await session.run_python(
            sid="s1", turn_id="t1", loop=_FakeLoop(),
            input={"code": "from tools import Write"},
        )
        assert imported.is_error  # the stub was never mounted

        hand_written = await session.run_python(
            sid="s1", turn_id="t1", loop=_FakeLoop(),
            input={"code": "__host_call__('Write', {'file_path': 'x'})"},
        )
        assert hand_written.is_error
        assert _PLAN_REFUSAL in hand_written.text

        reading = await session.run_python(
            sid="s1", turn_id="t1", loop=_FakeLoop(),
            input={"code": "from tools import Read\nRead()"},
        )
        assert not reading.is_error and "ran" in reading.text

        # Approval mid-turn: the next cell writes, no new session needed.
        flag.active = False
        writing = await session.run_python(
            sid="s1", turn_id="t1", loop=_FakeLoop(),
            input={"code": "from tools import Write\nWrite()"},
        )
        assert not writing.is_error and "ran" in writing.text
    finally:
        await session.aclose()


# ── the engine wires its own flag in ──────────────────────────────────────


class _IdleProvider:
    name = "anthropic"
    caps = ProviderCaps()

    async def stream(self, request: ProviderRequest):
        yield StreamChunk("message_stop", {})


def _code_mode_loop(**kwargs) -> tuple[NativeLoop, CodeModeSession]:
    registry = _registry()
    session = CodeModeSession(registry)
    loop = NativeLoop(
        provider=_IdleProvider(), store=MemoryStore(), model="claude-sonnet-5",
        registry=registry, codemode=session, **kwargs,
    )
    return loop, session


def test_native_loop_hands_code_mode_its_plan_mode_flag() -> None:
    loop, session = _code_mode_loop(plan_mode=True)

    assert session.plan_mode is not None
    assert loop._tool_available("Write") is False
    assert loop._tool_available("Read") is True
    assert "Write" not in session.render_list()

    loop._set_plan_mode(False)
    assert loop._tool_available("Write") is True
    assert "Write" in session.render_list()


def test_the_plan_tools_own_promise_is_true_on_the_code_mode_surface() -> None:
    """The regression, stated as the tool states it.

    ``ENTER_RESULT`` tells the model its write and shell tools have left the
    surface. Under code mode that used to be false in every way that mattered.
    """
    assert "have left your surface" in ENTER_RESULT
    assert "the shell" in ENTER_RESULT and "Task" in ENTER_RESULT
    loop, session = _code_mode_loop(plan_mode=True)

    listing = session.render_list()
    for name in _WRITE_TOOLS:
        assert name not in listing
        assert loop._tool_available(name) is False
        assert name not in session._projection().sources()


# ── dispatch, whichever bridge is wired ───────────────────────────────────


async def _plan_registry() -> ToolRegistry:
    registry = _registry()
    registry.register_builtin(PLAN_SPEC, plan_handler)
    registry.register_builtin(STOP_SPEC, stop_handler)
    return registry


async def test_dispatch_refuses_a_write_tool_while_plan_mode_is_on() -> None:
    """``_gated_call`` is the chokepoint every path reaches — the JSON surface,
    the code-mode bridge, and anything routed through the loop. A write call
    arriving here while planning is refused with the plan-mode text, not run."""
    store = MemoryStore()
    sid = (await store.create_session()).session_id
    provider = ScriptedProvider([
        _tool_call("t1", "Edit", "{}"),
        _text("understood"),
    ])
    loop = NativeLoop(
        provider=provider, store=store, model="m", session_id=sid,
        registry=await _plan_registry(),
        permissions=PermissionEngine("never_ask"), plan_mode=True,
    )
    await loop.submit(UserTurn(content="edit it"))
    await loop.wait_turn()

    results = {r.tool_use_id: r for r in loop.bus.log(sid) if isinstance(r, ToolResult)}
    assert results["t1"].is_error
    assert _PLAN_REFUSAL in results["t1"].output
    assert "Edit" in results["t1"].output


async def test_dispatch_lets_the_write_through_once_the_plan_is_approved() -> None:
    """Same turn, after the approval — the wall must not outlive the mode."""
    store = MemoryStore()
    sid = (await store.create_session()).session_id
    provider = ScriptedProvider([
        _tool_call("t1", "StopPlanning", json.dumps({"plan": "1. edit foo"})),
        _tool_call("t2", "Edit", "{}"),
        _text("done"),
    ])
    loop = NativeLoop(
        provider=provider, store=store, model="m", session_id=sid,
        registry=await _plan_registry(),
        permissions=PermissionEngine("never_ask"), plan_mode=True,
    )
    await loop.submit(UserTurn(content="design it"))
    async for e in loop.bus.subscribe(sid):
        if isinstance(e, QuestionRequest):
            await loop.submit(RespondQuestion(request_id=e.request_id, answer="approve"))
        if isinstance(e, TurnComplete):
            break
    await loop.wait_turn()

    results = {r.tool_use_id: r for r in loop.bus.log(sid) if isinstance(r, ToolResult)}
    assert loop.plan_mode is False
    assert not results["t2"].is_error, results["t2"].output
    assert results["t2"].output == "ran"
