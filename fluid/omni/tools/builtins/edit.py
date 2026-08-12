"""Edit — exact string replacement with read-before-write + uniqueness.

Enforces two of the tool-design-discipline rules:
- the file must have been Read this session (``ctx.read_files``);
- ``old_string`` must be unique in the file unless ``replace_all`` is set.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from omni.tools.builtins._util import resolve
from omni.tools.registry import ToolCtx, ToolOutcome, ToolSpec

SPEC = ToolSpec(
    name="Edit",
    description="Replace an exact string in a file. Requires the file to have been Read; "
    "old_string must be unique unless replace_all is true.",
    input_schema={
        "type": "object",
        "properties": {
            "file_path": {"type": "string"},
            "old_string": {"type": "string"},
            "new_string": {"type": "string"},
            "replace_all": {"type": "boolean"},
        },
        "required": ["file_path", "old_string", "new_string"],
    },
    risk="write",
)


async def handler(input: dict[str, Any], ctx: ToolCtx) -> ToolOutcome:
    path: Path = resolve(input["file_path"], ctx)
    if str(path) not in ctx.read_files:
        return ToolOutcome(
            text=f"must Read {path} before editing it", is_error=True
        )
    if not path.exists():
        return ToolOutcome(text=f"file not found: {path}", is_error=True)

    old = input["old_string"]
    new = input["new_string"]
    replace_all = bool(input.get("replace_all", False))
    content = path.read_text(encoding="utf-8")
    count = content.count(old)
    if count == 0:
        return ToolOutcome(text="old_string not found in file", is_error=True)
    if count > 1 and not replace_all:
        return ToolOutcome(
            text=f"old_string is not unique ({count} matches); pass replace_all or add context",
            is_error=True,
        )
    updated = content.replace(old, new) if replace_all else content.replace(old, new, 1)
    path.write_text(updated, encoding="utf-8")
    n = count if replace_all else 1
    return ToolOutcome(text=f"edited {path} ({n} replacement{'s' if n != 1 else ''})")
