"""SessionSearch — recall across ALL stored sessions: search or list them.

HistorySearch covers the current session's path; this tool sweeps the whole
session store, so "the thing we did last week" is answered from the record
instead of asked again. With a query it is keyword search; without one it
lists recent sessions — the entry point for "what were we last working on?".
The lookups belong to the backend: the host injects ``search(terms, *, limit,
exclude_session_id)`` and ``recent_sessions(*, limit, exclude_session_id)``
callables through the tool context, and this module only ranks and formats
what comes back. The stock implementations live on ``JsonlStore`` — bounded,
line-oriented sweeps of the store's directory, no index and no dependencies —
but a store backed by SQL or a vector index serves the same tool by offering
the same callables.
"""

from __future__ import annotations

import inspect
import re
from datetime import datetime
from typing import Any

from fluid.tools.builtins._util import repeat_search_note
from fluid.tools.registry import ToolCtx, ToolOutcome, ToolSpec

_SNIPPET_RADIUS = 160
_MAX_MATCHES_PER_SESSION = 3
_OUTPUT_CAP = 16_000
_DEFAULT_RECENT = 10


def describe(tools: frozenset[str] | None = None) -> str:
    """SessionSearch's description, specialized to the session's surface."""
    has = (lambda name: tools is None or name in tools)
    open_route = (
        " Open a matched session with SessionView to see what was actually "
        "done there."
        if has("SessionView")
        else ""
    )
    recall_route = (
        " This covers OTHER sessions; for content trimmed from THIS "
        "session use HistorySearch/HistoryExpand."
        if has("HistorySearch") and has("HistoryExpand")
        else " This covers OTHER sessions, not the current one."
    )
    return (
        "Your recall over past sessions. With a query: keyword-search every "
        "stored conversation and return matching snippets, best sessions "
        "first. WITHOUT a query: list recent sessions, newest first, with "
        "titles and a last-reply preview — start there for \"what were we "
        "working on?\". Use it whenever earlier work is referenced (\"like "
        "we did last time\", \"the script from yesterday\") — search before "
        "asking the user to repeat themselves.\n"
        "\n"
        "Matches are scored by how many query terms a message contains. "
        "Results name the session id, its title, and a snippet around the "
        "match; they are recall, not live state — verify anything you act "
        "on against the current files. The header states how many sessions "
        "matched and how many of them are shown — a large total is the "
        "backend's raw hit count, not a reason to re-query with different "
        "words; raise `limit` to see further down the same ranking."
        + open_route + recall_route
    )


SPEC = ToolSpec(
    name="SessionSearch",
    description=describe(),
    input_schema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "Keywords (matched case-insensitively, best matches "
                    "first). Omit to list recent sessions instead."
                ),
            },
            "limit": {
                "type": "integer",
                "description": "Maximum sessions to report (default 5; 10 when listing).",
            },
        },
        "required": [],
    },
    risk="safe",
    # Host- or loader-wired search callable; absent (in-memory store, no host
    # injection) the engine drops the tool from the request schemas.
    requires_extra=("session_search",),
)


