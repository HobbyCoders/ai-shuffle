"""Round-6 audit follow-ups: the items the fix PR flagged as not done.

* Edit's read gate now asks WHICH LINES were read, not just whether the file
  was: ``_ = Read(path, limit=5)`` no longer unlocks an edit at line 400.
* ``NativeLoop`` reads every catalog through the wired-capability predicate,
  so a deferred tool whose host injection is missing is neither searchable
  nor billed.
* The compiled environment section is the fixed half only; the date and git
  state ride each turn as a reminder, so a new day no longer rewrites the
  cached prompt prefix.
* ``RespondQuestion(user_answered=True)`` lands ``USER_ANSWERED_KEY`` in the
  tool's answers, which is what lets AskUser credit a person.
"""

from __future__ import annotations

import asyncio
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from fluid.context.compiler import PromptInputs, compile_prompt
from fluid.context.environment import environment_block, environment_note
from fluid.engine.native import NativeLoop
from fluid.events import RespondQuestion
from fluid.providers.base import ProviderCaps, ProviderRequest, StreamChunk
from fluid.store.base import MemoryStore
from fluid.tools import read_window
from fluid.tools.builtins import register_builtins
from fluid.tools.builtins.edit import handler as edit_handler
USER_ANSWERED_KEY = "__user_answered__"
from fluid.tools.builtins.read import handler as read_handler
from fluid.tools.builtins.write import handler as write_handler
from fluid.tools.registry import (
    InputRequired,
    ToolCtx,
    ToolOutcome,
    ToolRegistry,
    ToolSpec,
    _Entry,
)



class _IdleProvider:
    name = "anthropic"
    caps = ProviderCaps()

    async def stream(self, request: ProviderRequest) -> AsyncIterator[StreamChunk]:
        if False:  # pragma: no cover — never called by these tests
            yield StreamChunk("message_stop", {})


def _loop(**kw: Any) -> NativeLoop:
    return NativeLoop(
        provider=_IdleProvider(), store=MemoryStore(), model="m",
        **kw,
    )


def _ctx(tmp_path: Path) -> ToolCtx:
    return ToolCtx(cwd=str(tmp_path), session_id="s1")


def _numbered(n: int) -> str:
    return "".join(f"line {i}\n" for i in range(1, n + 1))


# ─── Edit: read-window coverage ──────────────────────────────────────────────


async def test_a_short_read_no_longer_unlocks_the_whole_file(tmp_path: Path) -> None:
    path = tmp_path / "big.txt"
    path.write_text(_numbered(300), encoding="utf-8")
    ctx = _ctx(tmp_path)
    await read_handler({"file_path": str(path), "limit": 5}, ctx)
    assert read_window.windows_of(ctx, str(path)) == [(1, 5)]

    outcome = await edit_handler(
        {"file_path": str(path), "old_string": "line 250\n", "new_string": "LINE 250\n"},
        ctx,
    )
    assert outcome.is_error
    assert "line 250 of" in outcome.text
    assert "lines 1-5" in outcome.text
    assert "offset=245" in outcome.text
    assert path.read_text(encoding="utf-8") == _numbered(300), "nothing written"


async def test_an_edit_inside_the_read_window_goes_through(tmp_path: Path) -> None:
    path = tmp_path / "big.txt"
    path.write_text(_numbered(300), encoding="utf-8")
    ctx = _ctx(tmp_path)
    await read_handler({"file_path": str(path), "offset": 240, "limit": 20}, ctx)
    assert read_window.windows_of(ctx, str(path)) == [(240, 259)]

    outcome = await edit_handler(
        {"file_path": str(path), "old_string": "line 250\n", "new_string": "LINE 250\n"},
        ctx,
    )
    assert not outcome.is_error, outcome.text
    assert "LINE 250" in path.read_text(encoding="utf-8")


