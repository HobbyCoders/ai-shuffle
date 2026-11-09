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


def _uninspectable_is_unrelated(base, proc, protected_prefixes=None, *, strict=False):
    status = dict(line.split(":", 1) for line in (base / "status").read_text().splitlines() if ":" in line)
    uids = {int(uid) for uid in status["Uid"].split()}
    if strict and os.getuid() in uids:
        # Ancestry, not argv spelling, establishes a launcher's role. An opaque
        # ancestor can still hold external scratch: settled paths have no lease.
        return _is_ancestor(base, proc, strict=True)
    if 0 not in uids:
        if strict:
            return False  # Opaque apps may reference arbitrary session scratch.
        # Retaining cwd/fds then switching every UID to another user is a known gap.
        if os.getuid() not in uids:
            return True
        return _is_application_process(base, status, proc, protected_prefixes)
    # Only exempt established init ancestors, not arbitrary root daemons or
    # elevated children. Init shims predate our scratch and only reap/forward
    # signals; they cannot have inherited its cwd/fds. Trust their fixed role
    # not to open application scratch later. Unknown root processes fail closed.
    name = status["Name"].strip()
    if name not in {"tini", "docker-init"} and not (base.name == "1" and name in {"init", "systemd"}):
        return False
    return _is_ancestor(base, proc)


def _python_args(base, status):
    if not re.fullmatch(r"python(?:[0-9]+(?:\.[0-9]+)*)?", status["Name"].strip()):
        return None
    argv = (base / "cmdline").read_bytes().rstrip(b"\0").split(b"\0")
    if not argv or not re.fullmatch(r"python(?:[0-9]+(?:\.[0-9]+)*)?", Path(os.fsdecode(argv[0])).name):
        return None
    args = argv[1:]
    while args and args[0] in {b"-s", b"-I", b"-E", b"-u", b"-B"}:
        args = args[1:]
    return args


def _is_application_process(base, status, proc, protected_prefixes=None):
    # Normal startup makes app/runner processes non-dumpable. Live-owner receipts
    # protect their own scratch, but an orphaned preview app can retain a dead
    # launcher's tree. Exempting an opaque app therefore protects ALL preview
    # trees for this sweep. The supervisor only starts/reaps children and never
    # opens their scratch.
    args = _python_args(base, status)
    if args is None:
        return False
    devloop = (len(args) == 3 and args[:2] == [b"-m", b"scripts.devloop.child"]
               and Path(os.fsdecode(args[2])).is_absolute())
    if devloop or args in ([b"-m", b"app.main"], [b"-m", b"app.preview_process"]):
        if protected_prefixes is not None:
            protected_prefixes.add("ai-shuffle-preview-")
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


def _is_ancestor(base, proc, *, strict=False):
    pid = os.getpid()
    seen = {int(base.name): process_identity(int(base.name), proc)} if strict else {}
    while pid and pid not in seen and len(seen) < MAX_ENTRIES:
        seen[pid] = process_identity(pid, proc) if strict else None
        try:
            parent = dict(line.split(":", 1) for line in (proc / str(pid) / "status").read_text().splitlines() if ":" in line)
        except (FileNotFoundError, ProcessLookupError):
            return False  # incomplete ancestry cannot establish an exemption
        pid = int(parent["PPid"])
        if str(pid) == base.name:
            # Recheck every start identity, not just the target: an intermediate
            # PID may also exit and be reused while this live chain is read.
            return not strict or all(start is not None and process_identity(pid, proc) == start
                                     for pid, start in seen.items())
    return False


def _managed_postgres(base, proc, deadline):
    # Use the established saved service identity, never an environment UID alone.
    # Require a postgres-only service chain reaching our live non-init ancestry,
    # not a launcher spelling. Agent databases and unrelated daemons do not qualify.
    from app.core.private_storage import _service_ids
    ids = _service_ids()
    if ids is None or ids[0] in (0, os.getuid()):
        return False
    seen = {}
    while base.name not in seen and len(seen) < MAX_ENTRIES and time.monotonic() < deadline:
        seen[base.name] = process_identity(int(base.name), proc)
        status = dict(line.split(":", 1) for line in (base / "status").read_text().splitlines() if ":" in line)
        if (len(seen) > 1 and base.name != "1"
                and status["Name"].strip() not in {"tini", "docker-init", "init", "systemd"}
                and _is_ancestor(base, proc, strict=True)):
            return all(start is not None and process_identity(pid, proc) == start
                       for pid, start in seen.items())
        if status["Name"].strip() != "postgres" or {int(uid) for uid in status["Uid"].split()} != {ids[0]}:
            return False
        base = proc / status["PPid"].strip()
    return False


