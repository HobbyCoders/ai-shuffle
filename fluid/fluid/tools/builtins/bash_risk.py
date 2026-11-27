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
  that program's extra check (if any), and is named bare: a program written
  as a path (``./ls``, ``scripts/cat``, ``/usr/bin/grep``, ``C:\\bin\\git.exe``)
  runs whatever file is there, so it is ``write``;
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
``cat ?env``, ``cat ~/.ssh/*``).

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
  ``-o`` or ``-R``. ``file``: no ``-C``/``--compile``. ``env``: no arguments
  (``env cmd`` runs ``cmd``). ``rg``: no ``--pre`` or ``--hostname-bin`` (both
  run a command).

A banned option counts when attached (``-ofile``) or bundled (``-uo file``),
and for programs that parse long options with getopt_long (sort, file, sed,
date, gawk) when abbreviated to a prefix that names it unambiguously
(``--outp=file``; ``sort --c`` is ambiguous and getopt_long refuses it).
ripgrep and tree reject abbreviations, so their long options match exactly.
Every word is scanned, ``--`` and option values included. See :func:`_no_flags`.
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

import posixpath
import re
import shlex
from collections.abc import Callable
from dataclasses import dataclass
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
        if "ENVIRON" in a:
            return False  # the process environment, credentials included
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
    #
    # A glob or a brace list is one word here and several once the shell has
    # expanded it: ``uniq *.txt`` with two matches overwrites the second.
    positional = 0
    options_done = False
    for a in args:
        if any(ch in a for ch in "*?[{"):
            return False
        if options_done or a == "-" or not a.startswith("-"):
            positional += 1
        elif a == "--":
            options_done = True
    return positional <= 1


def _env_ok(args: list[str]) -> bool:
    # Bare ``env`` prints the whole environment, and a shell command inherits
    # the credentials in child_env.ALLOWED_CREDENTIAL_NAMES on purpose
    # (GH_TOKEN, the provider keys), so printing it is a credential read.
    return args in (["--help"], ["--version"])


def _printenv_ok(args: list[str]) -> bool:
    # ``printenv PATH`` is a plain read. Bare ``printenv`` is the whole
    # environment, and a name that reads as a credential is one.
    from fluid.tools.child_env import ALLOWED_CREDENTIAL_NAMES, is_host_secret

    if not args:
        return False
    for name in args:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            return False
        if name.upper() in ALLOWED_CREDENTIAL_NAMES or is_host_secret(name):
            return False
    return True


# ``env`` and ``$ENV`` are how a jq program reads the process environment;
# ``.env`` (a field) and ``"env"`` (a string) are not.
_JQ_ENV_RE = re.compile(r"\$ENV\b|(?<![\w.$\"])env(?![\w\"])")


# Code jq loads from a file is code this check never reads. As with ``env``,
# ``.include`` (a field) and ``"import"`` (a string) are not the keyword.
_JQ_MODULE_RE = re.compile(
    r"(?<![\w.$\"])(?:import|include|modulemeta|get_search_list)(?![\w\"])"
)
_JQ_FLAGS = frozenset({
    "--raw-output", "--raw-output0", "--join-output", "--ascii-output",
    "--compact-output", "--slurp", "--null-input", "--raw-input", "--sort-keys",
    "--color-output", "--monochrome-output", "--tab", "--unbuffered", "--stream",
    "--stream-errors", "--seq", "--exit-status", "--binary", "--args",
    "--jsonargs", "--help", "--version", "--build-configuration",
})
# Long options that take the next words as their value.
_JQ_VALUE_OPTIONS = {
    "--indent": 1, "--arg": 2, "--argjson": 2, "--slurpfile": 2, "--rawfile": 2,
    "--argfile": 2,
}


def _jq_ok(args: list[str]) -> bool:
    """A jq filter written on the command line that reads no environment.

    The filter is the first word that is not an option or an option's value,
    so the options are parsed to find it: only the ones listed here pass, and
    a word jq would not know asks. ``-f``/``--from-file`` and
    ``-L``/``--library-path`` take the filter or its modules from a file, and
    ``import``/``include`` load one from inside the filter.
    """
    if any(_JQ_ENV_RE.search(a) or _JQ_MODULE_RE.search(a) for a in args):
        return False
    filter_seen = False
    i = 0
    while i < len(args):
        a = args[i]
        i += 1
        if a == "--":
            break
        if a.startswith("--"):
            if a in _JQ_VALUE_OPTIONS:
                i += _JQ_VALUE_OPTIONS[a]
                if i > len(args):
                    return False
            elif a not in _JQ_FLAGS:
                return False
        elif a.startswith("-") and len(a) > 1:
            # Short flags bundle (-nr). -f and -L read code from a file.
            if not re.fullmatch(r"-[A-Za-z]+", a) or "f" in a or "L" in a:
                return False
        elif not filter_seen:
            if a == _GLOB_OPERAND:
                return False  # a file name where the filter goes
            filter_seen = True
    # After ``--`` the next word is the filter when none came before.
    return filter_seen or i >= len(args) or args[i] != _GLOB_OPERAND


def _exact(*allowed: tuple[str, ...]) -> Check:
    """Allow only when the argument list equals one of the given tuples."""
    options = {tuple(a) for a in allowed}
    return lambda args: tuple(args) in options


# Program → extra check (None means the program is fine with any arguments,
# subject to the global redirection/substitution rules).
READ_ONLY_PROGRAMS: dict[str, Check | None] = {
    "ls": None, "dir": None, "cat": None, "head": None, "tail": None,
    "more": None, "wc": None,  # not less: LESSOPEN/LESSCLOSE run commands
    "grep": None, "egrep": None, "fgrep": None,
    # Both run a program. ripgrep rejects abbreviated long options, so exact.
    "rg": _no_flags("--pre", "--hostname-bin"),
    "find": _find_ok,
    "file": _no_flags(
        "-C", "--compile", takes_value="efFmP", long_options=_FILE_LONG_OPTIONS,
    ),
    "stat": None, "du": None, "df": None,
    "pwd": None, "echo": None, "printf": None, "true": None,
    "which": None, "where": None, "type": None,
    "env": _env_ok, "printenv": _printenv_ok,
    "date": _date_ok, "whoami": None, "uname": None,
    # tree -o writes its listing to a file; -R (with -H) writes 00Tree.html
    # into every directory. tree takes option values from the next word, so
    # every letter of a bundle is a flag. tree has its own parser, not
    # getopt, so every word is scanned, ``--`` or not.
    "tree": _no_flags("-o", "-R"),
    "diff": None, "cmp": None,
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
    "jq": _jq_ok,
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


# --output writes a file. --ext-diff and --textconv ask git to run the program
# a repository's own config names (diff.external, diff.*.textconv).
_GIT_BANNED_LONG = ("--output", "--ext-diff", "--textconv")


def _git_sets_banned_option(arg: str) -> bool:
    """True when ``arg`` is, or may be read as, one of ``_GIT_BANNED_LONG``.

    git takes any unambiguous start of a long option (``--ext-d``,
    ``--textc``, ``--outp=f``). Which starts are unambiguous depends on the
    subcommand and the git version, so every start of a banned name counts:
    ``--text`` asks although it is an option of its own.
    """
    if not arg.startswith("--") or len(arg) < 3:
        return False
    name = arg.split("=", 1)[0]
    return any(arg.startswith(b) or b.startswith(name) for b in _GIT_BANNED_LONG)


def _git_ok(args: list[str]) -> bool:
    from fluid.tools.child_env import git_hardening_supported

    # The settings that stop git running a program a folder names
    # (child_env.harden_git_env) need git 2.38. On an older git, or when git
    # cannot be asked, no git command is rated read-only.
    if not git_hardening_supported():
        return False
    if any(_git_sets_banned_option(a) for a in args):
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


_OPERATOR_CHARS = frozenset("();<>|&")
# What may follow a ``$`` the shell leaves alone (``grep 'a$' f`` needs no
# help, ``grep "a$|b$" f`` and ``grep a$ f`` do).
_DOLLAR_STAYS_LITERAL_BEFORE = frozenset(" \t|)")


@dataclass(frozen=True)
class _Word:
    """One word of a command line, or one run of operator characters."""

    raw: str  # as written, quotes kept
    value: str  # with the quotes removed: what the program gets if nothing expands
    operator: bool = False  # ``;``, ``&&``, ``|``, ``>`` ... outside quotes
    quoted_operator: bool = False  # ``';'``, ``\;``: an argument, not a separator
    dollar: bool = False  # ``$X``, ``"$X"``, ``$'..'``: text the shell fills in
    # The literal text before the first unquoted glob character, None when the
    # word has none. Every file name the shell expands the word to starts
    # with it, so ``src/*.py`` cannot become a flag and ``*.py`` can (a file
    # named ``-o.py``).
    glob_prefix: str | None = None
    brace_list: bool = False  # ``{a,b}`` or ``{1..3}`` outside quotes

    @property
    def varies(self) -> bool:
        """The shell may run other words, or more or fewer, than this one."""
        return self.dollar or self.glob_prefix is not None or self.brace_list


@dataclass(frozen=True)
class _Scan:
    """One command line as the shell reads it, before any word is run."""

    text: str  # the line without its comment
    words: tuple[_Word, ...]  # words and operator runs, in order

    @property
    def hazard(self) -> bool:
        """The shell would run words other than the ones written, for any program."""
        return any(w.dollar or w.quoted_operator for w in self.words)


def _lines(command: str) -> list[str] | None:
    """The lines the shell splits ``command`` into, or None.

    Only a newline ends a line. ``str.splitlines`` also splits on vertical
    tab, form feed and the Unicode separators, which the shell keeps inside a
    word, so text after one was classified as a command of its own while the
    shell ran it as arguments. Every other control character is refused. A
    carriage return is one of them, also before a newline: the shell keeps it
    as a letter, so ``uniq a.txt \\r`` has a second operand (the file uniq
    writes) that a reader which drops it never sees.
    """
    if any((ch < " " and ch not in "\n\t") or ch == "\x7f" for ch in command):
        return None
    return command.split("\n")


def _scan_line(line: str) -> _Scan | None:
    """Read ``line`` the way a POSIX shell does. None when it cannot be read.

    ``shlex`` drops the quotes, so on its own it cannot tell an argument from
    shell syntax, and it is not a shell: it starts a comment at a ``#`` in the
    middle of a word. Every difference is a command the classifier never saw.
    This is the one reader: the read-only check, the ``allow_always`` labels
    and the allow rules all take their words from it, so none of them can
    split a line differently from the others. Each word records how the shell
    may change it before the program sees it (see :class:`_Word`):

    * ``$`` outside single quotes. ``-exe${x}c`` and ``$'-exec'`` are ``-exec``
      once expanded, ``.e${x}nv`` is ``.env``, and ``$GH_TOKEN`` is a secret.
    * A quoted or escaped word made only of operator characters (``';'``,
      ``\\;``): an argument to the shell, a command separator to ``shlex``.
    * A glob: zero, one or many file names, any of which may start with ``-``
      unless the word has a literal start that rules it out. A leading ``^``
      counts (zsh with EXTENDED_GLOB).
    * A brace list: several words, built from pieces (``e{nv,x}``).

    Unbalanced quotes and a trailing backslash return None.
    """
    words: list[_Word] = []
    start: int | None = None  # where the current word began
    value: list[str] = []  # the word with its quotes removed
    quoted = dollar = brace_list = False
    glob_at: int | None = None  # len(value) at the word's first unquoted glob
    brace_at: int | None = None  # len(value) at the word's first unquoted {
    i, n = 0, len(line)

    def close(end: int) -> None:
        nonlocal start, quoted, dollar, brace_list, glob_at, brace_at
        if start is None:
            return
        text = "".join(value)
        words.append(_Word(
            raw=line[start:end],
            value=text,
            quoted_operator=quoted and bool(text) and all(
                ch in _OPERATOR_CHARS for ch in text
            ),
            dollar=dollar,
            glob_prefix=None if glob_at is None else text[:glob_at],
            brace_list=brace_at is not None and brace_list,
        ))
        start = None
        value.clear()
        quoted = dollar = brace_list = False
        glob_at = brace_at = None

    while i < n:
        ch = line[i]
        if ch in " \t":
            close(i)
            i += 1
            continue
        if ch in _OPERATOR_CHARS:
            close(i)
            end = i + 1
            while end < n and line[end] in _OPERATOR_CHARS:
                end += 1
            words.append(_Word(raw=line[i:end], value=line[i:end], operator=True))
            i = end
            continue
        if ch == "#" and start is None:
            return _Scan(line[:i], tuple(words))  # a comment
        if start is None:
            start = i
        if ch == "\\":
            if i + 1 >= n:
                return None  # a line continuation: the next line joins this one
            value.append(line[i + 1])
            quoted = True
            i += 2
            continue
        if ch == "'":
            end = line.find("'", i + 1)
            if end < 0:
                return None
            value.extend(line[i + 1:end])
            quoted = True
            i = end + 1
            continue
        if ch == '"':
            quoted = True
            i += 1
            while True:
                if i >= n:
                    return None
                inner = line[i]
                if inner == '"':
                    i += 1
                    break
                if inner == "\\" and i + 1 < n and line[i + 1] in '"\\$`':
                    value.append(line[i + 1])
                    i += 2
                    continue
                if inner == "$":
                    after = line[i + 1] if i + 1 < n else " "
                    if after != '"' and after not in _DOLLAR_STAYS_LITERAL_BEFORE:
                        dollar = True
                value.append(inner)
                i += 1
            continue
        if ch == "$":
            after = line[i + 1] if i + 1 < n else " "
            if after not in _DOLLAR_STAYS_LITERAL_BEFORE:
                dollar = True
        elif ch in "*?[" or (ch == "^" and not value):
            if glob_at is None:
                glob_at = len(value)
        elif ch == "{":
            if brace_at is None:
                brace_at = len(value)
        elif brace_at is not None and (ch == "," or line[i:i + 2] == ".."):
            brace_list = True
        value.append(ch)
        i += 1
    close(n)
    return _Scan(line, tuple(words))


def _posix_shell() -> bool:
    """False when commands run through cmd.exe (Windows without Git Bash).

    Everything here reads POSIX syntax. cmd.exe does not: single quotes do not
    quote there (``git status 'a & calc'`` runs ``calc``), ``^`` is dropped
    from the middle of a word (``--outp^ut=f``) and ``%VAR%`` expands, so
    nothing this module proves about a command holds under it. It also looks
    for a bare program name in the working folder before PATH, so ``git`` can
    be a ``git.cmd`` a repository ships. Nothing is rated read-only there, an
    approval stays one-shot and no allow rule matches a command.
    """
    from fluid.tools.shell import resolve_shell

    return resolve_shell().posix


def _split_commands(scan: _Scan) -> list[list[_Word]] | None:
    """The simple commands of one scanned line, empty ones included.

    None when the line has an operator that is not a plain separator: a
    redirection, a background ``&``, a subshell, ``|&``.
    """
    groups: list[list[_Word]] = [[]]
    for word in scan.words:
        if not word.operator:
            groups[-1].append(word)
        elif word.value in _SEPARATORS:
            groups.append([])
        else:
            return None
    return groups


# Stands in for a file name the shell puts where a glob was written. No check
# accepts it as an awk program, a sed script, a jq filter, a subcommand or a
# variable name, so a command passes with it only where a file may go.
_GLOB_OPERAND = "@@"

# Checks that look at each word on its own, wherever it sits: adding or
# dropping operands between the words cannot change their answer.
_WORDWISE_PROGRAMS = frozenset({"find", "rg", "file", "tree", "sort"})


def _arguments_ok(program: str, check: Check, args: list[_Word]) -> bool:
    """``check`` over the arguments the shell may hand ``program``.

    A check reads the words as written. The shell hands the program other
    words when one is a brace list or a glob, so those pass only where no
    expansion can change the answer:

    * a brace list never does: ``e{nv,x}`` is ``env`` to jq and nothing the
      check saw;
    * a glob that could expand to a flag never does (``*``, ``*.txt``,
      ``-n*``: a file named ``-delete`` or ``-o.txt``);
    * a glob with a literal start (``src/*.py``, ``./*``) expands to operands.
      For the word-by-word checks that is enough. Every other check counts
      words or reads one as a program, so the globs must close the command,
      and the check must hold with none, one and two file names in their
      place: ``uniq out/*`` writes its second match, ``awk x*`` runs its
      first as a program, ``git -C sub* status`` takes its second as the
      subcommand.
    """
    values = [w.value for w in args]
    if any(w.brace_list for w in args):
        return False
    globs = [k for k, w in enumerate(args) if w.glob_prefix is not None]
    if not globs:
        return check(values)
    if any(
        not args[k].glob_prefix or args[k].glob_prefix.startswith("-") for k in globs
    ):
        return False
    if program in _WORDWISE_PROGRAMS:
        return check(values)
    first = globs[0]
    if len(globs) != len(args) - first:
        return False
    return all(check(values[:first] + [_GLOB_OPERAND] * count) for count in (0, 1, 2))


def _simple_command_ok(words: list[_Word]) -> bool:
    # Leading VAR=value assignments pass only for inert variables; a bare
    # assignment is not a command.
    at = 0
    while at < len(words) and _ENV_ASSIGNMENT.match(words[at].value):
        if not _inert_assignment(words[at].value):
            return False
        at += 1
    if at >= len(words):
        return False
    # The assignments and the program are run as written, or not at all.
    if any(w.glob_prefix is not None or w.brace_list for w in words[:at + 1]):
        return False
    name = words[at].value
    # A program named by path (./ls, scripts/cat, /tmp/x/grep, C:\bin\git.exe)
    # runs whatever file sits there, not the utility its basename suggests.
    if re.search(r"[\\/]", name):
        return False
    program = _program_name(name)
    if program not in READ_ONLY_PROGRAMS:
        return False
    check = READ_ONLY_PROGRAMS[program]
    return check is None or _arguments_ok(program, check, words[at + 1:])


def is_read_only(command: str) -> bool:
    """True when every part of ``command`` is provably read-only."""
    if not command or not command.strip():
        return False
    if "$(" in command or "`" in command:
        return False
    lines = _lines(command)
    if lines is None:
        return False
    for line in lines:
        line = re.sub(r"(?<!\S)2>&1(?!\S)", " ", line)
        if not line.strip():
            continue
        scan = _scan_line(line)
        if scan is None or scan.hazard:
            return False
        if not scan.text.strip():
            continue  # a comment line
        groups = _split_commands(scan)
        if groups is None:
            return False  # redirection, background &, subshells, |& and friends
        for group in groups:
            # A quoted ``>`` is an argument, and still asks, as it always has.
            if any(">" in w.value or "<(" in w.value for w in group):
                return False
            if not _simple_command_ok(group):
                return False
    return True


def _command_word_literal(words: list[_Word]) -> bool:
    """True when the shell runs the program this simple command names.

    False when the word that names the program, or one before it, can expand
    to something else: ``$X status``, ``g* status``, ``{git,rm} x``.
    """
    for word in words:
        if word.brace_list or word.glob_prefix is not None:
            return False
        if not _ENV_ASSIGNMENT.match(word.value):
            return not word.dollar
        # An assignment's value is filled in but never split into words.
    return True


def _label_words_vary(words: list[_Word]) -> bool:
    """True when the shell can replace a word that says what this command runs.

    Those are the words :func:`_scope_label` reads: the assignments, the
    program, a wrapper's duration, the program it wraps, the subcommand.
    ``timeout $T git status`` runs ``rm`` when ``$T`` is ``5 rm``. A wrapper
    with options hides which word is the program, so there any expansion
    counts.
    """
    at = 0
    while at < len(words) and _ENV_ASSIGNMENT.match(words[at].value):
        # An assignment's value is filled in but never split into words.
        if words[at].brace_list or words[at].glob_prefix is not None:
            return True
        at += 1
    if at >= len(words):
        return False
    if words[at].varies:
        return True
    program = _program_name(words[at].value)
    rest = words[at + 1:]
    if program in WRAPPER_PROGRAMS and rest:
        if rest[0].varies:
            return True
        if rest[0].value.startswith("-"):
            return any(w.varies for w in rest)
        return _label_words_vary(rest[1:] if program == "timeout" else rest)
    if program in SUBCOMMAND_PROGRAMS and rest:
        return rest[0].varies
    return False


def simple_commands(command: str, *, for_allow: bool = False) -> list[str] | None:
    """The simple commands of a command line, quoting kept, or None.

    ``git commit -m "a b" && rm -rf /`` -> ``['git commit -m "a b"', 'rm -rf /']``.
    Splits on ``&&``, ``||``, ``;``, ``|``, ``|&``, a background ``&`` and
    newlines. None when the line uses command substitution, a subshell or
    process substitution, or cannot be read: the pieces it would run are not
    visible in the text, so a caller matching text against a policy must not
    treat the split as complete.

    ``for_allow`` is for a caller about to let the command run on the
    strength of its text. It adds three refusals: a control character the
    shell and this reader may treat differently, a word naming what runs
    that the shell can replace (``$X``, a glob, a brace list; see
    :func:`_label_words_vary`), and cmd.exe, which does not read the line
    the way this does. Without it the split is best effort and
    as wide as it can be, which is what a deny or ask rule wants: the parts
    of both readings of a control character, as a letter and as a line break.
    """
    if not command or not command.strip() or "$(" in command or "`" in command:
        return None
    if for_allow:
        if not _posix_shell():
            return None
        lines = _lines(command)
        return None if lines is None else _simple_command_texts(lines, for_allow=True)
    parts = _simple_command_texts(command.split("\n"), for_allow=False)
    if any(ch in command for ch in "\r\x0b\x0c\x1c\x1d\x1e\x85  "):
        other = _simple_command_texts(command.splitlines(), for_allow=False)
        parts = list(dict.fromkeys([*(parts or ()), *(other or ())])) or None
    return parts


def _simple_command_texts(lines: list[str], *, for_allow: bool) -> list[str] | None:
    out: list[str] = []
    for line in lines:
        if not line.strip():
            continue
        scan = _scan_line(line)
        if scan is None:
            return None
        current: list[_Word] = []
        groups: list[list[_Word]] = []
        for word in scan.words:
            if not word.operator:
                current.append(word)
            elif word.value in _SEPARATORS or word.value in ("&", "|&"):
                groups.append(current)
                current = []
            elif set(word.value) <= set("<>&"):
                current.append(word)  # a redirection stays part of its command
            else:
                return None  # subshell parentheses, unknown operator soup
        groups.append(current)
        for group in groups:
            if not group:
                continue
            if for_allow and _label_words_vary([w for w in group if not w.operator]):
                return None
            out.append(" ".join(w.raw for w in group))
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


def _scope_label(words: list[_Word]) -> str | None:
    """The approval label for one simple command's words.

    A wrapper labels as ``wrapper <label of the wrapped command>``; a
    subcommand program as ``program subcommand``; anything else as the
    program alone. None when a wrapper carries options (``sudo -u root``):
    which token is the wrapped program is then not knowable from the text,
    and a guess would scope the approval wrongly. None too for a program
    named by path (``/usr/bin/git``, ``scripts/git``, ``./build.sh``), at any
    depth of wrapping: that runs whatever file sits there, so approving it
    must not cover the bare name or another path (and a relative path names
    a different file once the working directory changes); it stays one-shot.

    None as well when a word the label is read from can expand: the
    assignments, the program, a wrapper's duration, the wrapped program, the
    subcommand. ``timeout $T git status`` runs ``rm`` when ``$T`` is ``5 rm``,
    and a label of ``timeout git`` would have covered it. Words after those
    may expand freely (``git commit -m "$MSG"``): they cannot change which
    program runs.
    """
    at = 0
    while at < len(words) and _ENV_ASSIGNMENT.match(words[at].value):
        # An assignment's value is filled in but never split into words.
        if words[at].brace_list or words[at].glob_prefix is not None:
            return None
        at += 1
    if at >= len(words) or words[at].varies:
        return None
    name = words[at].value
    if re.search(r"[\\/]", name):
        return None
    program = _program_name(name)
    rest = words[at + 1:]
    if program in WRAPPER_PROGRAMS:
        if not rest:
            return program
        if rest[0].varies or rest[0].value.startswith("-"):
            return None
        if program == "timeout":
            # ``timeout DURATION COMMAND``: the duration is not the program.
            rest = rest[1:]
            if not rest:
                return None
        inner = _scope_label(rest)
        return None if inner is None else f"{program} {inner}"
    if program in SUBCOMMAND_PROGRAMS:
        if rest and rest[0].varies:
            return None
        if rest and not rest[0].value.startswith("-"):
            return f"{program} {rest[0].value.lower()}"
        return program
    return program


def _simple_command_words(command: str) -> list[list[_Word]] | None:
    """The words of every simple command, or None when they are not knowable."""
    if not command or not command.strip() or "$(" in command or "`" in command:
        return None
    if not _posix_shell():
        return None
    lines = _lines(command)
    if lines is None:
        return None
    out: list[list[_Word]] = []
    for line in lines:
        if not line.strip():
            continue
        scan = _scan_line(line)
        if scan is None:
            return None
        groups = _split_commands(scan)
        if groups is None:
            return None
        out.extend(group for group in groups if group)
    return out or None


def program_names(command: str) -> frozenset[str] | None:
    """Every program a command invokes (``cat a | grep b`` → {cat, grep}).

    None when the command cannot be lexed or uses substitution/subshells,
    so callers fall back to the broadest scope rather than a wrong one.
    """
    groups = _simple_command_words(command)
    if groups is None:
        return None
    names: set[str] = set()
    for words in groups:
        if not _command_word_literal(words):
            return None
        tokens = [w.value for w in words]
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
    groups = _simple_command_words(command)
    if groups is None:
        return None
    labels: set[str] = set()
    for words in groups:
        label = _scope_label(list(words))
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
    "proc/self/environ", "proc/1/environ",
)
# What ``_names_process_environment`` covers, for a glob written from the root.
_PROC_EXAMPLE_PATHS = (
    "proc", "proc/self", "proc/1", "proc/thread-self",
    "proc/self/environ", "proc/1/environ", "proc/thread-self/environ",
    "proc/self/task/1", "proc/self/task/1/environ",
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
        if alt.replace("\\", "/").startswith("/") and _glob_could_name_process(parts):
            return True
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


def _glob_could_name_process(parts: list[str]) -> bool:
    """True when a glob written from the root (``/proc/[0-9]*/environ``,
    ``/pro?/self``, ``/**/environ``) can match a process folder or its
    environment file. ``**`` stands for any number of folders in zsh and in
    bash with globstar, so it is tried as none to three of them."""
    from fnmatch import fnmatchcase

    shapes = [parts]
    if "**" in parts:
        at = parts.index("**")
        shapes = [parts[:at] + ["*"] * count + parts[at + 1:] for count in range(4)]
    for shape in shapes:
        for example in _PROC_EXAMPLE_PATHS:
            names = example.split("/")
            if len(names) == len(shape) and all(
                fnmatchcase(name, glob) for name, glob in zip(names, shape)
            ):
                return True
    return False


def _climbs_through_a_folder(path: str) -> bool:
    """True for ``a/../b``: a ``..`` after a named folder.

    The kernel follows ``a`` when it is a link before it climbs, so the text
    does not say where the path ends: ``/dev/fd/../environ`` is
    ``/proc/self/environ``. A leading ``../..`` climbs from the working
    folder and stays readable.
    """
    named = False
    for part in path.replace("\\", "/").split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if named:
                return True
        else:
            named = True
    return False


def _touches_sensitive_path(command: str, cwd: object = None) -> bool:
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

    base = _working_folder(cwd)
    if base is not None and (
        _names_process_environment(base) or is_sensitive_path(base + "/x")
    ):
        return True  # ``rg token`` with no operand reads the folder it runs in
    lines = _lines(command)
    if lines is None:
        return True
    for line in lines:
        scan = _scan_line(line)
        if scan is None:
            return True
        for word in scan.words:
            if word.operator:
                continue
            raw = word.raw
            if raw.startswith("-"):
                # ``--file=.env``: the value is a path all the same.
                if "=" not in raw:
                    continue
                raw = raw.split("=", 1)[1]
            try:
                literal = " ".join(shlex.split(raw))
            except ValueError:
                return True
            glob = _has_unquoted_glob(raw)
            if _climbs_through_a_folder(literal):
                return True
            for path in _operand_spellings(literal, base):
                if is_sensitive_path(path) or _names_process_environment(path):
                    return True
                if glob and _glob_could_name_sensitive_path(path):
                    return True
    return False


def _working_folder(cwd: object) -> str | None:
    """The tool call's own ``cwd`` with forward slashes, or None."""
    if not isinstance(cwd, str) or not cwd.strip():
        return None
    return cwd.strip().replace("\\", "/").rstrip("/") or "/"