async def test_a_read_that_reaches_the_end_owns_the_rest_of_the_file(tmp_path: Path) -> None:
    path = tmp_path / "small.txt"
    path.write_text(_numbered(40), encoding="utf-8")
    ctx = _ctx(tmp_path)
    await read_handler({"file_path": str(path)}, ctx)
    assert read_window.windows_of(ctx, str(path)) == [(1, None)]
    outcome = await edit_handler(
        {"file_path": str(path), "old_string": "line 40\n", "new_string": "end\n"}, ctx
    )
    assert not outcome.is_error, outcome.text


async def test_a_write_covers_the_whole_file(tmp_path: Path) -> None:
    path = tmp_path / "new.txt"
    ctx = _ctx(tmp_path)
    await write_handler({"file_path": str(path), "content": _numbered(50)}, ctx)
    assert read_window.windows_of(ctx, str(path)) == [(1, None)]
    outcome = await edit_handler(
        {"file_path": str(path), "old_string": "line 49\n", "new_string": "x\n"}, ctx
    )
    assert not outcome.is_error, outcome.text


async def test_windows_follow_the_lines_an_edit_moves(tmp_path: Path) -> None:
    """Read 1-100, insert 10 lines at 40, edit at old line 95: still covered."""
    path = tmp_path / "big.txt"
    path.write_text(_numbered(300), encoding="utf-8")
    ctx = _ctx(tmp_path)
    await read_handler({"file_path": str(path), "limit": 100}, ctx)
    inserted = "line 40\n" + "".join(f"new {i}\n" for i in range(10))
    outcome = await edit_handler(
        {"file_path": str(path), "old_string": "line 40\n", "new_string": inserted}, ctx
    )
    assert not outcome.is_error, outcome.text
    assert read_window.windows_of(ctx, str(path)) == [(1, 110)]
    # Old line 95 is now line 105 — inside the grown window.
    outcome = await edit_handler(
        {"file_path": str(path), "old_string": "line 95\n", "new_string": "LINE 95\n"}, ctx
    )
    assert not outcome.is_error, outcome.text
    # Old line 101 is now 111 — still unseen, still refused.
    outcome = await edit_handler(
        {"file_path": str(path), "old_string": "line 101\n", "new_string": "no\n"}, ctx
    )
    assert outcome.is_error and "line 111 of" in outcome.text


async def test_a_file_on_the_ledger_without_windows_passes_as_before(tmp_path: Path) -> None:
    """A ledger rehydrated from a history older than windows keeps the old gate."""
    path = tmp_path / "old.txt"
    path.write_text(_numbered(300), encoding="utf-8")
    ctx = _ctx(tmp_path)
    ctx.read_files.add(str(path))
    outcome = await edit_handler(
        {"file_path": str(path), "old_string": "line 250\n", "new_string": "x\n"}, ctx
    )
    assert not outcome.is_error, outcome.text


async def test_replace_all_needs_every_match_in_a_window(tmp_path: Path) -> None:
    path = tmp_path / "dup.txt"
    path.write_text("a\n" + _numbered(200) + "a\n", encoding="utf-8")
    ctx = _ctx(tmp_path)
    await read_handler({"file_path": str(path), "limit": 10}, ctx)
    outcome = await edit_handler(
        {"file_path": str(path), "old_string": "a\n", "new_string": "b\n", "replace_all": True},
        ctx,
    )
    assert outcome.is_error and "line 202 of" in outcome.text


async def test_the_indentation_fallback_is_gated_too(tmp_path: Path) -> None:
    path = tmp_path / "mod.py"
    body = "def f():\n    pass\n\n\n" + "".join(f"x{i} = {i}\n" for i in range(200)) + "def g():\n    return 1\n"
    path.write_text(body, encoding="utf-8")
    ctx = _ctx(tmp_path)
    await read_handler({"file_path": str(path), "limit": 3}, ctx)
    outcome = await edit_handler(
        {"file_path": str(path), "old_string": "  return 1\n", "new_string": "  return 2\n"},
        ctx,
    )
    assert outcome.is_error and "outside the part of the file you have Read" in outcome.text


