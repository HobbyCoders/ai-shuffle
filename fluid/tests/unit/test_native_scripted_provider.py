"""End-to-end tests of the native leg against a scripted fake provider.

A real loopback HTTP server (``fixtures/fake_provider_server.py``) plays the
exact SSE frames each test writes, in the three wire formats the native engine
speaks: Anthropic Messages, OpenAI chat/completions (OpenRouter, local and
custom OpenAI-dialect rows) and OpenAI Responses. The real provider clients
(httpx, SSE parsing, retry, watchdogs, normalizers) and the real ``NativeLoop``
run against it, and each test asserts on both sides: the fluid events and
store nodes the loop produced, and what the engine sent the provider next.
Hermetic: loopback only.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx
import pytest

import fluid.engine.native as native_mod
from fluid.engine.native import NativeLoop
from fluid.events import (
    ErrorEvent,
    PermissionRequest,
    RespondPermission,
    ToolResult,
    ToolUse,
    TurnComplete,
    Usage,
    UserTurn,
)
from fluid.events.model import CacheMissAlert, Interrupted, ProviderRetry, TurnStarted
from fluid.events.ops import Interrupt
from fluid.messages import Message, Text, ToolResultBlock, ToolUseBlock
from fluid.permissions.engine import PermissionEngine
from fluid.providers import _chat_completions
from fluid.providers._retry import RetryPolicy
from fluid.providers.anthropic.client import AnthropicClient
from fluid.providers.base import ProviderRequest
from fluid.providers.local.client import LocalClient
from fluid.providers.openai_responses.client import OpenAIResponsesClient
from fluid.providers.openrouter.client import OpenRouterClient
from fluid.providers.pricing import ModelPrice
from fluid.store.base import MemoryStore
from fluid.tools.registry import ToolCtx, ToolOutcome, ToolRegistry, ToolSpec


def _load_fake_server():
    name = "fake_provider_server"
    if name in sys.modules:
        return sys.modules[name]
    path = Path(__file__).parent.parent / "fixtures" / "fake_provider_server.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


fps = _load_fake_server()
Chat, Messages, Responses = fps.Chat, fps.Messages, fps.Responses
CUT, Reply, Sleep = fps.CUT, fps.Reply, fps.Sleep

TURN_BUDGET = 20.0
FAST_RETRY = RetryPolicy(max_retries=2, base_delay=0.01, max_delay=0.05, jitter=0)
_TERMINALS = (TurnComplete, Interrupted, ErrorEvent)
MODELS = {
    "anthropic": "claude-sonnet-4-5",
    "openrouter": "vendor/fake-model",
    "local": "local-model",
    "responses": "gpt-5",
}
DIALECTS = ("anthropic", "openrouter", "responses")


def _client(kind: str, url: str, **kwargs: Any) -> Any:
    kwargs.setdefault("retry", FAST_RETRY)
    if kind == "anthropic":
        return AnthropicClient(api_key="k", base_url=url + "/v1/messages", **kwargs)
    if kind == "openrouter":
        return OpenRouterClient(api_key="k", base_url=url + "/v1/chat/completions", **kwargs)
    if kind == "local":
        return LocalClient(base_url=url, **kwargs)
    return OpenAIResponsesClient(api_key="k", base_url=url + "/v1/responses", **kwargs)


def text_reply(kind: str, text: str, **usage: Any) -> list:
    return {"anthropic": Messages, "responses": Responses}.get(kind, Chat).text(text, **usage)


def tool_reply(kind: str, calls: list[dict], **kwargs: Any) -> list:
    return {"anthropic": Messages, "responses": Responses}.get(kind, Chat).tool_calls(
        calls, **kwargs
    )


Responder = Callable[[NativeLoop, PermissionRequest], Awaitable[None]]


class Harness:
    """A NativeLoop on one fake provider, with a two-tool registry.

    ``Echo`` is safe and echoes its input; ``Write`` is write-risk (asks under
    ``on_write``) and records that it ran. ``ran`` lists every executed call.
    """

    def __init__(self, server: Any, kind: str = "anthropic", *, policy: str = "never_ask",
                 client_kwargs: dict | None = None, **loop_kwargs: Any) -> None:
        self.server = server
        self.ran: list[tuple[str, dict]] = []
        registry = ToolRegistry()

        async def echo(inp: dict, _ctx: ToolCtx) -> ToolOutcome:
            self.ran.append(("Echo", inp))
            return ToolOutcome(text="echo:" + json.dumps(inp, sort_keys=True))

        async def write(inp: dict, _ctx: ToolCtx) -> ToolOutcome:
            self.ran.append(("Write", inp))
            return ToolOutcome(text="wrote " + str(inp.get("file_path")))

        schema = {"type": "object", "properties": {"text": {"type": "string"}}}
        registry.register_builtin(ToolSpec(name="Echo", description="Echo.",
                                           input_schema=schema), echo)
        registry.register_builtin(ToolSpec(
            name="Write", description="Write.", risk="write",
            input_schema={"type": "object", "properties": {
                "file_path": {"type": "string"}, "content": {"type": "string"}}},
        ), write)
        self.store = MemoryStore()
        self.loop = NativeLoop(
            provider=_client(kind, server.url, **(client_kwargs or {})), store=self.store,
            model=MODELS[kind], registry=registry, permissions=PermissionEngine(policy),
            **loop_kwargs,
        )

    async def turn(self, prompt: str = "go", *, responder: Responder | None = None,
                   during: Callable[[NativeLoop], Awaitable[None]] | None = None) -> list:
        async def answer() -> None:
            seen: set[str] = set()
            while True:
                for event in self.loop.bus.log(self.loop.session_id):
                    if isinstance(event, PermissionRequest) and event.request_id not in seen:
                        seen.add(event.request_id)
                        assert responder is not None
                        await responder(self.loop, event)
                await asyncio.sleep(0.01)

        before = len(self.loop.bus.log(self.loop.session_id)) if self.loop._session else 0
        await self.loop.submit(UserTurn(content=prompt))
        helpers = [asyncio.create_task(answer())] if responder else []
        if during is not None:
            helpers.append(asyncio.create_task(during(self.loop)))
        try:
            await asyncio.wait_for(self.loop.wait_turn(), TURN_BUDGET)
        finally:
            for task in helpers:
                task.cancel()
            await asyncio.gather(*helpers, return_exceptions=True)
        return self.loop.bus.log(self.loop.session_id)[before:]

    async def stored(self) -> list:
        return [n.message for n in await self.store.load_path(self.loop.session_id)]

    async def aclose(self) -> None:
        await self.loop.aclose()


@pytest.fixture
def server():
    srv = fps.FakeProviderServer()
    try:
        yield srv
    finally:
        srv.close()


@pytest.fixture(autouse=True)
def _no_empty_retry_backoff(monkeypatch):
    monkeypatch.setattr(native_mod, "_empty_retry_delay", lambda attempt: 0.0)


def _of(events: list, cls: type) -> list:
    return [e for e in events if isinstance(e, cls)]


def _terminal(events: list) -> Any:
    [terminal] = _of(events, _TERMINALS)
    return terminal


def _roles(messages: list) -> list[str]:
    return [m.role for m in messages]


def _fed_back(server: Any, index: int) -> list[tuple[str, bool, str]]:
    """(call id, is_error, text) of each tool result the engine sent on request
    ``index``, whatever the dialect."""
    body = server.requests[index].body
    out: list[tuple[str, bool, str]] = []
    for item in body.get("messages") or []:
        if item.get("role") == "tool":
            out.append((item["tool_call_id"], None, item["content"]))
        elif item.get("role") == "user" and isinstance(item.get("content"), list):
            for block in item["content"]:
                if block.get("type") == "tool_result":
                    text = "".join(c.get("text", "") for c in block.get("content") or [])
                    out.append((block["tool_use_id"], block["is_error"], text))
    for item in body.get("input") or []:
        if item.get("type") == "function_call_output":
            out.append((item["call_id"], None, item["output"]))
    return out


# ─── replies and tool calls, per dialect ────────────────────────────────────


@pytest.mark.parametrize("kind", [*DIALECTS, "local"])
async def test_a_text_reply_is_one_assistant_node_with_its_usage(server, kind):
    server.script(text_reply(kind, "Hello there."))
    run = Harness(server, kind)
    try:
        events = await run.turn("hi")
        stored = await run.stored()
    finally:
        await run.aclose()

    done = _terminal(events)
    assert isinstance(done, TurnComplete) and done.stop_reason == "end_turn"
    assert _roles(stored) == ["user", "assistant"] and stored[1].text() == "Hello there."
    [usage] = _of(events, Usage)
    assert (usage.input_tokens, usage.output_tokens) == (10, 5)
    assert done.usage["input_tokens"] == 10 and done.usage["output_tokens"] == 5


@pytest.mark.parametrize("kind", DIALECTS)
async def test_parallel_calls_run_in_order_and_their_results_go_back_by_id(server, kind):
    calls = [{"id": "call_a", "name": "Echo", "args": {"text": "first"}},
             {"id": "call_b", "name": "Echo", "args": {"text": "second"}}]
    server.script(tool_reply(kind, calls), text_reply(kind, "both done"))
    run = Harness(server, kind)
    try:
        events = await run.turn()
        stored = await run.stored()
    finally:
        await run.aclose()

    uses, results = _of(events, ToolUse), _of(events, ToolResult)
    assert [(u.tool_use_id, u.input) for u in uses] == [
        ("call_a", {"text": "first"}), ("call_b", {"text": "second"})]
    assert [(r.tool_use_id, r.is_error) for r in results] == [
        ("call_a", False), ("call_b", False)]
    assert run.ran == [("Echo", {"text": "first"}), ("Echo", {"text": "second"})]
    # Each ToolUse precedes the results, and every result follows its use.
    order = [type(e).__name__ for e in events if isinstance(e, (ToolUse, ToolResult))]
    assert order == ["ToolUse", "ToolUse", "ToolResult", "ToolResult"]
    assert [(i, t) for i, _, t in _fed_back(server, 1)] == [
        ("call_a", 'echo:{"text": "first"}'), ("call_b", 'echo:{"text": "second"}')]
    assert _roles(stored) == ["user", "assistant", "user", "assistant"]
    assert [b.id for b in stored[1].content if isinstance(b, ToolUseBlock)] == [
        "call_a", "call_b"]
    assert [b.tool_use_id for b in stored[2].content if isinstance(b, ToolResultBlock)] == [
        "call_a", "call_b"]


@pytest.mark.parametrize("kind", DIALECTS)
async def test_arguments_split_across_many_chunks_are_reassembled_byte_for_byte(
    server, kind
):
    args = {"text": 'quote " brace } comma , unicode é ✓ newline \n end'}
    server.script(
        tool_reply(kind, [{"id": "call_1", "name": "Echo", "args": args}], pieces=17),
        text_reply(kind, "ok"),
    )
    run = Harness(server, kind)
    try:
        events = await run.turn()
    finally:
        await run.aclose()

    [use] = _of(events, ToolUse)
    assert use.input == args
    assert run.ran == [("Echo", args)]


@pytest.mark.parametrize("kind", DIALECTS)
async def test_malformed_arguments_fail_the_call_not_the_turn(server, kind):
    server.script(
        tool_reply(kind, [{"id": "call_1", "name": "Echo", "args": '{"text": "a",}'}]),
        text_reply(kind, "retrying differently"),
    )
    run = Harness(server, kind)
    try:
        events = await run.turn()
    finally:
        await run.aclose()

    [result] = _of(events, ToolResult)
    assert result.is_error and "not valid JSON" in result.output
    assert run.ran == []
    assert isinstance(_terminal(events), TurnComplete)
    [(call_id, _, text)] = _fed_back(server, 1)
    assert call_id == "call_1" and "not valid JSON" in text


@pytest.mark.parametrize("kind", DIALECTS)
async def test_an_unknown_tool_is_answered_in_context(server, kind):
    server.script(
        tool_reply(kind, [{"id": "call_1", "name": "Teleport", "args": {"to": "mars"}}]),
        text_reply(kind, "cannot"),
    )
    run = Harness(server, kind)
    try:
        events = await run.turn()
    finally:
        await run.aclose()

    [use] = _of(events, ToolUse)
    [result] = _of(events, ToolResult)
    assert use.name == result.name == "Teleport"
    assert result.is_error and "unknown tool: Teleport" in result.output
    assert isinstance(_terminal(events), TurnComplete)


async def test_a_multi_step_tool_loop_ends_on_the_final_message(server):
    server.script(
        Messages.tool_calls([{"id": "t1", "name": "Echo", "args": {"text": "1"}}]),
        Messages.tool_calls([{"id": "t2", "name": "Echo", "args": {"text": "2"}}],
                            text="Next."),
        Messages.tool_calls([{"id": "t3", "name": "Echo", "args": {"text": "3"}}]),
        Messages.text("All three done.", input_tokens=40, output_tokens=7),
    )
    run = Harness(server, "anthropic")
    try:
        events = await run.turn()
        stored = await run.stored()
    finally:
        await run.aclose()

    assert [r.tool_use_id for r in _of(events, ToolResult)] == ["t1", "t2", "t3"]
    done = _terminal(events)
    assert isinstance(done, TurnComplete) and done.num_model_calls == 4
    assert done.usage["input_tokens"] == 70 and done.usage["output_tokens"] == 22
    assert len(_of(events, Usage)) == 4
    assert _roles(stored) == ["user", *["assistant", "user"] * 3, "assistant"]
    assert stored[-1].text() == "All three done."
    # Each call carried the whole history so far: request n has 2n-1 messages.
    assert [len(r.body["messages"]) for r in server.requests] == [1, 3, 5, 7]


# ─── permissions ────────────────────────────────────────────────────────────


def _write_call(kind: str = "anthropic") -> list:
    return tool_reply(kind, [{"id": "call_w", "name": "Write",
                              "args": {"file_path": "a.txt", "content": "x"}}])


def _respond(decision: str, message: str | None = None) -> Responder:
    async def respond(loop: NativeLoop, request: PermissionRequest) -> None:
        await loop.submit(RespondPermission(
            request_id=request.request_id, decision=decision, message=message))
    return respond


async def test_ask_puts_a_write_to_the_responder_and_runs_it_when_allowed(server):
    server.script(_write_call(), Messages.text("written"))
    run = Harness(server, policy="on_write")
    try:
        events = await run.turn(responder=_respond("allow"))
    finally:
        await run.aclose()

    [request] = _of(events, PermissionRequest)
    assert (request.name, request.tool_use_id, request.risk) == ("Write", "call_w", "write")
    assert run.ran == [("Write", {"file_path": "a.txt", "content": "x"})]
    [result] = _of(events, ToolResult)
    assert not result.is_error
    kinds = [type(e).__name__ for e in events
             if isinstance(e, (ToolUse, PermissionRequest, ToolResult))]
    assert kinds == ["ToolUse", "PermissionRequest", "ToolResult"]


async def test_a_denied_write_gives_the_model_the_responders_reason(server):
    server.script(_write_call(), Messages.text("ok"))
    run = Harness(server, policy="on_write")
    try:
        events = await run.turn(responder=_respond("deny", "not in this repo"))
    finally:
        await run.aclose()

    assert run.ran == []
    [result] = _of(events, ToolResult)
    assert result.is_error and result.output == "not in this repo"
    assert _fed_back(server, 1) == [("call_w", True, "not in this repo")]
    assert isinstance(_terminal(events), TurnComplete)


async def test_an_unanswered_prompt_is_denied_after_the_permission_timeout(server):
    server.script(_write_call(), Messages.text("ok"))
    run = Harness(server, policy="on_write")
    run.loop.permission_timeout = 0.2
    try:
        events = await run.turn()
    finally:
        await run.aclose()

    assert run.ran == []
    [result] = _of(events, ToolResult)
    assert result.is_error and "timed out" in result.output
    assert isinstance(_terminal(events), TurnComplete)


async def test_stop_while_a_prompt_is_open_ends_the_turn_without_running_the_tool(server):
    server.script(_write_call(), Messages.text("never sent"))
    run = Harness(server, policy="on_write")

    async def stop(loop: NativeLoop, request: PermissionRequest) -> None:
        await loop.submit(Interrupt())

    try:
        events = await run.turn(responder=stop)
        stored = await run.stored()
    finally:
        await run.aclose()

    assert run.ran == []
    [result] = _of(events, ToolResult)
    assert result.is_error and result.tool_use_id == "call_w"
    assert isinstance(_terminal(events), Interrupted)
    assert len(server.requests) == 1
    # The transcript stays well formed: the call has its (cancelled) result.
    assert _roles(stored) == ["user", "assistant", "user"]


@pytest.mark.parametrize(
    "policy, asks", [("never_ask", False), ("on_write", True), ("always_ask", True)]
)
async def test_the_policy_decides_whether_a_write_asks(server, policy, asks):
    server.script(_write_call(), Messages.text("ok"))
    run = Harness(server, policy=policy)
    try:
        events = await run.turn(responder=_respond("allow"))
    finally:
        await run.aclose()

    assert bool(_of(events, PermissionRequest)) is asks
    assert run.ran == [("Write", {"file_path": "a.txt", "content": "x"})]


# ─── provider refusals, retries and what the user sees ──────────────────────


@pytest.mark.parametrize("kind", DIALECTS)
async def test_a_429_with_retry_after_is_retried_and_the_turn_succeeds(server, kind):
    server.script(fps.error_reply(429, "slow down", retry_after="0"), text_reply(kind, "ok"))
    run = Harness(server, kind)
    try:
        events = await run.turn()
    finally:
        await run.aclose()

    assert isinstance(_terminal(events), TurnComplete)
    assert len(server.requests) == 2
    assert server.requests[0].body == server.requests[1].body


@pytest.mark.parametrize("kind", DIALECTS)
async def test_retriable_refusals_that_never_clear_end_the_turn_as_a_retriable_error(
    server, kind
):
    server.script(*[fps.error_reply(503, "upstream down") for _ in range(3)])
    run = Harness(server, kind)
    try:
        events = await run.turn()
        stored = await run.stored()
    finally:
        await run.aclose()

    error = _terminal(events)
    assert isinstance(error, ErrorEvent) and error.retriable
    assert "upstream down" in error.message
    assert len(server.requests) == 3
    assert _roles(stored) == ["user"]


async def test_a_bad_request_is_not_retried(server):
    server.script(fps.error_reply(400, "tools.0.name: invalid"))
    run = Harness(server, "anthropic")
    try:
        events = await run.turn()
    finally:
        await run.aclose()

    error = _terminal(events)
    assert isinstance(error, ErrorEvent) and not error.retriable
    assert "tools.0.name: invalid" in error.message
    assert len(server.requests) == 1


async def test_an_overloaded_stream_before_content_is_retried(server):
    server.script([Messages.start(), Messages.error()], Messages.text("ok"))
    run = Harness(server, "anthropic")
    try:
        events = await run.turn()
    finally:
        await run.aclose()

    assert isinstance(_terminal(events), TurnComplete)
    assert len(server.requests) == 2


async def test_an_overloaded_stream_after_a_tool_call_leaves_no_call_unanswered(server):
    """Regression: the ToolUse event of a tool block that closed before the
    stream failed had no ToolResult, so a host kept its card open forever."""
    server.script([
        Messages.start(), *Messages.tool_block(0, "call_1", "Echo", {"text": "x"}),
        Messages.error(),
    ])
    run = Harness(server, "anthropic")
    try:
        events = await run.turn()
    finally:
        await run.aclose()

    error = _terminal(events)
    assert isinstance(error, ErrorEvent) and error.message == "Overloaded"
    assert [u.tool_use_id for u in _of(events, ToolUse)] == ["call_1"]
    [result] = _of(events, ToolResult)
    assert result.tool_use_id == "call_1" and result.is_error
    assert run.ran == []


# ─── streams that stall, get cut, or end early ──────────────────────────────


@pytest.mark.parametrize("kind", ["anthropic", "responses"])
async def test_a_reply_cut_without_its_terminal_event_is_an_error_not_an_answer(
    server, kind
):
    """Regression: a body that ended cleanly mid-reply (a gateway closing the
    socket) was stored as the finished answer and the turn completed."""
    if kind == "anthropic":
        cut = [Messages.start(), *Messages.text_block(0, "The answer is")]
    else:
        cut = [Responses.created(), *Responses.message("msg_1", "The answer is")]
    server.script(cut)
    run = Harness(server, kind)
    try:
        events = await run.turn()
        stored = await run.stored()
    finally:
        await run.aclose()

    error = _terminal(events)
    assert isinstance(error, ErrorEvent)
    assert "truncated" in error.message
    # What streamed is kept, marked as interrupted rather than finished.
    assert _roles(stored) == ["user", "assistant"]
    assert stored[1].text() == "The answer is"
    assert stored[1].provider_meta == {"interrupted": "provider_error"}
    assert error.node_id is not None


@pytest.mark.parametrize("kind", ["anthropic", "responses"])
async def test_a_tool_call_cut_mid_arguments_is_retried_not_stored_half_open(server, kind):
    """Regression: an Anthropic stream that ended inside a tool block stored
    an assistant tool_use with no result and no ToolUse event, and the turn
    completed as if the model had finished."""
    if kind == "anthropic":
        cut = [Messages.start(),
               *Messages.tool_block(0, "call_1", "Echo", '{"text": "hel', stop=False)]
    else:
        cut = [Responses.created(),
               *Responses.function_call("fc_0", "call_1", "Echo", '{"text": "hel', done=False)]
    server.script(cut, text_reply(kind, "second try"))
    run = Harness(server, kind)
    try:
        events = await run.turn()
        stored = await run.stored()
    finally:
        await run.aclose()

    [retry] = _of(events, ProviderRetry)
    assert retry.reason == "truncated stream from provider (cut mid tool arguments)"
    assert retry.max_attempts == native_mod.TRUNCATED_STREAM_MAX_RETRIES == 3
    assert isinstance(_terminal(events), TurnComplete)
    assert _of(events, ToolUse) == [] and run.ran == []
    assert _roles(stored) == ["user", "assistant"] and stored[1].text() == "second try"


@pytest.mark.parametrize("kind", ["anthropic", "responses"])
async def test_a_tool_call_cut_after_its_block_closed_gets_an_error_result(server, kind):
    if kind == "anthropic":
        cut = [Messages.start(), *Messages.tool_block(0, "call_1", "Echo", {"text": "x"})]
    else:
        cut = [Responses.created(),
               *Responses.function_call("fc_0", "call_1", "Echo", {"text": "x"})]
    server.script(cut)
    run = Harness(server, kind)
    try:
        events = await run.turn()
    finally:
        await run.aclose()

    assert isinstance(_terminal(events), ErrorEvent)
    [use] = _of(events, ToolUse)
    [result] = _of(events, ToolResult)
    assert result.tool_use_id == use.tool_use_id == "call_1" and result.is_error
    assert run.ran == []


async def test_a_chat_stream_cut_mid_arguments_is_retried(server):
    frames = Chat.tool_calls([{"id": "c1", "name": "Echo", "args": {"text": "hello"}}],
                             finish=None, usage=False, done=False)
    server.script(frames[:-1], Chat.text("fine"))
    run = Harness(server, "openrouter")
    try:
        events = await run.turn()
    finally:
        await run.aclose()

    assert isinstance(_terminal(events), TurnComplete)
    assert run.ran == [] and len(server.requests) == 2


async def test_a_socket_dropped_mid_reply_keeps_the_text_and_says_so(server):
    server.script(Reply(frames=[
        Messages.start(),
        Messages.ev("content_block_start", index=0, content_block={"type": "text", "text": ""}),
        Messages.ev("content_block_delta", index=0,
                    delta={"type": "text_delta", "text": "Partial"}),
        CUT,
    ], framing="chunked"))
    run = Harness(server, "anthropic")
    try:
        events = await run.turn()
        stored = await run.stored()
    finally:
        await run.aclose()

    error = _terminal(events)
    assert isinstance(error, ErrorEvent) and not error.retriable
    assert "connection was interrupted" in error.message
    assert _roles(stored) == ["user", "assistant"] and stored[1].text() == "Partial"
    assert stored[1].provider_meta == {"interrupted": "transport_failure"}
    assert len(server.requests) == 1  # never replayed after content flowed


async def test_a_stream_that_stalls_after_content_times_out_instead_of_hanging(server):
    server.script([
        Messages.start(),
        Messages.ev("content_block_start", index=0, content_block={"type": "text", "text": ""}),
        Messages.ev("content_block_delta", index=0, delta={"type": "text_delta", "text": "Hm"}),
        Sleep(10),
    ])
    run = Harness(server, "anthropic", client_kwargs={"timeout": 0.3})
    try:
        events = await run.turn()
    finally:
        await run.aclose()

    error = _terminal(events)
    assert isinstance(error, ErrorEvent) and "connection was interrupted" in error.message
    assert len(server.requests) == 1


async def test_a_chat_provider_that_never_sends_data_is_given_up_on(server, monkeypatch):
    monkeypatch.setattr(_chat_completions, "FIRST_DATA_TIMEOUT", 0.2)
    server.script([Sleep(10)], Chat.text("second replica answered"))
    run = Harness(server, "openrouter")
    try:
        events = await run.turn()
    finally:
        await run.aclose()

    assert [r.reason for r in _of(events, ProviderRetry)] == ["empty response from provider"]
    assert isinstance(_terminal(events), TurnComplete)
    # OpenRouter's sticky routing key is dropped for the retry.
    assert server.requests[0].body.get("session_id")
    assert "session_id" not in server.requests[1].body


@pytest.mark.parametrize("kind", ["anthropic", "local"])
async def test_an_empty_completion_is_retried_without_a_false_sticky_alert(server, kind):
    """Regression: an empty completion on a provider that is never sent an
    affinity key (Anthropic, a local server) still announced that its sticky
    routing key was dropped."""
    if kind == "anthropic":
        empty = [Messages.start(input_tokens=0), *Messages.end("end_turn", 0)]
    else:
        empty = [Chat.chunk({}, "stop"), Chat.usage(0, 0), Chat.DONE]
    server.script(empty, text_reply(kind, "ok"))
    run = Harness(server, kind)
    try:
        events = await run.turn()
    finally:
        await run.aclose()

    assert [r.reason for r in _of(events, ProviderRetry)] == ["empty response from provider"]
    assert isinstance(_terminal(events), TurnComplete)
    assert _of(events, CacheMissAlert) == []


# ─── usage present or absent ────────────────────────────────────────────────


@pytest.mark.parametrize("kind", ["local", "openrouter"])
async def test_a_complete_chat_stream_without_a_usage_chunk_is_still_an_answer(
    server, kind
):
    """Regression: a server that ignores ``stream_options.include_usage`` ends
    with finish_reason and [DONE] but no usage chunk; every such reply was
    dropped and the turn failed on "stream ended before ... usage chunk"."""
    server.script(Chat.text("Complete answer.", usage=False))
    run = Harness(server, kind)
    try:
        events = await run.turn()
        stored = await run.stored()
    finally:
        await run.aclose()

    done = _terminal(events)
    assert isinstance(done, TurnComplete) and done.stop_reason == "end_turn"
    assert stored[-1].text() == "Complete answer."
    # Billed all the same: estimated like an interrupted call, and flagged.
    [usage] = _of(events, Usage)
    assert usage.input_tokens > 0 and usage.output_tokens > 0
    assert usage.raw["input_tokens_estimated"] is True
    assert usage.raw["output_tokens_estimated"] is True


async def test_a_tool_loop_without_usage_chunks_runs_to_the_end(server):
    server.script(
        Chat.tool_calls([{"id": "c1", "name": "Echo", "args": {"text": "a"}}], usage=False),
        Chat.text("done", usage=False),
    )
    run = Harness(server, "local")
    try:
        events = await run.turn()
    finally:
        await run.aclose()

    assert run.ran == [("Echo", {"text": "a"})]
    assert isinstance(_terminal(events), TurnComplete)


async def test_a_chat_stream_without_finish_reason_or_usage_is_still_truncated(server):
    server.script(Chat.text("maybe cut", usage=False, finish=None))
    run = Harness(server, "local")
    try:
        events = await run.turn()
    finally:
        await run.aclose()

    error = _terminal(events)
    assert isinstance(error, ErrorEvent) and "terminal usage chunk" in error.message


async def test_usage_reported_across_message_start_and_delta_is_booked_once(server):
    server.script([
        Messages.start(input_tokens=120, cache_read=4000), *Messages.text_block(0, "hi"),
        *Messages.end("end_turn", output_tokens=33),
    ])
    run = Harness(server, "anthropic")
    try:
        events = await run.turn()
    finally:
        await run.aclose()

    [usage] = _of(events, Usage)
    assert (usage.input_tokens, usage.output_tokens, usage.cache_read_tokens) == (120, 33, 4000)
    done = _terminal(events)
    assert done.usage["input_tokens"] == 120 and done.cost_usd and done.cost_usd > 0


# ─── tool calls with no id ──────────────────────────────────────────────────


async def test_tool_calls_streamed_without_ids_get_distinct_ones(server):
    """Regression: an OpenAI-compatible server that omits ``tool_calls[].id``
    gave every call the id "", so parallel results could not be told apart and
    the next request answered each with ``tool_call_id: ""``."""
    calls = [{"id": "", "name": "Echo", "args": {"text": "a"}},
             {"id": "", "name": "Echo", "args": {"text": "b"}}]
    server.script(Chat.tool_calls(calls, with_ids=False), Chat.text("ok"))
    run = Harness(server, "local")
    try:
        events = await run.turn()
    finally:
        await run.aclose()

    ids = [u.tool_use_id for u in _of(events, ToolUse)]
    assert len(set(ids)) == 2 and all(ids)
    assert [r.tool_use_id for r in _of(events, ToolResult)] == ids
    sent = server.requests[1].body["messages"]
    assert [c["id"] for c in sent[1]["tool_calls"]] == ids
    assert [m["tool_call_id"] for m in sent[2:4]] == ids


# ─── Stop and loop limits ───────────────────────────────────────────────────


@pytest.mark.parametrize("kind", DIALECTS)
async def test_stop_mid_stream_ends_the_turn_and_abandons_the_request(server, kind):
    if kind == "anthropic":
        head = [Messages.start(),
                Messages.ev("content_block_start", index=0,
                            content_block={"type": "text", "text": ""}),
                Messages.ev("content_block_delta", index=0,
                            delta={"type": "text_delta", "text": "Working"})]
    elif kind == "responses":
        head = [Responses.created(), *Responses.message("m1", "Working", pieces=1)[:2]]
    else:
        head = [Chat.chunk({"content": "Working"})]
    server.script([*head, Sleep(30)], text_reply(kind, "next turn"))
    run = Harness(server, kind)

    async def stop_on_text(loop: NativeLoop) -> None:
        while not any(getattr(e, "text", None) == "Working"
                      for e in loop.bus.log(loop.session_id)):
            await asyncio.sleep(0.01)
        await loop.submit(Interrupt())

    try:
        events = await run.turn(during=stop_on_text)
        stored = await run.stored()
        again = await run.turn("again")
    finally:
        await run.aclose()

    assert isinstance(_terminal(events), Interrupted)
    assert _roles(stored) == ["user", "assistant"] and stored[1].text() == "Working"
    assert isinstance(_terminal(again), TurnComplete)
    assert len(server.requests) == 2


async def test_the_model_call_cap_answers_the_last_calls_and_stops(server):
    server.script(
        Messages.tool_calls([{"id": "t1", "name": "Echo", "args": {"text": "1"}}]),
        Messages.tool_calls([{"id": "t2", "name": "Echo", "args": {"text": "2"}}]),
    )
    run = Harness(server, "anthropic", max_model_calls_per_turn=2)
    try:
        events = await run.turn()
        stored = await run.stored()
    finally:
        await run.aclose()

    done = _terminal(events)
    assert isinstance(done, TurnComplete) and done.stop_reason == "max_calls"
    results = _of(events, ToolResult)
    assert [(r.tool_use_id, r.is_error) for r in results] == [("t1", False), ("t2", True)]
    assert "maximum number of model calls" in results[1].output
    assert run.ran == [("Echo", {"text": "1"})]
    assert _roles(stored)[-1] == "user"  # every call has its result


async def test_the_doom_loop_guard_refuses_a_repeated_identical_call(server):
    same = {"text": "same"}
    server.script(
        *[Messages.tool_calls([{"id": f"t{i}", "name": "Echo", "args": same}])
          for i in range(3)],
        Messages.text("changing approach"),
    )
    run = Harness(server, "anthropic")
    try:
        events = await run.turn()
    finally:
        await run.aclose()

    results = _of(events, ToolResult)
    assert [r.is_error for r in results] == [False, False, True]
    assert "doom-loop guard" in results[2].output
    assert len(run.ran) == 2
    assert isinstance(_terminal(events), TurnComplete)


# ─── the request the engine sends ───────────────────────────────────────────


@pytest.mark.parametrize("kind", DIALECTS)
async def test_the_tools_offered_are_the_registry_surface(server, kind):
    server.script(text_reply(kind, "ok"))
    run = Harness(server, kind)
    try:
        await run.turn()
    finally:
        await run.aclose()

    body = server.requests[0].body
    names = sorted(t.get("name") or t.get("function", {}).get("name") for t in body["tools"])
    assert names == ["Echo", "Write"]
    assert body["stream"] is True
    assert body["model"] == MODELS[kind]



# ─── provider error status and message ──────────────────────────────────────


@pytest.mark.parametrize("kind", DIALECTS)
async def test_a_refusal_reports_its_http_status_and_the_providers_own_words(server, kind):
    """Regression: the ErrorEvent carried only the raw body text, so a host
    could not tell a rate limit from any other failure."""
    server.script(*[fps.error_reply(429, "Rate limit reached, slow down", retry_after="0")
                    for _ in range(3)])
    run = Harness(server, kind)
    try:
        events = await run.turn()
    finally:
        await run.aclose()

    error = _terminal(events)
    assert isinstance(error, ErrorEvent) and error.retriable
    assert error.status == 429
    assert error.provider_message == "Rate limit reached, slow down"
    assert len(server.requests) == 3


async def test_an_html_error_page_is_reduced_to_a_short_message(server):
    page = ("<html><head><title>502 Bad Gateway</title><style>body{color:red}</style>"
            "</head><body><h1>502 Bad Gateway</h1>" + "<p>upstream &amp; retry</p>" * 200
            + "</body></html>")
    bad_gateway = Reply(status=502, frames=[page], content_type="text/html")
    server.script(bad_gateway, bad_gateway, bad_gateway)
    run = Harness(server, "openrouter")
    try:
        events = await run.turn()
    finally:
        await run.aclose()

    error = _terminal(events)
    assert error.status == 502
    summary = error.provider_message
    assert summary.startswith("502 Bad Gateway 502 Bad Gateway upstream & retry")
    assert len(summary) <= 300 and "<" not in summary and "color:red" not in summary


async def test_an_in_stream_rate_limit_reports_status_429(server):
    server.script([Messages.start(), *Messages.text_block(0, "Hal"),
                   Messages.error("rate_limit_error", "Number of requests exceeded")])
    run = Harness(server, "anthropic")
    try:
        events = await run.turn()
    finally:
        await run.aclose()

    error = _terminal(events)
    assert (error.status, error.provider_message) == (429, "Number of requests exceeded")


async def test_a_dropped_connection_has_no_http_status(server):
    server.script(Reply(frames=[Messages.start(), *Messages.text_block(0, "x")[:2], CUT],
                        framing="chunked"))
    run = Harness(server, "anthropic")
    try:
        events = await run.turn()
    finally:
        await run.aclose()

    assert _terminal(events).status is None


# ─── partial text on a failed stream ────────────────────────────────────────


@pytest.mark.parametrize("shape", ["anthropic_error", "chat_error", "chat_cut"])
async def test_text_streamed_before_a_failure_is_kept_as_interrupted(server, shape):
    """Regression: a turn that failed mid-stream (an in-stream error, a body
    cut before its terminal chunk) dropped the text the user had watched
    arrive, while a dropped connection kept it."""
    if shape == "anthropic_error":
        kind = "anthropic"
        frames = [Messages.start(), *Messages.text_block(0, "Half an answer"),
                  Messages.error("invalid_request_error", "bad")]
    elif shape == "chat_error":
        kind = "openrouter"
        frames = [*Chat.text("Half an answer", finish=None, usage=False, done=False),
                  fps.sse({"error": {"code": 400, "message": "bad"}})]
    else:
        kind = "local"
        frames = Chat.text("Half an answer", finish=None, usage=False, done=False)
    server.script(frames)
    run = Harness(server, kind)
    try:
        events = await run.turn()
        stored = await run.stored()
    finally:
        await run.aclose()

    error = _terminal(events)
    assert isinstance(error, ErrorEvent)
    streamed = "".join(e.text for e in events if type(e).__name__ == "TextDelta")
    assert streamed == "Half an answer"
    assert _roles(stored) == ["user", "assistant"]
    assert stored[1].text() == streamed
    assert stored[1].provider_meta == {"interrupted": "provider_error"}
    assert error.node_id is not None and len(server.requests) == 1


async def test_a_failed_stream_stores_its_closed_tool_call_with_an_error_result(server):
    server.script([Messages.start(), *Messages.text_block(0, "Let me check."),
                   *Messages.tool_block(1, "call_1", "Echo", {"text": "x"}),
                   Messages.error()])
    run = Harness(server, "anthropic")
    try:
        events = await run.turn()
        stored = await run.stored()
    finally:
        await run.aclose()

    assert isinstance(_terminal(events), ErrorEvent) and run.ran == []
    assert _roles(stored) == ["user", "assistant", "user"]
    [use] = [b for b in stored[1].content if isinstance(b, ToolUseBlock)]
    [result] = stored[2].content
    assert isinstance(result, ToolResultBlock) and result.is_error
    assert use.id == result.tool_use_id == "call_1"


# ─── streams cut mid tool arguments ─────────────────────────────────────────


@pytest.mark.parametrize("kind", ["anthropic", "openrouter"])
async def test_a_stream_cut_mid_arguments_every_time_gives_up_after_three_retries(
    server, kind
):
    """Regression: a stream cut inside a tool call's arguments was retried as
    an "empty response" up to ten times."""
    if kind == "anthropic":
        cut = [Messages.start(),
               *Messages.tool_block(0, "call_1", "Echo", '{"text": "hel', stop=False)]
    else:
        cut = Chat.tool_calls([{"id": "c1", "name": "Echo", "args": {"text": "hello"}}],
                              finish=None, usage=False, done=False)[:-1]
    server.script(*[list(cut) for _ in range(4)], text_reply(kind, "never reached"))
    run = Harness(server, kind)
    try:
        events = await run.turn()
    finally:
        await run.aclose()

    retries = _of(events, ProviderRetry)
    assert [(r.attempt, r.max_attempts) for r in retries] == [(1, 3), (2, 3), (3, 3)]
    assert {r.reason for r in retries} == {
        "truncated stream from provider (cut mid tool arguments)"}
    error = _terminal(events)
    assert isinstance(error, ErrorEvent)
    assert "cut mid tool arguments 4 times in a row" in error.message
    assert len(server.requests) == 4 and run.ran == []


async def test_an_empty_reply_keeps_its_own_label_and_budget(server):
    empty = [Messages.start(input_tokens=0), *Messages.end("end_turn", 0)]
    server.script(empty, empty, Messages.text("ok"))
    run = Harness(server, "anthropic")
    try:
        events = await run.turn()
    finally:
        await run.aclose()

    assert [(r.reason, r.max_attempts) for r in _of(events, ProviderRetry)] == [
        ("empty response from provider", native_mod.EMPTY_COMPLETION_MAX_RETRIES)] * 2
    assert isinstance(_terminal(events), TurnComplete)


# ─── OpenRouter-only body fields ────────────────────────────────────────────


class _SendTo(httpx.AsyncBaseTransport):
    """Deliver every request to the fake server, whatever host it names."""

    def __init__(self, origin: str) -> None:
        self._origin = httpx.URL(origin)
        self._inner = httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        request.url = request.url.copy_with(
            scheme=self._origin.scheme, host=self._origin.host, port=self._origin.port,
        )
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


@pytest.mark.parametrize("url,explicit,expected", [
    ("https://openrouter.ai/api/v1/chat/completions", None, True),
    ("https://eu.openrouter.ai/api/v1/chat/completions", None, True),
    ("https://api.groq.com/openai/v1/chat/completions", None, False),
    ("https://api.x.ai/v1/chat/completions", None, False),
    ("http://vllm.internal:8000/v1/chat/completions", None, False),
    ("https://proxy.example/v1/chat/completions", True, True),
])
async def test_openrouter_body_fields_go_only_to_openrouter(server, url, explicit, expected):
    """Regression: custom OpenAI-format rows (vLLM, Groq, xAI) run through the
    OpenRouter client and were sent ``usage`` and ``provider``, which a strict
    server rejects."""
    server.script(Chat.text("ok"))
    http = httpx.AsyncClient(transport=_SendTo(server.url))
    store = MemoryStore()
    loop = NativeLoop(
        provider=OpenRouterClient(api_key="k", base_url=url, http_client=http,
                                  retry=FAST_RETRY, openrouter=explicit),
        store=store, model="vendor/fake-model",
    )
    try:
        await loop.submit(UserTurn(content="hi"))
        await asyncio.wait_for(loop.wait_turn(), TURN_BUDGET)
    finally:
        await loop.aclose()
        await http.aclose()

    body = server.requests[0].body
    assert ("usage" in body, "provider" in body) == (expected, expected)
    if expected:
        assert body["usage"] == {"include": True}
        assert body["provider"] == {"allow_fallbacks": False}
    assert body["stream_options"] == {"include_usage": True}


# ─── a provider that says nothing ───────────────────────────────────────────


async def test_a_silent_anthropic_stream_after_its_headers_ends_at_the_first_byte_timeout(server):
    """Regression: a stream that answered 200 and then sent nothing held the
    turn for the whole 600 s read timeout.

    R11-6: it is not re-sent either. The provider accepted the call (and may
    bill it), and a gateway that holds the first event until its upstream
    answers cost a full first-byte budget per attempt, four times over."""
    server.script([Sleep(30)], Messages.text("second attempt"))
    run = Harness(server, "anthropic", client_kwargs={"first_byte_timeout": 0.3})
    started = time.monotonic()
    try:
        events = await run.turn()
        stored = await run.stored()
    finally:
        await run.aclose()

    assert time.monotonic() - started < 10
    assert isinstance(_terminal(events), ErrorEvent)
    assert len(server.requests) == 1
    assert all(node.text() != "second attempt" for node in stored)


async def test_anthropic_headers_that_never_arrive_hit_the_first_byte_timeout():
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            await asyncio.sleep(30)
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=b"".join(Messages.text("ok")))

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = AnthropicClient(api_key="k", base_url="https://api.anthropic.test/v1/messages",
                             http_client=http, retry=FAST_RETRY, first_byte_timeout=0.3)
    loop = NativeLoop(provider=client, store=MemoryStore(), model=MODELS["anthropic"])
    started = time.monotonic()
    try:
        await loop.submit(UserTurn(content="hi"))
        await asyncio.wait_for(loop.wait_turn(), TURN_BUDGET)
    finally:
        await loop.aclose()
        await http.aclose()

    assert time.monotonic() - started < 10
    assert calls == 2
    assert isinstance(_terminal(loop.bus.log(loop.session_id)), TurnComplete)


async def test_a_provider_that_never_answers_is_not_reported_as_an_interrupted_reply():
    """R10-24: after every first-byte timeout the error said the response
    "may be incomplete" and "No automatic replay was attempted", although
    several requests were sent and nothing at all came back."""
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        await asyncio.sleep(30)
        raise AssertionError("unreachable")

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = AnthropicClient(api_key="k", base_url="https://api.anthropic.test/v1/messages",
                             http_client=http, retry=FAST_RETRY, first_byte_timeout=0.2)
    loop = NativeLoop(provider=client, store=MemoryStore(), model=MODELS["anthropic"])
    try:
        await loop.submit(UserTurn(content="hi"))
        await asyncio.wait_for(loop.wait_turn(), TURN_BUDGET)
    finally:
        await loop.aclose()
        await http.aclose()

    error = _terminal(loop.bus.log(loop.session_id))
    assert isinstance(error, ErrorEvent)
    assert calls > 1
    assert f"sent nothing before the connection failed ({calls} attempts)" in error.message
    assert "No automatic replay" not in error.message
    assert "incomplete" not in error.message


def test_the_first_byte_timeout_defaults_to_sixty_seconds():
    assert AnthropicClient(api_key="k")._first_byte_timeout == 60.0
    assert OpenRouterClient(api_key="k")._first_byte_timeout == 60.0
    assert OpenAIResponsesClient(api_key="k")._first_byte_timeout == 60.0
    # A local server may hold its headers while it loads a model.
    assert LocalClient(base_url="http://127.0.0.1:1")._first_byte_timeout is None


def _hold_headers(server: Any, seconds: float) -> None:
    """The next request gets its scripted reply, but no response headers for
    ``seconds`` (released early when the server closes)."""
    original = server._next_reply
    held = [1]

    def delayed() -> Any:
        reply = original()
        if held[0]:
            held[0] -= 1
            server._stopping.wait(seconds)
        return reply

    server._next_reply = delayed


@pytest.mark.parametrize("kind", ["openrouter", "responses"])
async def test_headers_that_never_arrive_hit_the_first_byte_timeout_and_are_retried(
    server, kind
):
    """Regression: only the Anthropic client had a first-byte bound, so a
    chat/completions or Responses server that accepted the connection and
    said nothing held the turn for the whole 600 s read timeout. Before the
    headers nothing was accepted, so the call is re-sent like any pre-content
    transport failure."""
    _hold_headers(server, 30)
    server.script(text_reply(kind, "never sent"), text_reply(kind, "second attempt"))
    run = Harness(server, kind, client_kwargs={"first_byte_timeout": 0.3})
    started = time.monotonic()
    try:
        events = await run.turn()
        stored = await run.stored()
    finally:
        await run.aclose()

    assert time.monotonic() - started < 10
    assert isinstance(_terminal(events), TurnComplete)
    assert len(server.requests) == 2
    assert stored[-1].text() == "second attempt"


@pytest.mark.parametrize("kind", ["openrouter", "responses"])
async def test_a_silent_stream_after_its_headers_is_reported_as_truncated(server, kind):
    """After a 200 the provider accepted (and may bill) the call: the client
    does not re-send it but reports the empty stream it got."""
    server.script([Sleep(30)], text_reply(kind, "never requested"))
    client = _client(kind, server.url, first_byte_timeout=0.3)
    request = ProviderRequest(
        model=MODELS[kind], messages=[Message(role="user", content=[Text("hi")])]
    )
    started = time.monotonic()
    chunks = [chunk async for chunk in client.stream(request)]

    assert time.monotonic() - started < 10
    [error] = [c for c in chunks if c.kind == "error"]
    assert "terminal usage chunk" in error.payload["message"]
    assert "truncated response" in error.payload["message"]
    assert "no data within 0.3s first-byte timeout" in error.payload["message"]
    assert error.payload["retriable"] is False
    assert "transport_failure" not in error.payload
    assert not any(c.kind in ("message_start", "text_delta") for c in chunks)
    assert len(server.requests) == 1


@pytest.mark.parametrize("kind", ["openrouter", "responses"])
async def test_a_silent_stream_after_its_headers_is_retried_by_the_engine_as_empty(
    server, kind
):
    server.script([Sleep(30)], text_reply(kind, "second attempt"))
    run = Harness(server, kind, client_kwargs={"first_byte_timeout": 0.3})
    started = time.monotonic()
    try:
        events = await run.turn()
        stored = await run.stored()
    finally:
        await run.aclose()

    assert time.monotonic() - started < 10
    assert [r.reason for r in _of(events, ProviderRetry)] == ["empty response from provider"]
    assert isinstance(_terminal(events), TurnComplete)
    assert len(server.requests) == 2
    assert stored[-1].text() == "second attempt"


# ─── replies without usage ──────────────────────────────────────────────────


async def test_a_reply_without_usage_is_booked_at_an_estimated_cost(server):
    """Regression: a reply with no usage chunk booked 0 tokens and no cost.

    A hosted chat-completions server: the guessed token counts are priced
    from the host's table and the price is flagged as table-sourced."""
    server.script(Chat.text("An answer long enough to estimate.", usage=False))
    rates = ModelPrice(input=3.0, output=15.0, source="test")
    run = Harness(server, "openrouter", pricing=lambda model: rates)
    try:
        events = await run.turn()
    finally:
        await run.aclose()

    [usage] = _of(events, Usage)
    assert usage.output_tokens > 0 and usage.input_tokens > 0
    assert usage.raw["output_tokens_estimated"] and usage.raw["input_tokens_estimated"]
    assert usage.raw["cost_from_table"] is True
    done = _terminal(events)
    assert done.cost_usd and done.cost_usd > 0
    # An estimate is not a measurement: the context gauge keeps its own.
    assert run.loop._last_call_usage.get(run.loop.session_id) is None


