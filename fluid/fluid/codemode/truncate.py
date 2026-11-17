"""Result-size policy for code-mode.

A tool result whose serialized text exceeds ``DEFAULT_LIMIT`` bytes should not
be poured into the model's context in full. The kernel keeps the full payload
alive in its persistent namespace behind a ``ResultRef`` handle (see
``vfs.ResultRef`` — a ``str`` subclass with a truncated ``repr``), so the model
slices/parses it in a later ``run_python`` with no further host round-trip and
no token blowup.

The second policy here is the *cell stdout* budget: :func:`fair_share_stdout`
divides a fixed cap across the cell's individual prints instead of keeping the
head of the concatenated stream, so a cell that prints a summary last still
gets to show it.

This module is the tiny, dependency-free policy shared by host and kernel: it
decides *when* a result is large and wraps it, taking the ``ResultRef`` class by
injection to avoid an import cycle (``vfs`` is a top-level module inside the
kernel but ``fluid.codemode.vfs`` on the host).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

DEFAULT_LIMIT = 8192  # bytes; results larger than this ride behind a ResultRef


def exceeds(text: str, limit: int = DEFAULT_LIMIT) -> bool:
    """True when ``text``'s UTF-8 size is over ``limit``."""
    return len(text.encode("utf-8", "replace")) > limit


def to_result_ref(
    text: str, ref_cls: Callable[[str], Any], *, limit: int = DEFAULT_LIMIT
) -> Any:
    """Return ``ref_cls(text)`` when ``text`` is large, else ``text`` unchanged.

    ``ref_cls`` is ``vfs.ResultRef``, injected so this module needs no import of
    ``vfs`` (whose import path differs between host and kernel).
    """
    return ref_cls(text) if exceeds(text, limit) else text


class ReadPointer(str):
    """A pointer to a payload the kernel already holds — not the payload.

    A repeat read returns guidance ("it is in ``_refs[7]``") rather than the
    bytes. As a plain ``str`` that guidance is indistinguishable from content
    to code: ``card = Read(...)`` followed by ``card.splitlines()`` would
    quietly iterate the *notice*.

    Subclassing ``str`` keeps everything that made the pointer useful —
    ``print(pointer)`` shows the guidance, f-strings and ``==`` behave, and the
    kernel's ``repr``-based value display still carries the text — while the
    two operations that consume it *as content* raise instead of lying. That is
    the least surprising of the options: refusing every operation would hide
    the guidance the model needs to read, and a silent str keeps the bug.
    """

    #: Registry id of the payload this points at, and the payload's size.
    key: int | None = None
    size: int = 0

    def __new__(cls, text: str, key: int | None = None, size: int = 0):
        obj = super().__new__(cls, text)
        obj.key = key
        obj.size = size
        return obj

    def __repr__(self) -> str:
        # The guidance rides inside the repr: a bare tool call in the last
        # expression of a cell reaches the model through repr(), and the
        # pointer is useless if that is where the text disappears.
        return f"<ReadPointer #{self.key}: {str.__str__(self)}>"

    def _not_content(self, op: str):
        raise TypeError(
            f"{op} on this value would read the pointer text, not the file — "
            f"the content is in the kernel as _refs[{self.key}] "
            f"({self.size:,} bytes). Bind it first: text = _refs[{self.key}]; "
            f"then {op} on text."
        )

    def __iter__(self):
        self._not_content("iteration")

    def splitlines(self, *args, **kwargs):  # noqa: D102 - see _not_content
        self._not_content(".splitlines()")

    def split(self, *args, **kwargs):  # noqa: D102 - see _not_content
        self._not_content(".split()")


def repeat_read_pointer(key: int, size: int) -> ReadPointer:
    """The result a repeat read of an unchanged file gets instead of the bytes.

    A passive "unchanged since your earlier read" note does not stop a
    re-read. This removes the choice: the payload is already alive in the kernel
    under a registry id, so the second read returns the id and the size and
    nothing else. Slicing ``_refs[key]`` costs no host round-trip at all.

    Like :func:`windowed_read_pointer` it leads with the expression to type,
    because a pointer that only *describes* the situation gets read as prose
    and answered with another read.
    """
    return ReadPointer(
        f"unchanged since your earlier read — this value is a pointer, not the "
        f"file. Print only the slice you need: text = _refs[{key}]; "
        f"print(text[:2000]). _refs[{key}] holds all {size:,} bytes; slice it "
        f"instead of re-reading.",
        key=key,
        size=size,
    )


