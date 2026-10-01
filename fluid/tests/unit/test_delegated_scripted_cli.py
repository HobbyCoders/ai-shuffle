"""End-to-end tests of the delegated (``claude`` CLI) leg against a scripted CLI.

Each test writes the exact stream-json frames the fake CLI plays
(``fixtures/fake_claude_script.py``) and asserts on both sides of the pipe:
the fluid events and store nodes the loop produced, and what the CLI read on
its stdin (permission answers, interrupts, control requests), its argv and its
environment. Hermetic: a Python child process, no network.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Awaitable, Callable

import pytest

import fluid.engine.delegated as delegated_mod
from fluid.engine.delegated import DelegatedLoop
from fluid.events import (
    ErrorEvent,
    PermissionRequest,
    RespondPermission,
    TextDelta,
    ToolResult,
    ToolUse,
    TurnComplete,
    Usage,
    UserTurn,
)
from fluid.events.model import Interrupted, RateLimit
from fluid.events.ops import Interrupt, SetModel, SetPermissionMode
from fluid.store.base import MemoryStore

FAKE = Path(__file__).parent.parent / "fixtures" / "fake_claude_script.py"
TURN_BUDGET = 20.0
_TERMINALS = (TurnComplete, Interrupted, ErrorEvent)

Responder = Callable[[DelegatedLoop, PermissionRequest], Awaitable[None]]


class Scripted:
    """A DelegatedLoop wired to the scripted fake CLI, plus its record file."""

    def __init__(self, tmp_path: Path, script: dict, **loop_kwargs: Any) -> None:
        self.script_path = tmp_path / "script.json"
        self.script_path.write_text(json.dumps(script), encoding="utf-8")
        self.record_path = tmp_path / "record.jsonl"
        env = {
            "FAKE_CLAUDE_SCRIPT": str(self.script_path),
            "FAKE_CLAUDE_RECORD": str(self.record_path),
            **loop_kwargs.pop("env", {}),
        }
        self.store = MemoryStore()
        self.loop = DelegatedLoop(
            store=self.store, model="claude-fake",
            claude_cmd=[sys.executable, str(FAKE)], env=env, **loop_kwargs,
        )
        self.sid: str | None = None

    def records(self, kind: str) -> list[dict]:
        if not self.record_path.exists():
            return []
        rows = [json.loads(line) for line in self.record_path.read_text().splitlines() if line]
        return [r for r in rows if r["kind"] == kind]

    def stdin_frames(self, ftype: str, subtype: str | None = None) -> list[dict]:
        frames = [r["frame"] for r in self.records("stdin") if r["frame"].get("type") == ftype]
        if subtype is not None:
            frames = [f for f in frames if (f.get("request") or {}).get("subtype") == subtype]
        return frames

    async def turn(
        self, text: str, *, on_permission: Responder | None = None,
        on_event: Callable[[Any], Awaitable[None]] | None = None,
    ) -> list:
        """Run one user turn; the events it produced, terminal last."""
        if self.sid is None:
            self.sid = (await self.store.create_session()).session_id
            self.loop._session_id = self.sid
        start = self.loop.bus.last_seq(self.sid)
        seen: list = []

        async def watch() -> None:
            async for event in self.loop.bus.subscribe(self.sid, since_seq=start):
                seen.append(event)
                if isinstance(event, PermissionRequest) and on_permission is not None:
                    await on_permission(self.loop, event)
                if on_event is not None:
                    await on_event(event)
                if isinstance(event, _TERMINALS):
                    return

        watcher = asyncio.create_task(watch())
        await asyncio.sleep(0)  # subscribed before the turn can publish
        await self.loop.submit(UserTurn(content=text))
        await asyncio.wait_for(watcher, TURN_BUDGET)
        await asyncio.wait_for(self.loop.wait_turn(), TURN_BUDGET)
        return seen

    async def aclose(self) -> None:
        await self.loop.aclose()


def _answer(decision: str, **kwargs: Any) -> Responder:
    async def respond(loop: DelegatedLoop, event: PermissionRequest) -> None:
        await loop.submit(RespondPermission(request_id=event.request_id, decision=decision, **kwargs))

    return respond


def _of(events: list, cls: type) -> list:
    return [e for e in events if isinstance(e, cls)]


def _tool_turn(tid: str = "toolu_1", name: str = "Bash", args: dict | None = None,
               *, ask: bool = True, **ask_extra: Any) -> list[dict]:
    args = args if args is not None else {"command": "ls"}
    steps: list[dict] = [{"op": "tool_use", "id": tid, "name": name, "input": args,
                          "usage": {"input_tokens": 100, "output_tokens": 20,
                                    "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}]
    if ask:
        steps.append({"op": "ask", "id": f"cli_req_{tid}", "tool_use_id": tid, "name": name,
                      "input": args, **ask_extra})
    steps += [
        {"op": "tool_result", "tool_use_id": tid, "content": "a.txt\nb.txt"},
        {"op": "text", "text": "Listed the files.",
         "usage": {"input_tokens": 130, "output_tokens": 8,
                   "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}},
        {"op": "result", "num_turns": 2, "cost": 0.0123,
         "usage": {"input_tokens": 230, "output_tokens": 28,
                   "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}},
    ]
    return steps


# ─── A tool call, end to end ────────────────────────────────────────────────


async def test_a_tool_call_surfaces_with_its_name_input_and_result(tmp_path) -> None:
    run = Scripted(tmp_path, {"turns": [_tool_turn(ask=False)]})
    try:
        events = await run.turn("list the files")
        [use] = _of(events, ToolUse)
        assert (use.name, use.input, use.tool_use_id) == ("Bash", {"command": "ls"}, "toolu_1")
        [result] = _of(events, ToolResult)
        assert (result.name, result.output, result.is_error) == ("Bash", "a.txt\nb.txt", False)
        assert result.tool_use_id == "toolu_1"
        assert isinstance(events[-1], TurnComplete)
        assert "".join(e.text for e in _of(events, TextDelta)) == "Listed the files."
        # Order: the call, then its result, then the answer text, then the end.
        kinds = [type(e).__name__ for e in events if isinstance(e, (ToolUse, ToolResult, TurnComplete))]
        assert kinds == ["ToolUse", "ToolResult", "TurnComplete"]

        path = await run.store.load_path(run.sid)
        assert [n.message.role for n in path] == ["user", "assistant", "user", "assistant"]
        # The CLI got exactly the user's words as its turn.
        [user] = run.stdin_frames("user")
        assert user["message"]["content"] == [{"type": "text", "text": "list the files"}]
    finally:
        await run.aclose()


# ─── Permission prompts (Ask mode) ──────────────────────────────────────────


async def test_an_allowed_prompt_runs_the_call_with_the_rewritten_input(tmp_path) -> None:
    steps = _tool_turn()
    steps[2] = {"op": "tool_result", "tool_use_id": "toolu_1", "echo_input": True}
    run = Scripted(tmp_path, {"turns": [steps]})
    try:
        events = await run.turn(
            "list", on_permission=_answer("allow", updated_input={"command": "ls -la"}),
        )
        [prompt] = _of(events, PermissionRequest)
        assert (prompt.name, prompt.input, prompt.tool_use_id) == ("Bash", {"command": "ls"}, "toolu_1")
        [decision] = run.records("decision")
        assert decision["response"] == {"behavior": "allow", "updatedInput": {"command": "ls -la"}}
        [result] = _of(events, ToolResult)
        assert not result.is_error and json.loads(result.output) == {"command": "ls -la"}
        assert isinstance(events[-1], TurnComplete)
    finally:
        await run.aclose()


async def test_a_denied_prompt_reaches_the_model_as_the_denial_text(tmp_path) -> None:
    run = Scripted(tmp_path, {"turns": [_tool_turn()]})
    try:
        events = await run.turn("list", on_permission=_answer("deny", message="not today"))
        [decision] = run.records("decision")
        assert decision["response"] == {"behavior": "deny", "message": "not today"}
        [result] = _of(events, ToolResult)
        assert result.is_error and result.output == "not today"
        assert isinstance(events[-1], TurnComplete)
    finally:
        await run.aclose()


async def test_allow_always_is_echoed_and_answers_the_next_prompt_itself(tmp_path) -> None:
    suggestion = [{"type": "addRules", "behavior": "allow", "destination": "session",
                   "rules": [{"toolName": "WebFetch"}]}]
    first = _tool_turn("toolu_1", "WebFetch", {"url": "https://a.test"}, suggestions=suggestion)
    second = _tool_turn("toolu_2", "WebFetch", {"url": "https://b.test"})
    run = Scripted(tmp_path, {"turns": [first, second]})
    prompts: list = []

    async def respond(loop, event):
        prompts.append(event)
        await _answer("allow_always")(loop, event)

    try:
        await run.turn("fetch a", on_permission=respond)
        events = await run.turn("fetch b", on_permission=respond)
        # The second prompt never reached the host: the loop answered it.
        assert len(prompts) == 1 and not _of(events, PermissionRequest)
        first_answer, second_answer = [d["response"] for d in run.records("decision")]
        assert first_answer["updatedPermissions"] == suggestion
        assert second_answer["behavior"] == "allow"
        assert not _of(events, ToolResult)[0].is_error
    finally:
        await run.aclose()


async def test_an_unanswered_prompt_is_denied_after_the_timeout(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(delegated_mod, "PERMISSION_TIMEOUT", 0.3)
    run = Scripted(tmp_path, {"turns": [_tool_turn()]})
    try:
        events = await run.turn("list")  # nobody answers
        [decision] = run.records("decision")
        assert decision["response"]["behavior"] == "deny"
        assert "timed out" in decision["response"]["message"]
        [result] = _of(events, ToolResult)
        assert result.is_error and "timed out" in result.output
        assert isinstance(events[-1], TurnComplete)
    finally:
        await run.aclose()


# ─── Pre-approved tools (Auto / accept-edits modes, grants) ─────────────────


async def test_granted_tools_and_accept_edits_run_without_a_prompt(tmp_path) -> None:
    bridged = "mcp__fluid_bundled__mcp__builtins__Recall"
    turn = [
        {"op": "tool_use", "id": "t1", "name": bridged, "input": {"query": "x"}},
        {"op": "ask", "id": "r1", "tool_use_id": "t1", "name": bridged, "input": {"query": "x"},
         "skip_if_allowed": True},
        {"op": "tool_result", "tool_use_id": "t1", "content": "recalled"},
        {"op": "tool_use", "id": "t2", "name": "Edit", "input": {"file_path": "a.txt"}},
        {"op": "ask", "id": "r2", "tool_use_id": "t2", "name": "Edit",
         "input": {"file_path": "a.txt"}, "skip_if_allowed": True},
        {"op": "tool_result", "tool_use_id": "t2", "content": "edited"},
        {"op": "text", "text": "done"},
        {"op": "result"},
    ]
    run = Scripted(tmp_path, {"turns": [turn]}, permission_mode="acceptEdits",
                   permission_grants=[bridged])
    try:
        events = await run.turn("go")
        assert not _of(events, PermissionRequest)
        assert [r.output for r in _of(events, ToolResult)] == ["recalled", "edited"]
        [start] = run.records("start")
        argv = start["argv"]
        assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"
        assert argv[argv.index("--allowed-tools") + 1] == bridged
        assert argv[argv.index("--permission-prompt-tool") + 1] == "stdio"
        assert all(d["response"].get("by") == "cli_rules" for d in run.records("decision"))

        # A mode switch mid-session reaches the running CLI.
        await run.loop.submit(SetPermissionMode(mode="bypassPermissions"))
        await run.turn("again")
        [switch] = run.stdin_frames("control_request", "set_permission_mode")
        assert switch["request"]["mode"] == "bypassPermissions"
    finally:
        await run.aclose()


# ─── Stop / cancel ──────────────────────────────────────────────────────────


def _interruptible_turn(result_subtype: str) -> list[dict]:
    return [
        {"op": "stream_text", "text": "Working on it", "chunks": 2},
        {"op": "wait_interrupt"},
        {"op": "frame", "frame": {"type": "user", "message": {"role": "user", "content": [
            {"type": "text", "text": "[Request interrupted by user]"}]}}},
        {"op": "result", "subtype": result_subtype, "cost": 0.004, "num_turns": 1,
         "usage": {"input_tokens": 9, "output_tokens": 3,
                   "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}},
    ]


async def _interrupt_on_first_text(run: Scripted):
    async def on_event(event):
        if isinstance(event, TextDelta):
            await run.loop.submit(Interrupt())
    return on_event


@pytest.mark.parametrize("subtype", ["success", "error_during_execution"])
async def test_stop_mid_turn_ends_it_as_interrupted_with_its_cost(tmp_path, subtype) -> None:
    """The CLI answers an interrupt with a result frame; whichever subtype it
    reports, a turn the user stopped ends as Interrupted, never as an error."""
    run = Scripted(tmp_path, {"turns": [_interruptible_turn(subtype)]}, partial=True)
    try:
        events = await run.turn("long job", on_event=await _interrupt_on_first_text(run))
        assert run.stdin_frames("control_request", "interrupt")
        assert not _of(events, ErrorEvent)
        terminal = events[-1]
        assert isinstance(terminal, Interrupted)
        assert terminal.cost_usd == pytest.approx(0.004)
        # The session still works afterwards.
        after = await run.turn("next")
        assert isinstance(after[-1], TurnComplete)
    finally:
        await run.aclose()


async def test_stop_on_the_first_turn_of_a_resumed_cli_does_not_replay_it(tmp_path) -> None:
    """An interrupted first turn on a ``--resume`` process that maps no frame
    is not a rejected resume handle: the handle stays and the turn is not
    re-sent to a fresh CLI."""
    turn = [{"op": "wait_interrupt"},
            {"op": "result", "subtype": "error_during_execution", "cost": 0.0}]
    run = Scripted(tmp_path, {"turns": [turn]}, cli_session_id="cli-prev")

    async def stop_at_start(event):
        if type(event).__name__ == "TurnStarted":
            await asyncio.sleep(0.2)  # the user frame is on its way
            await run.loop.submit(Interrupt())

    try:
        events = await run.turn("hello again", on_event=stop_at_start)
        assert isinstance(events[-1], Interrupted), events[-1]
        assert len(run.records("start")) == 1  # no respawn
        assert len(run.stdin_frames("user")) == 1  # no replay
        assert run.loop.cli_session_id == "cli-prev"
    finally:
        await run.aclose()


async def test_stop_before_the_turn_reaches_the_cli_keeps_it_from_running(tmp_path) -> None:
    """A Stop that lands while the CLI is still starting must not let the
    turn run its tools once the CLI is up (the native loop honours an early
    interrupt the same way)."""
    run = Scripted(tmp_path, {"turns": [_tool_turn(ask=False)]})
    try:
        run.sid = (await run.store.create_session()).session_id
        run.loop._session_id = run.sid
        await run.loop.submit(UserTurn(content="do the thing"))
        await run.loop.submit(Interrupt())  # before the task spawned the CLI
        await asyncio.wait_for(run.loop.wait_turn(), TURN_BUDGET)
        events = run.loop.bus.log(run.sid)
        assert isinstance(events[-1], Interrupted), events[-1]
        assert not _of(events, ToolUse) and not _of(events, ToolResult)
        assert not run.stdin_frames("user")
        # The next turn runs normally.
        after = await run.turn("now go")
        assert isinstance(after[-1], TurnComplete)
    finally:
        await run.aclose()


async def test_stop_while_a_prompt_is_open_denies_it_and_ends_the_turn(tmp_path) -> None:
    turn = [*_tool_turn()[:3], {"op": "wait_interrupt"}, {"op": "result"}]
    run = Scripted(tmp_path, {"turns": [turn]})

    async def stop_instead(loop, event):
        await loop.submit(Interrupt())

    try:
        events = await run.turn("list", on_permission=stop_instead)
        [decision] = run.records("decision")
        assert decision["response"] == {
            "behavior": "deny", "message": "interrupted while awaiting permission",
        }
        assert isinstance(events[-1], Interrupted)
    finally:
        await run.aclose()


# ─── Between turns ──────────────────────────────────────────────────────────


async def test_a_wake_between_turns_is_drained_and_its_prompt_denied(tmp_path) -> None:
    first = [
        {"op": "text", "text": "first answer"}, {"op": "result"},
        # A turn the CLI starts on its own after the result, asking permission.
        {"op": "sleep", "s": 0.05},
        {"op": "tool_use", "id": "wake_t", "name": "Bash", "input": {"command": "rm -rf x"}},
        {"op": "ask", "id": "wake_r", "tool_use_id": "wake_t", "name": "Bash",
         "input": {"command": "rm -rf x"}},
        {"op": "tool_result", "tool_use_id": "wake_t"},
        {"op": "text", "text": "WAKE text"}, {"op": "result"},
    ]
    second = [{"op": "text", "text": "second answer"}, {"op": "result"}]
    run = Scripted(tmp_path, {"turns": [first, second]})
    try:
        await run.turn("one")
        await asyncio.sleep(0.3)
        events = await run.turn("two")
        assert "".join(e.text for e in _of(events, TextDelta)) == "second answer"
        assert not _of(events, PermissionRequest) and not _of(events, ToolUse)
        [decision] = run.records("decision")
        assert decision["response"]["behavior"] == "deny"
        assert "between turns" in decision["response"]["message"]
        path = await run.store.load_path(run.sid)
        assert all("WAKE" not in n.message.text() for n in path)
    finally:
        await run.aclose()


# ─── A CLI that fails ───────────────────────────────────────────────────────


class _NotYetReaped:
    """A CLI process that has exited but whose exit the child watcher has not
    recorded yet: ``returncode`` still reads None, as it does for a moment
    after every crash (and for as long as a CLI that closed stdout lingers)."""

    returncode = None

    def __init__(self, proc: Any) -> None:
        self._proc = proc

    def __getattr__(self, name: str) -> Any:
        return getattr(self._proc, name)


async def test_a_cli_crash_mid_turn_errors_it_and_the_next_turn_respawns(tmp_path) -> None:
    script = {
        "turns": [[{"op": "text", "text": "partial"}, {"op": "exit", "code": 3}]],
        # The respawned CLI resumes the conversation.
        "resume_turns": [[{"op": "text", "text": "back again"}, {"op": "result"}]],
    }
    run = Scripted(tmp_path, script)
    try:
        events = await run.turn("one")
        assert isinstance(events[-1], ErrorEvent)
        assert "closed before result" in events[-1].message
        # The next turn may start before the crashed process is reaped.
        run.loop._proc = _NotYetReaped(run.loop._proc)
        events = await run.turn("two")
        assert isinstance(events[-1], TurnComplete), events[-1]
        assert "".join(e.text for e in _of(events, TextDelta)) == "back again"
        starts = run.records("start")
        assert len(starts) == 2 and "--resume" in starts[1]["argv"]
    finally:
        await run.aclose()


async def test_a_cli_that_died_between_turns_behind_buffered_lines_is_respawned(tmp_path) -> None:
    """R8-11: stdout still held lines ahead of EOF, so the dead CLI counted as
    reusable; the between-turn drain read them, hit EOF, and the turn then
    wrote into the closed pipe and failed instead of resuming."""
    script = {
        "turns": [[{"op": "text", "text": "first"}, {"op": "result"},
                   {"op": "raw", "line": "stray output after the result"},
                   {"op": "exit", "code": 0}]],
        "resume_turns": [[{"op": "text", "text": "resumed"}, {"op": "result"}]],
    }
    run = Scripted(tmp_path, script)
    try:
        events = await run.turn("one")
        assert isinstance(events[-1], TurnComplete), events[-1]
        proc = run.loop._proc
        for _ in range(200):  # the stray line and EOF are both buffered
            if proc.stdout._eof:
                break
            await asyncio.sleep(0.01)
        assert proc.stdout._eof and not proc.stdout.at_eof()
        # Exited, but not reaped yet: returncode still reads None.
        run.loop._proc = _NotYetReaped(proc)
        events = await run.turn("two")
        assert isinstance(events[-1], TurnComplete), events[-1]
        assert "".join(e.text for e in _of(events, TextDelta)) == "resumed"
        starts = run.records("start")
        assert len(starts) == 2 and "--resume" in starts[1]["argv"]
    finally:
        await run.aclose()


async def test_a_cli_that_exits_at_start_fails_the_turn_instead_of_hanging(tmp_path) -> None:
    run = Scripted(tmp_path, {"spawn_exit": {"code": 1, "stderr": "Invalid API key"}})
    try:
        events = await run.turn("hi")
        assert isinstance(events[-1], ErrorEvent)
    finally:
        await run.aclose()


async def test_a_failed_result_is_an_error_that_still_bills_the_turn(tmp_path) -> None:
    turn = [{"op": "text", "text": "trying"},
            {"op": "result", "subtype": "error_during_execution", "errors": ["tool crashed"],
             "cost": 0.02, "usage": {"input_tokens": 40, "output_tokens": 4}}]
    run = Scripted(tmp_path, {"turns": [turn]})
    try:
        events = await run.turn("hi")
        error = events[-1]
        assert isinstance(error, ErrorEvent) and "tool crashed" in error.message
        assert error.cost_usd == pytest.approx(0.02)
        assert error.usage["input_tokens"] == 40
        assert error.num_model_calls == 1
    finally:
        await run.aclose()


async def test_malformed_and_foreign_stdout_lines_are_skipped(tmp_path) -> None:
    turn = [
        {"op": "raw", "line": "Warning: something the CLI printed"},
        {"op": "raw", "line": "{not json"},
        {"op": "raw", "line": "[1, 2]"},
        {"op": "raw", "line": "42"},
        {"op": "raw", "line": "null"},
        {"op": "frame", "frame": {"type": "assistant", "message": None}},
        {"op": "frame", "frame": {"type": "user", "message": "x"}},
        {"op": "frame", "frame": {"type": "brand_new_frame", "x": 1}},
        {"op": "text", "text": "still fine"},
        {"op": "result"},
    ]
    run = Scripted(tmp_path, {"turns": [turn]})
    try:
        events = await run.turn("hi")
        assert not _of(events, ErrorEvent), _of(events, ErrorEvent)
        assert isinstance(events[-1], TurnComplete)
        assert "".join(e.text for e in _of(events, TextDelta)) == "still fine"
    finally:
        await run.aclose()


# ─── Usage ──────────────────────────────────────────────────────────────────


async def test_usage_is_reported_once_per_model_call_with_the_clis_totals(tmp_path) -> None:
    call_1 = {"input_tokens": 100, "output_tokens": 20,
              "cache_read_input_tokens": 5000, "cache_creation_input_tokens": 300}
    call_2 = {"input_tokens": 30, "output_tokens": 12,
              "cache_read_input_tokens": 5300, "cache_creation_input_tokens": 0}
    turn = [
        # One model message split into two per-block frames repeating its usage.
        {"op": "thinking", "text": "plan", "msg_id": "m1", "usage": call_1},
        {"op": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "a"},
         "msg_id": "m1", "usage": call_1},
        {"op": "serve", "subtype": "get_context_usage", "count": 1, "timeout": 5},
        {"op": "tool_result", "tool_use_id": "t1", "content": "contents"},
        {"op": "text", "text": "done", "msg_id": "m2", "usage": call_2},
        {"op": "serve", "subtype": "get_context_usage", "count": 2, "timeout": 5},
        {"op": "result", "num_turns": 2, "cost": 0.0456,
         "usage": {"input_tokens": 130, "output_tokens": 32,
                   "cache_read_input_tokens": 10300, "cache_creation_input_tokens": 300}},
    ]
    run = Scripted(tmp_path, {"turns": [turn]})
    try:
        events = await run.turn("hi")
        usages = _of(events, Usage)
        calls = [u for u in usages if u.input_tokens or u.output_tokens]
        snapshots = [u for u in usages if u.context_usage]
        assert [(u.input_tokens, u.output_tokens, u.cache_read_tokens, u.cache_creation_tokens)
                for u in calls] == [(100, 20, 5000, 300), (30, 12, 5300, 0)]
        assert [u.raw["model_call_index"] for u in calls] == [1, 2]
        # The context breakdown after each call, carrying that call's index.
        assert [u.raw.get("model_call_index") for u in snapshots] == [1, 2]
        assert all(u.context_usage["totalTokens"] == 12000 for u in snapshots)
        done = events[-1]
        assert isinstance(done, TurnComplete)
        assert done.cost_usd == pytest.approx(0.0456)
        assert done.num_model_calls == 2
        assert done.usage["cache_read_input_tokens"] == 10300
    finally:
        await run.aclose()


def _se(event: dict) -> dict:
    return {"op": "frame", "frame": {"type": "stream_event", "uuid": "u", "event": event}}


def _block_frame(msg_id: str, block: dict, usage: dict, stop: str | None) -> dict:
    return {"op": "frame", "frame": {"type": "assistant", "message": {
        "id": msg_id, "model": "claude-fake", "role": "assistant", "content": [block],
        "usage": usage, "stop_reason": stop,
    }}}


async def test_usage_takes_each_calls_output_from_its_message_delta(tmp_path) -> None:
    """What CLI 2.1.285 really sends: every per-block assistant frame repeats
    the message_start usage (output a few tokens); the final output count
    arrives only on the partial stream's message_delta."""
    start_1 = {"input_tokens": 100, "output_tokens": 2,
               "cache_read_input_tokens": 5000, "cache_creation_input_tokens": 300}
    start_2 = {"input_tokens": 30, "output_tokens": 1,
               "cache_read_input_tokens": 5300, "cache_creation_input_tokens": 0}
    text = {"type": "text", "text": "reading"}
    tool = {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "a"}}
    turn = [
        _se({"type": "message_start", "message": {"id": "m1", "usage": start_1}}),
        _se({"type": "content_block_start", "index": 0, "content_block": {"type": "text"}}),
        _se({"type": "content_block_delta", "index": 0,
             "delta": {"type": "text_delta", "text": "reading"}}),
        _se({"type": "content_block_stop", "index": 0}),
        _block_frame("m1", text, start_1, None),
        _se({"type": "content_block_start", "index": 1, "content_block": {
            "type": "tool_use", "id": "t1", "name": "Read"}}),
        _se({"type": "content_block_delta", "index": 1, "delta": {
            "type": "input_json_delta", "partial_json": '{"file_path": "a"}'}}),
        _se({"type": "content_block_stop", "index": 1}),
        _block_frame("m1", tool, start_1, None),
        _se({"type": "message_delta", "delta": {"stop_reason": "tool_use"},
             "usage": {"output_tokens": 20}}),
        _se({"type": "message_stop"}),
        {"op": "serve", "subtype": "get_context_usage", "count": 1, "timeout": 5},
        {"op": "tool_result", "tool_use_id": "t1", "content": "contents"},
        _se({"type": "message_start", "message": {"id": "m2", "usage": start_2}}),
        _se({"type": "content_block_start", "index": 0, "content_block": {"type": "text"}}),
        _se({"type": "content_block_delta", "index": 0,
             "delta": {"type": "text_delta", "text": "done"}}),
        _se({"type": "content_block_stop", "index": 0}),
        _block_frame("m2", {"type": "text", "text": "done"}, start_2, None),
        _se({"type": "message_delta", "delta": {"stop_reason": "end_turn"},
             "usage": {"output_tokens": 12}}),
        _se({"type": "message_stop"}),
        {"op": "serve", "subtype": "get_context_usage", "count": 2, "timeout": 5},
        {"op": "result", "num_turns": 2, "cost": 0.0456,
         "usage": {"input_tokens": 130, "output_tokens": 32,
                   "cache_read_input_tokens": 10300, "cache_creation_input_tokens": 300}},
    ]
    run = Scripted(tmp_path, {"turns": [turn]}, partial=True)
    try:
        events = await run.turn("hi")
        usages = _of(events, Usage)
        calls = [u for u in usages if not u.context_usage]
        assert [(u.input_tokens, u.output_tokens, u.cache_read_tokens, u.cache_creation_tokens)
                for u in calls] == [(100, 20, 5000, 300), (30, 12, 5300, 0)]
        assert [u.raw["model_call_index"] for u in calls] == [1, 2]
        # Each call's context snapshot follows that call's own record, so a
        # host never counts a snapshot as a call of its own.
        order = [(u.raw.get("model_call_index"), bool(u.context_usage)) for u in usages]
        assert order == [(1, False), (1, True), (2, False), (2, True)]
        [use] = _of(events, ToolUse)
        assert use.input == {"file_path": "a"}
        done = events[-1]
        assert isinstance(done, TurnComplete) and done.num_model_calls == 2
    finally:
        await run.aclose()


