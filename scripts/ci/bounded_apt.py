"""Two bounded apt/Playwright attempts on disposable Linux CI runners.

Elevate the supervisor, not just apt: a user-owned timeout cannot reliably kill
sudo's root children. Preserve HOME/PATH so managed Chromium stays in the runner's
usual cache. Lock waiting and interrupted-dpkg repair share the install budget.
Only missing packages are installed; preinstalled runner libraries are not upgraded.
"""
from __future__ import annotations

import fcntl
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

# Recent completed installs took 24-31s, but slow-mirror samples exceeded two
# 90s attempts. Five minutes covers the observed ~150 kB/s package transfer with
# index/install margin; this is a bounded download budget, not a test timeout.
ATTEMPT_SECONDS = 300
KILL_AFTER_SECONDS = 10
APT_OPTIONS = '''
APT::Get::Upgrade "false";
Dir::Etc::sourcelist "sources.list.d/ubuntu.sources";
Dir::Etc::sourceparts "-";
Acquire::Languages "none";
'''
LOCKS = (
    "/var/lib/dpkg/lock-frontend",
    "/var/lib/dpkg/lock",
    "/var/lib/apt/lists/lock",
    "/var/cache/apt/archives/lock",
)


def wait_for_locks() -> None:
    # apt/dpkg use POSIX record locks, not flock. Never remove the lock files:
    # unlinking them would let two package managers mutate the database at once.
    while True:
        try:
            for name in LOCKS:
                path = Path(name)
                if not path.exists():
                    continue
                with path.open("r+") as lock:
                    fcntl.lockf(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            time.sleep(0.2)


def kill_group(pid: int, sig: int) -> None:
    try:
        os.killpg(pid, sig)
    except ProcessLookupError:
        pass


def main(args: list[str]) -> int:
    if args[0] == "--attempt":
        wait_for_locks()
        if args[1] == "repair":
            subprocess.run(["dpkg", "--configure", "-a"], check=True)
        os.execvp(args[2], args[2:])

    if args[0] != "--root":
        os.execvp("sudo", ["sudo", "-n", "env", f"HOME={os.environ['HOME']}",
                           f"PATH={os.environ['PATH']}", sys.executable,
                           str(Path(__file__).resolve()), "--root", *args])

    # Playwright invokes apt itself. APT_CONFIG applies --no-upgrade to those
    # calls too, so apt skips installed libraries instead of downloading newer
    # candidates. Missing packages still use apt's normal signed-index checks.
    # Restrict refresh to Ubuntu and omit translations rather than fetching
    # unrelated runner feeds and language indexes on every disposable machine.
    with tempfile.TemporaryDirectory(prefix="ci-apt-") as directory:
        config = Path(directory) / "apt.conf"
        config.write_text(APT_OPTIONS)
        os.environ["APT_CONFIG"] = str(config)
        return supervise(args[1:])


def supervise(command: list[str]) -> int:
    def stop(signum: int, _frame: object) -> None:
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    interrupted = False
    for attempt in (1, 2):
        started = time.monotonic()
        print(f"CI apt attempt {attempt}: budget {ATTEMPT_SECONDS}s", flush=True)
        # No nested sudo is needed: the timeout supervisor and its entire new
        # session are root-owned, including Playwright's apt children.
        child = subprocess.Popen(
            [sys.executable, __file__, "--attempt",
             "repair" if interrupted else "clean", *command],
            start_new_session=True,
        )
        try:
            result = child.wait(timeout=ATTEMPT_SECONDS)
            interrupted = result < 0 or result in (124, 137)
        except subprocess.TimeoutExpired:
            interrupted = True
            result = 124
            kill_group(child.pid, signal.SIGTERM)
            try:
                child.wait(timeout=KILL_AFTER_SECONDS)
            except subprocess.TimeoutExpired:
                pass
        finally:
            # Always kill the group, even if its leader exited on TERM while an
            # apt grandchild ignored it. Reap before any lock probe or retry.
            kill_group(child.pid, signal.SIGKILL)
            child.wait()
        elapsed = time.monotonic() - started
        if result == 0:
            print(f"CI apt attempt {attempt}: completed in {elapsed:.1f}s", flush=True)
            return 0
        print(f"::warning::Dependency install attempt {attempt} failed "
              f"(exit {result}; deadline {ATTEMPT_SECONDS}s; elapsed {elapsed:.1f}s)", flush=True)
    print("::error::Dependency installation failed after two bounded attempts", flush=True)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
