"""Edit — exact string replacement with read-before-write + uniqueness.

Enforces two of the tool-design-discipline rules:
- the file must have been Read this session (``ctx.read_files``);
- ``old_string`` must be unique in the file unless ``replace_all`` is set.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fluid.tools.builtins._util import resolve
from fluid.tools.registry import ToolCtx, ToolOutcome, ToolSpec

SPEC = ToolSpec(
    name="Edit",
    description=(
        "Perform an exact string replacement in an existing file. When you "
        "say you will edit or update file content, invoke Edit immediately.\n"
        "\n"
        "Rules:\n"
        "- The file must have been Read earlier in this session, or the call "
        "fails.\n"
        "- old_string must match the file byte-for-byte, including "
        "indentation and line breaks. Do not include Read's line-number "
        "prefix (the leading number and tab) — that is display formatting, "
        "not file content.\n"
        "- old_string must be unique in the file; if it matches more than "
        "once the edit fails. Add surrounding lines to make it unique, or "
        "pass replace_all=true to change every occurrence (useful for "
        "renames).\n"
        "- After a successful Edit, do not Read the file back just to "
        "verify — a failed match would have returned an error instead."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "Absolute or cwd-relative path of the file to modify.",
            },
            "old_string": {
                "type": "string",
                "description": "The exact existing text to replace.",
            },
            "new_string": {
                "type": "string",
                "description": "The replacement text (must differ from old_string).",
            },
            "replace_all": {
                "type": "boolean",
                "description": "Replace every occurrence of old_string (default false).",
            },
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
