"""The opt-in isolated execution profile for model-driven child processes.

Off by default. With ``[sandbox] mode = "isolated"`` every process Fluid starts
to run a command the model chose (a Bash call, a Monitor command, the code-mode
kernel) goes through a small launcher first:

    python -I -S COPY --env FILE --ack FILE --rw DIR ... --ro DIR ... --net off|on -- COMMAND...

The launcher asks the kernel to confine itself, then replaces itself with
``COMMAND``. On Linux the confinement is Landlock (filesystem access rules,
and TCP bind/connect rules from ABI 4) plus a seccomp filter (no new
sockets when the network is off, and io_uring, which could open them behind
seccomp's back). Both are inherited across ``exec`` and ``fork`` and cannot be
lifted by the confined process. Any step that fails stops the command with
exit status 126 before it runs; nothing runs half-confined.

The launcher itself starts with an empty environment that Fluid builds: the
command's environment (settings ``env`` included, which a project controls)
is data in the ``--env`` file and is installed only by the final ``execve``,
after confinement. Once Landlock and seccomp are in place the launcher writes
one byte to the ``--ack`` file (opened before confinement, closed before
``exec``, in a directory no confined process can open). A spawn site that
finds no ack knows the command never ran, whatever the process printed or
exited with.

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
session carries in ``ToolCtx.extra["isolation"]``, :meth:`IsolationPolicy.launch`
returns a :class:`Launch` (the command line, and the ack to check once the
process is done) or raises :class:`IsolationUnavailable`.

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
#: Set on a tool context by the engine factory: the policy it resolved, so a
#: later change to ``extra["isolation"]`` is refused instead of half applied.
BOUND_KEY = "isolation_bound"
#: The byte the launcher writes to its ack file once it is confined.
ACK = b"\x01"
#: The whole environment the launcher process starts with.
LAUNCHER_ENV: dict[str, str] = {}

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
FS_RESOLVE_UNIX = 1 << 16  # ABI 9: connect to a pathname Unix socket
NET_BIND_TCP = 1 << 0  # ABI 4
NET_CONNECT_TCP = 1 << 1
NET_BIND_UDP = 1 << 2  # ABI 10
NET_CONNECT_SEND_UDP = 1 << 3
SCOPE_ABSTRACT_UNIX_SOCKET = 1 << 0  # ABI 6
SCOPE_SIGNAL = 1 << 1

# Rights that may appear on a rule for a single file (not a directory); the
# kernel's ACCESS_FILE mask.
_FILE_RIGHTS = (FS_EXECUTE | FS_WRITE_FILE | FS_READ_FILE | FS_TRUNCATE | FS_IOCTL_DEV
                | FS_RESOLVE_UNIX)
_READ = FS_EXECUTE | FS_READ_FILE | FS_READ_DIR
#: The only device nodes a confined child may open, and how. Nothing else
#: under /dev (no /dev/shm, no directory listing, no device ioctls).
_RW_DEVICE = FS_READ_FILE | FS_WRITE_FILE | FS_TRUNCATE
DEVICE_RULES = (("/dev/null", _RW_DEVICE), ("/dev/full", _RW_DEVICE), ("/dev/tty", _RW_DEVICE),
                ("/dev/zero", FS_READ_FILE), ("/dev/random", FS_READ_FILE),
                ("/dev/urandom", FS_READ_FILE))

#: The lowest Landlock ABI the profile accepts. ABI 1 (Linux 5.13) already
#: confines every open; truncate(2), which it does not, is then blocked with
#: seccomp. ABI 1 also refuses every cross-directory rename and link.
MIN_ABI = 1
#: The newest ABI this backend was reviewed against (the kernel's
#: include/uapi/linux/landlock.h and Documentation/userspace-api/landlock.rst).
#: ABI 7 (audit log flags), 8 (thread sync) and 11 (a no_new_privs flag) add
#: no access rights; 9 adds FS_RESOLVE_UNIX and 10 the UDP rights, both
#: handled below; 12 adds capability and namespace *permissions*, which
#: restrict privilege use rather than file or network access and are left
#: unhandled, as on older kernels. A newer kernel may add rights this code
#: would leave unhandled, so it is refused until reviewed.
MAX_ABI = 12


def fs_rights(abi: int) -> int:
    rights = (1 << 13) - 1
    if abi >= 2:
        rights |= FS_REFER
    if abi >= 3:
        rights |= FS_TRUNCATE
    if abi >= 5:
        rights |= FS_IOCTL_DEV
    if abi >= 9:
        rights |= FS_RESOLVE_UNIX
    return rights


def net_rights(abi: int, network: bool) -> int:
    """The network rights the ruleset handles (denies): all of them when the
    network is off, none when it is on."""
    if network or abi < 4:
        return 0
    rights = NET_BIND_TCP | NET_CONNECT_TCP
    if abi >= 10:
        rights |= NET_BIND_UDP | NET_CONNECT_SEND_UDP
    return rights


# --- seccomp -------------------------------------------------------------------

PR_SET_NO_NEW_PRIVS = 38
PR_GET_SECCOMP = 21
PR_SET_SECCOMP = 22
SECCOMP_MODE_FILTER = 2
SECCOMP_RET_KILL_PROCESS = 0x80000000
SECCOMP_RET_ERRNO = 0x00050000
SECCOMP_RET_ALLOW = 0x7FFF0000
_EPERM = 1
_EACCES = 13
AF_UNIX = 1
SOCK_STREAM = 1
SOCK_SEQPACKET = 5
SOCK_NONBLOCK = 0o4000
SOCK_CLOEXEC = 0o2000000

#: Per architecture: the audit arch value seccomp reports, and syscall numbers
#: (arch/x86/entry/syscalls/syscall_64.tbl and scripts/syscall.tbl).
#: "metadata" are the ownership and extended-attribute setters, which Landlock
#: does not mediate: chown/fchown/lchown/fchownat, set/remove xattr in every
#: form, including setxattrat/removexattrat (Linux 6.13).
SECCOMP_ARCHES = {
    "x86_64": {"audit": 0xC000003E, "x32": True,
               "socket": 41, "socketpair": 53, "truncate": 76, "io_uring": (425, 426, 427),
               "metadata": (92, 93, 94, 260, 188, 189, 190, 197, 198, 199, 463, 466)},
    "aarch64": {"audit": 0xC00000B7, "x32": False,
                "socket": 198, "socketpair": 199, "truncate": 45, "io_uring": (425, 426, 427),
                "metadata": (54, 55, 5, 6, 7, 14, 15, 16, 463, 466)},
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
    numbers differ and would slip past the table. Ownership and xattr changes
    always fail with ``EPERM`` (what an unprivileged chown returns, which
    tools such as ``cp -p`` and ``tar`` already tolerate); the other blocked
    calls fail with ``EACCES``; everything else is allowed.

    With the network off, ``socketpair`` is allowed only for ``AF_UNIX``
    stream and seqpacket pairs (asyncio and most runtimes need those). A
    datagram pair is refused: ``sendto`` with an address on one reaches any
    pathname datagram socket, inside the sandbox or not.
    """
    spec = SECCOMP_ARCHES[arch]
    ld_w_abs, jeq_k, jge_k, ret_k, and_k = 0x20, 0x15, 0x35, 0x06, 0x54
    deny = SECCOMP_RET_ERRNO | _EACCES
    blocked: list[tuple[int, int]] = [(nr, SECCOMP_RET_ERRNO | _EPERM) for nr in spec["metadata"]]
    if block_network:
        blocked += [(nr, deny) for nr in (spec["socket"], *spec["io_uring"])]
    if block_truncate:
        blocked.append((spec["truncate"], deny))
    prog = [
        (ld_w_abs, 0, 0, 4),  # seccomp_data.arch
        (jeq_k, 1, 0, spec["audit"]),
        (ret_k, 0, 0, SECCOMP_RET_KILL_PROCESS),
        (ld_w_abs, 0, 0, 0),  # seccomp_data.nr
    ]
    if spec["x32"]:
        prog += [(jge_k, 0, 1, 0x40000000), (ret_k, 0, 0, SECCOMP_RET_KILL_PROCESS)]
    for nr, verdict in blocked:
        prog += [(jeq_k, 0, 1, nr), (ret_k, 0, 0, verdict)]
    if block_network:
        # seccomp_data.args[i] is a u64 at 16 + 8*i; both arches are little
        # endian and the kernel reads domain and type as int, so the low word.
        prog += [
            (jeq_k, 0, 8, spec["socketpair"]),  # not socketpair: to the final allow
            (ld_w_abs, 0, 0, 16),  # domain
            (jeq_k, 1, 0, AF_UNIX),
            (ret_k, 0, 0, deny),
            (ld_w_abs, 0, 0, 24),  # type
            (and_k, 0, 0, ~(SOCK_NONBLOCK | SOCK_CLOEXEC) & 0xFFFFFFFF),
            (jeq_k, 2, 0, SOCK_STREAM),
            (jeq_k, 1, 0, SOCK_SEQPACKET),
            (ret_k, 0, 0, deny),
        ]
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

    try:
        libc = _libc()
    except OSError as exc:
        return 0, f"libc could not be loaded: {exc}"
    abi = libc.syscall(ctypes.c_long(SYS_LANDLOCK_CREATE_RULESET), ctypes.c_void_p(None),
                       ctypes.c_long(0), ctypes.c_long(LANDLOCK_CREATE_RULESET_VERSION))
    return _abi_verdict(int(abi), ctypes.get_errno())


