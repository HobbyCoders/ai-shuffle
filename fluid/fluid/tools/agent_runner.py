"""The narrow spawn runner for agent processes under Docker agent isolation.

With ``AGENT_ISOLATION=on`` the container supervisor keeps the install's keys,
database descriptor and PostgreSQL files under a separate service account and
starts the app as PUID with that account only in its *saved* user id (see
``docker/agent_isolation.py``). Every process the app starts for an agent (a
Bash command, a Monitor shell, a chat's MCP server, a hook, a delegated CLI, a
terminal) goes through this runner first:

    python -I -S agent_runner.py --root DIR [--root DIR ...] -- COMMAND...

Before it executes ``COMMAND`` the runner refuses an environment that carries
the app's own secrets and a working directory outside the approved roots,
drops supplementary groups, sets real, effective and saved ids to the real
user and group (the agent account, PUID/PGID), sets ``no_new_privs`` and closes
every inherited descriptor above stderr. Any check that fails stops the
command with exit status 126; nothing is run half-restricted.

The script part is stdlib only and runs with ``-I -S``, so nothing in the
working directory, the user site or ``PYTHON*`` variables is loaded. The
importable part, :func:`wrap`, is what spawn sites call: it returns its
arguments unchanged unless the app runs with ``SHUFFLE_AGENT_ISOLATION=on``.
"""

from __future__ import annotations

import os
import re
import sys

FLAG = "SHUFFLE_AGENT_ISOLATION"
ROOTS = "SHUFFLE_AGENT_ROOTS"
REFUSED = 126
RUNNER = os.path.abspath(__file__)

# Kept in step with fluid.tools.child_env.is_app_secret (a test compares them):
# the runner runs with -I -S and imports nothing outside the stdlib.
_APP_SECRET_NAMES = frozenset({
    "SHUFFLE_DESKTOP_LIFECYCLE_TOKEN", "SHUFFLE_DESKTOP_ATTACH_TOKEN", "SHUFFLE_DESKTOP_LISTENER_TOKEN",
    "OWNER_PASSWORD", "LICENSE_KEY",
    "ADMIN_PASSWORD", "ADMIN_USERNAME", "SECRET_KEY", "CLUSTER_SECRET",
    "DATABASE_URL", "DSN", "JWT_SECRET", "SESSION_SECRET", "AUTH_SECRET",
})
_APP_SECRET_PREFIX = re.compile(r"^(?:PG(?!ID$)|POSTGRES_|DATABASE_)", re.IGNORECASE)

PR_SET_NO_NEW_PRIVS = 38
PR_GET_NO_NEW_PRIVS = 39


def is_app_secret(name: str) -> bool:
    upper = name.upper()
    return upper in _APP_SECRET_NAMES or upper.endswith("_DSN") or bool(_APP_SECRET_PREFIX.match(upper))


def _internal(name: str) -> bool:
    return name.upper().startswith("SHUFFLE_AGENT_")


# --- Spawn sites -----------------------------------------------------------


def enabled(environ=None) -> bool:
    return (os.environ if environ is None else environ).get(FLAG) == "on"


def wrap(argv, cwd=None, env=None, *, environ=None):
    """``(argv, cwd, env)`` for an agent child.

    Off (the default) returns the same three objects untouched. On, ``argv``
    is prefixed with the runner, ``env`` (the inherited environment when
    ``None``) loses any app secret and the isolation markers, and a missing
    ``cwd`` becomes the agent's home, since the app's own directory is not an
    approved root.
    """
    environ = os.environ if environ is None else environ
    if environ.get(FLAG) != "on":
        return argv, cwd, env
    source = environ if env is None else env
    dropped = sorted(k for k in source if is_app_secret(k))
    if dropped:
        import logging

        logging.getLogger(__name__).warning(
            "agent isolation: not passing %s to an agent process", ", ".join(dropped))
    env = {k: v for k, v in source.items() if not is_app_secret(k) and not _internal(k)}
    argv = [os.fspath(a) for a in argv]
    import shutil

    if not argv or ("/" not in argv[0] and shutil.which(argv[0], path=env.get("PATH", os.defpath)) is None):
        # What a direct spawn raises, so "command not installed" handling
        # keeps working instead of seeing the runner's exit status.
        import errno

        name = argv[0] if argv else ""
        raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), name)
    if cwd is None:
        import pwd

        cwd = pwd.getpwuid(os.getuid()).pw_dir
    prefix = [sys.executable, "-I", "-S", RUNNER]
    for root in (environ.get(ROOTS) or "").split(":"):
        if root:
            prefix += ["--root", root]
    return [*prefix, "--", *argv], cwd, env


