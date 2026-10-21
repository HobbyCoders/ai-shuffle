"""Bounded Linux backstop for receipted AI Shuffle scratch, not a /tmp cleaner.

Generic tmp*, probe-, cdp-, ci- and unmarked legacy directories are NEVER owned.
Permission/scan failures fail closed. A dead owner plus a six-hour TTL is needed
in addition to checking every process's cwd and open descriptors.
"""
import asyncio
import json
import logging
import os
import shutil
import stat
import tempfile
import time
from pathlib import Path

from app.core.temp_scratch import MARKER, PREFIXES, process_identity

log = logging.getLogger(__name__)
MAX_ENTRIES = 10000
MAX_TREE_ENTRIES = 2048
MAX_REMOVALS = 8
SWEEP_SECONDS = 2.0


def _references(proc, deadline):
    references = set()
    count = 0
    # No /proc (or unreadable processes): do not guess that scratch is unused.
    with os.scandir(proc) as processes:
        for process in processes:
            if not process.name.isdigit():
                continue
            count += 1
            if count > MAX_ENTRIES or time.monotonic() >= deadline:
                return None
            base = Path(process.path)
            try:
                links = [base / "cwd", base / "root"]
                with os.scandir(base / "fd") as descriptors:
                    for descriptor in descriptors:
                        links.append(Path(descriptor.path))
                        count += 1
                        if count > MAX_ENTRIES or time.monotonic() >= deadline:
                            return None
                for link in links:
                    try:
                        value = os.readlink(link)
                    except (FileNotFoundError, ProcessLookupError):
                        continue
                    if value.startswith("/"):
                        references.add(value.removesuffix(" (deleted)"))
            except (FileNotFoundError, ProcessLookupError):
                continue  # exited during observation
    return references


def _small_stale_tree(path, cutoff, device, deadline):
    pending = [path]
    count = 0
    while pending:
        with os.scandir(pending.pop()) as entries:
            for entry in entries:
                count += 1
                if count > MAX_TREE_ENTRIES or time.monotonic() >= deadline:
                    return False
                info = entry.stat(follow_symlinks=False)
                if entry.name == ".git":
                    return False  # a checkout is never disposable scratch
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
                    if (info.st_uid != os.getuid() or info.st_mtime > cutoff
                            or marker.is_symlink() or marker.stat().st_size > 1024):
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
        return removed  # inaccessible process metadata: fail closed
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
