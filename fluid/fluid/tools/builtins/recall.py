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

import asyncio
import time
from typing import Any

from fluid.tools.registry import ToolCtx, ToolOutcome, ToolSpec

_DEFAULT_LIMIT = 3
_MAX_LIMIT = 10
_MAX_IDS = 10


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
        "Search your long-term memory: every saved fact (archived ones too) "
        "plus the saved-skill index. Use ids=[...] for the full text of a "
        "# Memory line before relying on it, and keywords when the task "
        "touches something you may have learned before — a preference, a "
        "convention, a port, a past correction. Returns the top matches per "
        "category with their source." + session_route
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
            "ids": {
                "type": "array", "items": {"type": "string"},
                "description": "Ids for full text.",
            },
        },
    },
    risk="safe",
    # Loader-wired store handle; absent (no continual store) the engine drops
    # the tool from the request schemas.
    requires_extra=("recall_store",),
)


async def handler(input: dict[str, Any], ctx: ToolCtx) -> ToolOutcome:
    query = str(input.get("query") or "").strip()
    ids = input.get("ids")
    if ids is not None and (not isinstance(ids, list) or not all(isinstance(i, str) for i in ids)):
        return ToolOutcome(text="ids must be a list of memory ids", is_error=True)
    ids = list(dict.fromkeys(i.strip() for i in ids or () if i.strip()))
    if len(ids) > _MAX_IDS:
        return ToolOutcome(text=f"ids takes at most {_MAX_IDS} memory ids", is_error=True)
    if not query and not ids:
        return ToolOutcome(text="query or ids is required", is_error=True)
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

    # One scope rule for ids and keywords: global facts plus this session's
    # project (the loader always sets memory_project; None is unscoped).
    project = ctx.extra.get("memory_project")
    if ids:
        fetched = await asyncio.to_thread(_fetch, ids, store, project)
        if not query:
            return fetched
        found = await asyncio.to_thread(_lookup, query, limit, store, project)
        return ToolOutcome(text=fetched.text + "\n\n" + found.text,
                           is_error=fetched.is_error and found.is_error)
    return await asyncio.to_thread(_lookup, query, limit, store, project)


def _fetch(ids: list[str], store: Any, project: str | None = None) -> ToolOutcome:
    """Exact fetch by id: full text and every label, one block per memory.
    Another project's fact is reported like a missing id, so its text and
    existence stay out of this session."""
    from fluid.context.screening import screen_findings
    try:
        hits = store.memories_by_id(ids, project=project)
    except Exception as exc:  # noqa: BLE001
        return ToolOutcome(text=f"recall failed: {exc}", is_error=True)
    successor = _local_ids(store) if any(e.superseded_by for e, _ in hits) else {}
    lines: list[str] = []
    seen: set[str] = set()
    for entry, archived in hits:
        seen.update({entry.id, f"{entry.origin}:{entry.id.split(':')[-1]}"})
        findings = screen_findings(entry.text) + screen_findings(entry.when or "")
        if findings:
            lines.append(f"- [{entry.id}] [withheld — suspected injection pattern(s): "
                         f"{', '.join(dict.fromkeys(findings))}]")
            continue
        tags = _tags(entry, archived, successor)
        if entry.when:
            tags.insert(1, f"when: {entry.when}")
        if entry.always:
            tags.append("always applies")
        lines.append(f"- [{entry.id}] {entry.text}  ({', '.join(tags)})")
        if entry.conflicted:
            lines.append(f"  Unresolved conflict; ask the user for the current fact, then correct "
                         f"[{entry.id}] revision {entry.revision} with Remember.")
    missing = [i for i in ids if i not in seen]
    if missing:
        where = " in this project" if project is not None else ""
        lines.append("No memory with id " + ", ".join(f"[{i}]" for i in missing) + where
                     + " (it may have been forgotten); search by keyword instead.")
    return ToolOutcome(text="\n".join(lines), is_error=not hits)


