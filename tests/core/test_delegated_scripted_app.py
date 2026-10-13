"""The delegated (Claude subscription) leg end to end, through the app adapter.

A scripted fake ``claude`` (``fluid/tests/fixtures/fake_claude_script.py``) is
what the adapter's CLI resolver returns, so the real
``HarnessAdapter`` → ``DelegatedLoop`` → stream-json pipe and the adapter's
translation into the app's provider events all run. Each test asserts on what
the app sees (tool calls, results, the permission callback, the terminal
ResultMessage and the usage the app books) and on what the CLI saw (argv,
environment, the answers written to its stdin). Hermetic: no network.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest
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

FAKE = (
    Path(__file__).resolve().parents[2]
    / "fluid" / "tests" / "fixtures" / "fake_claude_script.py"
)
TURN_BUDGET = 20.0
BRIDGED_RECALL = "mcp__fluid_bundled__mcp__builtins__Recall"
BRIDGED_REMEMBER = "mcp__fluid_bundled__mcp__builtins__Remember"


@pytest.fixture(autouse=True)
def _isolated_home(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    from app.core import guidance_surface

    monkeypatch.setenv(guidance_surface.GUIDANCE_HOME_ENV, str(home))


def _use_fake_cli(monkeypatch) -> list:
    """Make the scripted fake the ``claude`` the adapter runs.

    The adapter's resolver is patched to run the fake with this interpreter
    rather than relying on a shim on PATH: on Windows ``shutil.which`` skips a
    file without a PATHEXT extension, so a shim was passed over and the search
    reached the machine's real ``claude.exe``, which billed the subscription.
    PATH discovery itself is covered by ``test_claude_cmd_resolution.py``.
    """
    command = [sys.executable, str(FAKE)]
    monkeypatch.setattr(ha, "_resolve_claude_cmd", lambda **_: list(command))
    assert ha._resolve_claude_cmd() == command, "the adapter must run the scripted fake"
    return command


@pytest.fixture(autouse=True)
def _claude_on_path(monkeypatch):
    return _use_fake_cli(monkeypatch)


class PermissionResultAllow:  # the app's callback results, matched by class name
    def __init__(self, updated_input: dict | None = None) -> None:
        self.updated_input = updated_input


class PermissionResultDeny:
    def __init__(self, message: str = "") -> None:
        self.message = message


class Run:
    def __init__(self, tmp_path: Path, script: dict, *, mode: str = "default",
                 env: dict | None = None, **options: Any) -> None:
        self.script_path = tmp_path / "script.json"
        self.script_path.write_text(json.dumps(script), encoding="utf-8")
        self.record_path = tmp_path / "record.jsonl"
        self.options = SessionOptions(
            model="fable",
            system_prompt={"type": "preset", "preset": "claude_code", "append": "APPEND"},
            cwd=str(tmp_path),
            permission_mode=mode,
            env={
                "FAKE_CLAUDE_SCRIPT": str(self.script_path),
                "FAKE_CLAUDE_RECORD": str(self.record_path),
                **(env or {}),
            },
            extra={"fluid_registry": ToolRegistry()},
            **options,
        )
        self.adapter = ha.HarnessAdapter()

    async def __aenter__(self) -> "Run":
        await self.adapter.connect(self.options)
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.adapter.disconnect()

    def records(self, kind: str) -> list[dict]:
        if not self.record_path.exists():
            return []
        rows = [json.loads(line) for line in self.record_path.read_text().splitlines() if line]
        return [r for r in rows if r["kind"] == kind]

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


def _bash_turn(args: dict | None = None) -> list[dict]:
    args = args or {"command": "git status"}
    return [
        {"op": "tool_use", "id": "toolu_1", "name": "Bash", "input": args},
        {"op": "ask", "id": "cli_req_1", "tool_use_id": "toolu_1", "name": "Bash", "input": args},
        {"op": "tool_result", "tool_use_id": "toolu_1", "content": "On branch main"},
        {"op": "text", "text": "The tree is clean."},
        {"op": "result", "num_turns": 2, "cost": 0.0321,
         "usage": {"input_tokens": 50, "output_tokens": 10,
                   "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}},
    ]


# ─── A tool call through the app's permission callback (Ask mode) ───────────


@pytest.mark.asyncio
async def test_an_approved_tool_call_reaches_the_app_transcript(tmp_path):
    asked: list = []

    async def can_use_tool(name, tool_input, context):
        asked.append((name, dict(tool_input), context.tool_use_id))
        return PermissionResultAllow()

    run = Run(tmp_path, {"turns": [_bash_turn()]})
    run.adapter._can_use_tool = can_use_tool
    async with run:
        messages = await run.turn("status?")

    assert asked == [("Bash", {"command": "git status"}, "toolu_1")]
    [use] = _tool_uses(messages)
    assert (use.id, use.name, use.input) == ("toolu_1", "Bash", {"command": "git status"})
    [result] = _tool_results(messages)
    assert result.tool_use_id == "toolu_1" and not result.is_error
    assert _result_text(result) == "On branch main"
    assert _text(messages) == "The tree is clean."
    final = messages[-1]
    assert not final.is_error and final.total_cost_usd == pytest.approx(0.0321)
    [decision] = run.records("decision")
    assert decision["response"]["behavior"] == "allow"


@pytest.mark.asyncio
async def test_a_denied_tool_call_gives_the_model_the_apps_reason(tmp_path):
    async def can_use_tool(name, tool_input, context):
        return PermissionResultDeny(message="The user declined this command.")

    run = Run(tmp_path, {"turns": [_bash_turn()]})
    run.adapter._can_use_tool = can_use_tool
    async with run:
        messages = await run.turn("status?")

    [result] = _tool_results(messages)
    assert result.is_error and _result_text(result) == "The user declined this command."
    assert not messages[-1].is_error
    [decision] = run.records("decision")
    assert decision["response"] == {"behavior": "deny", "message": "The user declined this command."}


@pytest.mark.asyncio
async def test_an_approver_that_fails_denies_instead_of_hanging(tmp_path):
    async def can_use_tool(name, tool_input, context):
        raise RuntimeError("approval service down")

    run = Run(tmp_path, {"turns": [_bash_turn()]})
    run.adapter._can_use_tool = can_use_tool
    async with run:
        messages = await run.turn("status?")
    [result] = _tool_results(messages)
    assert result.is_error
    assert run.records("decision")[0]["response"]["behavior"] == "deny"


# ─── Pre-approved tools per permission mode ─────────────────────────────────


def _granted_turn() -> list[dict]:
    steps: list[dict] = []
    for tid, name, args in (
        ("t1", BRIDGED_RECALL, {"query": "deploy steps"}),
        ("t2", BRIDGED_REMEMBER, {"fact": "x"}),
        ("t3", "Edit", {"file_path": "a.txt", "old_string": "a", "new_string": "b"}),
    ):
        steps += [
            {"op": "tool_use", "id": tid, "name": name, "input": args},
            {"op": "ask", "id": f"r_{tid}", "tool_use_id": tid, "name": name, "input": args,
             "skip_if_allowed": True},
            {"op": "tool_result", "tool_use_id": tid, "content": f"{tid} ran"},
        ]
    return [*steps, {"op": "text", "text": "done"}, {"op": "result"}]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode, prompted, denied", [
    # Ask: the safe app tool is granted; the write tool and Edit prompt.
    ("default", ["Remember", "Edit"], {"t2", "t3"}),
    # Accept edits: the CLI runs Edit itself; Remember still prompts.
    ("acceptEdits", ["Remember"], set()),
    # Auto: the CLI's classifier mode; what reaches the app is approved.
    ("auto", ["Remember", "Edit"], set()),
    ("bypassPermissions", [], set()),
])
async def test_the_permission_mode_and_grants_decide_what_prompts(tmp_path, mode, prompted, denied):
    asked: list = []

    async def can_use_tool(name, tool_input, context):
        asked.append(name.rsplit("__", 1)[-1])
        if mode == "default":
            return PermissionResultDeny(message="no")
        return PermissionResultAllow()

    run = Run(
        tmp_path, {"turns": [_granted_turn()]}, mode=mode,
        mcp_servers={"builtins": {"type": "sdk", "name": "builtins", "instance": object()}},
        allowed_tools=["mcp__builtins__Recall", "mcp__builtins__Remember"],
    )
    run.adapter._can_use_tool = can_use_tool
    async with run:
        messages = await run.turn("go")

    assert asked == prompted
    results = {b.tool_use_id: b for b in _tool_results(messages)}
    assert {tid for tid, b in results.items() if b.is_error} == denied
    [start] = run.records("start")
    argv = start["argv"]
    assert argv[argv.index("--permission-mode") + 1] == mode
    grants = argv[argv.index("--allowed-tools") + 1].split(",")
    # Remember is a write tool: never granted. Of the CLI's own tools only
    # read-only search and WebSearch are; never Edit, Write, Bash or Read.
    assert grants[0] == BRIDGED_RECALL and BRIDGED_REMEMBER not in grants
    assert {"Glob", "Grep", "WebSearch"} <= set(grants)
    assert not {"Edit", "Write", "Bash", "PowerShell", "Read", "NotebookEdit"} & set(grants)


# ─── Stop ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("subtype", ["success", "error_during_execution"])
async def test_stop_mid_turn_ends_with_an_aborted_result_that_keeps_the_cost(tmp_path, subtype):
    turn = [
        {"op": "stream_text", "text": "Working on it", "chunks": 2},
        {"op": "wait_interrupt"},
        {"op": "frame", "frame": {"type": "user", "message": {"role": "user", "content": [
            {"type": "text", "text": "[Request interrupted by user]"}]}}},
        {"op": "result", "subtype": subtype, "cost": 0.004, "num_turns": 1,
         "usage": {"input_tokens": 9, "output_tokens": 3,
                   "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}},
    ]
    run = Run(tmp_path, {"turns": [turn]}, include_partial_messages=True)
    stopped: list = []

    async def stop_on_text(message):
        if isinstance(message, StreamEvent) and not stopped:
            if (message.event.get("delta") or {}).get("type") == "text_delta":
                stopped.append(True)
                await run.adapter.interrupt()

    async with run:
        messages = await run.turn("long job", on_message=stop_on_text)
        again = await run.turn("next")

    final = messages[-1]
    assert stopped
    assert final.terminal_reason == "aborted_streaming" and not final.is_error
    assert final.total_cost_usd == pytest.approx(0.004)
    assert final.num_turns == 1
    assert not again[-1].is_error


# ─── A refused call's HTTP status ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_failed_results_http_status_reaches_the_apps_result(tmp_path):
    """A lapsed Claude Code login ends the turn with ``api_error_status`` 401.

    The status travels from the CLI's result frame through fluid's ErrorEvent
    to the ResultMessage the query loops read, which is what raises the
    "sign in to Claude Code again" notice.
    """
    turn = [
        {"op": "frame", "frame": {
            "type": "result", "subtype": "error_during_execution", "is_error": True,
            "api_error_status": 401, "num_turns": 1, "total_cost_usd": 0.0,
            "duration_ms": 5, "duration_api_ms": 3,
            "errors": ["Invalid API key · Please run /login"],
            "usage": {"input_tokens": 0, "output_tokens": 0,
                      "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
        }},
    ]
    run = Run(tmp_path, {"turns": [turn]})
    async with run:
        messages = await run.turn("hello")

    final = messages[-1]
    assert isinstance(final, ResultMessage)
    assert final.is_error
    assert final.api_error_status == 401


# ─── Usage the app books for the turn ───────────────────────────────────────


@pytest.mark.asyncio
async def test_the_turns_usage_reaches_what_the_app_books(tmp_path):
    from app.core.query_persistence import _turn_end_booking
    from app.core.query_streaming import is_context_snapshot_only

    call_1 = {"input_tokens": 120, "output_tokens": 30,
              "cache_read_input_tokens": 4000, "cache_creation_input_tokens": 500}
    call_2 = {"input_tokens": 40, "output_tokens": 15,
              "cache_read_input_tokens": 4500, "cache_creation_input_tokens": 0}
    turn = [
        {"op": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "a"},
         "usage": call_1},
        {"op": "serve", "subtype": "get_context_usage", "count": 1, "timeout": 5},
        {"op": "tool_result", "tool_use_id": "t1", "content": "text"},
        {"op": "text", "text": "summary", "usage": call_2},
        {"op": "serve", "subtype": "get_context_usage", "count": 2, "timeout": 5},
        {"op": "result", "num_turns": 2, "cost": 0.0789,
         "usage": {"input_tokens": 160, "output_tokens": 45,
                   "cache_read_input_tokens": 8500, "cache_creation_input_tokens": 500}},
    ]
    run = Run(tmp_path, {"turns": [turn]})
    async with run:
        messages = await run.turn("read it")

    # The query loop's fold (query_engine.execute_query): per-call records set
    # the context-window figures, the terminal sets the turn's totals.
    metadata: dict = {}
    per_call = [m.usage for m in messages if isinstance(m, AssistantMessage) and m.usage]
    snapshots = [u for u in per_call if is_context_snapshot_only(u)]
    for usage in per_call:
        if not is_context_snapshot_only(usage):
            metadata["last_call_tokens_in"] = usage.get("input_tokens", 0)
            metadata["last_call_cache_read_tokens"] = usage.get("cache_read_input_tokens", 0)
    final = messages[-1]
    metadata.update(
        num_turns=final.num_turns, total_cost_usd=final.total_cost_usd, is_error=final.is_error,
        tokens_in=final.usage["input_tokens"], tokens_out=final.usage["output_tokens"],
        cache_read_tokens=final.usage["cache_read_input_tokens"],
        cache_creation_tokens=final.usage["cache_creation_input_tokens"],
    )
    booking = await _turn_end_booking("s", "k", metadata, interrupted=False)

    assert len(per_call) - len(snapshots) == 2
    assert [s["context_usage"]["totalTokens"] for s in snapshots] == [12000, 12000]
    assert metadata["last_call_tokens_in"] == 40
    assert metadata["last_call_cache_read_tokens"] == 4500
    assert booking["model_calls"] == 2
    assert booking["cost_usd"] == pytest.approx(0.0789)
    assert (booking["tokens_in"], booking["tokens_out"]) == (160, 45)
    assert booking["cache_read_tokens"] == 8500
    assert booking["turn_increment"] == 1


# ─── The CLI's environment on the subscription ──────────────────────────────


@pytest.mark.asyncio
async def test_the_subscription_cli_never_sees_a_host_api_key_or_host_secrets(
    tmp_path, monkeypatch,
):
    from app.core import claude_auth_mode
    from app.core.environment import inject_claude_code_auth

    monkeypatch.setattr(claude_auth_mode, "current_mode", lambda: claude_auth_mode.MODE_SUBSCRIPTION)
    for name, value in {
        "ANTHROPIC_API_KEY": "sk-host", "ANTHROPIC_AUTH_TOKEN": "host-token",
        "OWNER_PASSWORD": "pw", "DATABASE_URL": "postgres://x", "CLUSTER_SECRET": "c",
    }.items():
        monkeypatch.setenv(name, value)
    env = inject_claude_code_auth({"ANTHROPIC_API_KEY": "sk-profile"})
    run = Run(tmp_path, {}, env=env)
    async with run:
        final = (await run.turn("hi"))[-1]
    assert not final.is_error
    [start] = run.records("start")
    child = start["env"]
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OWNER_PASSWORD",
                 "DATABASE_URL", "CLUSTER_SECRET", "FLUID_CHILD_ENV_UNSET"):
        assert name not in child, name
    assert child["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"] == "1"
    # The bridge token rides the MCP config's env, never the argv.
    assert not any("token" in arg.lower() for arg in start["argv"])