async def test_a_local_reply_without_usage_is_estimated_but_never_priced(server):
    """The same reply from the local dialect: the token counts are still
    estimated and flagged, but no price table applies, not even the host's
    resolver, which would otherwise bill a local model whose name matches a
    hosted one. Local inference costs 0, and that 0 is not an estimate."""
    server.script(Chat.text("An answer long enough to estimate.", usage=False))
    rates = ModelPrice(input=3.0, output=15.0, source="test")
    run = Harness(server, "local", pricing=lambda model: rates)
    try:
        events = await run.turn()
    finally:
        await run.aclose()

    [usage] = _of(events, Usage)
    assert usage.output_tokens > 0 and usage.input_tokens > 0
    assert usage.raw["output_tokens_estimated"] and usage.raw["input_tokens_estimated"]
    assert usage.raw["cost_usd"] == 0
    assert "cost_from_table" not in usage.raw
    done = _terminal(events)
    assert done.cost_usd == 0


async def test_an_empty_reply_without_usage_is_still_retried_as_empty(server):
    server.script([Chat.chunk({}, "stop"), Chat.DONE], Chat.text("ok"))
    run = Harness(server, "local")
    try:
        events = await run.turn()
    finally:
        await run.aclose()

    assert [r.reason for r in _of(events, ProviderRetry)] == ["empty response from provider"]
    assert isinstance(_terminal(events), TurnComplete)


