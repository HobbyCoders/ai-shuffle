"""SessionView — open a window into one stored session.

SessionSearch finds sessions; this tool reads them. The default window is the
tail — the last messages of a session are where its conclusion lives (what
was delivered, what was left open), which is exactly what "continue where we
left off" needs. The backend belongs to the host: it injects a
``session_slice(session_id, *, start, count)`` callable through the tool
context (stock implementation: ``JsonlStore.session_slice``), and this module
only formats the window.
"""

from __future__ import annotations

import inspect
from datetime import datetime
from typing import Any

from fluid.store.base import clip_text
from fluid.tools.registry import ToolCtx, ToolOutcome, ToolSpec

_DEFAULT_COUNT = 10
_MAX_COUNT = 40
_TEXT_CAP = 700
_TOOLS_SHOWN = 6
_OUTPUT_CAP = 16_000


def describe(tools: frozenset[str] | None = None) -> str:
    """SessionView's description, specialized to the session's surface."""
    has = (lambda name: tools is None or name in tools)
    find_route = (
        " Get session ids from SessionSearch (search, or call it with no "
        "query to list recent sessions)."
        if has("SessionSearch")
        else ""
    )
    return (
        "Open a stored past session: its title, date, and a window of its "
        "messages, with tool activity compacted to one line per call. By "
        "default the window is the session's TAIL — the final exchange, "
        "where what was delivered and what was left open live. Use it to "
        "pick work back up (\"continue where we left off\") or to see how "
        "something was done before redoing it.\n"
        "\n"
        "Page with offset/count: offset is a 0-based message index, negative "
        "counts from the end (default -10). The window is recall, not live "
        "state — verify anything you act on against the current files."
        + find_route
    )


SPEC = ToolSpec(
    name="SessionView",
    description=describe(),
    input_schema={
        "type": "object",
        "properties": {
            "session_id": {
                "type": "string",
                "description": "The stored session to open (from SessionSearch results).",
            },
            "offset": {
                "type": "integer",
                "description": (
                    "First message to show, 0-based; negative counts from "
                    "the end (default -10)."
                ),
            },
            "count": {
                "type": "integer",
                "description": f"Messages to show (default {_DEFAULT_COUNT}, max {_MAX_COUNT}).",
            },
        },
        "required": ["session_id"],
    },
    risk="safe",
    # Host- or loader-wired slice callable; absent (in-memory store, no host
    # injection) the engine drops the tool from the request schemas.
    requires_extra=("session_view",),
)


async def handler(input: dict[str, Any], ctx: ToolCtx) -> ToolOutcome:
    slice_fn = (ctx.extra or {}).get("session_view")
    if slice_fn is None:
        return ToolOutcome(
            text="SessionView unavailable: no session view backend injected.",
            is_error=True,
        )
    session_id = str(input.get("session_id", "")).strip()
    if not session_id:
        return ToolOutcome(text="session_id is required", is_error=True)
    count = min(_MAX_COUNT, max(1, int(input.get("count") or _DEFAULT_COUNT)))
    raw_offset = input.get("offset")
    start = int(raw_offset) if raw_offset is not None else -count

    window = slice_fn(session_id, start=start, count=count)
    if inspect.isawaitable(window):
        window = await window
    if window is None:
        return ToolOutcome(
            text=f"no stored session {session_id!r} — ids come from SessionSearch.",
            is_error=True,
        )

    title = f" {window.title!r}" if window.title else ""
    stamp = (
        datetime.fromtimestamp(window.last_ts).strftime("%Y-%m-%d")
        if window.last_ts else "unknown date"
    )
    if not window.messages:
        return ToolOutcome(
            text=(
                f"session {window.session_id}{title} — {stamp}, "
                f"{window.total} messages; the requested window is empty "
                f"(offset {start})."
            )
        )
    last = window.start + len(window.messages) - 1
    lines = [
        f"session {window.session_id}{title} — {stamp}, "
        f"{window.total} messages; showing {window.start}–{last}:"
    ]
    for i, msg in enumerate(window.messages):
        text = clip_text(msg.text, _TEXT_CAP) if msg.text.strip() else ""
        lines.append(f"[{window.start + i} {msg.role}] {text}".rstrip())
        for tool_line in msg.tools[:_TOOLS_SHOWN]:
            lines.append(f"    {tool_line}")
        if len(msg.tools) > _TOOLS_SHOWN:
            lines.append(f"    (+{len(msg.tools) - _TOOLS_SHOWN} more tool lines)")
    if window.start > 0:
        lines.append(
            f"[{window.start} earlier message(s) — page with offset/count]"
        )
    text = "\n".join(lines)
    if len(text) > _OUTPUT_CAP:
        text = text[:_OUTPUT_CAP] + "\n[output capped — lower count]"
    return ToolOutcome(text=text)
