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
``cat ?env``, ``cat ~/.ssh/*``). This check is best-effort: it reads literal
operands only, so a path carried in an option (``--from-file=.env``), built
while the command runs, or reached through a recursive search is not seen.
Bash is not a credential boundary; the isolated exec profile is.

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
- ``sed``: must carry ``-n``/``--quiet``/``--silent``, may carry only
  ``-E``/``-r``/``-s``/``-u``/``-z`` besides, and every script must be a print
  (``p``) or a ``/``-delimited substitution with ``g``/``p``/``i``/digit flags,
  each optionally addressed by line numbers or a ``/regex/``. Any other script
  (a ``w`` write, a GNU ``e`` command, another delimiter) is ``write``.
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

"Always allow" (:func:`session_scope`) is narrower still: only the fixed
forms in :data:`SCOPED_FORMS` get a scope, and the scope is the whole parsed
command, so a later call is covered only when it is the same command.

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
# The sed scripts the classifier accepts: an optional address (line numbers,
# ``$`` or a ``/regex/``, alone or as a range), then ``p`` or a ``/``-delimited
# ``s`` command whose flags cannot write (``w``) or run (``e``) anything.
_SED_REGEX = r"/(?:[^/\\\n]|\\[^\n])*/"
_SED_ADDRESS = rf"(?:\d+|\$|{_SED_REGEX})"
_SED_SAFE_SCRIPT = re.compile(
    rf"(?:{_SED_ADDRESS}(?:,{_SED_ADDRESS})?)?"
    rf"(?:p|s{_SED_REGEX}(?:[^/\\\n]|\\[^\n])*/[gpiI0-9]*)"
)
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
    """``sed -n`` with only print and substitute scripts (:data:`_SED_SAFE_SCRIPT`).

    Fail closed: an option outside the short list, a glued ``-eSCRIPT``, an
    abbreviated long option or a script the pattern does not match makes the
    command ``write``."""
    quiet = False
    scripts: list[str] = []
    operands: list[str] = []
    only_operands = False
    rest = iter(args)
    for a in rest:
        if only_operands or a == "-" or not a.startswith("-"):
            operands.append(a)
        elif a == "--":
            only_operands = True
        elif a.startswith("--"):
            name, eq, value = a.partition("=")
            if name in ("--quiet", "--silent") and not eq:
                quiet = True
            elif name == "--expression" and eq:
                scripts.append(value)
            else:
                return False
        else:
            for j, ch in enumerate(a[1:], 1):
                if ch == "n":
                    quiet = True
                elif ch == "e" and j == len(a) - 1:  # -e SCRIPT
                    script = next(rest, None)
                    if script is None:
                        return False
                    scripts.append(script)
                elif ch not in "Ersuz":
                    return False
    if not scripts and operands:
        scripts.append(operands[0])
    return quiet and bool(scripts) and all(_SED_SAFE_SCRIPT.fullmatch(s) for s in scripts)


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
    # file -C compiles a magic file and writes NAME.mgc. printf -v assigns a
    # shell variable (PATH included) for the commands after it.
    "find": _find_ok, "file": _no_flags("-C", "--compile", values="eFfmP"),
    "stat": None, "du": None, "df": None,
    "pwd": None, "echo": None, "printf": _no_flags("-v"), "true": None,
    "which": None, "where": None, "type": None,
    "env": _env_ok, "printenv": None,
    # tree -o writes its listing to a file; -R reruns it with -o 00Tree.html
    # in every directory.
    "date": _no_flags("-s", "--set", values="dfr", optional="I"),
    "whoami": None, "uname": None, "tree": _no_flags("-o", "-R", "--output"),
    "diff": None, "cmp": None,
    # sort -o writes; --compress-program runs a program.
    "sort": _no_flags("-o", "--output", "--compress-program", values="kStT"),
    "uniq": _uniq_ok, "cut": None, "tr": None,
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
    "npm": _package_manager("--logs-dir"), "pnpm": _package_manager_ok,
    "yarn": _package_manager_ok,
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


# "Always allow" covers only these fully parsed, non-destructive forms, and
# only the exact command answered: a scope is the whole parsed command, so
# ``git log --oneline`` and ``git log -p`` are different grants. Anything else
# (wrappers, interpreters, ``git commit``/``push``/``checkout``, a compound, an
# option not listed here) has no scope, so "always" acts as "once". A static
# parser cannot prove two different commands equivalent, nor that an
# arbitrary command is harmless, so the list stays small and fails closed.
_GIT_SCOPED_OPTIONS: dict[str, frozenset[str]] = {
    "status": frozenset({"-s", "--short", "-b", "--branch", "-sb", "--porcelain"}),
    "log": frozenset({
        "--oneline", "--stat", "--graph", "--decorate", "--name-only", "--name-status",
        "-p", "--patch",
    }),
    "diff": frozenset({
        "--stat", "--shortstat", "--cached", "--staged", "--name-only", "--name-status",
    }),
    "show": frozenset({"--stat", "--name-only", "--name-status", "--oneline"}),
}
_GIT_LOG_COUNT = re.compile(r"-\d+")
_SED_PRINT_LINES = re.compile(r"\d+(?:,(?:\d+|\$))?p")


