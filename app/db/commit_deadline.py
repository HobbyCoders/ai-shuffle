"""Client commit deadlines independent of PostgreSQL interrupt handling.

COMMIT can wait in WAL/fsync where statement_timeout is not a reliable bound.
One watchdog disconnects overdue sockets (never retries an uncertain commit).
It never calls connection.cancel()/close(), which can themselves block in libpq.
"""
from __future__ import annotations

import errno
import logging
import socket
import threading
import time
from contextlib import contextmanager
from typing import Iterator

import psycopg2.extensions

logger = logging.getLogger("app.db")
COMMIT_TIMEOUT_SECONDS = 30.0
_CLOSE_FAILURE_LOG_WINDOW_S = 30.0
_close_failure_log: tuple[float | None, int] = (None, 0)
_close_failure_lock = threading.Lock()


def _close_duplicate(sock: socket.socket) -> int | None:
    """Close the private duplicate, returning the errno if that failed.

    Nothing about the commit depends on this descriptor once the wait is over:
    libpq reports the outcome on its own socket. EBADF means other code in this
    process closed this descriptor number, and raising here would turn a finished
    commit into a failure, hide a real commit error, or end the watchdog's thread
    and with it every later deadline. CPython forgets the descriptor before calling
    close(2), so a failure cannot leak it or close it twice.
    """
    try:
        sock.close()
    except OSError as exc:
        return exc.errno if isinstance(exc.errno, int) else -1
    return None


def _report_close_failure(code: int) -> None:
    """Keep a stray close visible without one line per commit. Errno only."""
    global _close_failure_log
    now = time.monotonic()
    with _close_failure_lock:
        last, suppressed = _close_failure_log
        if last is not None and now - last < _CLOSE_FAILURE_LOG_WINDOW_S:
            _close_failure_log = (last, suppressed + 1)
            return
        _close_failure_log = (now, 0)
    logger.warning(
        "Commit deadline descriptor close failed: errno=%s suppressed=%d; the commit outcome "
        "is unaffected, but EBADF means other code in this process closed this descriptor",
        errno.errorcode.get(code, code), suppressed,
    )


class _CommitWatchdog:
    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._pending: dict[socket.socket, float] = {}
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
        with self._condition:
            self._pending[duplicate] = time.monotonic() + seconds
            if not self._started:
                threading.Thread(target=self._run, name="db-commit-deadline", daemon=True).start()
                self._started = True
            self._condition.notify()
        return duplicate

    def disarm(self, sock: socket.socket) -> bool:
        # Serialize with shutdown: once disarmed, a returned pooled connection
        # cannot be disconnected by its previous borrower's deadline.
        with self._condition:
            self._pending.pop(sock, None)
            expired = sock in self._expired
            self._expired.discard(sock)
            close_failure = _close_duplicate(sock)
            self._condition.notify()
        if close_failure is not None:
            _report_close_failure(close_failure)
        return expired

    def _run(self) -> None:
        while True:
            close_failure = None
            with self._condition:
                if not self._pending:
                    self._condition.wait()
                    continue
                deadline = min(self._pending.values())
                sock = next(s for s, value in self._pending.items() if value == deadline)
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    self._condition.wait(remaining)
                    continue
                self._pending.pop(sock)
                self._expired.add(sock)
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass  # Already disconnected by PostgreSQL/the network.
                finally:
                    close_failure = _close_duplicate(sock)
            if close_failure is not None:
                _report_close_failure(close_failure)


_watchdog = _CommitWatchdog()


@contextmanager
def bounded_commit(conn: psycopg2.extensions.connection) -> Iterator[None]:
    """Bound the blocking COMMIT/ROLLBACK wait, not transaction execution time."""
    # Unit tests/shims do not have a libpq socket. Production always does.
    if not isinstance(conn, psycopg2.extensions.connection):
        yield
        return
    sock = _watchdog.arm(conn.fileno(), COMMIT_TIMEOUT_SECONDS)
    try:
        yield
    finally:
        expired = _watchdog.disarm(sock)
        if expired:
            # The driver call has unwound on this thread: close is now safe.
            conn.close()
            logger.warning(
                "PostgreSQL commit deadline exceeded; disconnected; outcome may be unknown"
            )
    if expired:
        raise psycopg2.OperationalError("commit deadline exceeded; outcome may be unknown")