def windowed_read_pointer(
    key: int, size: int, start: int, stop: int | None, *, offset: int = 0
) -> ReadPointer:
    """The result a re-read of an unchanged file at a *new* window gets.

    Keying the pointer only on the exact ``(path, offset, limit)`` window would
    let the same file be re-read at every new offset while the whole file sits
    in the kernel. If the ref contains
    the window, that window is a slice of it — so the answer is the slicing
    expression, spelled out, rather than the lines.

    ``start``/``stop`` are 0-based line bounds within the FILE; ``offset`` is
    the file line the held ref itself starts at (0 when it holds the file
    whole), so a ref built from an earlier windowed read still gets a slice
    expression whose indices are right for *it*.
    """
    lo = start - offset
    hi = "" if stop is None else str(stop - offset)
    span = f"[{lo}:{hi}]"
    last = "end" if stop is None else f"{stop}"
    holds = (
        "the whole file is already in the kernel"
        if not offset
        else "an earlier read covering these lines is already in the kernel"
    )
    return ReadPointer(
        f"unchanged since your earlier read — {holds}: _refs[{key}] "
        f"({size:,} bytes). Lines {start + 1}-{last} are "
        f"_refs[{key}].splitlines(){span} — slice it instead of re-reading.",
        key=key,
        size=size,
    )


# ─── cell stdout budget ──────────────────────────────────────────────────────

DEFAULT_STDOUT_CAP = 8000  # chars of cell stdout that reach the model
_DROP_MARKER = "…[-{n} chars]"
# A truncated print keeps head AND tail (40/60) once its share is at least
# this many chars; below it, head only, as a split would leave two stubs.
_SPLIT_MIN = 400
# Chars of a truncated cell's raw stdout the kernel hands the host for the
# spill file (head and tail kept past this, a runaway print loop bounded).
FULL_STDOUT_MAX = 2 * 1024 * 1024


# A failed cell's error text. An exception carries whatever the cell put in
# it (``raise ValueError(diff)``), so the kernel bounds it where the exception
# object is still in hand: each message, note and SyntaxError source line is
# clipped to ERROR_PART_CAP, so one huge part cannot push out the frames or
# the other chain links and group children; the whole rendered error is then
# held to ERROR_CAP as the backstop. The unclipped text rides to the host
# separately (``error_full``) for the spill file and never reaches the model.
ERROR_PART_CAP = 2_000
ERROR_CAP = 8_000
ERROR_SUMMARY_CAP = 300


def clip_part(text: str, limit: int = ERROR_PART_CAP) -> str:
    """``text`` whole when it fits; else its head and tail around a count,
    ``limit`` chars in all."""
    if len(text) <= limit:
        return text
    room = max(0, limit - len(f" …[-{len(text):,} chars]… "))
    head = room * 3 // 4
    tail = room - head
    marker = f" …[-{len(text) - head - tail:,} chars]… "
    return f"{text[:head]}{marker}{text[len(text) - tail:]}"


def bound_error(text: str, summary: str, limit: int = ERROR_CAP) -> str:
    """The backstop: ``text`` whole when it fits; else a first line naming the
    exception that ended the cell (``summary``, built from the exception
    object, never parsed from ``text``), then head and a longer tail."""
    if len(text) <= limit:
        return text
    head = limit // 4
    tail = limit - head
    lead = f"[the cell ended with: {summary}]\n" if summary else ""
    return (
        f"{lead}{text[:head]}\n…[-{len(text) - head - tail:,} chars of the error]…\n"
        f"{text[len(text) - tail:]}"
    )


def bounded_full_stdout(text: str, limit: int = FULL_STDOUT_MAX, *, what: str = "cell stdout") -> str:
    """``text`` held to ``limit`` chars for the spill file, head and tail kept.

    Only a cut result is longer than ``limit`` (the marker pushes it over).
    """
    if len(text) <= limit:
        return text
    half = limit // 2
    dropped = len(text) - 2 * half
    return f"{text[:half]}\n…[{dropped:,} chars of {what} not kept]…\n{text[-half:]}"


