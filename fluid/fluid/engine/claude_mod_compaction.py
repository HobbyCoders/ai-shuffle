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
    is_turn_start,
    is_wake_text,
    strip_system_reminders,
)
from fluid.context.trim import block_chars
from fluid.messages import Image, Message, Text, ToolResultBlock, ToolUseBlock
from fluid.store.base import SessionNode, new_id

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
# History older than this many targets' worth of content before the recent
# tail folds straight into one epoch (see ``_deep_history_end``).
DEEP_FOLD_FACTOR = 4
# An automatic compaction runs at Claude Code's threshold: one that keeps
# more than this share of the conversation would leave it there, so Claude
# Code's own summary runs instead. A manual one only has to save something.
AUTO_MAX_RETAINED = 0.75
MANUAL_MIN_SAVING = 0.02

# A row Fluid builds for stored content it will not rebuild (a call Claude
# Code holds with blocks Fluid cannot reproduce, a message with nothing it
# can show): it names the nodes, and the next compaction folds them.
STAND_IN_HEADER = "[earlier content not shown here"
_REF = re.compile(r'HistoryExpand\("(node_[0-9a-f]+(?:\.\.node_[0-9a-f]+)?)"')
# How Fluid's own placeholders for a tool result open: a result Claude Code
# shows differently from the stored one, and not like this, carries blocks
# only Claude Code can show (a tool_reference, an image).
_PLACEHOLDER_OPENINGS = ("[old tool result trimmed", "[large tool result", "[image consumed",
                         "[image attachment consumed")
# Tools whose results carry blocks only Claude Code holds.
_OPAQUE_RESULT_TOOLS = frozenset({"ToolSearch"})

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
    """What a row Fluid built stands for: the stored ranges it covers, and its
    compaction note; None for any other row.

    A Fluid row opens with a digest, epoch or note header (a user text row)
    or with the stand-in header (either role). Rows Fluid merged are joined by
    a blank line, so a header counts at the start or after one. Every
    ``HistoryExpand`` ref the row carries outside those ranges is claimed too
    (a stand-in's nodes, or anything an older build merged in), so no stored
    message a Fluid row names can fall out of the view.
    """
    text = row.get("text")
    if row.get("toolUses") or row.get("toolResults") or not isinstance(text, str):
        return None
    stand_in = text.startswith(STAND_IN_HEADER)
    if not stand_in and (row.get("role") != "user" or not (
            text.startswith(NOTE_HEADER) or _DIGEST_HEADER.match(text)
            or _EPOCH_HEADER.match(text))):
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
            continue
        lo = index.get(match.group(1))
        hi = index.get(match.group(2) or match.group(1))
        if lo is None or hi is None or hi < lo or (ranges and lo <= ranges[-1][1]):
            continue
        ranges.append((lo, hi, kind))
    inside = {k for lo, hi, _ in ranges for k in range(lo, hi + 1)}
    for ref in _REF.findall(text):
        first, _, last = ref.partition("..")
        lo, hi = index.get(first), index.get(last or first)
        if lo is None or hi is None or hi < lo or any(k in inside for k in range(lo, hi + 1)):
            continue
        ranges.append((lo, hi, SPAN_DIGEST))
        inside.update(range(lo, hi + 1))
    ranges.sort()
    return ranges, note


def _is_fluid_text(row: dict[str, Any]) -> bool:
    """A digest, epoch or note row Fluid built (never a stored message)."""
    text = row.get("text", "")
    return (row.get("role") == "user" and "handle" not in row and not row.get("toolUses")
            and not row.get("toolResults")
            and (text.startswith(NOTE_HEADER) or bool(_DIGEST_HEADER.match(text))
                 or bool(_EPOCH_HEADER.match(text))))


def _is_spacer(row: dict[str, Any]) -> bool:
    return row.get("text") == SPACER_TEXT and not row.get("toolUses") and not row.get("toolResults")


