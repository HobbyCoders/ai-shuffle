"""Ctrl-C in the CLI: interrupt the turn first, quit on the second press.

The in-process tests call the REPL's handler directly or raise the signal on
the test process; the subprocess test delivers a real console signal to a
child REPL blocked on a pipe stdin and expects it to exit 130 promptly.
"""

import asyncio
import io
import os
import signal
import subprocess
import sys
import threading
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from fluid.cli.__main__ import run_once
from fluid.cli.repl import SIGINT_EXIT_CODE, LineReader, Repl
from fluid.config.loader import BUILTIN_DEFAULTS
from fluid.config.model import HarnessConfig
from fluid.engine.native import NativeLoop
from fluid.events.model import Interrupted
from fluid.providers.base import ProviderCaps, ProviderRequest, StreamChunk
from fluid.store.base import MemoryStore

REPO_ROOT = Path(__file__).resolve().parents[2]


class BlockingProvider:
    """Streams nothing until the turn is interrupted; records that it started."""

    name = "anthropic"
    caps = ProviderCaps(explicit_cache_breakpoints=True, max_cache_breakpoints=4)

    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def stream(self, request: ProviderRequest) -> AsyncIterator[StreamChunk]:
        self.started.set()
        yield StreamChunk("message_start", {"usage": {"input_tokens": 10}})
        await asyncio.Event().wait()


def _on_main_thread() -> bool:
    return threading.current_thread() is threading.main_thread()


async def test_line_reader_keeps_the_line_typed_after_an_abandoned_read() -> None:
    release = threading.Event()
    lines = iter(["hello", "world"])

    def input_fn(prompt: str) -> str:
        release.wait(5)
        return next(lines)

    reader = LineReader(input_fn)
    first = reader.read("> ")
    await asyncio.sleep(0.05)
    reader.abandon()
    assert await first == ""

    out = io.StringIO()
    second = reader.read("> ", out=out)
    assert out.getvalue() == "> "  # the blocked call cannot print the new prompt
    release.set()
    assert await asyncio.wait_for(second, 5) == "hello"
    assert await asyncio.wait_for(reader.read("> "), 5) == "world"


async def test_first_sigint_interrupts_the_turn_and_keeps_the_session() -> None:
    provider = BlockingProvider()
    store = MemoryStore()
    sid = (await store.create_session()).session_id
    engine = NativeLoop(provider=provider, store=store, model="m", session_id=sid)
    out = io.StringIO()
    repl = Repl(engine, sid, input_fn=lambda _: "", out=out)

    turn = asyncio.create_task(repl.run_turn("hi"))
    await asyncio.wait_for(provider.started.wait(), 5)
    repl.on_sigint()
    assert await asyncio.wait_for(turn, 5) == "interrupted"
    assert any(isinstance(e, Interrupted) for e in engine.bus.log(sid))
    assert "[interrupted]" in out.getvalue()
    assert repl.exit_code == 0
    assert not repl._exit_requested

    # At the prompt, the next press asks to quit.
    repl.on_sigint()
    assert repl._exit_requested
    assert repl.exit_code == SIGINT_EXIT_CODE
    await engine.aclose()


async def test_second_sigint_during_a_turn_ends_the_repl_with_130() -> None:
    provider = BlockingProvider()
    store = MemoryStore()
    sid = (await store.create_session()).session_id
    engine = NativeLoop(provider=provider, store=store, model="m", session_id=sid)
    started = threading.Event()
    block = threading.Event()

    def input_fn(prompt: str) -> str:
        if not started.is_set():
            started.set()
            return "hi"
        block.wait(5)
        return "/exit"

    repl = Repl(engine, sid, input_fn=input_fn, out=io.StringIO())
    loop_task = asyncio.create_task(repl.run())
    await asyncio.wait_for(provider.started.wait(), 5)
    repl.on_sigint()
    repl.on_sigint()
    assert await asyncio.wait_for(loop_task, 5) == SIGINT_EXIT_CODE
    assert any(isinstance(e, Interrupted) for e in engine.bus.log(sid))
    block.set()
    await engine.aclose()


