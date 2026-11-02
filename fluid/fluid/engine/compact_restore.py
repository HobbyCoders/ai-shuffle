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

One hook answer cannot carry the whole note: Claude Code saves hook context over
10,000 characters to a file and shows the model a 2 KB preview, so a longer note
only looked restored. :class:`RestorationQueue` sends the note in parts that fit,
one per hook answer, and credits a section when its last part has gone out. What
is left when the turn ends goes back on the next turn's tail.
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

DELIVERED = "delivered"
CUT = "cut"
OMITTED = "omitted"
# Part of it went out; the rest waits for the next hook answer or turn.
PENDING = "pending"

# A cuttable section is only cut when at least this much of it fits: a sliver
# of a memory snapshot is noise, and the next turn puts the whole one back.
MIN_CUT_CHARS = 1_000
SEPARATOR = "\n\n"
_CUT_NOTE = "\n[{name} cut: it was longer than {limit} characters]"
_SPLIT_NOTE = "\n[{name} continues in a later note]"


@dataclass(frozen=True)
class RestorationSection:
    """One part of the note a host puts back after a compaction.

    ``name`` is stable ("environment", "memory", ...): receipts and logs
    name sections by it, never by content. ``priority``: lower claims the
    budget first. ``max_chars`` bounds this section alone (None: only the
    note's own bound). ``cuttable`` lets an oversized section go out cut
    instead of not at all; a cut section is never credited as restored.
    ``on_delivered`` is called once, only when the section went out whole.
    ``next_turn_covers`` says the host's ``fresh_context`` already puts this
    section back on the next turn's tail, so the queue must not send it there too.
    """

    name: str
    text: str
    priority: int = 0
    max_chars: int | None = None
    cuttable: bool = False
    on_delivered: Callable[[], None] | None = None
    next_turn_covers: bool = False


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
    chosen, statuses, entries = _fit(sections, limit)
    note = SEPARATOR.join(chosen[i] for i, _, _ in entries if i in chosen)
    receipts = [
        SectionReceipt(name=s.name, status=statuses[i], chars=len(chosen.get(i, "")),
                       original_chars=len(text))
        for i, s, text in entries
    ]
    return note, receipts


def _fit(
    sections: Sequence[RestorationSection], limit: int,
) -> tuple[dict[int, str], dict[int, str], list[tuple[int, RestorationSection, str]]]:
    """:func:`pack_sections`'s choice: the text and status of each section by
    its position, and the non-empty sections."""
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
    return chosen, statuses, entries


@dataclass
class _Sending:
    """One section on its way out."""

    section: RestorationSection
    whole: str  # the section's own text, uncut
    final: str  # the status it ends with once ``left`` is out: delivered or cut
    left: str  # what has not gone out
    order: int = 0  # where the host put it: a part keeps the host's order
    sent: int = 0
    parts: int = 0


