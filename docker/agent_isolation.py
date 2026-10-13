"""Opt-in agent account split for the Docker worker (#1957). Off by default.

``AGENT_ISOLATION=on`` in the container settings keeps the install's own
secrets out of reach of everything an agent starts. One container then has
two accounts:

* the service account (``shuffle``, uid/gid ``AGENT_ISOLATION_SERVICE_UID`` /
  ``AGENT_ISOLATION_SERVICE_GID``, default 64000) owns the database descriptor
  and mode record, the credential and managed-connection keys, the preview
  key, PostgreSQL's files and the installed application updates. PostgreSQL
  runs as it;
* the agent account is PUID/PGID (``appuser``), exactly as today: Bash,
  Monitor, hook, MCP and delegated ``claude`` children and owner terminals.

The supervisor and the app run as PUID with the service account kept only as
their *saved* uid and gid (``drop``). Every program they execute loses it,
because execve sets the saved ids to the effective ones, and a PUID process
cannot trace, or read the /proc environment of, a process whose saved uid
differs. The app opens the protected files with a per-thread file-system uid
(``app.core.private_storage.service_files``). No sudo, setuid helper or
group membership is involved; the agent account holds none of the service
account's ids.

Root migrates ownership once per start, before dropping privileges
(``bootstrap``): it writes ``DATA_DIR/agent-isolation.json`` first, then
changes the owner of entries PUID owns, and never initializes, regenerates or
deletes anything. Turning the setting off hands everything back to PUID on the
next start. Both directions are idempotent and resumable.

Standard library only: root runs this before any application import.
"""
from __future__ import annotations

import contextlib
import grp
import json
import os
from pathlib import Path
import pwd
import stat
import subprocess
import threading

SETTING = "AGENT_ISOLATION"
SERVICE_NAME = "shuffle"
DEFAULT_SERVICE_ID = 64000
RECORD_NAME = "agent-isolation.json"
UPDATES = "application-updates"
# DATA_DIR entries only the service account may read. Everything else in
# DATA_DIR stays PUID's: the app writes it, and so may the agent, as today.
PROTECTED = ("managed-postgres.json", "database-mode.json", ".credential-keys",
             ".managed-connection", "preview-access.key", "postgres", UPDATES)
# Image code the app (which holds the service account as its saved uid) loads.
# The image ships these owned by appuser; an agent able to edit them could run
# code inside the app. Root owns them while the mode is on.
CODE_TREES = ("app", "scripts")
CODE_FILES = (".env.example", "THIRD_PARTY_NOTICES.md")
# Passed to the app (SHUFFLE_*) so it routes agent children through the runner.
APP_FLAG = "SHUFFLE_AGENT_ISOLATION"
APP_ROOTS = "SHUFFLE_AGENT_ROOTS"
EXTRA_DIRS = "AGENT_ISOLATION_EXTRA_DIRS"

_TRUE = {"1", "on", "true", "yes"}
_FALSE = {"", "0", "off", "false", "no"}


class IsolationError(Exception):
    """Secret-free operator diagnostic."""


class Identity:
    """The ids of one split: agent (PUID/PGID) and service account."""

    def __init__(self, puid, pgid, suid, sgid):
        self.puid, self.pgid, self.suid, self.sgid = int(puid), int(pgid), int(suid), int(sgid)

    def __eq__(self, other):
        return isinstance(other, Identity) and vars(self) == vars(other)

    def __repr__(self):
        return f"Identity(puid={self.puid}, pgid={self.pgid}, suid={self.suid}, sgid={self.sgid})"


# Set by ``drop`` in the supervisor once the split is in effect.
ACTIVE = None


def setting(environ=None):
    """"on", "off" or "invalid". Read from the container environment only:
    the agent can edit files the app reads settings from, never this."""
    raw = (os.environ if environ is None else environ).get(SETTING, "").strip().lower()
    return "on" if raw in _TRUE else "off" if raw in _FALSE else "invalid"


