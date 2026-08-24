"""SessionSearch — keyword recall across ALL stored sessions.

HistorySearch covers the current session's path; this tool sweeps the whole
session store, so "the thing we did last week" is answered from the record
instead of asked again. Scanning is line-oriented over the JSONL logs —
no index, no dependencies — and bounded, which is plenty for a personal
store of hundreds of sessions.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from fluid.tools.registry import ToolCtx, ToolOutcome, ToolSpec

_SNIPPET_RADIUS = 160
_MAX_MATCHES_PER_SESSION = 3
_OUTPUT_CAP = 16_000
_MAX_SESSIONS_SCANNED = 500

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
    # Host-wired store directory; absent (in-memory store) the engine drops
    # the tool from the request schemas.
    requires_extra=("session_store_dir",),
)


def _texts_of(message: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for block in message.get("content") or []:
        if not isinstance(block, dict):
            continue
        kind = block.get("kind")
        if kind == "text" and block.get("text"):
            out.append(str(block["text"]))
        elif kind == "toolresultblock":
            for inner in block.get("content") or []:
                if isinstance(inner, dict) and inner.get("kind") == "text" and inner.get("text"):
                    out.append(str(inner["text"]))
    return out


def _snippet(text: str, term: str) -> str:
    at = text.lower().find(term)
    start = max(0, at - _SNIPPET_RADIUS)
    end = min(len(text), at + len(term) + _SNIPPET_RADIUS)
    flat = " ".join(text[start:end].split())
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return f"{prefix}{flat}{suffix}"


def _scan_session(path: Path, terms: list[str]) -> tuple[int, float, list[str]]:
    """(score, last_ts, snippets) for one session log."""
    score = 0
    last_ts = 0.0
    snippets: list[str] = []
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                if not any(t in line.lower() for t in terms):
                    continue  # cheap line-level prefilter before JSON parsing
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("t") != "node":
                    continue
                node = rec.get("node") or {}
                last_ts = max(last_ts, float(node.get("ts") or 0.0))
                for text in _texts_of(node.get("message") or {}):
                    lowered = text.lower()
                    hit_terms = [t for t in terms if t in lowered]
                    if not hit_terms:
                        continue
                    score += len(hit_terms)
                    if len(snippets) < _MAX_MATCHES_PER_SESSION:
                        role = (node.get("message") or {}).get("role", "?")
                        snippets.append(
                            f"  [{role}] {_snippet(text, hit_terms[0])}"
                        )
    except OSError:
        return 0, 0.0, []
    return score, last_ts, snippets


async def handler(input: dict[str, Any], ctx: ToolCtx) -> ToolOutcome:
    store_dir = (ctx.extra or {}).get("session_store_dir")
    if not store_dir:
        return ToolOutcome(
            text="SessionSearch unavailable: no session store directory injected.",
            is_error=True,
        )
    query = str(input.get("query", "")).strip()
    terms = [t.lower() for t in re.split(r"\s+", query) if t]
    if not terms:
        return ToolOutcome(text="empty query", is_error=True)
    limit = max(1, int(input.get("limit") or 5))

    directory = Path(store_dir)
    logs = [
        p for p in sorted(
            directory.glob("*.jsonl"),
            key=lambda p: p.stat().st_mtime if p.exists() else 0,
            reverse=True,
        )
        if not p.name.endswith(".events.jsonl")
    ][:_MAX_SESSIONS_SCANNED]

    hits: list[tuple[int, float, str, list[str]]] = []
    for log in logs:
        session_id = log.name[: -len(".jsonl")]
        if session_id == ctx.session_id:
            continue  # this session's own history belongs to HistorySearch
        score, last_ts, snippets = _scan_session(log, terms)
        if score:
            hits.append((score, last_ts, session_id, snippets))
    if not hits:
        return ToolOutcome(
            text=f"no stored session matched {query!r} — try fewer or different terms."
        )
    hits.sort(key=lambda h: (-h[0], -h[1]))
    lines: list[str] = [f"{len(hits)} session(s) matched; best first:"]
    for score, last_ts, session_id, snippets in hits[:limit]:
        stamp = (
            datetime.fromtimestamp(last_ts).strftime("%Y-%m-%d")
            if last_ts else "unknown date"
        )
        lines.append(f"session {session_id} ({stamp}, score {score}):")
        lines.extend(snippets)
    text = "\n".join(lines)
    if len(text) > _OUTPUT_CAP:
        text = text[:_OUTPUT_CAP] + "\n[output capped — narrow the query]"
    return ToolOutcome(text=text)
