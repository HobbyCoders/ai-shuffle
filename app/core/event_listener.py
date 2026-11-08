"""Process-isolated PostgreSQL LISTEN with a bounded, memory-mapped mailbox.

The worker never polls libpq or reads a pipe. Only the child owns its database
socket; a stuck driver cannot hold the worker's GIL. Notifications are hints:
mailbox overflow forces session readers to reconcile, typed messages remain
best effort. No credentials enter argv, logs or an inherited child environment.
This file also runs with -I -S as the child (stdlib + the supplied driver path).
"""
from __future__ import annotations

import json
import mmap
import os
from pathlib import Path
import select
import signal
import struct
import subprocess
import sys
import tempfile
import time

_SLOTS = 128
_SLOT_BYTES = 8208
_HEADER_BYTES = 40  # Published sequence, heartbeats, Windows runtime PID/admission.
_SIZE = _HEADER_BYTES + _SLOTS * _SLOT_BYTES
_CHILD = Path(__file__).resolve()
_HEARTBEAT_SECONDS = 10.0


def _number(memory, offset):
    return struct.unpack_from("=Q", memory, offset)[0]


def _store(memory, offset, value):
    memory[offset:offset + 8] = struct.pack("=Q", value)


def _environment():
    # Credentials arrive only through the private request, never libpq's PG*
    # variables or provider/application settings inherited from the worker.
    allowed = {"PATH", "SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "TMP", "TEMP", "TMPDIR", "LD_LIBRARY_PATH"}
    return {key: value for key, value in os.environ.items() if key.upper() in allowed}


class NotificationListener:
    """Owned by the gateway thread; read() never calls the driver or blocks on IO."""

    def __init__(self, kwargs):
        from app.db.backend_probe import _interpreter
        import psycopg2

        self._directory = tempfile.TemporaryDirectory(prefix="event-listener-")
        self._memory = None
        self._child = None
        self._jobs = []
        self._job_api = None
        self._seen = 0
        self._started = time.monotonic()
        directory = Path(self._directory.name)
        try:
            request = directory / "request"
            with request.open("x", encoding="utf-8") as stream:
                os.chmod(request, 0o600)
                json.dump({"path": [str(Path(psycopg2.__file__).parents[1])],
                           "kwargs": kwargs}, stream)
            reply = directory / "mailbox"
            with reply.open("x+b") as stream:
                os.chmod(reply, 0o600)
                stream.truncate(_SIZE)
                self._memory = mmap.mmap(stream.fileno(), _SIZE)
            _store(self._memory, 16, time.monotonic_ns())
            options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
            if os.name != "nt" and not subprocess._USE_POSIX_SPAWN:
                raise RuntimeError("posix_spawn unavailable")
            self._child = subprocess.Popen(
                [_interpreter(), "-I", "-S", str(_CHILD), str(request), str(reply)],
                close_fds=False, env=_environment(), stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **options)
            if os.name == "nt":
                self._admit_windows()
        except BaseException:
            self.close()
            raise

    def _admit_windows(self):
        # Reuse the projection child's pinned identity / Job helpers. Both the
        # launcher and actual runtime are inert until admission, including venv
        # redirectors. Parent-only handles let the kernel kill even a frozen
        # interpreter when this worker dies, without a child-side Python check.
        from app.core.transcript_views.child_process import _job_object
        from app.core.transcript_views.windows_process import _pinned_windows_processes

        deadline = time.monotonic() + 5
        while not (runtime_pid := _number(self._memory, 24)):
            if self._child.poll() is not None or time.monotonic() >= deadline:
                raise RuntimeError("LISTEN child admission failed")
            time.sleep(.01)
        self._job_api = _job_object()
        with _pinned_windows_processes(self._child.pid, runtime_pid) as targets:
            for _, handle in targets:
                job = self._job_api.assign_kill_on_close_job_handle(handle)
                if job is None:
                    raise RuntimeError("LISTEN child ownership failed")
                self._jobs.append(job)
            _store(self._memory, 32, 1)

    @property
    def connected(self):
        heartbeat = _number(self._memory, 8)
        return bool(heartbeat and (time.monotonic_ns() - heartbeat) / 1e9 < _HEARTBEAT_SECONDS)

    def read(self):
        """Return (payloads, overflow); refuse stale/dead children, even if libpq froze."""
        _store(self._memory, 16, time.monotonic_ns())
        if self._child.poll() is not None:
            raise RuntimeError("LISTEN child exited")
        heartbeat = _number(self._memory, 8)
        age = ((time.monotonic_ns() - heartbeat) / 1e9 if heartbeat
               else time.monotonic() - self._started)
        if age > _HEARTBEAT_SECONDS:
            raise TimeoutError("LISTEN child heartbeat expired")
        latest = _number(self._memory, 0)
        overflow = latest - self._seen > _SLOTS
        if overflow:
            self._seen = latest - _SLOTS
        payloads = []
        while self._seen < latest:
            sequence = self._seen + 1
            offset = _HEADER_BYTES + (sequence % _SLOTS) * _SLOT_BYTES
            before = _number(self._memory, offset)
            length = struct.unpack_from("=I", self._memory, offset + 8)[0]
            if before != sequence or length > 8000:
                overflow = True
                break
            data = self._memory[offset + 12:offset + 12 + length]
            if _number(self._memory, offset) != sequence:
                overflow = True
                break
            payloads.append(data.decode("utf-8"))
            self._seen = sequence
        if overflow:
            # A concurrent overwrite is a gap too. A recheck restores all
            # session readers, even if the missing hint was a final event.
            self._seen = latest
        return payloads, overflow

    def close(self):
        child = self._child
        self._child = None
        try:
            if child is not None:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=2)
        finally:
            for job in self._jobs:
                self._job_api.close_job(job)
            self._jobs.clear()
            if self._memory is not None:
                self._memory.close()
                self._memory = None
            self._directory.cleanup()


