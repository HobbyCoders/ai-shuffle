"""Finish a dev instance's teardown when its launcher dies without running it.

``python -m scripts.devloop up`` cleans up on Ctrl-C, SIGTERM, SIGHUP, parent
loss and its TTL, but nothing runs in a SIGKILLed process. Monitor's TaskStop
does exactly that: it SIGTERMs the task's process group and, about a second
later, SIGKILLs every descendant it saw, which cuts ``up`` off in the middle
of stopping the app and PostgreSQL. Its status file then says "ready" for a
dead instance and its scratch directory (PostgreSQL data, workspace) stays.

``up`` starts this guardian first. It detaches twice (a new session, then a
fork whose parent exits), so it is neither in the task's process group nor a
descendant of the task, and survives both kills. It waits for the launcher
(pid plus start time, so a reused pid is not mistaken for it) to be gone,
then:

* SIGKILLs the app, Vite and PostgreSQL process groups the status recorded,
  each only while its pid still has the recorded start time, and any
  postmaster still running on the scratch directory's data;
* removes the status file if it still names the dead launcher (a newer ``up``
  for the checkout keeps its own);
* removes the scratch directory if its ownership receipt names the dead
  launcher, and PostgreSQL's private socket directory.

After a normal teardown there is nothing left and it exits quietly.
Standard library only, and it inherits no environment but PATH/LANG.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
POLL_SECONDS = 0.5
#: Longer than any instance lives (TTL at most four hours): never immortal.
MAX_LIFETIME_SECONDS = 5 * 3600
#: How long killed processes get to vanish before their files are removed.
KILL_WAIT_SECONDS = 10
SCRATCH_MARKER = ".ai-shuffle-scratch-owner"  # app.core.temp_scratch.MARKER
SCRATCH_PREFIX = "ai-shuffle-preview-"
SOCKET_PREFIX = "shuffle-pg-"


def _stat_fields(pid: int, proc: Path = Path("/proc")) -> list[str] | None:
    try:
        return (proc / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()
    except (OSError, IndexError):
        return None


def identity(pid: int) -> str | None:
    """Start time in clock ticks (``app.core.temp_scratch.process_identity``)."""
    fields = _stat_fields(pid)
    return fields[19] if fields and len(fields) > 19 else None


def alive(pid: int, start: str | None) -> bool:
    """``pid`` is still the process that had start time ``start`` (zombies are gone)."""
    if not isinstance(pid, int) or pid <= 1 or not start:
        return False
    fields = _stat_fields(pid)
    return bool(fields) and len(fields) > 19 and fields[19] == start and fields[0] != "Z"


# ─── spawning ──────────────────────────────────────────────────────────────


def spawn_guardian(status: Path, scratch: Path, *, launcher_pid: int | None = None) -> dict | None:
    """Start the detached guardian for ``launcher_pid`` (default: this process).

    Returns ``{"pid": ..., "pid_identity": ...}`` of the guardian, or None if
    it could not start (the instance still works; only the SIGKILL backstop is
    missing, which is logged).
    """
    pid = launcher_pid or os.getpid()
    start = identity(pid)
    if start is None:
        return None
    env = {key: os.environ[key] for key in ("PATH", "LANG") if key in os.environ}
    read, write = os.pipe()
    try:
        intermediate = subprocess.Popen(
            [sys.executable, "-m", "scripts.devloop.guardian", "--launcher-pid", str(pid),
             "--launcher-identity", start, "--status", str(status), "--scratch", str(scratch),
             "--report-fd", str(write)],
            cwd=ROOT, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True, pass_fds=(write,), close_fds=True,
        )
    except OSError:
        os.close(read)
        os.close(write)
        return None
    os.close(write)
    try:
        with os.fdopen(read, "rb") as stream:
            reported = stream.read(64).decode().strip()  # EOF once both ends exit/close
        intermediate.wait(timeout=10)
    except (OSError, subprocess.TimeoutExpired, UnicodeDecodeError):
        return None
    if not reported.isdigit():
        return None
    guardian = int(reported)
    return {"pid": guardian, "pid_identity": identity(guardian)}


def _detach(report_fd: int) -> None:
    """Fork once more and exit the parent: the guardian is nobody's descendant.

    Its parent becomes init (or the nearest subreaper), so a tree walk from
    the task's shell no longer finds it; ``start_new_session`` already took it
    out of the task's process group.
    """
    child = os.fork()
    if child:
        os.write(report_fd, str(child).encode())
        os.close(report_fd)
        os._exit(0)
    os.close(report_fd)
    for sig in (signal.SIGHUP, signal.SIGINT):
        signal.signal(sig, signal.SIG_IGN)


# ─── cleanup ───────────────────────────────────────────────────────────────


def _read_owned_json(path: Path) -> dict | None:
    """A regular file this user owns, never through a link; None otherwise."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            return None
        try:
            data = json.loads(stream.read().decode("utf-8"))
        except ValueError:
            return None
    return data if isinstance(data, dict) else None