def _id(environ, name):
    raw = environ.get(name, "").strip()
    if not raw:
        return DEFAULT_SERVICE_ID
    if not raw.isdigit() or not 0 < int(raw) < 2**31 - 1:
        raise IsolationError(f"{name} must be a positive numeric id")
    return int(raw)


def identity(puid, pgid, environ=None):
    """The split for this PUID/PGID; the service ids must be distinct from it."""
    environ = os.environ if environ is None else environ
    suid, sgid = _id(environ, "AGENT_ISOLATION_SERVICE_UID"), _id(environ, "AGENT_ISOLATION_SERVICE_GID")
    if not puid or not pgid:
        raise IsolationError("PUID/PGID must identify a non-root account")
    if suid == puid or sgid == pgid:
        raise IsolationError("the agent isolation service uid/gid must differ from PUID/PGID")
    return Identity(puid, pgid, suid, sgid)


# --- Ownership changes ---------------------------------------------------


class Ops:
    """File-system operations the migration performs; tests substitute them."""

    def lstat(self, path):
        return os.lstat(path)

    def lchown(self, path, uid, gid):
        os.chown(path, uid, gid, follow_symlinks=False)

    def chmod(self, path, mode):
        # Only ever called on a path just checked to be a directory or regular
        # file (never a symlink); Linux has no lchmod.
        os.chmod(path, mode)

    def listdir(self, path):
        return os.listdir(path)


def _entries(top, ops):
    """``top`` and everything below it, depth first, never following symlinks."""
    pending = [Path(top)]
    while pending:
        path = pending.pop()
        try:
            info = ops.lstat(path)
        except FileNotFoundError:
            continue
        yield path, info
        if stat.S_ISDIR(info.st_mode):
            pending.extend(path / name for name in sorted(ops.listdir(path), reverse=True))


def _protected_tops(data_dir, ops):
    """The protected entries that exist. A symlink there is refused: the
    migration must not follow one out of DATA_DIR."""
    tops = []
    for name in PROTECTED:
        path = Path(data_dir) / name
        try:
            info = ops.lstat(path)
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            raise IsolationError(f"DATA_DIR/{name} is a symlink; agent isolation refuses to follow it")
        tops.append(path)
    return tops


def _relative(data_dir, path):
    return Path(path).relative_to(data_dir).as_posix()


def _update_dirs(data_dir, ops):
    """application-updates, versions and each version directory: the app,
    running as PUID, must be able to pass through them to the code it runs."""
    root = Path(data_dir) / UPDATES
    found = []
    for path in (root, root / "versions"):
        try:
            if stat.S_ISDIR(ops.lstat(path).st_mode):
                found.append(path)
        except FileNotFoundError:
            return found
    if len(found) == 2:
        for name in sorted(ops.listdir(root / "versions")):
            path = root / "versions" / name
            if stat.S_ISDIR(ops.lstat(path).st_mode):
                found.append(path)
    return found


def read_record(data_dir, ops=None):
    """The migration record, or None. Only a root-owned regular file counts:
    DATA_DIR is the agent's while the mode is off, so anything else may have
    been planted. Ids in it are never trusted for anything but restoring
    group ownership inside the protected entries and DATA_DIR's own mode."""
    ops = ops or Ops()
    path = Path(data_dir) / RECORD_NAME
    try:
        info = ops.lstat(path)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1:
        return None
    try:
        record = json.loads(path.read_text())
    except (OSError, ValueError, UnicodeError):
        return None
    return record if isinstance(record, dict) and record.get("version") == 1 else None


