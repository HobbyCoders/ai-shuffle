"""OS-level sandbox for the code-mode kernel child (bubblewrap).

Why this exists
---------------
The kernel child's in-process guards (``_audit_guard`` and the builtins
patches in ``_runner``) are defence in depth, not a trust boundary: a process
that runs attacker code cannot fully sandbox itself. This module puts the
child behind a real one when the host can provide it, by launching it under
`bubblewrap <https://github.com/containers/bubblewrap>`_ (``bwrap``):

* **new user, pid, net, ipc, uts and cgroup namespaces** (``--unshare-all``
  plus ``--unshare-user``; ``--disable-userns`` when bwrap supports it, so the
  cell cannot nest a namespace of its own). No network: host calls travel over
  the stdio pipe, never a socket. ``allow_network=True`` re-shares the host
  network (``--share-net``) and exposes the resolver files.
* **an empty root filesystem** holding only what ``python -I -S`` needs, all
  read-only: ``/usr`` (with the merged-``/usr`` symlinks ``/bin``, ``/lib``,
  ``/lib64``, ``/sbin``), ``/etc/ld.so.cache`` and ``/etc/localtime``, the
  Python installation prefix(es) when they live outside ``/usr``, and the
  directory holding ``_runner.py`` and its stdlib-only siblings. Nothing else
  of the host is visible: no home directory, no ``/etc/passwd``, no workspace,
  no data dir. ``/tmp`` is a private tmpfs, ``/proc`` a fresh procfs that
  shows only the sandbox's own two processes, ``/dev`` the minimal set.
* **process lifetime tied to the host**: ``--die-with-parent`` and
  ``--new-session``. The host still kills the child's process group; that
  kills the outer bwrap, whose death SIGKILLs the in-namespace init and so
  every process in the pid namespace.
* **a minimal environment**: ``PATH``, ``LANG``, ``TMPDIR`` and nothing else
  (the unsandboxed child gets the host environment minus credentials).

The memory cap is unchanged: the child still applies ``RLIMIT_AS`` to itself.
No seccomp filter is loaded (bwrap only accepts a prebuilt BPF program and the
kernel ships none); the namespaces above are the boundary.

Operator setting: ``code_mode_os_sandbox`` (env ``CODE_MODE_OS_SANDBOX``)
-------------------------------------------------------------------------
``auto`` (default)
    Use the sandbox when ``bwrap`` is on ``PATH`` and a one-off self-test
    passes; otherwise log one warning naming the reason and run the child
    unsandboxed. A kernel configured with ``allow_fs=True`` also runs
    unsandboxed in this mode: direct file I/O was explicitly granted, and the
    sandbox would silently redirect it to a throwaway tmpfs.
``required``
    Never run the child without the sandbox. When it is unavailable the
    kernel does not start and every cell returns an error naming the reason.
``off``
    Never use the sandbox (the pre-sandbox behaviour).

The self-test runs once per host process (per bwrap/python pair) and a pass is
cached; a failure is cached for ``_FAILURE_TTL`` seconds only, so a host fixed
while the process runs is noticed without a restart. A spawn that bwrap itself
refuses after a passing self-test (its ``bwrap:`` message and exit status 1
before the child starts, e.g. user-namespace exhaustion) drops the cached pass;
``auto`` logs it and starts that kernel unsandboxed, ``required`` refuses with
bwrap's reason. The self-test fails, and ``auto`` falls back, when: ``bwrap`` is not installed;
unprivileged user namespaces are disabled (``kernel.unprivileged_userns_clone
=0``, ``user.max_user_namespaces=0``); AppArmor restricts them (Ubuntu 24.04+
``kernel.apparmor_restrict_unprivileged_userns=1`` without a bwrap profile);
or a container runtime's seccomp/AppArmor profile blocks ``unshare``/``mount``
(Docker's default profile does). The log line quotes bwrap's own error.

Docker: the image installs ``bubblewrap``, but the container must also be
allowed to create user namespaces — for example
``--security-opt seccomp=unconfined --security-opt apparmor=unconfined``, or a
custom seccomp profile permitting ``unshare``/``clone`` with namespace flags
and ``mount`` — and to mount a fresh procfs, which the kernel refuses while
Docker's masked and read-only ``/proc`` paths are mounted over the container's
own ``/proc`` (``bwrap: Can't mount proc on /newroot/proc: Operation not
permitted``): ``--security-opt systempaths=unconfined`` removes them. The host
must have unprivileged user namespaces enabled. Otherwise ``auto`` falls back
(with the log line) and ``required`` refuses.

Stdlib-only; imported host-side by ``subprocess_kernel``.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from fluid.tools.child_env import scrub_child_env

_log = logging.getLogger(__name__)

MODES = ("auto", "required", "off")
DEFAULT_MODE = "auto"
ENV_VAR = "CODE_MODE_OS_SANDBOX"

# Directory holding ``_runner.py`` and the siblings it imports.
_CODEMODE_DIR = os.path.dirname(os.path.abspath(__file__))

# System paths the interpreter and its shared libraries need, read-only. On a
# merged-/usr host the top-level ones are symlinks and are recreated as such.
_SYSTEM_DIRS = ("/usr",)
_SYSTEM_LINK_DIRS = ("/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32")
_SYSTEM_FILES = ("/etc/ld.so.cache", "/etc/localtime")
# Only exposed when the kernel is configured with ``allow_network``.
_NETWORK_FILES = (
    "/etc/resolv.conf", "/etc/hosts", "/etc/nsswitch.conf", "/etc/ssl",
    "/etc/ca-certificates",
)

_SELFTEST_TIMEOUT = 15.0
# Seconds a failed self-test is trusted before the next spawn probes again.
_FAILURE_TTL = 60.0

# bwrap prefixes every message of its own with this; a sandboxed child that
# exits 1 with it before the runner starts was refused by bwrap, not the cell.
BWRAP_ERROR_PREFIX = "bwrap:"

# bwrap's error when the fresh procfs mount is refused. Under Docker that is
# the container's masked/read-only /proc paths (``mount_too_revealing``); the
# seccomp/AppArmor options alone do not lift it.
_PROC_MOUNT_ERROR = "Can't mount proc"
PROC_MOUNT_HINT = (
    "the container's masked /proc paths block a fresh /proc mount; under "
    "Docker also add --security-opt systempaths=unconfined (compose: "
    "security_opt: - systempaths=unconfined)"
)

# Run inside the sandbox by the self-test. It must print ``ok`` and exit 0; it
# fails loudly if a socket can reach off-box, a host file outside the binds is
# visible, the pid namespace is not private, or the runner cannot be read.
_PROBE = r"""
import os, socket, sys
runner, sentinels = sys.argv[1], sys.argv[2:]
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.settimeout(2)
try:
    s.connect(("192.0.2.1", 9))
