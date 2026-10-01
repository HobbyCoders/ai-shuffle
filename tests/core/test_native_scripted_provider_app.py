"""The native leg end to end, through the app adapter, against a fake provider.

A scripted loopback HTTP server (``fluid/tests/fixtures/fake_provider_server.py``)
speaks the Anthropic Messages / chat-completions wire formats, and the real
``HarnessAdapter`` → ``NativeLoop`` → fluid provider client → httpx pipeline
runs against it with the app's own tool registry (fluid's builtin Read/Write/
Bash). Each test asserts on what the app sees (tool calls, results, the
permission callback, the terminal ResultMessage and its usage), on what the
provider was sent next (the tool result fed back), and on the fluid session
store. The last test runs one scenario on both legs — this fake provider and
the scripted fake ``claude`` CLI — and checks the app gets the same shape.
Hermetic: no network.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from fluid.messages import Text, ToolResultBlock as FluidToolResult, ToolUseBlock as FluidToolUse
from fluid.providers._retry import RetryPolicy
from fluid.providers.anthropic.client import AnthropicClient
from fluid.providers.openrouter.client import OpenRouterClient
from fluid.tools.registry import ToolRegistry

import app.core.provider.harness_adapter as ha
from app.core.provider.events import (
    AssistantMessage,
    ResultMessage,
    StreamEvent,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from app.core.provider.types import SessionOptions

FIXTURES = Path(__file__).resolve().parents[2] / "fluid" / "tests" / "fixtures"
FAKE_CLI = FIXTURES / "fake_claude_script.py"
TURN_BUDGET = 20.0
FAST_RETRY = RetryPolicy(max_retries=2, base_delay=0.01, max_delay=0.05, jitter=0)


def _load_fake_server():
    name = "fake_provider_server"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, FIXTURES / "fake_provider_server.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


fps = _load_fake_server()
Chat, Messages = fps.Chat, fps.Messages


@pytest.fixture(autouse=True)
def _isolated_home(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    from app.core import guidance_surface

    monkeypatch.setenv(guidance_surface.GUIDANCE_HOME_ENV, str(home))


@pytest.fixture
def provider():
    server = fps.FakeProviderServer()
    try:
        yield server
    finally:
        server.close()


class PermissionResultAllow:  # the app's callback results, matched by class name
    def __init__(self, updated_input: dict | None = None) -> None:
        self.updated_input = updated_input


class PermissionResultDeny:
    def __init__(self, message: str = "") -> None:
        self.message = message


class NativeRun:
    """One native-leg adapter bound to the fake provider."""

    def __init__(self, monkeypatch, tmp_path: Path, server, *, kind: str = "anthropic",
                 mode: str = "default", can_use_tool=None, **options: Any) -> None:
        self.server = server

        async def _client(leg_kind, options):
            assert leg_kind == kind
            if kind == "anthropic":
                return AnthropicClient(
                    api_key="sk-test", base_url=server.url + "/v1/messages", retry=FAST_RETRY,
                ), None
            return OpenRouterClient(
                api_key="sk-test", base_url=server.url + "/v1/chat/completions",
                retry=FAST_RETRY,
            ), None

        monkeypatch.setattr(ha, "_build_provider_client", _client)
        model = "claude-sonnet-4-5" if kind == "anthropic" else "vendor/fake-model"
        self.options = SessionOptions(
            model=model, cwd=str(tmp_path), permission_mode=mode,
            extra={"fluid_engine": "native", "fluid_provider": kind}, **options,
        )
        self.adapter = ha.HarnessAdapter()
        self.adapter._can_use_tool = can_use_tool

    async def __aenter__(self) -> "NativeRun":
        await self.adapter.connect(self.options)
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.adapter.disconnect()

    async def turn(self, prompt: str, *, on_message=None) -> list:
        async def collect() -> list:
            seen: list = []
            await self.adapter.send_query(prompt)
            async for message in self.adapter.receive_events():
                seen.append(message)
                if on_message is not None:
                    await on_message(message)
                if isinstance(message, ResultMessage):
                    return seen
            raise AssertionError("turn ended without a result")

        return await asyncio.wait_for(collect(), TURN_BUDGET)

    async def stored(self) -> list:
        engine = self.adapter.engine
        return [n.message for n in await engine.store.load_path(engine.session_id)]


def _tool_uses(messages: list) -> list[ToolUseBlock]:
    return [b for m in messages if isinstance(m, AssistantMessage)
            for b in m.content if isinstance(b, ToolUseBlock)]


def _tool_results(messages: list) -> list[ToolResultBlock]:
    return [b for m in messages if isinstance(m, UserMessage) and isinstance(m.content, list)
            for b in m.content if isinstance(b, ToolResultBlock)]


def _text(messages: list) -> str:
    return "".join(b.text for m in messages if isinstance(m, AssistantMessage)
                   for b in m.content if isinstance(b, TextBlock))


def _result_text(block: ToolResultBlock) -> str:
    content = block.content
    if isinstance(content, list):
        return "".join(str(item.get("text", "")) for item in content if isinstance(item, dict))
    return str(content or "")


def _fed_back(server, request_index: int) -> list[dict]:
    """The Anthropic tool_result blocks the engine sent on a later call."""
    body = server.requests[request_index].body
    return [b for m in body["messages"] if m["role"] == "user" and isinstance(m["content"], list)
            for b in m["content"] if b.get("type") == "tool_result"]


def _shape(messages: list) -> list[tuple]:
    """A leg-neutral outline of what the app saw in one turn."""
    out: list[tuple] = []
    for message in messages:
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, ToolUseBlock):
                    out.append(("tool_use", block.name, json.dumps(block.input, sort_keys=True)))
                elif isinstance(block, TextBlock) and block.text:
                    out.append(("text", block.text))
        elif isinstance(message, UserMessage) and isinstance(message.content, list):
            for block in message.content:
                if isinstance(block, ToolResultBlock):
                    out.append(("tool_result", bool(block.is_error)))
        elif isinstance(message, ResultMessage):
            out.append(("result", message.is_error))
    return out


# ─── tool calls through the app ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_tool_call_runs_and_its_result_reaches_the_app_the_model_and_the_store(
    monkeypatch, tmp_path, provider
):
    (tmp_path / "notes.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    path = str(tmp_path / "notes.txt")
    provider.script(
        Messages.tool_calls([{"id": "toolu_r1", "name": "Read", "args": {"file_path": path}}],
                            text="Reading."),
        Messages.text("It lists alpha and beta.", input_tokens=40, output_tokens=8),
    )
    async with NativeRun(monkeypatch, tmp_path, provider) as run:
        messages = await run.turn("what is in notes.txt?")
        stored = await run.stored()

    [use] = _tool_uses(messages)
    assert (use.id, use.name, use.input) == ("toolu_r1", "Read", {"file_path": path})
    [result] = _tool_results(messages)
    assert result.tool_use_id == "toolu_r1" and not result.is_error
    assert "alpha" in _result_text(result) and "beta" in _result_text(result)
    assert _text(messages) == "Reading.It lists alpha and beta."
    final = messages[-1]
    assert not final.is_error
    assert final.usage["input_tokens"] == 50 and final.usage["output_tokens"] == 13
    assert final.total_cost_usd and final.total_cost_usd > 0

    # The model's next call carried the tool's output back under the same id.
    [fed] = _fed_back(provider, 1)
    assert fed["tool_use_id"] == "toolu_r1" and not fed["is_error"]
    assert "alpha" in json.dumps(fed["content"])

    # The fluid store holds user → assistant(tool_use) → user(tool_result) → assistant.
    assert [m.role for m in stored] == ["user", "assistant", "user", "assistant"]
    assert any(isinstance(b, FluidToolUse) and b.id == "toolu_r1" for b in stored[1].content)
    assert any(isinstance(b, FluidToolResult) and b.tool_use_id == "toolu_r1"
               for b in stored[2].content)
    assert [b.text for b in stored[3].content if isinstance(b, Text)] == [
        "It lists alpha and beta."
    ]


@pytest.mark.asyncio
async def test_parallel_calls_keep_their_order_and_ids(monkeypatch, tmp_path, provider):
    for name in ("a.txt", "b.txt"):
        (tmp_path / name).write_text(name, encoding="utf-8")
    calls = [{"id": f"toolu_{n}", "name": "Read", "args": {"file_path": str(tmp_path / n)}}
             for n in ("a.txt", "b.txt")]
    provider.script(Messages.tool_calls(calls), Messages.text("both read"))
    async with NativeRun(monkeypatch, tmp_path, provider) as run:
        messages = await run.turn("read both")

    assert [u.id for u in _tool_uses(messages)] == ["toolu_a.txt", "toolu_b.txt"]
    results = _tool_results(messages)
    assert [r.tool_use_id for r in results] == ["toolu_a.txt", "toolu_b.txt"]
    assert ["a.txt" in _result_text(results[0]), "b.txt" in _result_text(results[1])] == [
        True, True
    ]
    assert [b["tool_use_id"] for b in _fed_back(provider, 1)] == ["toolu_a.txt", "toolu_b.txt"]


@pytest.mark.asyncio
async def test_an_unknown_tool_is_answered_in_context_and_the_turn_goes_on(
    monkeypatch, tmp_path, provider
):
    provider.script(
        Messages.tool_calls([{"id": "toolu_x", "name": "Teleport", "args": {"to": "mars"}}]),
        Messages.text("I cannot do that."),
    )
    async with NativeRun(monkeypatch, tmp_path, provider) as run:
        messages = await run.turn("go to mars")

    [result] = _tool_results(messages)
    assert result.is_error and "Teleport" in _result_text(result)
    assert not messages[-1].is_error
    [fed] = _fed_back(provider, 1)
    assert fed["is_error"] is True


# ─── permissions through the app's callback ─────────────────────────────────


def _write_turn(tmp_path: Path) -> tuple[list, dict]:
    args = {"file_path": str(tmp_path / "out.txt"), "content": "hello"}
    return Messages.tool_calls([{"id": "toolu_w1", "name": "Write", "args": args}]), args


@pytest.mark.asyncio
async def test_ask_mode_puts_a_write_to_the_app_and_runs_it_when_allowed(
    monkeypatch, tmp_path, provider
):
    asked: list = []

    async def can_use_tool(name, tool_input, context):
        asked.append((name, dict(tool_input), context.tool_use_id))
        return PermissionResultAllow()

    reply, args = _write_turn(tmp_path)
    provider.script(reply, Messages.text("written"))
    async with NativeRun(monkeypatch, tmp_path, provider, can_use_tool=can_use_tool) as run:
        messages = await run.turn("write it")

    assert asked == [("Write", args, "toolu_w1")]
    [result] = _tool_results(messages)
    assert not result.is_error
    assert (tmp_path / "out.txt").read_text(encoding="utf-8") == "hello"
    assert not messages[-1].is_error


@pytest.mark.asyncio
async def test_a_denied_write_gives_the_model_the_apps_reason(monkeypatch, tmp_path, provider):
    async def can_use_tool(name, tool_input, context):
        return PermissionResultDeny(message="The user declined this write.")

    reply, _ = _write_turn(tmp_path)
    provider.script(reply, Messages.text("ok, not writing"))
    async with NativeRun(monkeypatch, tmp_path, provider, can_use_tool=can_use_tool) as run:
        messages = await run.turn("write it")

    [result] = _tool_results(messages)
    assert result.is_error and _result_text(result) == "The user declined this write."
    assert not (tmp_path / "out.txt").exists()
    [fed] = _fed_back(provider, 1)
    assert fed["is_error"] is True and "declined" in json.dumps(fed["content"])
    assert not messages[-1].is_error


@pytest.mark.asyncio
async def test_a_permission_nobody_answers_is_denied_after_the_timeout(
    monkeypatch, tmp_path, provider
):
    async def never(name, tool_input, context):
        await asyncio.sleep(3600)

    reply, _ = _write_turn(tmp_path)
    provider.script(reply, Messages.text("gave up"))
    async with NativeRun(monkeypatch, tmp_path, provider, can_use_tool=never) as run:
        run.adapter.engine.permission_timeout = 0.3
        messages = await run.turn("write it")

    [result] = _tool_results(messages)
    assert result.is_error and "timed out" in _result_text(result)
    assert not (tmp_path / "out.txt").exists()
    assert not messages[-1].is_error


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["acceptEdits", "bypassPermissions"])
async def test_approving_modes_run_the_write_without_an_approver(
    monkeypatch, tmp_path, provider, mode
):
    reply, _ = _write_turn(tmp_path)
    provider.script(reply, Messages.text("written"))
    async with NativeRun(monkeypatch, tmp_path, provider, mode=mode) as run:
        messages = await run.turn("write it")

    [result] = _tool_results(messages)
    assert not result.is_error
    assert (tmp_path / "out.txt").read_text(encoding="utf-8") == "hello"


# ─── Stop, errors and usage ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stop_mid_stream_ends_the_turn_as_aborted_and_closes_the_stream(
    monkeypatch, tmp_path, provider
):
    provider.script([
        Messages.start(input_tokens=30),
        Messages.ev("content_block_start", index=0, content_block={"type": "text", "text": ""}),
        Messages.ev("content_block_delta", index=0,
                    delta={"type": "text_delta", "text": "Working on it"}),
        fps.Sleep(30),
    ])
    provider.script(Messages.text("Fresh start."))
    stopped: list = []
    async with NativeRun(
        monkeypatch, tmp_path, provider, include_partial_messages=True
    ) as run:
        async def stop_on_text(message):
            if not stopped and isinstance(message, StreamEvent):
                if (message.event.get("delta") or {}).get("type") == "text_delta":
                    stopped.append(True)
                    await run.adapter.interrupt()

        messages = await run.turn("start", on_message=stop_on_text)
        stored = await run.stored()
        again = await run.turn("next")

    # The same terminal the delegated leg gives a Stop: aborted, not an error,
    # and the prompt the provider already billed is kept.
    final = messages[-1]
    assert stopped
    assert final.terminal_reason == "aborted_streaming" and not final.is_error
    assert final.usage["input_tokens"] == 30 and final.total_cost_usd
    assert [m.role for m in stored] == ["user", "assistant"]
    assert stored[1].text() == "Working on it"
    # The stalled stream was abandoned, and the next turn runs normally.
    assert _text(again) == "Fresh start." and not again[-1].is_error
    assert len(provider.requests) == 2


@pytest.mark.asyncio
async def test_a_refused_request_surfaces_as_an_error_result_after_the_retries(
    monkeypatch, tmp_path, provider
):
    provider.script(*[fps.error_reply(529, "Overloaded") for _ in range(3)])
    async with NativeRun(monkeypatch, tmp_path, provider) as run:
        messages = await run.turn("hello")

    final = messages[-1]
    assert final.is_error
    assert "Overloaded" in json.dumps(final.errors or final.result or "")
    assert len(provider.requests) == 3  # the first try plus FAST_RETRY's two


@pytest.mark.asyncio
async def test_a_stream_cut_after_a_tool_call_is_an_error_and_leaves_no_open_tool(
    monkeypatch, tmp_path, provider
):
    """Regression: an Anthropic body that ended cleanly without message_stop
    was taken as a finished reply, and a tool block that had closed stayed a
    ToolUse with no result."""
    provider.script([
        Messages.start(),
        *Messages.tool_block(0, "toolu_c1", "Read", {"file_path": str(tmp_path / "x")}),
    ])
    async with NativeRun(monkeypatch, tmp_path, provider) as run:
        messages = await run.turn("read x")
        stored = await run.stored()

    final = messages[-1]
    assert final.is_error
    assert "truncated" in json.dumps(final.errors or final.result or "")
    uses, results = _tool_uses(messages), _tool_results(messages)
    assert [u.id for u in uses] == [r.tool_use_id for r in results] == ["toolu_c1"]
    assert results[0].is_error
    # The store matches what the app showed: the closed call, marked as part
    # of an interrupted reply, answered by its error result — never a call
    # left without one, and never a finished reply.
    assert [m.role for m in stored] == ["user", "assistant", "user"]
    assert stored[1].provider_meta == {"interrupted": "provider_error"}
    assert [b.id for b in stored[1].content if isinstance(b, FluidToolUse)] == ["toolu_c1"]
    [stored_result] = stored[2].content
    assert isinstance(stored_result, FluidToolResult) and stored_result.is_error


@pytest.mark.asyncio
async def test_chat_completions_tool_loop_through_the_app(monkeypatch, tmp_path, provider):
    (tmp_path / "c.txt").write_text("gamma", encoding="utf-8")
    provider.script(
        Chat.tool_calls([{"id": "call_1", "name": "Read",
                          "args": {"file_path": str(tmp_path / "c.txt")}}], pieces=4),
        Chat.text("It says gamma.", prompt=30, completion=4),
    )
    async with NativeRun(monkeypatch, tmp_path, provider, kind="openrouter") as run:
        messages = await run.turn("read c")

    [use] = _tool_uses(messages)
    assert (use.id, use.name) == ("call_1", "Read")
    [result] = _tool_results(messages)
    assert "gamma" in _result_text(result) and not result.is_error
    tool_msgs = [m for m in provider.requests[1].body["messages"] if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in tool_msgs] == ["call_1"]
    final = messages[-1]
    assert not final.is_error
    assert final.usage["input_tokens"] == 40 and final.usage["output_tokens"] == 9


# ─── the same scenario on both legs ─────────────────────────────────────────


def _install_fake_cli(monkeypatch) -> None:
    """Make the scripted fake CLI the ``claude`` the adapter runs.

    The resolver is patched instead of putting a shim on PATH: on Windows
    ``shutil.which`` skips a file without a PATHEXT extension, and the search
    then reached the machine's real ``claude.exe``.
    """
    command = [sys.executable, str(FAKE_CLI)]
    monkeypatch.setattr(ha, "_resolve_claude_cmd", lambda **_: list(command))
    assert ha._resolve_claude_cmd() == command, "the adapter must run the scripted fake"


@pytest.mark.asyncio
async def test_an_approved_write_has_the_same_shape_on_both_legs(
    monkeypatch, tmp_path, provider
):
    args = {"file_path": str(tmp_path / "out.txt"), "content": "hello"}

    async def allow(name, tool_input, context):
        return PermissionResultAllow()

    # Native: the fake provider asks for the Write, fluid runs it.
    provider.script(
        Messages.tool_calls([{"id": "toolu_w1", "name": "Write", "args": args}]),
        Messages.text("Done."),
    )
    async with NativeRun(monkeypatch, tmp_path, provider, can_use_tool=allow) as run:
        native = await run.turn("write it")

    # Delegated: the fake CLI asks for the same Write and reports it ran.
    _install_fake_cli(monkeypatch)
    script = tmp_path / "script.json"
    script.write_text(json.dumps({"turns": [[
        {"op": "tool_use", "id": "toolu_w1", "name": "Write", "input": args},
        {"op": "ask", "id": "cli_req_1", "tool_use_id": "toolu_w1", "name": "Write",
         "input": args},
        {"op": "tool_result", "tool_use_id": "toolu_w1", "content": "File written"},
        {"op": "text", "text": "Done."},
        {"op": "result", "num_turns": 2, "cost": 0.01,
         "usage": {"input_tokens": 20, "output_tokens": 10,
                   "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}},
    ]]}), encoding="utf-8")
    options = SessionOptions(
        model="fable", cwd=str(tmp_path), permission_mode="default",
        system_prompt={"type": "preset", "preset": "claude_code", "append": "APPEND"},
        env={"FAKE_CLAUDE_SCRIPT": str(script),
             "FAKE_CLAUDE_RECORD": str(tmp_path / "record.jsonl")},
        extra={"fluid_registry": ToolRegistry()},
    )
    adapter = ha.HarnessAdapter()
    adapter._can_use_tool = allow
    await adapter.connect(options)
    try:
        await adapter.send_query("write it")
        delegated: list = []

        async def collect() -> None:
            async for message in adapter.receive_events():
                delegated.append(message)
                if isinstance(message, ResultMessage):
                    return

        await asyncio.wait_for(collect(), TURN_BUDGET)
    finally:
        await adapter.disconnect()

    expected = [
        ("tool_use", "Write", json.dumps(args, sort_keys=True)),
        ("tool_result", False),
        ("text", "Done."),
        ("result", False),
    ]
    assert _shape(native) == expected
    assert _shape(delegated) == expected


# ─── provider refusals reach the app with their HTTP status ─────────────────


async def _chat_turn_outcome(tmp_path: Path, result: ResultMessage) -> dict:
    """What the app's chat-turn path does with one terminal ResultMessage:
    the frames the client sees, the metadata persisted for the turn (which the
    account-failover seam reads) and the notifications emitted."""
    from contextlib import ExitStack
    from unittest.mock import AsyncMock, patch

    import app.core.query_engine as qe
    from app.core import account_failover
    from tests.core.engine_harness import collect
    from tests.core.query_engine_flow_harness import engine_patches, frames_of, fresh_adapter

    emit = AsyncMock()
    with ExitStack() as stack:
        bounds = engine_patches(stack, cwd=str(tmp_path), adapter=fresh_adapter([result]))
        stack.enter_context(
            patch("app.services.notification_service.emit_notification", new=emit))
        events = await collect(qe._stream_to_websocket_impl(
            "hi", "native-429", "claude-code", api_user_id="u-1"))
    metadata = bounds.persist.await_args.kwargs["metadata"]

    notices = AsyncMock()
    with patch("app.services.notification_service.emit_notification", new=notices):
        if account_failover.is_usage_limit_error(metadata):
            await account_failover.attempt_failover("native-429", "u-1")
    return {
        "api_error_frames": frames_of(events, "api_error"),
        "api_error_status": metadata.get("api_error_status"),
        "generic_notifications": emit.await_count,
        "rate_limited_notices": [c.kwargs["message"] for c in notices.await_args_list],
    }


@pytest.mark.asyncio
async def test_a_native_429_reaches_the_app_like_the_cli_legs_429(
    monkeypatch, tmp_path, provider
):
    """Regression: a native provider's 429 surfaced as body text only; the
    ResultMessage had no ``api_error_status``, so the app's rate-limited
    notice never fired for native sessions."""
    provider.script(*[fps.error_reply(429, "Rate limit reached for requests",
                                      retry_after="0") for _ in range(3)])
    async with NativeRun(monkeypatch, tmp_path, provider) as run:
        messages = await run.turn("hi")

    native = messages[-1]
    assert isinstance(native, ResultMessage) and native.is_error
    assert native.api_error_status == 429
    assert native.errors == ["provider returned HTTP 429: Rate limit reached for requests"]
    assert len(provider.requests) == 3  # retried on retry-after first

    # The CLI leg's terminal result for the same refusal.
    cli = ResultMessage(subtype="success", is_error=True, api_error_status=429)
    native_outcome = await _chat_turn_outcome(tmp_path, native)
    cli_outcome = await _chat_turn_outcome(tmp_path, cli)

    assert native_outcome == cli_outcome
    assert native_outcome["api_error_status"] == 429
    assert native_outcome["api_error_frames"] == [
        {"type": "api_error", "status": 429, "session_id": "native-429"}]
    assert native_outcome["generic_notifications"] == 0  # the 429 notice owns it
    [notice] = native_outcome["rate_limited_notices"]
    assert notice.startswith("Usage limit reached")


@pytest.mark.asyncio
async def test_a_native_billing_429_is_not_a_usage_limit(monkeypatch, tmp_path, provider):
    """Regression (R10-12): OpenAI's ``insufficient_quota`` arrives as HTTP 429.
    It was retried three times and reached the app as a 429, which fired the
    "Usage limit reached — wait for the window" notice for an account that is
    simply out of credit."""
    body = {"error": {"message": "You exceeded your current quota, please check your "
                                 "plan and billing details.",
                      "type": "insufficient_quota", "code": "insufficient_quota"}}
    provider.script(*[fps.error_reply(429, body=body, retry_after="0") for _ in range(3)])
    async with NativeRun(monkeypatch, tmp_path, provider, kind="openrouter") as run:
        messages = await run.turn("hi")

    native = messages[-1]
    assert isinstance(native, ResultMessage) and native.is_error
    assert native.api_error_status == 402
    [error] = native.errors
    assert error.startswith("provider returned HTTP 402: Out of credit or quota")
    assert "You exceeded your current quota" in error
    assert len(provider.requests) == 1  # not retried

    outcome = await _chat_turn_outcome(tmp_path, native)
    assert outcome["rate_limited_notices"] == []
    assert outcome["api_error_status"] == 402


async def _resume(run: NativeRun) -> list:
    async def collect() -> list:
        seen: list = []
        await run.adapter.engine.wait_turn()  # the failed turn's task settles
        await run.adapter.send_resume()
        async for message in run.adapter.receive_events():
            seen.append(message)
            if isinstance(message, ResultMessage):
                return seen
        raise AssertionError("resume ended without a result")

    return await asyncio.wait_for(collect(), TURN_BUDGET)


@pytest.mark.asyncio
async def test_retry_after_a_reply_cut_off_by_a_provider_error_answers_the_prompt(
    monkeypatch, tmp_path, provider
):
    """Regression (R10-11): the partial reply kept after a retriable in-stream
    error sat at the head, so the app's Retry (``send_resume``) ended at once
    with "nothing to resume" instead of asking the provider again."""
    provider.script(
        [Messages.start(), *Messages.text_block(0, "Half an ans"),
         Messages.error("overloaded_error", "Overloaded")],
        Messages.text("The full answer"),
    )
    async with NativeRun(monkeypatch, tmp_path, provider) as run:
        failed = await run.turn("question")
        retried = await _resume(run)
        stored = await run.stored()

    assert failed[-1].is_error and failed[-1].api_error_status == 529
    result = retried[-1]
    assert isinstance(result, ResultMessage) and not result.is_error
    assert _text(retried) == "The full answer"
    assert [m.role for m in stored] == ["user", "assistant"]
    assert stored[0].text().startswith("question")
    assert stored[1].text() == "The full answer"
    assert len(provider.requests) == 2
    assert provider.requests[1].body == provider.requests[0].body
