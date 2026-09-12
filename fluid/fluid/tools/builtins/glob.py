"""Glob — find files by glob pattern, newest first.

Shares the pruned, deadline-bounded walk with Grep's fallback. Exists so a
model that wants "which files look like X" has a cheap tool for it instead of
reaching for a repo-wide content search.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from fluid.tools.builtins._util import (
    GlobSyntaxError,
    WalkState,
    compile_glob,
    truncate,
    walk_files,
)
from fluid.tools.registry import ToolCtx, ToolOutcome, ToolSpec

MAX_RESULTS = 200
MAX_LIMIT = 5000
DEADLINE_S = 10.0

def describe(tools: frozenset[str] | None = None) -> str:
    """Glob's description, specialized to the session's tool surface."""
    has = (lambda name: tools is None or name in tools)
    opening = "Find files by glob pattern (e.g. **/*.py, src/**/*.ts)." + (
        " Prefer this over find/ls via Bash for locating files by name or "
        "extension."
        if has("Bash")
        else ""
    )
    return (
        opening + "\n"
        "\n"
        "Returns ONE newline-joined string, not a list: a header line naming "
        "the searched root, then one matching path per line (relative to that "
        "root), sorted by modification time, newest first — so the files most "
        "likely to matter come first. In code mode split it before counting "
        "or slicing (`out.splitlines()[1:]`); `len(out)` counts characters and "
        "`out[:300]` cuts mid-path. No hits gives the header plus the literal "
        f"line `(no matches)`. Results are capped at {MAX_RESULTS} by default "
        f"(a note tells you when the cap was hit; raise `limit` up to "
        f"{MAX_LIMIT} or narrow the pattern). Heavy directories "
        "(node_modules, .git, build output, virtualenvs) are pruned from "
        "the walk.\n"
        "\n"
        "This is a recursive file search, not a directory listing: a pattern "
        "without `/` matches basenames at any depth; `src/*.py` is rooted "
        "under `path`, while `src/**/*.py` also includes descendants. "
        "Directories themselves are not returned. "
        "Patterns accept `**`, `?`, character classes and `{a,b}` alternation, "
        "and a leading `!` inverts the pattern into an exclusion (e.g. "
        "!{vendor/**,dist/**}). A pattern that cannot be honoured comes back as "
        "an error naming the construct, never as an empty result."
    )


SPEC = ToolSpec(
    name="Glob",
    description=describe(),
    input_schema={
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": (
                    "Glob pattern; ** matches any depth. A pattern with no '/' "
                    "matches the file name at any depth. A leading '!' lists "
                    "everything EXCEPT what matches, e.g. !{vendor/**,dist/**}."
                ),
            },
            "path": {"type": "string", "description": "Directory to search (default cwd)."},
            "limit": {
                "type": "integer",
                "description": (
                    f"Maximum matches to return (default {MAX_RESULTS}). Raise it "
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
        return MAX_RESULTS
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return MAX_RESULTS
    return max(1, min(MAX_LIMIT, value))


async def handler(input: dict[str, Any], ctx: ToolCtx) -> ToolOutcome:
    root = Path(input.get("path") or ctx.cwd)
    if not root.is_absolute():
        root = (Path(ctx.cwd) / root).resolve()
    limit = _clamp_limit(input.get("limit"))
    return await asyncio.to_thread(_glob, input["pattern"], root, limit)


def _glob(pattern: str, root: Path, limit: int = MAX_RESULTS) -> ToolOutcome:
    """Bounded glob walk. Runs on a thread — the walk can touch many paths."""
    head = f"globbed under {root}"
    if not root.exists():
        from fluid.tools.builtins._util import missing_path_note

        note = missing_path_note(str(root), root)
        # Soft in code mode, like Grep: the text is the call's result and the
        # cell continues. The JSON surface still sees is_error.
        return ToolOutcome(
            text=f"(path missing: {root}{note or ' — no near match'})", is_error=True,
            error_code="missing_path", soft_error=True,
        )
    # A pattern this compiler cannot honour is an error, not an empty listing:
    # "(no matches)" reads as a fact about the tree and gets acted on as one.
    try:
        matcher = compile_glob(pattern)
    except GlobSyntaxError as exc:
        return ToolOutcome(text=f"unsupported glob {pattern!r}: {exc}", is_error=True)

    # Collect everything the walk finds (up to a hard ceiling), THEN sort, THEN
    # cap: truncating during the walk and sorting afterwards would return an
    # arbitrary subset that merely *looks* like "the newest files".
    state = WalkState(deadline=time.monotonic() + DEADLINE_S)
    hits: list[tuple[float, Path]] = []
    ceiling_hit = False
    for path in walk_files(root, state):
        try:
            rel = path.relative_to(root).as_posix()
        except ValueError:
            rel = path.name
        if not matcher.matches(rel):
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0.0
        hits.append((mtime, path))
        if len(hits) >= MAX_LIMIT:
            ceiling_hit = True
            break

    hits.sort(key=lambda pair: (-pair[0], str(pair[1])))
    total = len(hits)
    notes: list[str] = []
    if total > limit:
        hits = hits[:limit]
        notes.append(
            f"[showing newest {limit} of {total} matched files — "
            "pass a higher limit or narrow the pattern]"
        )
    if ceiling_hit:
        notes.append(f"[walk stopped: hard ceiling {MAX_LIMIT} matched files reached]")
    if state.deadline_hit:
        notes.append(f"[search truncated: deadline reached after {state.files_seen} files]")
    # Paths print relative to the root named in the header: the root is stated
    # once, and a deep working directory would otherwise cost ~30 tokens per
    # line for nothing.
    def _show(path: Path) -> str:
        try:
            return path.relative_to(root).as_posix()
        except ValueError:
            return str(path)

    body = truncate("\n".join(_show(p) for _, p in hits)) or "(no matches)"
    return ToolOutcome(text="\n".join([head, body, *notes]))
