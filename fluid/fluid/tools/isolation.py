"""The opt-in isolated execution profile for model-driven child processes.

Off by default. With ``[sandbox] mode = "isolated"`` every process Fluid starts
to run a command the model chose (a Bash call, a Monitor command, the code-mode
kernel) goes through a small launcher first:

    python -I -S isolation.py --rw DIR ... --ro DIR ... --net off|on -- COMMAND...

The launcher asks the kernel to confine itself, then replaces itself with
``COMMAND``. On Linux the confinement is Landlock (filesystem access rules,
and TCP bind/connect rules from ABI 4) plus a seccomp filter (no new
sockets when the network is off, and io_uring, which could open them behind
seccomp's back). Both are inherited across ``exec`` and ``fork`` and cannot be
lifted by the confined process. Any step that fails stops the command with
exit status 126 before it runs; nothing runs half-confined.

The policy is an allowlist:

* writable: the session's working directory, ``writable_roots`` and a private
  per-session home and temp directory (``HOME`` and ``TMPDIR`` point there);
* read-only (and executable): the system directories (``/usr``, ``/etc``,
  ``/proc``, ...), the Python installation and ``readable_roots``;
* everything else, including the rest of the user's home directory, cannot be
  opened. A root that would expose a credential location (``~/.ssh``, cloud
  CLI credentials, Fluid's own token files, ...) is refused, so those stay
  unreadable whatever the configuration says.

Landlock also stops a confined process from ptracing, or reading the
``/proc/<pid>/environ``, ``mem`` and ``fd`` entries of, any process outside
its own sandbox, and from ABI 6 from signalling one.

The script part is stdlib only and runs with ``-I -S`` (nothing from the
working directory, the user site or ``PYTHON*`` variables is loaded). The
importable part is what spawn sites call: :func:`policy_of` reads the policy a
session carries in ``ToolCtx.extra["isolation"]``, :meth:`IsolationPolicy.wrap`
rewrites ``(argv, cwd, env)`` or raises :class:`IsolationUnavailable`.

A backend is per platform (:func:`backend`); only Linux has one today. A
macOS Seatbelt backend would implement the same two methods.
"""

from __future__ import annotations

import os
import sys

MODES = ("off", "isolated")
REFUSED = 126
SCRIPT = os.path.abspath(__file__)
EXTRA_KEY = "isolation"
#: The prefix of every refusal the launcher prints.
REFUSAL_PREFIX = "fluid isolation refused"

# --- Landlock (include/uapi/linux/landlock.h) --------------------------------

SYS_LANDLOCK_CREATE_RULESET = 444  # the same number on every architecture
SYS_LANDLOCK_ADD_RULE = 445
SYS_LANDLOCK_RESTRICT_SELF = 446
LANDLOCK_CREATE_RULESET_VERSION = 1 << 0
LANDLOCK_RULE_PATH_BENEATH = 1

FS_EXECUTE = 1 << 0
FS_WRITE_FILE = 1 << 1
FS_READ_FILE = 1 << 2
FS_READ_DIR = 1 << 3
FS_REMOVE_DIR = 1 << 4
FS_REMOVE_FILE = 1 << 5
FS_MAKE_CHAR = 1 << 6
FS_MAKE_DIR = 1 << 7
FS_MAKE_REG = 1 << 8
FS_MAKE_SOCK = 1 << 9
FS_MAKE_FIFO = 1 << 10
FS_MAKE_BLOCK = 1 << 11
FS_MAKE_SYM = 1 << 12
FS_REFER = 1 << 13  # ABI 2
FS_TRUNCATE = 1 << 14  # ABI 3
FS_IOCTL_DEV = 1 << 15  # ABI 5
NET_BIND_TCP = 1 << 0  # ABI 4
NET_CONNECT_TCP = 1 << 1
SCOPE_ABSTRACT_UNIX_SOCKET = 1 << 0  # ABI 6
SCOPE_SIGNAL = 1 << 1

# Rights that may appear on a rule for a single file (not a directory).
_FILE_RIGHTS = FS_EXECUTE | FS_WRITE_FILE | FS_READ_FILE | FS_TRUNCATE | FS_IOCTL_DEV
_READ = FS_EXECUTE | FS_READ_FILE | FS_READ_DIR
_DEVICES = FS_READ_FILE | FS_WRITE_FILE | FS_READ_DIR | FS_TRUNCATE | FS_IOCTL_DEV

