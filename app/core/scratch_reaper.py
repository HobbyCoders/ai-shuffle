"""Bounded Linux backstop for receipted AI Shuffle scratch, not a /tmp cleaner.

Generic tmp*, probe-, cdp-, ci- and unmarked legacy directories are NEVER owned.
Potentially relevant permission/scan failures fail closed. A dead owner plus a
six-hour TTL is needed in addition to checking process cwd and open descriptors.
"""
import asyncio
import json
import logging
import os
import re
import shutil
import stat
import tempfile
import time
from pathlib import Path

from app.core.temp_scratch import MARKER, PREFIXES, process_identity

log = logging.getLogger(__name__)
MAX_ENTRIES = 10000
MAX_DESCRIPTORS = 100000
_disabled_logged = False
MAX_TREE_ENTRIES = 2048
MAX_REMOVALS = 8
SWEEP_SECONDS = 2.0


def _disabled(reason):
    global _disabled_logged
    if not _disabled_logged:
        log.warning("Owned scratch sweep disabled: %s", reason)
        _disabled_logged = True
    return None


def _uninspectable_is_unrelated(base, proc):
    status = dict(line.split(":", 1) for line in (base / "status").read_text().splitlines() if ":" in line)
    uids = {int(uid) for uid in status["Uid"].split()}
    if 0 not in uids:
        # Retaining cwd/fds then switching every UID to another user is a known gap.
        if os.getuid() not in uids:
            return True
        return _is_application_process(base, status, proc)
    # Only exempt established init ancestors, not arbitrary root daemons or
    # elevated children. Init shims predate our scratch and only reap/forward
    # signals; they cannot have inherited its cwd/fds. Trust their fixed role
    # not to open application scratch later. Unknown root processes fail closed.
    name = status["Name"].strip()
    if name not in {"tini", "docker-init"} and not (base.name == "1" and name in {"init", "systemd"}):
        return False
    return _is_ancestor(base, proc)


def _is_application_process(base, status, proc):
    # Normal startup makes app/runner processes non-dumpable. Their scratch
    # receipts name their own PID, so the live-owner check protects it; app code
    # does not keep another, dead owner's scratch open. The container supervisor
    # only starts/reaps children and never opens their scratch.
    if not re.fullmatch(r"python(?:[0-9]+(?:\.[0-9]+)*)?", status["Name"].strip()):
        return False
    argv = (base / "cmdline").read_bytes().rstrip(b"\0").split(b"\0")
    if not argv or not re.fullmatch(r"python(?:[0-9]+(?:\.[0-9]+)*)?", Path(os.fsdecode(argv[0])).name):
        return False
    args = argv[1:]
    while args and args[0] in {b"-s", b"-I", b"-E", b"-u", b"-B"}:
        args = args[1:]
    if args in ([b"-m", b"app.main"], [b"-m", b"app.preview_process"]):
        return True
    if len(args) == 2 and args[0] == b"-c":
        from docker.agent_isolation import APP_SHIM
        return args[1] == APP_SHIM.encode()
    # The installed supervisor hands over to this exact script shape. Restrict
    # this launcher exemption to our ancestry, never a similarly named sibling.
    if len(args) == 2 and args[1] == b"run":
        script = Path(os.fsdecode(args[0]))
        return script.is_absolute() and script.parts[-2:] == ("docker", "container_runtime.py") and _is_ancestor(base, proc)
    return False


def _is_ancestor(base, proc):
    pid = os.getpid()
    seen = set()
    while pid and pid not in seen and len(seen) < MAX_ENTRIES:
        seen.add(pid)
        try:
            parent = dict(line.split(":", 1) for line in (proc / str(pid) / "status").read_text().splitlines() if ":" in line)
        except (FileNotFoundError, ProcessLookupError):
            return False  # incomplete ancestry cannot establish an exemption
        pid = int(parent["PPid"])
        if str(pid) == base.name:
            return True
    return False


