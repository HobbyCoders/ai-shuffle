"""MRTR completion — the engine's QuestionRequest/RespondQuestion round-trip.

An MCP ``tools/call`` returning ``input_required`` (spec 2026-07-28) must not
end the tool call: the engine emits one QuestionRequest per server request,
collects RespondQuestion answers, and retries the same call with
``inputResponses`` + the echoed ``request_state``. Bounded at 3 rounds.

The MCP layer is faked at the McpManager seam (the engine talks to it through
``registry.dispatch``), so the whole loop runs offline in the test process.
"""

import asyncio
from typing import Any, AsyncIterator

import pytest

from omni.engine.native import NativeLoop
from omni.events import QuestionRequest, RespondQuestion, ToolResult, TurnComplete, UserTurn
from omni.permissions.engine import PermissionEngine
from omni.providers.base import ProviderCaps, ProviderRequest, StreamChunk
from omni.store.base import MemoryStore
from omni.tools.registry import InputRequired, ToolOutcome, ToolRegistry, ToolSpec, _Entry


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


def _tool_call(tuid: str, name: str, args_json: str) -> list[StreamChunk]:
    return [
        StreamChunk("message_start", {"usage": {"input_tokens": 10}}),
        StreamChunk("block_start",
                    {"index": 0, "block_type": "tool_use", "tool_name": name, "tool_use_id": tuid}),
        StreamChunk("tool_args_delta", {"index": 0, "partial_json": args_json}),
        StreamChunk("block_stop", {"index": 0}),
        StreamChunk("usage", {"usage": {"output_tokens": 5}, "stop_reason": "tool_use"}),
        StreamChunk("message_stop", {}),
    ]


def _text(text: str) -> list[StreamChunk]:
    return [
        StreamChunk("message_start", {"usage": {"input_tokens": 10}}),
        StreamChunk("block_start", {"index": 0, "block_type": "text"}),
        StreamChunk("text_delta", {"index": 0, "text": text}),
        StreamChunk("block_stop", {"index": 0}),
        StreamChunk("usage", {"usage": {"output_tokens": 5}, "stop_reason": "end_turn"}),
        StreamChunk("message_stop", {}),
    ]


class PausingManager:
    """McpManager stand-in: first call pauses on input_required; the retry must
    echo request_state and carry the collected answers."""

    def __init__(self, *, pause_forever: bool = False) -> None:
        self.pause_forever = pause_forever
        self.dispatches: list[dict[str, Any]] = []

    async def dispatch(self, server: str, tool: str, input: dict[str, Any], *,
                       input_responses: Any = None, request_state: str | None = None):
        self.dispatches.append({
            "server": server, "tool": tool, "input": input,
            "input_responses": input_responses, "request_state": request_state,
        })
        if input_responses is None or self.pause_forever:
            return ToolOutcome(
                text="[input_required]",
                input_required=InputRequired(
                    server=server, tool=tool, request_state="state-1",
                    requests={"req-7": {"method": "elicitation/create",
                                        "message": "Which region?",
                                        "schema": {"type": "string"}}},
                ),
            )
        return ToolOutcome(text=f"done with {input_responses['req-7']}")

    async def aclose(self) -> None:
        pass


def _registry_with_mcp_tool(manager: PausingManager) -> ToolRegistry:
    registry = ToolRegistry(manager=manager)  # type: ignore[arg-type]
    spec = ToolSpec(name="mcp__srv__lookup", description="paused lookup",
                    input_schema={"type": "object"}, source="mcp")
    registry._entries["mcp__srv__lookup"] = _Entry(spec=spec, server="srv")
    registry._loaded.add("mcp__srv__lookup")
    return registry


async def _loop(manager: PausingManager, provider: ScriptedProvider) -> tuple[NativeLoop, str]:
    store = MemoryStore()
    sid = (await store.create_session()).session_id
    return NativeLoop(
        provider=provider, store=store, model="claude-sonnet-5", session_id=sid,
        registry=_registry_with_mcp_tool(manager),
        permissions=PermissionEngine("never_ask"),
    ), sid


async def test_input_required_round_trip_retries_with_answers() -> None:
    manager = PausingManager()
    provider = ScriptedProvider([
        _tool_call("t1", "mcp__srv__lookup", '{"q": "weather"}'),
        _text("All done."),
    ])
    loop, sid = await _loop(manager, provider)
    await loop.submit(UserTurn(content="look it up"))

    # Answer the QuestionRequest when it appears on the bus.
    question: QuestionRequest | None = None
    async for e in loop.bus.subscribe(sid):
        if isinstance(e, QuestionRequest):
            question = e
            await loop.submit(RespondQuestion(request_id=e.request_id, answer="eu-west"))
        if isinstance(e, TurnComplete):
            break
    await loop.wait_turn()

    assert question is not None
    assert question.message == "Which region?"
    assert question.tool_use_id == "t1" and question.name == "mcp__srv__lookup"
    assert question.schema == {"type": "string"}

    # Retry leg: same tool, answers keyed by the SERVER's request id, state echoed.
    assert len(manager.dispatches) == 2
    retry = manager.dispatches[1]
    assert retry["input_responses"] == {"req-7": "eu-west"}
    assert retry["request_state"] == "state-1"
    assert retry["input"] == {"q": "weather"}

    results = [e for e in loop.bus.log(sid) if isinstance(e, ToolResult)]
    assert len(results) == 1
    assert results[0].output == "done with eu-west" and not results[0].is_error


async def test_input_required_is_bounded() -> None:
    """A server that pauses forever gets an in-context error after 3 rounds,
    never a wedged turn."""
    manager = PausingManager(pause_forever=True)
    provider = ScriptedProvider([
        _tool_call("t1", "mcp__srv__lookup", "{}"),
        _text("gave up"),
    ])
    loop, sid = await _loop(manager, provider)
    await loop.submit(UserTurn(content="go"))

    answered = 0
    async for e in loop.bus.subscribe(sid):
        if isinstance(e, QuestionRequest):
            answered += 1
            await loop.submit(RespondQuestion(request_id=e.request_id, answer="x"))
        if isinstance(e, TurnComplete):
            break
    await loop.wait_turn()

    assert answered == 3  # one question per bounded round
    assert len(manager.dispatches) == 4  # initial + 3 retries
    results = [e for e in loop.bus.log(sid) if isinstance(e, ToolResult)]
    assert len(results) == 1
    assert results[0].is_error and "still input_required" in results[0].output


async def test_interrupt_while_parked_on_question() -> None:
    manager = PausingManager()
    provider = ScriptedProvider([_tool_call("t1", "mcp__srv__lookup", "{}")])
    loop, sid = await _loop(manager, provider)
    await loop.submit(UserTurn(content="go"))

    async for e in loop.bus.subscribe(sid):
        if isinstance(e, QuestionRequest):
            # Interrupt instead of answering — the tool call must fail closed.
            from omni.events import Interrupt
            await loop.submit(Interrupt())
            break
    await loop.wait_turn()

    results = [e for e in loop.bus.log(sid) if isinstance(e, ToolResult)]
    assert len(results) == 1
    assert results[0].is_error and "interrupted" in results[0].output