#: The lowest Landlock ABI the profile accepts. ABI 1 (Linux 5.13) already
#: confines every open; truncate(2), which it does not, is then blocked with
#: seccomp. ABI 1 also refuses every cross-directory rename and link.
MIN_ABI = 1


def fs_rights(abi: int) -> int:
    rights = (1 << 13) - 1
    if abi >= 2:
        rights |= FS_REFER
    if abi >= 3:
        rights |= FS_TRUNCATE
    if abi >= 5:
        rights |= FS_IOCTL_DEV
    return rights


# --- seccomp -------------------------------------------------------------------

PR_SET_NO_NEW_PRIVS = 38
PR_GET_SECCOMP = 21
PR_SET_SECCOMP = 22
SECCOMP_MODE_FILTER = 2
SECCOMP_RET_KILL_PROCESS = 0x80000000
SECCOMP_RET_ERRNO = 0x00050000
SECCOMP_RET_ALLOW = 0x7FFF0000
_EACCES = 13

#: Per architecture: the audit arch value seccomp reports, and syscall numbers.
SECCOMP_ARCHES = {
    "x86_64": {"audit": 0xC000003E, "x32": True,
               "socket": 41, "truncate": 76, "io_uring": (425, 426, 427)},
    "aarch64": {"audit": 0xC00000B7, "x32": False,
                "socket": 198, "truncate": 45, "io_uring": (425, 426, 427)},
}


def machine() -> str | None:
    """The seccomp architecture this interpreter runs as, or None."""
    import platform
    import struct

    name = platform.machine().lower()
    name = {"amd64": "x86_64", "arm64": "aarch64"}.get(name, name)
    if struct.calcsize("P") != 8:  # a 32-bit interpreter on a 64-bit kernel
        return None
    return name if name in SECCOMP_ARCHES else None


def seccomp_program(arch: str, *, block_network: bool, block_truncate: bool) -> list[tuple[int, int, int, int]]:
    """A classic-BPF seccomp filter as ``(code, jt, jf, k)`` instructions.

    A foreign syscall ABI (i386 or x32 on x86_64) kills the process, since its
    numbers differ and would slip past the table. Blocked calls fail with
    ``EACCES``; everything else is allowed.
    """
    spec = SECCOMP_ARCHES[arch]
    ld_w_abs, jeq_k, jge_k, ret_k = 0x20, 0x15, 0x35, 0x06
    deny = SECCOMP_RET_ERRNO | _EACCES
    blocked: list[int] = []
    if block_network:
        blocked += [spec["socket"], *spec["io_uring"]]
    if block_truncate:
        blocked.append(spec["truncate"])
    prog = [
        (ld_w_abs, 0, 0, 4),  # seccomp_data.arch
        (jeq_k, 1, 0, spec["audit"]),
        (ret_k, 0, 0, SECCOMP_RET_KILL_PROCESS),
        (ld_w_abs, 0, 0, 0),  # seccomp_data.nr
    ]
    if spec["x32"]:
        prog += [(jge_k, 0, 1, 0x40000000), (ret_k, 0, 0, SECCOMP_RET_KILL_PROCESS)]
    for nr in blocked:
        prog += [(jeq_k, 0, 1, nr), (ret_k, 0, 0, deny)]
    prog.append((ret_k, 0, 0, SECCOMP_RET_ALLOW))
    return prog


# --- Kernel probes (both halves) ------------------------------------------------


def _libc():
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    libc.prctl.restype = ctypes.c_int
    return libc


def _prctl(libc, option: int, arg2: int = 0) -> int:
    import ctypes

    zero = ctypes.c_ulong(0)
    return libc.prctl(ctypes.c_int(option), ctypes.c_ulong(arg2), zero, zero, zero)


def landlock_abi() -> tuple[int, str]:
    """``(abi, "")`` when Landlock can be used here, else ``(0, reason)``."""
    if not sys.platform.startswith("linux"):
        return 0, f"Landlock is Linux only (this is {sys.platform})"
    import ctypes
    import errno

    try:
        libc = _libc()
    except OSError as exc:
        return 0, f"libc could not be loaded: {exc}"
    abi = libc.syscall(ctypes.c_long(SYS_LANDLOCK_CREATE_RULESET), ctypes.c_void_p(None),
                       ctypes.c_long(0), ctypes.c_long(LANDLOCK_CREATE_RULESET_VERSION))
    if abi >= MIN_ABI:
        return int(abi), ""
    err = ctypes.get_errno()
    reasons = {
        errno.ENOSYS: "this kernel was built without Landlock, or a seccomp profile "
                      "(for example an old container runtime's) hides its system calls",
        errno.EOPNOTSUPP: "Landlock is built into this kernel but disabled at boot "
                          "(add landlock to the lsm= kernel parameter)",
        errno.EPERM: "the container's seccomp profile blocks the Landlock system calls",
    }
    import platform

    detail = reasons.get(err, os.strerror(err) if err else f"unexpected ABI {abi}")
    return 0, f"Landlock is unavailable on kernel {platform.release()}: {detail}"


