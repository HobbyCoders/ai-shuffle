"""Machine-sized slots for concurrent ``pytest -n`` runs.

``tests/conftest.py`` takes one OS file lock per controller before starting
xdist workers. Slots default to one per eight CPUs and per 12 GiB total RAM,
with a minimum of one and maximum of four. Total (not free) memory keeps the
budget stable across concurrent runs. Unknown memory means one slot.
``AI_SHUFFLE_PYTEST_SLOTS`` overrides this budget. Each gated controller is
capped at two workers, so the default admits at most eight xdist workers.

Waiters rotate their first slot and jitter their polling to share acquisition
opportunities rather than repeatedly racing in lockstep. This is best-effort
fairness, not a strict FIFO queue; OS locks do not promise FIFO admission.

The lock is an OS file lock (``fcntl.flock`` on POSIX, ``msvcrt.locking`` on
Windows), so the operating system drops it when the holding process exits;
a crashed run cannot block later ones. The gate is off on CI
(``GITHUB_ACTIONS`` set), for runs without xdist parallelism, inside xdist
workers, and when ``AI_SHUFFLE_PYTEST_GATE=0``. Waiting stops after
:data:`DEFAULT_MAX_WAIT_SECONDS` and the run goes ahead without the lock.
Any ``OSError`` turns the gate off for that run; nothing here raises into
pytest.
"""

from __future__ import annotations

import errno
import os
import random
import sys
import tempfile
import time
from typing import Callable, Mapping, Optional, TextIO

LOCK_FILE_NAME = "ai-shuffle-pytest-xdist.lock"
BYPASS_ENV = "AI_SHUFFLE_PYTEST_GATE"
SLOTS_ENV = "AI_SHUFFLE_PYTEST_SLOTS"
WORKERS_PER_SLOT = 2
# Only GITHUB_ACTIONS marks a CI run. A bare ``CI`` is not enough: the desktop
# app starts its worker with CI=true (desktop/src/main/source-build.ts
# buildEnvironment), so every agent shell on a desktop worker inherits it.
CI_ENV_NAMES = ("GITHUB_ACTIONS",)
DEFAULT_MAX_WAIT_SECONDS = 20 * 60
DEFAULT_NOTICE_INTERVAL_SECONDS = 15.0
DEFAULT_POLL_INTERVAL_SECONDS = 1.0

_CONFIG_ATTR = "_ai_shuffle_xdist_gate"

# errno values a non-blocking lock attempt reports when another process holds
# the lock: EWOULDBLOCK/EAGAIN from flock, EACCES/EDEADLOCK from msvcrt.locking.
_HELD_ERRNOS = {
    code
    for code in (
        getattr(errno, "EWOULDBLOCK", None),
        getattr(errno, "EAGAIN", None),
        getattr(errno, "EACCES", None),
        getattr(errno, "EDEADLOCK", None),
        getattr(errno, "EDEADLK", None),
    )
    if code is not None
}


def default_lock_path() -> str:
    """Lock file shared by every checkout on this machine."""
    return os.path.join(tempfile.gettempdir(), LOCK_FILE_NAME)


def _cpu_count() -> int:
    """Respect a container/worker's CPU affinity where the OS exposes it."""
    count = os.cpu_count() or 1
    try:
        return min(count, len(os.sched_getaffinity(0)))
    except (AttributeError, OSError):
        return count


def _total_memory_bytes() -> Optional[int]:
    """Portable physical RAM budget; never derive capacity from fluctuating free RAM."""
    try:
        import psutil

        return psutil.virtual_memory().total
    except (ImportError, OSError, ValueError, AttributeError):
        return None


