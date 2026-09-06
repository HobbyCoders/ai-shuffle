"""Ledger — the never-lossy pinned state that survives all compaction.

The one model-curated piece of the compaction design (the transcript tiers in
:mod:`fluid.context.compaction` are strictly mechanical): user constraints,
decisions, task state and open threads that must stay resident verbatim no
matter how much history is digested away. Mechanically extracting "what the
user constrained" is not possible, so the model maintains it through this
tool.

Persistence is a per-session file (Note's pattern) under
``<cwd>/.fluid/ledger/``. The engine appends the current ledger to every new
user turn as a system-reminder block, so the newest copy always rides the
recent window — which compaction never digests — while older copies are
stripped when their turns digest. Never-lossy without ever pinning anything in
the cached prompt prefix.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fluid.tools.registry import ToolCtx, ToolOutcome, ToolSpec

SECTIONS = ("constraints", "decisions", "state", "threads")


def _replace_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` through a temp file and one rename.

    The ledger is the state that must survive everything, so a crash mid-write
    must leave the previous ledger, never a half-written one.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)

SPEC = ToolSpec(
    name="Ledger",
    description=(
        "The session ledger: pinned state that survives all history "
        "compaction verbatim — the whole ledger is re-shown on every user "
        "turn, so you are always holding the current copy. Use it for "
        "user-stated constraints, decisions made, current task state, and "
        "open threads.\n"
        "\n"
        "Actions: 'add' appends an entry to a section (section + text); "
        "'remove' deletes the one entry in a section whose text matches "
        "exactly (section + text); 'replace' swaps a section's entire "
        "contents for `entries` (section + entries) — use it to roll task "
        "state forward in ONE call instead of a remove-per-line then an "
        "add-per-line; 'show' re-renders the ledger.\n"
        "\n"
        "add/remove/replace answer with a one-line delta, not the ledger — "
        "the copy on this turn is still accurate apart from that delta, so "
        "'show' is only worth a call when you have lost track. Keep entries "
        "terse: every entry costs context on every turn."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["add", "remove", "replace", "show"],
            },
            "section": {
                "type": "string",
                "enum": list(SECTIONS),
                "description": "Target section (action=add/remove/replace).",
            },
            "text": {
                "type": "string",
                "description": (
                    "Entry to add, or the full text of the entry to remove "
                    "(matching is whole-entry, not substring)."
                ),
            },
            "entries": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "action=replace: the section's complete new contents. An "
                    "empty list clears the section."
                ),
            },
        },
        "required": ["action"],
    },
    risk="safe",
)

_HEADER = "# Session ledger"


def _ledger_path(cwd: str | None, session_id: str | None) -> Path:
    return (
        Path(cwd or ".")
        / ".fluid"
        / "ledger"
        / f"{session_id or 'default'}.md"
    )


def _parse(text: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {s: [] for s in SECTIONS}
    current: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            name = stripped[3:].strip().lower()
            current = name if name in sections else None
        elif stripped.startswith("- ") and current is not None:
            sections[current].append(stripped[2:])
    return sections


def _render(sections: dict[str, list[str]]) -> str:
    lines = [_HEADER]
    for name in SECTIONS:
        entries = sections.get(name) or []
        if not entries:
            continue
        lines.append(f"## {name}")
        lines.extend(f"- {e}" for e in entries)
    return "\n".join(lines) if len(lines) > 1 else ""


def read_ledger(ctx: ToolCtx, session_id: str | None = None) -> str:
    """The current ledger text, or "" when empty/absent. ``session_id``
    overrides the ctx's (the engine knows the session before ctx does)."""
    path = _ledger_path(ctx.cwd, session_id or ctx.session_id)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    return _render(_parse(text))


# ``remove`` matches a whole entry, never a substring: one imprecise call must
# not be able to empty a section. A miss lists the near entries instead.
_CANDIDATES_SHOWN = 5


