"""The post-compaction restoration note as bounded, prioritized sections (#755)."""
from __future__ import annotations

from fluid.engine.compact_restore import (
    CUT,
    DELIVERED,
    MIN_CUT_CHARS,
    OMITTED,
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
