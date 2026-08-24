"""Write — create or overwrite a file."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fluid.tools.builtins._util import detect_newline, read_text_preserving, resolve, write_text_with_newline
from fluid.tools.fragments import NO_VERIFY_READBACK, READ_BEFORE_EDIT
from fluid.tools.registry import ToolCtx, ToolOutcome, ToolSpec

def describe(tools: frozenset[str] | None = None) -> str:
    """Write's description, specialized to the session's tool surface."""
    has = (lambda name: tools is None or name in tools)
    edit_routing = (
        "Use Write for new files or complete rewrites; use Edit for partial "
        "changes to an existing file (overwriting a large file to change a "
        "few lines wastes tokens and risks losing content)."
        + (f" {READ_BEFORE_EDIT}" if has("Read") else "")
        if has("Edit")
        else "Use Write for new files or complete rewrites."
    )
    overwrite_clause = (
        " Overwriting is destructive: for a file you did not create this "
        "session, Read it first so you know what you are replacing.\n"
        if has("Read")
        else " Overwriting is destructive: be sure you know what you are "
        "replacing.\n"
    )
    verify_clause = (
        f"A successful Write is confirmed by its result: {NO_VERIFY_READBACK}"
        if has("Read")
        else "A successful Write is confirmed by its result; a failure would "
        "have returned an error instead."
    )
    return (
        "Create a new file or fully overwrite an existing one; parent "
        "directories are created as needed. When you say you will create or "
        "write file content, invoke Write immediately — do not draft the "
        "content only in reasoning or route it through shell redirection.\n"
        "\n"
        + edit_routing + overwrite_clause +
        "\n"
        + verify_clause +
        " Never create documentation files (*.md, README) unless the user "
        "explicitly asked for one."
    )


SPEC = ToolSpec(
    name="Write",
    description=describe(),
    input_schema={
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "Absolute or cwd-relative path of the file to write.",
            },
            "content": {
                "type": "string",
                "description": "The complete new file content (UTF-8).",
            },
        },
        "required": ["file_path", "content"],
    },
    risk="write",
)


async def handler(input: dict[str, Any], ctx: ToolCtx) -> ToolOutcome:
    path: Path = resolve(input["file_path"], ctx)
    content = input.get("content", "")
    path.parent.mkdir(parents=True, exist_ok=True)
    # Keep an existing file's line ending; a new file takes whatever the
    # content uses (LF unless it is CRLF throughout). Never platform-translate.
    if path.exists():
        newline = read_text_preserving(path)[1]
    else:
        newline = detect_newline(content)
    write_text_with_newline(path, content, newline)
    # A freshly written file is known to the session (so a follow-up Edit works).
    ctx.read_files.add(str(path))
    return ToolOutcome(text=f"wrote {len(content)} bytes to {path}")
