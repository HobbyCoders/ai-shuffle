"""An unavailable POSIX identity is a lost duplicate, not Windows support."""
from __future__ import annotations

import errno
import os
import socket
import time
from types import SimpleNamespace

import psycopg2
import pytest

from app.db import commit_deadline


@pytest.mark.skipif(os.name == "nt", reason="reuses a raw POSIX descriptor")
@pytest.mark.parametrize("expire", [False, True], ids=["disarm", "expiry"])
def test_missing_initial_identity_never_closes_a_reused_number(monkeypatch, expire):
    watchdog = commit_deadline._CommitWatchdog()
    transport, peer = socket.socketpair()
    stranger, stranger_peer = socket.socketpair()
    real_identity = commit_deadline._identity
    replacement = None
    armed = None
    failure = None

    def lose_before_snapshot(fd):
        nonlocal replacement
        if replacement is None:
            os.close(fd)
            identity = real_identity(fd)
            assert identity is None
            # The snapshot failed; a later open reused that number before arm returned.
            os.dup2(stranger.fileno(), fd)
            replacement = socket.socket(fileno=fd)
            return identity
        return real_identity(fd)

    monkeypatch.setattr(commit_deadline, "_identity", lose_before_snapshot)
    try:
        try:
            armed = watchdog.arm(transport.fileno(), 0.02 if expire else 5.0)
        except OSError as exc:
            failure = exc
        monkeypatch.setattr(commit_deadline, "_identity", real_identity)
        if armed is not None:
            watchdog.bind(armed, real_identity(transport.fileno()), None)
            if expire:
                time.sleep(0.1)
            assert watchdog.disarm(armed) is expire
        assert replacement is not None
        replacement.settimeout(1)
        stranger_peer.settimeout(1)
        replacement.sendall(b"still owned by someone else")
        assert stranger_peer.recv(64) == b"still owned by someone else"
        stranger_peer.sendall(b"ok")
        assert replacement.recv(2) == b"ok"
        assert failure is not None and failure.errno == errno.EBADF
        assert not watchdog._pending and not watchdog._started
        peer.settimeout(1)
        transport.sendall(b"untouched")
        assert peer.recv(9) == b"untouched"
    finally:
        if armed is not None:
            watchdog.disarm(armed)
        # Pre-fix cleanup may have closed this number already. Do not close it twice.
        if replacement is not None:
            if real_identity(replacement.fileno()) == real_identity(stranger.fileno()):
                replacement.close()
            else:
                replacement.detach()
        for item in (transport, peer, stranger, stranger_peer):
            item.close()


@pytest.mark.parametrize("expire", [False, True], ids=["disarm", "expiry"])
def test_windows_without_identity_still_disarms_and_expires(monkeypatch, expire):
    # Winsock handles are not CRT file descriptors: a missing identity is supported.
    monkeypatch.setattr(commit_deadline, "os", SimpleNamespace(name="nt"))
    watchdog = commit_deadline._CommitWatchdog()
    transport, peer = socket.socketpair()
    armed = None
    try:
        assert commit_deadline._identity(transport.fileno()) is None
        armed = watchdog.arm(transport.fileno(), 0.02 if expire else 5.0)
        number = armed.fileno()
        if expire:
            peer.settimeout(1)
            assert peer.recv(1) == b""
        assert watchdog.disarm(armed) is expire
        assert armed.fileno() == -1
        with pytest.raises(OSError):
            socket.dup(number)
        if not expire:
            transport.sendall(b"ok")
            assert peer.recv(2) == b"ok"
    finally:
        if armed is not None:
            watchdog.disarm(armed)
        transport.close()
        peer.close()


@pytest.mark.skipif(os.name == "nt", reason="failed POSIX identity capture")
def test_failed_identity_is_a_lost_connection_before_commit(monkeypatch):
    transport, peer = socket.socketpair()

    class Connection:
        info = SimpleNamespace(backend_pid=None)

        def fileno(self):
            return transport.fileno()

    monkeypatch.setattr(commit_deadline.psycopg2.extensions, "connection", Connection)
    monkeypatch.setattr(commit_deadline, "_identity", lambda fd: None)
    # Close after wrapping, not before: the constructor itself checks the socket.
    real_socket = socket.socket

    def wrap_then_close(*args, **kwargs):
        sock = real_socket(*args, **kwargs)
        os.close(sock.fileno())
        return sock

    monkeypatch.setattr(commit_deadline.socket, "socket", wrap_then_close)
    sent = False
    try:
        with pytest.raises(psycopg2.OperationalError, match="statement not sent"):
            with commit_deadline.bounded_commit(Connection()):
                sent = True
        assert not sent
    finally:
        transport.close()
        peer.close()
