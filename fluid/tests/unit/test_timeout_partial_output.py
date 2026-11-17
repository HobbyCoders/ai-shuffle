"""A timed-out command or cell keeps what it printed before the timeout (#1549).

Three paths, each through a real subprocess:

- Bash's own timeout (also covered in test_tool_output_spill.py);
- Bash inside a code-mode cell under the kernel's per-call ceiling: Bash reads
  the time left on that ceiling when it starts and cuts its timeout so its own
  timeout fires first. The clock is the kernel's: it starts when the cell asks
  for the call, so time spent on hooks and gates counts and time spent waiting
  on a human does not;
- a code-mode cell past its wall clock: the kernel interrupts the child with
  SIGINT and reads the done frame that carries the stdout printed so far
  (POSIX only; elsewhere, or when the cell swallows the interrupt, the error
  says the output was lost).

Recovered output goes through the same caps and spill files as finished
output.
"""

from __future__ import annotations

import asyncio
import re
import shutil
import sys
from pathlib import Path

import pytest
from fluid.codemode import SubprocessKernel
from fluid.codemode.kernel import HostReply
from fluid.codemode.session import CodeModeSession
from fluid.codemode.subprocess_kernel import KernelConfig
from fluid.events import ToolResult
from fluid.hooks.bus import HookBus
from fluid.permissions.engine import PermissionEngine
from fluid.tools.builtins import bash
from fluid.tools.builtins._util import _TRUNCATE_LIMIT
from fluid.tools.registry import (
    ToolCtx,
    ToolOutcome,
    ToolRegistry,
    ToolSpec,
    current_call_in_cell,
    current_call_time_left,
)

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="SIGINT interrupt is POSIX-only")
needs_bash = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


def _ctx(tmp_path: Path) -> ToolCtx:
    return ToolCtx(
        cwd=str(tmp_path), session_id="sess-1",
        extra={"harness_state_root": tmp_path / "state"},
    )


def _spill_path(text: str) -> Path:
    match = re.search(r"saved to (.+?\.txt)", text)
    assert match, text
    return Path(match.group(1))


async def _null_host(name: str, kwargs: dict) -> HostReply:  # pragma: no cover
    return HostReply(error="no host")


class _Loop:
    def __init__(self, tmp_path: Path, hooks: HookBus | None = None) -> None:
        self.ctx = _ctx(tmp_path)
        self.hooks = hooks
        self.permissions = PermissionEngine("never_ask")
        self.events: list = []

    async def _emit(self, event) -> None:
        self.events.append(event)


def _hang_after(text: str) -> str:
    return f"print({text!r}, flush=True)\nwhile True:\n    pass\n"


def _py(code: str) -> str:
    return f'"{sys.executable}" -c "{code}"'


# ── case 3: a cell that prints, then hangs ──────────────────────────────────


@posix_only
async def test_a_timed_out_cell_returns_what_it_printed() -> None:
    kernel = SubprocessKernel(KernelConfig())
    await kernel.start()
    try:
        await kernel.exec("keep = 7", timeout=10, on_host_call=_null_host)
        slow = await kernel.exec(_hang_after("FATAL: lock held"), timeout=0.5, on_host_call=_null_host)
        assert "FATAL: lock held" in slow.stdout
        assert slow.error and "wall clock" in slow.error and "kernel restarted" in slow.error
        assert "printed before the timeout and may be incomplete" in slow.error
        assert "KeyboardInterrupt" not in slow.error  # the interrupt is the host's, not the cell's
        # The kernel was still restarted from its checkpoint.
        assert (await kernel.exec("keep", timeout=10, on_host_call=_null_host)).value_repr == "7"
    finally:
        await kernel.stop()


