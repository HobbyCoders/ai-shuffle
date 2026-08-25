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
        "Working notes — a scratchpad for the current plan and thoughts "
        "through a session. Actions: 'add' appends one or more notes "
        "(text: one note, or several lines for a batch; texts: an array), "
        "'remove' deletes notes containing text, 'list' returns all notes, "
        "'clear' empties them. Notes live outside the context window; calling "
        "Note is what brings them back into view. Before multi-step work, add "
        "every current step in one call. When a step is finished, remove it. "
        "Keep only notes that still matter; do not leave completed work in "
        "the list, and do not mark items done in place."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["add", "remove", "list", "clear"],
            },
            "text": {
                "type": "string",
                "description": (
                    "The note (action=add/remove). For add, one note per line "
                    "to batch several in one call. For remove, a substring "
                    "identifying notes to delete."
                ),
            },
            "texts": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Batch add: several notes in one call (action=add).",
            },
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


def _parse_lines(raw: str) -> list[str]:
    lines: list[str] = []
    for item in raw.splitlines():
        stripped = item.strip()
        if stripped.startswith("- "):
            body = stripped[2:].strip()
            if body:
                lines.append(body)
    return lines


def _split_note_text(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("- "):
            line = line[2:].strip()
        if line:
            lines.append(line)
    return lines


def _notes_from_add(payload: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    text = payload.get("text")
    if isinstance(text, str):
        lines.extend(_split_note_text(text))
    texts = payload.get("texts")
    if isinstance(texts, list):
        for item in texts:
            if isinstance(item, str):
                lines.extend(_split_note_text(item))
            elif item is not None:
                lines.extend(_split_note_text(str(item)))
    return lines


def _render(lines: list[str]) -> str:
    if not lines:
        return ""
    return "\n".join(f"- {line}" for line in lines)


def _write_lines(path: Path, lines: list[str]) -> None:
    if lines:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_render(lines) + "\n", encoding="utf-8")
    else:
        path.unlink(missing_ok=True)


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
        "Session notes (scratchpad — remove finished steps with the "
        "Note tool so the list stays current):\n"
        f"{text}\n</system-reminder>"
    )


def _outcome(path: Path) -> ToolOutcome:
    notes = _read_notes(path).strip()
    return ToolOutcome(text=f"# Notes\n{notes}" if notes else "# Notes\n(empty)")


async def handler(input: dict[str, Any], ctx: ToolCtx) -> ToolOutcome:
    action = str(input.get("action", "")).strip()
    path = _notes_path(ctx)
    if action == "add":
        incoming = _notes_from_add(input)
        if not incoming:
            return ToolOutcome(
                text="action=add requires text or texts", is_error=True
            )
        existing = _parse_lines(_read_notes(path))
        try:
            _write_lines(path, existing + incoming)
        except OSError as exc:
            return ToolOutcome(text=f"failed to write notes: {exc}", is_error=True)
    elif action == "remove":
        needle = str(input.get("text", "")).strip()
        if not needle:
            return ToolOutcome(text="action=remove requires text", is_error=True)
        existing = _parse_lines(_read_notes(path))
        kept = [line for line in existing if needle.lower() not in line.lower()]
        if len(kept) == len(existing):
            return ToolOutcome(
                text=f'no note contains "{needle}"', is_error=True
            )
        try:
            _write_lines(path, kept)
        except OSError as exc:
            return ToolOutcome(text=f"failed to write notes: {exc}", is_error=True)
    elif action == "clear":
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            return ToolOutcome(text=f"failed to clear notes: {exc}", is_error=True)
    elif action != "list":
        return ToolOutcome(text=f"unknown action: {action}", is_error=True)
    return _outcome(path)