def _no_match(section: str, needle: str, entries: list[str]) -> ToolOutcome:
    """A remove that matched no whole entry: delete nothing, show the near ones."""
    near = [e for e in entries if needle.lower() in e.lower()]
    if not near:
        return ToolOutcome(
            text=(
                f'no {section} entry reads exactly "{needle}" — nothing was '
                "removed. remove matches a whole entry, not a fragment of one; "
                "action=show lists the current entries."
            ),
            is_error=True,
        )
    shown = near[:_CANDIDATES_SHOWN]
    listing = "\n".join(f"- {e}" for e in shown)
    more = (
        f"\n({len(near) - len(shown)} more also contain it.)"
        if len(near) > len(shown)
        else ""
    )
    return ToolOutcome(
        text=(
            f'no {section} entry reads exactly "{needle}" — nothing was '
            "removed. remove matches a whole entry, not a fragment of one. "
            f"These {section} entries contain it:\n{listing}{more}\n"
            "Re-issue remove with one of those lines verbatim."
        ),
        is_error=True,
    )


def render_ledger_reminder(text: str) -> str:
    return (
        "<system-reminder>\n"
        "Session ledger (pinned state — survives history compaction; curate "
        "with the Ledger tool):\n"
        f"{text}\n</system-reminder>"
    )


def _new_entries(input: dict[str, Any]) -> list[str] | None:
    """``entries`` for a replace, accepting a list or a newline-separated blob.

    Models write both, and rejecting the second spelling would cost the call
    this action exists to save.
    """
    raw = input.get("entries")
    if raw is None:
        raw = input.get("text")
        if raw is None:
            return None
    if isinstance(raw, str):
        lines = raw.splitlines()
    elif isinstance(raw, (list, tuple)):
        lines = [str(item) for item in raw]
    else:
        return None
    out: list[str] = []
    for line in lines:
        entry = line.strip()
        if entry.startswith("- "):
            entry = entry[2:].strip()
        if entry:
            out.append(entry)
    return out


def _counts(sections: dict[str, list[str]], section: str) -> str:
    total = sum(len(v) for v in sections.values())
    return f"{len(sections.get(section) or [])} in {section}, {total} total"


async def handler(input: dict[str, Any], ctx: ToolCtx) -> ToolOutcome:
    action = str(input.get("action", "")).strip().lower()
    # "rotate" is what models call the replace idiom; accept their word for it.
    if action == "rotate":
        action = "replace"
    path = _ledger_path(ctx.cwd, ctx.session_id)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        raw = ""
    sections = _parse(raw)
    if action in ("add", "remove", "replace"):
        section = str(input.get("section", "")).strip().lower()
        if section not in SECTIONS:
            return ToolOutcome(
                text=f"action={action} requires section (one of {', '.join(SECTIONS)})",
                is_error=True,
            )
        text = str(input.get("text", "")).strip()
        if action == "replace":
            entries = _new_entries(input)
            if entries is None:
                return ToolOutcome(
                    text=(
                        "action=replace requires entries (the section's "
                        "complete new contents, as a list of strings)"
                    ),
                    is_error=True,
                )
            before = len(sections[section])
            sections[section] = entries
            delta = (
                f"replaced {section}: {before} entr"
                f'{"ies" if before != 1 else "y"} out, {len(entries)} in — '
                f"{_counts(sections, section)}"
            )
        elif action == "add":
            if not text:
                return ToolOutcome(text=f"action={action} requires text", is_error=True)
            sections[section].append(text)
            delta = f'added 1 {section} entry ("{text}") — {_counts(sections, section)}'
        else:
            if not text:
                return ToolOutcome(text=f"action={action} requires text", is_error=True)
            entries = sections[section]
            target = text.lower()
            removed = [e for e in entries if e.strip().lower() == target]
            kept = [e for e in entries if e.strip().lower() != target]
            if not removed:
                return _no_match(section, text, entries)
            sections[section] = kept
            delta = (
                f'removed {len(removed)} {section} entr'
                f'{"ies" if len(removed) != 1 else "y"} ("{removed[0]}") — '
                f"{_counts(sections, section)}"
            )
        rendered = _render(sections)
        try:
            if rendered:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(rendered + "\n", encoding="utf-8")
            else:
                path.unlink(missing_ok=True)
        except OSError as exc:
            return ToolOutcome(text=f"failed to write ledger: {exc}", is_error=True)
        # A delta, not the ledger. Echoing the whole rendered ledger after
        # every mutation put 1.18 MB of tool output into one audited session —
        # 19% of all its tool-result bytes — for state the engine already
        # recites onto every user turn.
        return ToolOutcome(text=delta)
    if action != "show":
        return ToolOutcome(text=f"unknown action: {action}", is_error=True)
    return ToolOutcome(text=_render(sections) or f"{_HEADER}\n(empty)")