def test_shift_semantics() -> None:
    ctx = ToolCtx()
    read_window.record(ctx, "f", 1, 50)
    read_window.record(ctx, "f", 80, 100)
    read_window.record(ctx, "f", 200, None)
    # Replace lines 60-61 (2 lines) with 5 lines: +3 below, nothing above.
    read_window.shift(ctx, "f", 60, 2, 5)
    assert read_window.windows_of(ctx, "f") == [(1, 50), (83, 103), (203, None)]
    # Delete lines 10-12 inside the first window: it shrinks, others move up.
    read_window.shift(ctx, "f", 10, 3, 0)
    assert read_window.windows_of(ctx, "f") == [(1, 47), (80, 100), (200, None)]


async def test_replay_rebuilds_windows_from_read_inputs(tmp_path: Path) -> None:
    from fluid.engine.native import _read_ledger_window
    from fluid.messages import ToolUseBlock

    assert _read_ledger_window(
        ToolUseBlock(id="t1", name="Read", input={"file_path": "a", "offset": 40, "limit": 20})
    ) == (40, 59)
    assert _read_ledger_window(
        ToolUseBlock(id="t2", name="Read", input={"file_path": "a"})
    ) == (1, None)
    assert _read_ledger_window(
        ToolUseBlock(id="t3", name="Write", input={"file_path": "a", "content": ""})
    ) is None


# ─── NativeLoop: wired-capability catalogs ───────────────────────────────────


def _registry_with_unwired_deferred() -> ToolRegistry:
    reg = ToolRegistry()
    register_builtins(reg)

    async def _ran(_input: dict, _ctx: ToolCtx) -> ToolOutcome:
        return ToolOutcome(text="ran")

    spec = ToolSpec(
        name="mcp__vault__read_secret", description="read a secret",
        input_schema={"type": "object", "properties": {}}, source="mcp",
        requires_extra=("vault_client",),
    )
    reg._entries[spec.name] = _Entry(spec=spec, server="vault")
    plain = ToolSpec(
        name="mcp__browser__click", description="click",
        input_schema={"type": "object", "properties": {}}, source="mcp",
    )
    reg._entries[plain.name] = _Entry(spec=plain, server="browser")
    return reg


def test_deferred_catalog_drops_tools_whose_capability_is_missing() -> None:
    loop = _loop(registry=_registry_with_unwired_deferred())
    deferred = {t["name"] for t in loop._deferred_tools_api()}
    assert "mcp__browser__click" in deferred
    assert "mcp__vault__read_secret" not in deferred
    # ToolSearch reads the same predicate.
    filt = loop.ctx.extra["tool_filter"]
    assert filt("mcp__browser__click") is True
    assert filt("mcp__vault__read_secret") is False
    assert filt("Read") is True
    # Wire it and it is back everywhere.
    loop.ctx.extra["vault_client"] = object()
    assert filt("mcp__vault__read_secret") is True
    assert "mcp__vault__read_secret" in {t["name"] for t in loop._deferred_tools_api()}


def test_unwired_builtins_are_not_announced_or_billed() -> None:
    reg = ToolRegistry()
    register_builtins(reg)
    loop = _loop(registry=reg)
    # SendMessage needs ``agent_message``; nothing wired it here.
    assert "SendMessage" not in (loop._deferred_catalog_reminder() or "")
    mounted = loop._mounted_tool_names()
    assert mounted is not None
    assert "Read" in mounted
    assert "SendMessage" not in mounted and "ListAgents" not in mounted


