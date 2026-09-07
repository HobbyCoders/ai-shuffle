"""Code mode must be able to ask the user something.

The shipped bug: ``AskUser`` called from inside
``run_python`` returned ``''``. Their first leg is an EMPTY ToolOutcome
carrying an ``input_required`` marker, and code mode's host-call gate dispatched
once and threw the marker away — no QuestionRequest, no modal, no second leg.
Models read ``=> ''`` as "the tool ran and said nothing", invented the user's
answers and left the tool waiting for input
nobody had been asked for. Plan mode never turned off.

These tests drive the real subprocess kernel through a real ``NativeLoop``, so
they pin the whole round-trip: in-kernel call → QuestionRequest on the bus →
RespondQuestion → retry leg → the answer text back inside the cell.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

from fluid.codemode.bridge import HostBridge
from fluid.codemode.session import CodeModeSession
from fluid.engine.native import NativeLoop
from fluid.events import (
    QuestionRequest,
    RespondQuestion,
    ToolResult,
    TurnComplete,
    UserTurn,
)
from fluid.permissions.engine import PermissionEngine
from fluid.providers.base import ProviderCaps, ProviderRequest, StreamChunk
from fluid.store.base import MemoryStore
from fluid.tools.builtins import ask_user
from fluid.tools.mcp.bundled_server import _answer
from fluid.tools.registry import (
    InputRequired,
    ToolCtx,
    ToolOutcome,
    ToolRegistry,
    ToolSpec,
)


# ── scripted provider ─────────────────────────────────────────────────────


class ScriptedProvider:
    name = "anthropic"
    caps = ProviderCaps(explicit_cache_breakpoints=True, max_cache_breakpoints=4)

    def __init__(self, scripts: list[list[StreamChunk]]) -> None:
        self.scripts = scripts
        self.calls = 0

    async def stream(self, request: ProviderRequest) -> AsyncIterator[StreamChunk]:
        script = self.scripts[min(self.calls, len(self.scripts) - 1)]
        self.calls += 1
        for chunk in script:
            yield chunk


def _text(text: str) -> list[StreamChunk]:
    return [
        StreamChunk("message_start", {"usage": {"input_tokens": 10}}),
        StreamChunk("block_start", {"index": 0, "block_type": "text"}),
        StreamChunk("text_delta", {"index": 0, "text": text}),
        StreamChunk("block_stop", {"index": 0}),
        StreamChunk("usage", {"usage": {"output_tokens": 5}, "stop_reason": "end_turn"}),
        StreamChunk("message_stop", {}),
    ]


def _run_python(code: str, tuid: str = "t1") -> list[StreamChunk]:
    return [
        StreamChunk("message_start", {"usage": {"input_tokens": 10}}),
        StreamChunk("block_start", {
            "index": 0, "block_type": "tool_use",
            "tool_name": "run_python", "tool_use_id": tuid,
        }),
        StreamChunk("tool_args_delta", {"index": 0, "partial_json": json.dumps({"code": code})}),
        StreamChunk("block_stop", {"index": 0}),
        StreamChunk("usage", {"usage": {"output_tokens": 5}, "stop_reason": "tool_use"}),
        StreamChunk("message_stop", {}),
    ]


async def _drive(loop: NativeLoop, sid: str, answer: str) -> list[QuestionRequest]:
    """Run the turn, answering every QuestionRequest with ``answer``."""
    asked: list[QuestionRequest] = []
    await loop.submit(UserTurn(content="go"))
    async for e in loop.bus.subscribe(sid):
        if isinstance(e, QuestionRequest):
            asked.append(e)
            await loop.submit(RespondQuestion(request_id=e.request_id, answer=answer))
        if isinstance(e, TurnComplete):
            break
    await loop.wait_turn()
    return asked


# ── AskUser, from inside the kernel ───────────────────────────────────────


async def test_in_kernel_ask_user_completes_the_round_trip() -> None:
    registry = ToolRegistry()
    registry.register_builtin(ask_user.SPEC, ask_user.handler)
    session = CodeModeSession(registry)
    code = (
        "from tools import AskUser\n"
        "print(AskUser(questions=[{'question': 'Which region?', "
        "'header': 'Region', 'options': [{'label': 'eu-west'}, "
        "{'label': 'us-east'}]}]))\n"
    )
    store = MemoryStore()
    sid = (await store.create_session()).session_id
    loop = NativeLoop(
        provider=ScriptedProvider([_run_python(code), _text("done")]),
        store=store, model="claude-sonnet-5", session_id=sid,
        registry=registry, codemode=session,
        permissions=PermissionEngine("never_ask"),
    )
    try:
        asked = await _drive(loop, sid, "1")

        # The question actually reached the user.
        assert len(asked) == 1
        assert "Which region?" in asked[0].message
        assert asked[0].name == "AskUser"

        results = [e for e in loop.bus.log(sid) if isinstance(e, ToolResult)]
        inner = next(e for e in results if e.name == "AskUser")
        assert inner.output != ""  # the bug: first leg's empty text
        assert "The user answered:" in inner.output
        assert "Region: eu-west" in inner.output
        assert not inner.is_error

        # And the kernel — the model's own code — saw the same text.
        cell = next(e for e in results if e.name == "run_python")
        assert "The user answered:" in cell.output
        assert "Region: eu-west" in cell.output
        assert not cell.is_error
    finally:
        await loop.aclose()


# ── HostBridge without an engine behind it ────────────────────────────────


def _ask_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register_builtin(ask_user.SPEC, ask_user.handler)
    return registry


_QUESTIONS = [{"question": "Which region?", "header": "Region"}]


async def test_standalone_bridge_refuses_instead_of_answering_nothing() -> None:
    """No round-trip wired (embedding, tests): the call must FAIL, loudly.

    Returning the first leg's empty text as a result is what taught models
    their questions had been answered with silence.
    """
    bridge = HostBridge(_ask_registry(), PermissionEngine("never_ask"))

    reply = await bridge.on_host_call("AskUser", {"questions": _QUESTIONS})

    assert reply.result is None
    assert reply.error is not None
    assert "AskUser" in reply.error
    assert "was NOT asked" in reply.error
    assert "JSON tool interface" in reply.error


async def test_bridge_with_a_round_trip_returns_the_second_leg() -> None:
    seen: list[InputRequired] = []

    async def collect(name: str, tool_use_id: str | None, marker: InputRequired):
        seen.append(marker)
        assert name == "AskUser"
        assert tool_use_id == "call_1"
        return {key: "eu-west" for key in marker.requests}

    bridge = HostBridge(
        _ask_registry(), PermissionEngine("never_ask"), on_input_required=collect
    )

    reply = await bridge.on_host_call(
        "AskUser", {"questions": _QUESTIONS}, tool_use_id="call_1"
    )

    assert len(seen) == 1
    assert reply.error is None and reply.denied is False
    assert reply.result is not None
    assert "The user answered:" in reply.result
    assert "Region: eu-west" in reply.result


async def test_bridge_round_trip_is_bounded() -> None:
    """A tool that never stops pausing gets an in-context error, not a wedge."""

    async def _always_pausing(_input: dict, _ctx: ToolCtx) -> ToolOutcome:
        return ToolOutcome(
            text="",
            input_required=InputRequired(
                server="builtin", tool="Nagging", request_state="s",
                requests={"q0": {"message": "again?"}},
            ),
        )

    registry = ToolRegistry()
    registry.register_builtin(
        ToolSpec(
            name="Nagging", description="Never satisfied.",
            input_schema={"type": "object", "properties": {}},
        ),
        _always_pausing,
    )
    rounds = 0

    async def collect(_name: str, _tuid: str | None, marker: InputRequired):
        nonlocal rounds
        rounds += 1
        return {key: "x" for key in marker.requests}

    bridge = HostBridge(
        registry, PermissionEngine("never_ask"), on_input_required=collect
    )
    reply = await bridge.on_host_call("Nagging", {})

    assert rounds == 3
    assert reply.result is None
    assert reply.error is not None and "still input_required" in reply.error


async def test_bridge_interrupted_round_trip_is_an_error_not_a_result() -> None:
    async def collect(_name: str, _tuid: str | None, _marker: InputRequired):
        return None  # interrupted while parked on the question

    bridge = HostBridge(
        _ask_registry(), PermissionEngine("never_ask"), on_input_required=collect
    )
    reply = await bridge.on_host_call("AskUser", {"questions": _QUESTIONS})

    assert reply.result is None
    assert reply.error is not None and "interrupted" in reply.error


async def test_bridge_refuses_a_re_entrant_run_python() -> None:
    """``run_python`` owns the kernel this call came out of; recursing into the
    code-mode session from inside it would drive the same kernel re-entrantly."""
    registry = ToolRegistry()
    session = CodeModeSession(registry)
    registry.register_builtin(
        ToolSpec(
            name="run_python", description="engine-driven",
            input_schema={"type": "object", "properties": {}},
            source="codemode",
        ),
        lambda _i, _c: ToolOutcome(text="should never run"),
    )
    bridge = HostBridge(registry, PermissionEngine("never_ask"))

    reply = await bridge.on_host_call("run_python", {"code": "1"})

    assert reply.result is None
    assert reply.error is not None and "already in it" in reply.error
    assert session is not None  # constructed only to mirror the real wiring


# ── the bundled MCP bridge drops the same marker ──────────────────────────


async def test_bundled_server_reports_an_unresolvable_round_trip() -> None:
    """The socket bridge to the delegated CLI has no user on the far end.

    It cannot run the round-trip — but it must say so rather than forward the
    first leg's empty text as a successful result.
    """
    reply = await _answer(
        _ask_registry(), ToolCtx(),
        {"op": "call", "name": "AskUser", "arguments": {"questions": _QUESTIONS}},
    )

    assert reply["is_error"] is True
    assert "AskUser" in reply["text"]
    assert "was NOT asked" in reply["text"]


async def test_bundled_server_still_forwards_a_normal_result() -> None:
    async def _ran(_input: dict, _ctx: ToolCtx) -> ToolOutcome:
        return ToolOutcome(text="ran")

    registry = ToolRegistry()
    registry.register_builtin(
        ToolSpec(
            name="Read", description="Read a file.",
            input_schema={"type": "object", "properties": {}},
        ),
        _ran,
    )
    reply = await _answer(
        registry, ToolCtx(), {"op": "call", "name": "Read", "arguments": {}}
    )

    assert reply == {"text": "ran", "is_error": False}