def _operand(token: str) -> bool:
    return bool(token) and not token.startswith("-")


def _short_flags(token: str, allowed: str) -> bool:
    return len(token) > 1 and token[0] == "-" and token[1] != "-" and all(
        ch in allowed for ch in token[1:]
    )


def _git_scoped(args: list[str]) -> bool:
    # No global options; a ``rev:path`` or ``:path`` operand reads an object
    # (the staged copy of a file) rather than a working-tree path.
    if not args or args[0] not in _GIT_SCOPED_OPTIONS:
        return False
    allowed = _GIT_SCOPED_OPTIONS[args[0]]
    return all(
        a in allowed or a == "--" or (args[0] == "log" and _GIT_LOG_COUNT.fullmatch(a))
        or (_operand(a) and ":" not in a)
        for a in args[1:]
    )


SCOPED_FORMS: dict[str, Check] = {
    "git": _git_scoped,
    "pwd": lambda args: args in ([], ["-L"], ["-P"]),
    "ls": lambda args: all(_operand(a) or _short_flags(a, "laAhtrS1dF") for a in args),
    # No recursive, hidden-file or ignore-bypassing options; no -e/-f values.
    "grep": lambda args: all(_operand(a) or _short_flags(a, "nivclwFEHhoqsx") for a in args),
    "rg": lambda args: all(_operand(a) or _short_flags(a, "nivclwFHoSsx") for a in args),
    "sed": lambda args: len(args) >= 2 and args[0] == "-n"
    and bool(_SED_PRINT_LINES.fullmatch(args[1])) and all(_operand(a) for a in args[2:]),
}


# A word Bash passes through unchanged: no quoting, expansion, globbing,
# braces, escapes or a leading ``~``.
_LITERAL_WORD = re.compile(r"[A-Za-z0-9_./:,=@%+-]+")


def scoped_form(command: str) -> str | None:
    """The exact form an "always" for ``command`` covers, or None (one-shot).

    Only a single simple command on :data:`SCOPED_FORMS`, run by its bare
    program name, that classifies ``safe`` (so no redirection, substitution,
    assignment or credential operand) has a form, and only when every word is
    literal as typed, so the words checked are the argv Bash runs
    (``rg {--pre,x}`` and ``ls $NOTE`` have none). Words split on ASCII
    spaces and tabs only, so any other whitespace or control character
    (``ls foo\u00a0bar`` is one argument to Bash) leaves no form; the form is
    those same words joined by single spaces."""
    words = [w for w in re.split(r"[ \t]+", command) if w]
    if not words or not all(_LITERAL_WORD.fullmatch(w) for w in words):
        return None
    groups = _simple_command_tokens(command)
    if groups != [words] or classify({"command": command}) != SAFE:
        return None
    program, *args = words
    check = SCOPED_FORMS.get(program)
    if check is None or not check(args):
        return None
    return " ".join(words)


def session_scope(input: dict[str, Any]) -> frozenset[str] | None:
    """``allow_always`` scope for Bash: the exact command, for the forms on
    :data:`SCOPED_FORMS` only. None everywhere else, which hosts treat as
    allow once."""
    command = input.get("command")
    form = scoped_form(command) if isinstance(command, str) else None
    return None if form is None else frozenset({form})


def describe_session_scope(input: dict[str, Any]) -> str:
    """What answering "always" to this Bash call allows, in plain words."""
    scope = session_scope(input)
    if scope is None:
        return "this call only (this command cannot be allowed for the session)"
    return f"for the session: exactly `{next(iter(scope))}`"


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
        "proc/1/environ",
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
        for rep_path in _sensitive_example_paths():
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


# Public for the delegated leg's PreToolUse check: a best-effort literal test.
touches_sensitive_path = _touches_sensitive_path


def classify(input: dict[str, Any]) -> str:
    """Effective risk of one Bash tool input: ``safe`` or ``write``.

    The stricter of this module's checks and main's classifier, kept as it was
    in :mod:`._bash_risk_main`: these checks can only add an ask."""
    from fluid.tools.builtins import _bash_risk_main

    command = input.get("command")
    if not isinstance(command, str):
        return WRITE
    if _bash_risk_main.classify(input) == WRITE or not is_read_only(command):
        return WRITE
    # Read-only, but targeting a credential file → keep the prompt.
    return WRITE if _touches_sensitive_path(command) else SAFE
