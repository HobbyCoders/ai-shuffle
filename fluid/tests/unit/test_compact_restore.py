"""The post-compaction restoration note as bounded, prioritized sections (#755)."""
from __future__ import annotations

import re

from fluid.engine.compact_restore import (
    CUT,
    DELIVERED,
    MIN_CUT_CHARS,
    OMITTED,
    PENDING,
    RestorationQueue,
    RestorationSection,
    as_sections,
    pack_sections,
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
    assert credited == ["environment"]
    assert _status(queue) == {"environment": DELIVERED, "memory": PENDING}
    assert queue.waiting and not queue.complete
    parts = [first]
    while queue.waiting:
        part = queue.take(9_500)
        assert part and len(part) <= 9_500
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
    # A small section is never split: it stays whole for the next answer.
    assert _status(queue)["first"] == PENDING
    assert queue.take(100) == ""
    assert queue.take(3_000) == "f" * 3_000
    assert queue.complete


def test_the_queue_always_ends_and_never_exceeds_its_total():
    queue = _queue(_s("memory", "m" * 100_000, 1, cuttable=True), total=24_000)
    parts = []
    for _ in range(10):
        if not queue.waiting:
            break
        parts.append(queue.take(9_500))
    assert not queue.waiting and len(parts) == 3
    assert sum(len(p) for p in parts) <= 24_000 + 200
    assert parts[-1].endswith("characters]")  # says it was cut
    assert _status(queue) == {"memory": CUT} and not queue.complete


def test_the_tail_sends_what_is_missing_without_repeating():
    credited: list[str] = []
    text = "\n".join(f"line {i:05d}" for i in range(2_000))
    queue = _queue(_s("record", text, 0, cuttable=True, on_delivered=lambda: credited.append("record")))
    first = queue.take(9_500).removesuffix("\n[record continues in a later note]")
    rest = queue.finish(24_000)
    assert (first + "\n" + rest).split("\n") == text.split("\n")
    assert credited == ["record"] and queue.complete


def test_the_tail_keeps_a_sections_bound_and_skips_what_the_host_covers():
    queue = _queue(
        _s("away", "a" * 50, 0, max_chars=40),
        _s("memory", "m" * 30_000, 1, cuttable=True, next_turn_covers=True),
        total=24_000,
    )
    queue.take(9_500)
    assert queue.finish(24_000) == ""
    assert _status(queue)["away"] == OMITTED and not queue.waiting


def test_what_the_tail_cannot_fit_is_not_credited():
    credited: list[str] = []
    queue = _queue(_s("note", "n" * 100_000, 0, cuttable=True,
                      on_delivered=lambda: credited.append("note")))
    queue.take(9_500)
    rest = queue.finish(24_000)
    assert len(rest) <= 24_000 and credited == []
    assert _status(queue) == {"note": CUT} and not queue.complete
