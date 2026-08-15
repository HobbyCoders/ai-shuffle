"""Write — create or overwrite a file."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fluid.tools.builtins._util import resolve
from fluid.tools.registry import ToolCtx, ToolOutcome, ToolSpec

SPEC = ToolSpec(
    name="Write",
    description="Write content to a file, creating parent directories as needed.",
    input_schema={
        "type": "object",
        "properties": {
            "file_path": {"type": "string"},
            "content": {"type": "string"},
        },
        "required": ["file_path", "content"],
    },
    risk="write",
)


async def handler(input: dict[str, Any], ctx: ToolCtx) -> ToolOutcome:
    path: Path = resolve(input["file_path"], ctx)
    content = input.get("content", "")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    # A freshly written file is known to the session (so a follow-up Edit works).
    ctx.read_files.add(str(path))
    return ToolOutcome(text=f"wrote {len(content)} bytes to {path}")