@posix_only
async def test_a_timed_out_cell_keeps_the_stdout_cap_and_spill(tmp_path) -> None:
    """Recovered output is cut to the cell stdout cap and the cut is spilled
    whole, as for a finished cell (#1538): a timeout is no way past the cap."""
    session = CodeModeSession(ToolRegistry(), config=KernelConfig())
    code = "print('HEAD')\nprint('x' * 50_000)\nprint('TAIL', flush=True)\nwhile True:\n    pass\n"
    try:
        out = await session.run_python(
            sid="s", turn_id="t", loop=_Loop(tmp_path), input={"code": code, "timeout": 0.5})
    finally:
        await session.aclose()
    assert out.is_error
    assert len(out.text) < 12_000
    assert "HEAD" in out.text and "TAIL" in out.text
    assert "printed before the timeout" in out.text
    full = _spill_path(out.text).read_text(encoding="utf-8")
    assert "HEAD" in full and "TAIL" in full and "x" * 50_000 in full


async def test_a_cell_that_swallows_the_interrupt_says_its_output_was_lost() -> None:
    kernel = SubprocessKernel(KernelConfig())
    await kernel.start()
    try:
        code = (
            "print('never seen', flush=True)\n"
            "while True:\n"
            "    try:\n"
            "        while True:\n"
            "            pass\n"
            "    except KeyboardInterrupt:\n"
            "        pass\n"
        )
        slow = await kernel.exec(code, timeout=0.5, on_host_call=_null_host)
        assert slow.stdout == ""
        assert slow.error and "output it printed before the timeout was lost" in slow.error
        assert (await kernel.exec("1 + 1", timeout=10, on_host_call=_null_host)).value_repr == "2"
    finally:
        await kernel.stop()


