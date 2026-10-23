"""Lower CPU priority for the shell commands the model runs.

A test suite or build the model starts competes for CPU with the host process
that runs the event loop, the UI and every other session. These helpers put
such children below the host: they still get all idle CPU, but the scheduler
serves the host first when both want it.

``FLUID_CHILD_NICE`` sets how far below (default 10; 0 turns it off). On POSIX
the value is a nice increment over the host's own nice value, applied to the
child's whole process group, so the command and everything it forks later run
at that priority (children inherit nice). On Windows any positive value means
``BELOW_NORMAL_PRIORITY_CLASS``, which child processes inherit as well.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from collections.abc import Mapping
from typing import Any

log = logging.getLogger(__name__)

CHILD_NICE_ENV = "FLUID_CHILD_NICE"
DEFAULT_CHILD_NICE = 10
#: The lowest priority POSIX allows; nice values run -20..19.
MAX_NICE = 19

#: ``BELOW_NORMAL_PRIORITY_CLASS`` — only defined in ``subprocess`` on Windows.
BELOW_NORMAL_PRIORITY_CLASS = getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)


def parse_child_nice(value: Any) -> int:
    """``value`` as a nice increment in 0..19; anything else is the default.

    A negative increment would ask to run children *above* the host, which an
    unprivileged process cannot do, so it is treated as invalid too.
    """
    if value is None:
        return DEFAULT_CHILD_NICE
    if isinstance(value, bool):
        return DEFAULT_CHILD_NICE
    try:
        nice = int(str(value).strip())
    except ValueError:
        return DEFAULT_CHILD_NICE
    if 0 <= nice <= MAX_NICE:
        return nice
    return DEFAULT_CHILD_NICE


def child_nice(environ: Mapping[str, str] | None = None) -> int:
    """The configured increment from ``FLUID_CHILD_NICE``; 0 means off."""
    env = os.environ if environ is None else environ
    raw = env.get(CHILD_NICE_ENV)
    if raw is None or not str(raw).strip():
        return DEFAULT_CHILD_NICE
    return parse_child_nice(raw)


def priority_creationflags(nice: int | None = None) -> int:
    """Windows ``creationflags`` bits for a lowered child; 0 elsewhere or when off."""
    if sys.platform != "win32":
        return 0
    if (child_nice() if nice is None else nice) <= 0:
        return 0
    return BELOW_NORMAL_PRIORITY_CLASS


def lower_process_group_priority(pid: int | None, nice: int | None = None) -> None:
    """Lower the priority of the process group led by ``pid`` (POSIX only).

    The caller must have spawned ``pid`` with ``start_new_session=True``, so
    the group id equals the pid. Setting the group rather than the one process
    also covers anything the shell forked between the spawn returning and this
    call. The target is the host's own nice value plus ``nice``, capped at 19.

    Never raises: a child that already exited (``ProcessLookupError``) or a
    platform that refuses (``PermissionError``, other ``OSError``) is logged at
    debug level and the command keeps running at normal priority.
    """
    # Looked up at call time: Windows has no setpriority (its priority class
    # is set through creationflags instead), and tests substitute their own.
    setpriority = getattr(os, "setpriority", None)
    if setpriority is None or not isinstance(pid, int):
        return
    increment = child_nice() if nice is None else nice
    if increment <= 0:
        return
    getpriority = getattr(os, "getpriority", None)
    try:
        base = getpriority(getattr(os, "PRIO_PROCESS", 0), 0) if getpriority else 0
    except OSError:
        base = 0
    target = min(MAX_NICE, base + increment)
    try:
        setpriority(getattr(os, "PRIO_PGRP", 1), pid, target)
    except OSError as exc:  # ProcessLookupError, PermissionError, ...
        log.debug("could not lower priority of process group %s: %s", pid, exc)


#: ``oom_score_adj`` for the shell commands the model runs (Linux). The kernel
#: adds it to a process's OOM badness, and children inherit it, so under a
#: host-wide memory shortage a test run or build is killed before the host
#: process that runs every session, or its database. 0 turns it off.
DEFAULT_CHILD_OOM_SCORE_ADJ = 300
#: The kernel's ceiling for ``oom_score_adj``.
MAX_OOM_SCORE_ADJ = 1000


def raise_oom_score_adj(pid: int | None, adj: int = DEFAULT_CHILD_OOM_SCORE_ADJ) -> None:
    """Make ``pid`` a preferred OOM-kill victim (Linux only); never lowers it.

    Raising the value needs no privilege, only lowering does, so this works
    in an unprivileged container. Call it right after the spawn: whatever the
    process forks afterwards inherits the value. Never raises; a process that
    already exited, another user's process, or a platform without
    ``/proc/<pid>/oom_score_adj`` leaves the score unchanged.
    """
    if not isinstance(pid, int) or pid <= 0:
        return
    try:
        target = min(int(adj), MAX_OOM_SCORE_ADJ)
    except (TypeError, ValueError):
        return
    if target <= 0:
        return
    path = f"/proc/{pid}/oom_score_adj"
    try:
        with open(path, encoding="ascii") as f:
            current = int(f.read().strip() or 0)
    except (OSError, ValueError):
        return
    if current >= target:
        return
    try:
        with open(path, "w", encoding="ascii") as f:
            f.write(str(target))
    except OSError as exc:
        log.debug("could not raise oom_score_adj of %s: %s", pid, exc)
