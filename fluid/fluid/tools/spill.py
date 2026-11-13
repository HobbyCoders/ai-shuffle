"""Spill files: long tool output stays recoverable without filling the context.

When a tool result is over its cap, the full text is written to the session's
harness scratch directory (``scratch_dir(ctx)/tool-output/``, the folder the
"# Harness scratch" reminder names, swept with it) and the model sees the head,
the tail and one marker line naming what was cut and where it went. The tail
gets the larger share by default: errors, tracebacks and exit summaries sit at
the end of command output. When the file cannot be written, the result falls
back to plain head + tail truncation.

The guardrail half: :func:`note_access` counts when the agent later reads or
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
from types import TracebackType
from typing import Any, Self

from fluid.state import project_state_root, safe_key, scratch_dir
from fluid.tools.registry import ToolCtx

_log = logging.getLogger(__name__)

SPILL_SUBDIR = "tool-output"
STATS_DIR = "spill-stats"
# Tools whose calls can read a spill file back.
_READERS = ("Read", "Grep", "Glob", "Bash", "PowerShell")
_PATH_KEYS = ("file_path", "path", "command")
_SAFE_TOOL = re.compile(r"[^A-Za-z0-9_]+")
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

# Bytes a streamed spill file may hold. A command printing more keeps the
# start of its output on disk (cut at a character boundary) and its tail
# inline; the marker names the char range kept by neither.
DISK_CEILING = 64 * 1024 * 1024
_FLUSH_CHARS = 65536  # an unterminated line is flushed once this long
# How far back a forced flush looks for an escape sequence still arriving.
_ESC_WINDOW = 256
# A complete escape sequence, as the ANSI stripper sees it. ``ESC ]`` alone is
# left out: it starts an OSC string that is complete only at its terminator.
_ESC_SEQ = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\^_])")


def _flush_point(text: str) -> int:
    """Where a forced flush may cut ``text`` without splitting an escape.

    Stripping ``ESC[31m`` cut in two would leave both halves in the file
    while the inline copy, stripped in one piece, drops it. An escape still
    incomplete at the end of ``text`` waits for the next read.
    """
    pos = max(0, len(text) - _ESC_WINDOW)
    while (esc := text.find("\x1b", pos)) >= 0:
        match = _ESC_SEQ.match(text, esc)
        if match is None:
            return esc
        pos = match.end()
    return len(text)


def _size(n: int) -> str:
    mib = 1024 * 1024
    return f"{n // mib} MiB" if n % mib == 0 else f"{n:,}-byte"


class SpillStream:
    """A tool's output written to its spill file as it arrives.

    The caller keeps only a bounded head and tail in memory; this keeps the
    whole output (up to :data:`DISK_CEILING` bytes) on disk, so the marker can
    promise the file holds what it says. Nothing touches disk until the output
    passes ``open_after`` chars: most results fit inline and never need a file.

    Bytes are decoded incrementally, so a multi-byte character split across
    reads waits for its last byte, and ``transform`` (ANSI stripping) runs on
    whole lines; a forced flush of a long unterminated line holds back an
    escape sequence still arriving. The file text therefore matches the
    inline text char for char. ``chars`` and ``newlines`` count the whole
    output even when writing failed or stopped at the ceiling.

    The spill file is the recovery record. Use the stream as a context manager
    around everything that produces the output: leaving the block because the
    user or turn cancelled deletes the partial file; leaving it any other way
    (an exception included) keeps it. A normal result renders with
    :func:`cut_streamed` inside the block, which settles the file first.
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
        self.written = 0  # chars on disk, for the marker
        self.written_bytes = 0  # what the ceiling counts
        self.ends_with_newline = False
        self.path: Path | None = None
        self.failed = spill_dir(ctx) is None
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._carry = ""
        self._pending: list[str] = []
        self._fh: Any = None
        self._tmp: Path | None = None
        self._hash = hashlib.sha256()
        self._closed = False

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        import asyncio

        if self._closed:
            return
        if exc_type is not None and issubclass(exc_type, (asyncio.CancelledError, KeyboardInterrupt)):
            self.discard()
            return
        self.end()
        path = self.finish()
        if path is not None and exc_type is not None:
            _log.info("%s raised %s; its output so far is kept at %s", self.tool, exc_type.__name__, path)

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
            at = _flush_point(self._carry)
            self._emit(self._carry[:at])
            self._carry = self._carry[at:]

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
            self._fh = self._tmp.open("wb")
            return True
        except Exception:  # noqa: BLE001 — no file means plain truncation
            _log.debug("tool-output spill stream open failed", exc_info=True)
            self.failed = True
            return False

    def _write(self, text: str) -> None:
        room = self.ceiling - self.written_bytes
        if room <= 0:
            return
        data = text.encode("utf-8", "replace")
        if len(data) > room:
            # The ceiling counts bytes on disk; stop at a character boundary.
            text = data[:room].decode("utf-8", "ignore")
            data = text.encode("utf-8")
        try:
            self._fh.write(data)
        except Exception:  # noqa: BLE001
            _log.debug("tool-output spill stream write failed", exc_info=True)
            self.failed = True
            self.discard()
            return
        self._hash.update(data)
        self.written += len(text)
        self.written_bytes += len(data)

    def end(self) -> None:
        """Flush the decoder and any unterminated last line."""
        self._carry += self._decoder.decode(b"", final=True)
        if self._carry:
            self._emit(self._carry)
            self._carry = ""

    def finish(self) -> Path | None:
        """Close the file and name it by content; its path, or None without one.

        The handle is closed before the rename and nothing else ever opens it
        for writing, so the file can be read the moment this returns, on
        Windows too.
        """
        if self._closed:
            return self.path
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
            self._fh = None
            path = self._tmp.with_name(
                f"{_SAFE_TOOL.sub('_', self.tool) or 'tool'}-{self._hash.hexdigest()[:12]}.txt")
            if path.is_file():
                self._tmp.unlink()
            else:
                self._tmp.replace(path)
            self._tmp = None
        except Exception:  # noqa: BLE001
            _log.debug("tool-output spill stream finish failed", exc_info=True)
            self.discard()
            return None
        _bump(self.ctx, "spilled", self.tool)
        self.path = path
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


def cut_streamed(
    text: str,
    limit: int,
    stream: SpillStream,
    *,
    head_share: float = 1 / 3,
    what: str = "full output",
) -> str:
    """Like :func:`cut`, for output whose full copy ``stream`` wrote to disk.

    ``text`` is what the caller kept in memory (head + tail, its middle maybe
    already dropped); counts come from ``stream``, which saw everything.
    ``what`` names the record in the marker, e.g. "output up to the timeout".
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
    if stream.capped:
        kept = (
            f"the first {stream.written:,} chars of the {what} saved to {path} "
            f"(session scratch; the file stops at its {_size(stream.ceiling)} cap)"
        )
        saved = (
            f"{kept}; chars {stream.written + 1:,}-{lost_to:,} of {total:,} were not kept"
            if lost_to > stream.written else f"{kept}; the rest is below"
        )
    else:
        saved = f"{what} saved to {path} (session scratch)"
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


# A spill file's name: the producing tool, a dash, 12 hex of its content hash.
_SPILL_NAME = r"([A-Za-z0-9_]+)-[0-9a-fA-F]{12}\.txt(?!\.?[A-Za-z0-9_])"
# A name standing alone (a Glob listing relative to the folder, ``ls``) or
# right after the folder name; never the tail of a longer word or path part.
_BARE_NAME = re.compile(r"(?:(?<=" + SPILL_SUBDIR + r"/)|(?<![A-Za-z0-9_./-]))" + _SPILL_NAME, re.IGNORECASE)
# A tool's own cut marker names the file it just wrote; that is not a read.
_OWN_MARKER = re.compile(r"saved to [^\n]*?\(session scratch")


def note_access(name: str, input: Any, result: str | None, ctx: ToolCtx | None) -> None:
    """Count a finished call that read a spill file back, by producing tool.

    Nothing about the query is interpreted. What counts is the spill files
    the call touched or returned: the path a Read opened, the files a Grep or
    Glob listed, the files a Bash or PowerShell command or its output names.
    The producing tool is the filename's prefix, and each producer counts
    once per call however many lines repeat its files. A name counts only
    when it is a file in this session's spill folder, written as a full path
    or right after ``tool-output/``, or bare when the call was aimed at the
    folder itself. A call aimed at the folder that names no spill file counts
    once as "unattributed".
    """
    if ctx is None or name not in _READERS or not isinstance(input, dict):
        return
    named = "\n".join(v for k in _PATH_KEYS if isinstance(v := input.get(k), str) and v)
    named = named.replace("\\", "/")
    output = "" if name == "Read" else _OWN_MARKER.sub("", result or "").replace("\\", "/")
    if SPILL_SUBDIR not in named.lower() and SPILL_SUBDIR not in output.lower():
        return
    directory = spill_dir(ctx)
    if directory is None:
        return
    root = str(directory).replace("\\", "/").rstrip("/")
    aimed = root.lower() in named.lower()
    candidates: set[str] = set()
    for match in _BARE_NAME.finditer(f"{named}\n{output}"):
        before = match.string[max(0, match.start() - len(SPILL_SUBDIR) - 1):match.start()]
        if aimed or before.lower() == f"{SPILL_SUBDIR}/":
            candidates.add(match.group(0).lower())
    producers: set[str] = set()
    if candidates:
        try:
            existing = {p.name.lower(): p.name for p in directory.iterdir()}
        except OSError:
            existing = {}
        producers = {real.rsplit("-", 1)[0] for c in candidates if (real := existing.get(c))}
    if not producers:
        if not aimed:
            return
        producers = {"unattributed"}
    for producer in sorted(producers):
        _bump(ctx, "read_back", producer)
    _bump(ctx, "read_back_by", name)
    _log.info(
        "tool-output spill read back: session=%s producer=%s reader=%s",
        ctx.session_id, ",".join(sorted(producers)), name,
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
