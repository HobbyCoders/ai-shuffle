"""The post-compaction restoration note as bounded, prioritized sections (#755)."""
from __future__ import annotations

import re

from fluid.engine.compact_restore import (
    CUT,
    DELIVERED,
    MIN_CUT_CHARS,
    OMITTED,
    PENDING,
    REPLACED,
    RestorationQueue,
    RestorationSection,
    as_sections,
    pack_sections,
    utf16_index,
    utf16_len,
)


def _s(name: str, text: str, priority: int = 0, **kw) -> RestorationSection:
    return RestorationSection(name=name, text=text, priority=priority, **kw)


def test_below_the_limit_the_note_is_the_sections_joined_as_before():
    sections = [_s("environment", " env "), _s("memory", "mem", 3), _s("away", "away", 1)]
    note, receipts = pack_sections(sections, 1_000)
    assert note == "env\n\nmem\n\naway"
    assert [(r.name, r.status, r.chars) for r in receipts] == [
        ("environment", DELIVERED, 3), ("memory", DELIVERED, 3), ("away", DELIVERED, 4)]


def test_empty_sections_are_not_listed():
    note, receipts = pack_sections([_s("a", "  "), _s("b", "x")], 100)
    assert note == "x" and [r.name for r in receipts] == ["b"]


def test_priority_decides_who_fits_and_order_is_kept():
    sections = [_s("big", "b" * 90, 2), _s("small", "s" * 20, 0)]
    note, receipts = pack_sections(sections, 100)
    # "small" claims first; "big" then no longer fits whole and is not cuttable.
    assert note == "s" * 20
    assert {r.name: r.status for r in receipts} == {"big": OMITTED, "small": DELIVERED}


def test_a_cuttable_section_is_cut_to_the_room_left_and_marked():
    sections = [_s("environment", "e" * 10, 0), _s("memory", "m" * 5_000, 1, cuttable=True)]
    note, receipts = pack_sections(sections, 3_000)
    assert len(note) == 3_000
    assert note.endswith("[memory cut: it was longer than 2988 characters]")
    memory = receipts[1]
    assert (memory.status, memory.chars, memory.original_chars) == (CUT, 2_988, 5_000)


def test_a_sliver_is_never_sent():
    sections = [_s("environment", "e" * 2_500, 0), _s("memory", "m" * 5_000, 1, cuttable=True)]
    _, receipts = pack_sections(sections, 2_500 + MIN_CUT_CHARS)
    assert receipts[1].status == OMITTED


def test_a_section_bound_applies_on_its_own():
    note, receipts = pack_sections([_s("away", "a" * 50, max_chars=40)], 1_000)
    assert note == "" and receipts[0].status == OMITTED
    cut, receipt = pack_sections(
        [_s("memory", "m" * 5_000, max_chars=2_000, cuttable=True)], 24_000)
    assert len(cut) == 2_000 and receipt[0].status == CUT


def test_a_later_small_section_still_fits_after_a_large_one_is_left_out():
    sections = [_s("big", "b" * 500, 0), _s("small", "s" * 10, 1)]
    note, receipts = pack_sections(sections, 100)
    assert note == "s" * 10
    assert [r.status for r in receipts] == [OMITTED, DELIVERED]


def test_the_separators_count_against_the_limit():
    sections = [_s("a", "a" * 50), _s("b", "b" * 50)]
    assert pack_sections(sections, 101)[1][1].status == OMITTED
    note, receipts = pack_sections(sections, 102)
    assert len(note) == 102 and receipts[1].status == DELIVERED


def test_a_plain_string_is_one_cuttable_note_section():
    [section] = as_sections("hello")
    assert (section.name, section.cuttable) == ("note", True)
    assert as_sections(None) == [] and as_sections(42) == []
    assert as_sections([_s("a", "x"), "junk"]) == [_s("a", "x")]


# ─── the note in parts (#755): each part reaches the model, and credit waits ──


def _queue(*sections: RestorationSection, total: int = 24_000) -> RestorationQueue:
    queue = RestorationQueue("cmp_test", total)
    queue.add(sections)
    return queue


def _status(queue: RestorationQueue) -> dict[str, str]:
    return {r.name: r.status for r in queue.receipts()}