# ─── OpenRouter's documented mid-stream error ───────────────────────────────


def _openrouter_mid_stream_error(code: Any = "server_error") -> bytes:
    """The chunk OpenRouter documents for a failure after streaming began: a
    top-level ``error`` next to a choice whose ``finish_reason`` is "error"."""
    return fps.sse({
        "id": "gen-1", "object": "chat.completion.chunk", "provider": "openai",
        "error": {"code": code, "message": "Provider disconnected unexpectedly"},
        "choices": [{"index": 0, "delta": {"content": ""}, "finish_reason": "error"}],
    })


async def test_openrouters_documented_mid_stream_error_is_an_error_not_an_answer(server):
    """Regression (R10-3): the chunk carries ``choices``, so the error branch
    skipped it, ``finish_reason: "error"`` read as end_turn and the cut-off
    text was stored as a finished answer."""
    server.script([Chat.chunk({"role": "assistant", "content": ""}),
                   Chat.chunk({"content": "Half an ans"}),
                   _openrouter_mid_stream_error(), Chat.DONE])
    run = Harness(server, "openrouter")
    try:
        events = await run.turn()
        stored = await run.stored()
    finally:
        await run.aclose()

    error = _terminal(events)
    assert isinstance(error, ErrorEvent)
    assert (error.status, error.provider_message) == (500, "Provider disconnected unexpectedly")
    assert _roles(stored) == ["user", "assistant"]
    assert stored[1].text() == "Half an ans"
    assert stored[1].provider_meta == {"interrupted": "provider_error"}
    assert len(server.requests) == 1  # never replayed after content


