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
  ``xargs``, ``env cmd``, ``python`` and friends are all ``write``);
- any leading ``VAR=value`` assignment names a variable in
  :data:`INERT_ENV_VARS`. Locale and colour variables cannot change what a
  program does; almost anything else can (``LD_PRELOAD``, ``GIT_EXTERNAL_DIFF``,
  ``GIT_PAGER``, ``BASH_ENV``, ``LESSOPEN`` all run a program of the caller's
  choosing), so an assignment outside the list makes the command ``write``.

A read-only command whose argument names a credential file (``cat .env``) is
still ``write``; see :func:`_touches_sensitive_path`. An unquoted glob argument
counts when any file it could expand to is a credential file (``cat .env*``,
``cat ?env``, ``cat ~/.ssh/*``). A process's environment
(``/proc/<pid>/environ``) is one of those files, and a recursive search
(``grep -r``, ``rg``) rooted at ``/``, ``/proc`` or a process directory is
``write`` too, since it reads that file without naming it.

Programs with extra rules:

- ``find``: none of ``-exec``, ``-execdir``, ``-ok``, ``-okdir``, ``-delete``,
  ``-fprint*``, ``-fls``.
- ``awk``/``gawk``/``mawk``: no ``system(``, ``getline`` or ``|`` in any
  argument (those run or write to commands), and no ``-f``/``--file``,
  ``-i``/``--include`` or ``-l``/``--load`` (script files and extension
  libraries the classifier cannot read).
- ``sed``: must carry ``-n``/``--quiet``/``--silent``, must not carry ``-i`` or
  ``--in-place`` or ``-f``/``--file`` (an unread script file), and no script
  argument that looks like a ``w`` file-write command or a GNU ``e`` command
  or ``s///e`` flag (both run the text as a shell command).
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
from collections.abc import Callable
from typing import Any

SAFE = "safe"
WRITE = "write"

Check = Callable[[list[str]], bool]  # args after the program → ok?

# Separators between simple commands. Anything else made purely of shell
# punctuation (``&``, ``|&``, ``(``, ``)``, ``>`` ...) is a reason to refuse.
_SEPARATORS = {"&&", "||", ";", "|", ";;"}

_ENV_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_SED_WRITE_CMD = re.compile(r"(?<![A-Za-z\\])[wW]\s*[\w./\\~-]")
# GNU sed ``e``: a standalone command (``1e cmd``) or an ``s`` flag
# (``s/a/b/e``, ``s/a/b/ge``). Address regexes (``/error/p``) are stripped
# before this runs so a word that merely contains ``e`` does not trip it;
# an ``e`` reached through a run of other ``s`` flags still does.
_SED_EXEC_CMD = re.compile(r"(?<![A-Za-z\\])[gpiImM0-9]*[eE]")
_SED_ADDRESS_RE = re.compile(r"/[^/]*/")
_FIND_EXEC_FLAGS = {"-exec", "-execdir", "-ok", "-okdir", "-delete", "-fls"}
_AWK_FILE_FLAGS = {"-f", "--file", "-i", "--include", "-l", "--load"}

# Variables a leading ``VAR=value`` may set without changing the command's
# class. Everything here only affects locale, time zone or colour output.
INERT_ENV_VARS = frozenset({
    "LANG", "LANGUAGE", "LC_ALL", "LC_COLLATE", "LC_CTYPE", "LC_MESSAGES",
    "LC_MONETARY", "LC_NUMERIC", "LC_TIME", "TZ", "TERM", "COLUMNS", "LINES",
    "NO_COLOR", "FORCE_COLOR", "CLICOLOR", "CLICOLOR_FORCE",
})


def _inert_assignment(token: str) -> bool:
    m = _ENV_ASSIGNMENT.match(token)
    return bool(m) and token[: m.end() - 1] in INERT_ENV_VARS


def _no_flags(*banned: str) -> Check:
    return lambda args: not any(a in banned or a.split("=", 1)[0] in banned for a in args)


def _find_ok(args: list[str]) -> bool:
    return not any(a in _FIND_EXEC_FLAGS or a.startswith("-fprint") for a in args)


def _awk_ok(args: list[str]) -> bool:
    for a in args:
        if "system(" in a or "getline" in a or "|" in a:
            return False
        # -f prog.awk, -fprog.awk, --file=prog.awk: a script we never see.
        if a.split("=", 1)[0] in _AWK_FILE_FLAGS or (
            a.startswith(("-f", "-i", "-l")) and not a.startswith("--") and len(a) > 2
        ):
            return False
    return True


def _sed_ok(args: list[str]) -> bool:
    quiet = False
    scripts: list[str] = []
    for a in args:
        if a in ("--quiet", "--silent"):
            quiet = True
        elif a.startswith(("--in-place", "--file")):
            return False
        elif a.startswith("--expression="):
            scripts.append(a.split("=", 1)[1])
        elif a.startswith("-") and not a.startswith("--") and len(a) > 1:
            flags = a[1:]
            if "i" in flags or "f" in flags:
                return False
            if "n" in flags:
                quiet = True
        else:
            scripts.append(a)
    if not quiet:
        return False
    return not any(_sed_script_runs_or_writes(s) for s in scripts)


def _sed_script_runs_or_writes(script: str) -> bool:
    if _SED_WRITE_CMD.search(script):
        return True
    return bool(_SED_EXEC_CMD.search(_SED_ADDRESS_RE.sub("", script)))


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
    "more": None, "wc": None,  # not less: LESSOPEN/LESSCLOSE run commands
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
    return name.removesuffix(".exe")


def _lex(line: str) -> list[str] | None:
    lex = shlex.shlex(line, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    try:
        return list(lex)
    except ValueError:  # unbalanced quotes
        return None


def _simple_command_ok(tokens: list[str]) -> bool:
    # Leading VAR=value assignments pass only for inert variables; a bare
    # assignment is not a command.
    while tokens and _ENV_ASSIGNMENT.match(tokens[0]):
        if not _inert_assignment(tokens[0]):
            return False
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


def simple_commands(command: str) -> list[str] | None:
    """The simple commands of a command line, quoting kept, or None.

    ``git commit -m "a b" && rm -rf /`` -> ``['git commit -m "a b"', 'rm -rf /']``.
    Splits on ``&&``, ``||``, ``;``, ``|``, ``|&``, a background ``&`` and
    newlines. None when the line uses command substitution, a subshell or
    process substitution, or cannot be lexed: the pieces it would run are not
    visible in the text, so a caller matching text against a policy must not
    treat the split as complete.
    """
    if not command or not command.strip() or "$(" in command or "`" in command:
        return None
    out: list[str] = []
    for line in command.splitlines():
        if not line.strip():
            continue
        lex = shlex.shlex(line, posix=False, punctuation_chars=True)
        lex.whitespace_split = True
        try:
            tokens = list(lex)
        except ValueError:
            return None
        current: list[str] = []
        for tok in tokens:
            if tok in _SEPARATORS or tok in ("&", "|&"):
                if current:
                    out.append(" ".join(current))
                current = []
                continue
            if tok and all(ch in "();<>|&" for ch in tok) and not set(tok) <= set("<>&"):
                return None  # subshell parentheses, unknown operator soup
            current.append(tok)
        if current:
            out.append(" ".join(current))
    return out or None


# Programs that run another program named on their command line. Approving
# ``sudo apt list`` once must not cover ``sudo rm -rf /``, so the label is the
# wrapper plus what it wraps: ``sudo apt``.
WRAPPER_PROGRAMS = frozenset((
    "sudo", "doas", "env", "xargs", "nohup", "nice", "ionice", "time", "timeout",
    "command", "exec", "busybox", "stdbuf", "unbuffer", "watch", "chronic",
))
# Programs whose first argument selects what they do. ``npm run`` and
# ``npm install`` are different programs for approval purposes, as are
# ``make build`` and ``make clean`` and ``git commit`` and ``git push``.
SUBCOMMAND_PROGRAMS = frozenset((
    "git", "npm", "npx", "pnpm", "yarn", "bun", "bunx", "uv", "uvx", "pip", "pip3",
    "pipx", "poetry", "cargo", "go", "make", "docker", "podman", "kubectl",
    "gh", "gcloud", "aws", "az", "terraform", "helm", "apt", "apt-get", "brew",
    "dnf", "yum", "systemctl", "conda", "dotnet",
))
# Subcommands that take a second verb: ``git stash list`` and ``git stash
# drop``, ``gh pr view`` and ``gh pr merge`` are different approvals. ``*``
# means every subcommand of the program does. ``run`` names a script (``npm run
# build``, ``uv run tool.py``), so it carries that too.
NESTED_SUBCOMMANDS: dict[str, frozenset[str]] = {
    "git": frozenset(("stash", "remote", "worktree", "submodule", "reflog", "notes", "lfs")),
    "docker": frozenset((
        "compose", "container", "image", "volume", "network", "system", "buildx",
        "builder", "context", "swarm", "stack", "service",
    )),
    "kubectl": frozenset(("config", "rollout")),
    "gh": frozenset("*"), "aws": frozenset("*"), "gcloud": frozenset("*"), "az": frozenset("*"),
    **{pm: frozenset(("run",)) for pm in ("npm", "pnpm", "yarn", "bun", "uv", "poetry")},
}
NESTED_SUBCOMMANDS["podman"] = NESTED_SUBCOMMANDS["docker"]
# Global options that take a separate value before the subcommand
# (``git -C repo commit``, ``kubectl -n ns delete``).
_GLOBAL_VALUE_OPTIONS = frozenset((
    "-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env",
    "--context", "-H", "--host", "--config", "-l", "--log-level",
    "-n", "--kubeconfig", "--cluster", "--user", "-s", "--server",
    "--prefix", "--dir", "-w", "--workspace", "--filter", "-f", "--file",
    "-R", "--repo", "--project", "--profile", "--region", "-j",
))
# Interpreters: an approval covers the script or module they run, not every
# script. Inline code (``python -c``, ``node -e``, ``bash -c``) is not
# labelled at all, so approving it is one call only.
INTERPRETERS = frozenset((
    "python", "python3", "py", "pypy", "pypy3", "node", "deno", "ruby", "perl", "php",
    "bash", "sh", "zsh", "dash", "ksh", "fish", "pwsh", "powershell",
))
_PYTHON_VERSIONED = re.compile(r"^python\d+(?:\.\d+)*$")
_INLINE_CODE_FLAGS = frozenset((
    "-c", "-e", "-p", "-r", "--eval", "--print", "-command", "-encodedcommand", "-ec",
))
_INTERPRETER_VALUE_OPTIONS = frozenset(("-W", "-X", "-I", "--require", "-file", "-f"))

# Forms no session approval covers: each keeps asking every time, even after
# "always" for the same program and subcommand. Kept to operations that
# destroy work or history, or publish, and cannot be taken back.
_DESTRUCTIVE_VERBS = frozenset((
    "delete", "destroy", "prune", "uninstall", "unpublish", "yank", "publish",
))
_ALWAYS_DESTRUCTIVE_PROGRAMS = frozenset(("shred", "wipefs", "mkfs"))


def _short_flags(args: list[str]) -> str:
    """Every single-dash short option letter in ``args`` (``-rf`` → ``rf``)."""
    return "".join(a[1:] for a in args if a.startswith("-") and not a.startswith("--"))


def _is_destructive(program: str, words: list[str], args: list[str]) -> bool:
    """True for a dangerous form of ``program`` (``words``: its subcommands).

    ``args`` is everything after the program, global options included.
    """
    if program in _ALWAYS_DESTRUCTIVE_PROGRAMS or program.startswith("mkfs."):
        return True
    if program == "dd":
        return any(a.startswith("of=") for a in args)
    if program in ("rm", "rmdir"):
        flags = _short_flags(args)
        return "r" in flags or "R" in flags or any(
            a in ("--recursive", "--no-preserve-root") for a in args
        )
    if program == "find":
        return "-delete" in args or any(
            a in ("-exec", "-execdir", "-ok", "-okdir") and _program_name(args[i + 1]) in ("rm", "shred")
            for i, a in enumerate(args[:-1])
        )
    if any(w in _DESTRUCTIVE_VERBS for w in words):
        return True
    if program != "git" or not words:
        return False
    sub, flags = words[0], _short_flags(args)
    if sub == "push":
        return (
            "f" in flags or "d" in flags
            or any(a.split("=", 1)[0] in ("--force", "--force-with-lease", "--force-if-includes",
                                          "--mirror", "--delete", "--prune") for a in args)
            or any(a.startswith(("+", ":")) for a in args)
        )
    if sub == "reset":
        return any(a in ("--hard", "--merge", "--keep") for a in args)
    if sub == "clean":
        return "f" in flags or "--force" in args
    if sub == "checkout":
        return "f" in flags or any(a in ("--", ".", "--force") for a in args)
    if sub == "switch":
        return "f" in flags or any(a in ("--force", "--discard-changes") for a in args)
    if sub == "branch":
        return "D" in flags or ("--delete" in args and ("--force" in args or "f" in flags))
    if sub in ("restore", "filter-branch", "filter-repo"):
        return True
    if sub == "stash":
        return words[1:2] in (["drop"], ["clear"])
    if sub == "reflog":
        return words[1:2] in (["expire"], ["delete"])
    if sub == "update-ref":
        return "d" in flags
    return False


def _subcommand_words(program: str, rest: list[str]) -> tuple[list[str], list[str]]:
    """``(subcommand words, tokens after them)`` for a subcommand program."""
    i = 0
    while i < len(rest) and rest[i].startswith("-"):
        i += 2 if rest[i] in _GLOBAL_VALUE_OPTIONS else 1
    if i >= len(rest):
        return [], []
    words = [rest[i].lower()]
    nested = NESTED_SUBCOMMANDS.get(program, frozenset())
    if ("*" in nested or words[0] in nested) and i + 1 < len(rest) and not rest[i + 1].startswith("-"):
        words.append(rest[i + 1] if words[0] == "run" else rest[i + 1].lower())
    return words, rest[i + len(words):]


def _interpreter_label(program: str, rest: list[str]) -> str | None:
    """``python x.py`` → ``python x.py``; inline code or stdin → None."""
    i = 0
    while i < len(rest):
        arg = rest[i]
        lowered = arg.lower()
        if lowered in _INLINE_CODE_FLAGS or (
            arg.startswith("-") and not arg.startswith("--") and len(arg) > 2
            and program in ("bash", "sh", "zsh", "dash", "ksh") and "c" in arg[1:]
        ):
            return None
        if arg == "-m" and i + 1 < len(rest):
            return f"{program} -m {rest[i + 1]}"
        if arg == "--" or not arg.startswith("-"):
            target = rest[i + 1] if arg == "--" and i + 1 < len(rest) else arg
            if arg == "--" and i + 1 >= len(rest):
                return None
            if program == "deno" and target == "run":
                return _interpreter_label(program, rest[i + 1:])
            return f"{program} {target}"
        i += 2 if lowered in _INTERPRETER_VALUE_OPTIONS or arg in _INTERPRETER_VALUE_OPTIONS else 1
    # No script: the interpreter reads its program from stdin (``curl … | sh``).
    return None


def _scope_label(tokens: list[str]) -> str | None:
    """The approval label for one simple command's tokens.

    A wrapper labels as ``wrapper <label of the wrapped command>``; a
    subcommand program as ``program subcommand`` (two verbs for the
    :data:`NESTED_SUBCOMMANDS`); an interpreter as ``interpreter script``;
    anything else as the program alone. None when no approval may cover the
    command beyond this one call: a wrapper with options (``sudo -u root``,
    where the wrapped program is not knowable from the text), inline
    interpreter code, or a dangerous form (:func:`_is_destructive`).
    """
    while tokens and _ENV_ASSIGNMENT.match(tokens[0]):
        tokens = tokens[1:]
    if not tokens:
        return None
    program = _program_name(tokens[0])
    rest = tokens[1:]
    if program in WRAPPER_PROGRAMS:
        if not rest:
            return program
        if rest[0].startswith("-"):
            return None
        inner = _scope_label(rest)
        return None if inner is None else f"{program} {inner}"
    words: list[str] = []
    if program in SUBCOMMAND_PROGRAMS:
        words, _after = _subcommand_words(program, rest)
    if _is_destructive(program, words, rest):
        return None
    if words:
        return " ".join((program, *words))
    if program in INTERPRETERS or _PYTHON_VERSIONED.match(program):
        return _interpreter_label(program, rest)
    return program


def _simple_command_tokens(command: str) -> list[list[str]] | None:
    """Token lists of every simple command, or None when they are not knowable."""
    if not command or not command.strip() or "$(" in command or "`" in command:
        return None
    out: list[list[str]] = []
    for line in command.splitlines():
        if not line.strip():
            continue
        tokens = _lex(line)
        if tokens is None:
            return None
        current: list[str] = []
        for tok in tokens:
            if tok in _SEPARATORS:
                if current:
                    out.append(current)
                current = []
                continue
            if tok and all(ch in "();<>|&" for ch in tok):
                return None
            current.append(tok)
        if current:
            out.append(current)
    return out or None


def program_names(command: str) -> frozenset[str] | None:
    """Every program a command invokes (``cat a | grep b`` → {cat, grep}).

    None when the command cannot be lexed or uses substitution/subshells,
    so callers fall back to the broadest scope rather than a wrong one.
    """
    groups = _simple_command_tokens(command)
    if groups is None:
        return None
    names: set[str] = set()
    for tokens in groups:
        while tokens and _ENV_ASSIGNMENT.match(tokens[0]):
            tokens = tokens[1:]
        if tokens:
            names.add(_program_name(tokens[0]))
    return frozenset(names) or None


def scope_labels(command: str) -> frozenset[str] | None:
    """The ``allow_always`` labels of a command: one per simple command.

    Plain programs label as themselves (``ls``); wrappers, subcommand
    programs and interpreters carry what they run (``sudo apt``, ``git
    commit``, ``npm run build``, ``python tool.py``). None when any part
    cannot be labelled or is a dangerous form, so the approval is one-shot
    rather than wide.
    """
    groups = _simple_command_tokens(command)
    if groups is None:
        return None
    labels: set[str] = set()
    for tokens in groups:
        label = _scope_label(list(tokens))
        if label is None:
            return None
        labels.add(label)
    return frozenset(labels) or None


def session_scope(input: dict[str, Any]) -> frozenset[str] | None:
    """``allow_always`` scope for Bash: what the command runs, by label."""
    command = input.get("command")
    return scope_labels(command) if isinstance(command, str) else None


def describe_session_scope(input: dict[str, Any]) -> str:
    """What answering "always" to this Bash call allows, in plain words."""
    labels = session_scope(input)
    if labels is None:
        return "this call only (this command cannot be allowed for the session)"
    return "for the session: " + ", ".join(sorted(labels))


# Concrete names the sensitive-file globs in fluid.tools.hardening cover, so an
# argument glob can be tested against them: ``?env`` matches ``.env``,
# ``~/.ssh/*`` matches ``.ssh/config``.
_SENSITIVE_EXAMPLE_NAMES = (
    ".env", ".env.local", "x.pem", "x.p12", "x.pfx",
    "id_rsa", "id_rsa.pub", "id_ed25519", "id_ed25519.pub", "id_ecdsa", "id_ecdsa.pub",
    ".netrc", "_netrc", ".npmrc", ".pypirc", ".htpasswd", ".git-credentials",
    "managed-postgres.json", "preview-access.key", ".credentials.json", ".pgpass",
    "environ",
)
_SENSITIVE_EXAMPLE_PATHS = (
    ".aws/credentials", ".ssh/config",
    ".claude/.credentials.json", ".config/gh/hosts.yml",
    ".credential-keys/server.key", "chrome-profile/Default/Cookies",
)
_GLOB_CHARS = frozenset("*?[{")
_BRACE_RE = re.compile(r"\{([^{}]*)\}")


def _has_unquoted_glob(raw: str) -> bool:
    """True when a raw (still-quoted) token has a glob or brace outside quotes."""
    quote: str | None = None
    for ch in raw:
        if quote:
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch in _GLOB_CHARS:
            return True
    return False


def _expand_braces(pattern: str, limit: int = 64) -> list[str] | None:
    """Innermost-first brace expansion; None when it cannot be done."""
    out = [pattern]
    for _ in range(limit):
        if not any(_BRACE_RE.search(p) for p in out):
            return out if all("{" not in p and "}" not in p for p in out) else None
        expanded: list[str] = []
        for p in out:
            m = _BRACE_RE.search(p)
            if m is None:
                expanded.append(p)
                continue
            for alt in m.group(1).split(","):
                expanded.append(p[: m.start()] + alt + p[m.end():])
        if len(expanded) > limit:
            return None
        out = expanded
    return None


def _glob_could_name_sensitive_path(pattern: str) -> bool:
    """True when a glob argument could expand to a credential file."""
    from fnmatch import fnmatchcase

    from fluid.tools.hardening import is_sensitive_path

    alternatives = _expand_braces(pattern)
    if alternatives is None:
        return True
    for alt in alternatives:
        # The literal skeleton: ``.env*`` -> ``.env``, ``config/.env?`` -> ``config/.env``.
        if is_sensitive_path(re.sub(r"[*?\[\]]", "", alt)):
            return True
        parts = [p for p in alt.replace("\\", "/").lower().split("/") if p]
        if not parts:
            continue
        if any(fnmatchcase(name, parts[-1]) for name in _SENSITIVE_EXAMPLE_NAMES):
            return True
        for rep_path in _SENSITIVE_EXAMPLE_PATHS:
            rep_parts = rep_path.split("/")
            tail = parts[-len(rep_parts):]
            if len(tail) == len(rep_parts) and all(
                fnmatchcase(r, g) for r, g in zip(rep_parts, tail)
            ):
                return True
    return False


def _touches_sensitive_path(command: str) -> bool:
    """True when any argument names a credential file (``cat .env`` etc.).

    A read-only command is still an exfiltration read when its target is a
    credential file, so it must not sail under the on_write policy. Best-effort
    shlex tokenization; a command we cannot tokenize was already rejected by
    the read-only classifier.

    Tokens are lexed with their quotes kept so an unquoted glob (``.env*``,
    ``?env``, ``.{env,bashrc}``) can be told apart from a quoted regex
    (``'foo.*'``): the shell expands the former after the classifier has seen
    it, so it counts when any file it could match is sensitive.
    """
    from fluid.tools.hardening import is_sensitive_path

    lex = shlex.shlex(command, posix=False)
    lex.whitespace_split = True
    try:
        raw_tokens = list(lex)
    except ValueError:
        return False
    for raw in raw_tokens[1:]:
        if raw.startswith("-"):
            continue
        try:
            literal = " ".join(shlex.split(raw))
        except ValueError:
            return True
        if is_sensitive_path(literal):
            return True
        if _has_unquoted_glob(raw) and _glob_could_name_sensitive_path(literal):
            return True
    return False


# Searchers that walk a directory tree. Every file a recursive search under a
# process directory (or above one) reads includes that process's ``environ``,
# which no argument names, so the search root itself counts.
_TREE_SEARCHERS = frozenset(("rg", "ag", "ack"))
_RECURSIVE_GREPS = frozenset(("grep", "egrep", "fgrep", "zgrep"))
_PROCESS_TREE_RE = re.compile(r"^(?:/|/proc|/proc/(?:\d+|self|thread-self)(?:/task/\d+)?)$")


def _greps_recursively(args: list[str]) -> bool:
    for i, arg in enumerate(args):
        if arg == "--":
            return False
        if arg in ("--recursive", "--dereference-recursive", "--directories=recurse"):
            return True
        if arg in ("-d", "--directories") and args[i + 1:i + 2] == ["recurse"]:
            return True
        if arg.startswith("-") and not arg.startswith("--") and ("r" in arg or "R" in arg):
            return True
    return False


def _searches_process_tree(command: str) -> bool:
    """True when a recursive search starts at ``/``, ``/proc`` or a process dir."""
    for tokens in _simple_command_tokens(command) or ():
        while tokens and _ENV_ASSIGNMENT.match(tokens[0]):
            tokens = tokens[1:]
        if not tokens:
            continue
        program, args = _program_name(tokens[0]), tokens[1:]
        if program not in _TREE_SEARCHERS and not (
            program in _RECURSIVE_GREPS and _greps_recursively(args)
        ):
            continue
        for arg in args:
            if arg.startswith("-"):
                continue
            path = arg.replace("\\", "/").rstrip("/") or "/"
            if _PROCESS_TREE_RE.match(path):
                return True
    return False


def classify(input: dict[str, Any]) -> str:
    """Effective risk of one Bash tool input: ``safe`` or ``write``."""
    command = input.get("command")
    if not isinstance(command, str):
        return WRITE
    if not is_read_only(command):
        return WRITE
    # Read-only, but targeting a credential file → keep the prompt.
    if _touches_sensitive_path(command) or _searches_process_tree(command):
        return WRITE
    return SAFE
