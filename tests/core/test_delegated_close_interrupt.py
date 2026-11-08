"""Closing a busy delegated CLI interrupts its reply before sending EOF."""
from __future__ import annotations

import asyncio
import json
import sys
import time

import pytest

from fluid.engine.delegated import DelegatedLoop
from fluid.store.base import MemoryStore


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
async def test_close_interrupts_a_reply_that_eof_does_not_abort(tmp_path):
    receipt = tmp_path / "interrupt.json"
    loop = DelegatedLoop(store=MemoryStore(), model="fixture")
    # Like stream-json, EOF stops accepting input but does not abort the current
    # provider reply. An interrupt frame does. This is an actual pipe/process,
    # not a mocked _send_interrupt or Process.wait.
    source = f"""
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
    proc = await _spawn(loop, source)
    loop._turn_task = asyncio.create_task(asyncio.sleep(60))
    start = time.monotonic()
    try:
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