def _archive_text(row: dict[str, Any]) -> str:
    """Everything a dropped row showed the model, as plain text."""
    parts = [row.get("text", "")]
    for use in row.get("toolUses", []):
        parts.append(f"[tool call {use.get('tool', '')} {use.get('tool_use_id', '')}: "
                     f"{json.dumps(use.get('input', {}), ensure_ascii=False)}]")
    for result in row.get("toolResults", []):
        status = "error" if result.get("isError") else "result"
        parts.append(f"[tool {status} {result.get('tool_use_id', '')}]: {result.get('text', '')}")
    return "\n".join(p for p in parts if p)


def _excerpt(row: dict[str, Any], ref: str | None) -> str | None:
    """One note line for a dropped row; None for a row with nothing visible
    (signed thinking alone, an image): it is counted, not quoted."""
    text = " ".join(row.get("text", "").split())
    tools = [use.get("tool", "") for use in row.get("toolUses", [])]
    if tools:
        text = f"{text} [calls: {', '.join(tools)}]".strip()
    if row.get("toolResults"):
        text = f"{text} [{len(row['toolResults'])} tool result(s)]".strip()
    if not text:
        return None
    if len(text) > _NOTE_LINE_CHARS:
        text = text[:_NOTE_LINE_CHARS] + "…"
    where = f' HistoryExpand("{ref}")' if ref else ""
    return f"- {row.get('role')}{where}: {text}"


