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

from collections.abc import Callable
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


# ─── cell stdout budget ──────────────────────────────────────────────────────

DEFAULT_STDOUT_CAP = 16384  # chars of cell stdout that reach the model
_DROP_MARKER = "…[-{n} chars]"
_NAMED_DROPS = 2  # how many of the heaviest drops the notice names


def _drop_marker(dropped: int) -> str:
    return _DROP_MARKER.format(n=dropped)


def fair_share_stdout(
    segments: list[str], cap: int = DEFAULT_STDOUT_CAP
) -> str:
    """Fit a cell's prints into ``cap`` chars, fairly, and name the damage.

    A tail-drop (``out[:cap]``) keeps the *first* ``cap`` chars and silently
    loses everything after — which is exactly where a model puts the thing it
    was working towards: the summary, the file list, the verdict. This instead
    max-min fair-shares the budget across ``segments`` (one segment ≈ one
    ``print``): every segment shorter than its share survives whole and frees
    its surplus for the greedy ones, so *something* from every print gets
    through. Each truncated segment keeps its head and carries an inline
    ``…[-N chars]`` marker, and a closing notice names the totals plus the
    heaviest drops so the model can see what to re-print in a smaller cell.

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
            # of prints): a bare ellipsis still shows the print happened.
            parts.append("…\n")
            drops.append((index + 1, len(segment)))
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
        f"{len(segments)} {plural}; heaviest drops: {named}]"
    )
    return "".join(parts).rstrip("\n") + "\n" + notice