def seccomp_available() -> bool:
    if not sys.platform.startswith("linux"):
        return False
    try:
        return _prctl(_libc(), PR_GET_SECCOMP) >= 0
    except OSError:
        return False


# --- The importable part: policy and spawn-site wrapping -----------------------


class IsolationUnavailable(RuntimeError):
    """The isolated profile is on but cannot be enforced; nothing was run."""


#: Read-only (and executable) system roots. Missing ones are skipped.
SYSTEM_READ_ROOTS = ("/bin", "/sbin", "/usr", "/lib", "/lib32", "/lib64", "/libx32",
                     "/etc", "/opt", "/proc", "/sys")
#: Credential locations, relative to the home directory, that no granted root
#: may contain. Fluid's own token files live under ~/.fluid and ~/.config/fluid.
HOME_CREDENTIALS = (
    ".ssh", ".gnupg", ".aws", ".azure", ".kube", ".docker", ".netrc", ".git-credentials",
    ".pgpass", ".npmrc", ".pypirc", ".config/gcloud", ".config/gh", ".config/fluid",
    ".fluid", ".claude", ".claude.json", ".codex",
)
#: Environment variables an isolated child inherits from the host (on top of
#: the session's settings ``env``, which is a deliberate hand-over). A trailing
#: ``*`` matches a prefix.
ENV_ALLOW = ("PATH", "LANG", "LANGUAGE", "LC_*", "TERM", "TZ", "USER", "LOGNAME", "SHELL",
             "COLUMNS", "LINES", "NO_COLOR", "FORCE_COLOR", "PWD")


#: Variables the dynamic loader, libc or the interpreter act on while the
#: launcher itself starts, before it is confined (an ``LD_PRELOAD`` from a
#: workspace settings file would otherwise run unconfined). They travel under
#: this prefix and are restored only for the confined command.
HELD_PREFIX = "FLUID_ISOLATION_HELD_"
_LOADER_NAMES = frozenset({
    "GCONV_PATH", "GETCONF_DIR", "GLIBC_TUNABLES", "HOSTALIASES", "LOCALDOMAIN", "LOCPATH",
    "MALLOC_TRACE", "NIS_PATH", "NLSPATH", "RESOLV_HOST_CONF", "RES_OPTIONS", "TZDIR",
})


def _loader_sensitive(name: str) -> bool:
    upper = name.upper()
    return upper.startswith(("LD_", "MALLOC_")) or upper in _LOADER_NAMES


def hold_loader_env(env: dict[str, str]) -> dict[str, str]:
    return {(HELD_PREFIX + k if _loader_sensitive(k) else k): v
            for k, v in env.items() if not k.startswith(HELD_PREFIX)}


def release_loader_env(env: dict[str, str]) -> dict[str, str]:
    out = {k: v for k, v in env.items() if not _loader_sensitive(k) and not k.startswith(HELD_PREFIX)}
    out.update({k[len(HELD_PREFIX):]: v for k, v in env.items() if k.startswith(HELD_PREFIX)})
    return out


def _real(path: str) -> str:
    return os.path.realpath(os.path.expanduser(path))


def protected_paths(home: str | None = None, extra: tuple[str, ...] = ()) -> tuple[str, ...]:
    home = home or os.path.expanduser("~")
    paths = [os.path.join(home, rel) for rel in HOME_CREDENTIALS]
    return tuple(_real(p) for p in (*paths, *extra))