def _note_row(
    dropped: list[dict[str, Any]], notes: list[str], archive: list[dict[str, str]] | None,
) -> dict[str, Any] | None:
    """One bounded row for every dropped row not in the stored history,
    folding in the notes earlier compactions left.

    With ``archive`` each dropped row's whole text goes there under a fresh
    node id, and its note line cites that id: the caller persists the archive
    before the answer is used, so ``HistoryExpand`` reads it back and
    ``HistorySearch`` finds it. Without one, the note says plainly that its
    excerpts are all that is left.
    """
    count = len(dropped)
    refs = [new_id("node") if archive is not None else None for _ in dropped]
    if archive is not None:
        archive.extend({"node_id": ref, "role": row.get("role", "user"), "text": _archive_text(row)}
                       for ref, row in zip(refs, dropped))
    lines = [line for row, ref in zip(reversed(dropped), reversed(refs))
             if (line := _excerpt(row, ref)) is not None]
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
    where = (
        "were archived with the session: HistoryExpand a ref below reads one whole, "
        "HistorySearch finds any of them"
        if archive is not None else
        "were dropped and are not recoverable: the excerpts below are all that is left of them"
    )
    header = (
        f"{NOTE_HEADER}{count} older rows of Claude Code's own context (reminders, attachments, "
        f"skill or command text, or an earlier Claude Code summary) were not in this session's "
        f"stored history; they {where}. Newest first:]"
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
        row["text"] = f'{STAND_IN_HEADER}; HistoryExpand("{node_id}") re-reads it]'
    return row


def _checked(row: dict[str, Any], calls: set[Any]) -> dict[str, Any]:
    if any(use["tool_use_id"] not in calls for use in row["toolUses"]):
        raise ValueError("cannot rebuild a call Claude Code does not have")
    return row


def _stand_in(role: str, node_ids: list[str]) -> dict[str, Any]:
    """Stored messages left out whole rather than rebuilt, named for recall."""
    refs = ", ".join(f'HistoryExpand("{node_id}")' for node_id in node_ids)
    return {"role": role, "text": f"{STAND_IN_HEADER} (tool calls and results only Claude Code "
                                  f"could show whole); {refs} re-reads them]", "toolUses": []}


def _alternate(out: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Roles alternate outside an open tool group, as the mod demands.

    Two built rows of one role merge: a parallel group's calls with calls and
    its results with results, so a rebuilt group is one call message and one
    result message with every id, input and error bit, and Fluid's digest,
    epoch and note rows with each other. Any other text is never merged into
    another row (the next compaction must still recognise each stored
    message): it gets a separator row, as a kept row (it has a handle) does.
    """
    final: list[dict[str, Any]] = []
    pending: set[Any] = set()
    for row in out:
        if final and final[-1]["role"] == row["role"]:
            left = final[-1]
            built = "handle" not in left and "handle" not in row
            text_only = _is_fluid_text(left) and _is_fluid_text(row)
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


def _tool_owners(nodes: list[SessionNode]) -> dict[str, int]:
    """Stored index of each tool call (``id``) and result (``result:id``)."""
    owners: dict[str, int] = {}
    for i, node in enumerate(nodes):
        for block in node.message.content:
            if isinstance(block, ToolUseBlock):
                owners.setdefault(block.id, i)
            elif isinstance(block, ToolResultBlock):
                owners.setdefault(f"result:{block.tool_use_id}", i)
    return owners


def _unit_members(
    group: list[dict[str, Any]], owners: dict[str, int], nodes: list[SessionNode],
) -> list[int] | None:
    """The stored nodes, in stored order, that hold exactly a tool group's
    calls and results; None unless every row is a tool row of one of them."""
    members: set[int] = set()
    for row in group:
        keys = ([use.get("tool_use_id") for use in row["toolUses"]]
                + [f"result:{r.get('tool_use_id')}" for r in row.get("toolResults", [])])
        if not keys or any(key not in owners for key in keys):
            return None
        found = {owners[key] for key in keys}
        if any(nodes[j].message.role != row["role"] for j in found):
            return None
        members |= found
    ids = {use.get("tool_use_id") for row in group for use in row["toolUses"]}
    for j in members:
        for block in nodes[j].message.content:
            if isinstance(block, (ToolUseBlock, ToolResultBlock)) and (
                    block.id if isinstance(block, ToolUseBlock) else block.tool_use_id) not in ids:
                return None  # the node also holds something outside the group
    return sorted(members)


def _deep_history_end(view: list[SessionNode], tail: int, keep_chars: int) -> int | None:
    """Index of the last node of the history older than ``keep_chars`` of raw
    content before the tail, ending where a turn starts; None when there is
    none worth folding."""
    used = 0
    for i in range(tail - 1, 0, -1):
        used += sum(block_chars(b) for b in view[i].message.content)
        if used > keep_chars:
            for j in range(i, 0, -1):
                if is_turn_start(view[j]):
                    return j - 1
            return None
    return None


def _opaque_nodes(nodes: list[SessionNode], owners: dict[str, int]) -> set[int]:
    """Stored nodes whose rows carry blocks Fluid cannot rebuild: an image, or
    the result of a tool whose result only Claude Code holds whole (a
    ToolSearch result's tool_reference blocks load the deferred tools later
    calls use). Such a row is kept whole or left out whole, never rebuilt."""
    names = {block.id: block.name for node in nodes for block in node.message.content
             if isinstance(block, ToolUseBlock)}
    opaque: set[int] = set()
    for i, node in enumerate(nodes):
        for block in node.message.content:
            if isinstance(block, Image) or (isinstance(block, ToolResultBlock) and (
                    names.get(block.tool_use_id) in _OPAQUE_RESULT_TOOLS
                    or any(isinstance(inner, Image) for inner in block.content or ()))):
                opaque.add(i)
    return opaque


# Turns the host starts on the owner's behalf: they continue the request
# before them and are not requests of their own (AI Shuffle's usage-limit
# resume, idle and context-limit continuations, a hand-off; a bare /compact).
HOST_TURN_PREFIXES = ("[auto-resume]", "[auto-continue]", "[hand-off]")
_COMPACT_COMMAND = re.compile(r"/compact(?:\s|$)")


def _is_request(node: SessionNode) -> bool:
    """A turn a person started: not a wake, a peer's message, a task
    notification or a turn the host started for them."""
    if not is_turn_start(node):
        return False
    text = strip_system_reminders(
        "".join(b.text for b in node.message.content if isinstance(b, Text))).strip()
    return not (is_wake_text(text) or text.startswith(HOST_TURN_PREFIXES)
                or _COMPACT_COMMAND.match(text))


def _pinned_requests(view: list[SessionNode], unknown: list[tuple[int, dict[str, Any]]]) -> tuple[
        list[int], set[int]]:
    """The view positions of the requests a person made, and those whose
    Claude Code rows stay verbatim: the newest request, and the newest
    earlier one that has any (a short follow-up must not push the skill or
    command it follows out of context). Two at most, so it stays bounded."""
    requests = [p for p, node in enumerate(view) if _is_request(node)]
    if not requests:
        return requests, set()
    pinned = {requests[-1]}
    owners = {_owner(requests, position) for position, _ in unknown}
    earlier = [r for r in requests[:-1] if r in owners]
    if earlier:
        pinned.add(earlier[-1])
    return requests, pinned


def _owner(requests: list[int], position: int) -> int | None:
    """The request a Claude Code row belongs to: the newest one at or before
    the view position it precedes (rows a turn's host-started continuations
    add belong to the request they continue)."""
    owner = None
    for r in requests:
        if r > position:
            break
        owner = r
    return owner


def compact_messages(
    rows: Any, nodes: list[SessionNode], *, config: CompactionConfig | None = None,
    context_window: int | None = None, trigger: str = "manual",
    archive: list[dict[str, str]] | None = None,
) -> list[dict[str, Any]]:
    """Claude Code's rows, compacted mechanically against the stored path.

    Rows only Claude Code holds (skill and command text, attachments, hook
    context) stay verbatim for the newest request a person made and for the
    newest earlier request that has any (:func:`_pinned_requests`); older
    ones leave through the compaction note, their whole text appended
    to ``archive`` (see :func:`_note_row`), which the caller must persist.

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
    # or result must never lose its other half. A group some rows of which
    # match no single node (a parallel group Fluid rebuilt as one call row and
    # one result row, a result only Claude Code can show) is still stored when
    # every tool id it carries is: it enters the view as a unit, kept as its
    # own rows while untouched and rebuilt or folded from the stored nodes
    # once it is old, so it keeps a recall ref.
    owners = _tool_owners(nodes)
    opaque = _opaque_nodes(nodes, owners)
    loose = []
    for start, end in _group_spans(rows):
        if any(anchor is None for anchor in anchors[start:end]):
            anchors[start:end] = [None] * (end - start)
            loose.append((start, end))
    taken = {k for a in anchors if a is not None for k in range(a.start, a.end + 1)}
    units: dict[int, tuple[int, list[int]]] = {}  # first row -> (end row, stored indexes)
    for start, end in loose:
        members = _unit_members(rows[start:end], owners, nodes)
        if members is not None and not taken & set(members):
            units[start] = (end, members)
            taken.update(members)

    claimed = taken
    view: list[SessionNode] = []
    at: dict[int, int] = {}  # stored index -> position in view
    rows_of: dict[int, list[dict[str, Any]]] = {}
    seeds: list[tuple[int, int, str]] = []
    trims: list[tuple[str, str]] = []
    unknown: list[tuple[int, dict[str, Any]]] = []  # (view position it precedes, row)
    notes: list[str] = []
    unit_of: dict[int, list[int]] = {}  # view position -> the unit's positions
    skip_to = -1

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

    for r, (row, anchor, fluid) in enumerate(zip(rows, anchors, parts)):
        if r < skip_to:
            continue
        if r in units:
            skip_to, members = units[r]
            positions = list(range(len(view), len(view) + len(members)))
            for k, position in zip(members, positions):
                at[k] = position
                view.append(nodes[k])
                unit_of[position] = positions
            rows_of[positions[0]] = rows[r:skip_to]
            continue
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
                    if block is None or result["text"] == "".join(
                            c.text for c in block.content if isinstance(c, Text)):
                        continue
                    if result["text"].startswith(_PLACEHOLDER_OPENINGS) and not block.is_error:
                        trims.append((anchor.node.node_id, block.tool_use_id))  # Fluid's trim
                    else:
                        # Claude Code shows it differently (a tool_reference):
                        # its row is the only whole copy.
                        opaque.update(range(anchor.start, anchor.end + 1))
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
    # History far older than the target would be digested span by span and
    # then folded anyway; fold it at once (the per-span pass re-renders the
    # whole path for every span, which a long first compaction cannot afford).
    deep = _deep_history_end(view, min(tail, len(view)), target * 4 * DEEP_FOLD_FACTOR)
    if deep is not None:
        compactor.rehydrate([Promotion(tier=3, spans=(
            (view[0].node_id, view[deep].node_id, EPOCH_STUB),))])
        compactor._settle_replay(view)
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

    def folded(first: int, last: int, message: Message) -> bool:
        """A digest, epoch or summary stands in the nodes' place (not a
        rebuild of one node with some blocks changed)."""
        return first != last or (message is not view[first].message
                                 and _is_fluid_text(project(message)))

    stored_at = {position: k for k, position in at.items()}
    hard = {p for p in range(len(view)) if stored_at.get(p) in opaque}
    in_fold = {k for first, last, m in segments if folded(first, last, m)
               for k in range(first, last + 1)}
    # A unit stays its own rows only while none of its nodes is compacted.
    touched = {k for first, last, m in segments if compacted(first, last, m)
               for k in range(first, last + 1)}
    rebuilt = {p for p, positions in unit_of.items() if touched & set(positions)}

    last_compacted = max((last for first, last, m in segments if compacted(first, last, m)),
                         default=-1)
    # Claude Code's own rows of the newest request, and of the newest earlier
    # request that has any, are the work in hand (a skill being followed, a
    # command's expansion): never dropped.
    requests, pinned = _pinned_requests(view, unknown)

    def kept(position: int) -> bool:
        return _owner(requests, position) in pinned

    calls = {use["tool_use_id"] for row in rows for use in row["toolUses"]}
    out: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    pending = list(unknown)
    emitted_units: set[int] = set()

    def take(upto: int) -> None:
        while pending and pending[0][0] <= upto:
            position, row = pending.pop(0)
            old = position <= last_compacted and not kept(position)
            (dropped if old else out).append(row)

    for first, last, message in segments:
        take(first)
        while pending and pending[0][0] <= last:  # inside a fold
            position, row = pending.pop(0)
            (out if kept(position) else dropped).append(row)
        if first == last and first in unit_of:
            positions = unit_of[first]
            head = positions[0]
            if head in emitted_units:
                continue
            if first not in rebuilt:
                out.extend(rows_of.get(head, []))  # the unit's own rows, once
                emitted_units.add(head)
            elif any(p in hard for p in positions):
                # Never rebuilt: whole as Claude Code holds it, or (some of it
                # folded) the rest named for recall.
                emitted_units.add(head)
                if not in_fold & set(positions):
                    out.extend(rows_of.get(head, []))
                else:
                    rest = [view[p].node_id for p in positions if p not in in_fold]
                    out.append(_stand_in("user", rest))
            else:
                out.append(_checked(_built(message, view[last].node_id), calls))
        elif folded(first, last, message):
            out.append(_built(message, view[last].node_id))
        elif compacted(first, last, message) and first in hard:
            out.extend(rows_of.get(first) or [_stand_in(view[first].message.role,
                                                        [view[first].node_id])])
        elif compacted(first, last, message) or not rows_of.get(first):
            out.append(_checked(_built(message, view[last].node_id), calls))
        else:
            out.extend(rows_of[first])
    take(len(view))
    note = _note_row(dropped, notes, archive)
    final = _alternate(([note] if note else []) + out)
    validate_rows(final, handles=False)
    # Content only: a handle is how Claude Code names a row, not context.
    before, after = (len(json.dumps([{k: v for k, v in r.items() if k != "handle"} for r in side]))
                     for side in (rows, final))
    limit = before * (AUTO_MAX_RETAINED if trigger == "auto" else 1 - MANUAL_MIN_SAVING)
    if after > limit:
        raise NothingToCompact("no safe compaction saving")
    return final