async def test_openrouters_mid_stream_error_before_content_is_retried_as_a_status(server):
    """Before any content the same chunk is a refusal with a status, retried
    by the client, not an "empty response" for the engine's ten retries."""
    server.script([Chat.chunk({"role": "assistant", "content": ""}),
                   _openrouter_mid_stream_error(), Chat.DONE],
                  Chat.text("ok"))
    run = Harness(server, "openrouter")
    try:
        events = await run.turn()
    finally:
        await run.aclose()

    assert isinstance(_terminal(events), TurnComplete)
    assert _of(events, ProviderRetry) == []
    assert len(server.requests) == 2


async def test_a_finish_reason_of_error_without_an_error_object_is_still_an_error(server):
    server.script([*Chat.text("Half", finish=None, usage=False, done=False),
                   Chat.chunk({}, "error"), Chat.DONE])
    run = Harness(server, "openrouter")
    try:
        events = await run.turn()
        stored = await run.stored()
    finally:
        await run.aclose()

    error = _terminal(events)
    assert isinstance(error, ErrorEvent) and "ended the stream with an error" in error.message
    assert stored[-1].provider_meta == {"interrupted": "provider_error"}


# ─── Resume after a reply cut off by a provider error ───────────────────────


async def _resume(run: Harness) -> list:
    from fluid.events.ops import Resume

    before = len(run.loop.bus.log(run.loop.session_id))
    await run.loop.submit(Resume())
    await asyncio.wait_for(run.loop.wait_turn(), TURN_BUDGET)
    return run.loop.bus.log(run.loop.session_id)[before:]