def _abi_verdict(abi: int, err: int) -> tuple[int, str]:
    import errno
    import platform

    if abi > MAX_ABI:
        return 0, (f"this kernel's Landlock ABI {abi} is newer than the isolated profile has been "
                   f"reviewed against (up to {MAX_ABI}); access rights it adds would go unenforced")
    if abi >= MIN_ABI:
        return abi, ""
    reasons = {
        errno.ENOSYS: "this kernel was built without Landlock, or a seccomp profile "
                      "(for example an old container runtime's) hides its system calls",
        errno.EOPNOTSUPP: "Landlock is built into this kernel but disabled at boot "
                          "(add landlock to the lsm= kernel parameter)",
        errno.EPERM: "the container's seccomp profile blocks the Landlock system calls",
    }
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


# The command's environment travels to the launcher as data, never as the
# launcher's own environment: the dynamic loader, libc and CPython's path
# setup (PYTHONEXECUTABLE and __PYVENV_LAUNCHER__ are read even under -I)
# act on variables before anything is confined.


def encode_env(env: dict[str, str]) -> bytes:
    """``NAME=value`` entries, each NUL-terminated (UTF-8, surrogateescape)."""
    out = bytearray()
    for name, value in env.items():
        key, val = name.encode("utf-8", "surrogateescape"), value.encode("utf-8", "surrogateescape")
        if not key or b"=" in key or b"\0" in key or b"\0" in val:
            raise ValueError(f"environment variable {name!r} cannot be passed to a command")
        out += key + b"=" + val + b"\0"
    return bytes(out)


