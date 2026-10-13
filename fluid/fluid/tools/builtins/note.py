"""Note — external plan/notes file with recitation (Manus-style).

Notes live OUTSIDE the model context in a per-session file under
the global per-project harness root (``notes/``); every mutation returns the
complete current notes so the plan is recited back into recent context (the part attention actually
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
        "'remove' deletes the notes whose text matches exactly and takes the "
        "same batch forms as add (retire a finished checklist in one call), "
        "'list' returns all notes, "
        "'clear' empties them. Notes live outside the context window; calling "
        "Note is what brings them back into view, and non-empty notes are "
        "recited on every later turn, so they cost context while they exist. "
        "For tasks under ~5 steps skip Note; a long or multi-turn plan is what "
        "it is for. Add the steps in one call, and batch removals with "
        "`texts` (retire several finished steps at once) rather than one call "
        "per step. Keep only notes that still matter; do not leave completed "
        "work in the list, and do not mark items done in place."
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
                    "The note (action=add/remove), one note per line to batch "
                    "several in one call. For remove, each line is a note's "
                    "full text — matching is whole-note, not substring."
                ),
            },
            "texts": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Batch: several notes in one call (action=add or remove)."
                ),
            },
        },
        "required": ["action"],
    },
    risk="safe",
)


def _notes_path(ctx: ToolCtx, session_id: str | None = None) -> Path:
    from fluid.state import state_path

    return state_path(ctx, "notes", session_id)


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


def _notes_from_payload(payload: dict[str, Any]) -> list[str]:
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
    # Keep an empty canonical tombstone so legacy notes cannot reappear.
    from fluid.tools.builtins.ledger import _replace_text

    _replace_text(path, _render(lines) + ("\n" if lines else ""))


def read_notes(ctx: ToolCtx, session_id: str | None = None) -> str:
    """The current notes text, or "" when empty/absent. ``session_id``
    overrides the ctx's (the engine knows the session before ctx does)."""
    from fluid.state import read_state

    return read_state(ctx, "notes", session_id).strip()


def render_notes_reminder(text: str) -> str:
    return (
        "<system-reminder>\n"
        "Session notes (scratchpad — remove finished steps with the "
        "Note tool so the list stays current):\n"
        f"{text}\n</system-reminder>"
    )


def _outcome(ctx: ToolCtx, prefix: str = "") -> ToolOutcome:
    notes = read_notes(ctx)
    body = f"# Notes\n{notes}" if notes else "# Notes\n(empty)"
    return ToolOutcome(text=f"{prefix}\n{body}" if prefix else body)


# ``remove`` matches whole notes, not every note *containing* the text, so one
# imprecise call cannot delete a checklist; a miss lists the near ones instead
# of guessing.
_CANDIDATES_SHOWN = 5


def _quoted(needles: list[str]) -> str:
    """``"a"`` / ``"a" or "b"`` — the needles, for the no-match message."""
    quoted = [f'"{n}"' for n in needles]
    if len(quoted) == 1:
        return quoted[0]
    return ", ".join(quoted[:-1]) + " or " + quoted[-1]


def _no_match(needles: list[str], existing: list[str]) -> ToolOutcome:
    """A remove that matched no whole note: delete nothing, show the near ones.

    ``soft_error`` because removing a note that is already gone is an
    idempotent no-op, not a failed step: on the code-mode surface a hard error
    raises inside the cell and throws away every call after it.
    """
    wanted = _quoted(needles)
    near = [
        line
        for line in existing
        if any(needle.lower() in line.lower() for needle in needles)
    ]
    if not near:
        return ToolOutcome(
            text=(
                f"no note reads exactly {wanted} — nothing was removed. "
                "remove matches a whole note, not a fragment of one; "
                "action=list shows the current notes."
            ),
            is_error=True,
            soft_error=True,
        )
    shown = near[:_CANDIDATES_SHOWN]
    listing = "\n".join(f"- {line}" for line in shown)
    more = (
        f"\n({len(near) - len(shown)} more also contain it.)"
        if len(near) > len(shown)
        else ""
    )
    return ToolOutcome(
        text=(
            f"no note reads exactly {wanted} — nothing was removed. remove "
            f"matches a whole note, not a fragment of one. These notes contain "
            f"it:\n{listing}{more}\n"
            "Re-issue remove with one of those lines verbatim."
        ),
        is_error=True,
        soft_error=True,
    )


async def handler(input: dict[str, Any], ctx: ToolCtx) -> ToolOutcome:
    action = str(input.get("action", "")).strip()
    path = _notes_path(ctx)
    prefix = ""
    if action == "add":
        incoming = _notes_from_payload(input)
        if not incoming:
            return ToolOutcome(
                text="action=add requires text or texts", is_error=True
            )
        existing = _parse_lines(read_notes(ctx))
        try:
            _write_lines(path, existing + incoming)
        except OSError as exc:
            return ToolOutcome(text=f"failed to write notes: {exc}", is_error=True)
    elif action == "remove":
        # Same batch forms as add, so retiring a finished checklist is one call.
        needles = _notes_from_payload(input)
        if not needles:
            return ToolOutcome(
                text="action=remove requires text or texts", is_error=True
            )
        existing = _parse_lines(read_notes(ctx))
        targets = {needle.lower() for needle in needles}
        removed = [line for line in existing if line.strip().lower() in targets]
        kept = [line for line in existing if line.strip().lower() not in targets]
        if not removed:
            return _no_match(needles, existing)
        try:
            _write_lines(path, kept)
        except OSError as exc:
            return ToolOutcome(text=f"failed to write notes: {exc}", is_error=True)
        hit = {line.strip().lower() for line in removed}
        missed = [needle for needle in needles if needle.lower() not in hit]
        label = f'"{removed[0]}"'
        if len(removed) > 1:
            label += f" +{len(removed) - 1} more"
        prefix = (
            f'removed {len(removed)} note{"s" if len(removed) != 1 else ""} '
            f"({label}); {len(kept)} remain"
        )
        if missed:
            # Partial batch: the removals still applied, so this is a note on
            # a successful call, not a failure.
            prefix += f"; no note read exactly {_quoted(missed)}"
    elif action == "clear":
        try:
            _write_lines(path, [])
        except OSError as exc:
            return ToolOutcome(text=f"failed to clear notes: {exc}", is_error=True)
    elif action != "list":
        return ToolOutcome(text=f"unknown action: {action}", is_error=True)
    return _outcome(ctx, prefix)