_NAMED_DROPS = 2  # how many of the heaviest drops the notice names
# How many kernel handles the notice names. Refs are created for every result
# of a truncated cell (see ``SPILL_MIN``), so the list has to be able to name
# them all; the ceiling only exists so a cell with fifty calls cannot turn the
# notice into a page.
_NAMED_REFS = 12
# A result the cell did not print in full is worth a handle only if recovering
# it saves a round-trip. Below this, re-running the tool is cheaper than
# reading a line about it.
SPILL_MIN = 512  # bytes


def _drop_marker(dropped: int) -> str:
    return _DROP_MARKER.format(n=dropped)


def _handle_list(refs) -> tuple[str, str]:
    """``(named handles, "and N more" tail)`` for a cell's refs.

    Accepts ``(key, size)`` or ``(key, size, tool_name)``; the tool name is
    included when known, so a cell that made several calls says which result
    each handle holds.
    """
    seen = list(dict.fromkeys(tuple(entry) for entry in refs))
    named = seen[:_NAMED_REFS]
    parts = []
    for entry in named:
        key, size = entry[0], entry[1]
        tool = f" {entry[2]}" if len(entry) > 2 and entry[2] else ""
        parts.append(f"_refs[{key}]{tool} ({size:,} bytes)")
    rest = len(seen) - len(named)
    return ", ".join(parts), (f" (and {rest} more in _refs)" if rest else "")


def _recovery_clause(refs: Sequence[tuple]) -> str:
    """How to get the dropped chars back — a handle, not an instruction to redo.

    A notice that ends at "heaviest drops" reads as "that output is gone" and
    buys a re-run of the tools that produced it. When the payload came from
    tool calls whose full results are alive in the kernel, the recovery is
    free and it should be spelled out.
    """
    handles, more = _handle_list(refs)
    if not handles:
        return (
            " Nothing in this cell is held as a ResultRef, so print less per "
            "cell — slice or summarize — rather than re-running the tools."
        )
    return (
        f" Each tool result this cell produced is held whole in the kernel: "
        f"{handles}{more} — slice it instead of re-running the tools or "
        "re-printing."
    )


def refs_footer(refs) -> str:
    """One line naming the refs a cell's host calls produced, or ``""``.

    Every cell that created refs says so, in one line, whether or not anything
    was cut, so a cell that stayed under the cap still leaves its handles
    discoverable.
    """
    handles, more = _handle_list(refs)
    if not handles:
        return ""
    return (
        f"[refs created this cell: {handles}{more} — the full results are in "
        "the kernel; slice them instead of re-running the tools.]"
    )