def decode_env(data: bytes) -> dict[str, str]:
    env: dict[str, str] = {}
    for entry in data.split(b"\0"):
        if not entry:
            continue
        key, sep, val = entry.partition(b"=")
        if not sep or not key:
            raise ValueError("malformed environment file")
        env[key.decode("utf-8", "surrogateescape")] = val.decode("utf-8", "surrogateescape")
    return env


def read_env_file(path: str) -> dict[str, str]:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(fd, "rb") as handle:
        return decode_env(handle.read())


def _real(path: str) -> str:
    return os.path.realpath(os.path.expanduser(path))


def protected_paths(home: str | None = None, extra: tuple[str, ...] = ()) -> tuple[str, ...]:
    home = home or os.path.expanduser("~")
    paths = [os.path.join(home, rel) for rel in HOME_CREDENTIALS]
    return tuple(_real(p) for p in (*paths, *extra))


def _contains(root: str, path: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def _overlaps(a: str, b: str) -> bool:
    return _contains(a, b) or _contains(b, a)


def is_device_path(path: str) -> bool:
    """``/dev`` or anything under it, after ``..`` is folded."""
    return _contains("/dev", os.path.normpath(path))


# --- What runs before confinement ---------------------------------------------
#
# The launcher is a Python script, so the interpreter, the stdlib it imports
# and the script itself all run *before* the kernel confines anything. None of
# them may be writable by an earlier confined command, or that command could
# make the next spawn run its code unconfined.
#
# The script is therefore never run from where Fluid is installed (a source
# checkout or editable install may be the workspace itself): its source is
# read once, when this module is imported, and written to a private
# directory outside every granted root. The interpreter cannot be copied, so
# a configuration that grants write access to it is refused.

#: The interpreter the launcher runs under: the real binary, not a venv
#: link whose pyvenv.cfg could point it at another stdlib.
LAUNCH_PYTHON = os.path.realpath(sys.executable) if sys.executable else ""
#: The launcher's source, as this process imported it (None if unreadable,
#: which refuses every isolated spawn).
LAUNCHER_SOURCE: bytes | None = None
if __name__ != "__main__":
    try:
        with open(__file__, "rb") as _handle:
            LAUNCHER_SOURCE = _handle.read()
    except OSError:  # pragma: no cover - a frozen or zipped install
        pass
#: Trees the dynamic loader and libc read while any process starts.
_LOADER_TREES = ("/etc", "/lib", "/lib32", "/lib64", "/libx32", "/usr/lib", "/usr/lib32",
                 "/usr/lib64", "/usr/libx32", "/usr/local/lib")
_launcher_copy: str | None = None


def launcher_script() -> str:
    """The private copy of the launcher every spawn runs (made on first use)."""
    global _launcher_copy
    if _launcher_copy is None or not os.path.isfile(_launcher_copy):
        import atexit
        import shutil
        import tempfile

        directory = tempfile.mkdtemp(prefix="fluid-launcher-")
        path = os.path.join(directory, "fluid_isolation_launcher.py")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o400)
        with os.fdopen(fd, "wb") as handle:
            handle.write(LAUNCHER_SOURCE)
        atexit.register(shutil.rmtree, directory, True)
        _launcher_copy = os.path.realpath(path)
    return _launcher_copy


