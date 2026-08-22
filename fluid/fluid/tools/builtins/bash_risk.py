"""Read-only classifier for Bash commands.

The Bash tool's static risk is ``write`` — running an arbitrary shell command
can do anything. But most of the commands a model actually runs are looks, not
changes: ``git status --short``, ``ls``, ``grep``, ``wc -l``. Prompting for each
of those under the default ``on_write`` policy (or denying them outright in a
non-interactive ``fluid run``) makes the tool nearly useless. This module is the
per-input override: :func:`classify` returns ``"safe"`` for a command it can
prove is read-only and ``"write"`` for everything else.

The classifier is deliberately conservative. It parses the command with
``shlex`` (POSIX rules, the Git Bash dialect used on Windows) and splits it on
``&&``, ``||``, ``;`` and ``|`` into simple commands. The whole command is safe
only when:

- every simple command's program is in :data:`READ_ONLY_PROGRAMS` and passes
  that program's extra check (if any);
- there is no output redirection (``>``, ``>>``, ``2>``, ``>&``, ``<(``);
  ``2>&1`` alone is tolerated because it merges streams without touching a file;
- there is no command substitution (``$(`` or backticks), no background ``&``,
  no subshell parentheses, and no program outside the table (so ``sudo``,
  ``xargs``, ``env cmd``, ``python`` and friends are all ``write``).

Programs with extra rules:

- ``find``: none of ``-exec``, ``-execdir``, ``-ok``, ``-okdir``, ``-delete``,
  ``-fprint*``, ``-fls``.
- ``awk``/``gawk``/``mawk``: no ``system(``, ``getline`` or ``|`` in any
  argument (those run or write to commands).
- ``sed``: must carry ``-n``/``--quiet``/``--silent``, must not carry ``-i`` or
  ``--in-place``, and no script argument that looks like a ``w`` file-write
  command.
- ``sort``: no ``-o``/``--output``. ``uniq``: at most one positional argument
  (the second is an output file). ``date``: no ``-s``/``--set``. ``env``: no
  arguments (``env cmd`` runs ``cmd``). ``rg``: no ``--pre`` (runs a command).
- ``git``: read-only subcommands only, listed in :data:`GIT_READ_ONLY` with
  their per-subcommand argument constraints; ``-C <dir>`` and ``--no-pager``
  global options are accepted, ``-c key=value`` is not (it can point git at an
  external program); any ``--output*`` argument is rejected.
- ``uv``/``pip``/``npm``/``pnpm``/``yarn``/``cargo``: only the informational
  subcommands in :data:`PACKAGE_MANAGER_READ_ONLY` (``uv pip list`` is handled
  by skipping the ``pip`` word).
- ``go``: only ``go version``. ``node``: only ``--version``/``-v``.

Everything is table-driven so the allowlist can grow without touching the
parser. Program names are matched on the basename (``/usr/bin/git``,
``C:\\Program Files\\Git\\bin\\git.exe`` both count as ``git``), case-insensitive,
with a trailing ``.exe`` stripped.
"""

from __future__ import annotations

import re
import shlex
from typing import Any, Callable

SAFE = "safe"
WRITE = "write"

Check = Callable[[list[str]], bool]  # args after the program → ok?

# Separators between simple commands. Anything else made purely of shell
# punctuation (``&``, ``|&``, ``(``, ``)``, ``>`` ...) is a reason to refuse.
_SEPARATORS = {"&&", "||", ";", "|", ";;"}

_ENV_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_SED_WRITE_CMD = re.compile(r"(?<![A-Za-z\\])[wW]\s*[\w./\\~-]")
_FIND_EXEC_FLAGS = {"-exec", "-execdir", "-ok", "-okdir", "-delete", "-fls"}


def _no_flags(*banned: str) -> Check:
    return lambda args: not any(a in banned or a.split("=", 1)[0] in banned for a in args)


def _find_ok(args: list[str]) -> bool:
    return not any(a in _FIND_EXEC_FLAGS or a.startswith("-fprint") for a in args)


def _awk_ok(args: list[str]) -> bool:
    return not any("system(" in a or "getline" in a or "|" in a for a in args)


def _sed_ok(args: list[str]) -> bool:
    quiet = False
    for a in args:
        if a in ("--quiet", "--silent"):
            quiet = True
        elif a.startswith("--in-place"):
            return False
        elif a.startswith("-") and not a.startswith("--"):
            flags = a[1:]
            if "i" in flags:
                return False
            if "n" in flags:
                quiet = True
    if not quiet:
        return False
    scripts = [a for a in args if not a.startswith("-")]
    return not any(_SED_WRITE_CMD.search(s) for s in scripts)


def _uniq_ok(args: list[str]) -> bool:
    return len([a for a in args if not a.startswith("-")]) <= 1


def _env_ok(args: list[str]) -> bool:
    return not args or args == ["--help"]


def _exact(*allowed: tuple[str, ...]) -> Check:
    """Allow only when the argument list equals one of the given tuples."""
    options = {tuple(a) for a in allowed}
    return lambda args: tuple(args) in options


# Program → extra check (None means the program is fine with any arguments,
# subject to the global redirection/substitution rules).
READ_ONLY_PROGRAMS: dict[str, Check | None] = {
    "ls": None, "dir": None, "cat": None, "head": None, "tail": None,
    "less": None, "more": None, "wc": None,
    "grep": None, "egrep": None, "fgrep": None, "rg": _no_flags("--pre"),
    "find": _find_ok, "file": None, "stat": None, "du": None, "df": None,
    "pwd": None, "echo": None, "printf": None, "true": None,
    "which": None, "where": None, "type": None,
    "env": _env_ok, "printenv": None,
    "date": _no_flags("-s", "--set"), "whoami": None, "uname": None, "tree": None,
    "diff": None, "cmp": None,
    "sort": _no_flags("-o", "--output"), "uniq": _uniq_ok, "cut": None, "tr": None,
    "awk": _awk_ok, "gawk": _awk_ok, "mawk": _awk_ok,
    "sed": _sed_ok,
    "basename": None, "dirname": None, "realpath": None, "readlink": None,
    "jq": None,
}

