"""How much of a delegated turn's file work went through the shell.

Claude Code can be steered to do its file work through Bash (``cat``,
``grep``, ``sed -i``, heredoc scripts) instead of its dedicated Read, Edit,
Write, Glob and Grep tools. Nothing else in the delegated leg makes that
visible: a Bash-only turn looks like any other turn unless someone reads the
transcript. This module counts it.

:func:`classify_shell` sorts one shell command by its head (the first command
of its first segment) into ``read``, ``search``, ``edit`` or ``other``.
Pipelines and multi-step scripts stay ``other`` unless their first command is
itself file work, so the count errs low. It is a regression signal, not an
audit: on real transcripts the head rules disagree with a manual reading on
roughly one command in twelve.

:class:`ToolMix` counts one turn's tool calls: shell calls classified as file
work are ``bash_file_ops``, calls to the dedicated file tools are
``dedicated_file_ops``.
"""

from __future__ import annotations

import re
import shlex
from typing import Any

#: Shell tools whose ``command`` input is classified.
SHELL_TOOLS = frozenset({"Bash", "PowerShell"})

#: The dedicated file tools, including the app's bundled search tools.
DEDICATED_FILE_TOOLS = frozenset({
    "Read", "Edit", "Write", "Glob", "Grep", "NotebookEdit",
    "mcp__fluid_bundled__Glob", "mcp__fluid_bundled__Grep",
})

#: A shell call counts as file work when its class is one of these.
FILE_WORK = frozenset({"read", "search", "edit"})

#: Warn when a turn did at least this many shell file operations ...
WARN_MIN_BASH_FILE_OPS = 5
#: ... and they were more than this share of its file work.
WARN_BASH_SHARE = 0.6

_READ_HEADS = frozenset({"cat", "head", "tail", "wc", "get-content"})
_SEARCH_HEADS = frozenset({
    "grep", "egrep", "fgrep", "rg", "find", "ls", "select-string", "get-childitem",
})
_PS_WRITE_HEADS = frozenset({"set-content", "add-content", "out-file"})

# ``cd <dir> &&`` (or ``;``), quoted or not: Claude Code prefixes commands
# with the working directory this way.
_CD_PREFIX = re.compile(
    r"""^\s*cd\s+(?:"[^"]*"|'[^']*'|[^\s;&|]+)\s*(?:&&|;)\s*""",
)
_HEREDOC_START = re.compile(r"""<<-?\s*(['"]?)([A-Za-z_][\w-]*)\1""")
_SEGMENT_SPLIT = re.compile(r"&&|\|\||;|\n")
_PY_WRITE = re.compile(
    r"""\.write_(?:text|bytes)\(|\bopen\([^)]*,\s*(?:mode\s*=\s*)?['"][wax]b?\+?['"]""",
)
# An output redirect of stdout (``>`` or ``>>``, not ``2>`` or ``&>``) and
# its target.
_REDIRECT = re.compile(r"""(?<![\d&>])>{1,2}(?!&)\s*(['"]?)([^\s'"<>|;&]+)\1""")
_PS_PIPE_WRITE = re.compile(r"\|\s*(?:set-content|add-content|out-file)\b", re.I)
_ENV_ASSIGN = re.compile(r"^[A-Za-z_]\w*=")


def _drop_heredoc_bodies(command: str) -> str:
    """The command with every heredoc body removed (the ``<<EOF`` line stays)."""
    out: list[str] = []
    pending: list[str] = []
    for line in command.split("\n"):
        if pending:
            if line.strip() == pending[0]:
                pending.pop(0)
            continue
        out.append(line)
        pending.extend(m.group(2) for m in _HEREDOC_START.finditer(line))
    return "\n".join(out)


def _words(segment: str) -> list[str]:
    try:
        return shlex.split(segment, posix=True)
    except ValueError:  # an unbalanced quote: fall back to whitespace
        return segment.split()


def _first_command(command: str) -> str:
    """The first command of the first ``&&``/``||``/``;``/newline segment,
    up to its first pipe."""
    for segment in _SEGMENT_SPLIT.split(command):
        segment = segment.strip()
        if segment:
            return segment.split("|", 1)[0].strip() if "|" in segment else segment
    return ""


def _first_segment(command: str) -> str:
    for segment in _SEGMENT_SPLIT.split(command):
        if segment.strip():
            return segment.strip()
    return ""


def classify_shell(command: Any) -> str:
    """``read``, ``search``, ``edit`` or ``other`` for one shell command.

    A leading ``cd <dir> &&`` is dropped, then only the head of the first
    segment decides: ``cat``/``head``/``tail``/``wc``/``sed`` (without
    ``-i``) read; ``grep``/``rg``/``find``/``ls`` search; ``sed -i``, a
    ``cat > <path>`` outside ``/tmp`` and a ``python`` whose script calls
    ``write_text`` or ``open(..., 'w')`` edit. PowerShell's
    ``Get-Content`` reads, ``Select-String``/``Get-ChildItem`` search and
    ``Set-Content``/``Out-File`` edit. Heredoc bodies are ignored except
    when a python head's script is checked for a file write.
    """
    if not isinstance(command, str) or not command.strip():
        return "other"
    full = _CD_PREFIX.sub("", command, count=1)
    body = _drop_heredoc_bodies(full)
    segment = _first_segment(body)
    words = _words(_first_command(body))
    while words and _ENV_ASSIGN.match(words[0]):
        words.pop(0)
    if not words:
        return "other"
    head = words[0].replace("\\", "/").rsplit("/", 1)[-1].lower()
    if head.endswith(".exe"):
        head = head[:-4]
    args = words[1:]

    if head == "sed":
        in_place = any(
            a == "--in-place" or a.startswith("--in-place=")
            or (a.startswith("-") and not a.startswith("--") and "i" in a[1:])
            for a in args
        )
        return "edit" if in_place else "read"
    if head.startswith("python") or head == "py":
        return "edit" if _PY_WRITE.search(full) else "other"
    if head in _PS_WRITE_HEADS or _PS_PIPE_WRITE.search(segment):
        return "edit"
    if head == "cat":
        # ``cat > path`` writes a file; ``2>/dev/null`` and a scratch file
        # under /tmp are not project edits.
        targets = [
            m.group(2) for m in _REDIRECT.finditer(segment) if m.group(2) != "/dev/null"
        ]
        if not targets:
            return "read"
        return "other" if all(t.startswith("/tmp") for t in targets) else "edit"
    if head in _READ_HEADS:
        return "read"
    if head in _SEARCH_HEADS:
        return "search"
    return "other"


class ToolMix:
    """One turn's count of shell file work against dedicated file tools."""

    def __init__(self) -> None:
        self.bash_file_ops = 0
        self.dedicated_file_ops = 0
        self._seen: set[str] = set()

    def record(self, tool_use_id: str, name: str, tool_input: Any) -> None:
        """Count one tool call (each ``tool_use_id`` at most once)."""
        if tool_use_id:
            if tool_use_id in self._seen:
                return
            self._seen.add(tool_use_id)
        if name in SHELL_TOOLS:
            command = tool_input.get("command") if isinstance(tool_input, dict) else None
            if classify_shell(command) in FILE_WORK:
                self.bash_file_ops += 1
        elif name in DEDICATED_FILE_TOOLS:
            self.dedicated_file_ops += 1

    @property
    def bash_heavy(self) -> bool:
        """True when shell file work dominated the turn's file work."""
        n, m = self.bash_file_ops, self.dedicated_file_ops
        return n >= WARN_MIN_BASH_FILE_OPS and n / (n + m) > WARN_BASH_SHARE
