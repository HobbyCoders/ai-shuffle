"""Grep — regex search, ripgrep when available, pure-Python fallback."""

from __future__ import annotations

import asyncio
import re
import shutil
from pathlib import Path
from typing import Any

from omni.tools.builtins._util import truncate
from omni.tools.registry import ToolCtx, ToolOutcome, ToolSpec

SPEC = ToolSpec(
    name="Grep",
    description="Search file contents by regular expression. Returns matching lines.",
    input_schema={
        "type": "object",
        "properties": {
            "pattern": {"type": "string"},
            "path": {"type": "string", "description": "File or directory (default cwd)."},
            "glob": {"type": "string", "description": "Filename glob filter, e.g. *.py."},
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

    rg = shutil.which("rg")
    if rg:
        args = [rg, "-n", "--no-heading", "--color=never"]
        if glob:
            args += ["--glob", glob]
        args += [pattern, str(root)]
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await proc.communicate()
        text = stdout.decode(errors="replace")
        return ToolOutcome(text=truncate(text) or "(no matches)")

    return _pure_python_grep(pattern, root, glob)


def _pure_python_grep(pattern: str, root: Path, glob: str | None) -> ToolOutcome:
    try:
        rx = re.compile(pattern)
    except re.error as exc:
        return ToolOutcome(text=f"invalid pattern: {exc}", is_error=True)
    files = (
        [root] if root.is_file()
        else sorted(root.rglob(glob) if glob else root.rglob("*"))
    )
    out: list[str] = []
    for f in files:
        if not f.is_file():
            continue
        try:
            for i, line in enumerate(f.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if rx.search(line):
                    out.append(f"{f}:{i}:{line}")
        except OSError:
            continue
    return ToolOutcome(text=truncate("\n".join(out)) or "(no matches)")
