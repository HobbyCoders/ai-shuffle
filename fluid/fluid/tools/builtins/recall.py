"""Recall — scored retrieval over the continual store (memories + skills).

The # Memory snapshot in the prompt is bounded (and project-scoped): entries
past the cap age into an archive, and other projects' facts are not rendered
at all. Recall is the explicit retrieval path over everything the learner
has ever kept — live memories in every scope, archived memories, and the
saved-skill index — so a bounded prompt never means lost knowledge. Each
live-memory hit bumps that entry's recall counter, which is what protects
useful entries from eviction (least-frequently-recalled goes first): using
memory is also what keeps it alive.

Wired through ``ctx.extra["recall_store"]`` (the SkillStore, injected by the
engine loader when the continual store exists); absent that injection the
engine drops the tool from the schema surface entirely.
"""

from __future__ import annotations

from typing import Any

from fluid.tools.registry import ToolCtx, ToolOutcome, ToolSpec

_DEFAULT_LIMIT = 3
_MAX_LIMIT = 10


def describe(tools: frozenset[str] | None = None) -> str:
    """Recall's description, specialized to the session's surface."""
    has = (lambda name: tools is None or name in tools)
    session_route = (
        " For past conversations and what happened in them, use "
        "SessionSearch instead — Recall covers learned facts and skills, "
        "not transcripts."
        if has("SessionSearch")
        else ""
    )
    return (
        "Search your long-term memory: every fact your continual learner "
        "has kept (including archived entries and other projects' entries "
        "that the # Memory list above does not show) plus the saved-skill "
        "index. Use it when the task touches something you may have learned "
        "before — a preference, a convention, a port, a past correction — "
        "and the # Memory list does not answer it. Returns the top matches "
        "per category with their source." + session_route
    )


SPEC = ToolSpec(
    name="Recall",
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
                "description": "Maximum matches per category (default 3, max 10).",
            },
        },
        "required": ["query"],
    },
    risk="safe",
    # Loader-wired store handle; absent (no continual store) the engine drops
    # the tool from the request schemas.
    requires_extra=("recall_store",),
)


async def handler(input: dict[str, Any], ctx: ToolCtx) -> ToolOutcome:
    query = str(input.get("query") or "").strip()
    if not query:
        return ToolOutcome(text="query is required", is_error=True)
    try:
        limit = min(_MAX_LIMIT, max(1, int(input.get("limit") or _DEFAULT_LIMIT)))
    except (TypeError, ValueError):
        limit = _DEFAULT_LIMIT
    store = ctx.extra.get("recall_store")
    if store is None:
        return ToolOutcome(
            text="unavailable: no continual store wired in this session",
            is_error=True,
        )

    from fluid.context.screening import screen_findings

    lines: list[str] = []
    try:
        memory_hits = store.search_memories(query, limit=limit, project=ctx.extra.get("memory_project"))
    except Exception as exc:
        return ToolOutcome(text=f"recall failed: {exc}", is_error=True)
    project = ctx.extra.get("memory_project")
    conflicts = [e for e in store.memories() if e.conflicted and e.provenance != "tool"
                 and (project is None or e.scope in ("global", project))
                 and any(t in (e.text + " " + (e.key or "")).lower() for t in query.lower().split())]
    for entry in conflicts[:limit]:
        lines.append(f"- [{entry.id}] Unresolved conflict; revision: {entry.revision}; key: {entry.key or 'unspecified'}. "
                     "Ask the user for the current fact, then correct this ID and revision with Remember. Do not guess a value.")
    if memory_hits:
        lines.append("Memories:")
        for entry, archived in memory_hits:
            findings = screen_findings(entry.text)
            if findings:
                lines.append(
                    f"- [{entry.id}] [withheld — suspected injection "
                    f"pattern(s): {', '.join(findings)}]"
                )
                continue
            tags = [f"revision: {entry.revision}", f"source: {entry.provenance}"]
            if entry.key:
                tags.append(f"key: {entry.key}")
            if entry.protected:
                tags.append("explicit save")
            if entry.provenance == "tool":
                tags.append("harvested from tool output, unconfirmed — confirm with Remember if still true")
            if archived:
                tags.append("archived")
            if entry.scope != "global":
                tags.append(f"scope: {entry.scope}")
            suffix = f"  ({', '.join(tags)})" if tags else ""
            lines.append(f"- [{entry.id}] {entry.text}{suffix}")

    tokens = [t for t in query.lower().split() if t]
    skill_hits: list[tuple[Any, bool]] = []
    try:
        # Live skills first, then the ones curation archived — archiving is
        # bounded storage, not deletion, so an archived skill stays findable.
        for s, archived in (
            *((s, False) for s in store.list()),
            *((s, True) for s in store.archived_skills()),
        ):
            hay = f"{s.name} {s.description}".lower()
            if any(t in hay for t in tokens):
                skill_hits.append((s, archived))
    except Exception:
        skill_hits = []
    if skill_hits:
        lines.append("Saved skills (usable in code mode via `from skills import <name>`):")
        for s, archived in skill_hits[:limit]:
            tag = "  (archived — restore by moving the file back)" if archived else ""
            lines.append(f"- {s.name}: {s.description} [used {s.uses}x]{tag}")

    if not lines:
        return ToolOutcome(
            text=(
                f"No stored memory or skill matches {query!r}. Matching is "
                "keyword-in-text, so try one or two broader words (the "
                "tool or file name, not the whole question) before "
                "concluding nothing is known. This is recall over learned "
                "facts only — absence here does not mean the answer does not "
                "exist elsewhere. Once you establish the fact this session, "
                "Remember it so the next session finds it here."
            )
        )
    lines.append(
        "(Recalled content is what was learned at the time — verify against "
        "current state before acting on it.)"
    )
    return ToolOutcome(text="\n".join(lines))
