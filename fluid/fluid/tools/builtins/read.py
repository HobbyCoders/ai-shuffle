"""Read — return file contents (cat -n style) and record the read.

Reading registers the file in ``ctx.read_files`` so Edit can enforce
read-before-write within the session (the Anthropic tool-discipline rule).

Three content paths, in order of dispatch:

* **Images** (png/jpeg/gif/webp) are returned as a base64 ``ToolImage`` so the
  model *sees* them. They are never decoded as text — a multi-MB image pushed
  through ``read_text(errors="replace")`` becomes megabytes of replacement-char
  garbage that then rides every subsequent provider request.
* **Other binaries** are refused with a short diagnostic instead of garbage.
* **Text** is read lazily (never the whole file for a windowed read) with a
  2,000-line default window, 40,000-character page budget and per-line cap,
  so a giant log can't blow up the context either.
"""

from __future__ import annotations

import base64
from io import BytesIO
from itertools import islice
from pathlib import Path
from typing import Any

from fluid.tools.builtins._util import (
    is_binary,
    missing_path_note,
    resolve,
    unchanged_read_note,
)
from fluid.tools import read_window
from fluid.tools.fragments import READ_BEFORE_EDIT
from fluid.tools.registry import (
    ContentWindow,
    ToolCtx,
    ToolImage,
    ToolOutcome,
    ToolSpec,
)

# Media types the Anthropic/OpenAI vision APIs accept as base64 images.
IMAGE_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

# Providers cap a base64 image around 5MB; 3.75MB of raw bytes encodes to 5MB.
MAX_IMAGE_BYTES = 3_750_000

DEFAULT_LINE_LIMIT = 2_000  # lines returned when no explicit limit is given
MAX_TEXT_CHARS = 40_000  # page body; stop on a line boundary, then name the next offset
MAX_LINE_CHARS = 2_000  # longer lines are cut with a marker

def describe(tools: frozenset[str] | None = None) -> str:
    """Read's description, specialized to the session's tool surface."""
    has = (lambda name: tools is None or name in tools)
    line_prefix = "Text is returned with `lineno<TAB>` prefixes" + (
        " (display formatting, not file content — strip the line-number "
        "prefix from anything you copy into Edit's old_string)"
        if has("Edit")
        else " (display formatting only, not file content)"
    )
    edit_license = f"- {READ_BEFORE_EDIT}" if has("Edit") else ""
    bash_route = " Prefer Read over cat/head/tail via Bash." if has("Bash") else ""
    if edit_license:
        closing = edit_license + bash_route
    elif bash_route:
        closing = "-" + bash_route
    else:
        closing = ""
    return (
        f"Read a file from the filesystem. {line_prefix}. Images "
        "(png/jpg/gif/webp) are returned visually so you can see them; an "
        f"image over {MAX_IMAGE_BYTES:,} bytes is refused — downscale or "
        "crop it first.\n"
        "\n"
        "Usage notes:\n"
        f"- Reads up to {DEFAULT_LINE_LIMIT} lines from the start by default. "
        f"Each page is also bounded to {MAX_TEXT_CHARS:,} characters. "
        "For a larger file, pass offset/limit — a "
        "truncation marker tells you the offset to continue from.\n"
        f"- Lines longer than {MAX_LINE_CHARS} characters are cut with a "
        "marker. A missing path or a non-image binary file returns a short "
        "diagnostic rather than content.\n"
        + (f"{closing}\n" if closing else "")
    ).rstrip("\n")


SPEC = ToolSpec(
    name="Read",
    description=describe(),
    input_schema={
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "Absolute or cwd-relative path."},
            "offset": {
                "type": "integer",
                "description": "1-based line number to start reading from (default 1).",
            },
            "limit": {
                "type": "integer",
                "description": f"Maximum lines to return (default {DEFAULT_LINE_LIMIT}).",
            },
        },
        "required": ["file_path"],
    },
    risk="safe",
)