def process_label(pid, name):
    # No cwd, descriptor path or command-line payload enters the log. Names are
    # single-line, bounded executable labels, not arbitrary argv or private paths.
    name = re.sub(r"[^A-Za-z0-9_.-]", "_", name.strip())[:32] or "unknown"
    return f"pid={str(pid)[:10]} name={name}"


def _process_label(base):
    try:
        name = (base / "comm").read_text(errors="replace")
    except OSError:
        name = "unknown"
    return process_label(base.name, name)


class References(set):
    def __init__(self):
        super().__init__()
        self.protected_roots = []
        self.preview_exemptions = 0
        self.owners = {}

    def add_owned(self, value, owner):
        self.add(value)
        self.owners.setdefault(value, owner)

    def holder(self, path):
        target = os.path.normcase(str(path))
        return next((owner for ref, owner in self.owners.items()
                     if target in os.path.normcase(ref)), None)


def _private_record(path):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o022 or info.st_size > 131072):
        raise ValueError("unsafe receipt")
    return json.loads(path.read_text())


def _isolated_preview(base, proc, deadline):
    """Prove an exact preview launcher owns live, private throwaway roots.

    Private HOME is NOT a filesystem sandbox: no receipt proves the opaque app
    has no external descriptor. Exempting it accepts loss of path access to
    quiet, settled external scratch; Linux keeps a held descriptor readable.
    """
    status = dict(line.split(":", 1) for line in (base / "status").read_text().splitlines() if ":" in line)
    args = _python_args(base, status)
    child = len(args or []) == 3 and args[:2] == [b"-m", b"scripts.devloop.child"]
    if not child and args not in ([b"-m", b"app.main"], [b"-m", b"app.preview_process"]):
        return None
    owner = proc / status["PPid"].strip()
    owner_status = dict(line.split(":", 1) for line in (owner / "status").read_text().splitlines() if ":" in line)
    if any(int(uid) != os.getuid() for row in (status, owner_status) for uid in row["Uid"].split()):
        return None
    launcher = _python_args(owner, owner_status) or []
    if not (launcher[:3] in ([b"-m", b"scripts.devloop", b"up"], [b"-m", b"app.preview", b"run"])
            or launcher[:2] == [b"-m", b"app.preview_run"]):
        return None
    identity = process_identity(int(base.name), proc)
    owner_identity = process_identity(int(owner.name), proc)
    if identity is None or owner_identity is None:
        return None
    checkout = Path(os.readlink(owner / "cwd"))
    temp = Path(tempfile.gettempdir()).resolve()
    directories = [Path(os.fsdecode(args[2])).parent] if child else temp.iterdir()
    for index, directory in enumerate(directories):
        if index >= MAX_ENTRIES or time.monotonic() >= deadline:
            return None
        if directory.parent != temp or not directory.name.startswith("ai-shuffle-preview-"):
            continue
        try:
            info = directory.lstat()
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077):
                continue
            receipt = _private_record(directory / MARKER)
            if (receipt.get("version") != 1 or receipt.get("uid") != os.getuid()
                    or receipt.get("pid") != int(owner.name)
                    or receipt.get("start") != owner_identity):
                continue
            scopes = [directory]
            if (b"--workers=2" in launcher or (b"--workers" in launcher
                    and launcher[launcher.index(b"--workers") + 1:][:1] == [b"2"])):
                scopes = [directory / "worker-a", directory / "worker-b"]
            roots = [directory, checkout]
            for scope in scopes:
                data = scope / "data"
                marker = scope / ".branch-preview"
                if not stat.S_ISREG(marker.lstat().st_mode) or marker.read_text() != str(data):
                    raise ValueError("missing private data marker")
                for path in (scope, scope / "home", data, scope / "workspace"):
                    current = path.lstat()
                    if not stat.S_ISDIR(current.st_mode) or current.st_uid != os.getuid():
                        raise ValueError("unsafe private root")
                    roots.append(path)
            if child:
                activity = _private_record(Path(os.fsdecode(args[2])))
                if (activity.get("pid") != int(base.name) or activity.get("identity") != identity
                        or not 0 <= time.monotonic() - activity["updated"] <= 5):
                    continue
            # A different worktree is fine. Its live status, when available,
            # supplies the actual source checkout as another protected root.
            from scripts.devloop.__main__ import default_status_path
            status_path = default_status_path(checkout)
            for pos, arg in enumerate(launcher):
                if arg == b"--status" and pos + 1 < len(launcher):
                    status_path = checkout / os.fsdecode(launcher[pos + 1])
                elif arg.startswith(b"--status="):
                    status_path = checkout / os.fsdecode(arg.split(b"=", 1)[1])
            try:
                record = _private_record(status_path)
                if (record.get("pid") == int(owner.name) and record.get("pid_identity") == receipt["start"]
                        and record.get("app", {}).get("pid") == int(base.name)
                        and record.get("app", {}).get("pid_identity") == identity):
                    roots.append(Path(record["checkout"]))
            except (OSError, ValueError, KeyError):
                pass
            if (identity == process_identity(int(base.name), proc)
                    and owner_identity == process_identity(int(owner.name), proc)):
                return roots
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return None


