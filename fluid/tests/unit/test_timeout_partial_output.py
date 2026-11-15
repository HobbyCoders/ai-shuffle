"""A timed-out command or cell keeps what it printed before the timeout (#1549).

Three paths, each through the real subprocess: Bash's own timeout (covered in
test_tool_output_spill.py), Bash inside a code-mode cell under the kernel's
per-call ceiling, and a code-mode cell past its wall clock. The cell path
interrupts the child with SIGINT, which only POSIX has; on Windows the cell
says its output was lost.
"""

from __future__ import annotations

import sys

import pytest

from fluid.codemode import SubprocessKernel
from fluid.codemode.kernel import HostReply
from fluid.codemode.session import CodeModeSession, _bash_under_ceiling
from fluid.codemode.subprocess_kernel import KernelConfig
from fluid.events import ToolResult
from fluid.permissions.engine import PermissionEngine
from fluid.tools.builtins import bash
from fluid.tools.registry import ToolCtx, ToolRegistry

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="SIGINT interrupt is POSIX-only")


async def _null_host(name: str, kwargs: dict) -> HostReply:  # pragma: no cover
    return HostReply(error="no host")


class _Loop:
    def __init__(self, cwd: str) -> None:
        self.ctx = ToolCtx(cwd=cwd)
        self.hooks = None
        self.permissions = PermissionEngine("never_ask")
        self.events: list = []

    async def _emit(self, event) -> None:
        self.events.append(event)


def _hang_after(text: str) -> str:
    return f"print({text!r}, flush=True)\nwhile True:\n    pass\n"


@posix_only
async def test_a_timed_out_cell_returns_what_it_printed() -> None:
    kernel = SubprocessKernel(KernelConfig())
    await kernel.start()
    try:
        await kernel.exec("keep = 7", timeout=10, on_host_call=_null_host)
        slow = await kernel.exec(_hang_after("FATAL: lock held"), timeout=0.5, on_host_call=_null_host)
        assert "FATAL: lock held" in slow.stdout
        assert slow.error and "wall clock" in slow.error and "kernel restarted" in slow.error
        assert "what it printed before the timeout" in slow.error
        assert "KeyboardInterrupt" not in slow.error  # the interrupt is ours, not the cell's
        # The kernel was still restarted from its checkpoint.
        assert (await kernel.exec("keep", timeout=10, on_host_call=_null_host)).value_repr == "7"
    finally:
        await kernel.stop()


@posix_only
async def test_a_timed_out_cell_keeps_the_stdout_cap_and_spill() -> None:
    """Recovered output goes through the same cap and full-output copy as a
    finished cell's (#1538)."""
    kernel = SubprocessKernel(KernelConfig())
    await kernel.start()
    try:
        code = "print('HEAD')\nprint('x' * 50_000)\nprint('TAIL', flush=True)\nwhile True:\n    pass\n"
        slow = await kernel.exec(code, timeout=0.5, on_host_call=_null_host)
        assert len(slow.stdout) < 20_000 and "HEAD" in slow.stdout and "TAIL" in slow.stdout
        assert "HEAD" in slow.stdout_full and "TAIL" in slow.stdout_full
        assert slow.stdout_total > 50_000
    finally:
        await kernel.stop()


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
async def test_a_timed_out_cell_reaches_the_model_through_the_session(tmp_path) -> None:
    session = CodeModeSession(ToolRegistry(), config=KernelConfig())
    try:
        out = await session.run_python(
            sid="s", turn_id="t", loop=_Loop(str(tmp_path)),
            input={"code": _hang_after("FATAL: port in use"), "timeout": 0.5},
        )
    finally:
        await session.aclose()
    assert out.is_error
    assert "FATAL: port in use" in out.text and "printed before the timeout" in out.text


async def test_bash_in_a_cell_times_out_under_the_ceiling_with_its_output(tmp_path) -> None:
    """The kernel cancels a host call at its ceiling, which discards Bash's
    capture. In-kernel Bash runs under a timeout just below that ceiling, so
    its own timeout fires first and the output it printed comes back."""
    registry = ToolRegistry()
    registry.register_builtin(bash.SPEC, bash.handler)
    session = CodeModeSession(registry, config=KernelConfig(host_call_timeout=4))
    loop = _Loop(str(tmp_path))
    command = f'"{sys.executable}" -c "print(\'FATAL: db locked\', flush=True); import time; time.sleep(60)"'
    code = f"from tools.Bash import Bash\nBash(command={command!r}, timeout=60)\n"
    try:
        out = await session.run_python(sid="s", turn_id="t", loop=loop, input={"code": code, "timeout": 2})
    finally:
        await session.aclose()
    results = [e for e in loop.events if isinstance(e, ToolResult) and e.name == "Bash"]
    assert len(results) == 1 and results[0].is_error
    assert "timed out after 3s" in results[0].output
    assert "FATAL: db locked" in results[0].output and "partial output" in results[0].output
    assert "cancelled before it returned" not in results[0].output
    assert "FATAL: db locked" in out.text


def test_bash_under_ceiling_only_lowers_a_timeout_that_would_be_cancelled() -> None:
    assert _bash_under_ceiling({"command": "x"}, 0) == {"command": "x"}  # unbounded
    assert _bash_under_ceiling({"command": "x"}, 900) == {"command": "x"}  # default 120 fits
    assert _bash_under_ceiling({"command": "x", "timeout": 600}, 900)["timeout"] == 600
    assert _bash_under_ceiling({"command": "x"}, 100) == {"command": "x", "timeout": 90}
    assert _bash_under_ceiling({"command": "x", "timeout": 10}, 8) == {"command": "x", "timeout": 6}
    assert _bash_under_ceiling({"command": "x", "timeout": 5}, 1) == {"command": "x", "timeout": 5}