def _contains(root: str, path: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def _env_allowed(name: str, allow: tuple[str, ...]) -> bool:
    upper = name.upper()
    for pattern in allow:
        pattern = pattern.upper()
        if upper == pattern or (pattern.endswith("*") and upper.startswith(pattern[:-1])):
            return True
    return False


class LinuxLandlock:
    """Landlock (+ seccomp) via the launcher script."""

    name = "linux-landlock"

    def unavailable_reason(self, network: bool) -> str | None:
        abi, reason = landlock_abi()
        if not abi:
            return reason
        if not network or abi < 3:
            if machine() is None:
                import platform

                return (f"the seccomp filter the profile needs (network off, or Landlock ABI {abi} "
                        f"< 3) has no syscall table for {platform.machine()}")
            if not seccomp_available():
                return "seccomp filters are unavailable in this kernel"
        return None

    def prefix(self, writable: tuple[str, ...], readable: tuple[str, ...], network: bool) -> list[str]:
        argv = [sys.executable, "-I", "-S", SCRIPT]
        for root in writable:
            argv += ["--rw", root]
        for root in readable:
            argv += ["--ro", root]
        return [*argv, "--net", "on" if network else "off", "--"]


class Unsupported:
    """A platform with no backend: every spawn is refused."""

    name = "unsupported"

    def __init__(self, platform: str) -> None:
        self.platform = platform

    def unavailable_reason(self, network: bool) -> str | None:
        return (f"the isolated profile has no backend for {self.platform} yet "
                "(Linux with Landlock only)")

    def prefix(self, writable, readable, network) -> list[str]:  # pragma: no cover - never reached
        raise IsolationUnavailable(self.unavailable_reason(network))


def backend(platform: str | None = None):
    platform = platform or sys.platform
    if platform.startswith("linux"):
        return LinuxLandlock()
    return Unsupported(platform)


class IsolationPolicy:
    """One session's isolated profile. Spawn sites call :meth:`wrap`."""

    def __init__(self, *, workspace: str, writable_roots: tuple[str, ...] = (),
                 readable_roots: tuple[str, ...] = (), network: bool = False,
                 env_allow: tuple[str, ...] = (), protect: tuple[str, ...] = (),
                 home: str | None = None, backend_impl=None) -> None:
        self.workspace = _real(workspace)
        self.writable_roots = tuple(_real(p) for p in writable_roots)
        self.readable_roots = tuple(_real(p) for p in readable_roots)
        self.network = bool(network)
        self.env_allow = (*ENV_ALLOW, *env_allow)
        self.protected = protected_paths(home, protect)
        self.backend = backend_impl or backend()
        self._private: str | None = None
        self._checked: str | None = None  # "" once the backend probe passed

    # -- checks --

    def unavailable_reason(self) -> str | None:
        """Why commands cannot run isolated here, or None. The kernel probe is
        cached; root checks are cheap and run every time."""
        if self._checked is None:
            self._checked = self.backend.unavailable_reason(self.network) or ""
        if self._checked:
            return self._checked
        return self._root_problem()

    def _root_problem(self) -> str | None:
        for root in (*self._writable_base(), *self._readable(extra=())):
            if root == "/":
                return "the filesystem root cannot be granted"
            for secret in self.protected:
                if _contains(root, secret) and os.path.lexists(secret):
                    return (f"{root} cannot be granted: it contains the credential location "
                            f"{secret}. Run the session in a project directory, or move the "
                            "directory out of writable_roots/readable_roots")
        return None

    def check(self) -> None:
        reason = self.unavailable_reason()
        if reason:
            raise IsolationUnavailable(
                f"Not run: the isolated execution profile is on ([sandbox] mode = \"isolated\") "
                f"but cannot be enforced: {reason}. Commands are refused rather than run "
                "unisolated; fix the cause or turn the profile off.")

    # -- roots --

    def _writable_base(self) -> tuple[str, ...]:
        return (self.workspace, *self.writable_roots)

    def _readable(self, extra: tuple[str, ...]) -> tuple[str, ...]:
        roots = [*SYSTEM_READ_ROOTS, *self.readable_roots, *extra]
        # The interpreter, for a `python` command and the code-mode kernel.
        for path in (sys.prefix, sys.base_prefix, sys.exec_prefix,
                     os.path.dirname(os.path.realpath(sys.executable))):
            roots.append(os.path.realpath(path))
        # /etc/resolv.conf is often a link into /run (systemd-resolved).
        resolv = os.path.realpath("/etc/resolv.conf")
        if not resolv.startswith("/etc/"):
            roots.append(resolv)
        out: list[str] = []
        for root in roots:
            if root not in out:
                out.append(root)
        return tuple(out)

    def private_dir(self) -> str:
        """The session's private home and temp directory, made on first use."""
        if self._private is None or not os.path.isdir(self._private):
            import atexit
            import shutil
            import tempfile

            path = tempfile.mkdtemp(prefix="fluid-isolated-")
            os.mkdir(os.path.join(path, "home"), 0o700)
            os.mkdir(os.path.join(path, "tmp"), 0o700)
            atexit.register(shutil.rmtree, path, True)
            self._private = path
        return self._private

    # -- the spawn-site API --

    def child_env(self, env: dict[str, str] | None, overlay: dict[str, str] | None = None) -> dict[str, str]:
        """The allowlisted environment: inherited names on :data:`ENV_ALLOW`
        (plus ``env_allow``), then ``overlay`` (settings env) whole, then the
        private ``HOME``/``TMPDIR``."""
        source = os.environ if env is None else env
        out = {k: v for k, v in source.items() if _env_allowed(k, self.env_allow)}
        out.update(overlay or {})
        private = self.private_dir()
        home, tmp = os.path.join(private, "home"), os.path.join(private, "tmp")
        out.update(HOME=home, TMPDIR=tmp, TMP=tmp, TEMP=tmp)
        return out

    def wrap(self, argv: list[str], cwd: str | None, env: dict[str, str] | None, *,
             overlay: dict[str, str] | None = None,
             readable: tuple[str, ...] = ()) -> tuple[list[str], str | None, dict[str, str]]:
        """``(argv, cwd, env)`` for a confined child, or :class:`IsolationUnavailable`.

        ``readable`` adds read-only roots for this spawn only (the code-mode
        kernel's own sources)."""
        self.check()
        env = hold_loader_env(self.child_env(env, overlay))
        writable = (*self._writable_base(), self.private_dir())
        read = self._readable(tuple(_real(p) for p in readable))
        prefix = self.backend.prefix(writable, read, self.network)
        return [*prefix, *[os.fspath(a) for a in argv]], cwd, env

    def describe(self) -> str:
        return (f"isolated ({self.backend.name}): writable {', '.join(self._writable_base())}; "
                f"network {'on' if self.network else 'off'}")


def policy_of(ctx) -> IsolationPolicy | None:
    """The isolated profile a tool context carries, or None (the default)."""
    extra = getattr(ctx, "extra", None) or {}
    policy = extra.get(EXTRA_KEY)
    return policy if isinstance(policy, IsolationPolicy) else None


def policy_from_config(sandbox, cwd: str) -> IsolationPolicy | None:
    """The policy for a :class:`fluid.config.model.SandboxConfig`, or None when off."""
    if sandbox is None or sandbox.mode != "isolated":
        return None
    return IsolationPolicy(
        workspace=cwd, writable_roots=sandbox.writable_roots, readable_roots=sandbox.readable_roots,
        network=sandbox.network, env_allow=sandbox.env_allow, protect=sandbox.protect,
    )


# --- The launcher ----------------------------------------------------------------


class Refused(Exception):
    pass


def _parse(args: list[str]) -> tuple[list[str], list[str], bool, list[str]]:
    rw: list[str] = []
    ro: list[str] = []
    network: bool | None = None
    index = 0
    while index < len(args) and args[index] != "--":
        flag = args[index]
        if index + 1 >= len(args):
            raise Refused(f"{flag} needs a value")
        value = args[index + 1]
        if flag == "--rw":
            rw.append(value)
        elif flag == "--ro":
            ro.append(value)
        elif flag == "--net" and value in ("on", "off"):
            network = value == "on"
        else:
            raise Refused(f"unexpected argument {flag!r}")
        index += 2
    command = args[index + 1:]
    if network is None:
        raise Refused("no --net policy")
    if not command:
        raise Refused("no command")
    return rw, ro, network, command


def _check(result: int, what: str) -> int:
    import ctypes

    if result < 0:
        raise Refused(f"{what} failed: {os.strerror(ctypes.get_errno())}")
    return result


def apply_landlock(rw: list[str], ro: list[str], network: bool) -> int:
    """Confine this thread (and everything it later executes). Returns the ABI."""
    import ctypes

    abi, reason = landlock_abi()
    if not abi:
        raise Refused(reason)
    libc = _libc()

    class RulesetAttr(ctypes.Structure):
        _fields_ = [("handled_access_fs", ctypes.c_uint64), ("handled_access_net", ctypes.c_uint64),
                    ("scoped", ctypes.c_uint64)]

    class PathBeneathAttr(ctypes.Structure):
        _pack_ = 1
        _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]

    handled = fs_rights(abi)
    attr = RulesetAttr(handled, 0, 0)
    size = 8
    if abi >= 4:
        size = 16
        if not network:
            attr.handled_access_net = NET_BIND_TCP | NET_CONNECT_TCP
    if abi >= 6:
        size = 24
        attr.scoped = SCOPE_ABSTRACT_UNIX_SOCKET | SCOPE_SIGNAL
    ruleset = _check(libc.syscall(ctypes.c_long(SYS_LANDLOCK_CREATE_RULESET), ctypes.byref(attr),
                                  ctypes.c_long(size), ctypes.c_long(0)), "landlock_create_ruleset")
    try:
        rules = [(p, handled) for p in rw] + [(p, _READ) for p in ro]
        rules += [("/dev", _DEVICES & handled), ("/dev/shm", handled)]
        for path, access in rules:
            try:
                fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise Refused(f"could not open {path}: {exc.strerror}") from exc
            try:
                import stat

                if not stat.S_ISDIR(os.fstat(fd).st_mode):
                    access &= _FILE_RIGHTS
                rule = PathBeneathAttr(access & handled, fd)
                _check(libc.syscall(ctypes.c_long(SYS_LANDLOCK_ADD_RULE), ctypes.c_long(ruleset),
                                    ctypes.c_long(LANDLOCK_RULE_PATH_BENEATH), ctypes.byref(rule),
                                    ctypes.c_long(0)), f"landlock_add_rule({path})")
            finally:
                os.close(fd)
        _check(_prctl(libc, PR_SET_NO_NEW_PRIVS, 1), "prctl(PR_SET_NO_NEW_PRIVS)")
        _check(libc.syscall(ctypes.c_long(SYS_LANDLOCK_RESTRICT_SELF), ctypes.c_long(ruleset),
                            ctypes.c_long(0)), "landlock_restrict_self")
    finally:
        os.close(ruleset)
    return abi