# git subcommand → check over the subcommand's own arguments.
GIT_READ_ONLY: dict[str, Check | None] = {
    "status": None, "log": None, "diff": None, "show": None,
    "rev-parse": None, "ls-files": None, "blame": None, "describe": None,
    "branch": lambda args: all(
        a in ("-a", "-r", "--list", "-l", "--show-current", "-v", "-vv") for a in args
    ),
    "remote": lambda args: args in ([], ["-v"], ["--verbose"]),
    "stash": lambda args: args == ["list"],
    "tag": lambda args: all(a in ("-l", "--list", "-n") or a.startswith("-n") for a in args),
    "config": lambda args: bool(args) and args[0] in (
        "--get", "--get-all", "--get-regexp", "--list", "-l",
    ),
}

PACKAGE_MANAGER_READ_ONLY = {
    "--version", "-V", "--help", "-h", "list", "tree", "outdated", "why", "info", "view",
}


def _git_ok(args: list[str]) -> bool:
    if any(a.startswith("--output") for a in args):
        return False
    i = 0
    while i < len(args):
        a = args[i]
        if a == "-C" and i + 1 < len(args):
            i += 2
            continue
        if a.startswith("-C") and len(a) > 2:
            i += 1
            continue
        if a == "--no-pager":
            i += 1
            continue
        break
    rest = args[i:]
    if not rest:
        return False
    check = GIT_READ_ONLY.get(rest[0], False)
    if check is False:
        return False
    return check is None or check(rest[1:])


def _package_manager_ok(args: list[str]) -> bool:
    if args and args[0] == "pip":  # uv pip list
        args = args[1:]
    return bool(args) and args[0] in PACKAGE_MANAGER_READ_ONLY


READ_ONLY_PROGRAMS.update({
    "git": _git_ok,
    "uv": _package_manager_ok, "pip": _package_manager_ok, "pip3": _package_manager_ok,
    "npm": _package_manager_ok, "pnpm": _package_manager_ok, "yarn": _package_manager_ok,
    "cargo": _package_manager_ok,
    "go": _exact(("version",)),
    "node": _exact(("--version",), ("-v",)),
})


def _program_name(token: str) -> str:
    name = re.split(r"[\\/]", token)[-1].lower()
    return name[:-4] if name.endswith(".exe") else name


def _lex(line: str) -> list[str] | None:
    lex = shlex.shlex(line, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    try:
        return list(lex)
    except ValueError:  # unbalanced quotes
        return None


def _simple_command_ok(tokens: list[str]) -> bool:
    # Leading VAR=value assignments are fine; a bare assignment is not a command.
    while tokens and _ENV_ASSIGNMENT.match(tokens[0]):
        tokens = tokens[1:]
    if not tokens:
        return False
    program = _program_name(tokens[0])
    if program not in READ_ONLY_PROGRAMS:
        return False
    check = READ_ONLY_PROGRAMS[program]
    return check is None or check(tokens[1:])


def is_read_only(command: str) -> bool:
    """True when every part of ``command`` is provably read-only."""
    if not command or not command.strip():
        return False
    if "$(" in command or "`" in command:
        return False
    for line in command.splitlines():
        line = re.sub(r"(?<!\S)2>&1(?!\S)", " ", line)
        if not line.strip():
            continue
        tokens = _lex(line)
        if tokens is None:
            return False
        current: list[str] = []
        groups: list[list[str]] = []
        for tok in tokens:
            if tok in _SEPARATORS:
                groups.append(current)
                current = []
                continue
            if ">" in tok or "<(" in tok:
                return False
            if tok and all(ch in "();<>|&" for ch in tok):
                return False  # background &, subshells, |& and friends
            current.append(tok)
        groups.append(current)
        if not all(_simple_command_ok(g) for g in groups):
            return False
    return True


def program_names(command: str) -> frozenset[str] | None:
    """Every program a command invokes (``cat a | grep b`` → {cat, grep}).

    None when the command cannot be lexed or uses substitution/subshells,
    so callers fall back to the broadest scope rather than a wrong one.
    """
    if not command or not command.strip() or "$(" in command or "`" in command:
        return None
    names: set[str] = set()
    for line in command.splitlines():
        if not line.strip():
            continue
        tokens = _lex(line)
        if tokens is None:
            return None
        expect_program = True
        for tok in tokens:
            if tok in _SEPARATORS:
                expect_program = True
                continue
            if tok and all(ch in "();<>|&" for ch in tok):
                return None
            if expect_program and not _ENV_ASSIGNMENT.match(tok):
                names.add(_program_name(tok))
                expect_program = False
    return frozenset(names) or None


def session_scope(input: dict[str, Any]) -> frozenset[str] | None:
    """``allow_always`` scope for Bash: the programs the command runs."""
    command = input.get("command")
    return program_names(command) if isinstance(command, str) else None


def classify(input: dict[str, Any]) -> str:
    """Effective risk of one Bash tool input: ``safe`` or ``write``."""
    command = input.get("command")
    if not isinstance(command, str):
        return WRITE
    return SAFE if is_read_only(command) else WRITE