def test_a_section_is_credited_only_when_its_last_part_is_out():
    credited: list[str] = []
    long = "\n".join(f"memory line {i:05d}" for i in range(1_000))
    queue = _queue(
        _s("environment", "today", 0, on_delivered=lambda: credited.append("environment")),
        _s("memory", long, 3, cuttable=True, on_delivered=lambda: credited.append("memory")),
    )
    first = queue.take(9_500)
    assert len(first) <= 9_500 and first.startswith("today\n\nmemory line 00000")
    assert first.endswith("[memory continues in a later note]")
    assert credited == []  # composed is not delivered: nothing counts before the write
    queue.acknowledge()
    assert credited == ["environment"]
    assert _status(queue) == {"environment": DELIVERED, "memory": PENDING}
    assert queue.waiting and not queue.complete
    parts = [first]
    while queue.waiting:
        part = queue.take(9_500)
        assert part and len(part) <= 9_500
        queue.acknowledge()
        parts.append(part)
    assert credited == ["environment", "memory"] and queue.complete
    assert _status(queue) == {"environment": DELIVERED, "memory": DELIVERED}
    # Every line went out exactly once, in order.
    seen = re.findall(r"memory line \d{5}", "\n".join(parts))
    assert seen == long.splitlines()


def test_parts_keep_the_hosts_order_and_a_small_section_waits_for_room():
    queue = _queue(_s("first", "f" * 3_000, 2), _s("second", "s" * 3_000, 0), _s("third", "t" * 100, 1))
    # Priority picks who goes, the host's order is how they read.
    part = queue.take(3_200)
    assert part.startswith("sss") and part.endswith("ttt")
    queue.acknowledge()
    # A small section is never split: it stays whole for the next answer.
    assert _status(queue)["first"] == PENDING
    assert queue.take(100) == ""
    assert queue.take(3_000) == "f" * 3_000
    queue.acknowledge()
    assert queue.complete


def test_the_queue_always_ends_and_never_exceeds_its_total():
    queue = _queue(_s("memory", "m" * 100_000, 1, cuttable=True), total=24_000)
    parts = []
    for _ in range(10):
        if not queue.waiting:
            break
        parts.append(queue.take(9_500))
        queue.acknowledge()
    assert not queue.waiting and len(parts) == 3
    assert sum(len(p) for p in parts) <= 24_000 + 200
    assert parts[-1].endswith("characters]")  # says it was cut
    assert _status(queue) == {"memory": CUT} and not queue.complete


def test_the_tail_sends_what_is_missing_without_repeating():
    credited: list[str] = []
    text = "\n".join(f"line {i:05d}" for i in range(2_000))
    queue = _queue(_s("record", text, 0, cuttable=True, on_delivered=lambda: credited.append("record")))
    first = queue.take(9_500).removesuffix("\n[record continues in a later note]")
    queue.acknowledge()
    rest = queue.finish(24_000).removeprefix("[record, continued]\n")
    assert credited == []
    queue.acknowledge()
    assert (first + "\n" + rest).split("\n") == text.split("\n")
    assert credited == ["record"] and queue.complete


def test_the_tail_keeps_a_sections_bound_and_skips_what_the_host_covers():
    queue = _queue(
        _s("away", "a" * 50, 0, max_chars=40),
        _s("memory", "m" * 30_000, 1, cuttable=True, replaced_by=lambda turn: "FULL" in turn),
        total=24_000,
    )
    queue.take(9_500)
    queue.acknowledge()
    assert queue.finish(24_000, "the turn's FULL memory") == ""
    assert _status(queue) == {"away": OMITTED, "memory": REPLACED} and not queue.waiting


def test_what_the_tail_cannot_fit_is_not_credited():
    credited: list[str] = []
    queue = _queue(_s("note", "n" * 100_000, 0, cuttable=True,
                      on_delivered=lambda: credited.append("note")))
    queue.take(9_500)
    queue.acknowledge()
    rest = queue.finish(24_000)
    queue.acknowledge()
    assert len(rest) <= 24_000 and credited == []
    assert _status(queue) == {"note": CUT} and not queue.complete


# ─── nothing counts until it is written (review of #1050, finding 3) ──────────