class _Unverifiable(Exception):
    pass


def boot_paths() -> tuple[tuple[str, ...], tuple[str, ...]]:
    """``(trees, files)`` read before the launcher confines itself, each both
    as named and as its real target (a loader directory may be a link).
    Raises :class:`_Unverifiable` for a layout this cannot pin down."""
    import sysconfig

    named = [os.path.dirname(launcher_script()), *_LOADER_TREES]
    stdlib = sysconfig.get_path("stdlib")
    if not stdlib or not os.path.isdir(stdlib):
        raise _Unverifiable(f"the interpreter's standard library ({stdlib!r}) is not a directory")
    named.append(stdlib)
    try:
        import _ctypes

        named.append(os.path.dirname(_ctypes.__file__))
    except (ImportError, AttributeError):  # pragma: no cover - builtin _ctypes
        pass
    named.append(os.path.join(sys.base_prefix, "lib"))
    trees: list[str] = []
    for tree in named:
        trees += [os.path.abspath(tree), os.path.realpath(tree)]
    if not LAUNCH_PYTHON or not os.path.isfile(LAUNCH_PYTHON) or os.path.islink(LAUNCH_PYTHON):
        raise _Unverifiable(f"the launcher's interpreter {LAUNCH_PYTHON!r} is not a regular file")
    bindir = os.path.dirname(LAUNCH_PYTHON)
    files = [LAUNCH_PYTHON, os.path.join(bindir, "pyvenv.cfg"),
             os.path.join(os.path.dirname(bindir), "pyvenv.cfg")]
    return tuple(dict.fromkeys(trees)), tuple(files)


def boot_problem(writable: tuple[str, ...]) -> str | None:
    """Why ``writable`` would let a confined command change what the next
    launch runs before confinement, or None. ``writable`` holds real paths;
    the trees are compared both as named and by their real targets."""
    if not LAUNCH_PYTHON:
        return "there is no Python interpreter to run the isolation launcher with"
    if not LAUNCHER_SOURCE:
        return "the isolation launcher's source could not be read"
    try:
        trees, files = boot_paths()
    except _Unverifiable as exc:
        return f"what the isolation launcher loads before it is confined cannot be verified: {exc}"
    for root in writable:
        for tree in trees:
            if _overlaps(root, tree):
                return (f"{root} cannot be writable: the isolation launcher reads {tree} before it "
                        "is confined, so a confined command could change what the next one runs "
                        "unconfined")
        for path in files:
            if _contains(root, os.path.dirname(path)):
                return (f"{root} cannot be writable: it holds {path}, which runs before the "
                        "isolation launcher is confined. Use a Python installed outside the "
                        "workspace and writable_roots")
    return None


