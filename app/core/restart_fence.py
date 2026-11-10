"""Admission fence for an idle-only application-update restart (#1121).

An idle restart must never stop work. A bare "is anything running?" read is
not enough: the supervisor verifies and stages the update for seconds after
that read, and a turn, background shell or Task can start in between. This
module closes that window.

Rules (every reader and writer goes through these functions):

- A producer that starts work calls :func:`admit` synchronously, before its
  first await that leads to the start, and holds the returned
  :class:`Admission` until the work is registered where
  ``desktop_lifecycle.active_session_ids()`` sees it, or its start is known to
  have been rolled back. An operation whose outcome is uncertain after its
  awaiter was cancelled (a thread call, a subprocess creation, a remote start)
  pins the admission until the operation itself finishes
  (:meth:`Admission.hold_until`, :meth:`Admission.to_thread`).
- :func:`admit` raises :class:`AdmissionClosed` while a restart is reserved.
  Producers refuse with its retryable message; a turn instead stays a durable
  queued row and is kicked again on release (:func:`defer_kick`).
- :func:`reserve` closes admission in one event-loop step, only when no
  admission is in flight and no work is registered. It never stops anything.
- :func:`release` (or the lease running out) reopens admission and kicks the
  turns that were held back.

In-process state only: a process restart forgets it, which is exactly when
nothing needs fencing any more.
"""
from __future__ import annotations

import asyncio
import itertools
import logging
import re
import time
from typing import Any, Callable, Dict, Optional, Set

logger = logging.getLogger(__name__)

# A supervisor that died after reserving must not fence this worker for good.
LEASE_SECONDS = 120.0

RETRY_MESSAGE = ("This worker is about to restart for an application update. "
                 "Nothing was started; try again in a minute.")

_TOKEN = re.compile(r"[A-Za-z0-9_-]{16,128}")

# The held reservation: token, monotonic expiry and the expiry timer.
_reservation: Optional[Dict[str, Any]] = None
# In-flight admissions by id.
_admissions: Dict[int, "Admission"] = {}
_ids = itertools.count(1)
# Sessions whose queued turn was held back while admission was closed.
_deferred_kicks: Set[str] = set()


class AdmissionClosed(RuntimeError):
    """A start refused because an idle restart is being prepared (retryable)."""

    def __init__(self, message: str = RETRY_MESSAGE):
        super().__init__(message)