def _kill_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def _postmaster(scratch: Path) -> int | None:
    """The postmaster still running on this scratch directory's data, if any."""
    data = scratch / "data" / "postgres" / "data"
    try:
        pid = int((data / "postmaster.pid").read_text().splitlines()[0])
        command = (Path("/proc") / str(pid) / "cmdline").read_bytes().split(b"\0")
        owner = os.stat(Path("/proc") / str(pid)).st_uid
    except (OSError, ValueError, IndexError):
        return None
    if owner != os.getuid() or os.fsencode(str(data)) not in command:
        return None
    return pid


def _owned_scratch(scratch: Path, launcher_pid: int, launcher_identity: str) -> bool:
    if not scratch.name.startswith(SCRATCH_PREFIX) or scratch.is_symlink():
        return False
    receipt = _read_owned_json(scratch / SCRATCH_MARKER)
    return bool(receipt) and receipt.get("pid") == launcher_pid \
        and receipt.get("start") == launcher_identity and receipt.get("uid") == os.getuid()


def _private_socket_dir(value: object) -> Path | None:
    if not isinstance(value, str):
        return None
    path = Path(value)
    try:
        info = os.lstat(path)
    except OSError:
        return None
    if (not path.name.startswith(SOCKET_PREFIX) or path.parent != Path(tempfile.gettempdir())
            or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()):
        return None
    return path


def cleanup(status_path: Path, scratch: Path, launcher_pid: int, launcher_identity: str) -> list[str]:
    """Undo what the dead launcher left behind; returns what was done."""
    done: list[str] = []
    status = _read_owned_json(status_path)
    names_launcher = bool(status) and status.get("pid") == launcher_pid \
        and status.get("pid_identity") == launcher_identity
    owned = _owned_scratch(scratch, launcher_pid, launcher_identity)
    victims: list[tuple[str, int, str | None]] = []
    if names_launcher:
        for key in ("app", "vite", "postgres"):
            record = status.get(key) or {}
            pid, start = record.get("pid"), record.get("pid_identity")
            if alive(pid, start):
                victims.append((key, pid, start))
    if owned:
        postmaster = _postmaster(scratch)
        if postmaster is not None and all(pid != postmaster for _, pid, _ in victims):
            victims.append(("postgres", postmaster, identity(postmaster)))
    for name, pid, _ in victims:
        _kill_group(pid)
        done.append(f"killed {name} (pid {pid})")
    deadline = time.monotonic() + KILL_WAIT_SECONDS
    while any(alive(pid, start) for _, pid, start in victims) and time.monotonic() < deadline:
        time.sleep(0.1)
    if names_launcher:
        socket_dir = _private_socket_dir((status.get("postgres") or {}).get("socket_dir"))
        if socket_dir is not None:
            shutil.rmtree(socket_dir, ignore_errors=True)
            done.append(f"removed {socket_dir}")
        # Re-read right before removing: a newer `up` may have replaced it.
        current = _read_owned_json(status_path)
        if current and current.get("pid") == launcher_pid \
                and current.get("pid_identity") == launcher_identity:
            status_path.unlink(missing_ok=True)
            done.append(f"removed stale status {status_path}")
    if owned:
        shutil.rmtree(scratch, ignore_errors=True)
        done.append(f"removed {scratch}")
    return done


def watch(status_path: Path, scratch: Path, launcher_pid: int, launcher_identity: str,
          *, poll: float = POLL_SECONDS, lifetime: float = MAX_LIFETIME_SECONDS) -> list[str]:
    """Wait for the launcher to be gone, then :func:`cleanup` after it."""
    deadline = time.monotonic() + lifetime
    while alive(launcher_pid, launcher_identity):
        if time.monotonic() >= deadline:
            return []
        time.sleep(poll)
    done = cleanup(status_path, scratch, launcher_pid, launcher_identity)
    if done:
        _note(status_path, launcher_pid, done)
    return done


def _note(status_path: Path, launcher_pid: int, done: list[str]) -> None:
    """One line beside the status file: the evidence that a teardown was cut off."""
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    line = f"{stamp} launcher {launcher_pid} died before its teardown: {'; '.join(done)}\n"
    log = status_path.with_name(status_path.name + ".guardian.log")
    try:
        fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as stream:
            stream.write(line)
    except OSError:
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.devloop.guardian",
                                     description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--launcher-pid", type=int, required=True)
    parser.add_argument("--launcher-identity", required=True)
    parser.add_argument("--status", type=Path, required=True)
    parser.add_argument("--scratch", type=Path, required=True)
    parser.add_argument("--report-fd", type=int,
                        help="Detach first and write the guardian's pid here (spawn_guardian)")
    args = parser.parse_args(argv)
    if args.report_fd is not None:
        _detach(args.report_fd)
    watch(args.status, args.scratch, args.launcher_pid, args.launcher_identity)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
