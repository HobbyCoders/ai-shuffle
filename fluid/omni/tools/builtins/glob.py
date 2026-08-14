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

from omni.tools.builtins._util import WalkState, glob_to_regex, truncate, walk_files
from omni.tools.registry import ToolCtx, ToolOutcome, ToolSpec

MAX_RESULTS = 200
DEADLINE_S = 10.0

SPEC = ToolSpec(
    name="Glob",
    description=(
        "Find files by glob pattern (e.g. **/*.py). Returns paths sorted by "
        f"modification time, newest first, capped at {MAX_RESULTS}."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": (
                    "Glob pattern; ** matches any depth. A pattern with no '/' "
                    "matches the file name at any depth."
                ),
            },
            "path": {"type": "string", "description": "Directory to search (default cwd)."},
        },
        "required": ["pattern"],
    },
    risk="safe",
)


async def handler(input: dict[str, Any], ctx: ToolCtx) -> ToolOutcome:
    root = Path(input.get("path") or ctx.cwd)
    if not root.is_absolute():
        root = (Path(ctx.cwd) / root).resolve()
    return await asyncio.to_thread(_glob, input["pattern"], root)


def _glob(pattern: str, root: Path) -> ToolOutcome:
    """Bounded glob walk. Runs on a thread — the walk can touch many paths."""
    head = f"globbed under {root}"
    if not root.exists():
        return ToolOutcome(text=f"path not found: {root}", is_error=True)
    rx = glob_to_regex(pattern)

    state = WalkState(deadline=time.monotonic() + DEADLINE_S)
    hits: list[tuple[float, Path]] = []
    capped = False
    for path in walk_files(root, state):
        try:
            rel = path.relative_to(root).as_posix()
        except ValueError:
            rel = path.name
        if not rx.match(rel):
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0.0
        hits.append((mtime, path))
        if len(hits) >= MAX_RESULTS:
            capped = True
            break

    hits.sort(key=lambda pair: (-pair[0], str(pair[1])))
    notes: list[str] = []
    if capped:
        notes.append(f"[results truncated: cap {MAX_RESULTS} reached]")
    if state.deadline_hit:
        notes.append(f"[search truncated: deadline reached after {state.files_seen} files]")
    body = truncate("\n".join(str(p) for _, p in hits)) or "(no matches)"
    return ToolOutcome(text="\n".join([head, body, *notes]))
