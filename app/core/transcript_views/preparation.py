"""One admitted CPU operation in a supervised, credential-free Python child.

Private bounded stdio, not multiprocessing: no inherited application entrypoint,
OS environment or resource tracker. Pickle carries only server-owned calls and
values over owned pipes, never bytes from an HTTP/worker/client transport.
"""
from __future__ import annotations

import asyncio
import pickle
import struct
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from app.core import transcript_replay as replay

from .child_process import launch, stop

T = TypeVar("T")
MAX_PREPARATION_BYTES = 32 * 1024 * 1024


class PreparationBusy(RuntimeError):
    pass


class PreparationUnavailable(RuntimeError):
    pass


async def join_worker(future: asyncio.Future):
    """Defer every cancellation until owned work has actually finished."""
    cancelled = False
    while not future.done():
        try:
            await asyncio.shield(future)
        except asyncio.CancelledError:
            cancelled = True
        except Exception:
            break
    if cancelled:
        if not future.cancelled():
            future.exception()
        raise asyncio.CancelledError
    return future.result()


def _packet(value) -> bytes:
    data = pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL)
    if len(data) > MAX_PREPARATION_BYTES:
        raise PreparationUnavailable("preparation_unavailable")
    return struct.pack("!I", len(data)) + data


class PreparationWorker:
    def __init__(self) -> None:
        self._process: asyncio.subprocess.Process | None = None
        self._future: asyncio.Task | None = None
        self._closing: asyncio.Task | None = None
        self._sequence = 0

    async def _stop(self) -> None:
        process, self._process = self._process, None
        await stop(process)

    async def _exchange(self, request):
        if self._process is None or self._process.returncode is not None:
            await self._stop()
            # -I ignores PYTHON* injection. The entrypoint adds only its own
            # checked-in root, never a caller/environment-provided import path.
            self._process = await launch(
                sys.executable, "-I", str(Path(__file__).with_name("preparation_child.py")),
                limit=MAX_PREPARATION_BYTES + 1)
        process = self._process
        assert process.stdin is not None and process.stdout is not None
        # Large marshaling remains off the serving loop, with cancellation
        # joined before the one admitted operation can release its slot.
        packet = await join_worker(asyncio.create_task(asyncio.to_thread(_packet, request)))
        process.stdin.write(packet)
        await process.stdin.drain()
        size, = struct.unpack("!I", await process.stdout.readexactly(4))
        if not 0 < size <= MAX_PREPARATION_BYTES:
            raise PreparationUnavailable("preparation_unavailable")
        data = await process.stdout.readexactly(size)
        value = await join_worker(asyncio.create_task(asyncio.to_thread(pickle.loads, data)))
        if (not isinstance(value, tuple) or len(value) != 3 or value[0] != self._sequence
                or type(value[1]) is not bool or (not value[1] and not isinstance(value[2], Exception))):
            raise PreparationUnavailable("preparation_unavailable")
        return value

    async def _execute(self, request):
        try:
            response = await asyncio.wait_for(self._exchange(request), 30.0)
        except (OSError, EOFError, ValueError, TypeError, pickle.PickleError,
                struct.error, asyncio.IncompleteReadError, asyncio.TimeoutError,
                PreparationUnavailable) as error:
            await self._stop()
            raise PreparationUnavailable("preparation_unavailable") from error
        if not response[1]:
            raise response[2]
        return response[2]

    async def run(self, call: Callable[..., T], *args, **kwargs) -> T:
        if self._closing is not None or (self._future is not None and not self._future.done()):
            raise PreparationBusy("preparation_busy")
        # Exactly two normalized switches cross the protocol, not raw env text.
        environment = {name: "1" if replay._perf_switch_on(name) else "0"
                       for name in (replay._COALESCE_PROGRESS_ENV, replay._SLIM_USAGE_ENV)}
        self._sequence += 1
        request = (self._sequence, call, args, kwargs, environment)
        self._future = asyncio.create_task(self._execute(request))
        return await join_worker(self._future)

    async def _shutdown(self) -> None:
        if self._future is not None:
            await asyncio.gather(self._future, return_exceptions=True)
        await self._stop()

    async def close(self) -> None:
        if self._closing is None:
            self._closing = asyncio.create_task(self._shutdown())
        await join_worker(self._closing)