def test_a_part_that_was_never_written_is_offered_again_whole():
    credited: list[str] = []
    queue = _queue(_s("rules", "r" * 2_000, 0, on_delivered=lambda: credited.append("rules")),
                   _s("note", "n" * 20_000, 1, cuttable=True))
    first = queue.take(9_500)
    queue.rollback()
    assert credited == [] and not queue.complete
    assert _status(queue) == {"rules": PENDING, "note": PENDING}
    assert queue.take(9_500) == first  # the same bytes, so no part is lost or doubled
    queue.acknowledge()
    assert credited == ["rules"]


def test_a_new_take_rolls_back_what_was_not_acknowledged():
    queue = _queue(_s("note", "\n".join(f"line {i:05d}" for i in range(3_000)), 0, cuttable=True))
    first = queue.take(9_500)
    assert queue.take(9_500) == first


def test_a_tail_that_was_not_written_can_be_composed_again():
    credited: list[str] = []
    queue = _queue(_s("rules", "r" * 2_000, 0, on_delivered=lambda: credited.append("rules")))
    text = queue.finish(24_000)
    queue.rollback()
    assert credited == [] and queue.waiting
    assert queue.finish(24_000) == text
    queue.acknowledge()
    assert credited == ["rules"] and queue.complete


# ─── the tail credits only what the turn confirms (finding 4) ─────────────────


def test_a_partial_section_is_not_replaced_by_a_turn_that_does_not_hold_it_all():
    """A turn with no full snapshot leaves the rest of the memory owed."""
    queue = _queue(_s("memory", "m" * 17_000, 0, cuttable=True,
                      replaced_by=lambda turn: "FULL SNAPSHOT" in turn))
    queue.take(9_500)
    queue.acknowledge()
    rest = queue.finish(24_000, "only a delta here")
    assert rest.startswith("[memory, continued]\n") and "m" * 100 in rest
    queue.acknowledge()
    assert _status(queue) == {"memory": DELIVERED} and queue.complete


def test_the_tail_goes_on_from_the_true_cursor_of_a_cut_section():
    """A cut section's prefix never goes out twice: the marker a part ends with
    is not part of the section, so the cursor is counted in source characters."""
    record = "RECORD_START\n" + "\n".join(f"record line {i:05d}" for i in range(4_000))
    queue = _queue(_s("working_record", record, 0, cuttable=True))
    parts = [queue.take(9_500)]
    queue.acknowledge()
    parts.append(queue.finish(9_500))
    queue.acknowledge()
    assert _status(queue) == {"working_record": CUT}
    joined = "\n".join(parts)
    assert joined.count("RECORD_START") == 1
    lines = re.findall(r"record line \d{5}", joined)
    assert lines == sorted(set(lines))  # nothing twice, nothing out of order


def test_drop_forgets_a_section_so_it_can_be_queued_afresh():
    queue = _queue(_s("rules", "old", 0))
    queue.take(1_000)
    queue.acknowledge()
    queue.drop("rules")
    queue.add([_s("rules", "new", 0)])
    assert queue.waiting and queue.take(1_000) == "new"


def test_a_section_cut_to_its_bound_does_not_start_over_in_the_tail():
    """Every bounded part of an oversized record went out, then the turn's tail:
    the next lines, or nothing, never the start of the record again."""
    record = "RECORD_START\n" + "\n".join(f"record line {i:05d}" for i in range(8_000))
    queue = _queue(_s("working_record", record, 0, cuttable=True))
    parts = []
    while queue.waiting:
        part = queue.take(9_500)
        if not part:
            break
        queue.acknowledge()
        parts.append(part)
    assert len(parts) >= 2
    parts.append(queue.finish(24_000))
    queue.acknowledge()
    joined = "\n".join(parts)
    assert joined.count("RECORD_START") == 1
    lines = re.findall(r"record line \d{5}", joined)
    assert lines == sorted(set(lines))
    assert _status(queue) == {"working_record": CUT}


def _units(text: str) -> int:
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


def test_every_budget_is_in_the_units_the_cli_counts() -> None:
    """Claude Code checks JavaScript's length, where an emoji is two units: 6,008
    Python characters of them are 12,008 to it, so they do not fit one answer."""
    text = "\U0001F600" * 6_000 + "TAIL-END"
    queue = RestorationQueue("cmp_1", 24_000)
    queue.add([RestorationSection(name="note", text=text, cuttable=True)])
    part = queue.take(9_500)
    queue.acknowledge()
    assert _units(part) <= 9_500 and "TAIL-END" not in part
    assert not queue.complete and _status(queue) == {"note": PENDING}
    assert part.encode("utf-8")  # no half of a pair: that would not encode
    rest = queue.take(9_500)
    queue.acknowledge()
    assert _units(rest) <= 9_500 and rest.endswith("TAIL-END") and queue.complete
    assert (part + rest).count("\U0001F600") == 6_000