@pytest.mark.parametrize("kind", ["anthropic", "openrouter"])
async def test_resume_reruns_a_prompt_whose_reply_was_cut_off_by_a_provider_error(
    server, kind
):
    """Regression (R10-11): keeping the partial text put an interrupted
    assistant node at the head, and Resume refused with "nothing to resume"
    although the error was retriable."""
    if kind == "anthropic":
        failed = [Messages.start(), *Messages.text_block(0, "Half an ans"),
                  Messages.error("overloaded_error", "Overloaded")]
    else:
        failed = [Chat.chunk({"content": "Half an ans"}), _openrouter_mid_stream_error(),
                  Chat.DONE]
    server.script(failed, text_reply(kind, "The full answer"))
    run = Harness(server, kind)
    try:
        first = _terminal(await run.turn("question"))
        assert isinstance(first, ErrorEvent) and first.retriable
        prompt_node = (await run.store.load_path(run.loop.session_id))[0]

        events = await _resume(run)
        stored = await run.stored()
    finally:
        await run.aclose()

    assert isinstance(_terminal(events), TurnComplete)
    # The retry answers the prompt itself: the cut-off reply is a dead branch,
    # and the request is the one the failed call sent.
    assert [(m.role, m.text()) for m in stored] == [
        ("user", "question"), ("assistant", "The full answer")]
    assert _of(events, TurnStarted)[0].node_id == prompt_node.node_id
    assert len(server.requests) == 2
    assert server.requests[1].body == server.requests[0].body


