"""A delegated loop must give back its OS resources however its close ends.

The app closes a loop under a deadline (``HarnessAdapter.disconnect_transport``).
When the CLI was slow to exit, that deadline cancelled ``aclose()`` before it
closed the bridge's socket server, and the loop was then dropped. uvloop keeps
only TCP servers alive, so the garbage collector took the unix one: Python's
socket finalizer closed the descriptor, then uvloop's handle closed the same
number again. A file a worker thread opened in that gap lost its descriptor,
which is how a static-asset response came out the wrong length.
"""

from __future__ import annotations

import asyncio
import gc
import os
import signal
import sys
import warnings
from pathlib import Path

import pytest
from fluid.engine.delegated import DelegatedLoop
from fluid.store.base import MemoryStore
from fluid.tools.registry import ToolOutcome, ToolRegistry, ToolSpec

posix_only = pytest.mark.skipif(os.name == "nt", reason="exercises unix sockets and process groups")

# A stand-in CLI that does not exit when its stdin closes.
_STUBBORN = "import time\nprint('up', flush=True)\ntime.sleep(60)\n"
# A CLI that exits at once, leaving a child in its group that ignores SIGTERM.
_LEAVES_CHILD = """
import signal, subprocess, sys
child = subprocess.Popen([sys.executable, "-c", (
    "import signal, time\\n"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\\n"
    "print('up', flush=True)\\n"
    "time.sleep(60)\\n"
)], stdout=subprocess.PIPE, text=True)
child.stdout.readline()
print(child.pid, flush=True)
"""


def _registry() -> ToolRegistry:
    async def handler(input, ctx):
        return ToolOutcome(text="ok")

    registry = ToolRegistry()
    registry.register_builtin(
        ToolSpec(name="mcp__builtins__Probe", description="Probe", input_schema={}), handler,
    )
    return registry


async def _loop_with_bridge() -> DelegatedLoop:
    loop = DelegatedLoop(store=MemoryStore(), model="opus", registry=_registry(), claude_cmd=["claude"])
    await loop._start_bundled_server()
    assert loop._socket_server is not None and loop._socket_server.is_serving()
    assert os.path.exists(loop._socket_path)
    return loop


async def _spawn(loop: DelegatedLoop, source: str) -> asyncio.subprocess.Process:
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", source,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    loop._proc = proc
    return proc


def _assert_released(loop: DelegatedLoop, socket_path: str, tmpdir: str) -> None:
    assert not loop._socket_server.is_serving()
    assert not os.path.exists(socket_path)
    assert not Path(tmpdir).exists()


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except OSError:
        return True
    return state != "Z"


@posix_only
@pytest.mark.asyncio
async def test_a_callers_deadline_still_ends_the_cli_and_frees_the_bridge():
    loop = await _loop_with_bridge()
    socket_path, tmpdir = loop._socket_path, loop._tmpdir
    proc = await _spawn(loop, _STUBBORN)
    await proc.stdout.readline()
    closed_bus = []
    real_close = loop.bus.close

    async def spy_close():
        closed_bus.append(True)
        await real_close()

    loop.bus.close = spy_close

    # The real caller's shape: an outer timeout that fires during the CLI's grace.
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(loop.aclose(), timeout=0.3)

    assert proc.returncode is not None, "the CLI outlived its close"
    _assert_released(loop, socket_path, tmpdir)
    assert closed_bus == [True]


@posix_only
@pytest.mark.asyncio
async def test_a_failing_process_stop_still_frees_the_bridge(monkeypatch):
    loop = await _loop_with_bridge()
    socket_path, tmpdir = loop._socket_path, loop._tmpdir

    async def boom(**_kwargs):
        raise RuntimeError("stop failed")

    monkeypatch.setattr(loop, "_stop_process", boom)
    with pytest.raises(RuntimeError, match="stop failed"):
        await loop.aclose()

    _assert_released(loop, socket_path, tmpdir)


@posix_only
@pytest.mark.asyncio
async def test_a_deadline_during_group_cleanup_still_kills_what_the_cli_left():
    loop = DelegatedLoop(store=MemoryStore(), model="opus", claude_cmd=["claude"])
    proc = await _spawn(loop, _LEAVES_CHILD)
    child_pid = int((await proc.stdout.readline()).strip())
    await proc.wait()
    assert _alive(child_pid)

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(loop.aclose(), timeout=0.3)

    for _ in range(100):
        if not _alive(child_pid):
            break
        await asyncio.sleep(0.02)
    try:
        assert not _alive(child_pid), "a process the CLI left behind survived the close"
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass


@posix_only
def test_a_dropped_loop_leaves_uvloop_no_unclosed_server_to_collect():
    """The failing shape itself: real uvloop, close cut short, loop dropped."""
    uvloop = pytest.importorskip("uvloop")

    async def scenario() -> None:
        loop = await _loop_with_bridge()
        proc = await _spawn(loop, _STUBBORN)
        await proc.stdout.readline()
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(loop.aclose(), timeout=0.3)
        del loop, proc
        gc.collect()
        await asyncio.sleep(0)

    event_loop = uvloop.new_event_loop()
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            event_loop.run_until_complete(scenario())
            gc.collect()
    finally:
        event_loop.close()

    unclosed = [str(w.message) for w in caught if "Server" in str(w.message) and "unclosed" in str(w.message)]
    assert not unclosed, unclosed
