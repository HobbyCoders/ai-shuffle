"""SessionSearch — keyword recall across ALL stored sessions.

HistorySearch covers the current session's path; this tool sweeps the whole
session store, so "the thing we did last week" is answered from the record
instead of asked again. The search itself belongs to the backend: the host
injects a ``search(terms, *, limit, exclude_session_id)`` callable through the
tool context, and this module only ranks and formats what comes back. The
stock implementation is ``JsonlStore.search`` — a bounded, line-oriented sweep
of the store's directory, no index and no dependencies — but a store backed by
SQL or a vector index serves the same tool by offering the same callable.
"""

from __future__ import annotations

import inspect
import re
from datetime import datetime
from typing import Any

from fluid.tools.registry import ToolCtx, ToolOutcome, ToolSpec

_SNIPPET_RADIUS = 160
_MAX_MATCHES_PER_SESSION = 3
_OUTPUT_CAP = 16_000

def describe(tools: frozenset[str] | None = None) -> str:
    """SessionSearch's description, specialized to the session's surface."""
    has = (lambda name: tools is None or name in tools)
    recall_route = (
        " This searches OTHER sessions; for content trimmed from THIS "
        "session use HistorySearch/HistoryExpand."
        if has("HistorySearch") and has("HistoryExpand")
        else " This searches OTHER sessions, not the current one."
    )
    return (
        "Search past sessions' stored conversations for keywords and return "
        "matching snippets, newest sessions first. Use when the user "
        "references earlier work you cannot see (\"like we did last time\", "
        "\"the script from yesterday\") — search before asking them to "
        "repeat themselves.\n"
        "\n"
        "Matches are scored by how many query terms a message contains. "
        "Results name the session id and a snippet around the match; they "
        "are recall, not live state — verify anything you act on against "
        "the current files." + recall_route
    )


SPEC = ToolSpec(
    name="SessionSearch",
    description=describe(),
    input_schema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Keywords (matched case-insensitively, best matches first).",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum sessions to report (default 5).",
            },
        },
        "required": ["query"],
    },
    risk="safe",
    # Host- or loader-wired search callable; absent (in-memory store, no host
    # injection) the engine drops the tool from the request schemas.
    requires_extra=("session_search",),
)


def _snippet(text: str, term: str) -> str:
    at = text.lower().find(term)
    start = max(0, at - _SNIPPET_RADIUS)
    end = min(len(text), at + len(term) + _SNIPPET_RADIUS)
    flat = " ".join(text[start:end].split())
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return f"{prefix}{flat}{suffix}"


async def handler(input: dict[str, Any], ctx: ToolCtx) -> ToolOutcome:
    search = (ctx.extra or {}).get("session_search")
    if search is None:
        return ToolOutcome(
            text="SessionSearch unavailable: no session search backend injected.",
            is_error=True,
        )
    query = str(input.get("query", "")).strip()
    terms = [t.lower() for t in re.split(r"\s+", query) if t]
    if not terms:
        return ToolOutcome(text="empty query", is_error=True)
    limit = max(1, int(input.get("limit") or 5))

    hits = search(terms, limit=limit, exclude_session_id=ctx.session_id)
    if inspect.isawaitable(hits):
        hits = await hits

    hits = [h for h in hits if h.score > 0]
    if not hits:
        return ToolOutcome(
            text=f"no stored session matched {query!r} — try fewer or different terms."
        )
    # The backend is asked to rank; re-sorting here keeps the contract cheap
    # for hosts that only filter.
    hits = sorted(hits, key=lambda h: (-h.score, -h.last_ts))
    lines: list[str] = [f"{len(hits)} session(s) matched; best first:"]
    for hit in hits[:limit]:
        stamp = (
            datetime.fromtimestamp(hit.last_ts).strftime("%Y-%m-%d")
            if hit.last_ts else "unknown date"
        )
        lines.append(f"session {hit.session_id} ({stamp}, score {hit.score}):")
        for role, text in hit.texts[:_MAX_MATCHES_PER_SESSION]:
            term = next((t for t in terms if t in text.lower()), "")
            lines.append(f"  [{role}] {_snippet(text, term)}")
    text = "\n".join(lines)
    if len(text) > _OUTPUT_CAP:
        text = text[:_OUTPUT_CAP] + "\n[output capped — narrow the query]"
    return ToolOutcome(text=text)
