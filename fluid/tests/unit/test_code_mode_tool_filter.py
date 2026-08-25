"""Ruling T9 — code mode describes and accepts only the session's tool surface.

``NativeLoop`` already withholds the schemas of tools outside a session's
surface, but code mode has its own vocabulary: a discovery catalog, generated
stubs mounted in the kernel, and a host-call gate. These tests pin that a
filtered tool is missing from every one of them, and that a hand-written
``__host_call__`` naming it fails loudly instead of quietly doing nothing.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from fluid.codemode.bridge import HostBridge
from fluid.codemode.projection import Projection
from fluid.codemode.session import CodeModeSession, register_code_mode_tools
from fluid.engine.native import NativeLoop
from fluid.permissions.engine import PermissionEngine
from fluid.providers.base import ProviderCaps, ProviderRequest, StreamChunk
from fluid.store.base import MemoryStore
from fluid.tools.registry import ToolCtx, ToolOutcome, ToolRegistry, ToolSpec


def _no_bash(name: str) -> bool:
    return name != "Bash"


def _registry() -> ToolRegistry:
    async def _ran(_input: dict, _ctx: ToolCtx) -> ToolOutcome:
        return ToolOutcome(text="ran")

    registry = ToolRegistry()
    for name, description in (
        ("Bash", "Run a shell command."),
        ("Read", "Read a file from disk."),
    ):
        registry.register_builtin(
            ToolSpec(
                name=name, description=description,
                input_schema={"type": "object", "properties": {}},
                source="builtin",
            ),
            _ran,
        )
    return registry


def _filtered_session() -> CodeModeSession:
    registry = _registry()
    session = CodeModeSession(registry)
    register_code_mode_tools(registry, session, tool_filter=_no_bash)
    return session


class _IdleProvider:
    name = "anthropic"
    caps = ProviderCaps()

    async def stream(self, request: ProviderRequest) -> AsyncIterator[StreamChunk]:
        yield StreamChunk("message_stop", {})


# ── discovery: list / search / docs ───────────────────────────────────────


def test_register_stamps_the_filter_onto_the_session() -> None:
    session = _filtered_session()
    assert session.tool_filter is _no_bash


def test_list_and_search_skip_a_filtered_tool() -> None:
    session = _filtered_session()

    listing = session.render_list()
    assert "Read" in listing and "Bash" not in listing

    assert "Bash" not in session.render_search("shell command")
    assert "Read" in session.render_search("read a file")


def test_docs_for_a_filtered_tool_read_as_an_unknown_name() -> None:
    """The model gets no signal that the tool exists but was withheld."""
    session = _filtered_session()

    filtered = session.render_docs("Bash")
    unknown = session.render_docs("Ordnance")
    assert filtered.startswith("No such tool:")
    assert filtered == unknown.replace("Ordnance", "Bash")
    assert "def Read(" in session.render_docs("Read")


def test_projection_excludes_a_filtered_tool() -> None:
    """No stub, no catalog entry, no docs — so a host rendering per-tool
    documentation sections from the projection cannot leak one."""
    specs = _registry().list_tools(loaded_only=False)
    projection = Projection(specs, tool_filter=_no_bash)

    assert projection.leaves() == ["Read"]
    assert "Bash" not in projection.sources()
    assert projection.docs("Bash") is None
    assert projection.search("shell") == []
    assert Projection(specs).leaves() == ["Bash", "Read"]  # unfiltered control


# ── the host-call gate ────────────────────────────────────────────────────


async def test_host_bridge_refuses_a_filtered_tool() -> None:
    registry = _registry()
    bridge = HostBridge(
        registry, PermissionEngine("never_ask"), tool_filter=_no_bash
    )

    refused = await bridge.on_host_call("Bash", {"command": "ls"})
    assert refused.result is None
    assert refused.error and "Bash" in refused.error
    assert "not available in this session" in refused.error

    allowed = await bridge.on_host_call("Read", {})
    assert allowed.result == "ran"


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


async def test_run_python_call_to_a_filtered_tool_errors_in_the_cell() -> None:
    """End to end through the real kernel: the refusal raises in the model's
    own code, so it lands in context instead of vanishing."""
    session = _filtered_session()
    try:
        blocked = await session.run_python(
            sid="s1", turn_id="t1", loop=_FakeLoop(),
            input={"code": "__host_call__('Bash', {'command': 'ls'})"},
        )
        assert blocked.is_error
        assert "not available in this session" in blocked.text

        allowed = await session.run_python(
            sid="s1", turn_id="t1", loop=_FakeLoop(),
            input={"code": "__host_call__('Read', {})"},
        )
        assert not allowed.is_error and "ran" in allowed.text
    finally:
        await session.aclose()


# ── the engine wires its own surface in ───────────────────────────────────


def test_native_loop_hands_code_mode_its_tool_surface() -> None:
    registry = _registry()
    session = CodeModeSession(registry)
    NativeLoop(
        provider=_IdleProvider(), store=MemoryStore(), model="claude-sonnet-5",
        registry=registry, codemode=session, disallowed_tools=["Bash"],
    )

    assert session.tool_filter is not None
    assert session.tool_filter("Read") is True
    assert session.tool_filter("Bash") is False
    assert "Bash" not in session.render_list()
    assert session.render_docs("Bash").startswith("No such tool:")


def test_code_mode_hides_a_tool_whose_capability_is_not_wired() -> None:
    """``requires_extra`` gates code mode too, not just the JSON schemas.

    The request surface has always dropped a tool whose host injection is
    missing, but the projection did not — so a cell could import SessionSearch
    and get "unavailable: no session search backend injected" back as a tool
    error. Absent capability has to be invisible on both surfaces.
    """
    registry = _registry()

    async def _ran(_input: dict, _ctx: ToolCtx) -> ToolOutcome:
        return ToolOutcome(text="ran")

    registry.register_builtin(
        ToolSpec(
            name="SessionSearch",
            description="Search stored sessions.",
            input_schema={"type": "object", "properties": {}},
            source="builtin",
            requires_extra=("session_search",),
        ),
        _ran,
    )
    session = CodeModeSession(registry)
    NativeLoop(
        provider=_IdleProvider(), store=MemoryStore(), model="claude-sonnet-5",
        registry=registry, codemode=session,
    )

    assert session.tool_filter is not None
    assert session.tool_filter("SessionSearch") is False
    assert "SessionSearch" not in session.render_list()
    assert session.render_docs("SessionSearch").startswith("No such tool:")
    # Wire the capability and the same tool becomes visible again.
    assert session.tool_filter("Read") is True


def test_code_mode_shows_a_tool_once_its_capability_is_wired() -> None:
    registry = _registry()

    async def _ran(_input: dict, _ctx: ToolCtx) -> ToolOutcome:
        return ToolOutcome(text="ran")

    registry.register_builtin(
        ToolSpec(
            name="SessionSearch",
            description="Search stored sessions.",
            input_schema={"type": "object", "properties": {}},
            source="builtin",
            requires_extra=("session_search",),
        ),
        _ran,
    )
    session = CodeModeSession(registry)
    ctx = ToolCtx()
    ctx.extra["session_search"] = lambda *a, **k: []
    NativeLoop(
        provider=_IdleProvider(), store=MemoryStore(), model="claude-sonnet-5",
        registry=registry, codemode=session, ctx=ctx,
    )

    assert session.tool_filter is not None
    assert session.tool_filter("SessionSearch") is True
    assert "SessionSearch" in session.render_list()