def _stamp(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if ts else "unknown date"


def _label(session_id: str, title: str | None) -> str:
    return f"session {session_id} {title!r}" if title else f"session {session_id}"


def _snippet(text: str, term: str) -> str:
    at = text.lower().find(term)
    start = max(0, at - _SNIPPET_RADIUS)
    end = min(len(text), at + len(term) + _SNIPPET_RADIUS)
    flat = " ".join(text[start:end].split())
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return f"{prefix}{flat}{suffix}"


def _snippet_lines(texts: list[tuple[str, str]], terms: list[str]) -> list[str]:
    """Up to ``_MAX_MATCHES_PER_SESSION`` *distinct* snippets for one session.

    A session that says the same thing in every matching message — a repeated
    instruction block, a re-pasted error — otherwise spends the whole per-
    session budget printing one string three times, so the cap costs context
    without adding evidence. Deduping on the rendered snippet lets the budget
    reach further down the session's hits.
    """
    lines: list[str] = []
    seen: set[str] = set()
    for role, text in texts:
        term = next((t for t in terms if t in text.lower()), "")
        snippet = _snippet(text, term)
        if snippet in seen:
            continue
        seen.add(snippet)
        lines.append(f"  [{role}] {snippet}")
        if len(lines) >= _MAX_MATCHES_PER_SESSION:
            break
    return lines


def _repeat_note(terms: list[str], limit: int, ctx: ToolCtx) -> str:
    """Grep's repeat-search note, for a query already run this session.

    The results still come back in full; the note only says the answer is
    already in the conversation, so a re-run does not read as new evidence.
    State lives in ``ctx.extra`` — the same per-session dict the backend
    callables arrive through.
    """
    seen: set[tuple[Any, ...]] = ctx.extra.setdefault("session_search_seen", set())
    key = (tuple(terms), limit)
    note = repeat_search_note(key in seen)
    seen.add(key)
    return note


async def _list_recent(input: dict[str, Any], ctx: ToolCtx) -> ToolOutcome:
    recent = (ctx.extra or {}).get("session_recent")
    if recent is None:
        return ToolOutcome(
            text=(
                "this backend cannot list sessions — provide a query to "
                "search instead."
            ),
            is_error=True,
        )
    limit = max(1, int(input.get("limit") or _DEFAULT_RECENT))
    rows = recent(limit=limit, exclude_session_id=ctx.session_id)
    if inspect.isawaitable(rows):
        rows = await rows
    if not rows:
        return ToolOutcome(text="no stored sessions yet.")
    lines = [f"{len(rows[:limit])} recent session(s), newest first:"]
    for row in rows[:limit]:
        count = f", {row.message_count} messages" if row.message_count else ""
        lines.append(f"{_label(row.session_id, row.title)} ({_stamp(row.last_ts)}{count})")
        if row.preview:
            lines.append(f"  last: {row.preview}")
    text = "\n".join(lines)
    if len(text) > _OUTPUT_CAP:
        text = text[:_OUTPUT_CAP] + "\n[output capped — lower the limit]"
    return ToolOutcome(text=text)


async def handler(input: dict[str, Any], ctx: ToolCtx) -> ToolOutcome:
    search = (ctx.extra or {}).get("session_search")
    if search is None:
        return ToolOutcome(
            text="SessionSearch unavailable: no session search backend injected.",
            is_error=True,
        )
    query = str(input.get("query", "") or "").strip()
    terms = [t.lower() for t in re.split(r"\s+", query) if t]
    if not terms:
        return await _list_recent(input, ctx)
    limit = max(1, int(input.get("limit") or 5))

    hits = search(terms, limit=limit, exclude_session_id=ctx.session_id)
    if inspect.isawaitable(hits):
        hits = await hits

    hits = [h for h in hits if h.score > 0]
    if not hits:
        return ToolOutcome(
            text=f"no stored session matched {query!r} — try fewer or different terms."
            + _repeat_note(terms, limit, ctx)
        )
    # The backend is asked to rank; re-sorting here keeps the contract cheap
    # for hosts that only filter.
    hits = sorted(hits, key=lambda h: (-h.score, -h.last_ts))
    shown = hits[:limit]
    # ``limit`` is a hint to the backend, which returns the whole ranked list —
    # so the count must say how many of the matches are actually on screen.
    # Reporting the total as if it were the listing read as "25 sessions are
    # relevant" and drove the same query again with different words.
    header = (
        f"{len(hits)} session(s) matched; showing the best {len(shown)} "
        "(raise limit to see more):"
        if len(hits) > len(shown)
        else f"{len(hits)} session(s) matched; best first:"
    )
    lines: list[str] = [header]
    for hit in shown:
        lines.append(
            f"{_label(hit.session_id, hit.title)} "
            f"({_stamp(hit.last_ts)}, score {hit.score}):"
        )
        lines.extend(_snippet_lines(hit.texts, terms))
    text = "\n".join(lines)
    if len(text) > _OUTPUT_CAP:
        text = text[:_OUTPUT_CAP] + "\n[output capped — narrow the query]"
    return ToolOutcome(text=text + _repeat_note(terms, limit, ctx))