def _references(proc, deadline):
    references = set()
    count = 0
    descriptor_count = 0
    # No /proc or an unreadable potentially relevant process: fail closed.
    with os.scandir(proc) as processes:
        for process in processes:
            if not process.name.isdigit():
                continue
            count += 1
            if count > MAX_ENTRIES or time.monotonic() >= deadline:
                return _disabled("process scan limit reached")
            base = Path(process.path)
            try:
                links = [base / "cwd", base / "root"]
                with os.scandir(base / "fd") as descriptors:
                    for descriptor in descriptors:
                        links.append(Path(descriptor.path))
                        descriptor_count += 1
                        if descriptor_count > MAX_DESCRIPTORS or time.monotonic() >= deadline:
                            return _disabled("descriptor scan limit reached")
                for link in links:
                    try:
                        value = os.readlink(link)
                    except (FileNotFoundError, ProcessLookupError):
                        continue
                    if value.startswith("/"):
                        references.add(value.removesuffix(" (deleted)"))
            except (FileNotFoundError, ProcessLookupError):
                continue  # exited during observation
            except PermissionError:
                try:
                    if _uninspectable_is_unrelated(base, proc):
                        continue
                except (FileNotFoundError, ProcessLookupError):
                    continue
                except (OSError, ValueError, KeyError):
                    pass
                return _disabled("potentially relevant process is not inspectable")
    return references


def _small_stale_tree(path, cutoff, device, deadline):
    pending = [path]
    count = 0
    while pending:
        has_head = has_objects = False
        with os.scandir(pending.pop()) as entries:
            for entry in entries:
                count += 1
                if count > MAX_TREE_ENTRIES or time.monotonic() >= deadline:
                    return False
                info = entry.stat(follow_symlinks=False)
                if entry.name == ".git" or (entry.name in {"commondir", "gitdir"} and not stat.S_ISDIR(info.st_mode)):
                    return False  # checkouts and separate git stores are never scratch
                has_head |= entry.name == "HEAD" and not stat.S_ISDIR(info.st_mode)
                has_objects |= entry.name == "objects" and stat.S_ISDIR(info.st_mode)
                if has_head and has_objects:
                    return False
                if info.st_dev != device or info.st_mtime > cutoff:
                    return False
                if stat.S_ISDIR(info.st_mode):
                    pending.append(Path(entry.path))
    return True


def sweep(root=None, *, ttl=21600, proc=Path("/proc"), now=None):
    """Remove at most eight small stale trees; never follow a root symlink."""
    if os.name != "posix" or ttl <= 0:
        return 0
    root = Path(root or tempfile.gettempdir()).resolve()
    cutoff = (time.time() if now is None else now) - ttl
    deadline = time.monotonic() + SWEEP_SECONDS
    removed = 0
    try:
        references = _references(proc, deadline)
        if references is None:
            return 0
        with os.scandir(root) as entries:
            for index, entry in enumerate(entries):
                if index >= MAX_ENTRIES or removed >= MAX_REMOVALS or time.monotonic() >= deadline:
                    break
                if not entry.name.startswith(PREFIXES) or not entry.is_dir(follow_symlinks=False):
                    continue
                path = Path(entry.path)
                try:
                    info = entry.stat(follow_symlinks=False)
                    marker = path / MARKER
                    marker_info = marker.lstat()
                    if (info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_mtime > cutoff
                            or not stat.S_ISREG(marker_info.st_mode)
                            or marker_info.st_uid != os.getuid() or marker_info.st_size > 1024):
                        continue
                    receipt = json.loads(marker.read_text(encoding="utf-8"))
                    if (receipt.get("version") != 1 or receipt.get("uid") != os.getuid()
                            or not isinstance(receipt.get("created"), (int, float))
                            or not 0 < receipt["created"] <= cutoff
                            or type(receipt.get("pid")) is not int or receipt["pid"] <= 0
                            or not isinstance(receipt.get("start"), str)):
                        continue
                    if process_identity(receipt["pid"], proc) == receipt["start"]:
                        continue
                    if any(ref == str(path) or ref.startswith(str(path) + "/") for ref in references):
                        continue
                    if not _small_stale_tree(path, cutoff, info.st_dev, deadline):
                        continue
                    # Recheck identity immediately before the symlink-safe rmtree.
                    current = path.lstat()
                    if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                        continue
                    if not shutil.rmtree.avoids_symlink_attacks:
                        continue
                    shutil.rmtree(path)
                    removed += 1
                except (OSError, ValueError, TypeError):
                    continue
    except OSError:
        _disabled("process metadata is unavailable")
        return removed
    return removed


async def maintain_scratch():
    while True:
        await asyncio.sleep(1800)
        try:
            removed = await asyncio.to_thread(sweep)
            if removed:
                log.info("Removed %s stale owned scratch directories", removed)
        except Exception:
            log.exception("Owned scratch sweep failed")
