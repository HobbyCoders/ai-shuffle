"""Shared helpers for builtin tools."""

from __future__ import annotations

import difflib
import os
import re
import time
from collections.abc import Iterator
from dataclasses import dataclass
from itertools import islice
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


class GlobSyntaxError(ValueError):
    """A glob construct the pure-Python glob compiler cannot honour.

    Raised rather than quietly compiling to something that matches nothing:
    a filter that drops every file turns a real hit into a clean "(no
    matches)", which reads as a fact about the tree and is acted on as one.
    The caller's job is to surface it as a hard error naming the construct.
    """


def _close_brace(pat: str, start: int) -> int:
    """Index of the ``}`` closing the group opened at ``start`` (nesting aware)."""
    depth = 0
    for i in range(start, len(pat)):
        if pat[i] == "{":
            depth += 1
        elif pat[i] == "}":
            depth -= 1
            if depth == 0:
                return i
    raise GlobSyntaxError("unbalanced '{' — the brace group is never closed")


def _split_alternatives(body: str) -> list[str]:
    """Split a brace body on its top-level commas; nested groups stay whole."""
    alts: list[str] = []
    depth = start = 0
    for i, ch in enumerate(body):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        elif ch == "," and depth == 0:
            alts.append(body[start:i])
            start = i + 1
    alts.append(body[start:])
    return alts


def _glob_expr(pat: str) -> str:
    """Translate a glob body (no leading ``!``) into a regex fragment."""
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
            if close == -1:
                raise GlobSyntaxError("unterminated '[' — the character class is never closed")
            body = pat[i + 1:close]
            if body.startswith(("!", "^")):
                body = "^" + body[1:]
            out.append(f"[{body}]")
            i = close + 1
            continue
        if ch == "{":
            close = _close_brace(pat, i)
            # Each alternative is a glob in its own right, so a separator or a
            # nested group inside the braces means what it means outside them.
            alts = _split_alternatives(pat[i + 1:close])
            out.append("(?:" + "|".join(_glob_expr(alt) for alt in alts) + ")")
            i = close + 1
            continue
        out.append(re.escape(ch))
        i += 1
    return "".join(out)


def glob_to_regex(pattern: str, *, case_sensitive: bool | None = None) -> re.Pattern[str]:
    """Compile a glob into a regex matched against a POSIX relative path.

    Supports ``**`` (any depth), ``*``/``?`` (within one segment), ``[...]``
    classes and ``{a,b}`` alternation — the last because ripgrep accepts it, so
    a ``*.{py,ts}`` that worked through Grep's rg branch must not silently match
    nothing through the Python one. Alternatives may contain ``/`` and may nest,
    for the same reason. A pattern with no ``/`` matches the file *name* at any
    depth, so a bare ``*.py`` behaves the way models tend to expect.

    Raises :class:`GlobSyntaxError` for anything it cannot translate faithfully,
    including a leading ``!`` — that is ripgrep's exclusion marker, not a
    literal, so it has to go through :func:`compile_glob`.
    """
    pat = pattern.replace("\\", "/")
    if pat.startswith("!"):
        raise GlobSyntaxError(
            "a leading '!' is ripgrep's exclusion marker, not a literal; "
            "compile it with compile_glob()"
        )
    expr = _glob_expr(pat)
    if "/" not in pat:
        expr = "(?:.*/)?" + expr
    if case_sensitive is None:
        case_sensitive = os.name != "nt"
    try:
        return re.compile(expr + r"\Z", 0 if case_sensitive else re.IGNORECASE)
    except re.error as exc:  # pragma: no cover - only reachable via odd classes
        raise GlobSyntaxError(f"glob does not compile: {exc}") from exc


@dataclass(frozen=True)
class GlobFilter:
    """A compiled ``glob`` value carrying ripgrep's leading-``!`` semantics.

    ``rg --glob '!x'`` is an *exclusion*: every file is searched except the ones
    matching ``x``. Translating that as an include filter — which is what a
    plain glob→regex pass does once the ``!`` has been escaped — matches nothing
    at all, and the search comes back as an honest-looking "(no matches)".
    """

    regex: re.Pattern[str]
    negated: bool

    def matches(self, rel_path: str) -> bool:
        """Does a root-relative POSIX path pass this filter?"""
        hit = self.regex.match(rel_path) is not None
        return not hit if self.negated else hit


def compile_glob(pattern: str, *, case_sensitive: bool | None = None) -> GlobFilter:
    """Compile one ripgrep-style ``--glob`` value into a path filter."""
    negated = pattern.startswith("!")
    body = pattern[1:] if negated else pattern
    if not body:
        raise GlobSyntaxError("'!' with nothing to exclude")
    return GlobFilter(
        regex=glob_to_regex(body, case_sensitive=case_sensitive), negated=negated
    )


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


# ── missing-path diagnostics ─────────────────────────────────────────────────
#
# A bad path is one of the most common tool failures and the most wasteful: the
# bare "file not found: X" costs a Glob or a Grep to recover from. Two shapes
# dominate, and both are recognisable from the string itself — a `path:line:`
# match line pasted straight out of Grep output, and a near-miss spelling in a
# directory that does exist. Naming the fix inline turns a two-call recovery
# into a zero-call one.

_GREP_MATCH_SUFFIX = re.compile(r"^(?P<path>.+?):\d+:")


def _grep_line_note(raw: str) -> str | None:
    """Recognise a Grep result line handed back as if it were a path."""
    match = _GREP_MATCH_SUFFIX.match(raw)
    if match is None:
        return None
    candidate = match.group("path")
    if not candidate or not Path(candidate).exists():
        return None
    return (
        f'this looks like a Grep result line ("path:line:content"); the path '
        f'part is "{candidate}" — pass just that'
    )


def _nearby_note(path: Path) -> str | None:
    """Closest existing names in the parent directory, when there are any."""
    parent = path.parent
    try:
        if not parent.is_dir():
            return f"its parent directory {parent} does not exist either"
        names = [entry.name for entry in islice(parent.iterdir(), 2000)]
    except OSError:
        return None
    close = difflib.get_close_matches(path.name, names, n=3, cutoff=0.6)
    if not close:
        return None
    return "did you mean: " + ", ".join(str(parent / name) for name in close)


def missing_path_note(raw: str, path: Path) -> str:
    """The trailing clause for a not-found diagnostic ("" when nothing helps)."""
    for note in (_grep_line_note(raw), _nearby_note(path)):
        if note:
            return f" — {note}"
    return ""


# ── repeat-work notes ────────────────────────────────────────────────────────
#
# Re-reading a file nothing has touched, and re-running a search already run,
# are the two cheapest calls to make and the two most common ones to waste a
# turn on. The result still comes back in full — the note only says the content
# is already in the conversation, so the next step can use it instead of asking
# for it again.


def unchanged_read_note(repeat: bool) -> str:
    """The trailing note for a Read that returns already-seen content."""
    if not repeat:
        return ""
    return (
        "\n\n[note: unchanged since your earlier read this session — reuse "
        "what you already learned instead of re-reading]"
    )


def repeat_search_note(repeat: bool) -> str:
    """The trailing note for a search whose inputs were already searched."""
    if not repeat:
        return ""
    return (
        "\n[note: identical search already run this session — its results are "
        "above in the conversation]"
    )
