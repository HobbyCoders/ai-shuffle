"""Round-9 audit: turn death, fail-fast cells, task plumbing, compaction stubs.

Items 6-10 of the round-9 fix plan:

* a hung external tool call is bounded (registry-level timeout, ToolResult
  emitted even when the call is cancelled), and a turn that makes no
  progress is closed by a watchdog with its cost recorded;
* Grep/Glob missing paths, Edit mismatches and MCP tool errors return as a
  cell's results instead of aborting it (timeouts and denials still do);
* subagents are named from their Task, SendMessage errors list who is
  reachable, and interim peer messages queue while the parent is parked;
* trim placeholders say what was lost and where it still lives, and short
  error results no longer block segment digestion;
* Note/Monitor/run_python wording matches the behaviour above.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from fluid.codemode import HostBridge, Projection, SubprocessKernel
from fluid.codemode.session import CodeModeSession
from fluid.codemode.subprocess_kernel import KernelConfig
from fluid.context.compaction import completed_segments
from fluid.context.trim import ToolResultTrimmer, TrimConfig, describe_trimmed_call
from fluid.engine.native import NativeLoop
from fluid.events import ErrorEvent, Interrupted, ToolResult, TurnComplete, Usage, UserTurn
from fluid.events.ops import AgentMessage
from fluid.messages import Message, Text, ToolResultBlock, ToolUseBlock
from fluid.permissions.engine import PermissionEngine
from fluid.providers.base import ProviderCaps, ProviderRequest, StreamChunk
from fluid.store.base import MemoryStore, SessionNode
from fluid.tools.builtins import edit as edit_mod
from fluid.tools.builtins import glob as glob_mod
from fluid.tools.builtins import grep as grep_mod
from fluid.tools.builtins import monitor as monitor_mod
from fluid.tools.builtins import note as note_mod
from fluid.tools.builtins import read as read_mod
from fluid.tools.builtins import send_message as send_mod
from fluid.tools.builtins import task as task_mod
from fluid.tools.inproc import create_inproc_server, tool
from fluid.tools.registry import (
    FATAL_EXTERNAL_ERROR_CODES,
    BackgroundTaskReceipt,
    ToolCtx,
    ToolOutcome,
    ToolRegistry,
)


async def _run(registry, bridge, code, *, timeout=8, config=None):
    kernel = SubprocessKernel(config)
    await kernel.start()
    try:
        await kernel.set_vfs(Projection(registry.list_tools()).sources())
        return await kernel.exec(code, timeout=timeout, on_host_call=bridge.on_host_call)
    finally:
        await kernel.stop()


def _inproc_registry(**kw) -> ToolRegistry:
    @tool("stale", "Returns a browser-style error",
          {"type": "object", "properties": {}})
    async def stale(args: dict) -> dict:
        return {"content": [{"type": "text", "text": "stale_ref: element ref_3 is no longer attached"}],
                "isError": True}

    @tool("slow", "Never answers", {"type": "object", "properties": {}})
    async def slow(args: dict) -> dict:
        await asyncio.sleep(30)
        return {"content": [{"type": "text", "text": "late"}]}

    @tool("fine", "Answers", {"type": "object", "properties": {}})
    async def fine(args: dict) -> dict:
        return {"content": [{"type": "text", "text": "ok"}]}

    registry = ToolRegistry(**kw)
    registry.register_inproc(create_inproc_server("browser", [stale, slow, fine]))
    return registry


# ─── 6a: external calls are bounded and always produce a result ─────────────


async def test_registry_bounds_external_calls() -> None:
    registry = _inproc_registry(external_call_timeout=0.2)
    out = await registry.dispatch("mcp__browser__slow", {}, ToolCtx())
    assert out.is_error and out.error_code == "tool_timeout"
    assert "did not return within 0.2 s" in out.text
    fine = await registry.dispatch("mcp__browser__fine", {}, ToolCtx())
    assert not fine.is_error and "ok" in fine.text
    assert "tool_timeout" in FATAL_EXTERNAL_ERROR_CODES


def test_mcp_client_default_call_timeout_is_a_minute() -> None:
    from fluid.tools.mcp.client import DEFAULT_CALL_TIMEOUT_S, McpClient

    assert DEFAULT_CALL_TIMEOUT_S == 60.0
    client = McpClient("fake", {"command": "noop"})
    timed = client._timed_out("browser_snapshot")
    assert timed["is_error"] and timed["error_code"] == "tool_timeout"


class _ProbeLoop:
    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry
        self.ctx = ToolCtx()
        self.hooks = None
        self.permissions = PermissionEngine("never_ask")
        self.events: list = []

    async def _emit(self, event) -> None:
        self.events.append(event)

    async def _await_permission(self, *args, **kwargs):  # pragma: no cover
        return "allow", None, None


@pytest.mark.asyncio
async def test_cancelled_in_kernel_call_still_gets_a_tool_result() -> None:
    """The kernel's per-call ceiling cancels a hung host call; the ToolUse
    that went out must still be answered by a ToolResult event."""
    registry = _inproc_registry(external_call_timeout=None)
    session = CodeModeSession(registry, config=KernelConfig(host_call_timeout=0.3))
    loop = _ProbeLoop(registry)
    try:
        out = await session.run_python(
            sid="s", turn_id="t", input={"code": "from tools import slow\nslow()\nprint('after')", "timeout": 5},
            loop=loop,
        )
    finally:
        await session.aclose()
    results = [e for e in loop.events if isinstance(e, ToolResult)]
    assert results and results[0].name.endswith("slow") and results[0].is_error
    assert "cancelled" in results[0].output
    assert "after" not in (out.text or "")


# ─── 6b: turn watchdog and the non-aclose cancel path ────────────────────────


class _HangingProvider:
    name = "anthropic"
    caps = ProviderCaps(explicit_cache_breakpoints=True, max_cache_breakpoints=4)

    async def stream(self, request: ProviderRequest) -> AsyncIterator[StreamChunk]:
        yield StreamChunk("message_start", {"usage": {"input_tokens": 1234, "cache_read_input_tokens": 0}})
        await asyncio.sleep(60)
        yield StreamChunk("message_stop", {})  # pragma: no cover


async def test_turn_watchdog_ends_a_silent_turn_with_its_cost() -> None:
    loop = NativeLoop(
        provider=_HangingProvider(), store=MemoryStore(), model="m", system="s",
        turn_watchdog_seconds=0.6,
    )
    await loop.submit(UserTurn(content="hi"))
    await asyncio.wait_for(loop.wait_turn(), timeout=10)  # no CancelledError leaks
    events = loop.bus.log(loop.session_id)
    errors = [e for e in events if isinstance(e, ErrorEvent)]
    assert errors and "watchdog" in errors[-1].message
    assert errors[-1].usage.get("input_tokens") == 1234
    done = [e for e in events if isinstance(e, TurnComplete)]
    assert done and done[-1].stop_reason == "error"
    assert done[-1].usage.get("input_tokens") == 1234
    assert not [e for e in events if isinstance(e, Interrupted)]
    # Cost of the cancelled stream was folded exactly once.
    usages = [e for e in events if isinstance(e, Usage)]
    assert len(usages) == 1 and usages[0].raw.get("cancelled") is True
    # The engine is usable afterwards.
    assert loop._turn_task.done()
    await loop.aclose()


async def test_aclose_still_reports_engine_closed() -> None:
    loop = NativeLoop(
        provider=_HangingProvider(), store=MemoryStore(), model="m", system="s",
        turn_watchdog_seconds=None,
    )
    await loop.submit(UserTurn(content="hi"))
    await asyncio.sleep(0.2)
    await loop.aclose()
    events = loop.bus.log(loop.session_id)
    stops = [e for e in events if isinstance(e, Interrupted)]
    assert stops and stops[-1].message == "engine closed"
    assert not [e for e in events if isinstance(e, TurnComplete)]


async def test_watchdog_waits_while_a_permission_is_pending() -> None:
    loop = NativeLoop(
        provider=_HangingProvider(), store=MemoryStore(), model="m", system="s",
        turn_watchdog_seconds=0.4,
    )
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    loop._pending_permissions["p1"] = fut
    await loop.submit(UserTurn(content="hi"))
    await asyncio.sleep(1.2)
    assert not loop._turn_task.done(), "a human wait must not trip the watchdog"
    fut.set_result(None)
    await asyncio.wait_for(loop.wait_turn(), timeout=10)
    assert [e for e in loop.bus.log(loop.session_id) if isinstance(e, TurnComplete)]
    await loop.aclose()


# ─── 7: fail-fast cells ───────────────────────────────────────────────────────


def _builtins_registry() -> ToolRegistry:
    registry = ToolRegistry()
    for module in (read_mod, grep_mod, glob_mod, edit_mod):
        registry.register_builtin(module.SPEC, module.handler)
    return registry


@pytest.mark.parametrize("call,marker", [
    ("Grep(pattern='needle', path='absent')", "(path missing"),
    ("Glob(pattern='*.py', path='absent')", "(path missing"),
])
async def test_grep_and_glob_missing_paths_are_soft_results(tmp_path, call, marker) -> None:
    registry = _builtins_registry()
    (tmp_path / "present.txt").write_text("needle", encoding="utf-8")
    ctx = ToolCtx(cwd=str(tmp_path))
    bridge = HostBridge(registry, PermissionEngine("never_ask"), ctx=ctx)
    result = await _run(registry, bridge,
        "from tools import Read, Grep, Glob\n"
        f"r = {call}\n"
        "print('RESULT:', r)\n"
        "print(Read(file_path='present.txt'))\n")
    assert result.error is None, result.error
    assert marker in result.stdout
    assert "needle" in result.stdout  # the cell kept going


async def test_read_missing_path_returns_a_falsy_missing_file(tmp_path) -> None:
    registry = _builtins_registry()
    bridge = HostBridge(registry, PermissionEngine("never_ask"), ctx=ToolCtx(cwd=str(tmp_path)))
    result = await _run(registry, bridge,
        "from tools import Read\nr = Read(file_path='absent.txt')\n"
        "print('reached', bool(r))\nr.splitlines()\n")
    assert "reached False" in result.stdout
    assert "MissingPathError" in (result.error or "")


async def test_edit_mismatch_is_a_soft_result_and_nothing_lands(tmp_path) -> None:
    registry = _builtins_registry()
    target = tmp_path / "f.py"
    target.write_text("x = 1\ny = 2\n", encoding="utf-8")
    bridge = HostBridge(registry, PermissionEngine("never_ask"), ctx=ToolCtx(cwd=str(tmp_path)))
    result = await _run(registry, bridge,
        "from tools import Read, Edit\n"
        "_ = Read(file_path='f.py')\n"
        "r = Edit(file_path='f.py', old_string='z = 9', new_string='z = 10')\n"
        "print('RESULT:', r[:40])\n"
        "print('continued')\n")
    assert result.error is None, result.error
    assert "Edit NOT applied" in result.stdout and "continued" in result.stdout
    assert target.read_text(encoding="utf-8") == "x = 1\ny = 2\n"
    # Not unique: same shape.
    target.write_text("a\na\n", encoding="utf-8")
    result = await _run(registry, bridge,
        "from tools import Read, Edit\n"
        "_ = Read(file_path='f.py')\n"
        "r = Edit(file_path='f.py', old_string='a', new_string='b')\n"
        "print('RESULT:', r)\nprint('continued')\n")
    assert result.error is None and "not unique" in result.stdout and "continued" in result.stdout
    assert target.read_text(encoding="utf-8") == "a\na\n"


async def test_edit_read_gate_still_aborts(tmp_path) -> None:
    registry = _builtins_registry()
    (tmp_path / "unread.txt").write_text("old", encoding="utf-8")
    bridge = HostBridge(registry, PermissionEngine("never_ask"), ctx=ToolCtx(cwd=str(tmp_path)))
    result = await _run(registry, bridge,
        "from tools import Edit\nEdit(file_path='unread.txt', old_string='old', new_string='new')\nprint('unreachable')\n")
    assert result.error is not None and "unreachable" not in result.stdout


async def test_mcp_tool_error_is_soft_but_timeout_and_denial_abort() -> None:
    registry = _inproc_registry(external_call_timeout=0.2)
    bridge = HostBridge(registry, PermissionEngine("never_ask"), ctx=ToolCtx())
    result = await _run(registry, bridge,
        "from tools import stale, fine\nr = stale()\nprint('RESULT:', r)\nprint(fine())\n")
    assert result.error is None, result.error
    assert "stale_ref" in result.stdout and "ok" in result.stdout
    # The registry timeout (error_code tool_timeout) still aborts the cell.
    result = await _run(registry, bridge,
        "from tools import slow\nslow()\nprint('unreachable')\n")
    assert result.error is not None and "unreachable" not in result.stdout
    # A denial still aborts.
    denied = HostBridge(registry, PermissionEngine("always_ask"), ctx=ToolCtx())
    result = await _run(registry, denied,
        "from tools import fine\nfine()\nprint('unreachable')\n")
    assert result.error is not None and "unreachable" not in result.stdout


# ─── 8: task plumbing ─────────────────────────────────────────────────────────


def test_agents_are_named_from_the_task() -> None:
    assert task_mod.agent_name_for({"agent": "general-purpose", "prompt": "Fix the flaky login test in CI\nmore"}) == "fix-the-flaky-login"
    assert task_mod.agent_name_for({"agent": "x", "label": "Design mockups"}) == "design-mockups"
    assert task_mod.agent_name_for({"agent": "x", "name": "reviewer"}) == "reviewer"
    assert task_mod.agent_name_for({"agent": "explore"}) == "explore"
    receipt = BackgroundTaskReceipt(task_id="task_1", worker_session_id=None, description="d")
    out = task_mod._background_outcome(receipt, {"agent": "x", "prompt": "Audit the sessions"})
    assert "as agent 'audit-the-sessions'" in out.text and "task id" in out.text


@pytest.mark.asyncio
async def test_task_dispatch_receives_the_derived_name() -> None:
    seen: list[dict] = []

    async def dispatch(input, ctx):
        seen.append(input)
        return {"task_id": "task_9"}

    ctx = ToolCtx()
    ctx.extra["task_dispatch"] = dispatch
    out = await task_mod.handler({"agent": "x", "prompt": "Write the docs", "mode": "background"}, ctx)
    assert seen[0]["name"] == "write-the-docs"
    assert out.background_task is not None and "write-the-docs" in out.text


@pytest.mark.asyncio
async def test_send_message_error_lists_reachable_agents_with_ids() -> None:
    async def deliver(input, ctx):
        return ToolOutcome(text=f"Unknown agent {input['to']!r}; use ListAgents", is_error=True)

    def roster(input, ctx):
        return [
            {"name": "fix-login", "ref": "ab12", "status": "running", "task_id": "task_ab12"},
            {"name": "main", "ref": None, "status": "idle"},
        ]

    ctx = ToolCtx()
    ctx.extra["agent_message"] = deliver
    ctx.extra["agent_roster"] = roster
    out = await send_mod.handler({"to": "general-purpose", "message": "status?"}, ctx)
    assert out.is_error
    assert "fix-login [ab12] (task id task_ab12)" in out.text
    assert "main" in out.text
    assert "task id" in send_mod.describe()


class _IdleProvider:
    name = "anthropic"
    caps = ProviderCaps(explicit_cache_breakpoints=True, max_cache_breakpoints=4)

    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def stream(self, request: ProviderRequest) -> AsyncIterator[StreamChunk]:
        self.prompts.append(request.messages[-1].text())
        yield StreamChunk("message_start", {"usage": {"input_tokens": 10}})
        yield StreamChunk("block_start", {"index": 0, "block_type": "text"})
        yield StreamChunk("text_delta", {"index": 0, "text": "ok"})
        yield StreamChunk("block_stop", {"index": 0})
        yield StreamChunk("usage", {"usage": {"output_tokens": 1}, "stop_reason": "end_turn"})
        yield StreamChunk("message_stop", {})


async def test_interim_peer_message_queues_while_parked() -> None:
    provider = _IdleProvider()
    loop = NativeLoop(provider=provider, store=MemoryStore(), model="m", system="s")
    await loop.submit(UserTurn(content="start"))
    await asyncio.wait_for(loop.wait_turn(), timeout=5)
    # Parked: a background task is pending.
    loop._background_rehydrated = True
    loop._background_pending["task_1"] = BackgroundTaskReceipt(
        task_id="task_1", worker_session_id=None, description="worker",
    )
    await loop.submit(AgentMessage(text="halfway there", sender="worker"))
    await asyncio.sleep(0.1)
    assert loop._turn_task.done(), "an interim message must not wake a parked parent"
    assert len(provider.prompts) == 1
    # Urgent still wakes.
    await loop.submit(AgentMessage(text="need a decision now", sender="worker", urgent=True))
    await asyncio.wait_for(loop.wait_turn(), timeout=5)
    assert len(provider.prompts) == 2 and "need a decision now" in provider.prompts[1]
    assert "halfway there" in provider.prompts[1]  # the queued one rode along
    # Not parked: an ordinary message wakes as before.
    loop._background_pending.clear()
    await loop.submit(AgentMessage(text="done", sender="worker"))
    await asyncio.wait_for(loop.wait_turn(), timeout=5)
    assert len(provider.prompts) == 3 and "done" in provider.prompts[2]
    await loop.aclose()


async def test_queued_peer_message_survives_engine_eviction() -> None:
    """The app evicts a parked engine and rebuilds it for the wake: the
    queued message must come back from the event log, once."""
    store = MemoryStore()
    provider = _IdleProvider()
    first = NativeLoop(provider=provider, store=store, model="m", system="s")
    await first.submit(UserTurn(content="start"))
    await asyncio.wait_for(first.wait_turn(), timeout=5)
    sid = first.session_id
    first._background_rehydrated = True
    first._background_pending["task_1"] = BackgroundTaskReceipt(
        task_id="task_1", worker_session_id=None, description="worker",
    )
    await first.submit(AgentMessage(text="interim note", sender="worker"))
    await asyncio.sleep(0.05)
    assert len(provider.prompts) == 1
    await first.aclose()

    provider2 = _IdleProvider()
    second = NativeLoop(provider=provider2, store=store, model="m", system="s", session_id=sid)
    await second.submit(UserTurn(content="wake"))
    await asyncio.wait_for(second.wait_turn(), timeout=5)
    assert "interim note" in provider2.prompts[0]
    await second.submit(UserTurn(content="again"))
    await asyncio.wait_for(second.wait_turn(), timeout=5)
    assert "interim note" not in provider2.prompts[1]
    await second.aclose()


# ─── 9: compaction stubs ──────────────────────────────────────────────────────


def test_describe_trimmed_call_names_refs_reads_and_loaded_tools() -> None:
    cell = ToolUseBlock(id="c1", name="run_python", input={"code": "Read(...)"})
    text = "stuff\n[refs created this cell: _refs[3] Read (12,000 bytes), _refs[4] Grep (2,000 bytes)]"
    out = describe_trimmed_call(cell, text)
    assert "_refs[3], _refs[4]" in out and "slice those" in out
    read = ToolUseBlock(id="c2", name="Read", input={"file_path": "/a/b.py", "offset": 40, "limit": 60})
    assert describe_trimmed_call(read, "1\tx") == " This was: read /a/b.py lines 40–99 (dropped)."
    whole = ToolUseBlock(id="c3", name="Read", input={"file_path": "/a/b.py"})
    assert "whole file" in describe_trimmed_call(whole, "")
    search = ToolUseBlock(id="c4", name="ToolSearch", input={"query": "select:browser_navigate,mcp__browser__browser_snapshot"})
    out = describe_trimmed_call(search, "")
    assert "browser_navigate, browser_snapshot" in out and "do not ToolSearch them again" in out
    keyword = ToolUseBlock(id="c5", name="ToolSearch", input={"query": "browser"})
    out = describe_trimmed_call(keyword, "Loaded 1 tool(s):\n- browser_act: from tools.browser_act import browser_act\n")
    assert "browser_act" in out
    assert describe_trimmed_call(ToolUseBlock(id="c6", name="Bash", input={}), "x") == ""


def _node(node_id: str, role: str, content: list) -> SessionNode:
    return SessionNode(node_id=node_id, parent_id=None, message=Message(role=role, content=content))


def test_trim_placeholder_carries_the_call_description() -> None:
    nodes = [
        _node("u0", "user", [Text("go")]),
        _node("a0", "assistant", [ToolUseBlock(id="t1", name="Read", input={"file_path": "/x.py", "offset": 1, "limit": 200})]),
        _node("r0", "user", [ToolResultBlock(tool_use_id="t1", content=[Text("1\tline\n" * 900)])]),
        _node("a1", "assistant", [ToolUseBlock(id="t2", name="run_python", input={"code": "x"})]),
        _node("r1", "user", [ToolResultBlock(tool_use_id="t2", content=[Text("out\n" * 800 + "[refs created this cell: _refs[7] Read (9,000 bytes)]")])]),
    ] + [
        _node(f"a{i}", "assistant", [ToolUseBlock(id=f"b{i}", name="Bash", input={})]) for i in range(2, 8)
    ] + [
        _node(f"r{i}", "user", [ToolResultBlock(tool_use_id=f"b{i}", content=[Text("ok")])]) for i in range(2, 8)
    ]
    trimmer = ToolResultTrimmer(TrimConfig(keep_recent_tool_results=1, min_result_chars=100))
    trimmed = trimmer.trim_batch(nodes)
    assert ("r0", "t1") in trimmed and ("r1", "t2") in trimmed
    rendered = trimmer.view(nodes)
    texts = {m.content[0].tool_use_id: m.content[0].content[0].text
             for m in rendered if m.role == "user" and isinstance(m.content[0], ToolResultBlock)}
    assert "read /x.py lines 1–200 (dropped)" in texts["t1"]
    assert "_refs[7]" in texts["t2"] and "HistoryExpand" in texts["t2"]


def test_small_error_results_do_not_block_segments() -> None:
    def turn(error_text: str) -> list[SessionNode]:
        nodes = [_node("start", "user", [Text("go")])]
        for i in range(16):
            nodes.append(_node(f"a{i}", "assistant", [ToolUseBlock(id=f"c{i}", name="run_python", input={})]))
            nodes.append(_node(f"r{i}", "user", [ToolResultBlock(
                tool_use_id=f"c{i}", content=[Text(error_text if i == 2 else "r" * 2_200)],
                is_error=(i == 2), delivery_metadata_version=1,
            )]))
        return nodes

    small = completed_segments(turn("Error: NameError: name 'x' is not defined"))
    assert len(small) == 2 and any(n.node_id == "r2" for n in small[0])
    large = completed_segments(turn("E" * 3_000))
    assert len(large) == 1 and not any(n.node_id == "r2" for n in large[0])


# ─── 10: wording ─────────────────────────────────────────────────────────────


def test_note_monitor_and_run_python_wording() -> None:
    from fluid.cli.prompt import compose_code_mode_addendum
    from fluid.codemode.session import _RUN_PYTHON_DESC

    note = note_mod.SPEC.description
    assert "under ~5 steps skip Note" in note and "batch removals with `texts`" in note
    assert "every current step in one call" not in note
    monitor = monitor_mod.describe()
    assert "BOUNDED summary" in monitor and "TaskOutput has the full buffer" in monitor
    assert "exit code of 0 needs no TaskOutput" in monitor
    assert "returns a falsy MissingFile instead of raising" in _RUN_PYTHON_DESC
    assert "other errors now return as results and do not abort the cell" in _RUN_PYTHON_DESC
    assert "Never continue past permission denials" not in _RUN_PYTHON_DESC
    addendum = compose_code_mode_addendum()
    assert "Other errors now return as results and do not abort the cell" in addendum


def test_addendum_without_read_does_not_say_other_errors() -> None:
    # "Other errors" refers back to the MissingFile sentence, which only a
    # surface with Read carries.
    from fluid.cli.prompt import compose_code_mode_addendum

    addendum = compose_code_mode_addendum(frozenset({"Grep", "Glob"}))
    assert "MissingFile" not in addendum
    assert "Other errors" not in addendum
    assert "Tool errors now return as results and do not abort the cell" in addendum
