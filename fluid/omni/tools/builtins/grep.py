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
import fnmatch
import re
import shutil
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from omni.tools.builtins._util import (
    MAX_FILE_BYTES,
    WalkState,
    is_binary,
    truncate,
    walk_files,
)
from omni.tools.registry import ToolCtx, ToolOutcome, ToolSpec

MAX_MATCHES = 200
RG_TIMEOUT_S = 30.0
FALLBACK_DEADLINE_S = 10.0
_MAX_FILESIZE = "2M"

SPEC = ToolSpec(
    name="Grep",
    description=(
        "Search file contents by regular expression. Returns matching lines "
        f"(capped at {MAX_MATCHES}); respects ignore files unless no_ignore is set."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "pattern": {"type": "string"},
            "path": {"type": "string", "description": "File or directory (default cwd)."},
            "glob": {"type": "string", "description": "Filename glob filter, e.g. *.py."},
            "no_ignore": {
                "type": "boolean",
                "description": "Also search ignored/hidden files (ripgrep -uu).",
            },
        },
        "required": ["pattern"],
    },
    risk="safe",
)


async def handler(input: dict[str, Any], ctx: ToolCtx) -> ToolOutcome:
    pattern = input["pattern"]
    root = Path(input.get("path") or ctx.cwd)
    if not root.is_absolute():
        root = (Path(ctx.cwd) / root).resolve()
    glob = input.get("glob")
    no_ignore = bool(input.get("no_ignore"))

    rg = shutil.which("rg")
    if rg:
        return await _ripgrep(rg, pattern, root, glob, no_ignore)
    return await asyncio.to_thread(_pure_python_grep, pattern, root, glob)


def _header(engine: str, root: Path) -> str:
    return f"searched via {engine} under {root}"


async def _ripgrep(
    rg: str, pattern: str, root: Path, glob: str | None, no_ignore: bool
) -> ToolOutcome:
    head = _header("rg", root)
    args = [
        rg, "-n", "--no-heading", "--color=never",
        "--max-count", str(MAX_MATCHES),
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
    except (TimeoutError, asyncio.TimeoutError):
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

    text = truncate(stdout.decode(errors="replace")).rstrip("\n")
    return ToolOutcome(text=f"{head}\n{text or '(no matches)'}")


def _pure_python_grep(pattern: str, root: Path, glob: str | None) -> ToolOutcome:
    """Bounded fallback used when ripgrep is not installed. Runs on a thread."""
    head = _header("python-fallback", root) + " (ripgrep not found)"
    try:
        rx = re.compile(pattern)
    except re.error as exc:
        return ToolOutcome(text=f"{head}\ninvalid pattern: {exc}", is_error=True)

    state = WalkState(deadline=time.monotonic() + FALLBACK_DEADLINE_S)
    out: list[str] = []
    capped = False
    for path in walk_files(root, state):
        if glob and not fnmatch.fnmatch(path.name, glob):
            continue
        for hit in _search_file(path, rx, MAX_MATCHES - len(out)):
            out.append(hit)
        if len(out) >= MAX_MATCHES:
            capped = True
            break

    notes: list[str] = []
    if capped:
        notes.append(f"[search truncated: match cap {MAX_MATCHES} reached]")
    if state.deadline_hit:
        notes.append(
            f"[search truncated: deadline reached after {state.files_seen} files]"
        )
    body = truncate("\n".join(out)) or "(no matches)"
    return ToolOutcome(text="\n".join([head, body, *notes]))


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