class Admission:
    """One start in flight. Reference counted: the caller holds one reference
    and every pinned operation holds another, so a cancelled awaiter cannot
    release an operation that is still running."""

    def __init__(self, label: str):
        self.id = next(_ids)
        self.label = label
        self._refs = 1
        self._released = False
        _admissions[self.id] = self

    @property
    def held(self) -> bool:
        return self._refs > 0

    def _unref(self) -> None:
        if self._refs <= 0:
            return
        self._refs -= 1
        if self._refs == 0:
            _admissions.pop(self.id, None)

    def release(self) -> None:
        """Drop the caller's reference (idempotent)."""
        if not self._released:
            self._released = True
            self._unref()

    def hold_until(self, operation: "asyncio.Future[Any]") -> "asyncio.Future[Any]":
        """Keep this admission busy until ``operation`` finishes, whatever
        happens to the coroutine awaiting it. Returns ``operation``."""
        self._refs += 1
        operation.add_done_callback(lambda _: self._unref())
        return operation

    def _pinned(self, fn: Callable[[], Any]) -> Callable[[], Any]:
        """``fn`` for a worker thread, holding this admission until it returns."""
        loop = asyncio.get_running_loop()
        self._refs += 1

        def run() -> Any:
            try:
                return fn()
            finally:
                try:
                    loop.call_soon_threadsafe(self._unref)
                except RuntimeError:  # loop closed: the process is exiting
                    pass

        return run

    async def to_thread(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """``asyncio.to_thread`` whose thread keeps the admission busy until it
        returns, even when the awaiting coroutine is cancelled first."""
        return await asyncio.to_thread(self._pinned(lambda: fn(*args, **kwargs)))

    async def run_in_executor(self, executor: Any, fn: Callable[[], Any]) -> Any:
        """``loop.run_in_executor`` with the same guarantee as :meth:`to_thread`."""
        return await asyncio.get_running_loop().run_in_executor(executor, self._pinned(fn))

    def __enter__(self) -> "Admission":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.release()


def _reopen(why: str) -> None:
    global _reservation
    held, _reservation = _reservation, None
    if held is not None and held.get("timer") is not None:
        held["timer"].cancel()
    sessions = sorted(_deferred_kicks)
    _deferred_kicks.clear()
    logger.info("Restart fence: admission reopened (%s); kicking %d held session(s)", why, len(sessions))
    if sessions:
        from app.core import turn_scheduler
        for sid in sessions:
            turn_scheduler.kick(sid)


def _expire_if_due() -> None:
    if _reservation is not None and time.monotonic() >= _reservation["expires_at"]:
        logger.warning("Restart fence: reservation expired without a restart")
        _reopen("lease expired")


def is_closed() -> bool:
    """Whether an idle restart holds admission closed."""
    _expire_if_due()
    return _reservation is not None


def admit(label: str) -> Admission:
    """Reserve a start; raises :class:`AdmissionClosed` while fenced. Call it
    synchronously before the first await that leads to the start."""
    if is_closed():
        raise AdmissionClosed()
    return Admission(label)


def defer_kick(session_id: str) -> None:
    """Remember a session whose queued turn was held back by the fence."""
    _deferred_kicks.add(session_id)


def in_flight() -> list[str]:
    """Labels of starts still in flight (diagnostics and refusals)."""
    return [admission.label for admission in _admissions.values()]


async def reserve(token: str) -> Dict[str, Any]:
    """Close admission for an idle restart, or refuse and change nothing.

    ``token`` is chosen by the caller, so it can release a reservation whose
    answer it never received. The same token reserving again is idempotent.
    """
    if not isinstance(token, str) or not _TOKEN.fullmatch(token):
        raise ValueError("Invalid restart reservation token")
    from app.core import desktop_lifecycle, session_runtime

    try:
        # A turn that already ended must not refuse the restart.
        await session_runtime.reconcile_executions()
    except Exception:  # noqa: BLE001 — unreconciled is the stricter answer.
        logger.warning("Restart fence: could not reconcile stuck turn executions", exc_info=True)
    # From here on: one event-loop step, no await between check and latch.
    global _reservation
    _expire_if_due()
    if _reservation is not None:
        if _reservation["token"] == token:
            return {"reserved": True, "lease_seconds": LEASE_SECONDS}
        return {"reserved": False, "active_sessions": 0, "admissions": len(_admissions),
                "reason": "Another restart is already being prepared"}
    busy = desktop_lifecycle.active_session_ids()
    if busy or _admissions:
        return {"reserved": False, "active_sessions": len(busy), "admissions": len(_admissions),
                "reason": "Finish active sessions before restarting"}
    timer = None
    try:
        timer = asyncio.get_running_loop().call_later(LEASE_SECONDS + 0.05, _expire_if_due)
    except RuntimeError:
        pass
    _reservation = {"token": token, "expires_at": time.monotonic() + LEASE_SECONDS, "timer": timer}
    logger.info("Restart fence: admission closed for an idle restart")
    return {"reserved": True, "lease_seconds": LEASE_SECONDS}


def release(token: str) -> bool:
    """Reopen admission held by ``token``. Unknown tokens change nothing."""
    if _reservation is None or not isinstance(token, str) or _reservation["token"] != token:
        return False
    _reopen("released")
    return True


def _reset_for_tests() -> None:
    global _reservation
    if _reservation is not None and _reservation.get("timer") is not None:
        _reservation["timer"].cancel()
    _reservation = None
    _admissions.clear()
    _deferred_kicks.clear()
