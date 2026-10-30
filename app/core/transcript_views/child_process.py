"""The credential-free launch/termination boundary for private projection children."""
from __future__ import annotations

import asyncio
import os
import subprocess

SAFE_ENV_KEYS = frozenset({"PATH", "Path", "SystemRoot", "SYSTEMROOT", "WINDIR", "TMP", "TEMP"})


async def launch(*command: str, limit: int) -> asyncio.subprocess.Process:
    environment = {key: value for key, value in os.environ.items() if key in SAFE_ENV_KEYS}
    options = {}
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.BELOW_NORMAL_PRIORITY_CLASS
    process = await asyncio.create_subprocess_exec(
        *command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, env=environment, close_fds=True,
        limit=limit, **options)
    if os.name == "posix":
        try:
            os.setpriority(os.PRIO_PROCESS, process.pid, 10)
        except OSError:
            pass
    return process


async def stop(process: asyncio.subprocess.Process | None) -> None:
    if process is None:
        return
    if process.stdin is not None:
        process.stdin.close()
    if process.returncode is None:
        try:
            process.terminate()
        except ProcessLookupError:
            pass
    try:
        await asyncio.wait_for(process.wait(), 1.0)
    except asyncio.TimeoutError:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        await process.wait()
