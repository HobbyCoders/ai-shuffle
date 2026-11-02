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
import shutil
import signal
import sys
import time
import warnings
from pathlib import Path

import pytest
from fluid.engine import delegated
from fluid.engine.delegated import DelegatedLoop
from fluid.store.base import MemoryStore
from fluid.tools.registry import ToolOutcome, ToolRegistry, ToolSpec

from app.core.provider.harness_adapter import HarnessAdapter
from app.core.session_pool import SessionPool, SessionState

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
# A CLI that exits when its stdin closes.
_EXITS_ON_EOF = "import sys\nprint('up', flush=True)\nsys.stdin.read()\n"
# The same, but a child that ignores SIGTERM keeps the CLI's stdout open after it.
_CHILD_HOLDS_STDOUT = """
import subprocess, sys
child = subprocess.Popen([sys.executable, "-c", (
    "import os, signal, time\\n"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\\n"
    "print(os.getpid(), flush=True)\\n"
    "time.sleep(60)\\n"
)])
sys.stdin.read()
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
    assert loop._socket_server not in delegated._OPEN_BRIDGE_SERVERS
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


class _SlowJournal:
    def __init__(self) -> None:
        self.started = False
        self.finished = False

    async def aclose(self) -> None:
        self.started = True
        await asyncio.sleep(30)
        self.finished = True


@posix_only
@pytest.mark.asyncio
async def test_a_cancelled_close_does_not_start_the_journal_drain():
    """The drain has a 90 second budget of its own; the caller's is much shorter."""
    loop = await _loop_with_bridge()
    loop._journal = journal = _SlowJournal()
    proc = await _spawn(loop, _STUBBORN)
    await proc.stdout.readline()

    started = time.monotonic()
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(loop.aclose(), timeout=0.2)

    assert time.monotonic() - started < 3
    assert not journal.started


@posix_only
@pytest.mark.asyncio
async def test_a_deadline_cuts_the_journal_drain_short_after_a_clean_stop():
    loop = await _loop_with_bridge()
    socket_path, tmpdir = loop._socket_path, loop._tmpdir
    loop._journal = journal = _SlowJournal()

    started = time.monotonic()
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(loop.aclose(), timeout=0.2)

    assert time.monotonic() - started < 3
    assert journal.started and not journal.finished
    _assert_released(loop, socket_path, tmpdir)


@posix_only
@pytest.mark.asyncio
@pytest.mark.parametrize("source, exits_cleanly", [(_STUBBORN, False), (_EXITS_ON_EOF, True)])
async def test_a_deadline_before_the_process_stop_still_ends_the_cli(monkeypatch, source, exits_cleanly):
    """A slow SessionEnd hook used up the deadline before the CLI was reached."""
    loop = await _loop_with_bridge()
    proc = await _spawn(loop, source)
    await proc.stdout.readline()

    async def slow_hook():
        await asyncio.sleep(30)

    monkeypatch.setattr(loop, "_emit_session_end", slow_hook)
    started = time.monotonic()
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(loop.aclose(), timeout=0.2)

    assert time.monotonic() - started < 5
    assert proc.returncode is not None, "the CLI outlived its close"
    assert loop._proc is None
    # The CLI still gets a moment to exit by itself, so it can finish its writes.
    assert (proc.returncode == 0) is exits_cleanly


@posix_only
@pytest.mark.asyncio
@pytest.mark.parametrize("ends_by", ["deadline", "grace"])
async def test_a_child_holding_the_exited_clis_stdout_still_goes(ends_by):
    """``proc.wait()`` stays pending for the pipe although the CLI has exited."""
    loop = DelegatedLoop(store=MemoryStore(), model="opus", claude_cmd=["claude"])
    proc = await _spawn(loop, _CHILD_HOLDS_STDOUT)
    child_pid = int((await proc.stdout.readline()).strip())

    try:
        if ends_by == "deadline":
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(loop.aclose(), timeout=0.5)
        else:
            await loop._stop_process(grace=0.5)
        assert proc.returncode == 0, "the stand-in CLI should have exited on its own"
        for _ in range(150):
            if not _alive(child_pid):
                break
            await asyncio.sleep(0.02)
        assert not _alive(child_pid), "a process the CLI left behind survived the close"
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass


@posix_only
@pytest.mark.asyncio
@pytest.mark.parametrize("removal", ["remove_dead", "evict_dead", "remove", "evict"])
async def test_dropping_a_dead_session_frees_its_engine_and_publishes_nothing(monkeypatch, removal):
    """A dead CLI does not close the parent's side of the bridge.

    A replacement engine may already be numbering this session's events, so the
    old one must not write any: not from the journal, and not from a hook.
    """
    ended_workflows = []
    published = []

    async def fake_close(session_id):
        ended_workflows.append(session_id)

    async def spy_emit(event):
        published.append(event)

    async def spy_session_end():
        published.append("session end")

    monkeypatch.setattr("app.core.swarm.close", fake_close)
    loop = await _loop_with_bridge()
    socket_path, tmpdir = loop._socket_path, loop._tmpdir
    loop._journal = journal = _SlowJournal()
    monkeypatch.setattr(loop, "_emit", spy_emit)
    monkeypatch.setattr(loop, "_emit_session_end", spy_session_end)
    proc = await _spawn(loop, _STUBBORN)
    await proc.stdout.readline()
    adapter = HarnessAdapter()
    adapter._engine = loop
    assert not adapter.is_alive()
    state = SessionState(adapter=adapter, is_connected=True)
    pool = SessionPool()
    pool.put("dead", state)

    if removal == "remove_dead":
        await pool.remove_dead("dead")
    elif removal == "evict_dead":
        assert await pool.evict_dead() == ["dead"]
    elif removal == "remove":
        await pool.remove("dead")
    else:
        await pool._do_evict("dead", state)
    if pool._background_teardowns:
        await asyncio.gather(*pool._background_teardowns)

    _assert_released(loop, socket_path, tmpdir)
    assert proc.returncode is not None, "the CLI outlived the session being dropped"
    assert not ended_workflows
    assert not journal.started
    assert not published


async def _turn_with_slow_cleanup() -> None:
    try:
        await asyncio.sleep(30)
    except asyncio.CancelledError:
        await asyncio.sleep(0.4)
        raise


@posix_only
@pytest.mark.asyncio
async def test_a_deadline_during_the_turns_cleanup_is_not_swallowed():
    loop = await _loop_with_bridge()
    socket_path, tmpdir = loop._socket_path, loop._tmpdir
    loop._journal = journal = _SlowJournal()
    loop._turn_task = asyncio.create_task(_turn_with_slow_cleanup())
    await asyncio.sleep(0)

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(loop.aclose(), timeout=0.05)

    assert not journal.started
    _assert_released(loop, socket_path, tmpdir)
    loop._turn_task.cancel()
    await asyncio.gather(loop._turn_task, return_exceptions=True)


@posix_only
@pytest.mark.asyncio
async def test_a_close_from_an_already_cancelled_task_still_finishes():
    """A turn's ``finally`` can close the loop; only a new cancel may cut it short."""
    loop = await _loop_with_bridge()
    loop._journal = journal = _SlowJournal()
    journal.aclose = _noted_quick_close(journal)
    loop._turn_task = asyncio.create_task(asyncio.sleep(30))
    await asyncio.sleep(0)

    async def closer():
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            await loop.aclose()
            raise

    task = asyncio.create_task(closer())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert loop._turn_task.cancelled()
    assert journal.finished


def _noted_quick_close(journal: _SlowJournal):
    async def aclose() -> None:
        journal.started = journal.finished = True

    return aclose


@posix_only
def test_a_bridge_nobody_closed_is_kept_from_the_collector():
    """Whatever path drops a loop unclosed, uvloop must not collect its server."""
    uvloop = pytest.importorskip("uvloop")

    async def scenario() -> None:
        already_open = set(delegated._OPEN_BRIDGE_SERVERS)
        loop = await _loop_with_bridge()
        socket_path, tmpdir = loop._socket_path, loop._tmpdir
        del loop
        gc.collect()
        await asyncio.sleep(0)
        try:
            _, writer = await asyncio.open_unix_connection(socket_path)
            writer.close()
        finally:
            for server in delegated._OPEN_BRIDGE_SERVERS - already_open:
                server.close()
                delegated._OPEN_BRIDGE_SERVERS.discard(server)
            shutil.rmtree(tmpdir, ignore_errors=True)

    event_loop = uvloop.new_event_loop()
    try:
        event_loop.run_until_complete(scenario())
    finally:
        event_loop.close()
