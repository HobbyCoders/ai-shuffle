"""Closing a busy delegated CLI interrupts its reply before sending EOF."""
from __future__ import annotations

import asyncio
import json
import sys
import time

import pytest

from fluid.engine.delegated import DelegatedLoop
from fluid.events.model import ErrorEvent, Interrupted
from fluid.events.ops import BackgroundTaskFinished, RespondQuestion, UserTurn
from fluid.store.base import MemoryStore
from fluid.tools.registry import BackgroundTaskReceipt


async def _spawn(loop, source):
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-u", "-c", source,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        start_new_session=sys.platform != "win32",
    )
    loop._proc = proc
    assert await proc.stdout.readline() == b"ready\n"
    return proc


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["direct", "question", "background"])
async def test_close_interrupts_a_reply_that_eof_does_not_abort(tmp_path, monkeypatch, source):
    receipt = tmp_path / "interrupt.json"
    loop = DelegatedLoop(store=MemoryStore(), model="fixture")
    # Like stream-json, EOF stops accepting input but does not abort the current
    # provider reply. An interrupt frame does. This is an actual pipe/process,
    # not a mocked _send_interrupt or Process.wait.
    program = f"""
import json, sys, time
print('ready', flush=True)
interrupted = False
for line in sys.stdin:
    frame = json.loads(line)
    if frame.get('request', {{}}).get('subtype') == 'interrupt':
        with open({str(receipt)!r}, 'w') as f:
            json.dump(frame, f)
        interrupted = True
if not interrupted:
    time.sleep(60)
"""
    proc = await _spawn(loop, program)
    try:
        if source == "direct":
            loop._turn_task = asyncio.create_task(asyncio.sleep(60))
        else:
            started = asyncio.Event()

            async def reply(op):
                started.set()
                await asyncio.Event().wait()

            monkeypatch.setattr(loop, "_run_turn", reply)
            if source == "question":
                # Drive the real question producer, answer and wake. Its wake task
                # directly awaits the reply: cancelling the wake cancels the turn.
                [request_id] = await loop._post_questions([{"question": "Continue?"}])
                await loop.submit(RespondQuestion(request_id=request_id, answer="yes"))
            else:
                # The background completion launcher does NOT await its turn, but
                # the turn it creates must get the same close-time interrupt.
                await loop._background_task_started(BackgroundTaskReceipt(task_id="background-result"))
                await loop.submit(BackgroundTaskFinished(task_id="background-result", summary="done"))
            await asyncio.wait_for(started.wait(), timeout=2)
        start = time.monotonic()
        await asyncio.wait_for(loop.aclose(), timeout=2)
        assert time.monotonic() - start < 2
        assert proc.returncode == 0
        assert json.loads(receipt.read_text())["type"] == "control_request"
        assert loop._turn_task.done()
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()


@pytest.mark.asyncio
async def test_idle_close_keeps_the_clean_eof_path():
    loop = DelegatedLoop(store=MemoryStore(), model="fixture")
    proc = await _spawn(loop, "import sys\nprint('ready')\nassert sys.stdin.read() == ''\n")
    await asyncio.wait_for(loop.aclose(), timeout=2)
    assert proc.returncode == 0


@pytest.mark.asyncio
async def test_a_blocked_interrupt_write_cannot_hold_close(monkeypatch):
    loop = DelegatedLoop(store=MemoryStore(), model="fixture")
    loop._turn_task = asyncio.create_task(asyncio.sleep(60))
    attempted = asyncio.Event()

    async def blocked_interrupt():
        attempted.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(loop, "_send_interrupt", blocked_interrupt)
    await asyncio.wait_for(loop.aclose(), timeout=2)
    assert attempted.is_set()
    assert loop._turn_task.done()


@pytest.mark.asyncio
async def test_outer_cancellation_during_interrupt_still_reaps_the_cli(monkeypatch):
    loop = DelegatedLoop(store=MemoryStore(), model="fixture")
    proc = await _spawn(loop, "import sys\nprint('ready')\nsys.stdin.read()\n")
    loop._turn_task = asyncio.create_task(asyncio.sleep(60))
    attempted = asyncio.Event()
    closed_bus = []
    real_close = loop.bus.close

    async def blocked_interrupt():
        attempted.set()
        await asyncio.Event().wait()

    async def close_bus():
        closed_bus.append(True)
        await real_close()

    monkeypatch.setattr(loop, "_send_interrupt", blocked_interrupt)
    monkeypatch.setattr(loop.bus, "close", close_bus)
    closing = asyncio.create_task(loop.aclose())
    try:
        await asyncio.wait_for(attempted.wait(), timeout=2)
        # Same cancellation as the adapter's outer wait_for deadline, delivered
        # while the bounded write is suspended rather than after EOF.
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(closing, timeout=2)
        assert proc.returncode == 0
        assert loop._proc is None
        assert loop._turn_task.done()
        assert loop._background_closed
        assert closed_bus == [True]
    finally:
        if not closing.done():
            closing.cancel()
            await asyncio.gather(closing, return_exceptions=True)
        if proc.returncode is None:
            proc.kill()
            await proc.wait()