def _publish(memory, sequence, payload):
    data = payload.encode("utf-8")
    if len(data) > 8000:
        raise ValueError("oversized notification")
    offset = _HEADER_BYTES + (sequence % _SLOTS) * _SLOT_BYTES
    _store(memory, offset, 0)  # Invalidate before touching a previously published slot.
    struct.pack_into("=I", memory, offset + 8, len(data))
    memory[offset + 12:offset + 12 + len(data)] = data
    _store(memory, offset, sequence)
    _store(memory, 0, sequence)


def _listen(kwargs, memory):
    import psycopg2

    conn = psycopg2.connect(**kwargs)
    conn.autocommit = True
    conn.cursor().execute("LISTEN session_events")
    sequence = 0
    while True:
        if hasattr(signal, "alarm"):
            signal.alarm(30)  # Kernel deadline still fires with a frozen GIL.
        now = time.monotonic_ns()
        if (now - _number(memory, 16)) / 1e9 > 15:
            return  # Parent died or stopped reading.
        _store(memory, 8, now)
        if select.select([conn], [], [], 0.5)[0]:
            conn.poll()
            while conn.notifies:
                sequence += 1
                _publish(memory, sequence, conn.notifies.pop(0).payload)


def _main():
    if hasattr(signal, "SIGALRM"):
        signal.signal(signal.SIGALRM, signal.SIG_DFL)
        signal.alarm(30)
    with open(sys.argv[1], encoding="utf-8") as stream:
        request = json.load(stream)
    sys.path.extend(request["path"])
    with open(sys.argv[2], "r+b") as stream, mmap.mmap(stream.fileno(), _SIZE) as memory:
        if os.name == "nt":
            _store(memory, 24, os.getpid())
            deadline = time.monotonic() + 5
            while not _number(memory, 32):
                if time.monotonic() >= deadline:
                    return  # Parent died before admission; never enter the driver.
                time.sleep(.01)
        _listen(request["kwargs"], memory)


if __name__ == "__main__":
    try:
        _main()
    except BaseException:
        os._exit(1)  # Never print connection details; parent reconnects on exit.
    os._exit(0)
