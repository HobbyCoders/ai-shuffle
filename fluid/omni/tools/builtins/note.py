"""Note — external plan/notes file with recitation (Manus-style).

Notes live OUTSIDE the model context in a per-session file under
``<cwd>/.omni/notes/``; every mutation returns the complete current notes so
the plan is recited back into recent context (the part attention actually
reads) instead of fading out mid-window. Nothing here touches the prompt
prefix, so notes never invalidate the provider cache.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from omni.tools.registry import ToolCtx, ToolOutcome, ToolSpec

SPEC = ToolSpec(
    name="Note",
    description=(
        "Session notes for plans and durable working state. Actions: 'add' "
        "appends a note (requires text), 'list' returns all notes, 'clear' "
        "empties them. add/clear return the full notes afterward — use notes "
        "to keep multi-step plans on track."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["add", "list", "clear"]},
            "text": {"type": "string", "description": "The note (action=add)."},
        },
        "required": ["action"],
    },
    risk="safe",
)


def _notes_path(ctx: ToolCtx) -> Path:
    session = ctx.session_id or "default"
    return Path(ctx.cwd or ".") / ".omni" / "notes" / f"{session}.md"


def _read_notes(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


async def handler(input: dict[str, Any], ctx: ToolCtx) -> ToolOutcome:
    action = str(input.get("action", "")).strip()
    path = _notes_path(ctx)
    if action == "add":
        text = str(input.get("text", "")).strip()
        if not text:
            return ToolOutcome(text="action=add requires text", is_error=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"- {text}\n")
    elif action == "clear":
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            return ToolOutcome(text=f"failed to clear notes: {exc}", is_error=True)
    elif action != "list":
        return ToolOutcome(text=f"unknown action: {action}", is_error=True)
    notes = _read_notes(path).strip()
    return ToolOutcome(text=f"# Notes\n{notes}" if notes else "# Notes\n(empty)")
