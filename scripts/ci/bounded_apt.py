"""Two bounded apt/Playwright attempts on disposable Linux CI runners.

Elevate the supervisor, not just apt: a user-owned timeout cannot reliably kill
sudo's root children. Preserve HOME/PATH so managed Chromium stays in the runner's
usual cache. Lock waiting and interrupted-dpkg repair share the install budget.
Only missing packages are installed; preinstalled runner libraries are not upgraded.

``--debs DIR COMMAND...`` keeps the packages a successful run downloaded in DIR,
with a manifest, for the workflow to cache. When DIR already holds a manifest
(a cache hit), those exact signed .deb files are installed with no remote apt
source, so nothing is downloaded and no index is refreshed, and COMMAND runs only if that install or its check fails.
"""
from __future__ import annotations

import fcntl
import os
from pathlib import Path
import shutil
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
# A hit installs about 21 MB of local fonts and libraries in 4-12s on hosted
# runners. Keep this short: it comes on top of both apt attempts and the browser
# download inside the workflow step's 13-minute deadline (see test_bounded_apt).
CACHED_SECONDS = 25
MANIFEST = "packages.txt"
ARCHIVES = Path("/var/cache/apt/archives")
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
        if args[0] == "--debs":  # Root resolves paths from its own directory.
            args = ["--debs", str(Path(args[1]).resolve()), *args[2:]]
        os.execvp("sudo", ["sudo", "-n", "env", f"HOME={os.environ['HOME']}",
                           f"PATH={os.environ['PATH']}", sys.executable,
                           str(Path(__file__).resolve()), "--root", *args])

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    command, debs = args[1:], None
    if command[0] == "--debs":
        debs, command = Path(command[1]), command[2:]
    # Playwright invokes apt itself. APT_CONFIG applies --no-upgrade to those
    # calls too, so apt skips installed libraries instead of downloading newer
    # candidates. Missing packages still use apt's normal signed-index checks.
    # Restrict refresh to Ubuntu and omit translations rather than fetching
    # unrelated runner feeds and language indexes on every disposable machine.
    with tempfile.TemporaryDirectory(prefix="ci-apt-") as directory:
        config = Path(directory) / "apt.conf"
        # A cache needs the downloads apt-get keeps in its archive directory.
        config.write_text(APT_OPTIONS + ('APT::Keep-Downloaded-Packages "true";\n' if debs else ""))
        os.environ["APT_CONFIG"] = str(config)
        cached_failed, before = False, set()
        if debs is not None:
            if (debs / MANIFEST).is_file():
                if install_cached(debs, Path(directory)):
                    return 0
                # Start over from apt; repair whatever the failed install left.
                cached_failed = True
            shutil.rmtree(debs, ignore_errors=True)
            before = set(ARCHIVES.glob("*.deb"))
        # Root npx must not create root-owned entries in the runner's ~/.npm.
        # Only dependency setup uses this disposable cache; browser downloads
        # remain a separate, runner-owned workflow command.
        os.environ["npm_config_cache"] = str(Path(directory) / "npm")
        result = supervise(command, repair_first=cached_failed)
        if result == 0 and debs is not None:
            keep_downloads(debs, before)
        return result


def bounded(command: list[str], mode: str, seconds: float) -> int:
    """Run COMMAND in its own root session once; 124 when it overran SECONDS."""
    # No nested sudo is needed: the timeout supervisor and its entire new
    # session are root-owned, including Playwright's apt children.
    child = subprocess.Popen([sys.executable, __file__, "--attempt", mode, *command],
                             start_new_session=True)
    try:
        return child.wait(timeout=seconds)
    except subprocess.TimeoutExpired:
        kill_group(child.pid, signal.SIGTERM)
        try:
            child.wait(timeout=KILL_AFTER_SECONDS)
        except subprocess.TimeoutExpired:
            pass
        return 124
    finally:
        # Always kill the group, even if its leader exited on TERM while an
        # apt grandchild ignored it. Reap before any lock probe or retry.
        kill_group(child.pid, signal.SIGKILL)
        child.wait()


