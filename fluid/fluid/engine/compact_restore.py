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
only looked restored. It measures JavaScript string length, UTF-16 code units, so
every budget here is in those units (:func:`utf16_len`), where an emoji counts two.
Positions in a text stay Python indexes. :class:`RestorationQueue` sends the note in parts that fit,
one per hook answer. Handing a part out only stages it: the caller says whether
the answer carrying it was written (:meth:`RestorationQueue.acknowledge`) or not
(:meth:`RestorationQueue.rollback`), and only an acknowledged last part credits a
section. What is left when the turn ends goes back on the next turn's tail.
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import Any

logger = logging.getLogger(__name__)

DELIVERED = "delivered"
CUT = "cut"
OMITTED = "omitted"
# Part of it went out; the rest waits for the next hook answer or turn.
PENDING = "pending"
# A newer whole copy went out in its place, seen on the turn that carried it.
REPLACED = "replaced"

# A cuttable section is only cut when at least this much of it fits: a sliver
# of a memory snapshot is noise, and the next turn puts the whole one back.
MIN_CUT_CHARS = 1_000
SEPARATOR = "\n\n"
_CUT_NOTE = "\n[{name} cut: it was longer than {limit} characters]"
_SPLIT_NOTE = "\n[{name} continues in a later note]"


def utf16_len(text: str) -> int:
    """How long ``text`` is to the CLI: JavaScript's ``length``, in UTF-16 code units."""
    return len(text) if text.isascii() else len(text.encode("utf-16-le", "surrogatepass")) // 2


def utf16_index(text: str, units: int) -> int:
    """The Python index where the longest start of ``text`` within ``units`` UTF-16
    code units ends. It never falls inside a surrogate pair."""
    if units <= 0:
        return 0
    if text.isascii():
        return min(len(text), units)
    used = 0
    at = len(text)
    for i, char in enumerate(text):
        used += 2 if char > "\uffff" else 1
        if used > units:
            at = i
            break
    # A pair can also be two separate surrogates, joined into one where it is read.
    if 0 < at < len(text) and "\ud800" <= text[at - 1] <= "\udbff" and "\udc00" <= text[at] <= "\udfff":
        at -= 1
    return at


@dataclass(frozen=True)
class RestorationSection:
    """One part of the note a host puts back after a compaction.

    ``name`` is stable ("environment", "memory", ...): receipts and logs
    name sections by it, never by content. ``priority``: lower claims the
    budget first. ``max_chars`` bounds this section alone (None: only the
    note's own bound). ``cuttable`` lets an oversized section go out cut
    instead of not at all; a cut section is never credited as restored.
    ``on_delivered`` is called once, only when the section went out whole.
    ``replaced_by`` is for a section the host itself puts back on a turn (the
    # Memory snapshot): given what that turn carries for the model, it says
    whether a whole newer copy is there. Then the queue need not send the rest.
    """

    name: str
    text: str
    priority: int = 0
    max_chars: int | None = None  # UTF-16 units, like every budget here
    cuttable: bool = False
    on_delivered: Callable[[], None] | None = None
    replaced_by: Callable[[str], bool] | None = None


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
        if utf16_len(text) <= bound:
            chosen[i], statuses[i] = text, DELIVERED
        elif section.cuttable and bound >= MIN_CUT_CHARS:
            cut = _CUT_NOTE.format(name=section.name, limit=bound)
            chosen[i] = text[: utf16_index(text, bound - utf16_len(cut))].rstrip() + cut
            statuses[i] = CUT
        else:
            statuses[i] = OMITTED
            continue
        budget -= separator + utf16_len(chosen[i])
    return chosen, statuses, entries


@dataclass
class _Sending:
    """One section on its way out."""

    section: RestorationSection
    whole: str  # the section's own text, uncut
    final: str  # the status it ends with once ``left`` is out: delivered or cut
    left: str  # what has not gone out
    order: float = 0  # where the host put it: a part keeps the host's order
    sent: int = 0  # how much of ``left`` as first queued has gone out
    parts: int = 0
    body: int = 0  # how much of ``whole`` that holds (not a cut's closing note)
    credited: bool = False

    @property
    def cursor(self) -> int:
        """How much of ``whole`` has really gone out."""
        return min(self.sent, self.body)


