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
- Windows: a kill-on-close Job Object taken at spawn (:func:`spawn_job`) holds
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

from fluid.tools.child_env import scrub_child_env
from fluid.tools.job_object import assign_kill_on_close_job, close_job, release_job

log = logging.getLogger(__name__)

#: How long :func:`kill_tree` waits to reap the root after the kill.
REAP_TIMEOUT_S = 5.0


def spawn_job(proc: asyncio.subprocess.Process) -> int | None:
    """Windows: put a just-spawned ``proc`` in a kill-on-close job.

    Returns the handle to pass to :func:`kill_tree` or :func:`release_tree`
    exactly once, or None (always on POSIX, where the group is the record).
    """
    return assign_kill_on_close_job(proc.pid)


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


async def kill_tree(
    proc: asyncio.subprocess.Process,
    job: int | None = None,
    *,
    reap_timeout: float = REAP_TIMEOUT_S,
) -> None:
    """Kill ``proc`` and every descendant, whether or not ``proc`` has exited.

    ``proc`` must have been spawned with ``start_new_session`` on POSIX;
    ``job`` is the handle :func:`spawn_job` returned (consumed here). The
    signals go out before the first await, so a second cancellation cannot
    skip them; reaping the root then waits at most ``reap_timeout`` seconds,
    because a descendant that left the group can hold the pipes that
    ``proc.wait()`` also waits for.
    """
    if sys.platform == "win32":
        if job is not None:
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