def apply_seccomp(arch: str | None, *, block_network: bool, block_truncate: bool) -> bool:
    """Install the filter when it has anything to block. True when installed."""
    if not (block_network or block_truncate):
        return False
    if arch is None:
        raise Refused("no seccomp syscall table for this architecture")
    import ctypes

    prog = seccomp_program(arch, block_network=block_network, block_truncate=block_truncate)

    class SockFilter(ctypes.Structure):
        _fields_ = [("code", ctypes.c_uint16), ("jt", ctypes.c_uint8), ("jf", ctypes.c_uint8),
                    ("k", ctypes.c_uint32)]

    class SockFprog(ctypes.Structure):
        _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.POINTER(SockFilter))]

    filters = (SockFilter * len(prog))(*[SockFilter(*ins) for ins in prog])
    fprog = SockFprog(len(prog), filters)
    libc = _libc()
    _check(_prctl(libc, PR_SET_NO_NEW_PRIVS, 1), "prctl(PR_SET_NO_NEW_PRIVS)")
    _check(libc.prctl(ctypes.c_int(PR_SET_SECCOMP), ctypes.c_ulong(SECCOMP_MODE_FILTER),
                      ctypes.byref(fprog), ctypes.c_ulong(0), ctypes.c_ulong(0)),
           "prctl(PR_SET_SECCOMP)")
    return True


def main(args: list[str] | None = None) -> int:
    args = sys.argv[1:] if args is None else args
    try:
        if not sys.platform.startswith("linux"):
            raise Refused(f"no isolation backend for {sys.platform}")
        rw, ro, network, command = _parse(args)
        abi = apply_landlock(rw, ro, network)
        apply_seccomp(machine(), block_network=not network, block_truncate=abi < 3)
        try:
            limit = os.sysconf("SC_OPEN_MAX")
        except (ValueError, OSError):
            limit = 65536
        os.closerange(3, max(limit, 3))
    except (Refused, OSError) as exc:
        sys.stderr.write(f"{REFUSAL_PREFIX}: {exc}\n")
        return REFUSED
    try:
        os.execvpe(command[0], command, release_loader_env(dict(os.environ)))
    except OSError as exc:
        sys.stderr.write(f"fluid isolation: {command[0]}: {exc.strerror or exc}\n")
        return 127
    return 127  # pragma: no cover - execvp does not return


if __name__ == "__main__":
    sys.exit(main())
