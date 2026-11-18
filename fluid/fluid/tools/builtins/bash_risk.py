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

Programs with extra rules. A banned option is refused in every spelling the
program's parser accepts (see :func:`_no_flags`): bare, with its value glued
on (``-oFILE``), inside combined short flags (``-uo FILE``), as
``--opt=value`` and as an abbreviated long option (``--out``):

- ``find``: none of ``-exec``, ``-execdir``, ``-ok``, ``-okdir``, ``-delete``,
  ``-fprint*``, ``-fls``.
- ``awk``/``gawk``/``mawk``: no ``system(``, ``getline`` or ``|`` in any
  argument (those run or write to commands), and none of ``-f``/``--file``,
  ``-i``/``--include``, ``-l``/``--load``, ``-E``/``--exec`` (script files and
  extension libraries the classifier cannot read), ``-o``, ``-p``, ``-d``
  (they write a file), ``-D`` (the debugger) or ``-W`` (options by name).
- ``sed``: must carry ``-n``/``--quiet``/``--silent``, must not carry ``-i`` or
  ``--in-place`` or ``-f``/``--file`` (an unread script file), and no script
  argument (``-e`` glued or not) that looks like a ``w`` file-write command or
  a GNU ``e`` command or ``s///e`` flag (both run the text as a shell command).
- ``sort``: no ``-o``/``--output`` or ``--compress-program``. ``uniq``: at most
  one operand (the second is an output file). ``date``: no ``-s``/``--set``.
  ``tree``: no ``-o`` or ``-R`` (both write files). ``file``: no
  ``-C``/``--compile`` (writes a compiled magic file). ``printf``: no ``-v``
  (assigns a shell variable). ``env``: no arguments (``env cmd`` runs
  ``cmd``). ``rg``: no ``--pre`` or ``--hostname-bin`` (both run a command).
- ``git``: read-only subcommands only, listed in :data:`GIT_READ_ONLY` with
  their per-subcommand argument constraints; ``-C <dir>`` and ``--no-pager``
  global options are accepted, ``-c key=value`` is not (it can point git at an
  external program); any ``--output*`` argument or abbreviation of it is
  rejected.
- ``uv``/``pip``/``npm``/``pnpm``/``yarn``/``cargo``: only the informational
  subcommands in :data:`PACKAGE_MANAGER_READ_ONLY` (``uv pip list`` is handled
  by skipping the ``pip`` word), without ``pip --log``, ``pip``/``uv
  --python``, ``npm --logs-dir`` or ``cargo --config``.
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


def _abbreviates(name: str, options: tuple[str, ...]) -> bool:
    """``--name`` spells one of ``options``: in full, or as the unambiguous
    prefix GNU ``getopt_long`` (and git, optparse) accept (``--out`` for
    ``--output``). An ambiguous prefix counts too: it can only make the
    command ask."""
    return len(name) > 2 and any(option.startswith(name) for option in options)


def _no_flags(*banned: str, values: str = "", optional: str = "") -> Check:
    """Refuse every spelling of the ``banned`` options, GNU style.

    A short option (``"-o"``) is caught bare, with its value glued on
    (``-oFILE``) and inside a run of combined flags (``-uo FILE``, ``-uoFILE``);
    a long option (``"--output"``) with ``=value`` and as any abbreviation.
    ``values`` names short options known to take a required value and
    ``optional`` those whose value can only be glued on: the rest of the token
    (and, for ``values``, the next argument when nothing is glued) is that
    value, not more flags. A short option in neither list is read as a flag,
    so its glued value is scanned as flags too: options the check does not
    know can only make the command ask, never hide a banned one. Everything
    after ``--`` is an operand.
    """
    short = {b[1] for b in banned if len(b) == 2 and b[0] == "-" and b[1] != "-"}
    long = tuple(b for b in banned if b.startswith("--"))

    def check(args: list[str]) -> bool:
        skip_value = False
        for a in args:
            if skip_value:
                skip_value = False
                continue
            if a == "--":
                return True
            if a.startswith("--"):
                if _abbreviates(a.split("=", 1)[0], long):
                    return False
                continue
            if not a.startswith("-") or len(a) < 2:
                continue
            for j, ch in enumerate(a[1:], 1):
                if ch in short:
                    return False
                if ch in values or ch in optional:
                    skip_value = ch in values and j == len(a) - 1
                    break
        return True

    return check


def _find_ok(args: list[str]) -> bool:
    return not any(a in _FIND_EXEC_FLAGS or a.startswith("-fprint") for a in args)


# Options that load a program file the classifier never sees (-f, -i, -l,
# -E/--exec), write a file (-o pretty print, -p profile, -d variable dump),
# start the debugger (-D, which runs commands read from stdin), or pass an
# implementation option by name (-W profile, -W exec ...). -F, -v and -e take
# a value.
_awk_options_ok = _no_flags(
    "-f", "-i", "-l", "-E", "-o", "-p", "-d", "-D", "-W",
    "--file", "--include", "--load", "--exec", "--pretty-print", "--profile",
    "--dump-variables", "--debug",
    values="Fve",
)


def _awk_ok(args: list[str]) -> bool:
    for a in args:
        if "system(" in a or "getline" in a or "|" in a:
            return False
    return _awk_options_ok(args)


def _sed_ok(args: list[str]) -> bool:
    quiet = False
    scripts: list[str] = []
    operands = False
    for a in args:
        if operands or a == "-" or not a.startswith("-"):
            scripts.append(a)
        elif a == "--":
            operands = True
        elif a.startswith("--"):
            name, _, value = a.partition("=")
            if _abbreviates(name, ("--in-place", "--file")):
                return False
            if name in ("--quiet", "--silent"):
                quiet = True
            if value:  # --expression=..., or a value scanned to be safe
                scripts.append(value)
        else:
            for j, ch in enumerate(a[1:], 1):
                if ch in "if":
                    return False
                if ch == "n":
                    quiet = True
                elif ch in "el":  # -e SCRIPT, -l N: the rest is the value
                    if a[j + 1:]:
                        scripts.append(a[j + 1:])
                    break
    if not quiet:
        return False
    return not any(_sed_script_runs_or_writes(s) for s in scripts)


def _sed_script_runs_or_writes(script: str) -> bool:
    if _SED_WRITE_CMD.search(script):
        return True
    return bool(_SED_EXEC_CMD.search(_SED_ADDRESS_RE.sub("", script)))


def _uniq_ok(args: list[str]) -> bool:
    if "--" in args:  # after it every argument is an operand, ``-x`` included
        cut = args.index("--")
        operands = [a for a in args[:cut] if not a.startswith("-")] + args[cut + 1:]
    else:
        operands = [a for a in args if not a.startswith("-")]
    return len(operands) <= 1


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
    # rg --pre and --hostname-bin run a program.
    "grep": None, "egrep": None, "fgrep": None, "rg": _no_flags("--pre", "--hostname-bin"),
    "find": _find_ok, "file": _no_flags("-C", "--compile", values="eFfmP"), "stat": None, "du": None, "df": None,
    # file -C compiles a magic file and writes NAME.mgc. printf -v assigns a
    # shell variable (PATH included) for the commands after it.
    "pwd": None, "echo": None, "printf": _no_flags("-v"), "true": None,
    "which": None, "where": None, "type": None,
    "env": _env_ok, "printenv": None,
    # tree -o writes its listing to a file; -R reruns it with -o 00Tree.html
    # in every directory.
    "date": _no_flags("-s", "--set", values="dfr", optional="I"),
    "whoami": None, "uname": None, "tree": _no_flags("-o", "-R", "--output"),
    "diff": None, "cmp": None,
    # sort -o writes; --compress-program runs a program.
    "sort": _no_flags("-o", "--output", "--compress-program", values="kStT"), "uniq": _uniq_ok, "cut": None, "tr": None,
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
    # --output=FILE writes; git accepts abbreviations (--out=FILE) too.
    if any(
        a.startswith("--output") or _abbreviates(a.split("=", 1)[0], ("--output",)) for a in args
    ):
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


def _package_manager(*banned: str) -> Check:
    """An informational subcommand, without the options that write or run:
    pip --log writes a log file, pip/uv --python run the named interpreter,
    npm --logs-dir writes its debug log there, cargo --config can name the
    rustc that cargo runs to read target information."""
    options_ok = _no_flags(*banned)
    return lambda args: _package_manager_ok(args) and options_ok(args)


_pip_ok = _package_manager("--log", "--log-file", "--python")

READ_ONLY_PROGRAMS.update({
    "git": _git_ok,
    "uv": _package_manager("--python", "-p"), "pip": _pip_ok, "pip3": _pip_ok,
    "npm": _package_manager("--logs-dir"), "pnpm": _package_manager_ok, "yarn": _package_manager_ok,
    "cargo": _package_manager("--config"),
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
# build``), so it carries that too.
NESTED_SUBCOMMANDS: dict[str, frozenset[str]] = {
    "git": frozenset(("stash", "remote", "worktree", "submodule", "reflog", "notes", "lfs")),
    "docker": frozenset((
        "compose", "container", "image", "volume", "network", "system", "buildx",
        "builder", "context", "swarm", "stack", "service",
    )),
    "kubectl": frozenset(("config", "rollout")),
    "gh": frozenset("*"), "aws": frozenset("*"), "gcloud": frozenset("*"), "az": frozenset("*"),
    **{pm: frozenset(("run",)) for pm in ("npm", "pnpm", "yarn", "bun")},
}
NESTED_SUBCOMMANDS["podman"] = NESTED_SUBCOMMANDS["docker"]
# ``uv run CMD`` and friends run another command: their label carries that
# command's own label (``uv run python tool.py``), so a grant covers what runs.
_RUNNER_VERBS = frozenset((("uv", "run"), ("poetry", "run"), ("pipx", "run"), ("conda", "run")))

# Options a program accepts before its subcommand, as (options taking a
# value, options without one). Anything else before the verb (``git -c
# key=value``, which can turn a push into a mirror push) cannot be parsed
# with confidence, so the command gets no session scope at all.
_PREFIX_OPTIONS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "git": (
        frozenset(("-C", "--git-dir", "--work-tree", "--namespace")),
        frozenset(("--no-pager", "-P", "--paginate", "-p", "--no-optional-locks",
                   "--no-replace-objects", "--literal-pathspecs", "--bare")),
    ),
    "docker": (
        frozenset(("-H", "--host", "--context", "-c", "--config", "-l", "--log-level")),
        frozenset(("-D", "--debug", "--tls", "--tlsverify")),
    ),
    "kubectl": (
        frozenset(("-n", "--namespace", "--context", "--kubeconfig", "--cluster", "--user",
                   "-s", "--server")),
        frozenset(),
    ),
    "helm": (frozenset(("-n", "--namespace", "--kube-context", "--kubeconfig")), frozenset()),
    "npm": (frozenset(("--prefix", "-w", "--workspace")),
            frozenset(("-g", "--global", "-s", "--silent", "-q", "--quiet"))),
    "pnpm": (frozenset(("-C", "--dir", "--filter", "-F", "-w", "--workspace")),
             frozenset(("-r", "--recursive", "-s", "--silent"))),
    "yarn": (frozenset(("--cwd",)), frozenset(("-s", "--silent"))),
    "make": (frozenset(("-C", "--directory", "-f", "--file", "--makefile", "-j", "--jobs", "-l")),
             frozenset(("-k", "-s", "--silent", "-B", "--no-print-directory", "-w"))),
    "cargo": (frozenset(("-C", "--color", "--config")),
              frozenset(("-q", "--quiet", "-v", "--verbose", "--locked", "--offline", "--frozen"))),
    "uv": (frozenset(("--directory", "--project")),
           frozenset(("-q", "--quiet", "-v", "--verbose", "--offline", "--no-cache"))),
    "pip": (frozenset(()), frozenset(("-q", "--quiet", "-v", "--verbose", "--isolated",
                                     "--disable-pip-version-check", "--no-cache-dir"))),
    "aws": (frozenset(("--profile", "--region", "--output")), frozenset(("--no-cli-pager",))),
    "gcloud": (frozenset(("--project", "--account", "--configuration", "--format")),
               frozenset(("-q", "--quiet"))),
    "az": (frozenset(("--subscription", "-o", "--output")), frozenset(("--only-show-errors",))),
    "apt": (frozenset(()), frozenset(("-y", "--yes", "-q", "-qq", "--assume-yes"))),
    "systemctl": (frozenset(()), frozenset(("--user", "--system", "--no-pager", "-q", "--quiet"))),
}
for _alias, _base in (("podman", "docker"), ("pip3", "pip"), ("apt-get", "apt"),
                      ("dnf", "apt"), ("yum", "apt")):
    _PREFIX_OPTIONS[_alias] = _PREFIX_OPTIONS[_base]
# Options between a subcommand and its second verb (``docker compose -p dev
# up``). As above, anything else there leaves the command unscoped.
_NESTED_PREFIX_OPTIONS: dict[tuple[str, str], tuple[frozenset[str], frozenset[str]]] = {
    ("docker", "compose"): (
        frozenset(("-p", "--project-name", "-f", "--file", "--profile", "--env-file",
                   "--project-directory", "--ansi", "--progress", "--parallel")),
        frozenset(("--dry-run", "--compatibility", "--all-resources")),
    ),
}
_NESTED_PREFIX_OPTIONS[("podman", "compose")] = _NESTED_PREFIX_OPTIONS[("docker", "compose")]

# Interpreters: an approval covers the script (by its path, resolved against
# the directory the command runs in) or module they run, not every script.
# Inline code (``python -c``, ``node -e``, ``bash -c``, ``deno eval``) and a
# program read from stdin are not labelled at all, so approving them is one
# call only, as is any option the parser does not know.
INTERPRETERS = frozenset((
    "python", "python3", "py", "pypy", "pypy3", "node", "deno", "ruby", "perl", "php",
    "bash", "sh", "zsh", "dash", "ksh", "fish", "pwsh", "powershell",
))
_PYTHON_VERSIONED = re.compile(r"^python\d+(?:\.\d+)*$")
_SHELLS = frozenset(("bash", "sh", "zsh", "dash", "ksh", "fish"))


class _Short:
    """One interpreter family's single-dash options.

    ``flags``: letters taking no value; ``values``: letters taking one (the
    rest of the token or the next token); ``inline``: letters whose value is
    program text; ``script``: a letter naming the script (``php -f x``).
    """

    def __init__(self, flags: str, values: str = "", inline: str = "", script: str = "",
                 module: str = "", long_flags: tuple[str, ...] = (),
                 long_values: tuple[str, ...] = (), long_inline: tuple[str, ...] = ()) -> None:
        self.flags, self.values, self.inline = flags, values, inline
        self.script, self.module = script, module
        self.long_flags, self.long_values = frozenset(long_flags), frozenset(long_values)
        self.long_inline = frozenset(long_inline)


_INTERPRETER_OPTIONS: dict[str, _Short] = {
    "python": _Short("bBdEhiIOPqRsSuvV", values="WX", inline="c", module="m",
                     long_flags=("--version", "--help"),
                     long_values=("--check-hash-based-pycs",)),
    "node": _Short("hivc", values="r", inline="ep",
                   long_flags=("--inspect", "--inspect-brk", "--enable-source-maps", "--watch",
                               "--no-warnings", "--trace-warnings", "--expose-gc", "--check",
                               "--trace-uncaught", "--preserve-symlinks", "--version", "--help"),
                   long_values=("--require", "--import", "--loader", "--experimental-loader",
                                "--conditions", "--title"),
                   long_inline=("--eval", "--print")),
    "ruby": _Short("wdvcWl", values="IrC", inline="e"),
    "perl": _Short("wTtcsWl", values="IMm", inline="eE"),
    "php": _Short("nqlsH", values="dcz", inline="rRBEF", script="f"),
    "shell": _Short("aefhkmnptuvxBCEHPTl", values="oO",
                    long_flags=("--login", "--norc", "--noprofile", "--posix", "--restricted"),
                    long_values=("--rcfile", "--init-file")),
    "deno": _Short("qA", values="c",
                   long_flags=("--allow-all", "--quiet", "--watch", "--check", "--no-check",
                               "--unstable", "--no-prompt", "--reload"),
                   long_values=("--config", "--import-map", "--lock", "--location", "--env-file")),
}
_PWSH_INLINE = frozenset(("-command", "-c", "-encodedcommand", "-ec", "-e", "-enc"))
_PWSH_SCRIPT = frozenset(("-file", "-f"))
_PWSH_FLAGS = frozenset(("-noprofile", "-nop", "-nologo", "-noninteractive", "-nonint"))
_PWSH_VALUES = frozenset(("-executionpolicy", "-ep", "-ex", "-workingdirectory", "-wd",
                          "-outputformat", "-of", "-inputformat", "-if", "-settingsfile"))

# Forms no session approval covers: each keeps asking every time, even after
# "always" for the same program and subcommand. Kept to operations that
# destroy work or history, or publish, and cannot be taken back.
_DESTRUCTIVE_VERBS = frozenset((
    "delete", "destroy", "prune", "uninstall", "unpublish", "yank", "publish",
    "rm", "rmi", "remove",
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
    if program in ("docker", "podman") and words[:2] == ["compose", "down"]:
        return "v" in _short_flags(args) or "--volumes" in args
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
    if sub == "commit":
        return "--amend" in args
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


def _skip_options(
    tokens: list[str], table: tuple[frozenset[str], frozenset[str]] | None,
) -> int | None:
    """Index of the first non-option token, or None at an option not in ``table``."""
    values, flags = table or (frozenset(), frozenset())
    i = 0
    while i < len(tokens) and tokens[i].startswith("-") and tokens[i] != "-":
        tok = tokens[i]
        name = tok.split("=", 1)[0]
        if tok in flags:
            i += 1
        elif tok in values:
            i += 2
        elif "=" in tok and name in values and name.startswith("--"):
            i += 1
        elif not tok.startswith("--") and tok[:2] in values and len(tok) > 2:
            i += 1  # attached short value: ``-C/repo``, ``-j4``
        else:
            return None
    return i


def _subcommand_words(program: str, rest: list[str]) -> tuple[list[str], list[str]] | None:
    """``(subcommand words, tokens after them)``; None when not parseable.

    ``([], [])`` when the program has no subcommand (``git --version``).
    """
    if program == "cargo" and rest and rest[0].startswith("+"):
        rest = rest[1:]  # ``cargo +nightly build``: a toolchain, not a verb
    i = _skip_options(rest, _PREFIX_OPTIONS.get(program))
    if i is None:
        return None
    if i >= len(rest):
        return [], []
    words = [rest[i].lower()]
    nested = NESTED_SUBCOMMANDS.get(program, frozenset())
    if "*" in nested or words[0] in nested:
        tail = rest[i + 1:]
        j = _skip_options(tail, _NESTED_PREFIX_OPTIONS.get((program, words[0])))
        if j is None:
            return None
        if j < len(tail):
            words.append(tail[j] if words[0] == "run" else tail[j].lower())
            return words, tail[j + 1:]
        return words, tail[j:]
    return words, rest[i + 1:]


def _resolved(path: str, cwd: str | None) -> str:
    """``path`` made absolute against ``cwd`` when known, else as spelled."""
    import os

    expanded = os.path.expanduser(path)
    if os.path.isabs(expanded):
        return os.path.normpath(expanded).replace("\\", "/")
    if cwd:
        return os.path.normpath(os.path.join(cwd, expanded)).replace("\\", "/")
    return path


def _script_label(program: str, script: str, cwd: str | None) -> str:
    return f"{program} {_resolved(script, cwd)}"


def _interpreter_label(program: str, rest: list[str], cwd: str | None) -> str | None:
    """``python x.py`` → ``python <cwd>/x.py``; inline code, stdin or an
    option the parser does not know → None."""
    family = (
        "python" if program in ("python", "python3", "py", "pypy", "pypy3")
        or _PYTHON_VERSIONED.match(program)
        else "shell" if program in _SHELLS else program
    )
    if family in ("pwsh", "powershell"):
        return _pwsh_label(program, rest, cwd)
    if family == "deno":
        verb_at = _skip_options(rest, None)
        if verb_at is None or verb_at >= len(rest):
            return None
        verb = rest[verb_at].lower()
        if verb != "run":
            # ``deno eval``/``deno repl`` run inline code; other verbs name a
            # tool (``deno fmt``) and are labelled like a subcommand program.
            return None if verb in ("eval", "repl") else f"deno {verb}"
        rest = rest[verb_at + 1:]
    opts = _INTERPRETER_OPTIONS[family]
    i = 0
    while i < len(rest):
        arg = rest[i]
        if arg == "--":
            return _script_label(program, rest[i + 1], cwd) if i + 1 < len(rest) else None
        if arg == "-" or not arg.startswith(("-", "+")):
            if arg == "-":
                return None  # the program comes from stdin
            return _script_label(program, arg, cwd)
        if arg.startswith("--") or (family == "shell" and arg[:2] in ("+o", "+O")):
            name = arg.split("=", 1)[0]
            if name in opts.long_inline:
                return None
            if name in opts.long_flags or (family == "node" and name.startswith(
                    ("--experimental-", "--no-", "--max-old-space-size")) and name != "--experimental-loader"):
                i += 1
            elif name in opts.long_values or arg[:2] in ("+o", "+O"):
                i += 1 if "=" in arg else 2
            elif family == "deno" and name.startswith(("--allow-", "--deny-", "--unstable-")):
                i += 1
            else:
                return None
            continue
        letters, j, step = arg[1:], 0, 1
        while j < len(letters):
            letter = letters[j]
            if letter in opts.inline:
                return None
            if family == "shell" and letter in "si":
                return None  # ``bash -s``: the program comes from stdin
            if letter in opts.module:
                module = letters[j + 1:] or (rest[i + 1] if i + 1 < len(rest) else "")
                if not module:
                    return None
                where = f" in {_resolved('.', cwd)}" if cwd else ""
                return f"{program} -m {module}{where}"
            if letter in opts.script:
                script = letters[j + 1:] or (rest[i + 1] if i + 1 < len(rest) else "")
                return _script_label(program, script, cwd) if script else None
            if letter in opts.values:
                if j + 1 == len(letters):
                    step = 2
                break
            if letter not in opts.flags:
                return None
            j += 1
        i += step
    # No script: the interpreter reads its program from stdin (``curl … | sh``).
    return None


def _pwsh_label(program: str, rest: list[str], cwd: str | None) -> str | None:
    i = 0
    while i < len(rest):
        arg = rest[i].lower()
        if arg in _PWSH_INLINE:
            return None
        if arg in _PWSH_SCRIPT:
            return _script_label(program, rest[i + 1], cwd) if i + 1 < len(rest) else None
        if arg in _PWSH_FLAGS:
            i += 1
        elif arg in _PWSH_VALUES:
            i += 2
        elif arg.startswith("-"):
            return None
        else:
            return _script_label(program, rest[i], cwd)
    return None


def _scope_label(tokens: list[str], cwd: str | None = None) -> str | None:
    """The approval label for one simple command's tokens.

    A wrapper labels as ``wrapper <label of the wrapped command>``; a
    subcommand program as ``program subcommand`` (two verbs for the
    :data:`NESTED_SUBCOMMANDS`); an interpreter as ``interpreter script``;
    anything else as the program alone. None when no approval may cover the
    command beyond this one call: a wrapper with options (``sudo -u root``,
    where the wrapped program is not knowable from the text), options before
    a subcommand the parser does not know, inline interpreter code, or a
    dangerous form (:func:`_is_destructive`).
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
        inner = _scope_label(rest, cwd)
        return None if inner is None else f"{program} {inner}"
    words: list[str] = []
    after: list[str] = rest
    if program in SUBCOMMAND_PROGRAMS:
        parsed = _subcommand_words(program, rest)
        if parsed is None:
            return None
        words, after = parsed
    if _is_destructive(program, words, rest):
        return None
    if words and (program, words[0]) in _RUNNER_VERBS:
        # ``uv run python tool.py``: what runs is the inner command. Options
        # of the runner itself (``uv run --with x``) are not parsed.
        if not after or after[0].startswith("-"):
            return None
        inner = _scope_label(list(after), cwd)
        return None if inner is None else f"{program} run {inner}"
    if words:
        return " ".join((program, *words))
    if program in SUBCOMMAND_PROGRAMS and rest:
        return None  # options only, or nothing parseable as a verb
    if program in INTERPRETERS or _PYTHON_VERSIONED.match(program):
        return _interpreter_label(program, rest, cwd)
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


def effective_cwd(input: dict[str, Any], cwd: str | None = None) -> str | None:
    """The directory a Bash call runs in: its ``cwd`` field (against the
    session's ``cwd``), else the session's ``cwd``; None when unknown."""
    import os

    own = input.get("cwd")
    if isinstance(own, str) and own.strip():
        own = os.path.expanduser(own.strip())
        if os.path.isabs(own):
            return os.path.normpath(own)
        return os.path.normpath(os.path.join(cwd, own)) if cwd else None
    return cwd


def scope_labels(command: str, cwd: str | None = None) -> frozenset[str] | None:
    """The ``allow_always`` labels of a command: one per simple command.

    Plain programs label as themselves (``ls``); wrappers, subcommand
    programs and interpreters carry what they run (``sudo apt``, ``git
    commit``, ``npm run build``, ``python /repo/tool.py``). None when any
    part cannot be labelled, is a dangerous form, or reads a credential file
    or a process's environment (:func:`touches_sensitive_data`), so the
    approval is one-shot rather than wide.
    """
    groups = _simple_command_tokens(command)
    if groups is None or touches_sensitive_data(command, cwd):
        return None
    labels: set[str] = set()
    for tokens in groups:
        label = _scope_label(list(tokens), cwd)
        if label is None:
            return None
        labels.add(label)
    return frozenset(labels) or None


def session_scope(input: dict[str, Any], cwd: str | None = None) -> frozenset[str] | None:
    """``allow_always`` scope for Bash: what the command runs, by label.

    ``cwd`` is the session's working directory; relative scripts resolve
    against the call's own ``cwd`` field on top of it.
    """
    command = input.get("command")
    if not isinstance(command, str):
        return None
    return scope_labels(command, effective_cwd(input, cwd))


def describe_session_scope(input: dict[str, Any], cwd: str | None = None) -> str:
    """What answering "always" to this Bash call allows, in plain words."""
    labels = session_scope(input, cwd)
    if labels is None:
        command = input.get("command")
        if isinstance(command, str) and touches_sensitive_data(command, effective_cwd(input, cwd)):
            return "this call only (it reads a credential file, which always asks)"
        return "this call only (this command cannot be allowed for the session)"
    text = "for the session: " + ", ".join(sorted(labels))
    if effective_cwd(input, cwd) is None and any(
        label.split(" ", 1)[0] in INTERPRETERS or _PYTHON_VERSIONED.match(label.split(" ", 1)[0])
        for label in labels
    ):
        text += " (scripts in the current directory only)"
    return text


# Concrete names the sensitive-file globs in fluid.tools.hardening cover, so an
# argument glob can be tested against them: ``?env`` matches ``.env``,
# ``~/.ssh/*`` matches ``.ssh/config``.
_SENSITIVE_EXAMPLE_NAMES = (
    ".env", ".env.local", "x.pem", "x.p12", "x.pfx",
    "id_rsa", "id_rsa.pub", "id_ed25519", "id_ed25519.pub", "id_ecdsa", "id_ecdsa.pub",
    ".netrc", "_netrc", ".npmrc", ".pypirc", ".htpasswd", ".git-credentials",
    "managed-postgres.json", "preview-access.key", ".credentials.json", ".pgpass",
    "environ", ".yarnrc.yml",
)


def _sensitive_example_paths() -> tuple[str, ...]:
    """Directory-qualified examples, derived from the shared policy itself."""
    from fluid.tools.hardening import (
        SENSITIVE_DIR_NAMES,
        SENSITIVE_PATH_GLOBS,
        SENSITIVE_PATH_SUFFIXES,
    )

    return (
        *(suffix.strip("/").lower() for suffix in SENSITIVE_PATH_SUFFIXES),
        *(f"{name}/x" for name in SENSITIVE_DIR_NAMES),
        *(glob.strip("/").replace("*", "1").lower() for glob in SENSITIVE_PATH_GLOBS),
        "proc/1/environ", "proc/self/environ",
    )


_GLOB_CHARS = frozenset("*?[{")
_BRACE_RE = re.compile(r"\{([^{}]*)\}")
# Globs are expanded on disk too, up to this many matches each.
_GLOB_EXPANSION_LIMIT = 256


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


def _path_is_sensitive(path: str, cwd: str | None) -> bool:
    """``is_sensitive_path`` over every spelling the file-tool rules match:
    ``~`` expanded, ``.``/``..`` collapsed, absolute against ``cwd`` and
    with symlinks followed (:func:`fluid.permissions.rules.path_candidates`)."""
    import os

    from fluid.permissions.rules import path_candidates
    from fluid.tools.hardening import is_sensitive_path

    raw, normalized = path_candidates(os.path.expanduser(path), cwd)
    return any(is_sensitive_path(candidate) for candidate in raw | normalized | {path})


def _glob_could_name_sensitive_path(pattern: str, cwd: str | None = None) -> bool:
    """True when a glob argument could expand to a credential file."""
    import glob
    import itertools
    import os
    from fnmatch import fnmatchcase

    alternatives = _expand_braces(pattern)
    if alternatives is None:
        return True
    examples = _sensitive_example_paths()
    for alt in alternatives:
        # The literal skeleton: ``.env*`` -> ``.env``, ``config/.env?`` -> ``config/.env``.
        if _path_is_sensitive(re.sub(r"[*?\[\]]", "", alt), cwd):
            return True
        normalized = os.path.normpath(os.path.expanduser(alt)).replace("\\", "/")
        parts = [p for p in normalized.lower().split("/") if p]
        if not parts:
            continue
        if any(fnmatchcase(name, parts[-1]) for name in _SENSITIVE_EXAMPLE_NAMES):
            return True
        for rep_path in examples:
            rep_parts = rep_path.split("/")
            tail = parts[-len(rep_parts):]
            if len(tail) == len(rep_parts) and all(
                fnmatchcase(r, g) for r, g in zip(rep_parts, tail)
            ):
                return True
        # What the shell would expand it to right now, matched as literals.
        expanded = os.path.expanduser(alt)
        if not os.path.isabs(expanded):
            if not cwd:
                continue
            expanded = os.path.join(cwd, expanded)
        try:
            matches = list(itertools.islice(glob.iglob(expanded), _GLOB_EXPANSION_LIMIT))
        except (OSError, ValueError, re.error):
            return True
        if any(_path_is_sensitive(match, cwd) for match in matches):
            return True
    return False


def _touches_sensitive_path(command: str, cwd: str | None = None) -> bool:
    """True when any argument names a credential file (``cat .env`` etc.).

    A read-only command is still an exfiltration read when its target is a
    credential file, so it must not sail under the on_write policy. Best-effort
    shlex tokenization; a command we cannot tokenize was already rejected by
    the read-only classifier. Each argument is matched in every spelling the
    file-tool rules use (``~/.kube/./config``, a symlink to a credential file,
    a path relative to ``cwd``).

    Tokens are lexed with their quotes kept so an unquoted glob (``.env*``,
    ``?env``, ``.{env,bashrc}``) can be told apart from a quoted regex
    (``'foo.*'``): the shell expands the former after the classifier has seen
    it, so it counts when any file it could match is sensitive.
    """
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
        if not literal:
            continue
        if _path_is_sensitive(literal, cwd):
            return True
        if _has_unquoted_glob(raw) and _glob_could_name_sensitive_path(literal, cwd):
            return True
    return False


# Searchers that walk a directory tree. Every file a recursive search under a
# process directory (or above one) reads includes that process's ``environ``,
# which no argument names, so the search root itself counts.
_TREE_SEARCHERS = frozenset(("rg", "ag", "ack"))
_RECURSIVE_GREPS = frozenset(("grep", "egrep", "fgrep", "zgrep"))
_PROCESS_TREE_RE = re.compile(r"^(?:/|/proc|/proc/(?:\d+|self|thread-self)(?:/task/\d+)?)$")
# Searcher options whose value is the next token, so it is not a search root.
_SEARCH_VALUE_OPTIONS = frozenset((
    "-A", "-B", "-C", "-m", "-e", "-f", "-d", "-D", "-g", "-t", "-T", "-j", "-M",
    "--regexp", "--file", "--max-count", "--glob", "--type", "--type-not",
    "--max-depth", "--context", "--after-context", "--before-context", "--threads",
))


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


def _search_roots(args: list[str]) -> list[str]:
    """The directories a searcher walks: its operands after the pattern, or
    ``.`` when it names none (``grep -r x`` and ``rg x`` search the cwd)."""
    operands: list[str] = []
    explicit_pattern = False
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--":
            operands.extend(args[i + 1:])
            break
        if arg.startswith("-") and arg != "-":
            name = arg.split("=", 1)[0]
            if name in ("-e", "-f", "--regexp", "--file"):
                explicit_pattern = True
            i += 2 if arg in _SEARCH_VALUE_OPTIONS else 1
            continue
        operands.append(arg)
        i += 1
    if not explicit_pattern:
        operands = operands[1:]
    return operands or ["."]


def _searches_process_tree(command: str, cwd: str | None = None) -> bool:
    """True when a recursive search starts at ``/``, ``/proc`` or a process dir.

    Roots are normalised the way the file-tool rules match paths (against
    ``cwd``, ``..`` collapsed, symlinks followed), so ``/proc/../proc`` and
    ``.`` run from ``/proc`` count.
    """
    import os

    from fluid.permissions.rules import path_candidates

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
        for root in _search_roots(args):
            raw, normalized = path_candidates(os.path.expanduser(root), cwd)
            for form in raw | normalized | {root}:
                path = form.replace("\\", "/").rstrip("/") or "/"
                if _PROCESS_TREE_RE.match(path):
                    return True
    return False


def touches_sensitive_data(command: str, cwd: str | None = None) -> bool:
    """True when a command reads a credential file or a process's environment.

    Such a call always asks under ``on_write`` (:func:`classify`) and is never
    covered by a session approval (:func:`scope_labels`).
    """
    return _touches_sensitive_path(command, cwd) or _searches_process_tree(command, cwd)


def classify(input: dict[str, Any], cwd: str | None = None) -> str:
    """Effective risk of one Bash tool input: ``safe`` or ``write``.

    ``cwd`` is the session's working directory, when the caller knows it.
    """
    command = input.get("command")
    if not isinstance(command, str):
        return WRITE
    if not is_read_only(command):
        return WRITE
    # Read-only, but targeting a credential file → keep the prompt.
    if touches_sensitive_data(command, effective_cwd(input, cwd)):
        return WRITE
    return SAFE


def location_dependent(labels: frozenset[str]) -> bool:
    """True when a label names a script or module by a path relative to a
    working directory the caller did not resolve (``python tool.py``): a grant
    for it would cover the same spelling run anywhere."""
    for label in labels:
        words = label.split(" ")
        while words and words[0] in WRAPPER_PROGRAMS | {"uv", "poetry", "pipx", "conda", "run"}:
            words = words[1:]
        if not words:
            continue
        program = words[0]
        if program not in INTERPRETERS and not _PYTHON_VERSIONED.match(program):
            continue
        if program == "deno" and len(words) == 2 and not _ABSOLUTE_LABEL_PATH.match(words[1]):
            continue  # ``deno fmt``: a tool, not a script
        if words[1:2] == ["-m"]:
            if not re.search(r" in (?:/|[A-Za-z]:/)", label):
                return True
        elif len(words) > 1 and not _ABSOLUTE_LABEL_PATH.match(words[1]):
            return True
    return False


_ABSOLUTE_LABEL_PATH = re.compile(r"^(?:/|[A-Za-z]:/)")