def _read_image(path: Path, media_type: str) -> ToolOutcome:
    size = path.stat().st_size
    if size > MAX_IMAGE_BYTES:
        return ToolOutcome(
            text=(
                f"image too large to attach: {path.name} is {size} bytes "
                f"(limit {MAX_IMAGE_BYTES}). Downscale or crop it first."
            ),
            is_error=True,
        )
    raw = path.read_bytes()
    size = len(raw)
    if not raw:
        return ToolOutcome(
            text=f"not a readable image: {path} is {size} bytes (empty file)",
            is_error=True,
        )
    try:
        from PIL import Image as PILImage
    except ImportError:
        pass  # Preserve pass-through on workers without Pillow.
    else:
        try:
            with PILImage.open(BytesIO(raw)) as image:
                image.verify()
            # verify() may only inspect headers (notably for JPEG). Reopen
            # after verification and decode pixels, but keep the original bytes.
            with PILImage.open(BytesIO(raw)) as image:
                image.load()
        except Exception:
            return ToolOutcome(
                text=f"not a readable image: {path} is {size} bytes (cannot decode)",
                is_error=True,
            )
    data = base64.b64encode(raw).decode("ascii")
    # The caption carries the FULL path, not just the file name: an image is
    # consumed once and then trimmed out of history (fluid.context.trim), and
    # the placeholder that replaces it can only tell the model to re-Read the
    # file if the path survives in the text block beside the image.
    return ToolOutcome(
        text=f"[image: {path}, {media_type}, {size} bytes]",
        images=(ToolImage(media_type=media_type, data=data),),
    )


def _read_text(path: Path, offset: int, limit: int | None) -> tuple[str, bool, int]:
    """Read a bounded page, returning text, whether more remains, and line count.

    The character budget stops before a whole numbered line, never midway
    through one. The caller must use the delivered count for continuation,
    Edit coverage and ResultRef slicing, not the requested window length.
    """
    window_len = int(limit) if limit else DEFAULT_LINE_LIMIT
    out: list[str] = []
    chars = 0
    more = False
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        lines = (line.rstrip("\r\n") for line in fh)
        for i, line in enumerate(islice(lines, offset - 1, offset - 1 + window_len)):
            if len(line) > MAX_LINE_CHARS:
                line = line[:MAX_LINE_CHARS] + "… [line truncated]"
            numbered_line = f"{offset + i}\t{line}"
            size = len(numbered_line) + bool(out)
            if out and chars + size > MAX_TEXT_CHARS:
                more = True
                break
            out.append(numbered_line)
            chars += size
        else:
            more = next(lines, None) is not None
    numbered = "\n".join(out)
    if more:
        numbered += (
            f"\n... [output limited to {len(out)} lines — "
            f"continue with offset={offset + len(out)}]"
        )
    return numbered, more, len(out)


