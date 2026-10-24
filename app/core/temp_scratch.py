"""Stdlib-only ownership receipt shared by checks and the app's scratch reaper."""
import errno
import json
import os
import signal
import tempfile
import time
import warnings
from collections.abc import Iterator
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
def cleanup_on_sigterm(*, handler=None):
    """Unwind cleanup on termination without replacing staged/custom handlers."""
    def stop(signum, frame):
        raise SystemExit(128 + signum)

    previous = signal.getsignal(signal.SIGTERM)
    installed = False
    if previous == signal.SIG_DFL:
        try:
            signal.signal(signal.SIGTERM, handler or stop)
            installed = True
        except ValueError:  # signals can only be installed by the main thread
            pass
    try:
        yield
    finally:
        if installed:
            signal.signal(signal.SIGTERM, previous)


def _cleanup_scratch(
    directory: tempfile.TemporaryDirectory[str], receipt: str | None,
    *, warn_on_cleanup_race: bool,
) -> None:
    for attempt in range(3):
        try:
            directory.cleanup()
            return
        except OSError as exc:
            if not warn_on_cleanup_race or exc.errno != errno.ENOTEMPTY:
                raise
            if attempt == 2:
                # rmtree may have removed the marker before a late write refilled it.
                # Keep the ownership receipt so the dead-owner reaper can recover it.
                if receipt is not None:
                    (Path(directory.name) / MARKER).write_text(receipt, encoding="utf-8")
                warnings.warn(
                    "scratch cleanup raced with a late writer; "
                    f"leftover after 3 attempts: {directory.name}",
                    RuntimeWarning, stacklevel=2,
                )
                return
            warnings.warn(
                "scratch cleanup raced with a late writer; "
                f"retrying removal: {directory.name}",
                RuntimeWarning, stacklevel=2,
            )
            time.sleep(0.05)


@contextmanager
def scratch_directory(
    prefix: str, *, root: str | Path | None = None, warn_on_cleanup_race: bool = False,
) -> Iterator[str]:
    if prefix not in PREFIXES:
        raise ValueError("Scratch prefix is not allowlisted")
    directory = tempfile.TemporaryDirectory(prefix=prefix, dir=root)
    receipt = None
    try:
        if os.name == "posix" and Path("/proc/self/stat").exists():
            receipt = json.dumps({
                "version": 1, "pid": os.getpid(), "start": process_identity(os.getpid()),
                "created": time.time(), "uid": os.getuid(),
            })
            (Path(directory.name) / MARKER).write_text(receipt, encoding="utf-8")
        yield directory.name
    finally:
        _cleanup_scratch(directory, receipt, warn_on_cleanup_race=warn_on_cleanup_race)