async def test_resume_still_refuses_after_a_completed_reply(server):
    server.script(Messages.text("done"))
    run = Harness(server, "anthropic")
    try:
        await run.turn()
        events = await _resume(run)
    finally:
        await run.aclose()

    error = _terminal(events)
    assert isinstance(error, ErrorEvent) and error.message.startswith("nothing to resume")
    assert len(server.requests) == 1


# ─── billing refusals are not rate limits ───────────────────────────────────


_QUOTA_BODY = {"error": {
    "message": "You exceeded your current quota, please check your plan and billing details.",
    "type": "insufficient_quota", "code": "insufficient_quota"}}


@pytest.mark.parametrize("kind", DIALECTS)
@pytest.mark.parametrize("status,body", [
    (429, _QUOTA_BODY),
    (429, {"error": {"message": "Billing hard limit has been reached",
                     "type": "invalid_request_error",
                     "code": "billing_hard_limit_reached"}}),
    (402, {"error": {"code": "insufficient_credits", "message": "Insufficient credits."}}),
])
async def test_a_billing_refusal_is_fatal_and_not_reported_as_a_rate_limit(
    server, kind, status, body
):
    """Regression (R10-12): OpenAI sends ``insufficient_quota`` as HTTP 429.
    It was retried three times and reported as 429, so the app told the user
    to wait for a usage window that will never reset."""
    server.script(*[fps.error_reply(status, body=body, retry_after="0") for _ in range(3)])
    run = Harness(server, kind)
    try:
        events = await run.turn()
    finally:
        await run.aclose()

    error = _terminal(events)
    assert isinstance(error, ErrorEvent) and not error.retriable
    assert error.status == 402
    assert error.provider_message.startswith("Out of credit or quota")
    assert body["error"]["message"] in error.provider_message
    assert len(server.requests) == 1