async def handler(input: dict[str, Any], ctx: ToolCtx) -> ToolOutcome:
    path: Path = resolve(input["file_path"], ctx)
    if not path.exists() or not path.is_file():
        note = missing_path_note(str(input["file_path"]), path)
        return ToolOutcome(
            text=f"file not found: {path}{note}", is_error=True,
            error_code="missing_path" if not path.exists() else None,
        )

    media_type = IMAGE_MEDIA_TYPES.get(path.suffix.lower())
    if media_type is not None:
        outcome = _read_image(path, media_type)
        if not outcome.is_error:
            ctx.read_files.add(str(path))
            read_window.record_whole(ctx, str(path))
        return outcome

    if is_binary(path):
        return ToolOutcome(
            text=(
                f"binary file: {path} ({path.stat().st_size} bytes) — "
                "Read shows text and images only"
            ),
            is_error=True,
        )

    offset = max(1, int(input.get("offset", 1)))
    limit = input.get("limit")
    numbered, more, window_len = _read_text(path, offset, limit)
    ctx.read_files.add(str(path))
    # The lines actually delivered, for Edit's window check: a windowed read
    # that stopped short owns exactly its window; one that reached the end of
    # the file owns everything from its offset down.
    read_window.record(ctx, str(path), offset, offset + window_len - 1 if more else None)

    # Same path, same window, same bytes on disk → the content is already in
    # the conversation. A write elsewhere moves mtime_ns, so the entry stops
    # matching without any explicit invalidation.
    key = (str(path), offset, int(limit) if limit else None)
    stamp = _stamp(path)
    prior_stamp = ctx.read_seen.get(key)
    # …but "already in the conversation" is a claim about the MODEL's context,
    # not about this process's memory. In code mode an oversized result never
    # reaches the model at all — it is handed to the cell as a ResultRef whose
    # repr is a stub, and a cell that only measured it (``print(len(out))``)
    # learned nothing. Telling that model to "reuse what you already learned"
    # would be false. So the note only fires when the earlier read's payload was small enough
    # that the calling surface delivered it whole.
    # Sized the way the code-mode kernel sizes it — UTF-8 bytes, not chars.
    delivered = (
        ctx.result_budget is None
        or len(numbered.encode("utf-8", "replace")) <= ctx.result_budget
    )
    repeat = (
        stamp is not None and prior_stamp == stamp and key in ctx.read_delivered
    )
    if stamp is not None:
        ctx.read_seen[key] = stamp
        if delivered:
            ctx.read_delivered.add(key)
        elif prior_stamp != stamp:
            ctx.read_delivered.discard(key)

    if not numbered:
        # No lines, so no payload to claim an identity for — and in particular
        # no window claim, which would otherwise let a later read be answered
        # by slicing a ref for lines this call never saw.
        return ToolOutcome(
            text=_no_lines_note(path, offset) + unchanged_read_note(repeat)
        )

    # A read that starts at line 1 and runs out of file is the whole file,
    # whatever offset/limit was asked for, so it claims the window-independent
    # identity. Everything else claims its own window's identity *and* names
    # the whole-file one, so a holder of the whole file can serve this slice.
    window = None if offset == 1 and not more else (offset, key[2])
    return ToolOutcome(
        text=numbered + unchanged_read_note(repeat),
        content_id=_content_id(str(path), window, stamp),
        window_of=(
            None
            if window is None or stamp is None
            else ContentWindow(
                content_id=_content_id(str(path), None, stamp),
                start=offset - 1,
                stop=offset - 1 + window_len,
            )
        ),
    )


def _count_lines(path: Path) -> int | None:
    """Line count, or None when the file cannot be re-read. Error path only."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            return sum(1 for _ in fh)
    except OSError:
        return None


def _no_lines_note(path: Path, offset: int) -> str:
    """Why a read produced nothing — an empty file, or a window past the end.

    "(empty file)" for a 900-line file read at ``offset=1200`` would send the
    model hunting for a bug that is not there. Say which case it is, and hand
    back the number that makes the next offset obvious.
    """
    if offset <= 1:
        return "(empty file)"
    total = _count_lines(path)
    if total is None:
        return f"no lines at offset {offset} — the file could not be re-read"
    plural = "line" if total == 1 else "lines"
    if total == 0:
        return "(empty file)"
    return (
        f"offset {offset} is past the end of the file ({total} {plural}) — "
        f"the last readable offset is {total}"
    )


def _stamp(path: Path) -> tuple[int, int] | None:
    """``(mtime_ns, size)`` — the cheap identity of a file's current bytes."""
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def _content_id(
    path: str,
    window: tuple[int, int | None] | None,
    stamp: tuple[int, int] | None,
) -> str | None:
    """Identity of the payload this call returned, or None if unknowable.

    Window *and* file version, so a different offset/limit or a byte that moved
    yields a different id — except that ``window=None`` means "the whole file",
    the identity every read that returned all of it shares no matter which
    offset/limit produced it. Code mode compares these against the ids it has
    already turned into ResultRefs; a match means the kernel is holding this
    very payload, so the repeat read comes back as a pointer instead of a copy,
    and a match on the whole-file id makes a *windowed* re-read a slice of it
    (see ``codemode/_runner.py``). The trailing "already seen" note is not part
    of the id — it is presentation, and it is what the pointer replaces.
    """
    if stamp is None:
        return None
    if window is None:
        span = "*"
    else:
        offset, limit = window
        span = f"{offset}|{limit if limit else ''}"
    return f"Read|{path}|{span}|{stamp[0]}|{stamp[1]}"