def _tags(entry: Any, archived: bool, successor: dict[str, str]) -> list[str]:
    tags = [f"revision: {entry.revision}", f"source: {entry.provenance}"]
    if entry.key:
        tags.append(f"key: {entry.key}")
    if entry.protected:
        tags.append("explicit save")
    elif entry.durable:
        tags.append("agent-saved")
    if entry.updated_at:
        # A date lets the caller notice a fact a newer one superseded.
        tags.append("updated: " + time.strftime("%Y-%m-%d", time.gmtime(entry.updated_at)))
    if entry.provenance == "tool":
        tags.append("harvested from tool output, unconfirmed — confirm with Remember if still true")
    if entry.superseded_by:
        shown = successor.get(entry.superseded_by, entry.superseded_by)
        tags.append(f"superseded by [{shown}]; not current")
    if archived:
        tags.append("archived")
    if entry.scope != "global":
        tags.append(f"scope: {entry.scope}")
    return tags


def _local_ids(store: Any) -> dict[str, str]:
    """Replica-stable global IDs to the IDs Recall and Remember use here."""
    try:
        from fluid.codemode.facts import global_id
        return {global_id(vars(e)): e.id for e in (*store.memories(), *store.archived_memories())
                if e.origin}
    except Exception:  # noqa: BLE001 — a label falls back to the global ID
        return {}


def _lookup(query: str, limit: int, store: Any, project: str | None) -> ToolOutcome:
    from fluid.context.screening import screen_findings
    from fluid.codemode.relevance import recall_terms, recall_scorer

    score = recall_scorer(recall_terms(query))
    lines: list[str] = []
    try:
        memory_hits = store.search_memories(query, limit=limit, project=project)
    except Exception as exc:
        return ToolOutcome(text=f"recall failed: {exc}", is_error=True)
    conflicts = [e for e in store.memories() if (e.conflicted or (
                     e.protected and e.superseded_by and not e.supersession_confirmed)) and e.provenance != "tool"
                 and (project is None or e.scope in ("global", project))
                 and score(e.text + " " + (e.key or ""))]
    for entry in conflicts[:limit]:
        if entry.superseded_by:
            lines.append(f"- [{entry.id}] Superseded by [{entry.superseded_by}]; both facts are retained. "
                         "The old value is not current. Resolve this user-kept pair with the owner.")
        lines.append(f"- [{entry.id}] Unresolved conflict; revision: {entry.revision}; key: {entry.key or 'unspecified'}. "
                     "Ask the user for the current fact, then correct this ID and revision with Remember. Do not guess a value.")
    if memory_hits:
        successor = _local_ids(store) if any(e.superseded_by for e, _ in memory_hits) else {}
        lines.append("Memories:")
        for entry, archived in memory_hits:
            findings = screen_findings(entry.text)
            if findings:
                lines.append(
                    f"- [{entry.id}] [withheld — suspected injection "
                    f"pattern(s): {', '.join(findings)}]"
                )
                continue
            findings = screen_findings(entry.when or "")
            tags = _tags(entry, archived, successor)
            if entry.when and not findings:
                tags.insert(1, f"when: {entry.when}")
            if entry.always:
                tags.append("always applies")
            suffix = f"  ({', '.join(tags)})" if tags else ""
            lines.append(f"- [{entry.id}] {entry.text}{suffix}")

    skill_hits: list[tuple[int, Any, bool]] = []
    try:
        # Live skills first, then the ones curation archived — archiving is
        # bounded storage, not deletion, so an archived skill stays findable.
        for s, archived in (
            *((s, False) for s in store.list()),
            *((s, True) for s in store.archived_skills()),
        ):
            weight = score(f"{s.name} {s.description}")
            if weight:
                skill_hits.append((weight, s, archived))
    except Exception:
        skill_hits = []
    if skill_hits:
        lines.append("Saved skills (usable in code mode via `from skills import <name>`):")
        for _, s, archived in sorted(skill_hits, key=lambda hit: (-hit[0], hit[2], hit[1].name))[:limit]:
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