def _write_record(data_dir, record, ops):
    path = Path(data_dir) / RECORD_NAME
    try:
        info = ops.lstat(path)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0:
            path.unlink()  # planted: replace, never write through it
    except FileNotFoundError:
        pass
    temporary = path.with_name(RECORD_NAME + ".new")
    temporary.unlink(missing_ok=True)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, "w") as stream:
        json.dump(record, stream, indent=1, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def plan_enable(data_dir, ident, ops=None):
    """What ``enable`` changes, without changing it: a list of
    (path, uid, gid, previous gid) and the protected entries found."""
    ops = ops or Ops()
    changes = []
    for top in _protected_tops(data_dir, ops):
        for path, info in _entries(top, ops):
            if info.st_uid == ident.puid:
                changes.append((path, ident.suid, ident.sgid, info.st_gid))
    return changes


def enable(data_dir, ident, ops=None, log=print):
    """Give the protected entries to the service account. Records first;
    changes only what PUID owns; resumable. Returns the record."""
    ops = ops or Ops()
    data_dir = Path(data_dir)
    root_info = ops.lstat(data_dir)
    if not stat.S_ISDIR(root_info.st_mode):
        raise IsolationError("DATA_DIR must be a directory")
    changes = plan_enable(data_dir, ident, ops)  # refuses symlinks before any change
    previous = read_record(data_dir, ops) or {}
    if previous.get("state") == "enabled" and changes:
        # Once migrated, the service account creates these itself. A PUID
        # entry appearing later may have been planted by an agent process
        # (a key it knows): never adopt it. The app refuses it by owner.
        for path, *_ in changes:
            log(f"agent isolation: {path} belongs to PUID, not the service account; not adopting it."
                " Remove it if nothing of yours created it, or turn the mode off and on again to migrate it.")
        changes = []
    groups = previous.get("groups") if isinstance(previous.get("groups"), dict) else {}
    for path, _uid, _gid, old_gid in changes:
        if old_gid != ident.pgid:
            groups[_relative(data_dir, path)] = old_gid
    data_root = previous.get("data_root")
    if not isinstance(data_root, dict) or root_info.st_uid != ident.suid:
        data_root = {"uid": root_info.st_uid, "gid": root_info.st_gid,
                     "mode": stat.S_IMODE(root_info.st_mode)}
    record = {"version": 1, "state": "enabling", "puid": ident.puid, "pgid": ident.pgid,
              "service_uid": ident.suid, "service_gid": ident.sgid,
              "data_root": data_root, "groups": groups}
    _write_record(data_dir, record, ops)
    for path, uid, gid, _old in changes:
        ops.lchown(path, uid, gid)
    for path in _update_dirs(data_dir, ops):
        info = ops.lstat(path)
        if info.st_uid == ident.suid and not info.st_mode & stat.S_IXOTH:
            ops.chmod(path, stat.S_IMODE(info.st_mode) | stat.S_IXOTH)
    # DATA_DIR itself: the service account must create and replace its files
    # there (the descriptor, the preview key), the app and agent must keep
    # writing theirs through the group, and the sticky bit stops PUID from
    # renaming or deleting what the service account owns.
    if root_info.st_uid != ident.suid or root_info.st_gid != ident.pgid:
        ops.lchown(data_dir, ident.suid, ident.pgid)
    mode = stat.S_IMODE(root_info.st_mode) | stat.S_ISVTX | stat.S_IRWXU | stat.S_IRWXG
    if stat.S_IMODE(root_info.st_mode) != mode:
        ops.chmod(data_dir, mode)
    record["state"] = "enabled"
    _write_record(data_dir, record, ops)
    if changes:
        log(f"agent isolation: gave {len(changes)} protected DATA_DIR entries to the service account")
    return record


def needs_rollback(data_dir, ident, ops=None):
    """True when a previous start enabled the mode. Mode-off starts call this
    and nothing else: a few lstat calls, no change."""
    ops = ops or Ops()
    data_dir = Path(data_dir)
    try:
        if ops.lstat(data_dir / RECORD_NAME):
            return True
    except FileNotFoundError:
        pass
    for path in (data_dir, *(data_dir / name for name in PROTECTED)):
        try:
            if ops.lstat(path).st_uid == ident.suid:
                return True
        except FileNotFoundError:
            continue
    return False


def _safe_gid(value, ident):
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 \
        and value not in (ident.sgid,) else ident.pgid


def disable(data_dir, ident, ops=None, log=print):
    """Hand everything the service account owns in the protected entries and
    DATA_DIR back to PUID. Works without the record (it only restores group
    ids and DATA_DIR's previous mode). Returns the number of entries changed."""
    ops = ops or Ops()
    data_dir = Path(data_dir)
    record = read_record(data_dir, ops) or {}
    groups = record.get("groups") if isinstance(record.get("groups"), dict) else {}
    changed = 0
    for top in _protected_tops(data_dir, ops):
        for path, info in _entries(top, ops):
            if info.st_uid != ident.suid:
                continue
            # Our own relative path is the lookup key; the record's keys never
            # become paths.
            gid = _safe_gid(groups.get(_relative(data_dir, path)), ident)
            ops.lchown(path, ident.puid, gid)
            changed += 1
    for path in _update_dirs(data_dir, ops):
        info = ops.lstat(path)
        if info.st_uid == ident.puid and info.st_mode & stat.S_IXOTH:
            ops.chmod(path, stat.S_IMODE(info.st_mode) & ~stat.S_IXOTH)
    info = ops.lstat(data_dir)
    if info.st_uid == ident.suid:
        previous = record.get("data_root") if isinstance(record.get("data_root"), dict) else {}
        uid = previous.get("uid")
        uid = uid if isinstance(uid, int) and uid > 0 and uid != ident.suid else ident.puid
        gid = _safe_gid(previous.get("gid"), ident)
        mode = previous.get("mode")
        mode = (mode & 0o7777) if isinstance(mode, int) else stat.S_IMODE(info.st_mode) & ~stat.S_ISVTX
        ops.lchown(data_dir, uid, gid)
        ops.chmod(data_dir, mode)
        changed += 1
    (data_dir / RECORD_NAME).unlink(missing_ok=True)
    log(f"agent isolation is off: handed {changed} DATA_DIR entries back to PUID")
    return changed


# --- Image code ----------------------------------------------------------


def _code_entries(app_dir, ops, strict=True):
    app_dir = Path(app_dir)
    device = ops.lstat(app_dir).st_dev
    yield app_dir, ops.lstat(app_dir)
    for name in CODE_FILES + CODE_TREES:
        path = app_dir / name
        try:
            info = ops.lstat(path)
        except FileNotFoundError:
            continue
        if info.st_dev != device:
            # A bind-mounted source tree belongs to the host; never chown it.
            if not strict:
                continue
            raise IsolationError(f"/app/{name} is a mount; agent isolation needs the image's own code")
        if name in CODE_TREES:
            yield from _entries(path, ops)
        else:
            yield path, info


def protect_code(app_dir, ident, ops=None, log=print):
    """Root owns the image code the app loads while the mode is on."""
    ops = ops or Ops()
    entries = list(_code_entries(app_dir, ops))
    for path, info in entries:
        if info.st_uid == ident.puid:
            ops.lchown(path, 0, 0)
    known = {*CODE_FILES, *CODE_TREES}
    for name in sorted(ops.listdir(app_dir)):
        try:
            info = ops.lstat(Path(app_dir) / name)
        except FileNotFoundError:
            continue
        if name not in known and (info.st_uid == ident.puid or info.st_mode & stat.S_IWOTH):
            log(f"agent isolation: /app/{name} stays writable by PUID; keep code out of it")


def release_code(app_dir, ident, ops=None):
    """Give the image code back to appuser, as the image and entrypoint do."""
    ops = ops or Ops()
    for path, info in list(_code_entries(app_dir, ops, strict=False)):
        if info.st_uid == 0:
            ops.lchown(path, ident.puid, ident.pgid)


# --- The service account ---------------------------------------------------


def ensure_account(ident, run=subprocess.run):
    """The image's ``shuffle`` account, at the configured ids, in no group the
    agent is in and with the agent in none of its groups."""
    try:
        account = pwd.getpwnam(SERVICE_NAME)
    except KeyError:
        raise IsolationError("this image has no agent isolation service account") from None
    try:
        group_id = grp.getgrnam(SERVICE_NAME).gr_gid
    except KeyError:
        raise IsolationError("this image has no agent isolation service group") from None
    if group_id != ident.sgid:
        run(["groupmod", "-o", "-g", str(ident.sgid), SERVICE_NAME], check=True)
    if account.pw_uid != ident.suid or account.pw_gid != ident.sgid:
        run(["usermod", "-o", "-u", str(ident.suid), "-g", str(ident.sgid), SERVICE_NAME], check=True)
    agent = pwd.getpwuid(ident.puid).pw_name
    for gid in {ident.sgid, ident.pgid}:
        try:
            members = set(grp.getgrgid(gid).gr_mem)
        except KeyError:
            continue
        if (gid == ident.sgid and agent in members) or (gid == ident.pgid and SERVICE_NAME in members):
            raise IsolationError("the agent and service accounts must not share a group")


def bootstrap(data_dir, ident, *, app_dir="/app", ops=None, log=print, run=subprocess.run):
    """Root, before the drop. Returns the identity to split with, or None.

    Off: roll back a previous enable if there is one, else change nothing.
    On: account, code, data. Any failure leaves the worker running as it does
    with the mode off (after undoing a partial change), with a loud log.
    """
    ops = ops or Ops()
    if ident is None:
        return None
    if setting() != "on":
        if setting() == "invalid":
            log(f"agent isolation: ignoring {SETTING}={os.environ.get(SETTING)!r}; use on or off")
        if needs_rollback(data_dir, ident, ops):
            disable(data_dir, ident, ops, log)
            release_code(app_dir, ident, ops)
        return None
    try:
        ensure_account(ident, run)
        protect_code(app_dir, ident, ops, log)
        enable(data_dir, ident, ops, log)
    except (IsolationError, OSError, subprocess.CalledProcessError) as error:
        reason = str(error) if isinstance(error, IsolationError) else type(error).__name__
        log(f"agent isolation could NOT be enabled ({reason}); agents run unisolated, as with "
            f"{SETTING}=off. Fix the cause and restart.")
        try:
            disable(data_dir, ident, ops, log)
            release_code(app_dir, ident, ops)
        except Exception as undo:  # noqa: BLE001 - report both, keep the first
            log(f"agent isolation: rollback after the failure also failed ({type(undo).__name__})")
            raise IsolationError("agent isolation partially applied; restart with it on or off") from None
        return None
    log("agent isolation is on: agent processes run as PUID without the install's keys or database")
    return ident


# --- Credentials ------------------------------------------------------------


def drop(ident):
    """Root to (PUID, service, service): agent-owned real and effective ids,
    the service account kept as the saved uid/gid and nothing else."""
    global ACTIVE
    os.setgroups([])
    os.setresgid(ident.pgid, ident.sgid, ident.sgid)
    os.setresuid(ident.puid, ident.suid, ident.suid)
    if (os.getresuid() != (ident.puid, ident.suid, ident.suid)
            or os.getresgid() != (ident.pgid, ident.sgid, ident.sgid) or os.getgroups()):
        raise IsolationError("agent isolation could not set the supervisor's ids")
    ACTIVE = ident


def service_spawn():
    """Popen arguments for a child that runs as the service account
    (PostgreSQL and its tools): real, effective and saved ids all service."""
    return {} if ACTIVE is None else {"user": ACTIVE.suid, "group": ACTIVE.sgid}


def agent_spawn():
    """Popen arguments for a child that runs as the agent account."""
    return {} if ACTIVE is None else {"user": ACTIVE.puid, "group": ACTIVE.pgid}


def own_record(path):
    """The migration record, which a fresh install's DATA_DIR already holds."""
    return ACTIVE is not None and Path(path).name == RECORD_NAME


def service_env():
    """What PostgreSQL children get in place of the agent's HOME (libpq reads
    ~/.pgpass and ~/.postgresql)."""
    return {} if ACTIVE is None else {"HOME": "/nonexistent"}


_fs = threading.local()


def _libc():
    import ctypes
    import ctypes.util
    return ctypes.CDLL(ctypes.util.find_library("c") or None, use_errno=True)


@contextlib.contextmanager
def agent_files():
    """Create files as the agent (the Chromium log and launch scripts the
    agent-owned Chromium reads). The supervisor's file-system ids, this thread
    only; a no-op unless the split is active."""
    if ACTIVE is None or getattr(_fs, "agent", False):
        yield
        return
    libc = _libc()
    libc.setfsgid(ACTIVE.pgid)
    libc.setfsuid(ACTIVE.puid)
    _fs.agent = True
    try:
        if libc.setfsuid(-1) != ACTIVE.puid:
            raise IsolationError("could not switch to the agent's file ids")
        yield
    finally:
        _fs.agent = False
        libc.setfsuid(ACTIVE.suid)
        libc.setfsgid(ACTIVE.sgid)


def ensure_traversable(root, log=print):
    """The supervisor creates update directories 0700 as the service account;
    the app, running as PUID, runs code from inside them. Grant search (never
    listing) on the directories on that path. The code is public; the state
    files beside it stay 0600."""
    if ACTIVE is None:
        return
    try:
        for path in _update_dirs(Path(root).parent, Ops()):
            info = os.lstat(path)
            if info.st_uid == os.geteuid() and not info.st_mode & stat.S_IXOTH:
                os.chmod(path, stat.S_IMODE(info.st_mode) | stat.S_IXOTH)
    except OSError as error:
        log(f"agent isolation: could not open the update directories to the app ({type(error).__name__})")


def trusted_path(value, stat_fn=os.stat):
    """PATH without directories root does not own outright: the supervisor
    keeps the service account and must not run a program the agent placed
    (~/.local/bin is first on the image's PATH)."""
    kept = []
    for entry in (value or "").split(os.pathsep):
        if not entry.startswith("/") or entry in kept:
            continue
        try:
            candidates = [Path(entry), *Path(entry).parents]
            if all(stat_fn(p).st_uid == 0 and not stat_fn(p).st_mode & 0o022 for p in candidates):
                kept.append(entry)
        except OSError:
            continue
    return os.pathsep.join(kept)


# The app launcher: started by the supervisor as (PUID, service, service), it
# makes the effective ids the agent's own (keeping the service account saved)
# and runs the app in this same process; an exec here would lose the saved ids.
APP_SHIM = (
    "import os, runpy, sys\n"
    "r, e, s = os.getresuid(); rg, eg, sg = os.getresgid()\n"
    "if r == e or rg == eg or os.getgroups():\n"
    "    sys.exit('agent isolation: the app launcher got unexpected ids')\n"
    "os.setresgid(rg, rg, eg); os.setresuid(r, r, e)\n"
    "if os.getresuid() != (r, r, e) or os.getresgid() != (rg, rg, eg):\n"
    "    sys.exit('agent isolation: the app launcher could not set its ids')\n"
    "sys.argv[:] = ['-m']\n"
    "sys.path[0] = os.getcwd()\n"
    "runpy.run_module('app.main', run_name='__main__', alter_sys=True)\n"
)


def app_command(command):
    """``[python, -m, app.main]`` as the supervisor would run it, through the
    launcher when the split is active (unchanged otherwise)."""
    if ACTIVE is None:
        return command
    if command[1:] != ["-m", "app.main"]:
        raise IsolationError("unexpected application command")
    return [command[0], "-s", "-c", APP_SHIM]


def agent_roots(settings, environ=None):
    """Directories an agent process may start in: the workspace, the agent's
    home, DATA_DIR (its protected entries refuse PUID by mode), /tmp and
    AGENT_ISOLATION_EXTRA_DIRS (colon separated)."""
    environ = os.environ if environ is None else environ
    roots = [str(settings.effective_workspace_dir), pwd.getpwuid(ACTIVE.puid).pw_dir if ACTIVE else
             os.path.expanduser("~"), str(settings.effective_data_dir), "/tmp"]
    roots += [p for p in environ.get(EXTRA_DIRS, "").split(":") if p.startswith("/")]
    return ":".join(dict.fromkeys(os.path.abspath(p) for p in roots))


def app_environment(settings, original_path, environ=None):
    """What the app's environment adds: the flag, the approved roots and the
    image's full PATH (the supervisor itself runs with ``trusted_path``)."""
    if ACTIVE is None:
        return {}
    return {APP_FLAG: "on", APP_ROOTS: agent_roots(settings, environ), "PATH": original_path}
