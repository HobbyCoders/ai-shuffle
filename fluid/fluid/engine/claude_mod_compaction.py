"""Mechanical compaction of Claude Code's handle-bearing message rows.

``DelegatedLoop(claude_mod_compaction=...)`` decides whether a loop mounts it
(on by default); nothing here reads the environment.

Only content matched to this session's stored path may be evicted. CLI-only
attachments and previous summaries are opaque barriers, retained by handle.
No transcript is imported, no stored node is mutated, and no model is called.
"""
from __future__ import annotations

import json
from typing import Any

from fluid.context.compaction import CompactionConfig, Compactor
from fluid.messages import Message, Text, ToolResultBlock, ToolUseBlock
from fluid.store.base import SessionNode

COMPACT_PATH = "/claude-mod/compact"
MAX_COMPACT_BODY_BYTES = 8 * 1024 * 1024
MAX_MESSAGES = 20_000


def project(message: Message) -> dict[str, Any]:
    """The CLI's public row projection, not its opaque thinking/image blocks."""
    row: dict[str, Any] = {
        "role": message.role,
        "text": "".join(b.text for b in message.content if isinstance(b, Text)),
        "toolUses": [
            {"tool_use_id": b.id, "tool": b.name, "input": b.input}
            for b in message.content if isinstance(b, ToolUseBlock)
        ],
    }
    results = [
        {"tool_use_id": b.tool_use_id,
         "text": "".join(c.text for c in b.content if isinstance(c, Text)),
         "isError": b.is_error}
        for b in message.content if isinstance(b, ToolResultBlock)
    ]
    if results:
        row["toolResults"] = results
    return row


def _signature(row: dict[str, Any]) -> str:
    # toolUses carry enriched result/meta fields in the CLI; these are not
    # prompt blocks. Compare only the actual tool call and result projection.
    return json.dumps({
        "role": row["role"], "text": row["text"],
        "toolUses": [{k: use[k] for k in ("tool_use_id", "tool", "input")}
                     for use in row["toolUses"]],
        "toolResults": [{"tool_use_id": r["tool_use_id"], "text": r["text"],
                         "isError": r.get("isError", False)}
                        for r in row.get("toolResults", [])],
    }, sort_keys=True, ensure_ascii=False, allow_nan=False)


def validate_rows(rows: Any, *, handles: bool) -> None:
    if not isinstance(rows, list) or not 0 < len(rows) <= MAX_MESSAGES:
        raise ValueError("invalid message count")
    if not isinstance(rows[0], dict) or rows[0].get("role") != "user":
        raise ValueError("history must start with a user message")
    seen_handles: set[str] = set()
    calls: set[str] = set()
    pending: set[str] = set()
    previous_role: str | None = None
    for row in rows:
        if not isinstance(row, dict) or row.get("role") not in ("user", "assistant"):
            raise ValueError("invalid message role")
        if not isinstance(row.get("text"), str) or not isinstance(row.get("toolUses"), list):
            raise ValueError("invalid message projection")
        if handles or "handle" in row:
            handle = row.get("handle")
            if not isinstance(handle, str) or not handle or handle in seen_handles:
                raise ValueError("missing or duplicate handle")
            seen_handles.add(handle)
        results = row.get("toolResults", [])
        if not isinstance(results, list):
            raise ValueError("invalid tool results")
        if (row["toolUses"] and row["role"] != "assistant") or (results and row["role"] != "user"):
            raise ValueError("tools on wrong role")
        # The CLI can stream a parallel call/result group as multiple rows.
        # A call group must be followed by only its results, with no unrelated
        # user text or new assistant turn inserted before it is complete.
        extending_calls = row["role"] == previous_role == "assistant" and bool(row["toolUses"])
        if pending and not results and not extending_calls:
            raise ValueError("tool call without adjacent results")
        if pending and previous_role == "user" and row["role"] == "assistant":
            raise ValueError("incomplete tool result group")
        for result in results:
            if (not isinstance(result, dict) or not isinstance(result.get("text"), str)
                    or type(result.get("isError", False)) is not bool):
                raise ValueError("invalid tool result")
            tid = result.get("tool_use_id")
            if not isinstance(tid, str) or tid not in pending:
                raise ValueError("orphan tool result")
            pending.remove(tid)
        for use in row["toolUses"]:
            if (not isinstance(use, dict) or not isinstance(use.get("tool"), str)
                    or not isinstance(use.get("input"), dict)):
                raise ValueError("invalid tool use")
            tid = use.get("tool_use_id")
            if not isinstance(tid, str) or not tid or tid in calls:
                raise ValueError("duplicate or missing tool id")
            calls.add(tid)
            pending.add(tid)
        previous_role = row["role"]
        try:
            _signature(row)
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("invalid message content") from exc
    if pending:
        raise ValueError("unfinished tool call")