async def test_multi_turn_cost_is_the_increase_of_the_clis_total(tmp_path) -> None:
    """The fake's default result reports the same total (0.001) every turn,
    as a real CLI process would if later turns cost nothing."""
    run = Scripted(tmp_path, {})
    try:
        first = (await run.turn("one"))[-1]
        second = (await run.turn("two"))[-1]
        assert first.cost_usd == pytest.approx(0.001)
        assert second.cost_usd == pytest.approx(0.0)
        assert second.usage["cli_total_cost_usd"] == pytest.approx(0.001)
    finally:
        await run.aclose()


# ─── The CLI's environment ──────────────────────────────────────────────────


async def test_the_cli_child_gets_a_scrubbed_environment(tmp_path, monkeypatch) -> None:
    for name, value in {
        "OWNER_PASSWORD": "hunter2", "DATABASE_URL": "postgres://x", "PGPASSWORD": "pg",
        "SHUFFLE_DESKTOP_LIFECYCLE_TOKEN": "t", "MY_SERVICE_TOKEN": "s",
        "ANTHROPIC_API_KEY": "sk-host", "ANTHROPIC_AUTH_TOKEN": "host-token",
        "HARMLESS_SETTING": "kept",
    }.items():
        monkeypatch.setenv(name, value)
    run = Scripted(tmp_path, {}, env={
        # What inject_claude_code_auth hands over in subscription mode.
        "FLUID_CHILD_ENV_UNSET": "ANTHROPIC_API_KEY,ANTHROPIC_AUTH_TOKEN",
        "FAL_KEY": "fal-on-purpose",
    })
    try:
        await run.turn("hi")
        [start] = run.records("start")
        env = start["env"]
        for secret in ("OWNER_PASSWORD", "DATABASE_URL", "PGPASSWORD",
                       "SHUFFLE_DESKTOP_LIFECYCLE_TOKEN", "MY_SERVICE_TOKEN",
                       "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "FLUID_CHILD_ENV_UNSET"):
            assert secret not in env, secret
        assert env["HARMLESS_SETTING"] == "kept"
        assert env["FAL_KEY"] == "fal-on-purpose"
        assert env["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"] == "1"
    finally:
        await run.aclose()


async def test_rejected_rate_limit_without_utilization_reaches_the_bus(tmp_path) -> None:
    """The 2.1.x CLI can report a window ``rejected`` with no utilization; the
    verdict is the only sign the window is exhausted, so it must not be lost."""
    turn = [
        {"op": "frame", "frame": {"type": "rate_limit_event", "rate_limit_info": {
            "status": "rejected", "rateLimitType": "five_hour", "resetsAt": 1900000000,
        }}},
        {"op": "text", "text": "limited"},
        {"op": "result"},
    ]
    run = Scripted(tmp_path, {"turns": [turn]})
    try:
        events = await run.turn("hi")
        assert isinstance(events[-1], TurnComplete)
        (limit,) = _of(events, RateLimit)
        assert (limit.kind, limit.status, limit.utilization, limit.resets_at) == (
            "five_hour", "rejected", None, 1900000000,
        )
    finally:
        await run.aclose()


# ─── Stop while the loop waits on the CLI ───────────────────────────────────


def _user_texts(run: Scripted) -> list[str]:
    return [f["message"]["content"][0]["text"] for f in run.stdin_frames("user")]


async def _stop_after(run: Scripted, delay: float) -> None:
    await asyncio.sleep(delay)
    await run.loop.submit(Interrupt())


async def test_stop_during_the_between_turn_drain_ends_the_turn_at_once(tmp_path) -> None:
    """A wake turn in flight holds the next turn in the drain (up to 30 s). A
    Stop must end that wait at once, with the turn Interrupted and its message
    never sent, instead of sitting out the wake."""
    wake_s = 2.0
    first = [
        {"op": "text", "text": "first answer"}, {"op": "result"},
        # A turn the CLI starts on its own: its first frame arms the drain wait.
        {"op": "sleep", "s": 0.05},
        {"op": "text", "text": "WAKE started"},
        {"op": "sleep", "s": wake_s},
        {"op": "text", "text": "WAKE text"}, {"op": "result"},
    ]
    second = [{"op": "text", "text": "second answer"}, {"op": "result"}]
    run = Scripted(tmp_path, {"turns": [first, second]})
    try:
        await run.turn("one")
        await asyncio.sleep(0.3)  # the wake's first frame is out
        clock = asyncio.get_running_loop()
        started = clock.time()
        stopper = asyncio.create_task(_stop_after(run, 0.1))
        events = await run.turn("two")
        elapsed = clock.time() - started
        await stopper
        assert isinstance(events[-1], Interrupted), events[-1]
        assert elapsed < 1.0, elapsed
        assert not _of(events, TextDelta)
        # R11-3: the Stop ended the drain, not the wake. The next turn, sent
        # while the wake is still running, waits for the wake's result instead
        # of writing into it and taking "WAKE text" as its answer.
        events = await run.turn("three")
        assert isinstance(events[-1], TurnComplete), events[-1]
        assert "".join(e.text for e in _of(events, TextDelta)) == "second answer"
        assert _user_texts(run) == ["one", "three"]  # "two" never reached the CLI
    finally:
        await run.aclose()


async def test_a_wake_that_starts_during_a_between_turn_switch_is_waited_for(tmp_path) -> None:
    """R12-1: a wake whose first frames the control pump reads (a model switch
    or a context-usage capture between turns) is still owed its result. The
    next turn waits for it instead of writing into it and taking "WAKE text"
    as its answer."""
    first = [
        {"op": "text", "text": "first answer"}, {"op": "result"},
        {"op": "sleep", "s": 0.05},
        {"op": "text", "text": "WAKE started"},
        # The switch is answered mid-wake: the pump reads "WAKE started".
        {"op": "serve", "subtype": "set_model", "count": 1, "timeout": 3},
        {"op": "sleep", "s": 1.5},
        {"op": "text", "text": "WAKE text"}, {"op": "result"},
    ]
    second = [{"op": "text", "text": "second answer"}, {"op": "result"}]
    run = Scripted(tmp_path, {"turns": [first, second]})
    try:
        await run.turn("one")
        await asyncio.sleep(0.3)  # the wake's first frame is out
        await run.loop.submit(SetModel(model="claude-other"))
        assert run.loop.model == "claude-other"
        assert run.loop._wake_in_flight
        events = await run.turn("two")
        assert isinstance(events[-1], TurnComplete), events[-1]
        assert "".join(e.text for e in _of(events, TextDelta)) == "second answer"
        assert _user_texts(run) == ["one", "two"]
    finally:
        await run.aclose()


async def test_stop_during_a_setting_acknowledgement_wait_keeps_the_old_value(
    tmp_path, monkeypatch,
) -> None:
    """A switch the CLI has not acknowledged yet waits up to CONTROL_TIMEOUT
    (5 s); a Stop ends that wait at once and the switch is not recorded. A
    Stop left over from before does not cut the next switch short.

    R11-4: the Stop still ends the turn the switch was preparing, and since
    the frame reached the CLI its model is unknown: the next turn re-sends the
    recorded one, and a CLI that never answers is restarted on it."""
    monkeypatch.setattr(delegated_mod, "CONTROL_TIMEOUT", 0.5)
    run = Scripted(tmp_path, {"controls": {"set_model": "mute"}})
    try:
        await run.turn("hi")
        clock = asyncio.get_running_loop()
        started = clock.time()
        stopper = asyncio.create_task(_stop_after(run, 0.1))
        with pytest.raises(RuntimeError, match="stopped before .* set_model"):
            await run.loop.submit(SetModel(model="claude-other"))
        elapsed = clock.time() - started
        await stopper
        assert elapsed < 1.0, elapsed
        assert run.loop.model == "claude-fake"
        assert not run.loop._pending_controls
        # The earlier Stop is spent for switches: an acknowledged one records.
        await run.loop.submit(SetPermissionMode(mode="acceptEdits"))
        assert run.loop.permission_mode == "acceptEdits"
        # The turn the stopped switch was preparing ends Interrupted, unsent.
        events = await run.turn("two")
        assert isinstance(events[-1], Interrupted), events[-1]
        assert not _of(events, TextDelta)
        # The next turn re-sends the model; unanswered, the CLI is restarted
        # on the recorded values and the session still takes turns.
        events = await run.turn("three")
        assert isinstance(events[-1], TurnComplete), events[-1]
        sent = [f["request"]["model"] for f in run.stdin_frames("control_request", "set_model")]
        assert sent == ["claude-other", "claude-fake"]
        starts = run.records("start")
        assert len(starts) == 2, starts
        argv = starts[1]["argv"]
        assert argv[argv.index("--model") + 1] == "claude-fake"
        assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"
        assert _user_texts(run) == ["hi", "three"]
    finally:
        await run.aclose()


def _record_order(run: Scripted) -> list[tuple[str, Any]]:
    """The fake's applied switches and received user texts, in order."""
    rows = [json.loads(line) for line in run.record_path.read_text().splitlines() if line]
    order: list[tuple[str, Any]] = []
    for row in rows:
        if row["kind"] == "applied" and row["subtype"] == "set_model":
            order.append(("model", row["value"]))
        elif row["kind"] == "stdin" and row["frame"].get("type") == "user":
            order.append(("user", row["frame"]["message"]["content"][0]["text"]))
    return order


@pytest.mark.parametrize("how", ["stop", "timeout"])
async def test_a_switch_the_cli_applies_after_the_wait_ended_is_undone_before_the_next_turn(
    tmp_path, monkeypatch, how,
) -> None:
    """R11-4: the CLI applies a set_model whose wait a Stop or the deadline
    already ended, and its late reply is dropped. The loop keeps the old model,
    so the next turn re-sends it before the user frame: the CLI and the
    recorded model agree again. A Stop also ends the turn it was preparing."""
    monkeypatch.setattr(delegated_mod, "CONTROL_TIMEOUT", 0.3)
    run = Scripted(tmp_path, {"controls": {"set_model": {"late": 0.6}}})
    try:
        await run.turn("one")
        stopper = asyncio.create_task(_stop_after(run, 0.1)) if how == "stop" else None
        with pytest.raises(RuntimeError, match="set_model"):
            await run.loop.submit(SetModel(model="claude-other"))
        if stopper is not None:
            await stopper
        assert run.loop.model == "claude-fake"
        if how == "stop":
            events = await run.turn("two")
            assert isinstance(events[-1], Interrupted), events[-1]
        # The CLI applies the switch after all (its reply finds no one waiting).
        for _ in range(500):
            if run.records("applied"):
                break
            await asyncio.sleep(0.01)
        assert run.records("applied")[0]["value"] == "claude-other"
        events = await run.turn("three" if how == "stop" else "two")
        assert isinstance(events[-1], TurnComplete), events[-1]
        last = "three" if how == "stop" else "two"
        assert _record_order(run) == [
            ("user", "one"), ("model", "claude-other"), ("model", "claude-fake"), ("user", last),
        ]
        assert len(run.records("start")) == 1  # re-sent, not restarted
        assert not run.loop._unconfirmed_settings
    finally:
        await run.aclose()


async def test_stop_while_a_dead_cli_is_respawned_does_not_start_the_turn(tmp_path) -> None:
    """R8-11 with a Stop: the CLI died between turns behind buffered lines, so
    the next turn respawns it. A Stop landing during that respawn must keep
    the turn off the new CLI; the turn after it runs there normally."""
    script = {
        "turns": [[{"op": "text", "text": "first"}, {"op": "result"},
                   {"op": "raw", "line": "stray output after the result"},
                   {"op": "exit", "code": 0}]],
        "resume_turns": [[{"op": "text", "text": "resumed"}, {"op": "result"}]],
    }
    run = Scripted(tmp_path, script)
    try:
        events = await run.turn("one")
        assert isinstance(events[-1], TurnComplete), events[-1]
        proc = run.loop._proc
        for _ in range(200):  # the stray line and EOF are both buffered
            if proc.stdout._eof:
                break
            await asyncio.sleep(0.01)
        run.loop._proc = _NotYetReaped(proc)

        respawning = asyncio.Event()
        original_guard = run.loop._version_guard

        async def slow_guard() -> None:
            respawning.set()
            await asyncio.sleep(0.3)
            await original_guard()

        async def stop_on_respawn() -> None:
            await respawning.wait()
            await run.loop.submit(Interrupt())

        run.loop._version_guard = slow_guard
        stopper = asyncio.create_task(stop_on_respawn())
        events = await run.turn("two")
        await stopper
        run.loop._version_guard = original_guard
        assert isinstance(events[-1], Interrupted), events[-1]
        assert not _of(events, TextDelta)

        events = await run.turn("three")
        assert isinstance(events[-1], TurnComplete), events[-1]
        assert "".join(e.text for e in _of(events, TextDelta)) == "resumed"
        starts = run.records("start")
        assert len(starts) == 2 and "--resume" in starts[1]["argv"]
        assert _user_texts(run) == ["one", "three"]  # "two" reached neither CLI
    finally:
        await run.aclose()
