"""Mechanical compaction of Claude Code's handle-bearing message rows.

``DelegatedLoop(claude_mod_compaction=...)`` decides whether a loop mounts it
(on by default); nothing here reads the environment.

One compactor runs over the whole conversation, the way the native leg's
does, so its recency tail is the conversation's newest work, not the tail of
each fragment between opaque rows. Fluid's own earlier output is read back as
what it is: a digest or epoch row names the stored range it stands for, so a
later compaction folds it again (older folds merge into one coarser epoch)
instead of keeping it as an opaque barrier, and the separator rows it adds
are dropped and made anew. Rows that are not in the session's stored history
(Claude Code's attachments and reminders, an earlier Claude Code summary)
stay verbatim while recent; once older than compacted history they collapse
into one bounded note. Every stored message that leaves the view stays
reachable through HistoryExpand / HistorySearch. No transcript is imported,
no stored node is mutated, and no model is called.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from fluid.context.compaction import (
    EPOCH_STUB,
    SPAN_DIGEST,
    CompactionConfig,
    Compactor,
    Promotion,
)
from fluid.messages import Message, Text, ToolResultBlock, ToolUseBlock
from fluid.store.base import SessionNode

COMPACT_PATH = "/claude-mod/compact"
MAX_COMPACT_BODY_BYTES = 8 * 1024 * 1024
MAX_MESSAGES = 20_000

# The separator between two rows of one role that cannot be merged (Claude
# Code needs alternating roles). Never stored; a later compaction drops it.
SPACER_TEXT = "Earlier context continues below."
# The one row standing for Claude Code rows that are not in the session's
# stored history once they are older than compacted history.
NOTE_HEADER = "[Fluid compaction note — "
NOTE_EXCERPT_CHARS = 1_500
_NOTE_LINE_CHARS = 200
# Used when the host does not know the model's window.
DEFAULT_CONTEXT_WINDOW = 200_000
# How far one compaction aims: the deep target of a known window, at most
# this many tokens of conversation (a 1M window would otherwise keep 150k).
TARGET_CAP_TOKENS = 50_000
# Guarded passes until the target or a fixed point; each pass runs every rung.
MAX_ROUNDS = 8
# An automatic compaction runs at Claude Code's threshold: one that keeps
# more than this share of the conversation would leave it there, so Claude
# Code's own summary runs instead. A manual one only has to save something.
AUTO_MAX_RETAINED = 0.75
MANUAL_MIN_SAVING = 0.02

_DIGEST_HEADER = re.compile(r"\[compacted turn (\S+?)(?:\.\.(\S+?))? — mechanical digest")
_EPOCH_HEADER = re.compile(r"\[epoch (\S+?)\.\.(\S+?) — \d+ older turns folded")
_NOTE_COUNT = re.compile(re.escape(NOTE_HEADER) + r"(\d+) ")


class NothingToCompact(ValueError):
    """Fluid's compaction cannot make this conversation (enough) smaller.

    The bridge answers it as an honest no-op: a manual compaction leaves the
    conversation as it is, an automatic one falls back to Claude Code.
    """


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


def _call_row(row: Any) -> bool:
    return (isinstance(row, dict) and row.get("role") == "assistant"
            and isinstance(row.get("toolUses"), list) and bool(row["toolUses"])
            and not row.get("toolResults"))


def _result_row(row: Any) -> bool:
    return (isinstance(row, dict) and row.get("role") == "user" and row.get("text") == ""
            and row.get("toolUses") == [] and isinstance(row.get("toolResults"), list)
            and bool(row["toolResults"]))


def _ids(items: list[Any], key: str) -> set[Any]:
    return {item.get(key) for item in items if isinstance(item, dict)}


def _tool_groups(rows: list[Any]) -> list[tuple[int, int]]:
    """``[start, end)`` of each tool group: call rows and the result rows that
    answer them, in whatever order the CLI lists them, until no call is open.

    Claude Code runs a tool while it still streams the rest of the message, so
    one parallel group can read call, call, result, call, result, result. No
    model turn can start while a call is open, so every call row seen then is
    part of the same group.
    """
    spans: list[tuple[int, int]] = []
    i = 0
    while i < len(rows):
        if not _call_row(rows[i]):
            i += 1
            continue
        pending: set[Any] = set()
        j = i
        while j < len(rows):
            row = rows[j]
            if _call_row(row):
                pending |= _ids(row["toolUses"], "tool_use_id")
            elif _result_row(row) and _ids(row["toolResults"], "tool_use_id") <= pending:
                pending -= _ids(row["toolResults"], "tool_use_id")
            else:
                break
            j += 1
            if not pending:
                break
        if not pending and j > i + 1:
            spans.append((i, j))
        i = max(j, i + 1)
    return spans


def group_tool_calls(rows: Any) -> Any:
    """Each tool group as one block of calls, then its results, in their order.

    The order the model's API request has. Anything else (no list, a group
    that never closes) is returned as it came, for ``validate_rows`` to judge.
    """
    if not isinstance(rows, list):
        return rows
    grouped = list(rows)
    for start, end in _tool_groups(rows):
        span = rows[start:end]
        grouped[start:end] = [r for r in span if _call_row(r)] + [r for r in span if not _call_row(r)]
    return grouped


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




@dataclass(frozen=True)
class _Anchor:
    """A row's place in the stored path: nodes ``start..end`` (inclusive),
    read as ``node``. ``exact`` when the row is that projection verbatim;
    a row matched by its tool ids or a stateless stub may differ (an earlier
    trim, a result only Claude Code can rebuild)."""

    start: int
    end: int
    node: SessionNode
    exact: bool


def _stub_signature(node: SessionNode, compactor: Compactor) -> str | None:
    """The projection a size stub (oversize user text or tool result, a pure
    function of size) renders for ``node``; None when it renders unchanged."""
    message = compactor.view([node])[0]
    if message is node.message:
        return None
    return _signature(project(message))


def _mapped_nodes(
    rows: list[dict[str, Any]], nodes: list[SessionNode], *,
    avoid: set[int] = frozenset(), compactor: Compactor | None = None,
) -> list[_Anchor | None]:
    """Monotone matching of rows to stored nodes; never a guessed recall ref.

    Claude may stream an assistant message as several nodes. Combine only
    contiguous same-role nodes, retaining a real end-node anchor for recall.

    A row whose handle is the uuid of exactly one stored frame with the same
    projection is that frame, wherever the cursor is: the CLI lists a parallel
    group's calls before their results, while the stream stored them in the
    order they ran (call, result, call, result).

    A repeated projection takes the oldest candidate after the cursor that no
    earlier fold covers (``avoid``): the text is identical, so the ref reads
    the same words either way. A row that differs from every projection
    still anchors by its tool ids (unique in a session) or as the stateless
    stub of a node, never anywhere else.
    """
    signatures: dict[str, list[int]] = {}
    stubs: dict[str, list[int]] = {}
    node_signatures: list[str] = []
    uuids: dict[str, list[int]] = {}
    owners: dict[str, int] = {}
    for i, node in enumerate(nodes):
        node_signature = _signature(project(node.message))
        node_signatures.append(node_signature)
        signatures.setdefault(node_signature, []).append(i)
        if compactor is not None:
            stub = _stub_signature(node, compactor)
            if stub is not None:
                stubs.setdefault(stub, []).append(i)
        uuid = node.message.provider_meta.get("claude_uuid")
        if isinstance(uuid, str):
            uuids.setdefault(uuid, []).append(i)
        for block in node.message.content:
            if isinstance(block, ToolUseBlock):
                owners.setdefault(block.id, i)
            elif isinstance(block, ToolResultBlock):
                owners.setdefault(f"result:{block.tool_use_id}", i)

    def first(candidates: list[int]) -> int | None:
        preferred = [j for j in candidates if j not in avoid]
        return (preferred or candidates or [None])[0]

    cursor = 0
    used: set[int] = set()
    mapped: list[_Anchor | None] = []
    for row in rows:
        signature = _signature(row)
        candidates = [i for i in signatures.get(signature, []) if i >= cursor and i not in used]
        anchor: _Anchor | None = None
        uuid_group = [j for j in uuids.get(row["handle"], []) if j >= cursor]
        preferred = [j for j in candidates if j in uuid_group]
        same_frame = [j for j in uuids.get(row["handle"], [])
                      if j not in used and node_signatures[j] == signature]
        if len(same_frame) == 1:
            j = same_frame[0]
            anchor = _Anchor(j, j, nodes[j], True)
        elif len(preferred) == 1 or candidates:
            j = preferred[0] if len(preferred) == 1 else first(candidates)
            anchor = _Anchor(j, j, nodes[j], True)
        elif (uuid_group and not row.get("toolResults")
              and uuid_group == list(range(uuid_group[0], uuid_group[-1] + 1))
              and all(nodes[j].message.role == row["role"] for j in uuid_group)):
            message = Message(row["role"], [b for j in uuid_group for b in nodes[j].message.content])
            if _signature(project(message)) == signature:
                end = uuid_group[-1]
                anchor = _Anchor(uuid_group[0], end,
                                 SessionNode(nodes[end].node_id, nodes[end].parent_id, message), True)
        if anchor is None:
            stubbed = [i for i in stubs.get(signature, []) if i >= cursor and i not in used]
            j = first(stubbed)
            if j is not None:
                anchor = _Anchor(j, j, nodes[j], False)
        if anchor is None and (row["toolUses"] or row.get("toolResults")):
            keys = ([use.get("tool_use_id") for use in row["toolUses"]]
                    + [f"result:{r.get('tool_use_id')}" for r in row.get("toolResults", [])])
            found = {owners.get(key) for key in keys}
            if len(found) == 1 and None not in found:
                j = found.pop()
                if nodes[j].message.role == row["role"]:
                    anchor = _Anchor(j, j, nodes[j], False)
        mapped.append(anchor)
        if anchor is not None and anchor.exact:
            used.update(range(anchor.start, anchor.end + 1))
            cursor = max(cursor, anchor.end + 1)
    return mapped


def _fluid_parts(
    row: dict[str, Any], index: dict[str, int],
) -> tuple[list[tuple[int, int, str]], str | None] | None:
    """What a row Fluid built stands for: the stored ranges of its digests and
    epochs, and its compaction note; None for any other row.

    A Fluid row is a user text row that opens with a digest, epoch or note
    header; built rows merged into one are separated by a blank line, so a
    header counts at the start or after one. Each named node must be on this
    path, in order, and the ranges must not overlap.
    """
    text = row.get("text")
    if (row.get("role") != "user" or row.get("toolUses") or row.get("toolResults")
            or not isinstance(text, str)):
        return None
    ranges: list[tuple[int, int, str]] = []
    note: str | None = None
    starts = [0] + [m.end() for m in re.finditer(r"\n\n", text)]
    for n, pos in enumerate(starts):
        if text.startswith(NOTE_HEADER, pos):
            if note is None:
                stop = next((p for p in starts[n + 1:] if _DIGEST_HEADER.match(text, p)
                             or _EPOCH_HEADER.match(text, p)), len(text) + 2)
                note = text[pos:stop - 2]
            continue
        match = _DIGEST_HEADER.match(text, pos)
        kind = SPAN_DIGEST
        if match is None:
            match = _EPOCH_HEADER.match(text, pos)
            kind = EPOCH_STUB
        if match is None:
            if pos == 0:
                return None
            continue
        lo = index.get(match.group(1))
        hi = index.get(match.group(2) or match.group(1))
        if lo is None or hi is None or hi < lo or (ranges and lo <= ranges[-1][1]):
            continue
        ranges.append((lo, hi, kind))
    return ranges, note


def _is_spacer(row: dict[str, Any]) -> bool:
    return row.get("text") == SPACER_TEXT and not row.get("toolUses") and not row.get("toolResults")


def _excerpt(row: dict[str, Any]) -> str:
    text = " ".join(row.get("text", "").split())
    tools = [use.get("tool", "") for use in row.get("toolUses", [])]
    if tools:
        text = f"{text} [calls: {', '.join(tools)}]".strip()
    if row.get("toolResults"):
        text = f"{text} [{len(row['toolResults'])} tool result(s)]".strip()
    if len(text) > _NOTE_LINE_CHARS:
        text = text[:_NOTE_LINE_CHARS] + "…"
    return f"- {row.get('role')}: {text or '(no text)'}"


def _note_row(dropped: list[dict[str, Any]], notes: list[str]) -> dict[str, Any] | None:
    """One bounded row for every dropped row not in the stored history,
    folding in the notes earlier compactions left."""
    count = len(dropped)
    lines = [_excerpt(row) for row in reversed(dropped)]
    for note in reversed(notes):
        match = _NOTE_COUNT.match(note)
        count += int(match.group(1)) if match else 0
        lines.extend(line for line in note.split("\n")[1:] if line.startswith("- "))
    if not count:
        return None
    kept: list[str] = []
    used = 0
    for line in lines:
        if used + len(line) > NOTE_EXCERPT_CHARS:
            break
        kept.append(line)
        used += len(line) + 1
    header = (
        f"{NOTE_HEADER}{count} older rows of Claude Code's own context (reminders, "
        "attachments or an earlier Claude Code summary) are not in this session's stored "
        "history and were dropped. The conversation itself stays searchable with "
        "HistorySearch. Newest first:]"
    )
    return {"role": "user", "text": "\n".join([header, *kept]), "toolUses": []}


def _group_spans(rows: list[dict[str, Any]]) -> list[tuple[int, int]]:
    """``[start, end)`` of each run that opens a tool call and closes it,
    counting a result row that also carries text (``_tool_groups`` does not)."""
    spans: list[tuple[int, int]] = []
    pending: set[Any] = set()
    start = 0
    for i, row in enumerate(rows):
        if not pending:
            start = i
        pending |= _ids(row.get("toolUses", []), "tool_use_id")
        pending -= _ids(row.get("toolResults", []), "tool_use_id")
        if not pending and i > start:
            spans.append((start, i + 1))
    return spans


def _built(message: Message, node_id: str) -> dict[str, Any]:
    """A row Claude Code builds from text and tool blocks. Opaque blocks
    (signed thinking, images) are never rebuilt; a row left with nothing
    says where its original is."""
    row = project(message)
    if not row["text"] and not row["toolUses"] and not row.get("toolResults"):
        row["text"] = f'[earlier content not shown here; HistoryExpand("{node_id}") re-reads it]'
    return row


def _alternate(out: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Roles alternate outside an open tool group, as the mod demands.

    Two built rows of one role merge: text with text, a parallel group's
    calls with calls and its results with results, so a rebuilt group is one
    call message and one result message with every id, input and error bit.
    A kept row (it has a handle) is never rebuilt: it continues an open
    group as the engine's own row, or gets a separator row.
    """
    final: list[dict[str, Any]] = []
    pending: set[Any] = set()
    for row in out:
        if final and final[-1]["role"] == row["role"]:
            left = final[-1]
            built = "handle" not in left and "handle" not in row
            text_only = all(not r["toolUses"] and not r.get("toolResults") for r in (left, row))
            calls = left["toolUses"] and row["toolUses"]
            results = (left.get("toolResults") and row.get("toolResults")
                       and not left["toolUses"] and not row["toolUses"])
            if built and ((text_only and not pending) or calls or results):
                merged = {
                    "role": row["role"],
                    "text": "\n\n".join(t for t in (left["text"], row["text"]) if t),
                    "toolUses": left["toolUses"] + row["toolUses"],
                }
                if left.get("toolResults") or row.get("toolResults"):
                    merged["toolResults"] = left.get("toolResults", []) + row.get("toolResults", [])
                final[-1] = merged
            elif pending:
                final.append(row)  # the engine's own rows of an open group
            else:
                final.append({"role": "assistant" if row["role"] == "user" else "user",
                              "text": SPACER_TEXT, "toolUses": []})
                final.append(row)
        else:
            if not final and row["role"] != "user":
                final.append({"role": "user", "text": SPACER_TEXT, "toolUses": []})
            final.append(row)
        pending |= _ids(row["toolUses"], "tool_use_id")
        pending -= _ids(row.get("toolResults", []), "tool_use_id")
    return final