@pytest.mark.parametrize("kind", DIALECTS)
@pytest.mark.parametrize("message", [
    # OpenRouter's numeric ``code: 402``: a smaller request fixes this one.
    "This request requires more credits, or fewer max_tokens. You requested up "
    "to 64000 tokens, but can only afford 12000.",
    "Insufficient credits. Add more credits.",
])
async def test_a_bare_402_is_fatal_and_carries_the_providers_words(server, kind, message):
    """R11-8: every 402 was labelled "Out of credit or quota", including
    OpenRouter's "fewer max_tokens" refusal, which lowering max_tokens fixes.
    Only a billing code says credit; a bare 402 keeps the provider's text."""
    body = {"error": {"code": 402, "message": message}}
    server.script(*[fps.error_reply(402, body=body, retry_after="0") for _ in range(3)])
    run = Harness(server, kind)
    try:
        events = await run.turn()
    finally:
        await run.aclose()

    error = _terminal(events)
    assert isinstance(error, ErrorEvent) and not error.retriable
    assert error.status == 402
    assert error.provider_message.startswith(message[:60])
    assert "Out of credit" not in error.provider_message
    assert "out of credit" not in error.message
    assert len(server.requests) == 1


@pytest.mark.parametrize("kind", DIALECTS)
async def test_a_true_rate_limit_is_still_retried_and_reported_as_429(server, kind):
    rate = {"error": {"message": "Rate limit reached for requests",
                      "type": "requests", "code": "rate_limit_exceeded"}}
    server.script(fps.error_reply(429, body=rate, retry_after="0"), text_reply(kind, "ok"))
    run = Harness(server, kind)
    try:
        events = await run.turn()
    finally:
        await run.aclose()

    assert isinstance(_terminal(events), TurnComplete)
    assert len(server.requests) == 2