def _private_file(prefix: str, data: bytes = b"") -> str:
    """A new 0600 file beside the launcher copy: outside every granted root,
    so no confined process can open it."""
    import tempfile

    fd, path = tempfile.mkstemp(prefix=prefix, dir=os.path.dirname(launcher_script()))
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    return path


class Launch:
    """One confined spawn: the command line to start, and the ack to read
    once the process has exited (or been given up on).

    Spawn sites call :meth:`finish` exactly when they are done with the
    process: True means the launcher confined itself and handed over to the
    command; False means the command never ran, whatever the process
    printed or exited with."""

    def __init__(self, argv: list[str], cwd: str | None, env: dict[str, str],
                 env_path: str, ack_path: str) -> None:
        self.argv, self.cwd, self.env = argv, cwd, env
        self.env_path, self.ack_path = env_path, ack_path
        self._acked: bool | None = None

    def acked(self) -> bool:
        if self._acked is not None:
            return self._acked
        try:
            with open(self.ack_path, "rb") as handle:
                return handle.read(len(ACK) + 1) == ACK
        except OSError:
            return False

    def finish(self) -> bool:
        """Read the ack, then remove both files. Idempotent."""
        if self._acked is None:
            self._acked = self.acked()
            for path in (self.env_path, self.ack_path):
                try:
                    os.unlink(path)
                except OSError:
                    pass
        return self._acked

    @staticmethod
    def not_run(output: str, what: str = "the command") -> str:
        """The "not run" message for a launch that was not acknowledged, with
        the launcher's own refusal when it printed one."""
        line = next((ln.strip() for ln in (output or "").splitlines()
                     if ln.strip().startswith(REFUSAL_PREFIX)), "")
        reason = line or "the isolation launcher stopped before it confirmed confinement"
        return f"Not run: the isolated execution profile could not confine {what} ({reason})."


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
        # The seccomp filter is always installed: it blocks ownership and
        # xattr changes, which Landlock does not mediate.
        if machine() is None:
            import platform

            return (f"the seccomp filter the profile needs has no syscall table for "
                    f"{platform.machine()} (x86_64 and aarch64 only)")
        if not seccomp_available():
            return "seccomp filters are unavailable in this kernel"
        return None

    def prefix(self, writable: tuple[str, ...], readable: tuple[str, ...], network: bool, *,
               protect: tuple[str, ...] = (), env_file: str, ack_file: str) -> list[str]:
        argv = [LAUNCH_PYTHON, "-I", "-S", launcher_script(), "--env", env_file, "--ack", ack_file]
        for root in writable:
            argv += ["--rw", root]
        for root in readable:
            argv += ["--ro", root]
        for path in protect:
            argv += ["--protect", path]
        return [*argv, "--net", "on" if network else "off", "--"]


