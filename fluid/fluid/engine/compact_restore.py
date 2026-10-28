"""The host's note put back after a Claude Code compaction, as sections.

After the CLI compacts, the delegated leg re-injects the host's turn context
(environment, memory, working record, Away mode, scratch) through the
SessionStart(compact) hook. It used to be one string cut at a fixed size and
credited as restored even when its later parts were cut off (#755).

A host now hands :class:`RestorationSection` items. :func:`pack_sections`
fits them into one bounded note: each section has its own bound, the sections
claim the shared budget in ``priority`` order (lowest first), and the note
keeps the order the host gave. A section is delivered whole, cut (only when
the host marked it ``cuttable``) or omitted, and the :class:`SectionReceipt`
list says which, so the leg credits only what really went out.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

DELIVERED = "delivered"
CUT = "cut"
OMITTED = "omitted"

# A cuttable section is only cut when at least this much of it fits: a sliver
# of a memory snapshot is noise, and the next turn puts the whole one back.
MIN_CUT_CHARS = 1_000
SEPARATOR = "\n\n"
_CUT_NOTE = "\n[{name} cut: it was longer than {limit} characters]"


@dataclass(frozen=True)
class RestorationSection:
    """One part of the note a host puts back after a compaction.

    ``name`` is stable ("environment", "memory", ...): receipts and logs
    name sections by it, never by content. ``priority``: lower claims the
    budget first. ``max_chars`` bounds this section alone (None: only the
    note's own bound). ``cuttable`` lets an oversized section go out cut
    instead of not at all; a cut section is never credited as restored.
    ``on_delivered`` is called once, only when the section went out whole.
    """

    name: str
    text: str
    priority: int = 0
    max_chars: int | None = None
    cuttable: bool = False
    on_delivered: Callable[[], None] | None = None


@dataclass(frozen=True)
class SectionReceipt:
    """What happened to one section: ``status`` is ``delivered``, ``cut`` or
    ``omitted``; ``chars`` is how much of it went out, ``original_chars`` its
    whole size."""

    name: str
    status: str
    chars: int
    original_chars: int

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "status": self.status, "chars": self.chars,
                "original_chars": self.original_chars}


def as_sections(note: Any) -> list[RestorationSection]:
    """A provider's answer as sections. A plain string (the older contract)
    is one cuttable section named ``note``; anything else that is not a
    sequence of sections gives none."""
    if isinstance(note, str):
        return [RestorationSection(name="note", text=note, cuttable=True)]
    if isinstance(note, Sequence):
        return [s for s in note if isinstance(s, RestorationSection)]
    return []


def pack_sections(
    sections: Sequence[RestorationSection], limit: int,
) -> tuple[str, list[SectionReceipt]]:
    """The note for ``sections`` within ``limit`` characters, and a receipt
    per non-empty section (in the order given).

    Sections claim the budget by ``priority`` (ties keep their order); each
    one fits whole, is cut to what is left when ``cuttable`` and at least
    ``MIN_CUT_CHARS`` of it fits, or is left out. Separators count against
    the budget. Below the limit the note is exactly the sections joined by a
    blank line, as one string note was before.
    """
    texts = [(s, s.text.strip()) for s in sections]
    entries = [(i, s, text) for i, (s, text) in enumerate(texts) if text]
    budget = max(0, int(limit))
    chosen: dict[int, str] = {}
    statuses: dict[int, str] = {}
    for i, section, text in sorted(entries, key=lambda e: (e[1].priority, e[0])):
        separator = len(SEPARATOR) if chosen else 0
        room = budget - separator
        bound = min(room, section.max_chars) if section.max_chars else room
        if len(text) <= bound:
            chosen[i], statuses[i] = text, DELIVERED
        elif section.cuttable and bound >= MIN_CUT_CHARS:
            cut = _CUT_NOTE.format(name=section.name, limit=bound)
            chosen[i] = text[: bound - len(cut)].rstrip() + cut
            statuses[i] = CUT
        else:
            statuses[i] = OMITTED
            continue
        budget -= separator + len(chosen[i])
    note = SEPARATOR.join(chosen[i] for i, _, _ in entries if i in chosen)
    receipts = [
        SectionReceipt(name=s.name, status=statuses[i], chars=len(chosen.get(i, "")),
                       original_chars=len(text))
        for i, s, text in entries
    ]
    return note, receipts
