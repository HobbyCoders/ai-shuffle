"""Read — return file contents (cat -n style) and record the read.

Reading registers the file in ``ctx.read_files`` so Edit can enforce
read-before-write within the session (the Anthropic tool-discipline rule).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fluid.tools.builtins._util import resolve
from fluid.tools.registry import ToolCtx, ToolOutcome, ToolSpec

SPEC = ToolSpec(
    name="Read",
    description="Read a file from the filesystem. Returns contents with line numbers.",
    input_schema={
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "Absolute or cwd-relative path."},
            "offset": {"type": "integer", "description": "1-based first line to read."},
            "limit": {"type": "integer", "description": "Max lines to read."},
        },
        "required": ["file_path"],
    },
    risk="safe",
)


async def handler(input: dict[str, Any], ctx: ToolCtx) -> ToolOutcome:
    path: Path = resolve(input["file_path"], ctx)
    if not path.exists() or not path.is_file():
        return ToolOutcome(text=f"file not found: {path}", is_error=True)
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    offset = max(1, int(input.get("offset", 1)))
    limit = input.get("limit")
    end = offset - 1 + int(limit) if limit else len(lines)
    window = lines[offset - 1:end]
    numbered = "\n".join(f"{offset + i}\t{line}" for i, line in enumerate(window))
    ctx.read_files.add(str(path))
    return ToolOutcome(text=numbered or "(empty file)")