class Unsupported:
    """A platform with no backend: every spawn is refused."""

    name = "unsupported"

    def __init__(self, platform: str) -> None:
        self.platform = platform

    def unavailable_reason(self, network: bool) -> str | None:
        return (f"the isolated profile has no backend for {self.platform} yet "
                "(Linux with Landlock only)")

    def prefix(self, writable, readable, network, **_files) -> list[str]:  # pragma: no cover - never reached
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

    def unavailable_reason(self, readable: tuple[str, ...] = ()) -> str | None:
        """Why commands cannot run isolated here, or None. The kernel probe is
        cached; root checks are cheap and run every time. ``readable`` adds a
        spawn's own read-only roots to the check."""
        if self._checked is None:
            self._checked = self.backend.unavailable_reason(self.network) or ""
        if self._checked:
            return self._checked
        return self._root_problem(readable)

    def _root_problem(self, extra: tuple[str, ...] = ()) -> str | None:
        """Pathname checks, in both directions, on every root a spawn grants.

        The launcher repeats them on the objects it actually opens (see
        :func:`opened_problem`), which catches a root swapped for a symlink
        after this check."""
        writable = self._writable_base()
        for root in (*writable, *self._readable(extra)):
            if root == "/":
                return "the filesystem root cannot be granted"
            if is_device_path(root):
                return (f"{root} cannot be granted: device nodes are reachable only through the "
                        "profile's fixed device list (/dev/null, /dev/zero, /dev/urandom, ...)")
            for secret in self.protected:
                if not os.path.lexists(secret):
                    continue
                if _contains(root, secret):
                    return (f"{root} cannot be granted: it contains the credential location "
                            f"{secret}. Run the session in a project directory, or move the "
                            "directory out of writable_roots/readable_roots")
                if _contains(secret, root):
                    return (f"{root} cannot be granted: it is inside the credential location "
                            f"{secret}")
        return boot_problem(writable)

    def check(self, readable: tuple[str, ...] = ()) -> None:
        reason = self.unavailable_reason(readable)
        if reason:
            raise IsolationUnavailable(
                f"Not run: the isolated execution profile is on ([sandbox] mode = \"isolated\") "
                f"but cannot be enforced: {reason}. Commands are refused rather than run "
                "unisolated; fix the cause or turn the profile off.")

    # -- roots --

    def _writable_base(self) -> tuple[str, ...]:
        return (self.workspace, *self.writable_roots)

    def _readable(self, extra: tuple[str, ...]) -> tuple[str, ...]:
        # Real paths throughout: the launcher refuses a root that resolves to
        # anything other than the path it was given (/bin -> usr/bin and the
        # like are resolved here, not followed there).
        roots = [_real(p) for p in (*SYSTEM_READ_ROOTS, *self.readable_roots, *extra)]
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

            path = os.path.realpath(tempfile.mkdtemp(prefix="fluid-isolated-"))
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

    def launch(self, argv: list[str], cwd: str | None, env: dict[str, str] | None, *,
               overlay: dict[str, str] | None = None, readable: tuple[str, ...] = ()) -> Launch:
        """A confined spawn of ``argv``, or :class:`IsolationUnavailable`.

        ``env`` is filtered to the allowlist and ``overlay`` (the settings
        env) layered on top; the result is what the *command* gets, written
        to a private file the launcher installs after confinement. The
        returned :attr:`Launch.env` is the launcher's own, empty one.
        ``readable`` adds read-only roots for this spawn only (the code-mode
        kernel's own sources); they pass the same checks as configured roots.
        The caller must call :meth:`Launch.finish` when done with the process."""
        readable = tuple(_real(p) for p in readable)
        self.check(readable)
        command_env = self.child_env(env, overlay)
        from fluid.tools import agent_runner

        if agent_runner.enabled():
            # What agent_runner.wrap drops from a command's environment; the
            # runner only sees the launcher's empty one now.
            command_env = {k: v for k, v in command_env.items()
                           if not agent_runner.is_app_secret(k) and not k.upper().startswith("SHUFFLE_AGENT_")}
        env_path = _private_file("env-", encode_env(command_env))
        ack_path = _private_file("ack-")
        writable = (*self._writable_base(), self.private_dir())
        read = self._readable(readable)
        prefix = self.backend.prefix(writable, read, self.network, protect=self.protected,
                                     env_file=env_path, ack_file=ack_path)
        return Launch([*prefix, *[os.fspath(a) for a in argv]], cwd, dict(LAUNCHER_ENV),
                      env_path, ack_path)

    def for_session(self) -> IsolationPolicy:
        """The same policy for another session (a Task or Swarm worker): the
        same roots and network rule, its own private home and temp directory."""
        import copy

        twin = copy.copy(self)
        twin._private = None
        return twin

    def describe(self) -> str:
        return (f"isolated ({self.backend.name}): writable {', '.join(self._writable_base())}; "
                f"network {'on' if self.network else 'off'}")


class _Binding:
    """What the engine factory resolved (a policy, or None for off)."""

    __slots__ = ("policy",)

    def __init__(self, policy: IsolationPolicy | None) -> None:
        self.policy = policy


CHANGED_AFTER_BUILD = (
    "Not run: ToolCtx.extra[\"isolation\"] was changed after this engine was built, so its "
    "commands, hooks and code-mode kernel would no longer share one policy. A host must put "
    "the policy on the context before build_engine; changing it later is not supported.")