@posix_only
async def test_a_cancelled_cell_still_discards_and_restarts() -> None:
    """A turn cancel is not a timeout: nothing is recovered and the next cell
    runs on a fresh, restored kernel."""
    kernel = SubprocessKernel(KernelConfig())
    await kernel.start()
    try:
        await kernel.exec("keep = 3", timeout=10, on_host_call=_null_host)
        task = asyncio.ensure_future(
            kernel.exec(_hang_after("half done"), timeout=30, on_host_call=_null_host))
        await asyncio.sleep(0.5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert (await kernel.exec("keep", timeout=10, on_host_call=_null_host)).value_repr == "3"
    finally:
        await kernel.stop()


# ── case 2: Bash in a cell, under the kernel's per-call ceiling ─────────────


@needs_bash
async def test_bash_in_a_cell_times_out_under_the_ceiling_with_its_output(tmp_path) -> None:
    """Asked for 60s under an 8s ceiling, Bash runs for under 8 - 5 = 3s, says so,
    and returns what it printed instead of being cancelled with nothing.
    An observer that rewrites the timeout after the gate changes nothing: the
    cut is made on the input that runs, when it runs."""
    hooks = HookBus()

    async def raise_timeout(payload: dict) -> None:
        payload["input"]["timeout"] = 600  # shares the input dict with the gate

    hooks.on_tool_allowed(raise_timeout)
    registry = ToolRegistry()
    registry.register_builtin(bash.SPEC, bash.handler)
    session = CodeModeSession(registry, config=KernelConfig(host_call_timeout=8))
    loop = _Loop(tmp_path, hooks=hooks)
    command = _py("print('FATAL: db locked', flush=True); import time; time.sleep(60)")
    code = (
        "from tools.Bash import Bash\n"
        "try:\n"
        f"    Bash(command={command!r}, timeout=60)\n"
        "except Exception as exc:\n"
        "    print('CAUGHT', str(exc)[-300:])\n"
    )
    try:
        out = await session.run_python(sid="s", turn_id="t", loop=loop, input={"code": code, "timeout": 2})
    finally:
        await session.aclose()
    results = [e for e in loop.events if isinstance(e, ToolResult) and e.name == "Bash"]
    assert len(results) == 1 and results[0].is_error
    output = results[0].output
    # 8s ceiling - 5s grace, less the time the call spent before Bash started.
    assert re.search(r"timed out after 2\.\ds \(cut from 600s to fit the time left", output), output
    assert "FATAL: db locked" in output and "may be incomplete" in output
    assert "cancelled before it returned" not in output
    assert "FATAL: db locked" in out.text and "CAUGHT" in out.text


@needs_bash
async def test_in_cell_timeout_output_keeps_the_direct_cap_and_spills(tmp_path) -> None:
    """In a cell Bash's finished output gets the 50k cell limit; its timeout
    output is an error message and keeps the 12k direct cap, spilled whole."""
    ctx = _ctx(tmp_path)
    command = (
        "for i in $(seq 1 3000); do echo \"line $i of noise\"; done; "
        "echo FATAL-BEFORE-HANG; sleep 30"
    )
    cell = current_call_in_cell.set(True)
    left = current_call_time_left.set(lambda: 7.5)
    try:
        out = await bash.handler({"command": command, "timeout": 60}, ctx)
    finally:
        current_call_time_left.reset(left)
        current_call_in_cell.reset(cell)
    assert out.is_error
    assert "timed out after 2.5s (cut from 60s" in out.text
    assert len(out.text) < _TRUNCATE_LIMIT + 2_000
    assert "FATAL-BEFORE-HANG" in out.text
    full = _spill_path(out.text).read_text(encoding="utf-8")
    assert "line 1500 of noise" in full and full.rstrip().endswith("FATAL-BEFORE-HANG")


@needs_bash
async def test_bash_has_one_deadline_for_its_whole_run(tmp_path) -> None:
    """A command that closes its output late and then hangs gets the time
    left, not a second full timeout, so the cut timeout really bounds it."""
    loop = asyncio.get_running_loop()
    started = loop.time()
    command = "echo EARLY; sleep 1.5; exec >/dev/null 2>&1; sleep 30"
    out = await bash.handler({"command": command, "timeout": 2}, _ctx(tmp_path))
    assert out.is_error and "timed out after 2s" in out.text and "EARLY" in out.text
    assert loop.time() - started < 3.2


async def test_bash_with_too_little_time_left_is_not_run(tmp_path) -> None:
    marker = tmp_path / "ran"
    token = current_call_time_left.set(lambda: 5.5)
    try:
        out = await bash.handler({"command": f"touch '{marker}'"}, _ctx(tmp_path))
    finally:
        current_call_time_left.reset(token)
    assert out.is_error and "Bash was not run" in out.text
    assert not marker.exists()


@needs_bash
async def test_bash_with_room_runs_untouched(tmp_path) -> None:
    token = current_call_time_left.set(lambda: 900.0)
    try:
        out = await bash.handler({"command": "echo fine", "timeout": 30}, _ctx(tmp_path))
    finally:
        current_call_time_left.reset(token)
    assert not out.is_error and out.text.strip() == "fine"


async def test_the_budget_counts_gate_time_and_credits_human_wait() -> None:
    """The time left Bash sees is the kernel's own deadline: started when the
    cell asked for the call, minus time spent before the tool started (hooks,
    gates), plus only THIS call's human wait (earlier calls' waits in the same
    cell do not count)."""
    kernel = SubprocessKernel(KernelConfig(host_call_timeout=100))
    human = {"s": 40.0}  # an earlier call in this cell already waited 40s
    seen: dict = {}

    async def host(name: str, kwargs: dict) -> HostReply:
        await asyncio.sleep(0.5)  # a gate that takes time
        human["s"] += 30.0  # then a human answers an approval after 30s
        seen["left"] = current_call_time_left.get()()
        return HostReply(result="ok")

    reply = await kernel._call_host(host, "Bash", {}, 100.0, lambda: human["s"])
    assert reply.result == "ok"
    assert 129.0 < seen["left"] < 129.6  # 100 - 0.5 + 30, not + 70


async def test_the_deadline_is_enforced_with_the_same_credit() -> None:
    """The enforcement extends by this call's human wait only, matching what
    Bash was told."""
    kernel = SubprocessKernel(KernelConfig())
    human = {"s": 50.0}

    async def host(name: str, kwargs: dict) -> HostReply:
        await asyncio.sleep(5)
        return HostReply(result="late")

    loop = asyncio.get_running_loop()
    started = loop.time()
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(kernel._call_host(host, "x", {}, 0.3, lambda: human["s"]), 3)
    # Cancelled at its own 0.3s, not stretched by the earlier 50s wait.
    assert loop.time() - started < 1.5


async def test_a_worker_started_inside_a_bridged_call_is_not_bound(tmp_path) -> None:
    """Only a cell's own call carries the ceiling; a direct dispatch inside it
    (a Task worker's tool call) starts unbound."""
    registry = ToolRegistry()
    seen: list = []

    async def probe(input: dict, ctx: ToolCtx) -> ToolOutcome:
        seen.append(current_call_time_left.get())
        return ToolOutcome(text="ok")

    registry.register_builtin(
        ToolSpec(name="Probe", description="p", input_schema={"type": "object"}), probe)
    token = current_call_time_left.set(lambda: 10.0)
    try:
        await registry.dispatch("Probe", {}, _ctx(tmp_path), raw_shell_output=True)
        await registry.dispatch("Probe", {}, _ctx(tmp_path))
    finally:
        current_call_time_left.reset(token)
    assert seen[0] is not None and seen[0]() == 10.0
    assert seen[1] is None


# ── all three through the real native loop, scripted provider ───────────────


def _tool_call(name: str, args: dict, tuid: str) -> list:
    import json

    from fluid.providers.base import StreamChunk

    return [
        StreamChunk("message_start", {"usage": {"input_tokens": 10}}),
        StreamChunk("block_start", {"index": 0, "block_type": "tool_use", "tool_name": name, "tool_use_id": tuid}),
        StreamChunk("tool_args_delta", {"index": 0, "partial_json": json.dumps(args)}),
        StreamChunk("block_stop", {"index": 0}),
        StreamChunk("usage", {"usage": {"output_tokens": 5}, "stop_reason": "tool_use"}),
        StreamChunk("message_stop", {}),
    ]


def _final_text(text: str) -> list:
    from fluid.providers.base import StreamChunk

    return [
        StreamChunk("message_start", {"usage": {"input_tokens": 10}}),
        StreamChunk("block_start", {"index": 0, "block_type": "text"}),
        StreamChunk("text_delta", {"index": 0, "text": text}),
        StreamChunk("block_stop", {"index": 0}),
        StreamChunk("usage", {"usage": {"output_tokens": 5}, "stop_reason": "end_turn"}),
        StreamChunk("message_stop", {}),
    ]


class _Scripted:
    name = "anthropic"

    def __init__(self, scripts: list) -> None:
        from fluid.providers.base import ProviderCaps

        self.caps = ProviderCaps(explicit_cache_breakpoints=True, max_cache_breakpoints=4)
        self.scripts = scripts
        self.requests: list = []

    async def stream(self, request):
        self.requests.append(request)
        for chunk in self.scripts[min(len(self.requests) - 1, len(self.scripts) - 1)]:
            yield chunk


def _last_tool_result(request) -> str:
    from fluid.messages import ToolResultBlock

    for message in reversed(request.messages):
        for block in message.content:
            if isinstance(block, ToolResultBlock):
                content = block.content
                return content if isinstance(content, str) else repr(content)
    raise AssertionError("no tool result in the request")


@needs_bash
@posix_only
async def test_the_native_loop_hands_the_model_what_timed_out_calls_printed(tmp_path) -> None:
    """Bash's own timeout (JSON path), Bash under a cell's per-call ceiling,
    and a cell that prints then hangs: each next model request carries the
    text printed before the timeout, said to be partial."""
    from fluid.engine.native import NativeLoop
    from fluid.events import UserTurn
    from fluid.store.base import MemoryStore

    hang = _py("print('BASH-SAID-THIS', flush=True); import time; time.sleep(60)")
    direct = _Scripted([
        _tool_call("Bash", {"command": hang, "timeout": 1}, "b1"), _final_text("done")])
    registry = ToolRegistry()
    registry.register_builtin(bash.SPEC, bash.handler)
    loop = NativeLoop(
        provider=direct, store=MemoryStore(), model="claude-sonnet-5", registry=registry,
        permissions=PermissionEngine("never_ask"), ctx=_ctx(tmp_path),
    )
    try:
        await loop.submit(UserTurn(content="run it"))
        await loop.wait_turn()
    finally:
        await loop.aclose()
    seen = _last_tool_result(direct.requests[1])
    assert "timed out after 1s" in seen and "BASH-SAID-THIS" in seen and "may be incomplete" in seen

    in_cell = f"from tools.Bash import Bash\nBash(command={hang!r}, timeout=60)\n"
    cell_hang = _hang_after("CELL-SAID-THIS")
    coded = _Scripted([
        _tool_call("run_python", {"code": in_cell, "timeout": 2}, "c1"),
        _tool_call("run_python", {"code": cell_hang, "timeout": 1}, "c2"),
        _final_text("done"),
    ])
    session = CodeModeSession(registry, config=KernelConfig(host_call_timeout=7))
    loop = NativeLoop(
        provider=coded, store=MemoryStore(), model="claude-sonnet-5", registry=registry,
        codemode=session, permissions=PermissionEngine("never_ask"), ctx=_ctx(tmp_path),
    )
    try:
        await loop.submit(UserTurn(content="run them"))
        await loop.wait_turn()
    finally:
        await loop.aclose()
    seen = _last_tool_result(coded.requests[1])
    assert "BASH-SAID-THIS" in seen and "cut from 60s" in seen and "cancelled before" not in seen
    seen = _last_tool_result(coded.requests[2])
    assert "CELL-SAID-THIS" in seen and "printed before the timeout and may be incomplete" in seen


# ── the ceiling expiring after the tool finished (post-processing) ──────────


@needs_bash
async def test_a_slow_post_tool_hook_does_not_cost_the_cell_bashs_output(tmp_path) -> None:
    """Ceiling 7s, Bash asks for 60s, a PostToolUse hook awaits 6s: Bash times
    out at about 1.9s with its capped, spilled output; the ceiling then expires
    inside the hook. The cell still gets that output, with a note that the
    hooks did not finish, and the host's result event says the same."""
    from fluid.hooks.bus import POST_TOOL_USE

    hook_saw: list[str] = []

    async def slow_hook(event: str, payload: dict):
        hook_saw.append(payload["output"])
        await asyncio.sleep(6)

    hooks = HookBus()
    hooks.on(POST_TOOL_USE, slow_hook)
    registry = ToolRegistry()
    registry.register_builtin(bash.SPEC, bash.handler)
    session = CodeModeSession(registry, config=KernelConfig(host_call_timeout=7))
    loop = _Loop(tmp_path, hooks=hooks)
    command = (
        "for i in $(seq 1 3000); do echo \"line $i of noise\"; done; "
        "echo FATAL-BEFORE-HANG; sleep 60"
    )
    code = (
        "from tools.Bash import Bash\n"
        "try:\n"
        f"    Bash(command={command!r}, timeout=60)\n"
        "except Exception as exc:\n"
        "    print(str(exc))\n"
    )
    started = asyncio.get_running_loop().time()
    try:
        out = await session.run_python(sid="s", turn_id="t", loop=loop, input={"code": code, "timeout": 2})
    finally:
        await session.aclose()
    assert asyncio.get_running_loop().time() - started < 12
    assert hook_saw and "FATAL-BEFORE-HANG" in hook_saw[0]
    assert "FATAL-BEFORE-HANG" in out.text and "PostToolUse hooks did not finish" in out.text
    assert "did not return within" not in out.text
    # Still capped and spilled: the cell's 8k stdout cap, the full text on disk.
    assert len(out.text) < 12_000
    # Bash spilled its whole run, and the cell's spill keeps Bash's pointer.
    cell_full = _spill_path(out.text).read_text(encoding="utf-8")
    bash_file = re.search(r"saved to (\S+?Bash\S*?\.txt)", cell_full)
    assert bash_file, cell_full
    assert "line 1500 of noise" in Path(bash_file.group(1)).read_text(encoding="utf-8")
    events = [e for e in loop.events if isinstance(e, ToolResult) and e.name == "Bash"]
    assert len(events) == 1 and events[0].is_error
    assert "FATAL-BEFORE-HANG" in events[0].output and "cancelled before it returned" not in events[0].output


async def test_a_filed_reply_survives_the_ceiling_but_not_a_turn_cancel() -> None:
    from fluid.codemode.kernel import current_host_call_salvage

    kernel = SubprocessKernel(KernelConfig())

    async def files_then_hangs(name: str, kwargs: dict) -> HostReply:
        current_host_call_salvage.get().reply = HostReply(result="kept")
        await asyncio.sleep(30)  # post-processing that never ends
        return HostReply(result="never")

    # The ceiling: the filed reply comes back at once.
    reply = await asyncio.wait_for(kernel._call_host(files_then_hangs, "x", {}, 0.3, None), 3)
    assert reply.result == "kept"

    # A turn cancel: stops promptly, nothing is salvaged.
    loop = asyncio.get_running_loop()
    task = asyncio.ensure_future(kernel._call_host(files_then_hangs, "x", {}, 30, None))
    await asyncio.sleep(0.2)
    started = loop.time()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert loop.time() - started < 1


async def test_a_call_with_nothing_filed_still_times_out() -> None:
    kernel = SubprocessKernel(KernelConfig())

    async def hangs(name: str, kwargs: dict) -> HostReply:
        await asyncio.sleep(30)
        return HostReply(result="never")

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(kernel._call_host(hangs, "x", {}, 0.3, None), 3)


async def test_an_unbounded_inner_call_cannot_file_into_the_outer_slot() -> None:
    from fluid.codemode.kernel import current_host_call_salvage

    kernel = SubprocessKernel(KernelConfig())
    seen: list = []

    async def inner(name: str, kwargs: dict) -> HostReply:
        seen.append(current_host_call_salvage.get())
        return HostReply(result="inner")

    async def outer(name: str, kwargs: dict) -> HostReply:
        seen.append(current_host_call_salvage.get())
        return await kernel._call_host(inner, "y", {}, 0, None)

    assert (await kernel._call_host(outer, "x", {}, 10, None)).result == "inner"
    assert seen[0] is not None and seen[1] is None


@needs_bash
async def test_a_slow_result_event_does_not_cost_the_cell_its_reply(tmp_path) -> None:
    """The last await before the cell gets its reply is the host's ToolResult
    event. A ceiling that expires there returns the finished reply."""

    class _SlowLoop(_Loop):
        slowed = False

        async def _emit(self, event) -> None:
            if isinstance(event, ToolResult) and event.name == "Bash" and not self.slowed:
                self.slowed = True
                await asyncio.sleep(30)
            self.events.append(event)

    registry = ToolRegistry()
    registry.register_builtin(bash.SPEC, bash.handler)
    session = CodeModeSession(registry, config=KernelConfig(host_call_timeout=6))
    loop = _SlowLoop(tmp_path)
    code = (
        "from tools.Bash import Bash\n"
        "print(Bash(command='echo QUICK-RESULT')['output'])\n"
    )
    started = asyncio.get_running_loop().time()
    try:
        out = await session.run_python(sid="s", turn_id="t", loop=loop, input={"code": code, "timeout": 2})
    finally:
        await session.aclose()
    assert asyncio.get_running_loop().time() - started < 15
    assert "QUICK-RESULT" in out.text and "did not return within" not in out.text
