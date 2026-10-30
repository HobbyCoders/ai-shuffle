"""The credential-free launch/termination boundary for private projection children."""
from __future__ import annotations

import asyncio
import importlib.util
import os
import subprocess
import sys
from functools import cache
from pathlib import Path
from typing import Literal

SAFE_ENV_KEYS = frozenset({"PATH", "Path", "SystemRoot", "SYSTEMROOT", "WINDIR", "TMP", "TEMP"})


@cache
def _job_object():
    # Reuse Fluid's stdlib-only job helper without importing its tool registry
    # (or application configuration) in this lightweight launch boundary.
    package = importlib.util.find_spec("fluid")
    if package is None:
        raise OSError("projection_parent_ownership_failed")
    if package.origin is not None:
        source = Path(package.origin).parent / "tools/job_object.py"
    else:
        # Root-only source invocations see the outer fluid/ namespace, not the
        # inner package. Do not import that namespace or the tool registry.
        source = Path(__file__).resolve().parents[3] / "fluid/fluid/tools/job_object.py"
    spec = importlib.util.spec_from_file_location("_projection_windows_job", source)
    if spec is None or spec.loader is None:
        raise OSError("projection_parent_ownership_failed")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _windows_command(command: tuple[str, ...], runtime: Literal["python", "node"]) -> tuple[str, ...]:
    # The final argument is the checked-in script, with interpreter flags before
    # it. Neither gate imports startup/site/application code before admission.
    gate = Path(__file__).with_name(f"windows_{runtime}_child." + ("py" if runtime == "python" else "cjs"))
    flags = ("-S",) if runtime == "python" else ()
    return (*command[:-1], *flags, str(gate), command[-1])


async def launch(*command: str, limit: int, runtime: Literal["python", "node"]) -> asyncio.subprocess.Process:
    environment = {key: value for key, value in os.environ.items() if key in SAFE_ENV_KEYS}
    options = {}
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.BELOW_NORMAL_PRIORITY_CLASS
        command = _windows_command(command, runtime)
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
            # The Windows venv executable can be a redirector that already
            # started a real interpreter. Both are inert until G; enroll both
            # known PIDs, not a racy snapshot of an arbitrarily growing tree.
            identity = await asyncio.wait_for(process.stdout.readexactly(4), 5.0)
            runtime_pid = int.from_bytes(identity, "big")
            if runtime_pid <= 0:
                raise OSError("projection_invalid_runtime_identity")
            extra = (runtime_pid,) if runtime_pid != process.pid else ()
            job = _job_object().assign_kill_on_close_job(process.pid, *extra)
            if job is None:
                raise OSError("projection_parent_ownership_failed")
            # The handle is parent-only/non-inheritable. Abrupt parent death
            # closes it in the kernel even if the child holds the Python GIL.
            setattr(process, "_projection_job", job)
            # Only now may site hooks/entrypoint create descendants. Raw gates
            # consume exactly this byte, leaving framed protocol bytes intact.
            process.stdin.write(b"G")
            await process.stdin.drain()
        except BaseException:
            await stop(process)  # EOF + kill/reap, closing an attached job once.
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
