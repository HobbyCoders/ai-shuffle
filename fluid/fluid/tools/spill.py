"""Spill files: long tool output stays recoverable without filling the context.

When a tool result is over its cap, the full text is written to the session's
harness scratch directory (``scratch_dir(ctx)/tool-output/``, the folder the
"# Harness scratch" reminder names, swept with it) and the model sees the head,
the tail and one marker line naming what was cut and where it went. The tail
gets the larger share by default: errors, tracebacks and exit summaries sit at
the end of command output. When the file cannot be written, the result falls
back to plain head + tail truncation.

The guardrail half: :func:`note_readback` counts when the agent later reads or
greps a spill file, per session and per producing tool, in
``<project state root>/spill-stats/<session>.json`` (state, not scratch, so the
counts outlive the sweep). A cap that is never read back is cutting only what
nobody needed; one read back often is cutting too much.
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
# Tools whose calls can read a spill file back. Bash counts when its command
# names the spill folder (`grep … tool-output/…`, `sed -n …`).
_READERS = ("Read", "Grep", "Glob", "Bash", "PowerShell")
_PATH_KEYS = ("file_path", "path", "command")
_SAFE_TOOL = re.compile(r"[^A-Za-z0-9_]+")
_SPILL_NAME = re.compile(r"([A-Za-z0-9_]+)-[0-9a-f]{12}\.txt")
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
    """Write ``text`` to a fresh spill file; its path, or None on any failure."""
    directory = spill_dir(ctx)
    if directory is None:
        return None
    # Named by content: the same output maps to the same file, so a repeated
    # command does not pile up copies and its cut result reads identically
    # (repeat-result detection compares the text, marker included).
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


_READ_HINT = "Read it with offset/limit or Grep it instead of re-running"


def _marker(head: str, tail: str, dropped: int, total_lines: int, saved: str) -> str:
    """head + one marker line (cut line range, chars, where it went) + tail."""
    first = head.count("\n") + 1
    last = total_lines - tail.count("\n") - (0 if tail.endswith("\n") else 1)
    span = f"lines {first:,}-{last:,} of {total_lines:,}, " if last >= first else ""
    marker = f"... [cut {span}{dropped:,} chars — {saved}; {_READ_HINT}] ..."
    return f"{head.rstrip(chr(10))}\n{marker}\n{tail}"


def _line_count(newlines: int, ends_with_newline: bool) -> int:
    return newlines + (0 if ends_with_newline else 1)


def cut(
    text: str,
    limit: int,
    *,
    ctx: ToolCtx | None = None,
    tool: str = "tool",
    head_share: float = 1 / 3,
) -> str:
    """``text`` when it fits in ``limit``; otherwise head + marker + tail.

    With a ``ctx`` the full text is spilled first and the marker names the
    file and the cut line range. Without one, or when the write fails, the
    marker only counts the dropped chars (the old ``truncate`` format).
    """
    if len(text) <= limit:
        return text
    head, tail = _split(text, limit, head_share)
    dropped = len(text) - len(head) - len(tail)
    path = write_spill(text, ctx=ctx, tool=tool) if ctx is not None else None
    if path is None:
        return f"{head}\n... [{dropped} chars truncated] ...\n{tail}"
    total_lines = _line_count(text.count("\n"), text.endswith("\n"))
    return _marker(head, tail, dropped, total_lines, f"full output saved to {path} (session scratch)")


# ── streamed spills (command output too big to hold in memory) ───────────────

# Chars a streamed spill file may hold. A command printing more than this keeps
# its first DISK_CEILING chars on disk and its tail inline; the marker names the
# range that was kept by neither.
DISK_CEILING = 64 * 1024 * 1024
_FLUSH_CHARS = 65536  # an unterminated line is flushed once this long


class SpillStream:
    """A tool's output written to its spill file as it arrives.

    The caller keeps only a bounded head and tail in memory; this keeps every
    char (up to :data:`DISK_CEILING`) on disk, so the marker can promise the
    file holds what it says. Nothing touches disk until the output passes
    ``open_after`` chars: most results fit inline and never need a file.
    Bytes are decoded incrementally and ``transform`` (ANSI stripping) runs on
    whole lines, so the file matches the inline text. Counts (``chars``,
    ``newlines``) cover the whole output even when writing failed or stopped.
    """

    def __init__(
        self,
        ctx: ToolCtx | None,
        tool: str,
        *,
        open_after: int = 0,
        ceiling: int = DISK_CEILING,
        transform: Any = None,
    ) -> None:
        import codecs

        self.ctx, self.tool, self.ceiling = ctx, tool, ceiling
        self.open_after = open_after
        self.transform = transform or (lambda text: text)
        self.chars = 0
        self.newlines = 0
        self.written = 0
        self.ends_with_newline = False
        self.failed = spill_dir(ctx) is None
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._carry = ""
        self._pending: list[str] = []
        self._fh: Any = None
        self._tmp: Path | None = None
        self._hash = hashlib.sha256()
        self._closed = False

    @property
    def capped(self) -> bool:
        """True when the disk ceiling stopped the file short of the output."""
        return self.chars > self.written

    def feed(self, chunk: bytes) -> None:
        """Decode ``chunk`` and write its complete lines."""
        self._carry += self._decoder.decode(chunk)
        nl = self._carry.rfind("\n")
        if nl >= 0:
            self._emit(self._carry[: nl + 1])
            self._carry = self._carry[nl + 1:]
        elif len(self._carry) >= _FLUSH_CHARS:
            self._emit(self._carry)
            self._carry = ""

    def _emit(self, raw: str) -> None:
        text = self.transform(raw)
        if not text:
            return
        self.chars += len(text)
        self.newlines += text.count("\n")
        self.ends_with_newline = text.endswith("\n")
        if self.failed:
            return
        if self._fh is None:
            self._pending.append(text)
            if self.chars <= self.open_after:
                return
            pending, self._pending = "".join(self._pending), []
            if not self._open():
                return
            text = pending
        self._write(text)

    def _open(self) -> bool:
        directory = spill_dir(self.ctx)
        try:
            assert directory is not None
            directory.mkdir(parents=True, exist_ok=True)
            name = f".{_SAFE_TOOL.sub('_', self.tool) or 'tool'}-{uuid.uuid4().hex[:12]}.part"
            self._tmp = directory / name
            self._fh = self._tmp.open("w", encoding="utf-8", errors="replace", newline="")
            return True
        except Exception:  # noqa: BLE001 — no file means plain truncation
            _log.debug("tool-output spill stream open failed", exc_info=True)
            self.failed = True
            return False

    def _write(self, text: str) -> None:
        room = self.ceiling - self.written
        if room <= 0:
            return
        piece = text[:room]
        try:
            self._fh.write(piece)
        except Exception:  # noqa: BLE001
            _log.debug("tool-output spill stream write failed", exc_info=True)
            self.failed = True
            self.discard()
            return
        self._hash.update(piece.encode("utf-8", "replace"))
        self.written += len(piece)

    def end(self) -> None:
        """Flush the decoder and any unterminated last line."""
        self._carry += self._decoder.decode(b"", final=True)
        if self._carry:
            self._emit(self._carry)
            self._carry = ""

    def finish(self) -> Path | None:
        """Close and name the file by content; its path, or None without one."""
        if self._closed:
            return None
        if self._fh is None and not self.failed and self._pending:
            # Never passed open_after, but the caller wants a file anyway.
            pending, self._pending = "".join(self._pending), []
            if self._open():
                self._write(pending)
        self._closed = True
        if self.failed or self._fh is None or self._tmp is None:
            self.discard()
            return None
        try:
            self._fh.close()
            path = self._tmp.with_name(
                f"{_SAFE_TOOL.sub('_', self.tool) or 'tool'}-{self._hash.hexdigest()[:12]}.txt")
            if path.is_file():
                self._tmp.unlink()
            else:
                self._tmp.replace(path)
        except Exception:  # noqa: BLE001
            _log.debug("tool-output spill stream finish failed", exc_info=True)
            self.discard()
            return None
        _bump(self.ctx, "spilled", self.tool)
        return path

    def discard(self) -> None:
        """Close and delete any partial file. Safe to call more than once."""
        self._closed = True
        self._pending = []
        fh, self._fh = self._fh, None
        try:
            if fh is not None:
                fh.close()
        except Exception:  # noqa: BLE001
            pass
        tmp, self._tmp = self._tmp, None
        try:
            if tmp is not None:
                tmp.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass


def cut_streamed(text: str, limit: int, stream: SpillStream, *, head_share: float = 1 / 3) -> str:
    """Like :func:`cut`, for output whose full copy ``stream`` wrote to disk.

    ``text`` is what the caller kept in memory (head + tail, its middle maybe
    already dropped); counts come from ``stream``, which saw everything.
    """
    stream.end()
    total = max(stream.chars, len(text))
    if len(text) <= limit and total <= limit:
        stream.discard()
        return text
    head, tail = _split(text, limit, head_share)
    dropped = max(0, total - len(head) - len(tail))
    path = stream.finish()
    if path is None:
        return f"{head}\n... [{dropped} chars truncated] ...\n{tail}"
    total_lines = _line_count(stream.newlines, stream.ends_with_newline)
    lost_to = total - len(tail)
    if stream.capped and lost_to > stream.written:
        saved = (
            f"the first {stream.written:,} chars saved to {path} (session scratch, "
            f"{stream.ceiling // (1024 * 1024)} MiB cap); chars "
            f"{stream.written + 1:,}-{lost_to:,} of {total:,} were not kept"
        )
    elif stream.capped:
        saved = f"the first {stream.written:,} chars saved to {path} (session scratch); the rest is below"
    else:
        saved = f"full output saved to {path} (session scratch)"
    return _marker(head, tail, dropped, total_lines, saved)


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


def note_readback(name: str, input: Any, ctx: ToolCtx | None) -> None:
    """Count a Read/Grep/Bash call that touches this session's spill folder."""
    if ctx is None or name not in _READERS or not isinstance(input, dict):
        return
    values = [v for k in _PATH_KEYS if isinstance(v := input.get(k), str) and v]
    if not any(SPILL_SUBDIR in v.lower() for v in values):
        return
    directory = spill_dir(ctx)
    if directory is None:
        return
    root = str(directory).replace("\\", "/")
    for value in values:
        norm = value.replace("\\", "/")
        # Case-insensitive find: Windows paths arrive in any casing.
        at = norm.lower().find(root.lower())
        if at < 0:
            continue
        match = _SPILL_NAME.match(norm[at + len(root):].lstrip("/"))
        if match:
            producers = [match.group(1)]
        else:
            # The whole folder (a Grep, Glob or ls over it): once for each
            # tool whose spill files the call covers, so the counts still say
            # which cap the agent came back for.
            # Grep's file filter is ``glob`` (its ``pattern`` is the regex);
            # Glob's is ``pattern``.
            names = input.get("glob") if name == "Grep" else input.get("pattern") if name == "Glob" else None
            producers = _folder_producers(directory, names)
        if not producers:
            return
        for producer in producers:
            _bump(ctx, "read_back", producer)
        _bump(ctx, "read_back_by", name)
        _log.info(
            "tool-output spill read back: session=%s producer=%s reader=%s",
            ctx.session_id, ",".join(producers), name,
        )
        return


def _folder_producers(directory: Path, glob: Any = None) -> list[str]:
    """Producing tools of the spill files in ``directory`` (``glob``-filtered)."""
    import fnmatch

    pattern = glob.rsplit("/", 1)[-1] if isinstance(glob, str) and glob else None
    found: set[str] = set()
    try:
        for entry in directory.iterdir():
            match = _SPILL_NAME.fullmatch(entry.name)
            if match and (pattern is None or fnmatch.fnmatch(entry.name, pattern)):
                found.add(match.group(1))
    except OSError:
        return []
    return sorted(found)


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