def stop(signum: int, _frame: object) -> None:
    # Unwind through bounded()'s finally, which kills the whole apt group.
    raise SystemExit(128 + signum)


def supervise(command: list[str], repair_first: bool = False) -> int:
    interrupted = repair_first
    for attempt in (1, 2):
        started = time.monotonic()
        print(f"CI apt attempt {attempt}: budget {ATTEMPT_SECONDS}s", flush=True)
        result = bounded(command, "repair" if interrupted else "clean", ATTEMPT_SECONDS)
        interrupted = result < 0 or result in (124, 137)
        elapsed = time.monotonic() - started
        if result == 0:
            print(f"CI apt attempt {attempt}: completed in {elapsed:.1f}s", flush=True)
            return 0
        print(f"::warning::Dependency install attempt {attempt} failed "
              f"(exit {result}; deadline {ATTEMPT_SECONDS}s; elapsed {elapsed:.1f}s)", flush=True)
    print("::error::Dependency installation failed after two bounded attempts", flush=True)
    return 1


def install_cached(debs: Path, scratch: Path) -> bool:
    """Install a previous run's downloads from DIR; False means use apt as usual."""
    started = time.monotonic()
    packages = (debs / MANIFEST).read_text().split()
    files = sorted(str(path) for path in debs.glob("*.deb"))
    if not packages or not files:
        print("::warning::The cached dependency packages are empty; using apt", flush=True)
        return False
    # Local files only: with no remote source apt knows just the installed
    # system and these files, so it refuses, before changing anything, whatever
    # needs another package or a downgrade. Already-installed versions are left
    # as they are. (--no-download would skip the local copy step too, and apt
    # then fails with "Pathname to install is not absolute".)
    (scratch / "none").mkdir()
    (scratch / "none.list").touch()
    result = bounded(["apt-get", "install", "-y", "-q", "--no-install-recommends",
                      f"-oDir::Etc::sourcelist={scratch / 'none.list'}",
                      f"-oDir::Etc::sourceparts={scratch / 'none'}", *files],
                     "clean", CACHED_SECONDS)
    if result != 0:
        print(f"::warning::Cached dependency install failed (exit {result}); using apt", flush=True)
        return False
    missing = [name for name in packages if not installed(name)]
    if missing:
        print(f"::warning::Cached dependency install left {', '.join(missing)} missing; using apt",
              flush=True)
        return False
    print(f"CI apt: installed {len(files)} cached packages in "
          f"{time.monotonic() - started:.1f}s", flush=True)
    return True


def installed(package: str) -> bool:
    status = subprocess.run(["dpkg-query", "-W", "-f=${db:Status-Status}", package],
                            capture_output=True, text=True, check=False)
    return status.returncode == 0 and status.stdout.strip() == "installed"


def keep_downloads(debs: Path, before: set[Path]) -> None:
    """Copy what this install downloaded into DIR, with its package names, for the cache."""
    downloaded = sorted(set(ARCHIVES.glob("*.deb")) - before)
    if not downloaded:
        # Nothing new to keep: an empty cache would make the next hit install nothing.
        print("CI apt: nothing was downloaded; not caching", flush=True)
        return
    try:
        names = sorted(subprocess.run(["dpkg-deb", "--show", "--showformat=${Package}", str(path)],
                                      capture_output=True, text=True, check=True).stdout.strip()
                       for path in downloaded)
        debs.mkdir(parents=True)
        for path in downloaded:
            shutil.copy(path, debs / path.name)
        (debs / MANIFEST).write_text("".join(f"{name}\n" for name in names))
        # The cache save and restore run as the runner user.
        owner = (int(os.environ.get("SUDO_UID", os.getuid())),
                 int(os.environ.get("SUDO_GID", os.getgid())))
        for path in [debs, *debs.iterdir()]:
            os.chown(path, *owner)
    except (OSError, subprocess.CalledProcessError) as error:
        # The install itself succeeded; only the next run's shortcut is lost.
        print(f"::warning::Not caching the downloaded packages: {error}", flush=True)
        shutil.rmtree(debs, ignore_errors=True)
        return
    print(f"CI apt: kept {len(names)} downloaded packages for the cache", flush=True)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