def compact_messages(
    rows: Any, nodes: list[SessionNode], *, config: CompactionConfig | None = None,
    context_window: int | None = None, trigger: str = "manual",
) -> list[dict[str, Any]]:
    """Claude Code's rows, compacted mechanically against the stored path.

    Raises :class:`NothingToCompact` when the result would not be smaller
    (for ``trigger="auto"``: small enough to leave the threshold), and
    ``ValueError`` for rows that do not validate.
    """
    rows = group_tool_calls(rows)
    validate_rows(rows, handles=True)
    config = config or CompactionConfig()
    window = context_window or DEFAULT_CONTEXT_WINDOW
    index = {n.node_id: i for i, n in enumerate(nodes)}
    parts = [_fluid_parts(row, index) for row in rows]
    covered = {k for p in parts if p for lo, hi, _ in p[0] for k in range(lo, hi + 1)}
    anchors = _mapped_nodes(rows, nodes, avoid=covered, compactor=Compactor(config=config))
    for i, anchor in enumerate(anchors):
        if anchor is not None:
            parts[i] = None  # a stored message, whatever its text opens with
    # A tool group is in the stored view whole or not at all: a kept call
    # or result must never lose its other half.
    for start, end in _group_spans(rows):
        if any(anchor is None for anchor in anchors[start:end]):
            anchors[start:end] = [None] * (end - start)

    claimed = {k for a in anchors if a is not None for k in range(a.start, a.end + 1)}
    view: list[SessionNode] = []
    at: dict[int, int] = {}  # stored index -> position in view
    rows_of: dict[int, list[dict[str, Any]]] = {}
    seeds: list[tuple[int, int, str]] = []
    trims: list[tuple[str, str]] = []
    unknown: list[tuple[int, dict[str, Any]]] = []  # (view position it precedes, row)
    notes: list[str] = []

    # Rows ahead of anything stored (an earlier Claude Code summary, say)
    # stand for the stored history before them: that history is the
    # conversation's oldest epoch, so their originals keep a recall ref.
    first_known = next((i for i, (a, p) in enumerate(zip(anchors, parts)) if a or (p and p[0])),
                       len(rows))
    if any(not _is_spacer(r) and parts[i] is None for i, r in enumerate(rows[:first_known])):
        lowest = min([a.start for a in anchors if a is not None]
                     + [lo for p in parts if p for lo, _, _ in p[0]], default=0)
        prefix = [k for k in range(lowest) if k not in claimed]
        if prefix:
            for k in prefix:
                at[k] = len(view)
                view.append(nodes[k])
            seeds.append((0, len(view) - 1, EPOCH_STUB))

    for row, anchor, fluid in zip(rows, anchors, parts):
        if anchor is not None:
            placed = next((at[k] for k in range(anchor.start, anchor.end + 1) if k in at), None)
            if placed is None:
                placed = len(view)
                view.append(anchor.node)
                rows_of[placed] = []
            for k in range(anchor.start, anchor.end + 1):
                at.setdefault(k, placed)
            rows_of.setdefault(placed, []).append(row)
            if not anchor.exact:
                stored = {b.tool_use_id: b for b in anchor.node.message.content
                          if isinstance(b, ToolResultBlock)}
                for result in row.get("toolResults", []):
                    block = stored.get(result["tool_use_id"])
                    if block is not None and not block.is_error and result["text"] != "".join(
                            c.text for c in block.content if isinstance(c, Text)):
                        trims.append((anchor.node.node_id, block.tool_use_id))
        elif fluid is not None:
            ranges, note = fluid
            if note is not None:
                notes.append(note)
            for lo, hi, kind in ranges:
                members = [k for k in range(lo, hi + 1) if k not in claimed and k not in at]
                if not members:
                    continue
                first = len(view)
                for k in members:
                    at[k] = len(view)
                    view.append(nodes[k])
                seeds.append((first, len(view) - 1, kind))
        elif _is_spacer(row):
            continue
        else:
            unknown.append((len(view), row))
    if not view:
        raise NothingToCompact("no safe compaction saving")

    compactor = Compactor(config=config)
    compactor.rehydrate(
        [Promotion(tier=1, trimmed=tuple(dict.fromkeys(trims)))]
        + [Promotion(tier=3 if kind == EPOCH_STUB else 2,
                     spans=((view[a].node_id, view[b].node_id, kind),))
           for a, b, kind in seeds]
    )
    tail = compactor._recency_tail_start(view, window, 1.0)
    tail = len(view) if tail is None else tail
    ratio = config.deep_target_window_ratio or 0.15
    target = min(int(window * ratio), TARGET_CAP_TOKENS)
    # Every guarded rung per pass, then the old folds merge into one: until
    # the target is met or a pass changes nothing.
    for _ in range(MAX_ROUNDS):
        promoted = compactor.escalate(
            view, context_window=window, guarded_only=True, stop_tokens=target,
            decision_reason="manual",
        )
        merged = compactor.merge_epochs(view, before=tail)
        if compactor.estimate_view_tokens(view) <= target or (not promoted and merged is None):
            break

    segments = compactor.view_segments(view)

    def compacted(first: int, last: int, message: Message) -> bool:
        return first != last or (message is not view[first].message and first < tail)

    last_compacted = max((last for first, last, m in segments if compacted(first, last, m)),
                         default=-1)
    calls = {use["tool_use_id"] for row in rows for use in row["toolUses"]}
    out: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    pending = list(unknown)

    def take(upto: int) -> None:
        while pending and pending[0][0] <= upto:
            position, row = pending.pop(0)
            (dropped if position <= last_compacted else out).append(row)

    for first, last, message in segments:
        take(first)
        if pending and pending[0][0] <= last:  # inside a fold
            while pending and pending[0][0] <= last:
                dropped.append(pending.pop(0)[1])
        if compacted(first, last, message) or not rows_of.get(first):
            row = _built(message, view[last].node_id)
            if any(use["tool_use_id"] not in calls for use in row["toolUses"]):
                raise ValueError("cannot rebuild a call Claude Code does not have")
            out.append(row)
        else:
            out.extend(rows_of[first])
    take(len(view))
    note = _note_row(dropped, notes)
    final = _alternate(([note] if note else []) + out)
    validate_rows(final, handles=False)
    before, after = len(json.dumps(rows)), len(json.dumps(final))
    limit = before * (AUTO_MAX_RETAINED if trigger == "auto" else 1 - MANUAL_MIN_SAVING)
    if after > limit:
        raise NothingToCompact("no safe compaction saving")
    return final
