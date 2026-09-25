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

DEFAULT_STDOUT_CAP = 16384  # chars of cell stdout that reach the model
_DROP_MARKER = "…[-{n} chars]"
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
    its surplus for the greedy ones, so *something* from every print gets
    through. Each truncated segment keeps its head and carries an inline
    ``…[-N chars]`` marker, and a closing notice names the totals, the budget
    that did the cutting, the heaviest drops, and — when the cell's tool
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
        reserve = len(_drop_marker(len(segment) - budget)) + 1
        if budget <= reserve:
            # Shares too thin to carry a counted marker (a cell with hundreds
            # of prints): a bare ellipsis still shows the print happened,
            # paid for out of the segment's own share. A share too thin even
            # for that (thousands of prints) joins one ellipsis for the whole
            # run, so the payload cannot grow past the cap two characters at
            # a time.
            drops.append((index + 1, len(segment)))
            if budget >= 2 or not parts or parts[-1] != "…\n":
                parts.append("…\n")
            continue
        head = segment[: budget - reserve]
        marker = _drop_marker(len(segment) - len(head))
        parts.append(head + marker + "\n")
        kept_chars += len(head)
        drops.append((index + 1, len(segment) - len(head)))

    heaviest = sorted(drops, key=lambda d: -d[1])[:_NAMED_DROPS]
    named = ", ".join(f"print {n} (-{d})" for n, d in heaviest)
    plural = "print" if len(segments) == 1 else "prints"
    notice = (
        f"…[stdout truncated: kept {kept_chars} of {total} chars across "
        f"{len(segments)} {plural} — one cell's stdout budget is {cap:,} "
        f"chars; heaviest drops: {named}.{_recovery_clause(refs)}]"
    )
    return "".join(parts).rstrip("\n") + "\n" + notice