def test_a_note_is_cut_to_its_bound_in_those_units() -> None:
    note, [receipt] = pack_sections(
        [RestorationSection(name="note", text="\U0001F600" * 3_000, cuttable=True)], 2_000,
    )
    assert receipt.status == CUT and _units(note) <= 2_000
    note, [receipt] = pack_sections(
        [RestorationSection(name="env", text="\U0001F600" * 1_500)], 2_000,
    )
    assert receipt.status == OMITTED and note == ""


def test_a_budget_never_lands_inside_a_surrogate_pair() -> None:
    assert [utf16_index("\U0001F600\U0001F600x", units) for units in range(6)] == [0, 0, 1, 1, 2, 3]
    assert utf16_len("\U0001F600éx") == 4 and utf16_len("") == 0
    # Two separate surrogates become one pair where the CLI reads them.
    assert utf16_index("a😀b", 2) == 1 and utf16_len("a😀b") == 4
    assert utf16_index("\ud83dx", 1) == 1  # one on its own is one unit, and can end a part


def test_a_replaced_section_gives_back_what_it_had_not_sent() -> None:
    """The budget is full and the section to be replaced has only partly gone out:
    the new copy takes its unsent room and memory's, ahead of memory, and the total
    (what went out plus what is pending) never rises."""
    first = "i" * 14_000
    memory = "m" * 14_000
    queue = RestorationQueue("cmp_1", 24_000)
    queue.add([
        RestorationSection(name="instructions", text=first, cuttable=True),
        RestorationSection(name="memory", text=memory, priority=3, cuttable=True),
    ])
    sent = queue.take(9_500)
    queue.acknowledge()
    assert "m" not in sent.split("\n")[0]
    again = "BRIEF " + "b" * 12_000
    queue.drop("instructions")
    queue.add([RestorationSection(name="instructions", text=again, cuttable=True)])
    assert _status(queue)["instructions"] == PENDING
    parts = [sent]
    while queue.waiting:
        part = queue.take(9_500)
        assert part, "a pending part must fit an empty answer"
        queue.acknowledge()
        parts.append(part)
    assert again in "".join(parts[1:]).replace("\n[instructions continues in a later note]", "")\
        .replace("[instructions, continued]\n", "")
    assert sum(_units(part) for part in parts) <= 24_000 + 200  # part markers are not budgeted
    assert _status(queue)["instructions"] == DELIVERED
    # Memory got what was left, marked as cut: it is not credited, and goes on the tail.
    assert _status(queue)["memory"] in (CUT, OMITTED)
    assert "m" in queue.finish(24_000)


def test_a_section_put_ahead_comes_first_on_the_wire_and_nothing_is_kept_for_it_twice() -> None:
    queue = RestorationQueue("cmp_1", 2_000)
    queue.add([RestorationSection(name="memory", text="MEMORY", priority=3)])
    queue.add([RestorationSection(name="instructions", text="BRIEF", priority=0)])
    assert queue.take(1_000) == "BRIEF\n\nMEMORY"
    queue.rollback()
    for _ in range(50):  # replacing it over and over never eats the budget
        queue.drop("instructions")
        queue.add([RestorationSection(name="instructions", text="B" * 1_500, cuttable=True)])
    assert _status(queue)["instructions"] == PENDING
    assert _status(queue)["memory"] == PENDING
    assert queue.take(2_000) == "B" * 1_500 + "\n\nMEMORY"


def test_a_partly_sent_lower_section_keeps_its_cursor_when_it_yields() -> None:
    whole = _INDENTED.strip()
    queue = RestorationQueue("cmp_1", 20_000)
    queue.add([RestorationSection(name="memory", text=_INDENTED, priority=4, cuttable=True)])
    pieces = _memory_pieces(queue.take(9_000))
    queue.acknowledge()
    queue.add([RestorationSection(name="instructions", text="i" * 15_000, cuttable=True)])
    # The instructions take all the room left, so the rest of memory waits for the tail.
    assert _status(queue)["memory"] == CUT
    tail = queue.finish(40_000)
    assert "i" * 100 in tail
    # The record resumes where it really stopped: byte for byte, not just line numbers.
    pieces += _memory_pieces(tail)
    _delivered(whole, pieces)
    lines = re.findall(r"record line \d{5}", "".join(pieces))
    assert lines == [f"record line {i:05d}" for i in range(800)]


