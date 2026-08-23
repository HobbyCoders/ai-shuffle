"""Shared helpers for builtin tools."""

from __future__ import annotations

import os
import re
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from fluid.tools.registry import ToolCtx

_TRUNCATE_LIMIT = 30_000  # ~30KB; keep head + tail, drop the middle

# Terminal control sequences: CSI (colours, cursor moves), OSC (title/hyperlink
# strings, terminated by BEL or ST) and the lone two-byte escapes. Test runners
# and package managers emit these even into a pipe on some platforms, and they
# reach the model — and the UI — as gibberish.
ANSI_RE = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]"
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
    r"|\x1b[@-Z\\-_]"
)


def strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences from captured process output."""
    return ANSI_RE.sub("", text)

# Directories never worth walking for a content search or a file glob: VCS
# metadata, dependency trees, build output, tool caches. Pruning these in place
# is what keeps a repo-wide walk in the thousands of paths instead of hundreds
# of thousands.
PRUNE_DIRS = frozenset({
    ".git",
    ".agents",
    ".cache",
    ".claude",
    ".idea",
    ".next",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "target",
    "venv",
})

# Per-file read ceiling for the pure-Python search path (2MB).
MAX_FILE_BYTES = 2 * 1024 * 1024

# Bytes sampled from the head of a file to decide "is this binary?".
BINARY_SNIFF_BYTES = 8192


def resolve(file_path: str, ctx: ToolCtx) -> Path:
    """Resolve a path against the session cwd (absolute paths pass through)."""
    p = Path(file_path)
    if not p.is_absolute():
        p = Path(ctx.cwd) / p
    return p.resolve()


def truncate(text: str, limit: int = _TRUNCATE_LIMIT) -> str:
    """Head+tail truncation for large tool output (prevents context blowup)."""
    if len(text) <= limit:
        return text
    half = limit // 2
    head, tail = text[:half], text[-half:]
    dropped = len(text) - 2 * half
    return f"{head}\n... [{dropped} bytes truncated] ...\n{tail}"


@dataclass
class WalkState:
    """Budget + telemetry for :func:`walk_files`.

    ``deadline`` is a ``time.monotonic()`` stamp; when it passes, the walk stops
    and flags ``deadline_hit`` so callers can label their output as partial.
    """

    deadline: float | None = None
    files_seen: int = 0
    deadline_hit: bool = False

    @property
    def expired(self) -> bool:
        return self.deadline is not None and time.monotonic() >= self.deadline


def walk_files(root: Path, state: WalkState | None = None) -> Iterator[Path]:
    """Yield files under ``root``, pruning heavy directories and symlinks.

    Uses ``os.walk`` (not ``Path.rglob``) for two reasons: the ``dirnames``
    in-place edit prunes whole subtrees before they are ever listed, and
    ``followlinks=False`` keeps the walk out of symlink/junction cycles —
    ``rglob`` only grew a ``recurse_symlinks`` switch in 3.13.
    """
    state = state or WalkState()
    if root.is_file():
        state.files_seen += 1
        yield root
        return
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in PRUNE_DIRS]
        if state.expired:
            state.deadline_hit = True
            return
        base = Path(dirpath)
        for name in sorted(filenames):
            if state.expired:
                state.deadline_hit = True
                return
            state.files_seen += 1
            yield base / name


def is_binary(path: Path) -> bool:
    """True when the head of ``path`` contains a NUL byte (or cannot be read)."""
    try:
        with path.open("rb") as fh:
            return b"\0" in fh.read(BINARY_SNIFF_BYTES)
    except OSError:
        return True


def glob_to_regex(pattern: str, *, case_sensitive: bool | None = None) -> re.Pattern[str]:
    """Compile a glob into a regex matched against a POSIX relative path.

    Supports ``**`` (any depth), ``*``/``?`` (within one segment) and ``[...]``
    classes. A pattern with no ``/`` matches the file *name* at any depth, so a
    bare ``*.py`` behaves the way models tend to expect.
    """
    pat = pattern.replace("\\", "/")
    out: list[str] = []
    i, n = 0, len(pat)
    while i < n:
        ch = pat[i]
        if ch == "*":
            if pat[i:i + 3] == "**/":
                out.append("(?:.*/)?")
                i += 3
                continue
            if pat[i:i + 2] == "**":
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
            i += 1
            continue
        if ch == "?":
            out.append("[^/]")
            i += 1
            continue
        if ch == "[":
            close = pat.find("]", i + 1)
            if close != -1:
                body = pat[i + 1:close]
                if body.startswith(("!", "^")):
                    body = "^" + body[1:]
                out.append(f"[{body}]")
                i = close + 1
                continue
        out.append(re.escape(ch))
        i += 1
    expr = "".join(out)
    if "/" not in pat:
        expr = "(?:.*/)?" + expr
    if case_sensitive is None:
        case_sensitive = os.name != "nt"
    return re.compile(expr + r"\Z", 0 if case_sensitive else re.IGNORECASE)


def detect_newline(text: str) -> str:
    """The text's dominant line ending: CRLF when it has more CRLF than bare LF."""
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    return "\r\n" if crlf > lf else "\n"


def read_text_preserving(path: Path) -> tuple[str, str]:
    """Return ``(text normalized to LF, original newline)`` without translation."""
    with path.open("r", encoding="utf-8", newline="") as fh:
        raw = fh.read()
    return raw.replace("\r\n", "\n"), detect_newline(raw)


def write_text_with_newline(path: Path, text: str, newline: str) -> None:
    """Write LF-normalized ``text`` using ``newline`` for every line break, with
    no platform translation (so LF files stay LF on Windows)."""
    normalized = text.replace("\r\n", "\n")
    if newline != "\n":
        normalized = normalized.replace("\n", newline)
    with path.open("w", encoding="utf-8", newline="") as fh:
        fh.write(normalized)