def resolve_policy(injected, configured: IsolationPolicy | None) -> IsolationPolicy | None:
    """The one policy an engine runs under: the host's when it injected one,
    else the configured one. A host value of None never turns a configured
    profile off (config wins toward the safer setting)."""
    return injected if isinstance(injected, IsolationPolicy) else configured


def bind(ctx, policy: IsolationPolicy | None) -> None:
    """Put the resolved policy on ``ctx`` and record it, so every spawn site
    reads the same object and a later change is refused (:func:`policy_of`)."""
    if policy is not None:
        ctx.extra[EXTRA_KEY] = policy
    ctx.extra[BOUND_KEY] = _Binding(policy)


def policy_of(ctx) -> IsolationPolicy | None:
    """The isolated profile a tool context carries, or None (the default).

    Raises :class:`IsolationUnavailable` when the context was bound by the
    engine factory and ``extra["isolation"]`` no longer holds that policy."""
    extra = getattr(ctx, "extra", None) or {}
    policy = extra.get(EXTRA_KEY)
    policy = policy if isinstance(policy, IsolationPolicy) else None
    binding = extra.get(BOUND_KEY)
    if isinstance(binding, _Binding) and binding.policy is not policy:
        raise IsolationUnavailable(CHANGED_AFTER_BUILD)
    return policy


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


def _parse(args: list[str]) -> tuple[list[str], list[str], bool, list[str], list[str], dict[str, str]]:
    rw: list[str] = []
    ro: list[str] = []
    protect: list[str] = []
    files: dict[str, str] = {}
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
        elif flag == "--protect":
            protect.append(value)
        elif flag == "--net" and value in ("on", "off"):
            network = value == "on"
        elif flag in ("--env", "--ack") and flag not in files:
            files[flag] = value
        else:
            raise Refused(f"unexpected argument {flag!r}")
        index += 2
    command = args[index + 1:]
    if network is None:
        raise Refused("no --net policy")
    if not command:
        raise Refused("no command")
    if set(files) != {"--env", "--ack"}:
        raise Refused("no --env and --ack files")
    for path in (*rw, *ro, *protect, *files.values()):
        if not os.path.isabs(path):
            raise Refused(f"{path!r} is not an absolute path")
    return rw, ro, network, command, protect, files


def _identity(st) -> tuple[int, int]:
    return st.st_dev, st.st_ino


def protected_identities(protect: list[str]) -> tuple[set, set]:
    """``(locations, around)``: the (device, inode) of each existing protected
    location, and of every directory above one (short of ``/``)."""
    locations: set = set()
    around: set = set()
    for path in protect:
        try:
            locations.add(_identity(os.stat(path)))
        except OSError:
            continue
        parent = os.path.dirname(path)
        while parent and parent != "/":
            try:
                around.add(_identity(os.stat(parent)))
            except OSError:
                pass
            parent = os.path.dirname(parent)
    return locations, around


_DIR_FLAGS = os.O_PATH | os.O_CLOEXEC | os.O_DIRECTORY if hasattr(os, "O_PATH") else 0


def _resolved(fd: int) -> str:
    return os.readlink(f"/proc/self/fd/{fd}")


def opened_problem(fd: int, path: str, locations: set, around: set) -> str | None:
    """Why the object ``fd`` (opened from ``path``) must not be granted, or None.

    Checked on the opened object, not the name, so a root swapped for a
    symlink between the parent's check and this open is refused, and a bind
    mount of a protected location or of a directory above one is recognised
    by its inode. Walking up uses ``..`` from the object itself."""
    import stat

    try:
        actual = _resolved(fd)
    except OSError as exc:
        return f"could not confirm what {path} resolves to: {exc.strerror}"
    if actual != path:
        return f"{path} resolved to {actual} when opened; a granted root must not be a link"
    if is_device_path(actual):
        return f"{path} cannot be granted: device nodes are reachable only through the fixed device list"
    st = os.fstat(fd)
    ident = _identity(st)
    if ident == _identity(os.stat("/")):
        return "the filesystem root cannot be granted"
    if ident in locations or ident in around:
        return f"{path} is or contains a protected credential location"
    if stat.S_ISDIR(st.st_mode):
        current = os.open("..", _DIR_FLAGS, dir_fd=fd)
    else:
        parent = os.path.dirname(path)
        current = os.open(parent, _DIR_FLAGS)
        try:
            if _resolved(current) != parent:
                os.close(current)
                return f"the directory holding {path} moved while it was being checked"
        except OSError as exc:
            os.close(current)
            return f"could not confirm what {parent} resolves to: {exc.strerror}"
    try:
        below = ident
        for _ in range(4096):
            here = _identity(os.fstat(current))
            if here in locations:
                return f"{path} is inside a protected credential location"
            if here == below:  # ``..`` of the top is itself
                return None
            below = here
            parent_fd = os.open("..", _DIR_FLAGS, dir_fd=current)
            os.close(current)
            current = parent_fd
        return f"{path} is nested too deeply to check"
    finally:
        os.close(current)