def _operand_spellings(literal: str, base: str | None) -> list[str]:
    """``literal`` as written and, when relative, under the call's ``cwd``.

    ``cat credentials`` with ``cwd`` ``~/.aws`` and ``cat self/environ`` with
    ``cwd`` ``/proc`` name the same files as their absolute spellings.
    """
    spellings = [literal]
    path = literal.replace("\\", "/")
    if base is not None and path and not path.startswith(("/", "~")) and ":" not in path[:2]:
        spellings.append(posixpath.normpath(posixpath.join(base, path)))
    return spellings


# /proc/<pid>/environ is the environment of a running process, and a shell
# command's own environment carries credentials on purpose (see _env_ok). The
# process folder itself counts: ``grep -r . /proc/self`` reads the same file.
# ``/proc/<pid>/root`` is ``/`` again, any number of times.
_PROC_ROOT = r"(?:/proc/[^/]+/root)*"
_PROC_PROCESS = r"(?:\d+|self|thread-self)(?:/task/[^/]+)?"
_PROC_ENVIRON_RE = re.compile(rf"^{_PROC_ROOT}/proc(?:/{_PROC_PROCESS}(?:/environ)?)?/?$")
# The file itself, whatever leads up to it: ``link/proc/self/environ``.
_PROC_ENVIRON_FILE_RE = re.compile(rf"(?:^|/)proc/{_PROC_PROCESS}/environ$")


def _names_process_environment(literal: str) -> bool:
    path = posixpath.normpath(literal.replace("\\", "/"))
    if _PROC_ENVIRON_FILE_RE.search(path):
        return True
    # A relative path that climbs out of its folder can end at the root:
    # ``../../proc/self`` from two levels down is ``/proc/self``.
    climbed = re.sub(r"^(?:\.\./)+", "/", path)
    return bool(_PROC_ENVIRON_RE.match(climbed)) if climbed.startswith("/") else False


def classify(input: dict[str, Any]) -> str:
    """Effective risk of one Bash tool input: ``safe`` or ``write``."""
    command = input.get("command")
    if not isinstance(command, str):
        return WRITE
    if not _posix_shell():
        return WRITE  # cmd.exe: this module cannot read the command
    if not is_read_only(command):
        return WRITE
    # Read-only, but targeting a credential file → keep the prompt.
    return WRITE if _touches_sensitive_path(command, input.get("cwd")) else SAFE
