"""Client commit deadlines independent of PostgreSQL interrupt handling.

COMMIT can wait in WAL/fsync where statement_timeout is not a reliable bound.
One watchdog disconnects overdue sockets (never retries an uncertain commit).
It never calls connection.cancel()/close(), which can themselves block in libpq.

Descriptor numbers are not identities. A stray close elsewhere in the process can
free any number the deadline relies on, and the next open reuses it. So identities
come from a descriptor this module owns or from when the connection opened, never
from a number that could already have changed hands, and every use re-checks the
open file behind the number. Shutting down a stranger breaks an unrelated socket,
and closing one is itself the stray close that wedges the next connection (#1020).
A check and its use are separate calls, so a close landing between them cannot be
excluded from user space: this narrows that window to microseconds, it does not
remove it. Windows has no such check (see ``_identity``) and trusts the numbers.
"""
from __future__ import annotations

import errno
import logging
import os
import socket
import sys
import threading
import time
import traceback
import weakref
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

import psycopg2.extensions

logger = logging.getLogger("app.db")
COMMIT_TIMEOUT_SECONDS = 30.0
_CLOSE_FAILURE_LOG_WINDOW_S = 30.0
_close_failure_log: tuple[float | None, int] = (None, 0)
_close_failure_lock = threading.Lock()


def _identity(fd: int) -> tuple[int, int] | None:
    """The open file behind a number now; None if none, or on Windows.

    Windows socket numbers are Winsock handles, while os.fstat reads C runtime
    descriptors, a different table: a match there would describe an unrelated file.
    """
    if os.name == "nt":
        return None
    try:
        info = os.fstat(fd)
    except OSError:
        return None
    return info.st_dev, info.st_ino


def owner_frames(thread_id: int) -> str:
    """Where a thread is, as file:line:function only: no locals, source text or SQL."""
    frame = sys._current_frames().get(thread_id)
    if frame is None:
        return "thread-gone"
    stack = traceback.StackSummary.extract(
        traceback.walk_stack(frame), limit=12, lookup_lines=False)
    return " <- ".join(f"{item.filename}:{item.lineno}:{item.name}" for item in stack)


# The socket each pooled connection opened with: the only record that outlives its
# number changing hands while the connection sits idle in the pool.
_transports: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def remember_transport(conn: object) -> None:
    """Record a just-opened connection's socket. Doubles have none."""
    if isinstance(conn, psycopg2.extensions.connection):
        _transports[conn] = _identity(conn.fileno())


@dataclass
class _Armed:
    deadline: float
    fd: int
    # Where the platform cannot say (Windows) these are None and numbers are trusted.
    # What the duplicate is: decides whether closing that number is still ours to do.
    duplicate: tuple[int, int] | None
    # What the connection's socket is: decides what may be shut down.
    transport: tuple[int, int] | None
    thread_id: int
    thread_name: str
    backend_pid: int | None = None


def _close_duplicate(sock: socket.socket, identity: tuple[int, int] | None) -> int | None:
    """Close the private duplicate, returning the errno if that failed.

    Nothing about the commit depends on this descriptor once the wait is over:
    libpq reports the outcome on its own socket. EBADF means other code in this
    process closed this descriptor number, and raising here would turn a finished
    commit into a failure, hide a real commit error, or end the watchdog's thread
    and with it every later deadline. CPython forgets the descriptor before calling
    close(2), so a failure cannot leak it or close it twice.

    If the number now names another open file, the duplicate was already closed
    for us and this number is someone else's: forget it without closing it.
    """
    if identity is not None and _identity(sock.fileno()) != identity:
        sock.detach()
        return errno.EBADF
    try:
        sock.close()
    except OSError as exc:
        return exc.errno if isinstance(exc.errno, int) else -1
    return None