async def test_sigint_at_the_prompt_ends_the_repl_with_130() -> None:
    store = MemoryStore()
    sid = (await store.create_session()).session_id
    engine = NativeLoop(provider=BlockingProvider(), store=store, model="m", session_id=sid)
    block = threading.Event()

    def input_fn(prompt: str) -> str:
        block.wait(5)
        return "/exit"

    repl = Repl(engine, sid, input_fn=input_fn, out=io.StringIO())
    loop_task = asyncio.create_task(repl.run())
    await asyncio.sleep(0.05)
    repl.on_sigint()
    assert await asyncio.wait_for(loop_task, 5) == SIGINT_EXIT_CODE
    block.set()
    await engine.aclose()


async def test_run_once_sigint_interrupts_and_exits_130() -> None:
    if not _on_main_thread():
        pytest.skip("signal handlers can only be installed on the main thread")
    provider = BlockingProvider()
    out = io.StringIO()
    config = HarnessConfig.from_dict(BUILTIN_DEFAULTS)
    task = asyncio.create_task(run_once(config, MemoryStore(), "hi", provider=provider, out=out))
    await asyncio.wait_for(provider.started.wait(), 5)
    signal.raise_signal(signal.SIGINT)
    assert await asyncio.wait_for(task, 10) == SIGINT_EXIT_CODE
    assert "[interrupted]" in out.getvalue()


_DRIVER = '''
import asyncio, sys
from fluid.cli.repl import Repl
from fluid.engine.native import NativeLoop
from fluid.providers.base import ProviderCaps
from fluid.store.base import MemoryStore


class Provider:
    name = "anthropic"
    caps = ProviderCaps(explicit_cache_breakpoints=True, max_cache_breakpoints=4)

    async def stream(self, request):
        raise AssertionError("no turn runs in this test")
        yield


async def main() -> int:
    store = MemoryStore()
    sid = (await store.create_session()).session_id
    engine = NativeLoop(provider=Provider(), store=store, model="m", session_id=sid)
    print("READY", flush=True)
    code = await Repl(engine, sid).run()
    await engine.aclose()
    return code


sys.exit(asyncio.run(main()))
'''


def test_console_sigint_at_the_prompt_exits_130_promptly(tmp_path) -> None:
    """A real console signal to a REPL blocked on a pipe stdin: exit 130 in
    seconds, with no traceback. The reader thread is a daemon, so shutdown
    does not wait for the line that never comes."""
    driver = tmp_path / "driver.py"
    driver.write_text(_DRIVER, encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT), "PYTHONUNBUFFERED": "1"}
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    proc = subprocess.Popen(
        [sys.executable, str(driver)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        cwd=str(REPO_ROOT), env=env, creationflags=flags,
    )
    try:
        ready = threading.Event()

        def wait_ready() -> None:
            for raw in proc.stdout:
                if raw.startswith(b"READY"):
                    ready.set()
                    return

        threading.Thread(target=wait_ready, daemon=True).start()
        if not ready.wait(30):
            pytest.fail("child never reached the prompt: " + proc.stderr.read().decode(errors="replace"))
        # The prompt is printed by input() from the reader thread; give it a
        # moment to block there, which is the state the bug hung in.
        threading.Event().wait(0.5)
        sig = getattr(signal, "CTRL_BREAK_EVENT", signal.SIGINT)
        try:
            proc.send_signal(sig)
        except OSError as exc:
            pytest.skip(f"cannot deliver a console signal here: {exc}")
        try:
            code = proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pytest.fail("child did not exit within 10s of the signal")
        stderr = proc.stderr.read().decode(errors="replace")
    finally:
        if proc.poll() is None:
            proc.kill()
    assert code == SIGINT_EXIT_CODE, stderr
    assert "Traceback" not in stderr