def _bookend_stdout(
    segments: list[str], cap: int, total: int, refs: Sequence[tuple], shares: dict[int, int]
) -> str:
    """Shares too small for per-print markers: keep two readable windows instead."""
    def marker(prints: int, chars: int) -> str:
        return f"…[{prints} prints, {chars} chars cut]…\n"

    marker_size = len(marker(len(segments), total))
    room = max(0, cap - marker_size)
    # Preserve short summaries in input order. Each isolated group can split
    # one gap into two, so charge for that extra marker too. Leave at least
    # half the room for the first and last prints, even with many summaries.
    short_budget = room // 2
    shorts: list[tuple[int, int, str]] = []
    offset = 0
    for i, segment in enumerate(segments):
        if segment and len(segment) == shares[i]:
            adjacent = shorts and shorts[-1][0] == i - 1
            cost = len(segment) + (0 if adjacent else marker_size)
            if cost <= short_budget:
                shorts.append((i, offset, segment))
                short_budget -= cost
                room -= cost
        offset += len(segment)

    def window(indices: range, budget: int, *, tail: bool) -> tuple[str, int]:
        parts = []
        whole = 0
        for i in indices:
            part = segments[i]
            if len(part) > budget:
                # Prefer complete prints unless even the edge print won't fit.
                if not whole and budget:
                    parts.append(part[-budget:] if tail else part[:budget])
                break
            parts.append(part)
            whole += 1
            budget -= len(part)
        return "".join(reversed(parts) if tail else parts), whole

    n = len(segments)
    head, first = window(range(n), room // 2, tail=False)
    tail, last = window(range(n - 1, -1, -1), room - room // 2, tail=True)
    kept = len(head) + len(tail)
    parts = [head]
    cursor, pos = first, len(head)
    for i, offset, part in shorts:
        if offset < len(head) or offset + len(part) > total - len(tail):
            continue  # already in a bookend
        if pos < offset:
            parts.append(marker(i - cursor, offset - pos))
        parts.append(part)
        kept += len(part)
        cursor, pos = i + 1, offset + len(part)
    if pos < total - len(tail):
        cut = marker(n - last - cursor, total - len(tail) - pos)
        parts.append(cut if len(cut) <= cap else "…"[:cap])
    parts.append(tail)
    payload = "".join(parts)
    return (
        payload.rstrip("\n") + f"\n…[stdout truncated: kept {kept} of {total} chars across "
        f"{n} prints — one cell's stdout budget is {cap:,} chars.{_recovery_clause(refs)}]"
    )


def fair_share_stdout(
    segments: list[str],
    cap: int = DEFAULT_STDOUT_CAP,
    *,
    refs: Sequence[tuple] = (),
) -> str:
    """Fit a cell's prints into ``cap`` chars, fairly, and name the damage.

    A tail-drop (``out[:cap]``) keeps the *first* ``cap`` chars and silently
    loses everything after — which is exactly where a model puts the thing it
    was working towards: the summary, the file list, the verdict. This instead
    max-min fair-shares the budget across ``segments`` (one segment ≈ one
    ``print``): every segment shorter than its share survives whole and frees
    its surplus for the greedy ones. If shares are too thin even for markers,
    keep bookends and fitting short prints with counted gaps. Otherwise each cut
    segment carries an inline ``…[-N chars]`` marker. A closing notice names
    the stdout budget, retained text size, and — when the cell's tool
    results are alive in the kernel — the ``_refs`` handles that hold them
    whole, so the recovery is a slice rather than a re-run.

    ``refs`` is this cell's ``(registry id, size[, tool name])`` tuples, in the
    order the calls happened.

    Markers are paid for out of their own segment's share, so the returned
    payload never exceeds ``cap`` (the closing notice is metadata on top).
    """
    total = sum(len(s) for s in segments)
    if total <= cap or not segments:
        return "".join(segments)
    cap = max(cap, 0)

    # Max-min fair share: hand out the budget shortest-first, redistributing
    # every unused char to the segments still waiting.
    keep: dict[int, int] = {}
    remaining = cap
    order = sorted(range(len(segments)), key=lambda i: len(segments[i]))
    for pos, index in enumerate(order):
        share = remaining // (len(order) - pos)
        keep[index] = min(len(segments[index]), share)
        remaining -= keep[index]

    parts: list[str] = []
    drops: list[tuple[int, int]] = []  # (1-based print number, chars dropped)
    kept_chars = 0
    for index, segment in enumerate(segments):
        budget = keep[index]
        if budget >= len(segment):
            parts.append(segment)
            kept_chars += len(segment)
            continue
        # Reserve the marker (and the newline that closes the line) out of
        # this segment's own share, so the payload stays inside the cap even
        # when every print is truncated.
        dropped = len(segment) - budget
        reserve = len(_drop_marker(dropped)) + 1
        # The marker displaces text too: 99 dropped chars can become 112,
        # needing a third digit. Size the marker for the final count.
        while (width := len(_drop_marker(dropped + reserve)) + 1) > reserve:
            reserve = width
        if budget <= reserve:
            return _bookend_stdout(segments, cap, total, refs, keep)
        room = budget - reserve
        # A share big enough to split keeps the print's tail too: a printed
        # command result or log ends with its error or summary.
        tail_n = (room * 3) // 5 if room >= _SPLIT_MIN else 0
        head = segment[: room - tail_n]
        tail = segment[len(segment) - tail_n:] if tail_n else ""
        marker = _drop_marker(len(segment) - len(head) - len(tail))
        if tail:
            parts.append(head + marker + "\n" + tail)
        else:
            parts.append(head + marker + "\n")
        kept_chars += len(head) + len(tail)
        drops.append((index + 1, len(segment) - len(head) - len(tail)))

    heaviest = sorted(drops, key=lambda d: -d[1])[:_NAMED_DROPS]
    named = ", ".join(f"print {n} (-{d})" for n, d in heaviest)
    plural = "print" if len(segments) == 1 else "prints"
    notice = (
        f"…[stdout truncated: kept {kept_chars} of {total} chars across "
        f"{len(segments)} {plural} — one cell's stdout budget is {cap:,} "
        f"chars; heaviest drops: {named}.{_recovery_clause(refs)}]"
    )
    return "".join(parts).rstrip("\n") + "\n" + notice
