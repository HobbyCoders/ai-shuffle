"""Settled-session scratch, not durable harness state or a general disk cleaner.

Work is resumable (including the tree walk and individual unlinks), off-loop and
budgeted by time, files and bytes per paced slice, not a folder removal count. Unknown DB/process/filesystem facts keep
files. An hour of filesystem quiet protects commands outliving the settled flag.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import stat
import tempfile
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path

from fluid.state import safe_key

log = logging.getLogger(__name__)
QUIET_SECONDS = 3600
ORPHAN_SECONDS = 7 * 86400
PASS_SECONDS = 2
INTERVAL_SECONDS = 300
SLICE_PAUSE_SECONDS = 30
MAX_SLICE_FILES = 512
MAX_SLICE_BYTES = 256 * 1024 * 1024
MAX_SCAN_ENTRIES = 10000
KEY = re.compile(r"(?:[A-Za-z0-9_-]{1,160}|key~[0-9a-f]{64})\Z")


def _linked(info):
    # Covers Windows junctions and other reparse points even on Python <3.12.
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _real_directory(path):
    info = path.lstat()
    return stat.S_ISDIR(info.st_mode) and not _linked(info)


def _contained(root, path):
    if len(path.relative_to(root).parts) != 3 or path.parent.name != "scratch":
        return False
    return all(_real_directory(p) for p in (*root.parents, root, path.parent.parent, path.parent, path))


def _session_rows(key):
    """Fresh truth, including SDK ids used by older harness contexts.

    Unsafe ids are encoded by safe_key; those rare keys require a row scan,
    never treating an encoded live session as an orphan.
    """
    from app.db.base import get_db

    with get_db() as conn:
        cursor = conn.cursor()
        query = ("SELECT s.id, s.sdk_session_id, s.is_active, "
                 "EXISTS (SELECT 1 FROM turns t WHERE t.session_id=s.id AND t.ended_at IS NULL) AS open_turn "
                 "FROM sessions s")
        if not key.startswith("key~"):
            cursor.execute(query + " WHERE s.id=%s OR s.sdk_session_id=%s", (key, key))
        else:
            cursor.execute(query)
        return [dict(row) for row in cursor.fetchall()
                if key in {safe_key(row["id"]), safe_key(row["sdk_session_id"])}]


def _idle(key):
    rows = _session_rows(key)
    if any(row["is_active"] not in (False, 0) or row["open_turn"] for row in rows):
        return None
    return QUIET_SECONDS if rows else ORPHAN_SECONDS


def _process_references(deadline):
    if os.name == "posix" and Path("/proc/self/stat").exists():
        from app.core.scratch_reaper import _references
        return _references(Path("/proc"), deadline, strict=True)
    # Missing/access-denied process data is not an empty process table.
    import psutil

    refs = set()
    for process in psutil.process_iter():
        if time.monotonic() >= deadline:
            return None
        try:
            # PID 0 is the kernel idle pseudo-process, not a userspace owner.
            if process.pid == 0:
                continue
            refs.add(process.cwd())
            refs.update(item.path for item in process.open_files())
            refs.update(process.cmdline())
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
        except psutil.AccessDenied:
            return None
    return refs


def _in_use(path, refs):
    if refs is None:
        return True
    target = os.path.normcase(str(path))
    # Also match --option=/path and paths embedded in shell command strings.
    return any(target in os.path.normcase(ref) for ref in refs)


def _walk(path, cutoff, device):
    """Postorder, bounded-memory walk; never descend into links/mounts."""
    info = path.lstat()
    if info.st_dev != device or info.st_mtime > cutoff:
        raise ValueError("recent change or mount")
    if getattr(info, "st_file_attributes", 0) & 0x400:
        raise ValueError("Windows reparse point")
    if stat.S_ISDIR(info.st_mode):
        with os.scandir(path) as entries:
            for entry in entries:
                yield from _walk(Path(entry.path), cutoff, device)
    elif not (stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)):
        raise ValueError("special file")
    yield path, info


def _identity(info):
    return [info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode)]


def _remove_entry(root, path, info, root_identity):
    """Anchor POSIX unlinks to directory descriptors, never a raced symlink."""
    if os.name == "posix" and hasattr(os, "O_NOFOLLOW"):
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        descriptor = os.open(root, flags)
        try:
            if _identity(os.fstat(descriptor)) != root_identity:
                raise ValueError("state root changed")
            for part in path.parent.relative_to(root).parts:
                child = os.open(part, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            owner = os.fstat(descriptor)
            if not owner.st_mode & stat.S_IWUSR:
                if owner.st_uid != os.getuid():
                    raise ValueError("read-only foreign directory")
                os.fchmod(descriptor, owner.st_mode | stat.S_IWUSR)
            current = os.stat(path.name, dir_fd=descriptor, follow_symlinks=False)
            if _identity(current) != _identity(info):
                raise ValueError("entry changed")
            if stat.S_ISDIR(info.st_mode):
                os.rmdir(path.name, dir_fd=descriptor)
            else:
                os.unlink(path.name, dir_fd=descriptor)
        finally:
            os.close(descriptor)
        return
    # Windows: individual leaf operations only. All ancestors and reparse
    # attributes were checked by the caller; never recurse via rmtree.
    parent = path.parent
    owner = parent.lstat()
    if not owner.st_mode & stat.S_IWUSR:
        os.chmod(parent, owner.st_mode | stat.S_IWUSR)
    if stat.S_ISDIR(info.st_mode):
        path.rmdir()
    else:
        path.unlink()


@contextmanager
def _manifest():
    with tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as stream:
        yield stream


class ScratchSweep:
    """One maintenance task owns the cursor; no concurrent destructive sweeps."""
    def __init__(self, root):
        self.root = Path(os.path.abspath(root))
        self.candidates = None
        self.job = None
        self.removed_bytes = 0
        self.slice_files = 0
        self.slice_bytes = 0

    def close_job(self):
        if self.job:
            self.job["walk"].close()
            self.job["stack"].close()
            self.job = None

    def close(self):
        self.close_job()
        if self.candidates:
            self.candidates.close()
            self.candidates = None

    def _candidates(self):
        if not _real_directory(self.root):
            return
        with os.scandir(self.root) as projects:
            for project in projects:
                directory = Path(project.path)
                if not _real_directory(directory):
                    continue
                scratch = directory / "scratch"
                try:
                    if not _real_directory(scratch):
                        continue
                    with os.scandir(scratch) as sessions:
                        for session in sessions:
                            if KEY.fullmatch(session.name):
                                yield Path(session.path)
                except OSError:
                    continue

    def _start(self, path):
        if not _contained(self.root, path):
            return
        quiet = _idle(path.name)
        if quiet is None:
            return
        info = path.lstat()
        stack = ExitStack()
        self.job = {"path": path, "identity": _identity(info), "stack": stack,
                    "walk": _walk(path, time.time() - quiet, info.st_dev),
                    "manifest": stack.enter_context(_manifest()),
                    "deleting": False, "validated": False, "bytes": 0,
                    "cutoff": time.time() - quiet, "root_modified": info.st_mtime_ns,
                    "own_changes": {}, "state_identity": _identity(self.root.lstat())}

    def _guard(self, job, deadline):
        path = job["path"]
        if not _contained(self.root, path) or _identity(path.lstat()) != job["identity"]:
            return False
        if _idle(path.name) is None or _in_use(path, _process_references(deadline)):
            return False
        return path.lstat().st_mtime_ns == job["root_modified"]

    def step(self):
        deadline = time.monotonic() + PASS_SECONDS
        removed = 0
        scanned = 0
        self.slice_files = self.slice_bytes = 0
        if self.candidates is None:
            self.candidates = self._candidates()
        while time.monotonic() < deadline:
            try:
                if self.job is None:
                    self._start(next(self.candidates))
                    if self.job is None:
                        continue
                job = self.job
                if not job["deleting"]:
                    try:
                        path, info = next(job["walk"])
                    except StopIteration:
                        job["manifest"].seek(0)
                        job["deleting"] = True
                        continue
                    scanned += 1
                    job["bytes"] += info.st_size if stat.S_ISREG(info.st_mode) else 0
                    job["manifest"].write(json.dumps([str(path.relative_to(job["path"])),
                                                       _identity(info), info.st_mtime_ns, info.st_size]) + "\n")
                    if scanned >= MAX_SCAN_ENTRIES:
                        break
                    continue
                if not job["validated"]:
                    # A resumable walk may span minutes. Revalidate its complete
                    # manifest before deleting anything, not just the last leaf.
                    line = job["manifest"].readline()
                    if not line:
                        job["manifest"].seek(0)
                        job["validated"] = True
                        continue
                    relative, identity, modified, size = json.loads(line)
                    current = (job["path"] / relative).lstat()
                    if (_identity(current) != identity or current.st_mtime_ns != modified
                            or (not stat.S_ISDIR(current.st_mode) and current.st_size != size)):
                        raise ValueError("tree changed during scan")
                    scanned += 1
                    if scanned >= MAX_SCAN_ENTRIES:
                        break
                    continue
                # Recheck the complete process table and DB at every deletion
                # tranche (and DB again before each unlink). No cached settle flag.
                if not self._guard(job, deadline):
                    self.close_job()
                    continue
                tranche = min(deadline, time.monotonic() + 0.1)
                while time.monotonic() < tranche:
                    line = job["manifest"].readline()
                    if not line:
                        removed += 1
                        self.removed_bytes += job["bytes"]
                        self.close_job()
                        break
                    relative, identity, modified, size = json.loads(line)
                    path = job["path"] / relative
                    # Check ALL ancestors: a replaced nested directory must not
                    # redirect an unlink through a symlink or Windows junction.
                    if not _contained(self.root, job["path"]) or any(
                            not _real_directory(p) for p in path.parents
                            if p != self.root and self.root in p.parents):
                        raise ValueError("path changed")
                    info = path.lstat()
                    if _identity(info) != identity or (not stat.S_ISDIR(info.st_mode)
                            and (info.st_mtime_ns != modified or info.st_size != size)):
                        raise ValueError("tree changed")
                    if _idle(job["path"].name) is None:
                        raise ValueError("session resumed")
                    # Read-only generated folders are common. Only chmod real,
                    # owned directories; never follow a link to make it writable.
                    parent = path.parent
                    for ancestor in (parent, *parent.parents):
                        if ancestor != job["path"] and job["path"] not in ancestor.parents:
                            break
                        current = ancestor.lstat()
                        if (current.st_mtime > job["cutoff"]
                                and job["own_changes"].get(str(ancestor)) != current.st_mtime_ns):
                            raise ValueError("directory changed since scan")
                    _remove_entry(self.root, path, info, job["state_identity"])
                    job["own_changes"].pop(str(path), None)
                    job["own_changes"][str(parent)] = parent.lstat().st_mtime_ns
                    if parent == job["path"]:
                        job["root_modified"] = job["own_changes"][str(parent)]
                    self.slice_files += 1
                    self.slice_bytes += size if stat.S_ISREG(info.st_mode) else 0
                    if path == job["path"]:
                        removed += 1
                        self.removed_bytes += job["bytes"]
                        self.close_job()
                        break
                    # A single large file cannot be split safely. Allow that one
                    # unlink, then stop; all other work waits for the next slice.
                    if (self.slice_files >= MAX_SLICE_FILES
                            or self.slice_bytes >= MAX_SLICE_BYTES):
                        return removed
            except StopIteration:
                self.candidates.close()
                self.candidates = None
                break
            except (OSError, ValueError, RecursionError):
                # A bad folder never blocks its siblings, nor retries forever
                # within this pass. The next rotation starts with fresh evidence.
                log.debug("Session scratch retained: safety unavailable or changed", exc_info=True)
                self.close_job()
        if removed:
            log.info("Session scratch: removed=%d reclaimed_bytes=%d", removed, self.removed_bytes)
        return removed


class StorageUsage:
    """Incremental logical-byte telemetry; never follows links or reparse points."""
    def __init__(self, projects):
        self.projects = Path(projects)
        self.walk = None
        self.totals = {"scratch": 0, "checkouts": 0}
        self.next_scan = 0.0
        self.incomplete = False

    def _tree(self, category, path):
        try:
            info = path.lstat()
            if _linked(info):
                return
            if stat.S_ISREG(info.st_mode):
                yield category, info.st_size
            elif stat.S_ISDIR(info.st_mode):
                with os.scandir(path) as entries:
                    for entry in entries:
                        yield from self._tree(category, Path(entry.path))
                yield category, 0
        except (OSError, RecursionError):
            self.incomplete = True

    def _scan(self):
        from app.core.worktree_reclaim import _local_sources

        if _real_directory(self.projects):
            with os.scandir(self.projects) as projects:
                for project in projects:
                    path = Path(project.path)
                    if _real_directory(path):
                        yield from self._tree("scratch", path / "scratch")
        repos = {source["path"] for source in _local_sources()}
        for repo in repos:
            for scope in (".fluid", ".claude"):
                scope_path = Path(repo) / scope
                if scope_path.exists() and _real_directory(scope_path):
                    yield from self._tree("checkouts", scope_path / "worktrees")

    def close(self):
        if self.walk:
            self.walk.close()
            self.walk = None

    def step(self):
        if self.walk is None:
            if time.monotonic() < self.next_scan:
                return None
            self.totals = {"scratch": 0, "checkouts": 0}
            self.incomplete = False
            self.walk = self._scan()
        deadline = time.monotonic() + 0.25
        try:
            for _ in range(MAX_SCAN_ENTRIES):
                if time.monotonic() >= deadline:
                    break
                category, size = next(self.walk)
                self.totals[category] += size
        except StopIteration:
            self.close()
            self.next_scan = time.monotonic() + 1800
        except Exception:
            self.incomplete = True
            self.close()
            self.next_scan = time.monotonic() + 1800
            log.debug("Agent storage measurement incomplete", exc_info=True)
        log.info("Agent storage logical_bytes: scratch=%d checkouts=%d partial=%s",
                 self.totals["scratch"], self.totals["checkouts"], bool(self.walk) or self.incomplete)
        return dict(self.totals)


async def maintain_session_scratch():
    from app.core.config import settings

    sweeper = ScratchSweep(settings.get_fluid_projects_dir)
    usage = StorageUsage(settings.get_fluid_projects_dir)
    warned_at = float("-inf")
    await asyncio.sleep(60)
    try:
        while True:
            work = asyncio.create_task(asyncio.to_thread(sweeper.step))
            try:
                await asyncio.shield(work)
            except asyncio.CancelledError:
                await work  # finish the bounded slice before closing its handles
                raise
            except Exception:
                sweeper.close_job()
                log.warning("Session scratch: pass failed; files retained", exc_info=True)
            measurement = asyncio.create_task(asyncio.to_thread(usage.step))
            try:
                totals = await asyncio.shield(measurement)
            except asyncio.CancelledError:
                await measurement
                raise
            if totals and sum(totals.values()) >= 100 * 1024**3 and time.monotonic() - warned_at >= 86400:
                from app.services.notification_service import emit_notification
                await emit_notification("admin", severity="warning", category="agent_storage",
                                        title="Agent files use over 100 GiB",
                                        message="Session scratch and task checkouts use over 100 GiB. "
                                                "Cleanup retains files when safety cannot be established. "
                                                "Check the worker storage logs; this is not a size limit.")
                warned_at = time.monotonic()
            await asyncio.sleep(SLICE_PAUSE_SECONDS if sweeper.candidates else INTERVAL_SECONDS)
    finally:
        sweeper.close()
        usage.close()