# ─── first-byte timeout is a provider setting ───────────────────────────────


@pytest.mark.parametrize("kind", ["anthropic", "openrouter", "openai_responses", "local"])
@pytest.mark.parametrize("value,expected", [(None, 60.0), (5, 5.0), (0, None)])
def test_the_first_byte_timeout_comes_from_the_provider_config(kind, value, expected):
    """Regression (R10-15): the 60 s budget was fixed; a slow gateway that
    sends its first byte only after the upstream's first token could not
    raise it. It was also read for the Anthropic kind only."""
    from fluid.config.loader import _build_provider_client
    from fluid.config.model import HarnessConfig

    block: dict[str, Any] = {"kind": kind, "api_key_env": "UNSET_TEST_KEY"}
    if kind == "local":
        block["base_url"] = "http://127.0.0.1:1"
        if value is None:
            expected = None  # off unless configured
    if value is not None:
        block["first_byte_timeout"] = value
    config = HarnessConfig.from_dict({"providers": {"slow": block}})
    client = _build_provider_client(config, "slow")
    assert client._first_byte_timeout == expected
    assert config.to_dict()["providers"]["slow"]["first_byte_timeout"] == value


# ─── Responses: heartbeats alone do not keep a stream open forever ──────────


_HEARTBEAT = b": keepalive\n\n"


def _heartbeats(seconds: float, every: float = 0.05) -> list:
    frames: list = []
    for _ in range(int(seconds / every)):
        frames += [_HEARTBEAT, Sleep(every)]
    return frames


async def test_a_heartbeat_only_responses_stream_ends_as_truncated():
    """R15-6: a heartbeat counts as the first frame, so a server that answers
    200 and then sends only SSE comments passed the first-byte check and was
    bounded by the 900 s stream deadline alone (nothing when that is 0). The
    time since the last data event is now bounded too."""
    with fps.FakeProviderServer(_heartbeats(30)) as srv:
        client = _client("responses", srv.url, first_byte_timeout=0.2,
                         event_silence_timeout=0.5, stream_deadline=0)
        request = ProviderRequest(
            model=MODELS["responses"], messages=[Message(role="user", content=[Text("hi")])]
        )
        started = time.monotonic()
        chunks = [chunk async for chunk in client.stream(request)]
        elapsed = time.monotonic() - started

    assert 0.5 <= elapsed < 10
    [error] = [c for c in chunks if c.kind == "error"]
    assert "truncated response" in error.payload["message"]
    assert "only heartbeats for 0.5s" in error.payload["message"]
    # The heartbeat still satisfied the first-byte timer.
    assert "first-byte" not in error.payload["message"]
    assert error.payload["retriable"] is False
    assert any(c.kind == "provider_alive" for c in chunks)
    assert len(srv.requests) == 1


async def test_heartbeats_between_data_events_do_not_end_the_stream():
    """The bound covers only the wait for the first DATA event, not the whole
    call: long reasoning with heartbeats between real events still completes."""
    frames = [Responses.created(), *_heartbeats(0.3)]
    for piece in Responses.message("msg_1", "all done"):
        frames += [piece, *_heartbeats(0.3)]
    frames.append(Responses.completed())
    with fps.FakeProviderServer(frames) as srv:
        client = _client("responses", srv.url, event_silence_timeout=0.6)
        request = ProviderRequest(
            model=MODELS["responses"], messages=[Message(role="user", content=[Text("hi")])]
        )
        started = time.monotonic()
        chunks = [chunk async for chunk in client.stream(request)]

    assert time.monotonic() - started > 0.6  # longer than the bound in total
    assert not [c for c in chunks if c.kind == "error"]
    assert any(c.kind == "message_stop" for c in chunks)
    assert "".join(c.payload.get("text", "") for c in chunks if c.kind == "text_delta") == "all done"


async def test_heartbeats_after_data_outlast_the_read_timeout_bound():
    """R16-1: with encrypted-only reasoning, response.created (data) arrives
    and then only heartbeats until the reasoning item is done. That wait may
    exceed the read-timeout-length bound; only the stream deadline caps it."""
    frames = [Responses.created(), *_heartbeats(1.0)]
    frames += [*Responses.message("msg_1", "thought long"), Responses.completed()]
    with fps.FakeProviderServer(frames) as srv:
        client = _client("responses", srv.url, event_silence_timeout=0.3)
        request = ProviderRequest(
            model=MODELS["responses"], messages=[Message(role="user", content=[Text("hi")])]
        )
        started = time.monotonic()
        chunks = [chunk async for chunk in client.stream(request)]

    assert time.monotonic() - started >= 1.0  # well past the 0.3 s bound
    assert not [c for c in chunks if c.kind == "error"]
    assert any(c.kind == "message_stop" for c in chunks)
    assert "".join(c.payload.get("text", "") for c in chunks if c.kind == "text_delta") == "thought long"
    assert len(srv.requests) == 1


async def test_no_stream_deadline_leaves_heartbeats_after_data_unbounded():
    """R16-1: PROVIDER_STREAM_DEADLINE=0 turns the whole-call limit off. The
    default heartbeat bound (the read timeout, here 0.3 s) must not bring a
    cap back once data has arrived."""
    frames = [Responses.created(), *_heartbeats(1.0)]
    frames += [*Responses.message("msg_1", "no cap"), Responses.completed()]
    with fps.FakeProviderServer(frames) as srv:
        client = _client("responses", srv.url, timeout=0.3, stream_deadline=0)
        assert client._event_silence_timeout == 0.3
        request = ProviderRequest(
            model=MODELS["responses"], messages=[Message(role="user", content=[Text("hi")])]
        )
        chunks = [chunk async for chunk in client.stream(request)]

    assert not [c for c in chunks if c.kind == "error"]
    assert any(c.kind == "message_stop" for c in chunks)
    assert "".join(c.payload.get("text", "") for c in chunks if c.kind == "text_delta") == "no cap"


def test_the_event_silence_bound_defaults_to_the_read_timeout():
    assert OpenAIResponsesClient(api_key="k")._event_silence_timeout == 600.0
    assert OpenAIResponsesClient(api_key="k", timeout=42)._event_silence_timeout == 42
    assert OpenAIResponsesClient(api_key="k", event_silence_timeout=0)._event_silence_timeout == 0
