"""Stdlib-only ownership receipt shared by checks and the app's scratch reaper."""
import json
import os
import signal
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

MARKER = ".ai-shuffle-scratch-owner"
PREFIXES = ("ai-shuffle-ci-", "ai-shuffle-pytest-", "ai-shuffle-probe-", "ai-shuffle-preview-", "ai-shuffle-browser-")


def process_identity(pid, proc=Path("/proc")):
    """Linux start ticks distinguish a dead owner from a reused PID."""
    try:
        return (proc / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()[19]
    except (FileNotFoundError, ProcessLookupError):
        return None


@contextmanager
def cleanup_on_sigterm():
    """Unwind cleanup on termination without replacing staged/custom handlers."""
    def stop(signum, frame):
        raise SystemExit(128 + signum)

    previous = signal.getsignal(signal.SIGTERM)
    installed = False
    if previous == signal.SIG_DFL:
        try:
            signal.signal(signal.SIGTERM, stop)
            installed = True
        except ValueError:  # signals can only be installed by the main thread
            pass
    try:
        yield
    finally:
        if installed:
            signal.signal(signal.SIGTERM, previous)


@contextmanager
def scratch_directory(prefix, *, root=None):
    if prefix not in PREFIXES:
        raise ValueError("Scratch prefix is not allowlisted")
    with tempfile.TemporaryDirectory(prefix=prefix, dir=root) as directory:
        if os.name == "posix" and Path("/proc/self/stat").exists():
            receipt = {"version": 1, "pid": os.getpid(), "start": process_identity(os.getpid()),
                       "created": time.time(), "uid": os.getuid()}
            (Path(directory) / MARKER).write_text(json.dumps(receipt), encoding="utf-8")
        yield directory
