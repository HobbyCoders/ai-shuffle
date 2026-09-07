"""Read windows — which LINES of a file the session has actually seen.

``ToolCtx.read_files`` says a file was Read; it does not say how much of it.
That was enough for the original gate, and it is exactly what an audited
code-mode session exploited: ``_ = Read(path, limit=5)`` unlocked Edit for the
whole file, and the Edit that followed carried an ``old_string`` recalled from
memory rather than copied from the file. The gate fired, the edit missed, the
turn burned on a diagnosis of a mismatch that a real Read would have prevented.

So Read and Write also record the window they delivered, as inclusive
1-based line spans (``end`` ``None`` = to the end of the file), and Edit asks
whether the lines it is about to replace fall inside one. A file with no
windows on record — a history written before windows existed — passes as it
always did; the check only tightens where the session has the information.

Windows follow edits: a successful Edit shifts every window below it by the
line delta and grows the one it landed in, so a model that read lines 1-120 and
inserted ten lines at 40 still owns 1-130 rather than being told to re-read
lines it has just written. Nothing here raises; every function tolerates a
context built before the field existed.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "Window",
    "covers",
    "describe",
    "record",
    "record_whole",
    "shift",
    "windows_of",
]

#: An inclusive 1-based line span; ``None`` end means "through end of file".
Window = tuple[int, int | None]


def windows_of(ctx: Any, path: str) -> list[Window]:
    table = getattr(ctx, "read_windows", None)
    if not isinstance(table, dict):
        return []
    found = table.get(path)
    return list(found) if isinstance(found, list) else []


def record(ctx: Any, path: str, start: int, end: int | None) -> None:
    """Add one delivered window for ``path``."""
    table = getattr(ctx, "read_windows", None)
    if not isinstance(table, dict):
        return
    start = max(1, int(start))
    if end is not None:
        end = max(start, int(end))
    table.setdefault(path, []).append((start, end))


def record_whole(ctx: Any, path: str) -> None:
    """The whole file was delivered (an image Read, a Write): one open window."""
    table = getattr(ctx, "read_windows", None)
    if not isinstance(table, dict):
        return
    table[path] = [(1, None)]


def covers(ctx: Any, path: str, start: int, end: int) -> bool:
    """Whether lines ``start..end`` all lie inside one recorded window.

    True when nothing is on record for the path: the file-level ledger is
    then the only evidence, and it already said yes.
    """
    windows = windows_of(ctx, path)
    if not windows:
        return True
    return any(
        a <= start and (b is None or b >= end) for a, b in windows
    )


def shift(ctx: Any, path: str, start: int, old_lines: int, new_lines: int) -> None:
    """Move the windows of ``path`` after a replacement at ``start``.

    ``old_lines`` lines beginning at ``start`` became ``new_lines`` lines. A
    window wholly above is untouched; one wholly below moves by the delta; one
    that contains the edit grows (or shrinks) by it, floored at its own start.
    """
    table = getattr(ctx, "read_windows", None)
    if not isinstance(table, dict):
        return
    windows = table.get(path)
    if not isinstance(windows, list) or not windows:
        return
    delta = new_lines - old_lines
    if delta == 0:
        return
    last = start + max(old_lines, 1) - 1
    moved: list[Window] = []
    for a, b in windows:
        if b is not None and b < start:
            moved.append((a, b))
        elif a > last:
            moved.append((max(1, a + delta), None if b is None else max(1, b + delta)))
        else:
            moved.append((a, None if b is None else max(a, b + delta)))
    table[path] = moved


def describe(ctx: Any, path: str) -> str:
    """``1-120, 400-end`` — the windows on record, for an error message."""
    parts = []
    for a, b in windows_of(ctx, path):
        parts.append(f"{a}-{'end' if b is None else b}")
    return ", ".join(parts)
