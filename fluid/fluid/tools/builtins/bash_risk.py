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
- the shell builds no word the lexer cannot see (:func:`_hidden_expansion`):
  outside single quotes no parameter expansion at all (``$NAME``,
  ``"$NAME"``, ``${…}``, ``$1``, ``$_``, ``$?`` ...), no ANSI-C or locale
  quoting (``$'-exec'``, ``$"x"``) and no brace expansion (``{-exec,rm}``).
  Each of these turns into an option, a file name or extra words the checks
  below never saw; single-quoted awk and sed scripts are literal;
- every program is named bare: ``/usr/bin/grep`` or ``./cat`` is whatever
  file sits at that path, so it is ``write``;
- any leading ``VAR=value`` assignment names a variable in
  :data:`INERT_ENV_VARS`. Locale and colour variables cannot change what a
  program does; almost anything else can (``LD_PRELOAD``, ``GIT_EXTERNAL_DIFF``,
  ``GIT_PAGER``, ``BASH_ENV``, ``LESSOPEN`` all run a program of the caller's
  choosing), so an assignment outside the list makes the command ``write``.

A read-only command whose argument names a credential file (``cat .env``) or
a process's environment, command line or memory (``cat /proc/$PPID/environ``:
the parent is the host, whose environment holds the provider keys) is still
``write``; see :func:`_touches_sensitive_path`. Words are taken as the shell
passes them, quotes removed. An unquoted glob argument (``*``, ``?``,
``[``) asks: what it expands to is not in the text. A value attached to an
option counts too (``date --file=.env``), and relative arguments are also
read against the directory the command runs in (the call's ``cwd``, else the
session's: ``cat environ`` in ``/proc/1``); when that directory has a ``..``
component or sits under ``/proc`` or ``/dev/fd``, any relative argument
asks. A path through ``/proc/<pid>/fd``, ``cwd``, ``root`` or ``/dev/fd``
asks. Names are checked, not link targets: no file is looked at (this runs
on the event loop), so ``notes.txt`` linking to ``.env`` is not caught. It
is a preflight, not a boundary. ``echo``, ``printf`` and friends open
nothing, so their words get only the credential-name check.

A search or comparison across a directory tree prints whatever credential
file sits in it, so it is ``write``: recursive ``grep`` (any word with a
``-r``/``-R``/``-d`` letter, or a long option that could abbreviate
``--recursive``, ``--dereference-recursive`` or ``--directories``), and
``grep`` at all when ``GREP_OPTIONS`` is set (BSD grep reads options from
it). ``rg``, ``ag``, ``ack`` and ``diff`` are not in the table: rg searches
the tree by default and loads options (``--pre`` included) from
``RIPGREP_CONFIG_PATH``, and ``diff`` of two directories compares the files in
them (``diff -rN . empty`` prints every file). The built-in Grep tool is the
tree-wide search that withholds credential files.

Programs with extra rules:

- ``find``: none of ``-exec``, ``-execdir``, ``-ok``, ``-okdir``, ``-delete``,
  ``-fprint*``, ``-fls``.
- ``awk``/``gawk``/``mawk``: no ``system(``, ``getline`` or ``|`` in any
  argument, and no option but POSIX ``-F``/``-v`` (value attached or the next
  word) and ``--``: any other option asks, since awks disagree on them. The
  program text (the first operand) is lexed: a ``system`` call, ``getline``, a
  pipe or any ``@`` construct (``@include``, ``@load``, indirect ``@f()``)
  outside strings and regex literals asks, and so does a ``/`` the lexer
  cannot place as division or regex. See :func:`_awk_program_ok`.
- ``sed``: must carry ``-n``/``--quiet``/``--silent``, must not carry ``-i`` or
  ``--in-place`` or ``-f``/``--file`` (an unread script file) or ``-l`` (GNU
  and BSD sed disagree on whether it takes a value), and every script must
  parse as commands that only print or edit the pattern space;
  ``w``/``W``/``r``/``R``/``e``/``a``/``i``/``c``, an ``s`` flag other than
  ``g``/``p``/``i``/``m``/a number, or anything unparsed asks. See
  :func:`_sed_script_ok`.
- ``date``: display only, see :func:`_date_ok`: at most one operand, a
  ``+FORMAT``, and only ``-d``/``-u``/``-I``/``-R``/``--rfc-3339``/``-r``/``-f``
  (and their long names), ``--resolution``, ``--help`` and ``--version``.
- ``sort``: no ``-o``/``--output``, ``-T``/``--temporary-directory`` or
  ``--compress-program``; every letter of a short-option word counts. ``uniq``: at
  most one positional argument (the second is an output file). ``tree``: no
  ``-o`` or ``-R``. ``file``: no ``-C``/``--compile`` and no
  ``-p``/``--preserve-date`` (it resets the access time it read, a metadata
  write). ``env``: no arguments (``env cmd`` runs ``cmd``).

A banned option counts when attached (``-ofile``) or bundled (``-uo file``),
and for programs that parse long options with getopt_long (sort, file, sed,
date, gawk) when abbreviated to a prefix that names it unambiguously
(``--outp=file``; ``sort --c`` is ambiguous and getopt_long refuses it).
tree rejects abbreviations, so its long options match exactly.
Every word is scanned, ``--`` and option values included. See :func:`_no_flags`.
- ``git``: read-only subcommands only, listed in :data:`GIT_READ_ONLY` with
  their per-subcommand argument constraints; ``-C <dir>`` and ``--no-pager``
  global options are accepted, ``-c key=value`` is not (it can point git at an
  external program); any ``--output*`` argument is rejected. ``git diff``
  asks with ``--no-index`` or two or more operands (every word after ``--``
  is one) or a glob operand: given two paths outside the work tree (or run
  outside a repository) it compares them as plain directories, like
  ``diff -rN``.
- ``uv``/``pip``/``npm``/``pnpm``/``yarn``/``cargo``: only the informational
  subcommands in :data:`PACKAGE_MANAGER_READ_ONLY` (``uv pip list`` is handled
  by skipping the ``pip`` word).
- ``go``: only ``go version``. ``node``: only ``--version``/``-v``.

Everything is table-driven so the allowlist can grow without touching the
parser. Program names are matched case-insensitively, with a trailing
``.exe`` stripped.
"""

from __future__ import annotations

import os
import posixpath
import re
import shlex
from collections.abc import Callable
from dataclasses import dataclass
from fnmatch import fnmatchcase
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


def _legacy_banned(args: list[str], banned: tuple[str, ...]) -> bool:
    """The exact-token rule these checks started from, over every word.

    Kept under the stricter parsing below so that tightening a program's
    check can only add prompts, never drop one (``-o`` after ``--`` still asks).
    """
    return any(a in banned or a.split("=", 1)[0] in banned for a in args)


def _resolve_long(name: str, options: tuple[str, ...] | None) -> str | None:
    """The long option ``name`` (``--outp``) selects, or None.

    ``options`` None means the program matches long options exactly
    (ripgrep, tree), so ``name`` stands for itself. Otherwise getopt_long's
    rule: an exact name, else the one option ``name`` is a prefix of. None
    when it is a prefix of no option or of several: getopt_long refuses an
    ambiguous abbreviation, so ``sort --c`` (``--check`` or
    ``--compress-program``) runs nothing.
    """
    if options is None or name in options:
        return name
    matches = [o for o in options if o.startswith(name)]
    return matches[0] if len(matches) == 1 else None


def _sets_flag(
    arg: str, banned: tuple[str, ...], takes_value: str = "",
    long_options: tuple[str, ...] | None = None,
) -> bool:
    """True when ``arg`` sets one of the ``banned`` options.

    ``banned`` holds short (``-o``) and long (``--output``) names. A long one
    counts exactly, or abbreviated when ``long_options`` lists the program's
    real long options (see :func:`_resolve_long`). A short one counts on its
    own, with a value attached (``-ofile``) or bundled after other flags
    (``-uo file``, ``-uofile``). The scan of a bundle stops at the first
    letter in ``takes_value``: the rest of that word is the option's value,
    not flags (``sort -t,`` sets no ``-,``).
    """
    if arg.startswith("--"):
        name = arg.split("=", 1)[0]
        return len(name) > 2 and _resolve_long(name, long_options) in banned
    if arg.startswith("-") and len(arg) > 1:
        for ch in arg[1:]:
            if f"-{ch}" in banned:
                return True
            if ch in takes_value:
                return False
    return False


def _no_flags(
    *banned: str, takes_value: str = "", long_options: tuple[str, ...] | None = None,
) -> Check:
    """Check: none of ``banned`` is set, in any spelling :func:`_sets_flag` knows.

    Every word is scanned, ``--`` and option values included: which word a
    program reads as an option's value can differ by dialect (GNU sort's
    obsolete options take the next word only when it is a number), so an
    end-of-options early return could stop short of a real option.
    """
    def check(args: list[str]) -> bool:
        if _legacy_banned(args, banned):
            return False
        return not any(_sets_flag(a, banned, takes_value, long_options) for a in args)
    return check


def _find_ok(args: list[str]) -> bool:
    return not any(a in _FIND_EXEC_FLAGS or a.startswith("-fprint") for a in args)


# Long options that make grep descend into directories, or that could be
# abbreviated to one (getopt_long accepts any unambiguous prefix).
_GREP_RECURSIVE_LONG = ("--recursive", "--dereference-recursive", "--directories")


def _grep_ok(args: list[str]) -> bool:
    """No recursion: a tree-wide grep prints the lines of any ``.env`` in it.

    Deliberately blunt so no dialect slips through: any short-option word
    with an ``r``, ``R`` or ``d`` letter asks (``-d recurse``, ``-drec``,
    ``-inr``, even ``-e red``), and so does any long option that is a prefix
    of a recursion option. Every word is scanned, ``--`` included. BSD grep
    reads extra options from ``GREP_OPTIONS``, so grep asks when it is set.
    """
    if os.environ.get("GREP_OPTIONS"):
        return False
    for a in args:
        if a.startswith("--"):
            name = a.split("=", 1)[0]
            if len(name) > 2 and any(o.startswith(name) for o in _GREP_RECURSIVE_LONG):
                return False
        elif a.startswith("-") and any(ch in "rRd" for ch in a[1:]):
            return False
    return True


# Real long options, for getopt_long abbreviation (GNU coreutils / libmagic).
_SORT_LONG_OPTIONS = (
    "--batch-size", "--buffer-size", "--check", "--compress-program", "--debug",
    "--dictionary-order", "--field-separator", "--files0-from", "--general-numeric-sort",
    "--help", "--human-numeric-sort", "--ignore-case", "--ignore-leading-blanks",
    "--ignore-nonprinting", "--key", "--merge", "--month-sort", "--numeric-sort",
    "--output", "--parallel", "--random-sort", "--random-source", "--reverse", "--sort",
    "--stable", "--temporary-directory", "--unique", "--version", "--version-sort",
    "--zero-terminated",
)
_FILE_LONG_OPTIONS = (
    "--apple", "--brief", "--checking-printout", "--compile", "--debug", "--dereference",
    "--exclude", "--exclude-quiet", "--extension", "--files-from", "--help", "--keep-going",
    "--list", "--magic-file", "--mime", "--mime-encoding", "--mime-type", "--no-buffer",
    "--no-dereference", "--no-pad", "--no-sandbox", "--parameter", "--preserve-date",
    "--print0", "--raw", "--separator", "--special-files", "--uncompress",
    "--uncompress-noreport", "--version",
)

# How a long option takes its value: "none", "required" (``=v`` or the next
# word) or "optional" (``=v`` only). The option parsers below need this to
# tell an option's value from the program text or an operand.
_DATE_LONG_OPTIONS = {
    "--date": "required", "--debug": "none", "--file": "required", "--help": "none",
    "--iso-8601": "optional", "--reference": "required", "--resolution": "none",
    "--rfc-3339": "required", "--rfc-email": "none", "--set": "required",
    "--universal": "none", "--utc": "none", "--version": "none",
}
_DATE_DISPLAY_LONG = frozenset({
    "--date", "--file", "--iso-8601", "--reference", "--rfc-3339", "--rfc-email",
    "--universal", "--utc", "--help", "--version", "--resolution",
})


def _long_value(
    arg: str, args: list[str], i: int, table: dict[str, str],
) -> tuple[str, str, int] | None:
    """Parse long option ``arg`` against ``table``: (option, value, next index).

    ``i`` indexes the word after ``arg``; a required value not given with
    ``=`` is that word. None when the option is unknown or an ambiguous
    abbreviation, takes no value but was given one, or lacks its value.
    """
    name, eq, value = arg.partition("=")
    option = _resolve_long(name, tuple(table))
    if option is None:
        return None
    kind = table[option]
    if kind == "none" and eq:
        return None
    if kind == "required" and not eq:
        if i >= len(args):
            return None
        value, i = args[i], i + 1
    return option, value, i


def _date_ok(args: list[str]) -> bool:
    """Display forms only: ``date [-u] [-d STR|-r FILE|-f FILE] [+FORMAT]``.

    Any operand other than one ``+FORMAT`` (``date MMDDhhmm`` sets the clock)
    and any option outside the read-only set asks.
    """
    if _legacy_banned(args, ("-s", "--set")):
        return False
    operands: list[str] = []
    i = 0
    options_done = False
    while i < len(args):
        a = args[i]
        i += 1
        if options_done or a == "-" or not a.startswith("-"):
            operands.append(a)
        elif a == "--":
            options_done = True
        elif a.startswith("--"):
            parsed = _long_value(a, args, i, _DATE_LONG_OPTIONS)
            if parsed is None or parsed[0] not in _DATE_DISPLAY_LONG:
                return False
            i = parsed[2]
        else:
            for j, ch in enumerate(a[1:], 1):
                if ch in "uR":
                    continue
                if ch == "I":  # optional value, attached only (-Iseconds)
                    break
                if ch in "dfr":  # required value: the rest of the word or the next
                    if j == len(a) - 1:
                        if i >= len(args):
                            return False
                        i += 1
                    break
                return False
    return len(operands) <= 1 and all(o.startswith("+") for o in operands)


# The only options awk may carry: POSIX -F and -v, value attached (-F:) or
# the next word (-F :), and ``--``. Every other option asks. GNU-only ones
# are the risk: gawk's -e/--source supply the program, -f/-E/-i/-l/-W read
# code the classifier never sees, -d/-p/-o write files, and an awk that does
# not know an option (Apple's One True Awk warns and skips it) would run a
# different word as its program than the classifier lexed.
# The exact-token rule awk started from, kept over every word.
_AWK_FILE_FLAGS = {"-f", "--file", "-i", "--include", "-l", "--load"}


def _awk_ok(args: list[str]) -> bool:
    for a in args:
        if "system(" in a or "getline" in a or "|" in a:
            return False
        if a.split("=", 1)[0] in _AWK_FILE_FLAGS or (
            a.startswith(("-f", "-i", "-l")) and not a.startswith("--") and len(a) > 2
        ):
            return False
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            i += 1
            break
        if a == "-" or not a.startswith("-"):
            break
        if a[:2] not in ("-F", "-v"):
            return False
        i += 2 if len(a) == 2 else 1  # a separate value is the next word
        if i > len(args):
            return False
    operands = args[i:]
    if not operands:
        return True  # no program: awk prints its usage
    # The first operand is the program. An option-looking operand, the program
    # included (after ``--``), asks rather than rest on how each awk reads it.
    if any(o.startswith("-") and o != "-" for o in operands):
        return False
    return _awk_program_ok(operands[0])


_AWK_KEYWORDS = frozenset({
    "BEGIN", "END", "BEGINFILE", "ENDFILE", "function", "func", "if", "else", "while",
    "for", "do", "break", "continue", "next", "nextfile", "exit", "return", "delete",
    "in", "getline", "print", "printf", "case", "default", "switch", "length",
})
_AWK_CONTROL = frozenset({"if", "while", "for", "switch"})
# After these an operand is expected, so ``/`` opens a regex literal.
_AWK_REGEX_AFTER = frozenset({
    "", "(", ",", "{", "}", ";", "\n", "!", "~", "&&", "||",
    "=", "+", "-", "*", "%", "^", "<", "?", ":",
})
# After these an operand just ended, so ``/`` divides.
_AWK_DIVIDE_AFTER = frozenset({"name", "number", "string", "regex", "]", ")", "++", "--"})
_AWK_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_AWK_NUMBER = re.compile(r"\.?[0-9][0-9A-Za-z_.]*")


def _awk_literal_end(text: str, i: int, close: str, brackets: bool) -> int | None:
    """Index just past the literal opened before ``i`` and closed by ``close``.

    Backslash escapes the next character. With ``brackets``, a ``close``
    inside a bracket expression (``/[/]/``) does not end it, as in gawk. None
    when the literal does not end on its line.
    """
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "\n":
            return None
        if ch == close:
            return i + 1
        if brackets and ch == "[":
            j = i + 1
            if j < n and text[j] == "^":
                j += 1
            if j < n and text[j] == "]":
                j += 1
            while j < n and text[j] != "]":
                if text[j] == "\n":
                    return None
                if text[j] == "[" and text[j + 1:j + 2] in (":", "=", "."):
                    end = text.find(text[j + 1] + "]", j + 2)
                    if end < 0:
                        return None
                    j = end + 2
                    continue
                j += 1
            if j >= n:
                return None
            i = j
        i += 1
    return None


def _awk_program_ok(text: str) -> bool:
    """True when an awk program cannot run a command, write a file or load code.

    A small lexer: strings, regex literals and comments are skipped, so
    ``"a@b"``, ``/@example\\.com$/`` and a field named ``system_name`` pass.
    Outside them a ``system`` or ``getline`` name, ``|`` (pipes), ``>``
    (output redirection), ``@`` (gawk's ``@include``/``@load``/``@namespace``
    and indirect calls like ``@f()``, which reach ``system`` without spelling
    it) or a character awk has no use for asks. ``/`` is division or a regex
    depending on the grammar; where the previous token does not settle it
    (after a keyword such as ``print``, after ``if (…)``) the program asks.
    """
    prev = ""  # previous significant token's class: "", an operator, "name", ...
    parens: list[str] = []  # for each open "(", the token before it
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch in " \t\r":
            i += 1
        elif ch == "\\" and text[i + 1:i + 2] in ("\n", "\r"):
            i += 2  # line continuation
        elif ch == "#":
            end = text.find("\n", i)
            i = n if end < 0 else end
        elif ch == "\n":
            prev, i = "\n", i + 1
        elif ch == '"':
            end = _awk_literal_end(text, i + 1, '"', brackets=False)
            if end is None:
                return False
            prev, i = "string", end
        elif ch == "/":
            if prev in _AWK_DIVIDE_AFTER:
                prev, i = "/", i + 1
                continue
            if prev not in _AWK_REGEX_AFTER:
                return False  # cannot tell a regex literal from division
            end = _awk_literal_end(text, i + 1, "/", brackets=True)
            if end is None or end != _awk_literal_end(text, i + 1, "/", brackets=False):
                return False  # an awk without bracket handling would end it elsewhere
            prev, i = "regex", end
        elif m := _AWK_NAME.match(text, i):
            word = m.group()
            if word in ("system", "getline"):
                return False
            prev = word if word in _AWK_KEYWORDS else "name"
            i = m.end()
        elif m := _AWK_NUMBER.match(text, i):
            prev, i = "number", m.end()
        elif text.startswith(("&&", "||", "++", "--"), i):
            if text[i] == "|":
                return False
            prev, i = text[i:i + 2], i + 2
        elif ch == "(":
            parens.append(prev if prev in _AWK_CONTROL else "")
            prev, i = "(", i + 1
        elif ch == ")":
            # ``if (x) /re/`` starts a statement; ``(a) / 2`` divides.
            prev = "control)" if parens and parens.pop() else ")"
            i += 1
        elif ch in "{};,!~=+-*%^<?:[]$":
            prev, i = ch, i + 1
        else:
            return False  # @, |, >, & and anything else awk does not take here
    return True


# sed's long options (GNU) and how each takes its value.
_SED_LONG_OPTIONS = {
    "--debug": "none", "--expression": "required", "--file": "required",
    "--follow-symlinks": "none", "--help": "none", "--in-place": "optional",
    "--line-length": "required", "--null-data": "none", "--posix": "none",
    "--quiet": "none", "--regexp-extended": "none", "--sandbox": "none",
    "--separate": "none", "--silent": "none", "--unbuffered": "none",
    "--version": "none",
}


def _sed_tokens_ok(args: list[str]) -> bool:
    """The word-level rule sed started from, kept so the parser only adds prompts."""
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


def _sed_ok(args: list[str]) -> bool:
    if not _sed_tokens_ok(args):
        return False
    quiet = False
    scripts: list[str] = []
    operands: list[str] = []
    i = 0
    options_done = False
    while i < len(args):
        a = args[i]
        i += 1
        if options_done or a == "-" or not a.startswith("-"):
            operands.append(a)
            continue
        if operands:
            # GNU sed reads options after operands; BSD sed stops at the
            # first one, so the two would disagree on which word is the script.
            return False
        if a == "--":
            options_done = True
            continue
        if a.startswith("--"):
            parsed = _long_value(a, args, i, _SED_LONG_OPTIONS)
            if parsed is None or parsed[0] in ("--in-place", "--file"):
                return False
            option, value, i = parsed
            if option in ("--quiet", "--silent"):
                quiet = True
            elif option == "--expression":
                scripts.append(value)
            continue
        for j, ch in enumerate(a[1:], 1):
            if ch == "n":
                quiet = True
            elif ch in "rzuEsb":
                # Switches in GNU sed; -E/-r/-u are switches in BSD/macOS sed
                # too, and -z/-s/-b are errors there, which run nothing.
                continue
            elif ch == "e":  # script: the rest of the word or the next
                value = a[j + 1:]
                if not value:
                    if i >= len(args):
                        return False
                    value, i = args[i], i + 1
                scripts.append(value)
                break
            else:
                # -i, -f, anything unknown, and -l: GNU sed takes a value
                # after -l, BSD/macOS sed does not, so the two would read a
                # different word as the script.
                return False
    if not scripts and operands:
        scripts = [operands[0]]  # no -e: the first operand is the script
    return quiet and all(_sed_script_ok(s) for s in scripts)


# sed commands that only print, move or edit the pattern/hold space.
_SED_PLAIN_COMMANDS = frozenset("pP=dDgGhHxnNzF")
_SED_NUMBER_COMMANDS = frozenset("lqQ")  # optional number
_SED_LABEL_COMMANDS = frozenset("btT")  # optional label
_SED_S_FLAGS = frozenset("gpiImM0123456789")
_SED_LABEL = re.compile(r"[A-Za-z0-9_]*")


def _sed_regex_end(script: str, i: int, delim: str) -> int | None:
    """Index past the regex closing at ``delim``; None if seds could disagree.

    Both a bracket-aware and a bracket-blind reading must end the regex at
    the same place. Deliberately conservative: a ``delim`` inside a bracket
    expression (``s/[/]/x/``) asks rather than rest on which reading each
    sed and version uses; the prompt is the cheaper mistake.
    """
    end = _awk_literal_end(script, i, delim, brackets=False)
    if end is None or end != _awk_literal_end(script, i, delim, brackets=True):
        return None
    return end


def _sed_address(script: str, i: int, second: bool) -> int | None:
    """Index past one address at ``i`` (possibly empty), or None if unparsed."""
    n = len(script)
    if i < n and second and script[i] in "+~":
        i += 1
        if i >= n or not script[i].isdigit():
            return None
    if i < n and script[i].isdigit():
        while i < n and script[i].isdigit():
            i += 1
        if not second and i < n and script[i] == "~":  # GNU first~step
            i += 1
            while i < n and script[i].isdigit():
                i += 1
    elif i < n and script[i] == "$":
        i += 1
    elif i < n and script[i] == "/":
        end = _sed_regex_end(script, i + 1, "/")
        if end is None:
            return None
        i = end
        while i < n and script[i] in "IM":
            i += 1
    elif i < n and script[i] == "\\":
        return None  # \cREGEXc custom delimiter: not parsed
    return i


def _sed_script_ok(script: str) -> bool:
    """True when every command in a sed script only prints or edits text.

    A conservative parser for the common subset: addresses (numbers, ``$``,
    ``/re/``, ``first~step``, ``+N``), ``!``, blocks, comments, labels, the
    commands in :data:`_SED_PLAIN_COMMANDS`, ``l``/``q``/``Q`` with a number,
    ``b``/``t``/``T``, and ``s``/``y``. ``w``/``W`` and ``s///w`` write files,
    ``e`` and ``s///e`` run commands, ``r``/``R`` read files the credential
    check never sees; those, ``a``/``i``/``c`` text and anything else the
    parser does not know ask.
    """
    i, n = 0, len(script)
    while True:
        while i < n and script[i] in " \t\n;}":
            i += 1
        if i >= n:
            return True
        if script[i] == "#":
            end = script.find("\n", i)
            i = n if end < 0 else end
            continue
        start = i
        i = _sed_address(script, i, second=False)
        if i is not None and i > start and i < n and script[i] == ",":
            i = _sed_address(script, i + 1, second=True)
        if i is None:
            return False
        while i < n and script[i] in " \t!":
            i += 1
        if i >= n:
            return False
        cmd = script[i]
        i += 1
        if cmd == "{":
            continue
        if cmd in _SED_NUMBER_COMMANDS:
            while i < n and script[i] in " \t":
                i += 1
            while i < n and script[i].isdigit():
                i += 1
        elif cmd in _SED_LABEL_COMMANDS or cmd == ":":
            while i < n and script[i] in " \t":
                i += 1
            label = _SED_LABEL.match(script, i)
            if cmd == ":" and not label.group():
                return False
            i = label.end()
        elif cmd in "sy":
            delim = script[i:i + 1]
            if not delim or delim.isalnum() or delim in " \t\n\\}":
                return False
            i = (_sed_regex_end(script, i + 1, delim) if cmd == "s"
                 else _awk_literal_end(script, i + 1, delim, brackets=False))
            if i is None:
                return False
            i = _awk_literal_end(script, i, delim, brackets=False)
            if i is None:
                return False
            if cmd == "s":
                while i < n and script[i] in _SED_S_FLAGS:
                    i += 1
        elif cmd not in _SED_PLAIN_COMMANDS:
            return False
        while i < n and script[i] in " \t":
            i += 1
        if i < n and script[i] not in ";\n}#":
            return False  # w/e flag, a filename, or extra text after a command


def _uniq_ok(args: list[str]) -> bool:
    # A second positional is the output file. ``-`` (stdin) and anything
    # after ``--`` are positionals too: ``uniq - out`` writes ``out``.
    positional = 0
    options_done = False
    for a in args:
        if options_done or a == "-" or not a.startswith("-"):
            positional += 1
        elif a == "--":
            options_done = True
    return positional <= 1


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
    "grep": _grep_ok, "egrep": _grep_ok, "fgrep": _grep_ok,
    # No rg: it searches the whole tree unless given files, and options from
    # its config file (RIPGREP_CONFIG_PATH) can add --pre, which runs a program.
    "find": _find_ok,
    # -C compiles a magic file; -p resets the access time after reading.
    "file": _no_flags(
        "-C", "--compile", "-p", "--preserve-date",
        takes_value="efFmP", long_options=_FILE_LONG_OPTIONS,
    ),
    "stat": None, "du": None, "df": None,
    "pwd": None, "echo": None, "printf": None, "true": None,
    "which": None, "where": None, "type": None,
    "env": _env_ok, "printenv": None,
    "date": _date_ok, "whoami": None, "uname": None,
    # tree -o writes its listing to a file; -R (with -H) writes 00Tree.html
    # into every directory. tree takes option values from the next word, so
    # every letter of a bundle is a flag. tree has its own parser, not
    # getopt, so every word is scanned, ``--`` or not.
    "tree": _no_flags("-o", "-R"),
    # No diff: given two directories it compares the files in them, so
    # ``diff -rN . empty`` prints every file, .env included. cmp refuses
    # a directory.
    "cmp": None,
    # sort writes with -o, runs a helper with --compress-program and spills
    # into a chosen directory with -T. Which bundled letters take a value
    # differs between sorts, so every letter of a short-option word counts,
    # attached values included (a false ask beats a missed -o). Spill into
    # the default temp directory, with no -T, stays read-only.
    "sort": _no_flags(
        "-o", "-T", "--output", "--compress-program", "--temporary-directory",
        long_options=_SORT_LONG_OPTIONS,
    ),
    "uniq": _uniq_ok, "cut": None, "tr": None,
    "awk": _awk_ok, "gawk": _awk_ok, "mawk": _awk_ok,
    "sed": _sed_ok,
    "basename": None, "dirname": None, "realpath": None, "readlink": None,
    "jq": None,
}

def _git_diff_ok(args: list[str]) -> bool:
    """Not the plain-directory form: ``--no-index``, or two operands.

    git compares two paths as ``diff -rN`` would when one lies outside the
    work tree or the command runs outside a repository, which the text does
    not show; both need exactly two operands. Every word after ``--`` is an
    operand, dashes or not, and a glob may expand to several, so one asks.
    Option values count as operands here (``-S foo a``), which can only add
    a prompt.
    """
    for a in args:
        name = a.split("=", 1)[0]
        if len(name) >= 5 and "--no-index".startswith(name):
            return False
    if "--" in args:
        cut = args.index("--")
        operands = [a for a in args[:cut] if not a.startswith("-")] + args[cut + 1:]
    else:
        operands = [a for a in args if not a.startswith("-")]
    if any(ch in a for a in operands for ch in "*?["):
        return False
    return len(operands) < 2


# git subcommand → check over the subcommand's own arguments.
GIT_READ_ONLY: dict[str, Check | None] = {
    "status": None, "log": None, "diff": _git_diff_ok, "show": None,
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


_WORD_END = frozenset(" \t\r\n;&|<>()")


def _brace_expands(command: str, i: int) -> bool:
    """True when the unquoted ``{`` at ``i`` could open a brace expansion.

    Any ``,`` or ``..`` later in the same word counts, quoted or not; a
    quoted comma does not split, so that only adds a prompt.
    """
    quote = ""
    i += 1
    while i < len(command):
        ch = command[i]
        if quote:
            if ch == quote:
                quote = ""
        elif ch == "\\":
            i += 1
        elif ch in "'\"":
            quote = ch
        elif ch in _WORD_END:
            return False
        if ch == "," or command.startswith("..", i):
            return True
        i += 1
    return False


def _hidden_expansion(command: str) -> bool:
    """True when the shell would build a word the classifier cannot see.

    The per-program checks read the words as written, but the shell expands
    ``$X`` / ``"$X"`` / ``${X:--exec}`` to whatever the variable holds (an
    option, a path, several words), ``$'-exec'`` to ``-exec``, and
    ``{-exec,rm}`` to two words: ``find . ${X:--exec} rm {} +`` runs ``rm``.
    So outside single quotes any parameter expansion (``$NAME``, ``${…}``,
    ``$1``, ``$@``, ``$?``, ``$$``, ``$[…]``), ANSI-C or locale quoting, and an
    unquoted brace expansion refuse the fast path. Single-quoted text is
    literal, so awk and sed scripts keep their ``$1``.
    """
    quote = ""
    i, n = 0, len(command)
    while i < n:
        ch = command[i]
        if quote == "'":
            if ch == "'":
                quote = ""
        elif ch == "\\":
            i += 1  # the next character is literal
        elif ch == '"':
            quote = "" if quote else '"'
        elif ch == "'" and not quote:
            quote = "'"
        elif ch == "{" and not quote and _brace_expands(command, i):
            return True
        elif ch == "$":
            nxt = command[i + 1:i + 2]
            if nxt in ("'", '"'):
                if not quote:
                    return True  # $'…' / $"…"; inside "…" the $ is literal
            elif nxt and (nxt == "_" or nxt.isalnum() or nxt in "{@*#?-$![("):
                return True
        i += 1
    return False


def _simple_command_ok(tokens: list[str]) -> bool:
    # Leading VAR=value assignments pass only for inert variables; a bare
    # assignment is not a command.
    while tokens and _ENV_ASSIGNMENT.match(tokens[0]):
        if not _inert_assignment(tokens[0]):
            return False
        tokens = tokens[1:]
    if not tokens:
        return False
    # Only a bare name runs the program the table means: ``./grep`` or
    # ``/tmp/x/cat`` is whatever file sits there, whatever its basename.
    if "/" in tokens[0] or "\\" in tokens[0]:
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
    if _hidden_expansion(command):
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
# ``make build`` and ``make clean``.
SUBCOMMAND_PROGRAMS = frozenset((
    "npm", "npx", "pnpm", "yarn", "bun", "bunx", "uv", "uvx", "pip", "pip3",
    "pipx", "poetry", "cargo", "go", "make", "docker", "podman", "kubectl",
    "gh", "gcloud", "aws", "az", "terraform", "helm",
))


def _scope_label(tokens: list[str]) -> str | None:
    """The approval label for one simple command's tokens.

    A wrapper labels as ``wrapper <label of the wrapped command>``; a
    subcommand program as ``program subcommand``; anything else as the
    program alone. None when a wrapper carries options (``sudo -u root``):
    which token is the wrapped program is then not knowable from the text,
    and a guess would scope the approval wrongly.
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
    if program in SUBCOMMAND_PROGRAMS:
        if rest and not rest[0].startswith("-"):
            return f"{program} {rest[0].lower()}"
        return program
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

    Plain programs label as themselves (``git``); wrappers and subcommand
    programs carry what they run (``sudo apt``, ``npm run``). None when any
    part cannot be labelled, so the approval is one-shot rather than wide.
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


# Concrete names the sensitive-file globs in fluid.tools.hardening cover, so an
# argument glob can be tested against them: ``?env`` matches ``.env``,
# ``~/.ssh/*`` matches ``.ssh/config``.
_SENSITIVE_EXAMPLE_NAMES = (
    ".env", ".env.local", "x.pem", "x.p12", "x.pfx",
    "id_rsa", "id_rsa.pub", "id_ed25519", "id_ed25519.pub", "id_ecdsa", "id_ecdsa.pub",
    ".netrc", "_netrc", ".npmrc", ".pypirc", ".htpasswd", ".git-credentials",
    "managed-postgres.json",
)
_SENSITIVE_EXAMPLE_PATHS = (
    ".aws/credentials", ".ssh/config",
    ".claude/.credentials.json", ".config/gh/hosts.yml",
    ".credential-keys/server.key", "chrome-profile/Default/Cookies",
)
_GLOB_CHARS = frozenset("*?[{")
_BRACE_RE = re.compile(r"\{([^{}]*)\}")


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


_SHELL_PARAMETER = re.compile(r"\$(\{[^}]*\}?|[A-Za-z0-9_]+|.)?")


def _could_name_process_secret(path: str) -> bool:
    """True when ``path`` could be a process's environ, cmdline or mem file.

    Matched on the last component alone, wherever the directory part points:
    ``/proc/$PPID/environ``, ``/proc/self/task/1/environ`` and
    ``/dev/fd/../../1/environ`` (``/dev/fd`` is ``/proc/self/fd``) all reach a
    process, and the shell resolves ``..`` after symlinks, so the directory
    spelling proves nothing. A word without a ``/`` is a pattern or a file
    in the working directory, not a /proc path. Globs, braces and ``$NAME``
    in the last component count as wildcards (``/proc/self/e*``,
    ``/proc/1/{environ,x}``, ``/proc/1/$F``).
    """
    from fluid.tools.hardening import SENSITIVE_PROCESS_FILES

    normalized = path.replace("\\", "/").rstrip("/")
    if "/" not in normalized:
        return False
    last = _SHELL_PARAMETER.sub("*", normalized.rsplit("/", 1)[-1].lower())
    alternatives = _expand_braces(last)
    if alternatives is None:
        # Unbalanced (the shell keeps it literal: awk's ``'{print $1/2}'``)
        # or too many to list: braces and commas as wildcards over-match.
        alternatives = [re.sub(r"[{},]", "*", last)]
    return any(
        fnmatchcase(name, alt) for alt in alternatives for name in SENSITIVE_PROCESS_FILES
    )


# Programs that never open their operands: ``echo "progress/mem"`` prints
# text. Their words get only the credential-name check (as before the /proc
# rules existed), not those.
_NO_FILE_OPERANDS = frozenset({"echo", "printf", "true", "false", "basename", "dirname"})
# Paths that reach a file through another process's descriptor or directory
# (``/proc/1/fd/3``, ``/proc/1/cwd/x``, ``/dev/fd/3``). Where they point
# depends on the process reading them, so they ask.
_INDIRECTION_GLOBS = tuple(
    f"*/proc/*/{d}/*" for d in ("fd", "fdinfo", "cwd", "root", "map_files")
) + ("*/dev/fd/*",)
_OPERATOR_CHARS = frozenset(";&|()<>\n")


@dataclass(frozen=True)
class _Word:
    """One shell word after quote removal, with what was unquoted in it."""

    text: str
    glob: bool  # an unquoted * ? or [: the shell expands it
    brace: bool  # an unquoted {


def _shell_words(command: str) -> list[_Word | None] | None:
    """The words the shell passes, with None for each operator run.

    Follows sh quoting for the subset :func:`is_read_only` lets through (no
    expansions, no substitutions): single quotes are literal, a backslash
    escapes the next character (inside double quotes only ``$``, a backquote,
    ``"``, ``\\`` and a newline), and an unquoted newline or any of
    ``;&|()<>`` separates, so ``"example folder"/a.txt`` is one word. A ``#``
    stays an ordinary character, which can only add words to check. None
    (ask) when a quote is left open or an unquoted backslash escapes a blank
    or ends a line: quote such names instead.
    """
    out: list[_Word | None] = []
    chars: list[str] = []
    glob = brace = started = False
    quote = ""

    def end() -> None:
        nonlocal chars, glob, brace, started
        if started:
            out.append(_Word("".join(chars), glob, brace))
        chars, glob, brace, started = [], False, False, False

    i, n = 0, len(command)
    while i < n:
        ch = command[i]
        if quote == "'":
            if ch == "'":
                quote = ""
            else:
                chars.append(ch)
        elif quote == '"':
            if ch == '"':
                quote = ""
            elif ch == "\\" and command[i + 1:i + 2] in ("$", "`", '"', "\\", "\n"):
                i += 1
                if command[i] != "\n":
                    chars.append(command[i])
            else:
                chars.append(ch)
        elif ch == "\\":
            i += 1
            if i >= n or command[i] in " \t\r\n":
                return None  # an escaped blank or line join: ask, as main did
            chars.append(command[i])
            started = True
        elif ch in "'\"":
            quote, started = ch, True
        elif ch in " \t\r":
            end()
        elif ch in _OPERATOR_CHARS:
            end()
            if out and out[-1] is not None:
                out.append(None)
        else:
            glob = glob or ch in "*?["
            brace = brace or ch == "{"
            chars.append(ch)
            started = True
        i += 1
    if quote:
        return None
    end()
    return out


def _through_indirection(path: str) -> bool:
    lowered = "/" + path.replace("\\", "/").lower().rstrip("/") + "/"
    return any(fnmatchcase(lowered, g) for g in _INDIRECTION_GLOBS)


def _unsure_cwd(cwd: str) -> bool:
    """True when names relative to ``cwd`` cannot be judged by spelling.

    A ``..`` component resolves after symlinks, so the directory it lands in
    is not the one the text shows; under ``/proc`` or ``/dev/fd`` a relative
    name reaches another process's files.
    """
    lowered = cwd.replace("\\", "/").lower().rstrip("/")
    if ".." in lowered.split("/"):
        return True
    return any(lowered == d or lowered.startswith(d + "/") for d in ("/proc", "/dev/fd"))


def _touches_sensitive_path(command: str, bases: list[str] | None = None) -> bool:
    """True when any argument could name a credential file (``cat .env`` etc.).

    A read-only command is still an exfiltration read when its target is a
    credential file or a process's environment, so it must not sail under the
    on_write policy. Only the text is read: no file is looked at, so a link
    named ``notes.txt`` that points at ``.env`` is not caught (a known gap).

    Each word is taken as the shell passes it (:func:`_shell_words`). An
    unquoted glob asks: what it matches is not in the text. Otherwise the
    word is checked as written and with ``..`` folded, at every suffix of an
    option word (``--file=.env``, ``-f.env``), after a ``:`` (``git show
    HEAD:.env``) and joined to each of ``bases`` (``cat environ`` run in
    ``/proc/1``). A path through a descriptor or another process's directory
    asks, and so does any relative word when a base is unsure
    (:func:`_unsure_cwd`). Words given to :data:`_NO_FILE_OPERANDS` programs
    are text, not paths: they get the credential-name check alone.
    """
    words = _shell_words(command)
    if words is None:
        return True
    bases = [b.replace("\\", "/") for b in bases or []]
    unsure = any(_unsure_cwd(b) for b in bases)
    text = False
    at_program = True
    for word in words:
        if word is None:
            text, at_program = False, True
            continue
        if at_program and not _ENV_ASSIGNMENT.match(word.text):
            at_program = False
            name = word.text
            text = "/" not in name and "\\" not in name and _program_name(name) in _NO_FILE_OPERANDS
        elif _word_names_secret(word, bases, text=text, unsure=unsure):
            return True
    return False


def _word_names_secret(word: _Word, bases: list[str], *, text: bool, unsure: bool) -> bool:
    """True when one word could name a secret; ``text`` for a word printed, not opened."""
    from fluid.tools.hardening import is_sensitive_path

    literal = word.text
    if not text:
        if word.glob:
            return True
        if unsure and not literal.startswith(("/", "~")):
            return True
    if literal.startswith("-"):
        paths = [literal[k:] for k in range(len(literal))]
    else:
        paths = [literal]
    # git's <rev>:<path> (``git show HEAD:.env``) names a file too.
    paths += [p.split(":", 1)[1] for p in paths if ":" in p]
    spelled = list(paths)
    paths += [posixpath.join(base, p) for base in bases for p in spelled]
    paths += [posixpath.normpath(p) for p in list(paths) if p]
    for path in paths:
        if text:
            if is_sensitive_path(path):
                return True
        elif (
            is_sensitive_path(path)
            or _could_name_process_secret(path)
            or _through_indirection(path)
        ):
            return True
        if (word.glob or word.brace) and _glob_could_name_sensitive_path(path):
            return True
    return False


def classify(input: dict[str, Any]) -> str:
    """Effective risk of one Bash tool input: ``safe`` or ``write``.

    The Bash tool runs in the call's ``cwd``, else the session's, which
    :meth:`ToolSpec.effective_risk` passes under ``SESSION_CWD_KEY``; relative
    names are checked against it. Pure text work: no file is opened or
    stat'ed, because this runs on the event loop.
    """
    from fluid.tools.registry import SESSION_CWD_KEY

    command = input.get("command")
    if not isinstance(command, str):
        return WRITE
    if not is_read_only(command):
        return WRITE
    call = input.get("cwd")
    call = call if isinstance(call, str) and call else None
    session = input.get(SESSION_CWD_KEY)
    session = session if isinstance(session, str) and session else None
    bases = [b for b in (call or session,) if b]
    if call and session:
        bases.append(posixpath.join(session.replace("\\", "/"), call.replace("\\", "/")))
    # Read-only, but targeting a credential file → keep the prompt.
    return WRITE if _touches_sensitive_path(command, bases) else SAFE