def _references(proc, deadline, *, protected_prefixes=None, strict=False, on_disabled=None, on_exempt=None):
    def disabled(reason, base=None):
        if base is not None:
            reason += " " + _process_label(base)
        if on_disabled is not None:
            on_disabled(reason)
        return _disabled(reason)

    references = References()
    count = 0
    descriptor_count = 0
    # No /proc or an unreadable potentially relevant process: fail closed.
    with os.scandir(proc) as processes:
        for process in processes:
            if not process.name.isdigit():
                continue
            count += 1
            if count > MAX_ENTRIES or time.monotonic() >= deadline:
                return disabled("process scan limit reached")
            base = Path(process.path)
            owner = _process_label(base)
            try:
                links = [base / "cwd", base / "root"]
                with os.scandir(base / "fd") as descriptors:
                    for descriptor in descriptors:
                        links.append(Path(descriptor.path))
                        descriptor_count += 1
                        if descriptor_count > MAX_DESCRIPTORS or time.monotonic() >= deadline:
                            return disabled("descriptor scan limit reached", base)
                for link in links:
                    try:
                        value = os.readlink(link)
                    except (FileNotFoundError, ProcessLookupError):
                        continue
                    if value.startswith("/"):
                        references.add_owned(value.removesuffix(" (deleted)"), owner)
                references.add_owned(os.fsdecode((base / "cmdline").read_bytes()).replace("\0", " "), owner)
            except (FileNotFoundError, ProcessLookupError):
                continue  # exited during observation
            except PermissionError:
                try:
                    unrelated = (_uninspectable_is_unrelated(base, proc, protected_prefixes, strict=True)
                                 if strict else _uninspectable_is_unrelated(base, proc, protected_prefixes))
                    if unrelated and not strict:
                        continue  # Preserve non-strict UID/init proofs without cmdline access.
                    references.add_owned(os.fsdecode((base / "cmdline").read_bytes()).replace("\0", " "), owner)
                    if strict and _managed_postgres(base, proc, deadline):
                        continue
                    if strict:
                        try:
                            roots = _isolated_preview(base, proc, deadline)
                        except (OSError, ValueError, KeyError, ImportError):
                            if not unrelated:
                                raise
                            roots = None  # A proven ancestor needs no preview receipt.
                        if roots:
                            references.protected_roots.extend(roots)
                            references.update(str(path) for path in roots)
                            references.preview_exemptions += 1
                            if on_exempt is not None:
                                on_exempt(f"preview_exempt pid={base.name} name=python")
                            continue
                    if unrelated:
                        continue
                except (FileNotFoundError, ProcessLookupError):
                    if not base.exists():
                        continue
                except (OSError, ValueError, KeyError, ImportError):
                    pass
                return disabled("potentially relevant process is not inspectable", base)
    if time.monotonic() >= deadline:
        return disabled("process scan deadline reached")
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
        protected_prefixes = set()
        references = _references(proc, deadline, protected_prefixes=protected_prefixes)
        if references is None:
            return 0
        with os.scandir(root) as entries:
            for index, entry in enumerate(entries):
                if index >= MAX_ENTRIES or removed >= MAX_REMOVALS or time.monotonic() >= deadline:
                    break
                if not entry.name.startswith(PREFIXES) or not entry.is_dir(follow_symlinks=False):
                    continue
                if any(entry.name.startswith(prefix) for prefix in protected_prefixes):
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
                    if any(str(path) in ref for ref in references):
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
