"""Note — external plan/notes file with recitation (Manus-style).

Notes live OUTSIDE the model context in a per-session file under
``<cwd>/.fluid/notes/``; every mutation returns the complete current notes so
the plan is recited back into recent context (the part attention actually
reads) instead of fading out mid-window. The engine also recites non-empty
notes onto each new user turn (see :func:`read_notes` and the native loop's
reminder plumbing), so a plan written once stays visible all session. Nothing
here touches the prompt prefix, so notes never invalidate the provider cache.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fluid.tools.registry import ToolCtx, ToolOutcome, ToolSpec

SPEC = ToolSpec(
    name="Note",
    description=(
        "Working notes for plans and task progress — your todo list through "
        "a session. Actions: 'add' appends a note (requires text), 'list' "
        "returns all notes, 'clear' empties them. Notes live outside the "
        "context window; calling Note is what brings them back into view. "
        "Before multi-step work, add the steps as notes and check them off "
        "(add a done marker or restate remaining work) as you go."
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
    return Path(ctx.cwd or ".") / ".fluid" / "notes" / f"{session}.md"


def _read_notes(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def read_notes(ctx: ToolCtx, session_id: str | None = None) -> str:
    """The current notes text, or "" when empty/absent. ``session_id``
    overrides the ctx's (the engine knows the session before ctx does)."""
    path = _notes_path(
        ToolCtx(cwd=ctx.cwd, session_id=session_id or ctx.session_id)
    )
    return _read_notes(path).strip()


def render_notes_reminder(text: str) -> str:
    return (
        "<system-reminder>\n"
        "Session notes (working plan and progress — keep current with the "
        "Note tool):\n"
        f"{text}\n</system-reminder>"
    )


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