def slot_count(environ: Optional[Mapping[str, str]] = None) -> int:
    env = os.environ if environ is None else environ
    try:
        override = int(env.get(SLOTS_ENV, ""))
        if override >= 1:
            return override
    except (ValueError, TypeError):
        pass
    memory = _total_memory_bytes()
    memory_slots = memory // (12 * 1024**3) if memory else 1
    return max(1, min(4, _cpu_count() // 8, memory_slots))


def xdist_parallel(config) -> bool:
    """True when this run asks xdist for more than one worker."""
    try:
        value = config.getoption("numprocesses", default=None)
    except (ValueError, AttributeError):
        return False
    if isinstance(value, str):
        return value in ("auto", "logical")
    return isinstance(value, int) and not isinstance(value, bool) and value > 1


def gate_applies(config, environ: Optional[Mapping[str, str]] = None) -> bool:
    """Whether this pytest process should take the machine-wide lock."""
    env = os.environ if environ is None else environ
    if hasattr(config, "workerinput"):
        return False
    if env.get(BYPASS_ENV, "").strip() == "0":
        return False
    if any(env.get(name) for name in CI_ENV_NAMES):
        return False
    return xdist_parallel(config)


def _warn(stream: Optional[TextIO], message: str) -> None:
    out = stream if stream is not None else sys.stderr
    try:
        out.write(message + "\n")
        out.flush()
    except Exception:
        pass


class XdistGate:
    """An exclusive, non-blocking OS lock on one byte of ``path``."""

    def __init__(
        self,
        path: Optional[str] = None,
        *,
        max_wait: float = DEFAULT_MAX_WAIT_SECONDS,
        notice_interval: float = DEFAULT_NOTICE_INTERVAL_SECONDS,
        poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
        stream: Optional[TextIO] = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.path = path or default_lock_path()
        self.max_wait = max_wait
        self.notice_interval = notice_interval
        self.poll_interval = poll_interval
        self.stream = stream
        self._clock = clock
        self._sleep = sleep
        self._fd: Optional[int] = None
        self.held = False

    def _open(self) -> int:
        if self._fd is None:
            self._fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o666)
        return self._fd

    def _close(self) -> None:
        fd, self._fd = self._fd, None
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass

    def try_lock(self) -> bool:
        """Take the lock without waiting. False when another process holds it.

        Any other ``OSError`` propagates; :meth:`acquire` turns it into "no gate".
        """
        fd = self._open()
        try:
            if os.name == "nt":
                import msvcrt

                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in _HELD_ERRNOS:
                return False
            raise
        self.held = True
        return True

    def acquire(self) -> bool:
        """Wait for the lock. True once held; False after a timeout or error.

        Prints a notice to stderr when the lock is busy and every
        ``notice_interval`` seconds while waiting. Never raises.
        """
        try:
            start = self._clock()
            next_notice = start
            while True:
                if self.try_lock():
                    return True
                now = self._clock()
                waited = now - start
                if waited >= self.max_wait:
                    _warn(
                        self.stream,
                        f"pytest gate: waited {int(waited)}s for another pytest -n run "
                        "on this machine; proceeding without the lock.",
                    )
                    self._close()
                    return False
                if now >= next_notice:
                    _warn(
                        self.stream,
                        "pytest gate: another pytest -n run on this machine holds the "
                        f"lock; waiting ({int(waited)}s). Run focused tests without -n, "
                        f"or set {BYPASS_ENV}=0 to bypass.",
                    )
                    next_notice = now + self.notice_interval
                self._sleep(min(self.poll_interval, max(self.max_wait - waited, 0.0)))
        except OSError as exc:
            _warn(self.stream, f"pytest gate: lock unavailable ({exc}); running without it.")
            self._close()
            self.held = False
            return False

    def release(self) -> None:
        """Drop the lock and close the file. Never raises."""
        fd = self._fd
        if fd is not None and self.held:
            try:
                if os.name == "nt":
                    import msvcrt

                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError as exc:
                _warn(self.stream, f"pytest gate: unlock failed ({exc}); closing the lock file.")
        self.held = False
        self._close()


class XdistSlots(XdistGate):
    """Scan independent per-file locks, closing every losing descriptor immediately."""

    def __init__(self, path: Optional[str] = None, *, slots: int = 1, **kwargs) -> None:
        super().__init__(path, **kwargs)
        self._base_path = self.path
        self.slots = slots
        self._next_slot = os.getpid() % slots

    def try_lock(self) -> bool:
        if self.held:
            return True
        start = self._next_slot
        self._next_slot = (start + 1) % self.slots
        for offset in range(self.slots):
            self.path = f"{self._base_path}.{(start + offset) % self.slots}"
            if super().try_lock():
                return True
            self._close()
        return False

    def acquire(self) -> bool:
        # Jitter the inherited bounded wait/notice loop so independent sessions
        # do not always poll in the same order. Restore the supplied sleep hook.
        sleep = self._sleep
        self._sleep = lambda delay: sleep(delay * random.uniform(0.5, 1.0))
        try:
            return super().acquire()
        finally:
            self._sleep = sleep


def configure(config, environ: Optional[Mapping[str, str]] = None, **gate_kwargs) -> Optional[XdistSlots]:
    """``pytest_configure`` body: reserve a slot when the gate applies."""
    try:
        if not gate_applies(config, environ):
            return None
        requested = config.getoption("numprocesses", default=None)
        if isinstance(requested, str) or requested > WORKERS_PER_SLOT:
            config.option.numprocesses = WORKERS_PER_SLOT
            _warn(gate_kwargs.get("stream"),
                  f"pytest gate: limiting {requested!r} workers to {WORKERS_PER_SLOT} per slot.")
        gate_kwargs.setdefault("slots", slot_count(environ))
        gate = XdistSlots(**gate_kwargs)
        if gate.acquire():
            setattr(config, _CONFIG_ATTR, gate)
            return gate
    except Exception as exc:  # never break a pytest run over the gate
        _warn(gate_kwargs.get("stream"), f"pytest gate: disabled ({exc}).")
    return None


def unconfigure(config) -> None:
    """``pytest_unconfigure`` body: release a lock taken by :func:`configure`."""
    gate = getattr(config, _CONFIG_ATTR, None)
    if gate is None:
        return
    try:
        delattr(config, _CONFIG_ATTR)
    except AttributeError:
        pass
    gate.release()
