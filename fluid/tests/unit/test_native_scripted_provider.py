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
from pathlib import Path
from typing import Any, Awaitable, Callable

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
from fluid.events.model import CacheMissAlert, Interrupted, ProviderRetry
from fluid.events.ops import Interrupt
from fluid.messages import ToolResultBlock, ToolUseBlock
from fluid.permissions.engine import PermissionEngine
from fluid.providers import _chat_completions
from fluid.providers._retry import RetryPolicy
from fluid.providers.anthropic.client import AnthropicClient
from fluid.providers.local.client import LocalClient
from fluid.providers.openai_responses.client import OpenAIResponsesClient
from fluid.providers.openrouter.client import OpenRouterClient
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
    assert _roles(stored) == ["user"]


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

    assert [r.reason for r in _of(events, ProviderRetry)] == ["empty response from provider"]
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
    [usage] = _of(events, Usage)
    assert (usage.input_tokens, usage.output_tokens) == (0, 0)


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

