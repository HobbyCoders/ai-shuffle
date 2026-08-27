"""Grep — regex search: ripgrep when available, bounded pure-Python fallback.

Both branches are bounded and both label their output ("searched via ..."), so
an empty result is diagnosable rather than ambiguous. The fallback runs on a
worker thread: it used to walk the tree synchronously inside the coroutine,
which pinned the event loop for as long as the walk took (minutes on a large
repo) and stopped interrupts from landing.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import shutil
import time
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

from fluid.tools.builtins._util import (
    MAX_FILE_BYTES,
    GlobFilter,
    GlobSyntaxError,
    WalkState,
    compile_glob,
    is_binary,
    missing_path_note,
    repeat_search_note,
    truncate,
    walk_files,
)
from fluid.tools.registry import ToolCtx, ToolOutcome, ToolSpec

MAX_MATCHES = 200
MAX_LIMIT = 5000
RG_TIMEOUT_S = 30.0
FALLBACK_DEADLINE_S = 10.0
_MAX_FILESIZE = "2M"

def describe(tools: frozenset[str] | None = None) -> str:
    """Grep's description, specialized to the session's tool surface."""
    has = (lambda name: tools is None or name in tools)
    opening = "Search file contents by regular expression." + (
        " Always prefer this over running grep/rg through Bash — it is "
        "bounded, labels its output, and cannot hang the session."
        if has("Bash")
        else " Bounded: it labels its output and cannot hang the session."
    )
    read_route = " Read the file explicitly" if has("Read") else " Open the file explicitly"
    return (
        opening + "\n"
        "\n"
        "Results come back as `path:line:content` under a header naming the "
        "search engine (ripgrep when installed, otherwise a bounded Python "
        f"fallback). Matches are capped at {MAX_MATCHES} by default — a note "
        "tells you when the cap was hit; raise `limit` (up to "
        f"{MAX_LIMIT}) or narrow the pattern to see more. Ignored and hidden "
        "files are skipped unless no_ignore is set. Matches inside "
        "credential-pattern files (.env, keys) are withheld and flagged with "
        f"a note;{read_route} when its content is genuinely needed. An empty "
        "result means no matches in the searched scope, not an error.\n"
        "\n"
        "The `glob` filter accepts `**`, `?`, character classes and `{a,b}` "
        "alternation (e.g. *.{py,ts}) whichever engine runs, and a leading "
        "`!` excludes instead of includes (e.g. !{vendor/**,dist/**} searches "
        "everything else). A glob neither engine can honour comes back as an "
        "error naming the construct, never as an empty result.\n"
        "\n"
        "Patterns use ripgrep/Rust regex syntax when ripgrep is installed "
        "(escape literal braces, e.g. interface\\{\\}) and Python re syntax "
        "in the fallback; stick to the shared core (alternation, classes, "
        "quantifiers) for portable patterns."
    )


SPEC = ToolSpec(
    name="Grep",
    description=describe(),
    input_schema={
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "The regular expression to search for.",
            },
            "path": {"type": "string", "description": "File or directory (default cwd)."},
            "glob": {
                "type": "string",
                "description": (
                    "Path glob filter, e.g. *.py or src/**/*.ts. A leading '!' "
                    "excludes instead of includes, e.g. !vendor/**."
                ),
            },
            "no_ignore": {
                "type": "boolean",
                "description": "Also search ignored/hidden files (ripgrep -uu).",
            },
            "limit": {
                "type": "integer",
                "description": (
                    f"Maximum matches to return (default {MAX_MATCHES}). Raise it "
                    "or narrow the pattern when you expect many results."
                ),
            },
        },
        "required": ["pattern"],
    },
    risk="safe",
)


def _clamp_limit(raw: Any) -> int:
    """Agent-settable result cap, clamped to [1, MAX_LIMIT]. Junk → default."""
    if raw is None:
        return MAX_MATCHES
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return MAX_MATCHES
    return max(1, min(MAX_LIMIT, value))


async def handler(input: dict[str, Any], ctx: ToolCtx) -> ToolOutcome:
    pattern = input["pattern"]
    root = Path(input.get("path") or ctx.cwd)
    if not root.is_absolute():
        root = (Path(ctx.cwd) / root).resolve()
    glob = input.get("glob")
    no_ignore = bool(input.get("no_ignore"))
    limit = _clamp_limit(input.get("limit"))

    # Check the root before shelling out: ripgrep reports a missing directory as
    # a generic "rg error" under the "searched via rg under ..." header, which
    # reads like a search result and has been pasted back as a path.
    if not root.exists():
        note = missing_path_note(str(input.get("path") or ""), root)
        return ToolOutcome(
            text=f"path does not exist: {root}{note}", is_error=True
        )

    rg = shutil.which("rg")
    if rg:
        outcome = await _ripgrep(rg, pattern, root, glob, no_ignore, limit)
    else:
        outcome = await asyncio.to_thread(_pure_python_grep, pattern, root, glob, limit)

    # One choke point for the repeat-search note, so neither engine has to know
    # about it. The search still ran — the note only says its answer is already
    # in the conversation.
    if outcome.is_error:
        return outcome
    key = (pattern, str(root), glob, no_ignore, limit)
    note = repeat_search_note(key in ctx.grep_seen)
    ctx.grep_seen.add(key)
    return replace(outcome, text=outcome.text + note) if note else outcome


def _header(engine: str, root: Path) -> str:
    return f"searched via {engine} under {root}"


_MATCH_LINE_RE = re.compile(r"^(.*?):\d+:")


def _drop_sensitive_matches(lines: list[str]) -> tuple[list[str], int]:
    """Filter out match lines from credential files (.env, key files, …).

    A tree-wide Grep for "TOKEN" must not print .env contents into context.
    The files are named in a note instead of silently hidden — the model (and
    user) can still target one explicitly, which routes through the sensitive-
    file permission rule.
    """
    from fluid.tools.hardening import is_sensitive_path

    kept: list[str] = []
    dropped = 0
    for line in lines:
        match = _MATCH_LINE_RE.match(line)
        if match is not None and is_sensitive_path(match.group(1)):
            dropped += 1
        else:
            kept.append(line)
    return kept, dropped


def _sensitive_note(dropped: int) -> list[str]:
    if not dropped:
        return []
    return [
        "[hits in credential-pattern files (.env, keys, …) were withheld "
        "from this search — Read a file explicitly if it is genuinely needed]"
    ]


async def _ripgrep(
    rg: str, pattern: str, root: Path, glob: str | None, no_ignore: bool, limit: int
) -> ToolOutcome:
    head = _header("rg", root)
    # ``--max-count`` is a PER-FILE bound, so it cannot enforce the global cap
    # on its own — a thousand files each return up to ``limit`` lines. It stays
    # as a cheap per-file bound; the global slice happens below.
    args = [
        rg, "-n", "--no-heading", "--color=never",
        "--max-count", str(limit),
        "--max-filesize", _MAX_FILESIZE,
    ]
    if no_ignore:
        args.append("-uu")
    if glob:
        args += ["--glob", glob]
    # ``--`` so a pattern starting with '-' is not parsed as a flag.
    args += ["--", pattern, str(root)]

    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=RG_TIMEOUT_S)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError, OSError):
            proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        return ToolOutcome(
            text=f"{head}\nsearch timed out after {RG_TIMEOUT_S:g}s", is_error=True
        )

    # rg: 0 = matches, 1 = no matches, >=2 = real error (bad regex, bad path).
    if (proc.returncode or 0) >= 2:
        detail = stderr.decode(errors="replace").strip() or f"rg exited {proc.returncode}"
        return ToolOutcome(text=f"{head}\nrg error: {truncate(detail, 4000)}", is_error=True)

    lines = stdout.decode(errors="replace").split("\n")
    while lines and not lines[-1]:
        lines.pop()
    lines, dropped = _drop_sensitive_matches(lines)
    total = len(lines)
    notes: list[str] = _sensitive_note(dropped)
    if total > limit:
        lines = lines[:limit]
        notes.append(
            f"[showing first {limit} of {total} matches — "
            "pass a higher limit or narrow the pattern]"
        )
    text = truncate("\n".join(lines)).rstrip("\n")
    return ToolOutcome(text="\n".join([head, text or "(no matches)", *notes]))


def _pure_python_grep(
    pattern: str, root: Path, glob: str | None, limit: int = MAX_MATCHES
) -> ToolOutcome:
    """Bounded fallback used when ripgrep is not installed. Runs on a thread."""
    head = _header("python-fallback", root) + " (ripgrep not found)"
    try:
        rx = re.compile(pattern)
    except re.error as exc:
        return ToolOutcome(text=f"{head}\ninvalid pattern: {exc}", is_error=True)

    from fluid.tools.hardening import is_sensitive_path

    state = WalkState(deadline=time.monotonic() + FALLBACK_DEADLINE_S)
    # The same compiler Glob uses, so `*.{py,ts}`, `src/**/*.ts` and `!vendor/**`
    # mean the same thing here as they do under ripgrep. A glob this engine
    # cannot honour is a hard error: filtering everything out and reporting
    # "(no matches)" would read as a fact about the tree.
    try:
        glob_filter = compile_glob(glob) if glob else None
    except GlobSyntaxError as exc:
        return ToolOutcome(text=f"{head}\nunsupported glob {glob!r}: {exc}", is_error=True)
    out: list[str] = []
    capped = False
    dropped = 0
    for path in walk_files(root, state):
        if glob_filter is not None and not _glob_hit(glob_filter, path, root):
            continue
        if is_sensitive_path(str(path)):
            dropped += 1
            continue
        for hit in _search_file(path, rx, limit - len(out)):
            out.append(hit)
        if len(out) >= limit:
            capped = True
            break

    notes: list[str] = _sensitive_note(dropped)
    if capped:
        notes.append(
            f"[search truncated: match cap {limit} reached — "
            "pass a higher limit or narrow the pattern]"
        )
    if state.deadline_hit:
        notes.append(
            f"[search truncated: deadline reached after {state.files_seen} files]"
        )
    body = truncate("\n".join(out)) or "(no matches)"
    return ToolOutcome(text="\n".join([head, body, *notes]))


def _glob_hit(glob_filter: GlobFilter, path: Path, root: Path) -> bool:
    """Does ``path`` pass the glob filter, as a root-relative POSIX path?"""
    try:
        rel = path.relative_to(root).as_posix()
    except ValueError:
        rel = path.name
    return glob_filter.matches(rel)


def _search_file(path: Path, rx: re.Pattern[str], remaining: int) -> Iterator[str]:
    """Yield ``path:lineno:line`` for up to ``remaining`` matches in one file."""
    if remaining <= 0:
        return
    try:
        if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
            return
        if is_binary(path):
            return
        with path.open("rb") as fh:
            for lineno, raw in enumerate(fh, 1):
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if rx.search(line):
                    yield f"{path}:{lineno}:{line}"
                    remaining -= 1
                    if remaining <= 0:
                        return
    except OSError:
        return