def strip(argv):
    """The command a runner invocation executes, or ``argv`` unchanged.

    The runner replaces itself with the command, so a process recorded while
    it was still the runner must compare equal to the command afterwards.
    """
    parts = list(argv)
    for index, part in enumerate(parts[:4]):
        if os.path.basename(str(part)) == "agent_runner.py":
            try:
                return parts[parts.index("--", index) + 1:]
            except ValueError:
                return parts
    return parts


# --- The runner --------------------------------------------------------------


class Refused(Exception):
    pass


def _parse(args):
    roots, index = [], 0
    while index < len(args) and args[index] != "--":
        if args[index] != "--root" or index + 1 >= len(args):
            raise Refused(f"unexpected argument {args[index]!r}")
        roots.append(args[index + 1])
        index += 2
    command = args[index + 1:]
    if not roots:
        raise Refused("no approved working directories")
    if not command:
        raise Refused("no command")
    return roots, command


def check_env(environ):
    secrets = sorted(k for k in environ if is_app_secret(k))
    if secrets:
        raise Refused("the environment carries " + ", ".join(secrets))
    return {k: v for k, v in environ.items() if not _internal(k)}


def check_cwd(roots, cwd=None):
    cwd = os.path.realpath(os.getcwd() if cwd is None else cwd)
    for root in roots:
        if not os.path.isabs(root):
            continue
        root = os.path.realpath(root)
        if cwd == root or cwd.startswith(root.rstrip("/") + "/"):
            return cwd
    raise Refused(f"working directory {cwd} is outside the approved roots")


def drop_ids():
    """Real, effective and saved ids all the real user's; no extra groups."""
    uid, gid = os.getuid(), os.getgid()
    if uid == 0:
        raise Refused("will not run an agent process as root")
    try:
        os.setgroups([])
    except PermissionError:
        pass  # unprivileged: the list can only be checked
    if set(os.getgroups()) - {gid}:
        raise Refused("supplementary groups remain: " + ",".join(map(str, os.getgroups())))
    os.setresgid(gid, gid, gid)
    os.setresuid(uid, uid, uid)
    if os.getresuid() != (uid, uid, uid) or os.getresgid() != (gid, gid, gid):
        raise Refused("could not settle user and group ids")


def no_new_privs():
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        raise Refused("prctl(PR_SET_NO_NEW_PRIVS) failed: " + os.strerror(ctypes.get_errno()))
    if libc.prctl(PR_GET_NO_NEW_PRIVS, 0, 0, 0, 0) != 1:
        raise Refused("no_new_privs is not set")


def close_fds():
    try:
        limit = os.sysconf("SC_OPEN_MAX")
    except (ValueError, OSError):
        limit = 65536
    os.closerange(3, max(limit, 3))


def main(args=None, environ=None):
    args = sys.argv[1:] if args is None else args
    try:
        if not sys.platform.startswith("linux"):
            raise Refused("agent isolation runs on Linux only")
        roots, command = _parse(args)
        env = check_env(os.environ if environ is None else environ)
        check_cwd(roots)
        drop_ids()
        no_new_privs()
        close_fds()
    except (Refused, OSError) as exc:
        sys.stderr.write(f"agent runner refused: {exc}\n")
        return REFUSED
    try:
        os.execvpe(command[0], command, env)
    except OSError as exc:
        sys.stderr.write(f"agent runner: {command[0]}: {exc.strerror or exc}\n")
        return 127
    return 127  # pragma: no cover - execvpe does not return


if __name__ == "__main__":
    sys.exit(main())
