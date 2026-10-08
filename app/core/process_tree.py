"""Spawn and kill mechanics for the shells the harness runs on the user's box.

One place owns the two things that have to agree: how a background shell is
*created* (so its descendants can be reached later) and how it is *destroyed*
(so none of them outlive the stop). Both legs of Monitor — the MCP
``mcp__builtins__Monitor`` tool and fluid's bare ``Monitor`` builtin, which
share ``app.mcp.builtin_tools._spawn_background`` — go through here, as do the
session-teardown reap and the per-task stop button.

Why it exists: on Windows ``proc.terminate()`` kills exactly one process. The
command the model writes runs under Git Bash, which nests a *second* bash, and
a dev server is a third generation below that — so terminating the shell
leaves ``python run_local.py`` holding its listening socket while the harness
reports ``{"success": true, "running": false}``.

The approach mirrors fluid's ``kill_tree``: POSIX gets a
fresh session so ``killpg`` reaches the whole group; Windows gets
``CREATE_NEW_PROCESS_GROUP`` at spawn (making a graceful CTRL_BREAK possible)
and ``taskkill /F /T`` as the escalation. On top of fluid's version this module
also *verifies* the outcome — the snapshot of descendants taken before the kill
is re-checked afterwards, and anything still breathing is named in the result
rather than papered over.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import subprocess
import sys
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

_IS_WINDOWS = sys.platform == "win32"

#: ``CREATE_NEW_PROCESS_GROUP`` — only defined in ``subprocess`` on Windows.
_CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)

#: ``BELOW_NORMAL_PRIORITY_CLASS`` — likewise Windows-only; child processes
#: inherit it.
_BELOW_NORMAL_PRIORITY_CLASS = getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0x00004000)

#: How long a graceful signal gets before the forceful escalation.
DEFAULT_GRACE_S = 5.0

#: How long to keep re-checking that descendants really died.
DEFAULT_VERIFY_S = 3.0

#: Grace an orphaned descendant gets to finish its own shutdown before the
#: sweep force-kills it.
_ORPHAN_SETTLE_S = 1.0


# ---------------------------------------------------------------------------
# Spawn
# ---------------------------------------------------------------------------

def spawn_kwargs() -> Dict[str, Any]:
    """Extra ``create_subprocess_exec`` kwargs that make the tree killable.

    POSIX: a fresh session (hence a fresh process group) lets ``os.killpg``
    take out the shell *and* everything it spawned.

    Windows: ``CREATE_NEW_PROCESS_GROUP`` does two jobs. It makes
    ``CTRL_BREAK_EVENT`` deliverable to the child's group — the only graceful
    stop a console app on Windows understands — and, just as importantly, it
    keeps any console signal aimed at that group from travelling back up into
    the server process that spawned it.

    Windows also gets ``BELOW_NORMAL_PRIORITY_CLASS`` unless
    ``child_process_nice`` is 0; POSIX callers lower the priority with
    :func:`lower_priority` right after the spawn instead.
    """
    if _IS_WINDOWS:
        flags = _CREATE_NEW_PROCESS_GROUP
        if child_nice() > 0:
            flags |= _BELOW_NORMAL_PRIORITY_CLASS
        return {"creationflags": flags}
    return {"start_new_session": True}


def child_nice() -> int:
    """The ``child_process_nice`` setting as a nice increment; 0 means off."""
    from app.core.config import settings  # noqa: PLC0415 — keep this module import-light
    from fluid.tools.priority import parse_child_nice  # noqa: PLC0415

    return parse_child_nice(getattr(settings, "child_process_nice", None))


def lower_priority(proc: Any) -> None:
    """Run a shell spawned with :func:`spawn_kwargs` below the server's priority.

    POSIX only: sets the nice value of the child's whole process group (its
    pgid is its pid because of ``start_new_session``) to the server's own nice
    plus ``child_process_nice``, so the shell and everything it forks later
    yield the CPU to the server's event loop. Windows got its priority class
    from :func:`spawn_kwargs` (``os.setpriority`` does not exist there, so this
    is a no-op). Never raises; a child that already exited is logged at debug
    level.
    """
    from fluid.tools.priority import lower_process_group_priority  # noqa: PLC0415

    lower_process_group_priority(getattr(proc, "pid", None), child_nice())


def child_env(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """The environment a spawned shell should inherit.

    Windows still hands a child Python the ANSI code page (cp1252) for stdout,
    so the first ``print`` containing a box-drawing character or an emoji dies
    with ``UnicodeEncodeError`` — the process is gone before it ever binds its
    port, and the buffer the model reads back shows a traceback with no
    obvious cause. Force UTF-8 for children so their output survives the
    trip.

    Host secrets are scrubbed before inheritance, as for Bash and delegated
    CLI children. Explicit caller overlays remain deliberate handovers.
    UTF-8 defaults do not override an operator setting.
    """
    from fluid.tools.child_env import child_env_with_overlay

    env = child_env_with_overlay(extra)
    if _IS_WINDOWS:
        env.setdefault("PYTHONIOENCODING", "utf-8")
        env.setdefault("PYTHONUTF8", "1")
    return env


# ---------------------------------------------------------------------------
# Inspection
# ---------------------------------------------------------------------------

def _psutil():
    try:
        import psutil  # noqa: PLC0415 — optional, and never fatal

        return psutil
    except Exception:  # noqa: BLE001
        return None


def descendant_pids(pid: Optional[int]) -> List[int]:
    """Every pid below ``pid`` right now, deepest-first is not guaranteed.

    Best effort: without psutil (or for a pid that has already gone) this is
    an empty list, and callers degrade to "we cannot prove anything survived".
    """
    if not isinstance(pid, int):
        return []
    ps = _psutil()
    if ps is None:
        return []
    try:
        return [child.pid for child in ps.Process(pid).children(recursive=True)]
    except Exception:  # noqa: BLE001 — NoSuchProcess, AccessDenied, anything
        return []


def pid_alive(pid: int) -> bool:
    """Whether ``pid`` is still a live process.

    ``os.kill(pid, 0)`` is the POSIX probe; on Windows that call TERMINATES
    the target, so ask the process table instead — psutil when it is present,
    ``tasklist`` when it is not.
    """
    if not isinstance(pid, int):
        return False
    ps = _psutil()
    if ps is not None:
        try:
            proc = ps.Process(pid)
            return proc.is_running() and proc.status() != ps.STATUS_ZOMBIE
        except Exception:  # noqa: BLE001
            return False
    if _IS_WINDOWS:
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True, timeout=10,
            ).stdout
        except Exception:  # noqa: BLE001
            return False
        return str(pid) in (out or "")
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def listening_ports(pids: Iterable[int]) -> List[int]:
    """TCP ports the given pids are still LISTENing on.

    Purely for the honesty message — "task 4f2a1b left pid 91234 listening on
    8010". Empty
    whenever psutil is missing or the platform refuses the lookup.
    """
    ps = _psutil()
    if ps is None:
        return []
    ports: List[int] = []
    for pid in pids:
        try:
            for conn in ps.Process(pid).net_connections(kind="inet"):
                if conn.status == ps.CONN_LISTEN and conn.laddr:
                    port = getattr(conn.laddr, "port", None)
                    if isinstance(port, int) and port not in ports:
                        ports.append(port)
        except Exception:  # noqa: BLE001 — AccessDenied is normal here
            continue
    return sorted(ports)


# ---------------------------------------------------------------------------
# Kill
# ---------------------------------------------------------------------------

def _force_kill_pid(pid: int) -> None:
    """Last-resort single-pid kill for an orphan the tree kill missed."""
    try:
        if _IS_WINDOWS:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True, timeout=10,
            )
        else:
            os.kill(pid, signal.SIGKILL)
    except Exception:  # noqa: BLE001
        logger.debug("force kill of orphan pid %s failed", pid, exc_info=True)


def _signal_group(pid: int, *, force: bool, own_group: bool) -> bool:
    """Signal the whole group/tree rooted at ``pid``. False = not attempted.

    ``own_group`` records that the process was spawned through
    :func:`spawn_kwargs`, and it gates every *group*-addressed signal. A group
    signal aimed at a pid that is not its own group leader lands on OUR group
    instead — ``CTRL_BREAK_EVENT`` on Windows, ``killpg`` on POSIX — which is
    to say it kills the server. Windows' ``taskkill /T`` is the exception: it
    walks the parent/child table rather than the group, so it is safe either
    way and is the escalation on both branches.
    """
    if _IS_WINDOWS:
        if force or not own_group:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True, timeout=15,
            )
            return True
        # Graceful: uvicorn and friends handle SIGBREAK and shut down cleanly.
        os.kill(pid, signal.CTRL_BREAK_EVENT)
        return True
    if not own_group:
        return False
    os.killpg(pid, signal.SIGKILL if force else signal.SIGTERM)
    return True


async def _signal_tree(proc: Any, *, force: bool, own_group: bool) -> bool:
    """Best-effort :func:`_signal_group` off the event loop. False when it failed."""
    pid = getattr(proc, "pid", None)
    if not isinstance(pid, int):
        return False
    try:
        return await asyncio.to_thread(
            _signal_group, pid, force=force, own_group=own_group
        )
    except Exception:  # noqa: BLE001 — already-dead groups and consoleless
        # hosts both land here; the per-process fallbacks below still run.
        logger.debug(
            "tree signal (force=%s) for pid %s failed", force, pid, exc_info=True
        )
        return False


async def kill_process_tree(
    proc: Any,
    *,
    own_process_group: bool = False,
    grace: float = DEFAULT_GRACE_S,
    verify: float = DEFAULT_VERIFY_S,
) -> List[int]:
    """Stop ``proc`` and everything under it; return the pids that survived.

    The sequence, and why each step is there:

    1. Snapshot the descendants *first*. The walk follows parent links, and
       killing the root destroys them — after the fact there is nothing left
       to enumerate.
    2. Graceful: ``SIGTERM`` to the group (POSIX) or ``CTRL_BREAK_EVENT`` to
       the group (Windows, only when we created it). When there is no group
       to address, ``proc.terminate()`` is the fallback ask.
    3. Wait out ``grace``; on timeout escalate to ``SIGKILL`` / ``taskkill
       /F /T`` plus ``proc.kill()``, and reap with ``proc.wait()``.
    4. Sweep: re-check the snapshot. On Windows a grandchild frequently
       outlives the shell that started it, so anything still alive is killed
       individually and then re-polled for up to ``verify`` seconds.

    An empty return value is the only thing that entitles a caller to say the
    task stopped. A non-empty one names the orphans.
    """
    if proc is None:
        return []
    # psutil's tree walk is blocking — off the loop, like the app's other
    # process-tree walks.
    doomed = await asyncio.to_thread(descendant_pids, getattr(proc, "pid", None))

    if getattr(proc, "returncode", None) is None:
        signalled = await _signal_tree(
            proc, force=False, own_group=own_process_group
        )
        if not signalled:
            # No group to address (not spawned by us, or a pid we can't reach):
            # the single-process ask is all that's left for the graceful phase.
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
            except Exception:  # noqa: BLE001 — already exited / unsupported
                logger.debug("terminate() failed on background shell", exc_info=True)
        try:
            await asyncio.wait_for(proc.wait(), timeout=grace)
        except asyncio.TimeoutError:
            await _signal_tree(proc, force=True, own_group=own_process_group)
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            except Exception:  # noqa: BLE001
                logger.debug("kill() failed on background shell", exc_info=True)
            try:
                await proc.wait()
            except Exception:  # noqa: BLE001
                logger.debug("wait() failed on background shell", exc_info=True)
        except Exception:  # noqa: BLE001
            logger.debug("wait() failed on background shell", exc_info=True)

    return await _sweep_orphans(doomed, verify=verify)


async def _alive(pids: List[int]) -> List[int]:
    return [pid for pid in pids if await asyncio.to_thread(pid_alive, pid)]


async def _sweep_orphans(pids: List[int], *, verify: float) -> List[int]:
    """Force-kill any pid from the pre-kill snapshot that is still running.

    A short settle window comes first: a child that took the graceful signal
    is probably already on its way out, and force-killing it a millisecond
    later would throw away the clean shutdown we just asked for.
    """
    if not pids:
        return []
    loop = asyncio.get_running_loop()
    settle_deadline = loop.time() + _ORPHAN_SETTLE_S
    survivors = await _alive(pids)
    while survivors and loop.time() < settle_deadline:
        await asyncio.sleep(0.1)
        survivors = await _alive(survivors)
    if not survivors:
        return []
    for pid in survivors:
        await asyncio.to_thread(_force_kill_pid, pid)
    deadline = loop.time() + max(0.0, verify)
    while True:
        still = await _alive(survivors)
        if not still or loop.time() >= deadline:
            return still
        await asyncio.sleep(0.2)


def survivor_warning(task_id: str, survivors: List[int]) -> str:
    """The sentence a caller shows instead of claiming a clean stop."""
    ports = listening_ports(survivors)
    pid_list = ", ".join(str(p) for p in survivors)
    text = (
        f"Task {task_id} was stopped but {len(survivors)} child process(es) "
        f"survived: pid {pid_list}."
    )
    if ports:
        text += " Still listening on port " + ", ".join(str(p) for p in ports) + "."
    return text + " Kill them yourself before assuming the port is free."