# ─── environment: fixed prefix, volatile reminder ────────────────────────────


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "README.md").write_text("hi\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    return repo


def test_static_environment_block_omits_the_volatile_facts(git_repo: Path) -> None:
    block = environment_block(git_repo, include_volatile=False)
    assert "Working directory:" in block
    assert "Git repository: yes" in block
    assert "Today's date:" not in block
    assert "Current branch:" not in block
    assert "Recent commits:" not in block
    full = environment_block(git_repo)
    assert "Today's date:" in full and "Current branch: main" in full


def test_environment_note_carries_the_date_and_git_state(git_repo: Path) -> None:
    note = environment_note(git_repo)
    assert note.startswith("<system-reminder>") and note.endswith("</system-reminder>")
    assert "Today's date:" in note
    assert "Current branch: main" in note
    assert "Working tree: clean" in note
    assert "init" in note  # the recent commit


def test_compiled_environment_section_is_the_fixed_half(git_repo: Path) -> None:
    compiled = compile_prompt(PromptInputs(
        base="b", cwd=str(git_repo), include_env=True,
        include_global_instructions=False, include_project_instructions=False,
        include_skills=False, include_agents=False, include_plugins=False,
    ))
    env = [s for s in compiled.sections if s.kind == "environment"]
    assert len(env) == 1
    assert "Today's date:" not in env[0].text
    assert "Current branch:" not in env[0].text
    assert "Git repository: yes" in env[0].text


async def test_native_loop_reminds_the_volatile_environment_per_turn(git_repo: Path) -> None:
    compiled = compile_prompt(PromptInputs(
        base="b", cwd=str(git_repo), include_env=True,
        include_global_instructions=False, include_project_instructions=False,
        include_skills=False, include_agents=False, include_plugins=False,
    ))
    loop = _loop(system=compiled.text, system_sections=compiled, ctx=ToolCtx(cwd=str(git_repo)))
    note = await loop._environment_reminder()
    assert note is not None and "Today's date:" in note and "Current branch: main" in note


async def test_no_environment_section_means_no_reminder(git_repo: Path) -> None:
    compiled = compile_prompt(PromptInputs(
        base="b", cwd=str(git_repo), include_env=False,
        include_global_instructions=False, include_project_instructions=False,
        include_skills=False, include_agents=False, include_plugins=False,
    ))
    loop = _loop(system=compiled.text, system_sections=compiled, ctx=ToolCtx(cwd=str(git_repo)))
    note = await loop._environment_reminder()
    # Round 9: the runtime facts ride their own block (first turn only), but
    # the volatile date/git note stays tied to the environment section.
    assert note is not None and "Runtime facts" in note
    assert "Today's date:" not in note and "Current branch" not in note
    assert await loop._environment_reminder() is None  # unchanged facts: nothing
    # And a host that passed a bare string (no sections at all) is left alone.
    assert await _loop(system="plain")._environment_reminder() is None


# ─── RespondQuestion.user_answered ───────────────────────────────────────────


async def test_a_user_answered_response_marks_the_answers() -> None:
    loop = _loop()
    emitted: list[Any] = []

    async def _emit(event: Any) -> None:
        emitted.append(event)

    loop._emit = _emit  # type: ignore[method-assign]
    marker = InputRequired(
        server="builtin", tool="AskUser", request_state=None,
        requests={"approve": {"message": "Approve the plan?"}},
    )
    task = asyncio.create_task(
        loop._collect_input_responses("s", "t", "AskUser", "tu1", marker)
    )
    for _ in range(50):
        await asyncio.sleep(0)
        if loop._pending_questions:
            break
    request_id = next(iter(loop._pending_questions))
    loop._pending_questions[request_id].set_result(
        RespondQuestion(request_id=request_id, answer="1", user_answered=True)
    )
    answers = await task
    assert answers == {"approve": "1", USER_ANSWERED_KEY: True}


async def test_an_unattributed_response_leaves_the_answers_bare() -> None:
    loop = _loop()

    async def _emit(event: Any) -> None:
        return None

    loop._emit = _emit  # type: ignore[method-assign]
    marker = InputRequired(
        server="builtin", tool="AskUser", request_state=None,
        requests={"approve": {"message": "Approve the plan?"}},
    )
    task = asyncio.create_task(
        loop._collect_input_responses("s", "t", "AskUser", "tu1", marker)
    )
    for _ in range(50):
        await asyncio.sleep(0)
        if loop._pending_questions:
            break
    request_id = next(iter(loop._pending_questions))
    loop._pending_questions[request_id].set_result(
        RespondQuestion(request_id=request_id, answer="1")
    )
    assert await task == {"approve": "1"}
