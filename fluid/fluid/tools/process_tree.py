"""End a shell-spawned command together with everything it started.

The process asyncio holds is only the root of a tree: the shell, whose
descendants run the real work and hold its output pipes. The tree is recorded
at spawn and ended through that record whether or not the root has already
exited, because a root that exited first is exactly the case where a
descendant still holds the pipes and runs on with no owner.

- POSIX: the command is spawned with ``start_new_session``, so its pid is the
  id of the process group every descendant inherits. The group outlives its
  leader while any member lives, and the kernel never hands out a pid that is
  still in use as a group id, so ``killpg(pid)`` still reaches the leftovers
  after the leader has been reaped.
- Windows: a kill-on-close Job Object taken at spawn (:func:`spawn_tree`) holds
  every descendant, including a grandchild whose parent already exited, which
  ``taskkill /T`` cannot reach (#436). A command that finishes normally has its
  job released (:func:`release_tree`), so nothing it left running on purpose
  dies with it, the same as on POSIX.

Every function here is best-effort: it never raises (except the caller's own
cancellation while it waits to reap).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import subprocess
import sys
from typing import Any

from fluid.tools.child_env import scrub_child_env
from fluid.tools.job_object import (
    assign_kill_on_close_job,
    close_job,
    job_active_processes,
    release_job,
    resume_process,
    terminate_job,
)

log = logging.getLogger(__name__)

#: How long :func:`kill_tree` waits to reap the root after the kill.
REAP_TIMEOUT_S = 5.0
#: How long ``kill_tree(confirm=True)`` waits for the whole tree to be gone.
EXIT_CONFIRM_S = 1.0

#: Windows ``CreateProcess`` flag: the primary thread starts suspended.
_CREATE_SUSPENDED = 0x00000004


async def spawn_tree(
    *argv: str, **kwargs: Any,
) -> tuple[asyncio.subprocess.Process, int | None]:
    """``asyncio.create_subprocess_exec`` with the tree recorded from the start.

    Returns ``(proc, job)``: pass ``job`` to :func:`kill_tree` or
    :func:`release_tree` exactly once. It is None on POSIX, where the caller's
    ``start_new_session`` makes the process group the record.

    Windows: the shell starts suspended, joins a kill-on-close job, and only
    then runs, so a child it spawns at once is in the job too. Assigning the
    job after a running start left that child outside it, alive after Stop
    and holding the pipes. When no job can be had the process still runs
    (the kill falls back to ``taskkill /T``); when it cannot be resumed it
    is killed and the error raised, never left suspended.
    """
    if sys.platform != "win32":
        return await asyncio.create_subprocess_exec(*argv, **kwargs), None
    kwargs["creationflags"] = int(kwargs.get("creationflags") or 0) | _CREATE_SUSPENDED
    proc = await asyncio.create_subprocess_exec(*argv, **kwargs)
    # Known gap: if this process dies (crash, hard kill) between the spawn
    # above and the job assignment below, the shell stays suspended outside
    # any job, so nothing ends it. It runs no command (it never resumed) and
    # holds only its pipes and memory until it is killed by hand or the
    # machine restarts. The window is a few system calls long.
    job = assign_kill_on_close_job(proc.pid)
    try:
        resume_process(proc.pid)
    except BaseException:
        close_job(job)  # ends the suspended process when it is in the job
        with contextlib.suppress(ProcessLookupError, OSError):
            proc.kill()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), REAP_TIMEOUT_S)
        raise
    return proc, job


def release_tree(job: int | None) -> None:
    """A command that finished on its own: let go of its job without killing."""
    release_job(job)


def _kill_group(pid: int, *, leader_reaped: bool) -> None:
    """SIGKILL the process group ``pid`` leads (POSIX only)."""
    if not isinstance(pid, int) or pid <= 0:
        return
    if leader_reaped:
        # A live process holding the pid again means the old group is empty:
        # the kernel does not reuse a pid while it is a group id.
        try:
            os.kill(pid, 0)
            return
        except ProcessLookupError:
            pass
        except OSError:
            return
    try:
        os.killpg(pid, signal.SIGKILL)
    except OSError:
        pass  # no such group (nothing left) or not ours to signal


async def _confirm_exit(job: int) -> bool:
    """True once the job reports no active process, within ``EXIT_CONFIRM_S``.

    Windows only. On POSIX nothing can prove a tree exited: a descendant that
    called ``setsid``, or one a restricted ``/proc`` hides, is out of sight of
    any group or session check, so a kill there is only ever "attempted".
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + EXIT_CONFIRM_S
    while True:
        active = job_active_processes(job)
        if active is None:
            return False
        if active == 0:
            return True
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(0.05)


async def kill_tree(
    proc: asyncio.subprocess.Process,
    job: int | None = None,
    *,
    reap_timeout: float = REAP_TIMEOUT_S,
    confirm: bool = False,
) -> bool:
    """Kill ``proc`` and every descendant, whether or not ``proc`` has exited.

    ``proc`` must have been spawned with ``start_new_session`` on POSIX;
    ``job`` is the handle :func:`spawn_tree` returned (consumed here). The
    signals go out before the first await, so a second cancellation cannot
    skip them; reaping the root then waits at most ``reap_timeout`` seconds,
    because a descendant that left the group can hold the pipes that
    ``proc.wait()`` also waits for.

    With ``confirm`` it returns True only when the whole tree is then known
    to have exited: on Windows, the job reports no active process within
    ``EXIT_CONFIRM_S``. On POSIX it always returns False (see
    :func:`_confirm_exit`). A closed output pipe is no proof either: a
    survivor may have redirected its output. Without ``confirm`` it returns
    False and checks nothing.
    """
    confirmed = False
    if sys.platform == "win32":
        if job is not None and confirm and terminate_job(job):
            # Ended, but the handle stays open until the job reports empty.
            try:
                confirmed = await _confirm_exit(job)
            finally:
                close_job(job)
        elif job is not None:
            close_job(job)  # ends every member, an orphaned grandchild too
        elif proc.returncode is None:
            with contextlib.suppress(OSError):
                await asyncio.to_thread(
                    subprocess.run,
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True,
                    env=scrub_child_env(),
                )
    else:
        _kill_group(proc.pid, leader_reaped=proc.returncode is not None)
    if proc.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
    try:
        await asyncio.wait_for(proc.wait(), reap_timeout)
    except TimeoutError:
        log.warning("process %s did not exit after kill", proc.pid)
    return confirmed