def _warn_best_effort(message: str, *args: object) -> None:
    """A log handler's error reaches its caller; here that must not be a commit.

    It would replace the commit's outcome or end the watchdog's only thread, so a
    failing sink costs the line and nothing else.
    """
    try:
        logger.warning(message, *args)
    except Exception:  # noqa: BLE001 - a diagnostic never decides a commit's outcome
        pass


def _report_close_failure(code: int) -> None:
    """Keep a stray close visible without one line per commit. Errno only. Never raises."""
    global _close_failure_log
    try:
        now = time.monotonic()
        with _close_failure_lock:
            last, suppressed = _close_failure_log
            if last is not None and now - last < _CLOSE_FAILURE_LOG_WINDOW_S:
                _close_failure_log = (last, suppressed + 1)
                return
            _close_failure_log = (now, 0)
        _warn_best_effort(
            "Commit deadline descriptor close failed: errno=%s suppressed=%d; the commit outcome "
            "is unaffected, but EBADF means other code in this process closed this descriptor",
            errno.errorcode.get(code, code), suppressed,
        )
    except Exception:  # noqa: BLE001 - same rule for the limiter's own work
        pass


def _shutdown_quietly(sock: socket.socket) -> str:
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError as exc:  # Already disconnected by PostgreSQL/the network.
        return errno.errorcode.get(exc.errno or 0, "failed")
    return "ok"


def _disconnect(sock: socket.socket, armed: _Armed) -> tuple[str, str]:
    """Shut down the armed transport through whichever number still names it.

    Returns the shutdown result and what libpq's own number names now. When that
    number was replaced libpq polls a stranger: shutting down the real transport
    still ends the server's transaction, but nothing can wake that poll, so the
    borrower keeps its checkout until the stranger's peer closes (see
    ``_HOLD_CHECK_SECONDS`` in base.py for what limits the damage).
    """
    if armed.transport is None:
        return _shutdown_quietly(sock), "unknown"
    # Only a label for the log: the shutdown below validates what it actually holds.
    transport = "intact" if _identity(armed.fd) == armed.transport else "replaced"
    if _identity(sock.fileno()) == armed.transport:
        return _shutdown_quietly(sock), transport
    # The duplicate was lost: without this the commit has no bound. Duplicate first
    # and validate the copy, since libpq's number can change hands at any moment.
    # Nothing here may raise: it would end the watchdog and every later deadline.
    try:
        fresh_fd = socket.dup(armed.fd)
        if _identity(fresh_fd) != armed.transport:
            socket.close(fresh_fd)
            return "unreachable", transport
        with socket.socket(fileno=fresh_fd) as fresh:
            return _shutdown_quietly(fresh), transport
    except OSError as exc:
        return errno.errorcode.get(exc.errno or 0, "failed"), transport


