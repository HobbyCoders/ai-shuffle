"""Spill files: long tool output stays recoverable without filling the context.

When a tool result is over its cap, the text is written once to the session's
harness scratch directory (``scratch_dir(ctx)/tool-output/``, the folder the
"# Harness scratch" reminder names, swept with it) and the model sees the
head, the tail and one marker line naming what was cut and where it went. The
file holds exactly the text the head and tail were cut from. When the file
cannot be written, the result falls back to plain head + tail truncation.

The guardrail half: :func:`note_access` counts when the agent later Reads a
spill file or points Grep/Glob at the spill folder, in
``<project state root>/spill-stats/<session>.json`` (state, not scratch, so the
counts outlive the sweep). The file is one JSON object::

    {"session_id": "...", "updated_at": <unix time>,
     "spilled":      {"<producing tool>": <files written>},
     "read_back":    {"<producing tool>" | "unattributed": <reads>},
     "read_back_by": {"Read" | "Grep" | "Glob": <reads>}}

A cap that is never read back is cutting only what nobody needed; one read
back often is cutting too much.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from fluid.state import project_state_root, safe_key, scratch_dir
from fluid.tools.registry import ToolCtx

_log = logging.getLogger(__name__)

SPILL_SUBDIR = "tool-output"
STATS_DIR = "spill-stats"
READ_HINT = "Read it with offset/limit or Grep it instead of re-running"
_SAFE_TOOL = re.compile(r"[^A-Za-z0-9_]+")
# A spill file's name: the producing tool, a dash, 12 hex of its content hash.
_SPILL_NAME = re.compile(r"([A-Za-z0-9_]+)-[0-9a-f]{12}\.txt", re.IGNORECASE)
_stats_lock = threading.Lock()


def spill_dir(ctx: ToolCtx | None) -> Path | None:
    """``scratch/<session>/tool-output`` for this session, or None.

    None without a session id: ``scratch/default`` is shared and never swept.
    """
    if ctx is None or not ctx.session_id:
        return None
    try:
        return scratch_dir(ctx) / SPILL_SUBDIR
    except Exception:  # noqa: BLE001 — no state root means no spill
        return None


def write_spill(text: str, *, ctx: ToolCtx | None, tool: str) -> Path | None:
    """Write ``text`` to its spill file; its path, or None on any failure."""
    directory = spill_dir(ctx)
    if directory is None:
        return None
    # Named by content: the same output maps to the same file, so a repeated
    # command does not pile up copies and its cut result reads identically
    # (the marker stays byte-stable for the prompt cache and repeat detection).
    digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:12]
    path = directory / f"{_SAFE_TOOL.sub('_', tool) or 'tool'}-{digest}.txt"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        if not path.is_file():
            tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex[:8]}.tmp")
            with tmp.open("w", encoding="utf-8", errors="replace", newline="") as fh:
                fh.write(text)
            tmp.replace(path)
    except Exception:  # noqa: BLE001 — a failed spill falls back to truncation
        _log.debug("tool-output spill failed", exc_info=True)
        return None
    _bump(ctx, "spilled", tool)
    return path


def _split(text: str, limit: int, head_share: float) -> tuple[str, str]:
    """Head and tail within ``limit`` chars, cut at line ends where cheap."""
    head_n = max(0, int(limit * head_share))
    tail_n = max(0, limit - head_n)
    head = text[:head_n]
    tail = text[len(text) - tail_n:] if tail_n else ""
    # Snap to whole lines when a newline is near, so line numbers in the
    # marker are exact and Read offsets land where the agent expects.
    nl = head.rfind("\n")
    if nl >= max(0, len(head) - 400):
        head = head[: nl + 1]
    nl = tail.find("\n")
    if 0 <= nl < 400 and nl < len(tail) - 1:
        tail = tail[nl + 1:]
    return head, tail


def cut(
    text: str,
    limit: int,
    *,
    ctx: ToolCtx | None = None,
    tool: str = "tool",
    head_share: float = 1 / 3,
    saved_as: str = "full output",
    note: str = "",
) -> str:
    """``text`` when it fits in ``limit``; otherwise head + marker + tail.

    With a ``ctx`` the whole of ``text`` is spilled first and the marker names
    the file and the cut line range (lines of the file). ``saved_as`` says what
    the file holds ("full output" only when ``text`` is all the tool produced);
    ``note`` adds a plain caveat, e.g. a middle the caller never captured.
    Without a ctx, or when the write fails, the marker only counts the dropped
    chars (the old ``truncate`` format).
    """
    if len(text) <= limit:
        return text
    head, tail = _split(text, limit, head_share)
    dropped = len(text) - len(head) - len(tail)
    caveat = f"; {note}" if note else ""
    path = write_spill(text, ctx=ctx, tool=tool) if ctx is not None else None
    if path is None:
        return f"{head}\n... [{dropped} chars truncated{caveat}] ...\n{tail}"
    total = text.count("\n") + (0 if text.endswith("\n") else 1)
    first = head.count("\n") + 1
    last = total - tail.count("\n") - (0 if tail.endswith("\n") else 1)
    span = f"lines {first:,}-{last:,} of {total:,}, " if last >= first else ""
    marker = (
        f"... [cut {span}{dropped:,} chars — {saved_as} saved to {path} "
        f"(session scratch){caveat}; {READ_HINT}] ..."
    )
    return f"{head.rstrip(chr(10))}\n{marker}\n{tail}"


# ── read-back telemetry ──────────────────────────────────────────────────────


def _stats_path(ctx: ToolCtx) -> Path | None:
    try:
        return project_state_root(ctx) / STATS_DIR / f"{safe_key(ctx.session_id)}.json"
    except Exception:  # noqa: BLE001
        return None


def _bump(ctx: ToolCtx | None, bucket: str, key: str) -> None:
    """Add one to ``bucket[key]`` in the session's stats file."""
    if ctx is None or not ctx.session_id:
        return
    path = _stats_path(ctx)
    if path is None:
        return
    try:
        with _stats_lock:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    data = {}
            except (OSError, ValueError):
                data = {}
            counts = data.setdefault(bucket, {})
            counts[key] = int(counts.get(key, 0)) + 1
            data["session_id"] = ctx.session_id
            data["updated_at"] = time.time()
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex[:8]}.tmp")
            tmp.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
            tmp.replace(path)
    except Exception:  # noqa: BLE001 — telemetry never fails a call
        _log.debug("spill stats write failed", exc_info=True)


