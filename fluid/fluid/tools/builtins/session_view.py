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

import asyncio
import inspect
from datetime import datetime
from typing import Any

from fluid.store.base import clip_text
from fluid.tools.registry import ToolCtx, ToolOutcome, ToolSpec

_DEFAULT_COUNT = 10
# A window may hold many messages; the output budget, not the count, bounds
# what is printed (newest kept for a tail window, earliest for a forward page).
_MAX_COUNT = 200
_TEXT_CAP = 700
_TOOLS_SHOWN = 6
_OUTPUT_CAP = 16_000
_FOOTER_RESERVE = 300
_SHOW_MODES = ("all", "conversation", "tools")


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
        "counts from the end (default -10); count goes up to "
        f"{_MAX_COUNT} and what does not fit the output budget is named in "
        "a footer with the offset to continue from. show=\"conversation\" "
        "prints only user/assistant text, show=\"tools\" only tool activity. "
        "session_id takes either the store id or the app's session id, and "
        "opens sessions any of the owner's workers runs. The window is "
        "recall, not live state — verify anything you act on against the "
        "current files."
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
                "description": (
                    "The stored session to open (from SessionSearch results): "
                    "its store id or its app session id."
                ),
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
            "show": {
                "type": "string",
                "enum": list(_SHOW_MODES),
                "description": (
                    "'all' (default): text and tool lines; 'conversation': "
                    "user/assistant text only; 'tools': tool activity only."
                ),
            },
        },
        "required": ["session_id"],
    },
    risk="safe",
    # Host- or loader-wired slice callable; absent (in-memory store, no host
    # injection) the engine drops the tool from the request schemas.
    requires_extra=("session_view",),
)


def _accepts_viewer(slice_fn: Any) -> bool:
    """True when the host's backend scopes windows to the calling session.

    A multi-user host declares ``viewer_session_id`` on ``session_slice`` and
    hides sessions the caller may not read; the stock single-user backends
    take no such argument and are called exactly as before.
    """
    try:
        return "viewer_session_id" in inspect.signature(slice_fn).parameters
    except (TypeError, ValueError):
        return False


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

    scope = (
        {"viewer_session_id": ctx.session_id}
        if _accepts_viewer(slice_fn) else {}
    )
    window = await asyncio.to_thread(slice_fn, session_id, start=start, count=count, **scope)
    if inspect.isawaitable(window):
        window = await window
    if window is None:
        return ToolOutcome(
            text=f"no stored session {session_id!r} — ids come from SessionSearch.",
            is_error=True,
        )

    title = f" {window.title!r}" if window.title else ""
    worker = getattr(window, "worker", None)
    if worker:
        title += f" [on {worker}]"
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
    show = str(input.get("show") or "all").strip().lower()
    if show not in _SHOW_MODES:
        show = "all"
    blocks = [
        (window.start + i, block)
        for i, msg in enumerate(window.messages)
        if (block := _render(window.start + i, msg, show))
    ]
    # A tail window (the default, or a negative offset) keeps its newest
    # messages when the budget runs out; a forward page keeps its earliest.
    tail = raw_offset is None or int(raw_offset) < 0
    kept = _fit(blocks, tail)
    window_last = window.start + len(window.messages) - 1
    if len(kept) < len(blocks):
        lo = kept[0][0] if tail else window.start
        hi = window_last if tail else kept[-1][0]
    else:
        lo, hi = window.start, window_last
    mode = "" if show == "all" else f" ({show} only)"
    lines = [
        f"session {window.session_id}{title} — {stamp}, "
        f"{window.total} messages; showing {lo}–{hi}{mode}:"
    ]
    for _index, block in kept:
        lines.extend(block)
    if not blocks:
        lines.append(f"(no {'tool activity' if show == 'tools' else 'text'} in this window)")
    if len(kept) < len(blocks):
        if tail:
            lines.append(
                f"[output budget reached — {lo - window.start} older message(s) of "
                f"this window not shown; page with offset={window.start}, "
                f"count={lo - window.start}]"
            )
        else:
            lines.append(f"[output budget reached — continue with offset={hi + 1}]")
    if lo > 0 and (tail or len(kept) == len(blocks)):
        lines.append(f"[{lo} earlier message(s) — page with offset/count]")
    if not tail and len(kept) == len(blocks) and hi < window.total - 1:
        lines.append(
            f"[{window.total - 1 - hi} later message(s) — continue with offset={hi + 1}]"
        )
    text = "\n".join(lines)
    if len(text) > _OUTPUT_CAP:
        text = text[:_OUTPUT_CAP] + "\n[output capped — lower count]"
    return ToolOutcome(text=text)


def _render(index: int, msg: Any, show: str) -> list[str]:
    """One message's printed lines under ``show``; [] when it shows nothing."""
    text = clip_text(msg.text, _TEXT_CAP) if msg.text.strip() else ""
    if show == "conversation":
        return [f"[{index} {msg.role}] {text}"] if text else []
    if show == "tools":
        if not msg.tools:
            return []
        text = ""
    lines = [f"[{index} {msg.role}] {text}".rstrip()]
    for tool_line in msg.tools[:_TOOLS_SHOWN]:
        lines.append(f"    {tool_line}")
    if len(msg.tools) > _TOOLS_SHOWN:
        lines.append(f"    (+{len(msg.tools) - _TOOLS_SHOWN} more tool lines)")
    return lines


def _fit(blocks: list[tuple[int, list[str]]], tail: bool) -> list[tuple[int, list[str]]]:
    """The blocks that fit the output budget: newest first for a tail window."""
    budget = _OUTPUT_CAP - _FOOTER_RESERVE
    kept: list[tuple[int, list[str]]] = []
    used = 0
    for item in (reversed(blocks) if tail else blocks):
        size = sum(len(line) + 1 for line in item[1])
        if kept and used + size > budget:
            break
        kept.append(item)
        used += size
    if tail:
        kept.reverse()
    return kept