# Indented lines: a split leaves the rest of the text starting with spaces that belong to it.
_INDENTED = "".join(f"        record line {i:05d}\n" for i in range(800))


def _delivered(whole: str, pieces: list[str]) -> None:
    """``pieces`` are ``whole`` in order, byte for byte: nothing repeated or skipped
    and only blank text between them (what a split or a cut trims at its edge)."""
    at = 0
    for piece in pieces:
        found = whole.find(piece, at)
        assert found >= 0, f"not the source's own text from {at}: {piece[:40]!r}"
        assert not whole[at:found].strip(), f"skipped text at {at}: {whole[at:found][:40]!r}"
        at = found + len(piece)
    assert not whole[at:].strip(), f"never sent, from {at}: {whole[at:at + 40]!r}"


def _memory_pieces(text: str) -> list[str]:
    """The memory text in one hook answer or tail, without what the queue wraps it in."""
    pieces = []
    for part in text.split("\n\n"):
        if part.startswith("i"):
            continue
        part = part.removeprefix("[memory, continued]\n")
        part = re.sub(r"\n\[memory (continues in a later note|cut: [^\]]*)\]$", "", part)
        pieces.append(part)
    return pieces


def test_a_section_that_yields_keeps_its_indentation_through_to_delivery() -> None:
    whole = _INDENTED.strip()
    credited: list[str] = []
    queue = RestorationQueue("cmp_1", 30_000)
    queue.add([RestorationSection(name="memory", text=_INDENTED, priority=3, cuttable=True,
                                  on_delivered=lambda: credited.append("memory"))])
    pieces = _memory_pieces(queue.take(9_000))
    queue.acknowledge()
    stopped = next(r.chars for r in queue.receipts() if r.name == "memory")
    assert whole[stopped:].startswith("        record line ")
    queue.add([RestorationSection(name="instructions", text="i" * 2_000)])
    while queue.waiting:
        part = queue.take(9_000)
        assert part, "a pending part must fit an empty answer"
        queue.acknowledge()
        pieces += _memory_pieces(part)
    memory = next(r for r in queue.receipts() if r.name == "memory")
    assert (memory.status, memory.chars, memory.original_chars) == (DELIVERED, len(whole), len(whole))
    assert pieces[1].startswith("        record line ")  # the suffix went out as it was queued
    _delivered(whole, pieces)
    assert credited == ["memory"]
    assert queue.complete


def test_a_section_cut_when_it_yields_resumes_on_the_tail_at_its_own_source_text() -> None:
    whole = _INDENTED.strip()
    queue = RestorationQueue("cmp_1", 22_000)
    queue.add([RestorationSection(name="memory", text=_INDENTED, priority=3, cuttable=True)])
    pieces = _memory_pieces(queue.take(9_000))
    queue.acknowledge()
    queue.add([RestorationSection(name="instructions", text="i" * 8_000)])
    while queue.waiting:
        part = queue.take(9_000)
        assert part, "a pending part must fit an empty answer"
        queue.acknowledge()
        pieces += _memory_pieces(part)
    memory = next(r for r in queue.receipts() if r.name == "memory")
    assert memory.status == CUT
    tail = queue.finish(40_000)
    queue.acknowledge()
    pieces += _memory_pieces(tail)
    _delivered(whole, pieces)
    assert next(r for r in queue.receipts() if r.name == "memory").chars == len(whole)


def test_a_section_cut_on_the_tail_resumes_from_there_on_the_next_tail() -> None:
    whole = _INDENTED.strip()
    queue = RestorationQueue("cmp_1", 5_000)
    queue.add([RestorationSection(name="memory", text=_INDENTED, priority=3, cuttable=True)])
    pieces: list[str] = []
    for _ in range(12):
        tail = queue.finish(4_000)
        queue.acknowledge()
        if not tail:
            break
        pieces += _memory_pieces(tail)
    _delivered(whole, pieces)