except OSError:
    pass
else:
    sys.exit("network reachable inside the sandbox")
finally:
    s.close()
for path in sentinels:
    if os.path.lexists(path):
        sys.exit("host path %s visible inside the sandbox" % path)
pids = [p for p in os.listdir("/proc") if p.isdigit()]
if len(pids) > 4:
    sys.exit("pid namespace is not private (%d processes visible)" % len(pids))
if not os.path.isfile(runner):
    sys.exit("kernel runner not visible inside the sandbox")
print("ok")
"""


# Every ``required`` refusal starts with this; the skill store recognises it
# so a refusal is never recorded as a skill's verification failure.
REQUIRED_REFUSAL_MARK = "code_mode_os_sandbox=required, but"


class OsSandboxUnavailable(RuntimeError):
    """``required`` mode and the sandbox cannot be used on this host.

    An infrastructure condition, never a verdict on the code the kernel was
    about to run.
    """


# Unrecognised mode values already warned about: ``plan_spawn`` normalizes on
# every kernel start, and one warning per value is enough.
_warned_modes: set[str] = set()


def normalize_mode(value: object) -> str:
    """``auto``/``required``/``off``; anything unrecognised is ``auto``."""
    text = str(value or "").strip().lower()
    if text in MODES:
        return text
    if text and text not in _warned_modes:
        _warned_modes.add(text)
        _log.warning(
            "code_mode_os_sandbox=%r is not one of %s; using %r",
            value, "/".join(MODES), DEFAULT_MODE,
        )
    return DEFAULT_MODE


def default_mode() -> str:
    """The mode from ``CODE_MODE_OS_SANDBOX``, ``auto`` when unset."""
    return normalize_mode(os.environ.get(ENV_VAR, ""))


def find_bwrap() -> str | None:
    return shutil.which("bwrap")


def sandbox_env() -> dict[str, str]:
    """The whole environment the sandboxed child receives.

    ``HOME`` is deliberately absent: the runner scrubs the home path out of
    tracebacks, and with no home (and no ``/etc/passwd``) there is none to
    scrub.
    """
    return {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "TMPDIR": "/tmp",
    }


def _covered(path: str, roots: Iterable[str]) -> bool:
    return any(path == r or path.startswith(r.rstrip("/") + "/") for r in roots)


def python_roots(python: str) -> list[str]:
    """Directories that must be visible for ``python -I -S`` to start.

    The resolved interpreter's prefix, the running interpreter's base prefixes
    when ``python`` is this interpreter (a venv's ``python`` symlinks to the
    base install and ``-S`` never reads the venv's site-packages), and the
    ``home`` a copied venv's ``pyvenv.cfg`` points at. Paths already under
    ``/usr`` are left to the ``/usr`` bind.
    """
    real = os.path.realpath(python)
    candidates = [os.path.dirname(os.path.dirname(real))]
    if real == os.path.realpath(sys.executable):
        candidates += [sys.base_prefix, sys.base_exec_prefix]
    cfg = os.path.join(os.path.dirname(os.path.dirname(real)), "pyvenv.cfg")
    try:
        with open(cfg, encoding="utf-8") as fh:
            for line in fh:
                key, _, value = line.partition("=")
                if key.strip() == "home" and value.strip():
                    candidates.append(os.path.dirname(value.strip()))
    except OSError:
        pass
    roots: list[str] = []
    for cand in candidates:
        cand = os.path.realpath(cand) if cand else ""
        if not cand or _covered(cand, _SYSTEM_DIRS) or _covered(cand, roots):
            continue
        roots.append(cand)
    return roots


def _too_broad(path: str) -> bool:
    """A root whose bind would expose the host: ``/`` or a home directory."""
    home = os.path.realpath(os.path.expanduser("~"))
    path = os.path.realpath(path)
    if path == "/":
        return True
    return bool(home) and home != "/" and (
        path == home or home.startswith(path.rstrip("/") + "/")
    )


def build_bwrap_argv(
    bwrap: str,
    *,
    ro_paths: Iterable[str],
    allow_network: bool = False,
    disable_userns: bool = True,
    link_dirs: Mapping[str, str] | None = None,
) -> list[str]:
    """The bwrap prefix; the child's own argv is appended by the caller.

    ``ro_paths`` are bound read-only at the same path, after the private
    ``/tmp`` so a runner directory under ``/tmp`` still shows through.
    ``link_dirs`` maps a top-level directory to its symlink target (merged
    ``/usr``); the default reads the host.
    """
    argv = [bwrap, "--unshare-all"]
    if disable_userns:
        argv += ["--unshare-user", "--disable-userns"]
    if allow_network:
        argv.append("--share-net")
    argv += ["--die-with-parent", "--new-session"]
    for d in _SYSTEM_DIRS:
        argv += ["--ro-bind", d, d]
    links = _host_link_dirs() if link_dirs is None else dict(link_dirs)
    for d in _SYSTEM_LINK_DIRS:
        target = links.get(d)
        if target is not None:
            argv += ["--symlink", target, d]
        elif link_dirs is None and os.path.isdir(d):
            argv += ["--ro-bind", d, d]
    extra_files = list(_SYSTEM_FILES) + (list(_NETWORK_FILES) if allow_network else [])
    for f in extra_files:
        argv += ["--ro-bind-try", f, f]
    argv += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"]
    seen: list[str] = list(_SYSTEM_DIRS)
    for p in ro_paths:
        if not p or _covered(p, seen):
            continue
        seen.append(p)
        argv += ["--ro-bind", p, p]
    argv += ["--chdir", "/tmp"]
    return argv


def _host_link_dirs() -> dict[str, str]:
    out: dict[str, str] = {}
    for d in _SYSTEM_LINK_DIRS:
        if os.path.islink(d):
            out[d] = os.readlink(d)
    return out


# ── self-test (once per process per bwrap/python pair) ──────────────────────


@dataclass(frozen=True)
class SelfTest:
    ok: bool
    reason: str = ""
    disable_userns: bool = True


# (bwrap, python) -> (result, monotonic time it was taken)
_cache: dict[tuple[str, str], tuple[SelfTest, float]] = {}
_cache_lock = threading.Lock()


def _supports_disable_userns(bwrap: str) -> bool:
    try:
        out = subprocess.run(
            [bwrap, "--help"], capture_output=True, text=True, timeout=5,
            env=scrub_child_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return "--disable-userns" in (out.stdout + out.stderr)


def _sentinels(bound: list[str]) -> list[str]:
    """Host paths that exist but must not be visible inside the sandbox.

    ``/etc/passwd`` and one entry of the home directory that is not on the way
    to a bound path (binding a directory under ``~`` creates its empty parent
    directories inside the sandbox, so ``~`` itself may legitimately exist).
    """
    out = [p for p in ("/etc/passwd",) if os.path.lexists(p)]
    home = os.path.realpath(os.path.expanduser("~"))
    if home and home != "/":
        try:
            names = sorted(os.listdir(home))
        except OSError:
            names = []
        for name in names:
            path = os.path.join(home, name)
            if not any(_covered(b, [path]) or _covered(path, [b]) for b in bound):
                out.append(path)
                break
    return out


def _probe_once(bwrap: str, python: str, disable_userns: bool) -> SelfTest:
    runner = os.path.join(_CODEMODE_DIR, "_runner.py")
    bound = [*python_roots(python), _CODEMODE_DIR]
    argv = build_bwrap_argv(
        bwrap, ro_paths=bound, disable_userns=disable_userns,
    ) + [
        os.path.realpath(python), "-I", "-S", "-c", _PROBE,
        runner, *_sentinels(bound),
    ]
    try:
        out = subprocess.run(
            argv, capture_output=True, text=True, env=sandbox_env(),
            timeout=_SELFTEST_TIMEOUT, stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return SelfTest(False, f"self-test timed out after {_SELFTEST_TIMEOUT:.0f}s", disable_userns)
    except OSError as exc:
        return SelfTest(False, f"could not run bwrap: {exc}", disable_userns)
    if out.returncode == 0 and out.stdout.strip() == "ok":
        return SelfTest(True, "", disable_userns)
    detail = (out.stderr or out.stdout).strip().splitlines()
    tail = " | ".join(line.strip() for line in detail[-3:]) or f"exit status {out.returncode}"
    return SelfTest(False, explain(tail), disable_userns)


def explain(reason: str) -> str:
    """``reason`` plus the missing container option when bwrap names one."""
    if _PROC_MOUNT_ERROR in reason and PROC_MOUNT_HINT not in reason:
        return f"{reason} ({PROC_MOUNT_HINT})"
    return reason


def self_test(python: str, bwrap: str | None = None) -> SelfTest:
    """Whether the sandbox works here.

    A pass is cached for the life of the process (until :func:`invalidate`);
    a failure for ``_FAILURE_TTL`` seconds, since its cause (namespace
    exhaustion, a timeout under load, a host setting) can go away.
    """
    bwrap = bwrap or find_bwrap()
    if not bwrap:
        return SelfTest(False, "bwrap (bubblewrap) is not installed or not on PATH")
    key = (bwrap, os.path.realpath(python))
    with _cache_lock:
        cached = _cache.get(key)
        if cached is not None:
            result, taken = cached
            if result.ok or time.monotonic() - taken < _FAILURE_TTL:
                return result
        if any(_too_broad(r) for r in python_roots(python)):
            result = SelfTest(False, "the Python installation prefix is / or a home directory; binding it would expose the host")
        else:
            with_flag = _supports_disable_userns(bwrap)
            result = _probe_once(bwrap, python, with_flag)
            if not result.ok and with_flag:
                # Older kernels/bwrap builds refuse --unshare-user in some
                # setups (setuid bwrap); the rest of the sandbox still holds.
                retry = _probe_once(bwrap, python, False)
                if retry.ok:
                    result = retry
        _cache[key] = (result, time.monotonic())
        return result


def invalidate(python: str, bwrap: str | None = None) -> None:
    """Drop the cached self-test for this pair; the next spawn probes again."""
    bwrap = bwrap or find_bwrap()
    if not bwrap:
        return
    with _cache_lock:
        _cache.pop((bwrap, os.path.realpath(python)), None)


def cached_self_test(python: str, bwrap: str | None = None) -> SelfTest | None:
    """The last self-test result for this bwrap/python pair, from cache only.

    None when no self-test has run yet for the pair (or it is still running).
    A failure is returned even after ``_FAILURE_TTL`` has passed: it is still
    the last reason this host gave, which is what a status display wants.
    Never probes and never waits on ``_cache_lock`` (see :func:`is_active`).
    Without ``bwrap`` on ``PATH`` it returns the same failure :func:`self_test`
    would, since that result is never cached.
    """
    bwrap = bwrap or find_bwrap()
    if not bwrap:
        return SelfTest(False, "bwrap (bubblewrap) is not installed or not on PATH")
    cached = _cache.get((bwrap, os.path.realpath(python)))
    return cached[0] if cached is not None else None


def is_active(mode: str, python: str, *, allow_fs: bool = False) -> bool:
    """Whether a kernel spawned now would run under the sandbox, from cache only.

    True when ``mode`` is ``auto``/``required``, the host is Linux, ``auto``
    was not handed ``allow_fs`` (which runs the plain child), and a passing
    self-test for this bwrap/python pair is cached. Never runs the self-test
    (see :func:`prewarm`), so it is cheap enough for a per-call permission
    check; an unknown or failed self-test reads as inactive.

    It never waits on ``_cache_lock``: :func:`self_test` holds that lock for
    the whole probe (up to two ``_SELFTEST_TIMEOUT`` runs), and this is called
    synchronously on the event loop. A single dict read is atomic, and while a
    probe runs the entry is either absent or a stale failure (a pass is never
    re-probed), so an in-progress self-test reads as inactive.
    """
    mode = normalize_mode(mode)
    if mode == "off" or sys.platform != "linux":
        return False
    if mode == "auto" and allow_fs:
        return False
    bwrap = find_bwrap()
    if not bwrap:
        return False
    cached = _cache.get((bwrap, os.path.realpath(python)))
    return cached is not None and cached[0].ok


def prewarm(mode: str, python: str) -> threading.Thread | None:
    """Run the self-test in a daemon thread so :func:`is_active` has an answer
    before the first kernel spawn. None when there is nothing to probe."""
    mode = normalize_mode(mode)
    if mode == "off" or sys.platform != "linux" or not find_bwrap():
        return None

    def _run() -> None:
        try:
            self_test(python)
        except Exception:  # best effort; the first spawn probes again
            _log.debug("code-mode OS sandbox prewarm failed", exc_info=True)

    thread = threading.Thread(target=_run, name="os-sandbox-prewarm", daemon=True)
    thread.start()
    return thread


def reset_cache() -> None:
    """Forget the self-test results (tests)."""
    with _cache_lock:
        _cache.clear()
        _warned.clear()


# ── the spawn plan ───────────────────────────────────────────────────────────


@dataclass
class SpawnPlan:
    argv: list[str]
    env: dict[str, str] | None
    sandboxed: bool
    reason: str = ""


_warned: set[str] = set()


def _warn_once(reason: str) -> None:
    if reason in _warned:
        return
    _warned.add(reason)
    _log.warning(
        "code-mode OS sandbox unavailable, kernel child runs without it: %s", reason
    )


def _required_refusal(reason: str) -> OsSandboxUnavailable:
    return OsSandboxUnavailable(
        f"{REQUIRED_REFUSAL_MARK} the bubblewrap sandbox is "
        f"unavailable on this host ({reason}); the code-mode "
        "kernel was not started. Install bubblewrap and allow "
        "unprivileged user namespaces, or set code_mode_os_sandbox "
        "to auto/off."
    )


def is_bwrap_refusal(returncode: int | None, stderr: str) -> bool:
    """Whether a sandboxed child that died before its first frame was refused
    by bwrap itself (exit status 1 and bwrap's own message prefix)."""
    if returncode != 1:
        return False
    first = next((ln.strip() for ln in stderr.splitlines() if ln.strip()), "")
    return first.startswith(BWRAP_ERROR_PREFIX)


def plan_after_refusal(
    mode: str,
    *,
    python: str,
    runner: str,
    base_env: Mapping[str, str],
    reason: str,
) -> SpawnPlan:
    """The plain plan to use after bwrap refused a spawn the self-test passed.

    The cached self-test is dropped either way, so the next spawn probes
    again. ``auto`` logs the reason once and returns the unsandboxed plan;
    ``required`` raises :class:`OsSandboxUnavailable` with it.
    """
    invalidate(python)
    reason = explain(reason)
    if normalize_mode(mode) == "required":
        raise _required_refusal(reason)
    _warn_once(reason)
    return SpawnPlan([python, "-I", "-S", runner], dict(base_env), False, reason)


def plan_spawn(
    mode: str,
    *,
    python: str,
    runner: str,
    base_env: Mapping[str, str],
    allow_network: bool = False,
    allow_fs: bool = False,
    tester=None,
) -> SpawnPlan:
    """The argv and environment to spawn the kernel child with.

    ``base_env`` is the environment the unsandboxed child gets. ``tester`` is
    the self-test callable (``python -> SelfTest``), injectable for tests.
    Raises :class:`OsSandboxUnavailable` in ``required`` mode when the sandbox
    cannot be used.
    """
    mode = normalize_mode(mode)
    plain = [python, "-I", "-S", runner]
    if mode == "off" or sys.platform != "linux":
        if mode == "required":
            raise OsSandboxUnavailable(
                f"{REQUIRED_REFUSAL_MARK} the bubblewrap sandbox is "
                "Linux-only; the code-mode kernel was not started"
            )
        return SpawnPlan(plain, dict(base_env), False, "disabled")
    if mode == "auto" and allow_fs:
        return SpawnPlan(plain, dict(base_env), False, "allow_fs grants host file access")
    result = (tester or self_test)(python)
    if not result.ok:
        if mode == "required":
            raise _required_refusal(result.reason)
        _warn_once(result.reason)
        return SpawnPlan(plain, dict(base_env), False, result.reason)
    runner_dir = os.path.dirname(os.path.abspath(runner))
    prefix = build_bwrap_argv(
        find_bwrap() or "bwrap",
        ro_paths=[*python_roots(python), runner_dir, _CODEMODE_DIR],
        allow_network=allow_network,
        disable_userns=result.disable_userns,
    )
    argv = prefix + [os.path.realpath(python), "-I", "-S", runner]
    return SpawnPlan(argv, sandbox_env(), True)