# Reader tool -> the input key holding the path it reads or searches.
_READERS = {"Read": "file_path", "Grep": "path", "Glob": "path"}


def note_access(name: str, input: Any, ctx: ToolCtx | None) -> None:
    """Count a finished Read, Grep or Glob aimed at this session's spill folder.

    Only the call's explicit path is looked at, resolved against the session
    cwd (``..`` and symlinks followed; on Windows the comparison ignores
    case). It counts when that path is this session's spill folder or inside
    it, once per call: for the tool that wrote the file when the path names a
    spill file, otherwise (the folder itself) as "unattributed". Output text
    and shell commands are never scanned.
    """
    key = _READERS.get(name)
    raw = input.get(key) if key and isinstance(input, dict) else None
    if not isinstance(raw, str) or not raw.strip() or ctx is None:
        return
    directory = spill_dir(ctx)
    if directory is None:
        return
    try:
        # Resolved exactly as Read, Grep and Glob resolve it: no ``~``
        # expansion, so a literal ``~`` folder under the cwd is not mistaken
        # for this session's spill folder under the home directory.
        target = Path(raw)
        if not target.is_absolute():
            target = Path(ctx.cwd) / target
        target, root = target.resolve(), directory.resolve()
    except (OSError, RuntimeError, ValueError):
        return
    if not target.is_relative_to(root):
        return
    match = _SPILL_NAME.fullmatch(target.name) if target != root else None
    producer = match.group(1) if match else "unattributed"
    _bump(ctx, "read_back", producer)
    _bump(ctx, "read_back_by", name)
    _log.info(
        "tool-output spill read back: session=%s producer=%s reader=%s",
        ctx.session_id, producer, name,
    )


def session_stats(ctx: ToolCtx) -> dict[str, Any]:
    """The persisted counts for this session (``{}`` when none)."""
    path = _stats_path(ctx)
    if path is None:
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}