class RestorationQueue:
    """The note for one compaction, sent in parts that each fit one hook answer.

    ``total`` bounds the whole note (:func:`_fit`). :meth:`take` hands out the
    next part and credits a section (``on_delivered``) only when the last of it
    is out; :meth:`receipts` says where each one stands, and :meth:`finish` puts
    every section not yet whole on the next turn's tail. Parts only shrink what
    is left, so a queue always ends; a later compaction replaces it.
    """

    def __init__(self, compaction_id: str, total: int) -> None:
        self.compaction_id = compaction_id
        self.total = total
        self._used = 0
        self._sending: list[_Sending] = []
        self._omitted: list[_Sending] = []
        self._next = 0
        self.settled = False  # the leg has said how it ended, nothing is on the way

    def add(self, sections: Sequence[RestorationSection]) -> None:
        """Queue ``sections`` in priority order, within what ``total`` leaves."""
        chosen, statuses, entries = _fit(sections, self.total - self._used)
        by_order = {i: at for at, (i, _, _) in enumerate(entries)}
        base = self._next
        for i, section, text in sorted(entries, key=lambda e: (e[1].priority, e[0])):
            self._next = base + by_order[i]
            if statuses[i] == OMITTED:
                self._omitted.append(_Sending(section, text, OMITTED, ""))
                continue
            self._used += len(chosen[i]) + len(SEPARATOR)
            self._sending.append(_Sending(section, text, statuses[i], chosen[i], order=self._next))
        self._next = base + len(entries)

    @property
    def waiting(self) -> bool:
        """Some section still has text to send in a hook answer."""
        return any(item.left for item in self._sending)

    @property
    def complete(self) -> bool:
        """Every section went out whole."""
        return all(
            not item.left and item.final == DELIVERED for item in [*self._sending, *self._omitted]
        )

    def take(self, room: int) -> str:
        """The next part, at most ``room`` characters; "" when nothing fits.

        Whole sections go first. A section longer than the room left is split
        at a line break only when it is cuttable (a small one that sets
        behaviour waits for an answer with room for all of it).
        """
        parts: list[tuple[int, str]] = []
        left = room
        for item in self._sending:
            if not item.left:
                continue
            gap = len(SEPARATOR) if parts else 0
            lead = f"[{item.section.name}, continued]\n" if item.parts else ""
            size = len(lead) + len(item.left)
            if size + gap <= left:
                parts.append((item.order, lead + item.left))
                left -= size + gap
                item.sent, item.left, item.parts = item.sent + len(item.left), "", item.parts + 1
                self._credit(item)
                continue
            tail = _SPLIT_NOTE.format(name=item.section.name)
            fits = left - gap - len(lead) - len(tail)
            if not item.section.cuttable or fits < MIN_CUT_CHARS:
                continue
            cut = item.left.rfind("\n", MIN_CUT_CHARS // 2, fits + 1)
            cut = cut if cut > 0 else fits
            piece = item.left[:cut].rstrip()
            parts.append((item.order, lead + piece + tail))
            left -= gap + len(lead) + len(piece) + len(tail)
            rest = item.left[cut:].lstrip("\n")
            item.sent, item.left, item.parts = (
                item.sent + len(item.left) - len(rest), rest, item.parts + 1,
            )
        return SEPARATOR.join(text for _, text in sorted(parts, key=lambda part: part[0]))

    def finish(self, limit: int) -> str:
        """What the next turn's tail carries, within ``limit``: each section not
        yet whole, except one the host's ``fresh_context`` covers.

        Text that already went out is not repeated: a section partly sent sends
        what is missing, and only one that went out cut (its end is lost) or not
        at all goes back from its start. A section that does not fit stays cut or
        omitted, and the receipt says so.
        """
        again = [item for item in [*self._sending, *self._omitted]
                 if (item.left or item.final != DELIVERED) and not item.section.next_turn_covers]
        starts = [item.sent if item.left else 0 for item in again]
        chosen, statuses, _ = _fit(
            [RestorationSection(name=item.section.name,
                                text=item.left if item.final == DELIVERED
                                else item.whole[start:].lstrip("\n"),
                                priority=item.section.priority,
                                max_chars=item.section.max_chars,
                                cuttable=item.section.cuttable)
             for item, start in zip(again, starts)], limit,
        )
        for i, item in enumerate(again):
            if statuses.get(i) == DELIVERED:
                item.sent, item.left, item.final = len(item.whole), "", DELIVERED
                self._credit(item)
            elif statuses.get(i) == CUT:
                item.sent, item.left, item.final = starts[i] + len(chosen[i]), "", CUT
            elif item.left:  # part of it went out, and the rest does not fit
                item.left, item.final = "", CUT
        for item in self._sending:
            if item.section.next_turn_covers:
                item.left = ""
        return SEPARATOR.join(chosen[i] for i in range(len(again)) if i in chosen)

    def receipts(self) -> list[SectionReceipt]:
        """Where each section stands now, in the order they claimed the budget."""
        out = []
        for item in [*self._sending, *self._omitted]:
            status = PENDING if item.left else item.final
            out.append(SectionReceipt(name=item.section.name, status=status,
                                      chars=item.sent, original_chars=len(item.whole)))
        return out

    @staticmethod
    def _credit(item: _Sending) -> None:
        callback = item.section.on_delivered
        if item.final != DELIVERED or callback is None:
            return
        try:
            callback()
        except Exception:  # noqa: BLE001 — a host's bookkeeping never fails a hook
            logger.warning("the %s section's delivery callback failed", item.section.name,
                           exc_info=True)