@pytest.mark.asyncio
async def test_close_maps_interrupt_result_without_discarding_a_valid_resume(monkeypatch):
    store = MemoryStore()
    loop = DelegatedLoop(store=store, model="fixture", cli_session_id="cli-prev")
    loop._spawn_resume = "cli-prev"
    # The first turn on a resumed CLI has no mapped nodes yet. Its error result
    # on interrupt is NOT evidence that the valid resume handle was rejected.
    proc = await _spawn(loop, """
import json, sys
print('ready', flush=True)
for line in sys.stdin:
    frame = json.loads(line)
    if frame.get('request', {}).get('subtype') == 'interrupt':
        print(json.dumps({'type': 'result', 'subtype': 'error_during_execution',
                          'is_error': True, 'errors': ['interrupted']}), flush=True)
""")
    user_written = asyncio.Event()
    interrupt_processed = asyncio.Event()
    frames = []
    restarts = []
    real_write = loop._write_frame
    real_interrupt = loop._send_interrupt
    real_ensure = loop._ensure_process

    async def write_frame(frame):
        await real_write(frame)
        frames.append(frame)
        if frame.get("type") == "user":
            user_written.set()

    async def interrupt_while_reader_runs():
        await real_interrupt()
        # A blocked drain can yield after the frame was written. Hold that
        # window open until the real _run_turn/_drive_turn/mapper processes the
        # subprocess result, not an arbitrary sleep or a mocked turn reader.
        await asyncio.shield(loop._turn_task)
        interrupt_processed.set()

    async def ensure_existing_process():
        if loop._proc is None:
            restarts.append(True)
            raise RuntimeError("unexpected replacement CLI during close")
        return await real_ensure()

    monkeypatch.setattr(loop, "_write_frame", write_frame)
    monkeypatch.setattr(loop, "_send_interrupt", interrupt_while_reader_runs)
    monkeypatch.setattr(loop, "_ensure_process", ensure_existing_process)
    try:
        sid = (await loop._ensure_session()).session_id
        await loop.submit(UserTurn(content="continue the existing conversation"))
        await asyncio.wait_for(user_written.wait(), timeout=2)
        await asyncio.wait_for(loop.aclose(), timeout=2)
        assert interrupt_processed.is_set()
        assert loop.cli_session_id == "cli-prev"
        assert restarts == []
        assert len([f for f in frames if f.get("type") == "user"]) == 1
        events = await store.events_since(sid, 0)
        assert len([event for event in events if isinstance(event, Interrupted)]) == 1
        assert not any(isinstance(event, ErrorEvent) for event in events)
        assert proc.returncode == 0
    finally:
        if loop._turn_task is not None and not loop._turn_task.done():
            loop._turn_task.cancel()
            await asyncio.gather(loop._turn_task, return_exceptions=True)
        if proc.returncode is None:
            proc.kill()
            await proc.wait()


@pytest.mark.asyncio
async def test_a_claimed_background_wake_cannot_clear_close_interruption(monkeypatch):
    loop = DelegatedLoop(store=MemoryStore(), model="fixture")
    started = []
    interrupt_flags = []

    async def turn(op):
        started.append(op)
        await asyncio.Event().wait()

    async def interrupt():
        interrupt_flags.append(loop._interrupted.is_set())

    monkeypatch.setattr(loop, "_run_turn", turn)
    monkeypatch.setattr(loop, "_send_interrupt", interrupt)
    await loop._background_task_started(BackgroundTaskReceipt(task_id="background-result"))
    await loop.submit(BackgroundTaskFinished(task_id="background-result", summary="done"))
    # Claim inline so the real wake coroutine is queued but has not started.
    # Close enters before its next await; that queued wake must not clear the
    # flag while close's interrupt write lets other tasks run.
    await loop._claim_background_wake()
    await loop.aclose()
    assert started == []
    assert interrupt_flags == [True]
    assert loop._interrupted.is_set()
