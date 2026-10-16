"""adb, run as asyncio subprocesses with a deadline (never on the event loop)."""

from __future__ import annotations

import asyncio
import logging
import shlex
from dataclasses import dataclass
from typing import Optional, Sequence

from app.core.android_emulator.layout import SdkLayout

logger = logging.getLogger(__name__)


class AdbError(Exception):
    pass


@dataclass(frozen=True)
class Result:
    returncode: int
    stdout: bytes
    stderr: bytes

    @property
    def text(self) -> str:
        return self.stdout.decode("utf-8", "replace")

    @property
    def error_text(self) -> str:
        return (self.stderr or self.stdout).decode("utf-8", "replace").strip()


async def run(argv: Sequence[str], *, env: dict, timeout: float = 30.0,
              stdin: Optional[bytes] = None) -> Result:
    """Run ``argv`` and collect its output; past ``timeout`` it is killed."""
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(stdin), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise AdbError(f"{argv[1] if len(argv) > 1 else argv[0]} timed out after {timeout:g}s") from None
    except asyncio.CancelledError:
        proc.kill()
        raise
    return Result(proc.returncode or 0, out, err)


class Adb:
    """adb for one emulator serial."""

    def __init__(self, layout: SdkLayout, serial: str) -> None:
        self.layout = layout
        self.serial = serial

    def argv(self, *args: str) -> list:
        return [str(self.layout.adb), "-s", self.serial, *args]

    async def run(self, *args: str, timeout: float = 30.0, stdin: Optional[bytes] = None) -> Result:
        return await run(self.argv(*args), env=self.layout.env(), timeout=timeout, stdin=stdin)

    async def shell(self, command: str, *, timeout: float = 30.0) -> Result:
        """``adb exec-out`` keeps binary output intact and returns the command's own status."""
        return await self.run("exec-out", command, timeout=timeout)

    async def getprop(self, name: str, *, timeout: float = 10.0) -> str:
        result = await self.shell(f"getprop {shlex.quote(name)}", timeout=timeout)
        return result.text.strip()

    async def emu(self, *command: str, timeout: float = 10.0) -> Result:
        """An emulator console command (``adb emu``), e.g. ``rotate`` or ``kill``."""
        return await self.run("emu", *command, timeout=timeout)


async def start_server(layout: SdkLayout, timeout: float = 30.0) -> None:
    result = await run([str(layout.adb), "start-server"], env=layout.env(), timeout=timeout)
    if result.returncode != 0:
        raise AdbError(f"adb could not start: {result.error_text[:300]}")
