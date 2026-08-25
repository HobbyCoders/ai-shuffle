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
  2000-line default window and per-line length cap, so a giant log can't blow
  up the context either.
"""

from __future__ import annotations

import base64
from itertools import islice
from pathlib import Path
from typing import Any

from fluid.tools.builtins._util import is_binary, missing_path_note, resolve
from fluid.tools.fragments import READ_BEFORE_EDIT
from fluid.tools.registry import ToolCtx, ToolImage, ToolOutcome, ToolSpec

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
MAX_LINE_CHARS = 2_000  # longer lines are cut with a marker

def describe(tools: frozenset[str] | None = None) -> str:
    """Read's description, specialized to the session's tool surface."""
    has = (lambda name: tools is None or name in tools)
    line_prefix = "Text is returned with `lineno<TAB>` prefixes" + (
        " (display formatting only — never copy it into Edit old_string)"
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
        "When you only need part of a large file, pass offset/limit — a "
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
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return ToolOutcome(
        text=f"[image: {path.name}, {media_type}, {size} bytes]",
        images=(ToolImage(media_type=media_type, data=data),),
    )


def _read_text(path: Path, offset: int, limit: int | None) -> str:
    """Windowed, lazy line read — the file is consumed only up to the window
    end, so a Read with offset/limit on a huge file stays cheap."""
    window_len = int(limit) if limit else DEFAULT_LINE_LIMIT
    out: list[str] = []
    more = False
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        lines = (line.rstrip("\r\n") for line in fh)
        for i, line in enumerate(islice(lines, offset - 1, offset - 1 + window_len)):
            if len(line) > MAX_LINE_CHARS:
                line = line[:MAX_LINE_CHARS] + "… [line truncated]"
            out.append(f"{offset + i}\t{line}")
        more = next(lines, None) is not None
    numbered = "\n".join(out)
    if more:
        numbered += (
            f"\n... [output limited to {window_len} lines — "
            f"continue with offset={offset + window_len}]"
        )
    return numbered


async def handler(input: dict[str, Any], ctx: ToolCtx) -> ToolOutcome:
    path: Path = resolve(input["file_path"], ctx)
    if not path.exists() or not path.is_file():
        note = missing_path_note(str(input["file_path"]), path)
        return ToolOutcome(text=f"file not found: {path}{note}", is_error=True)

    media_type = IMAGE_MEDIA_TYPES.get(path.suffix.lower())
    if media_type is not None:
        outcome = _read_image(path, media_type)
        if not outcome.is_error:
            ctx.read_files.add(str(path))
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
    numbered = _read_text(path, offset, input.get("limit"))
    ctx.read_files.add(str(path))
    return ToolOutcome(text=numbered or "(empty file)")