class _CommitWatchdog:
    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._pending: dict[socket.socket, _Armed] = {}
        self._expired: set[socket.socket] = set()
        self._started = False

    def arm(self, fd: int, seconds: float) -> socket.socket:
        # Duplicate the descriptor so completion/expiry can never close a socket
        # that the OS has since reused. shutdown affects the original transport.
        # libpq keeps its transport nonblocking. socket.socket.dup() calls
        # settimeout(None) under the default timeout, making the shared transport
        # blocking. Duplicate the raw socket instead (also supports Winsock).
        # Wrapping it does not clear O_NONBLOCK; a non-None default timeout can
        # only enable nonblocking mode, which libpq already requires.
        duplicate_fd = socket.dup(fd)
        try:
            duplicate = socket.socket(fileno=duplicate_fd)
        except BaseException:
            socket.close(duplicate_fd)
            raise
        thread = threading.current_thread()
        # Read from the duplicate we own: libpq's number can change hands right after
        # dup, and recording that stranger would make it the one the deadline shuts down.
        identity = _identity(duplicate_fd)
        armed = _Armed(time.monotonic() + seconds, fd, identity, identity,
                       thread.ident or 0, thread.name)
        with self._condition:
            self._pending[duplicate] = armed
            if not self._started:
                threading.Thread(target=self._run, name="db-commit-deadline", daemon=True).start()
                self._started = True
            self._condition.notify()
        return duplicate

    def bind(self, sock: socket.socket, transport: tuple[int, int] | None,
             backend_pid: int | None) -> None:
        """Say which connection this deadline is for, once the pool knows its socket.

        If libpq's number changed hands before arm, the duplicate is of a stranger:
        only the identity from when the connection opened can reveal that.
        """
        with self._condition:
            armed = self._pending.get(sock)
            if armed is not None:
                armed.backend_pid = backend_pid
                if transport is not None:
                    armed.transport = transport

    def disarm(self, sock: socket.socket) -> bool:
        # Serialize with shutdown: once disarmed, a returned pooled connection
        # cannot be disconnected by its previous borrower's deadline.
        with self._condition:
            armed = self._pending.pop(sock, None)
            expired = sock in self._expired
            self._expired.discard(sock)
            # An expired duplicate was closed or forgotten by the watchdog.
            close_failure = None if armed is None else _close_duplicate(sock, armed.duplicate)
            self._condition.notify()
        if close_failure is not None:
            _report_close_failure(close_failure)
        return expired

    def _run(self) -> None:
        while True:
            with self._condition:
                if not self._pending:
                    self._condition.wait()
                    continue
                sock, armed = min(self._pending.items(), key=lambda item: item[1].deadline)
                remaining = armed.deadline - time.monotonic()
                if remaining > 0:
                    self._condition.wait(remaining)
                    continue
                self._pending.pop(sock)
                self._expired.add(sock)
                shutdown, transport = _disconnect(sock, armed)
                close_failure = _close_duplicate(sock, armed.duplicate)
            if close_failure is not None:
                _report_close_failure(close_failure)
            # Otherwise the borrower wakes now and reports the deadline itself.
            if shutdown != "ok" or transport == "replaced":
                self._report_expiry(armed, shutdown, transport, close_failure)

    @staticmethod
    def _report_expiry(
        armed: _Armed, shutdown: str, transport: str, close_failure: int | None,
    ) -> None:
        """A borrower this deadline could not wake, named while it is stuck. Never raises."""
        try:
            _warn_best_effort(
                "Commit deadline expired: shutdown=%s transport=%s duplicate_close=%s "
                "thread=%s backend_pid=%s frames=%s",
                shutdown, transport,
                "ok" if close_failure is None else errno.errorcode.get(close_failure, close_failure),
                armed.thread_name, armed.backend_pid, owner_frames(armed.thread_id),
            )
        except Exception:  # noqa: BLE001 - a diagnostic never stops later deadlines
            pass


_watchdog = _CommitWatchdog()


@contextmanager
def bounded_commit(conn: psycopg2.extensions.connection) -> Iterator[None]:
    """Bound the blocking COMMIT/ROLLBACK wait, not transaction execution time."""
    # Unit tests/shims do not have a libpq socket. Production always does.
    if not isinstance(conn, psycopg2.extensions.connection):
        yield
        return
    try:
        sock = _watchdog.arm(conn.fileno(), COMMIT_TIMEOUT_SECONDS)
    except OSError as exc:
        # Other code closed this number. Nothing was sent, and sending now could reach
        # whoever opens it next, with no deadline: fail as any lost connection does.
        raise psycopg2.OperationalError(
            "connection socket is gone; statement not sent") from exc
    try:
        _watchdog.bind(sock, _transports.get(conn), conn.info.backend_pid)
        yield
    finally:
        expired = _watchdog.disarm(sock)
        if expired:
            # The driver call has unwound on this thread: close is now safe.
            conn.close()
            _warn_best_effort(
                "PostgreSQL commit deadline exceeded; disconnected; outcome may be unknown"
            )
    if expired:
        raise psycopg2.OperationalError("commit deadline exceeded; outcome may be unknown")
