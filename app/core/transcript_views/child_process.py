"""The credential-free launch/termination boundary for private projection children."""
from __future__ import annotations

import asyncio
import importlib.util
import os
import subprocess
import sys
from functools import cache
from pathlib import Path

SAFE_ENV_KEYS = frozenset({"PATH", "Path", "SystemRoot", "SYSTEMROOT", "WINDIR", "TMP", "TEMP"})


@cache
def _job_object():
    # Reuse Fluid's stdlib-only job helper without importing its tool registry
    # (or application configuration) in this lightweight launch boundary.
    package = importlib.util.find_spec("fluid")
    if package is None or package.origin is None:
        raise OSError("projection_parent_ownership_failed")
    source = Path(package.origin).parent / "tools/job_object.py"
    spec = importlib.util.spec_from_file_location("_projection_windows_job", source)
    if spec is None or spec.loader is None:
        raise OSError("projection_parent_ownership_failed")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def launch(*command: str, limit: int) -> asyncio.subprocess.Process:
    environment = {key: value for key, value in os.environ.items() if key in SAFE_ENV_KEYS}
    options = {}
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.BELOW_NORMAL_PRIORITY_CLASS
    elif sys.platform == "linux":
        # exec preserves PID, pipes and the kernel parent-death signal. Unlike
        # preexec_fn, this is safe in the multithreaded serving process.
        command = (sys.executable, "-I", "-S", str(Path(__file__).with_name("linux_child.py")),
                   str(os.getpid()), *command)
    process = await asyncio.create_subprocess_exec(
        *command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, env=environment, close_fds=True,
        limit=limit, **options)
    if os.name == "nt":
        try:
            job = _job_object().assign_kill_on_close_job(process.pid)
            if job is None:
                raise OSError("projection_parent_ownership_failed")
            # The handle is parent-only/non-inheritable. Abrupt parent death
            # closes it in the kernel even if the child holds the Python GIL.
            setattr(process, "_projection_job", job)
        except BaseException:
            process.stdin.close()
            if process.returncode is None:
                process.kill()
            await process.wait()
            raise
    elif os.name == "posix":
        try:
            os.setpriority(os.PRIO_PROCESS, process.pid, 10)
        except OSError:
            pass
    return process


async def stop(process: asyncio.subprocess.Process | None) -> None:
    if process is None:
        return
    try:
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
    finally:
        job = getattr(process, "_projection_job", None)
        if job is not None:
            setattr(process, "_projection_job", None)
            _job_object().close_job(job)