def _mapped_nodes(rows: list[dict[str, Any]], nodes: list[SessionNode]) -> list[SessionNode | None]:
    """Monotone exact matching; repeated text never maps back to an older turn.

    Claude may stream an assistant message as several nodes. Combine only
    contiguous same-role nodes, retaining a real end-node anchor for recall.
    Result rows spanning different stored nodes stay opaque: a one-node
    placeholder must never promise more than that node can recover.
    """
    signatures: dict[str, list[int]] = {}
    uuids: dict[str, list[int]] = {}
    for i, node in enumerate(nodes):
        signatures.setdefault(_signature(project(node.message)), []).append(i)
        uuid = node.message.provider_meta.get("claude_uuid")
        if isinstance(uuid, str):
            uuids.setdefault(uuid, []).append(i)
    cursor = 0
    mapped: list[SessionNode | None] = []
    # Keep indices monotone even when the stored path contains an older CLI
    # prefix that has already been summarized on the delegated side.
    for row in rows:
        signature = _signature(row)
        candidates = [i for i in signatures.get(signature, []) if i >= cursor]
        match: tuple[int, int, Message] | None = None
        uuid_group = [j for j in uuids.get(row["handle"], []) if j >= cursor]
        preferred = [j for j in candidates if j in uuid_group]
        if len(preferred) == 1 or len(candidates) == 1:
            j = preferred[0] if len(preferred) == 1 else candidates[0]
            match = (j, j, nodes[j].message)
        elif (uuid_group and not row.get("toolResults")
              and uuid_group == list(range(uuid_group[0], uuid_group[-1] + 1))
              and all(nodes[j].message.role == row["role"] for j in uuid_group)):
            message = Message(row["role"], [b for j in uuid_group for b in nodes[j].message.content])
            if _signature(project(message)) == signature:
                match = (uuid_group[0], uuid_group[-1], message)
        # An ambiguous repeat or an unknown/split legacy row stays opaque.
        # Never guess a recall ref, nor scan the whole path per unknown row.
        if match is None:
            mapped.append(None)
        else:
            _, end, message = match
            mapped.append(SessionNode(nodes[end].node_id, nodes[end].parent_id, message))
            cursor = end + 1
    return mapped


def _buildable(message: Message) -> bool:
    # A rebuilt projection cannot preserve opaque image or signed thinking
    # blocks. Such a row may only survive through its original CLI handle.
    return all(
        isinstance(b, (Text, ToolUseBlock))
        or isinstance(b, ToolResultBlock) and all(isinstance(c, Text) for c in b.content)
        for b in message.content
    )


def compact_messages(
    rows: Any, nodes: list[SessionNode], *, config: CompactionConfig | None = None,
) -> list[dict[str, Any]]:
    validate_rows(rows, handles=True)
    anchors = _mapped_nodes(rows, nodes)
    # Native turn digests quote bounded error excerpts. This experiment's
    # stronger contract keeps error results verbatim even at tiers 2 and 3.
    mapped = [None if any(r.get("isError") for r in row.get("toolResults", [])) else node
              for row, node in zip(rows, anchors)]
    out: list[dict[str, Any]] = []
    i = 0
    while i < len(rows):
        if mapped[i] is None:
            out.append(rows[i])
            i += 1
            continue
        end = i
        while end < len(rows) and mapped[end] is not None:
            end += 1
        run = [n for n in mapped[i:end] if n is not None]
        compactor = Compactor(config=config or CompactionConfig())
        # Unknown window: compact_now runs one guarded rung per invocation.
        # Three invocations cover trimming, turn digests, and epoch folding;
        # tier 4 has no caller here, regardless of library configuration.
        for _ in range(3):
            compactor.compact_now(run)
        originals = {id(n.message): rows[i + k] for k, n in enumerate(run)}
        for message in compactor.view(run):
            original = originals.get(id(message))
            # Some pure renders copy an unchanged Message. Preserve the entire
            # original row (including enrichment and handle) in that case too.
            projection = project(message)
            if original is None:
                original = next((rows[i + k] for k, n in enumerate(run)
                                 if n.message == message), None)
            if original is None and not _buildable(message):
                raise ValueError("cannot rebuild opaque content")
            out.append(original if original is not None else projection)
        i = end
    # A digest is a user message; separate it from an adjacent user row without
    # rebuilding a kept handle (which could contain opaque signed state).
    known = {row["handle"]: node for row, node in zip(rows, anchors)}
    def can_build(row: dict[str, Any]) -> bool:
        if "handle" not in row:
            return True
        node = known.get(row["handle"])
        return node is not None and _buildable(node.message)

    alternating: list[dict[str, Any]] = []
    for row in out:
        if alternating and alternating[-1]["role"] == row["role"]:
            left = alternating[-1]
            if left["toolUses"] or row.get("toolResults"):
                if not can_build(left) or not can_build(row):
                    raise ValueError("cannot alternate an opaque tool group")
                # A newly built parallel group, not a kept handle: preserve
                # every result, id, input and error bit without fake messages
                # between calls and their results.
                alternating[-1] = {
                    "role": row["role"],
                    "text": "\n\n".join(t for t in (left["text"], row["text"]) if t),
                    "toolUses": left["toolUses"] + row["toolUses"],
                    "toolResults": left.get("toolResults", []) + row.get("toolResults", []),
                }
                continue
            alternating.append({"role": "assistant" if row["role"] == "user" else "user",
                                "text": "Earlier context continues below.", "toolUses": []})
        alternating.append(row)
    validate_rows(alternating, handles=False)
    if len(json.dumps(alternating)) >= len(json.dumps(rows)):
        raise ValueError("no safe compaction saving")
    return alternating