class RestorationQueue:
    """The note for one compaction, sent in parts that each fit one hook answer.

    ``total`` bounds the whole note (:func:`_fit`). :meth:`take` and :meth:`finish`
    stage the next text, and the caller then says whether what carried it was
    written: :meth:`acknowledge` keeps the stage and credits each section
    (``on_delivered``) whose last text it held, :meth:`rollback` undoes it, so the
    same text is offered again. Nothing is credited on the strength of having been
    composed. :meth:`receipts` says where each section stands, and :meth:`finish`
    puts every section not yet whole on the next turn's tail. Parts only shrink
    what is left, so a queue always ends; a later compaction replaces it.
    """

    def __init__(self, compaction_id: str, total: int) -> None:
        self.compaction_id = compaction_id
        self.total = total
        self._used = 0
        self._sending: list[_Sending] = []
        self._omitted: list[_Sending] = []
        self._next = 0
        self._staged: list[tuple[_Sending, str, int, int, str, int]] = []
        self.settled = False  # the leg has said how it ended, nothing is on the way

    def add(self, sections: Sequence[RestorationSection]) -> None:
        """Queue ``sections`` in priority order, within what ``total`` leaves.

        They claim the budget ahead of pending text of a lower priority, which is
        cut or left out for them by its own rules (:func:`_fit`) and goes first on
        the wire after them. What already went out stays counted, so a later
        section never raises the total: what it cannot get waits for the turn's
        tail (:meth:`finish`), as a cut section does.
        """
        self.rollback()
        given = [s for s in sections if s.text.strip()]
        claim = min((s.priority for s in given), default=None)
        yielding = [] if claim is None else [
            item for item in self._sending
            if item.left.strip() and item.section.priority > claim
        ]
        for item in yielding:
            self._release(item)
        offers = [*sections, *(replace(item.section, text=item.left) for item in yielding)]
        chosen, statuses, entries = _fit(offers, self.total - self._used)
        placed = [e for e in entries if e[0] < len(sections)]
        # After what the host gave so far or, with text yielding, just ahead of it.
        start = min((item.order for item in yielding), default=None)
        spaced = [self._next + at for at in range(len(placed))] if start is None else [
            start - 1 + (at + 1) / (len(placed) + 1) for at in range(len(placed))
        ]
        by_order = {e[0]: at for e, at in zip(placed, spaced)}
        for i, section, text in sorted(placed, key=lambda e: (e[1].priority, e[0])):
            if statuses[i] == OMITTED:
                self._omitted.append(_Sending(section, text, OMITTED, "", order=by_order[i]))
                continue
            self._used += utf16_len(chosen[i]) + len(SEPARATOR)
            self._sending.append(_Sending(section, text, statuses[i], chosen[i], order=by_order[i],
                                          body=_body_chars(section.name, chosen[i], statuses[i])))
        if start is None:
            self._next += len(placed)
        for at, item in enumerate(yielding, start=len(sections)):
            self._replan(item, chosen.get(at), statuses.get(at, OMITTED))
        self._sending.sort(key=lambda item: item.section.priority)

    def _release(self, item: _Sending) -> None:
        """Give back what ``item`` reserved and has not sent. What it sent stays
        counted: that text is in the conversation, whatever becomes of the item."""
        if item.left:
            self._used -= utf16_len(item.left) + (len(SEPARATOR) if not item.parts else 0)

    def _replan(self, item: _Sending, text: str | None, status: str) -> None:
        """``item``, which released its reservation, with what it could claim back."""
        if text is None:
            item.left, item.final = "", CUT if item.sent else OMITTED
            return
        self._used += utf16_len(text) + (len(SEPARATOR) if not item.parts else 0)
        item.left = text
        if status == CUT:
            item.body = item.sent + _body_chars(item.section.name, text, CUT)
            item.final = CUT

    def drop(self, name: str) -> None:
        """Forget the section called ``name``, about to be queued again, and give
        back what it reserved."""
        self.rollback()
        for item in self._sending:
            if item.section.name == name:
                self._release(item)
        self._sending = [item for item in self._sending if item.section.name != name]
        self._omitted = [item for item in self._omitted if item.section.name != name]

    @property
    def waiting(self) -> bool:
        """Some section still has text to send in a hook answer."""
        return any(item.left for item in self._sending)

    @property
    def complete(self) -> bool:
        """Every section went out whole, or a whole newer copy went out instead."""
        return all(
            not item.left and item.final in (DELIVERED, REPLACED)
            for item in [*self._sending, *self._omitted]
        )

    def take(self, room: int) -> str:
        """Stage the next part, at most ``room`` UTF-16 units; "" when nothing fits.

        Whole sections go first. A section longer than the room left is split
        at a line break only when it is cuttable (a small one that sets
        behaviour waits for an answer with room for all of it).
        """
        self.rollback()
        parts: list[tuple[int, str]] = []
        left = room
        for item in self._sending:
            if not item.left:
                continue
            gap = len(SEPARATOR) if parts else 0
            lead = f"[{item.section.name}, continued]\n" if item.parts else ""
            size = utf16_len(lead) + utf16_len(item.left)
            if size + gap <= left:
                self._stage(item)
                parts.append((item.order, lead + item.left))
                left -= size + gap
                item.sent, item.left, item.parts = item.sent + len(item.left), "", item.parts + 1
                continue
            tail = _SPLIT_NOTE.format(name=item.section.name)
            fits = left - gap - utf16_len(lead) - utf16_len(tail)
            if not item.section.cuttable or fits < MIN_CUT_CHARS:
                continue
            end = utf16_index(item.left, fits)
            cut = item.left.rfind("\n", MIN_CUT_CHARS // 2, end + 1)
            cut = cut if cut > 0 else end
            piece = item.left[:cut].rstrip()
            self._stage(item)
            parts.append((item.order, lead + piece + tail))
            left -= gap + utf16_len(lead) + utf16_len(piece) + utf16_len(tail)
            rest = item.left[cut:].lstrip("\n")
            item.sent, item.left, item.parts = (
                item.sent + len(item.left) - len(rest), rest, item.parts + 1,
            )
        return SEPARATOR.join(text for _, text in sorted(parts, key=lambda part: part[0]))

    def finish(self, limit: int, turn_text: str = "") -> str:
        """Stage what the next turn's tail carries, within ``limit``: each section
        not yet whole, from where it really stopped, so nothing that went out is
        repeated. ``turn_text`` is what that turn carries for the model already:
        a section whose ``replaced_by`` finds a whole newer copy there needs no more.

        A section that does not fit stays cut or omitted, and the receipt says so.
        """
        self.rollback()
        again: list[_Sending] = []
        for item in [*self._sending, *self._omitted]:
            if item.final in (DELIVERED, REPLACED) and not item.left:
                continue
            check = item.section.replaced_by
            if check is not None and _confirmed(check, turn_text, item.section.name):
                self._stage(item)
                item.left, item.final = "", REPLACED
                continue
            if item.whole[item.cursor:].strip():
                again.append(item)
        leads = [f"[{item.section.name}, continued]\n" if item.cursor else "" for item in again]
        chosen, statuses, _ = _fit(
            [RestorationSection(name=item.section.name,
                                text=lead + item.whole[item.cursor:].lstrip("\n"),
                                priority=item.section.priority,
                                max_chars=item.section.max_chars,
                                cuttable=item.section.cuttable)
             for item, lead in zip(again, leads)], limit,
        )
        for i, item in enumerate(again):
            self._stage(item)
            status = statuses.get(i)
            if status == DELIVERED:
                item.sent = item.body = len(item.whole)
                item.left, item.final = "", DELIVERED
            elif status == CUT:
                body = _body_chars(item.section.name, chosen[i], CUT) - len(leads[i])
                item.sent = item.body = item.cursor + max(0, body)
                item.left, item.final = "", CUT
            else:
                item.left, item.final = "", CUT if item.cursor else OMITTED
        return SEPARATOR.join(chosen[i] for i in range(len(again)) if i in chosen)

    def acknowledge(self) -> None:
        """What was staged reached the CLI: keep it and credit what it finished."""
        staged, self._staged = self._staged, []
        for item, *_ in staged:
            if not item.left and item.final == DELIVERED and not item.credited:
                item.credited = True
                self._credit(item)

    def rollback(self) -> None:
        """What was staged did not reach the CLI: offer the same text again."""
        staged, self._staged = self._staged, []
        for item, left, sent, parts, final, body in reversed(staged):
            item.left, item.sent, item.parts, item.final, item.body = left, sent, parts, final, body

    def receipts(self) -> list[SectionReceipt]:
        """Where each section stands now, in the order they claimed the budget."""
        out = []
        for item in [*self._sending, *self._omitted]:
            status = PENDING if item.left else item.final
            out.append(SectionReceipt(name=item.section.name, status=status,
                                      chars=item.sent, original_chars=len(item.whole)))
        return out

    def _stage(self, item: _Sending) -> None:
        self._staged.append((item, item.left, item.sent, item.parts, item.final, item.body))

    @staticmethod
    def _credit(item: _Sending) -> None:
        callback = item.section.on_delivered
        if callback is None:
            return
        try:
            callback()
        except Exception:  # noqa: BLE001 — a host's bookkeeping never fails a hook
            logger.warning("the %s section's delivery callback failed", item.section.name,
                           exc_info=True)


def _body_chars(name: str, chosen: str, status: str) -> int:
    """How much of ``chosen`` is the section's own text, not a cut's closing note."""
    if status != CUT:
        return len(chosen)
    at = chosen.rfind(f"\n[{name} cut:")
    return at if at >= 0 else len(chosen)


def _confirmed(check: Callable[[str], bool], turn_text: str, name: str) -> bool:
    """Whether ``check`` finds a newer copy in ``turn_text``. Never raises: a host's
    bookkeeping never fails a turn, and an answer it cannot give is no confirmation."""
    try:
        return bool(check(turn_text))
    except Exception:  # noqa: BLE001
        logger.warning("could not check the %s section's replacement", name, exc_info=True)
        return False