def _check(result: int, what: str) -> int:
    import ctypes

    if result < 0:
        raise Refused(f"{what} failed: {os.strerror(ctypes.get_errno())}")
    return result


def apply_landlock(rw: list[str], ro: list[str], network: bool, protect: list[str] = ()) -> int:
    """Confine this thread (and everything it later executes). Returns the ABI.

    Every root is opened first and the opened object is what gets checked
    (:func:`opened_problem`) and granted, so a name swapped between the
    parent's check and this open cannot redirect a rule."""
    import ctypes
    import stat

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
        attr.handled_access_net = net_rights(abi, network)
    if abi >= 6:
        size = 24  # later ABIs grow the struct with fields this backend leaves zero
        attr.scoped = SCOPE_ABSTRACT_UNIX_SOCKET | SCOPE_SIGNAL
    locations, around = protected_identities(list(protect))
    ruleset = _check(libc.syscall(ctypes.c_long(SYS_LANDLOCK_CREATE_RULESET), ctypes.byref(attr),
                                  ctypes.c_long(size), ctypes.c_long(0)), "landlock_create_ruleset")
    try:
        rules = [(p, handled, False) for p in rw] + [(p, _READ, False) for p in ro]
        rules += [(p, access, True) for p, access in DEVICE_RULES]
        for path, access, device in rules:
            try:
                fd = os.open(path, os.O_PATH | os.O_CLOEXEC | os.O_NOFOLLOW)
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise Refused(f"could not open {path}: {exc.strerror}") from exc
            try:
                mode = os.fstat(fd).st_mode
                if device:
                    if not stat.S_ISCHR(mode):
                        continue  # not the device node it should be: grant nothing
                elif stat.S_ISLNK(mode):  # O_NOFOLLOW opened the link itself
                    raise Refused(f"{path} is a symbolic link; a granted root must not be a link")
                else:
                    problem = opened_problem(fd, path, locations, around)
                    if problem:
                        raise Refused(problem)
                if not stat.S_ISDIR(mode):
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
    """Install the filter (always: it at least blocks ownership and xattr
    changes). True when installed."""
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
        rw, ro, network, command, protect, files = _parse(args)
        # Both files sit beside the launcher copy, outside every root, so they
        # are opened now: once confined this process could not open them.
        ack = os.open(files["--ack"], os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            env = read_env_file(files["--env"])
        except ValueError as exc:
            raise Refused(str(exc)) from exc
        try:
            os.unlink(files["--env"])  # the parent removes it too; sooner is better
        except OSError:
            pass
        abi = apply_landlock(rw, ro, network, protect)
        apply_seccomp(machine(), block_network=not network, block_truncate=abi < 3)
        # The positive acknowledgement: confined, about to run the command.
        if os.write(ack, ACK) != len(ACK):
            raise Refused("could not write the confinement acknowledgement")
        os.close(ack)
        try:
            limit = os.sysconf("SC_OPEN_MAX")
        except (ValueError, OSError):
            limit = 65536
        os.closerange(3, max(limit, 3))
    except (Refused, OSError) as exc:
        sys.stderr.write(f"{REFUSAL_PREFIX}: {exc}\n")
        return REFUSED
    try:
        os.execvpe(command[0], command, env)
    except OSError as exc:
        sys.stderr.write(f"fluid isolation: {command[0]}: {exc.strerror or exc}\n")
        return 127
    return 127  # pragma: no cover - execvp does not return


if __name__ == "__main__":
    sys.exit(main())
