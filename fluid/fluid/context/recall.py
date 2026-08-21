"""History recall — the read path back into what compaction evicted.

Compaction (:mod:`fluid.context.compaction`) is recoverable by construction:
the stored tree keeps full fidelity, and every digest/stub/placeholder carries
a node ref. This module is the other half: pure helpers that re-read originals
from a loaded path, used by the ``HistoryExpand`` and ``HistorySearch``
builtins. The engine injects a :class:`HistoryRecall` handle as
``ctx.extra["history_recall"]`` (same seam as ``tool_registry``).

Everything here is engine-free and store-agnostic — the tools stay thin and
the logic unit-tests without a loop.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from fluid.messages import Text, ToolResultBlock, ToolUseBlock
from fluid.store.base import SessionNode, SessionStore

# A ref is one node id or an inclusive range: "node_ab12" / "node_a..node_b".
_REF_RE = re.compile(r"^(node_[0-9a-f]+)(?:\.\.(node_[0-9a-f]+))?$")

DEFAULT_EXPAND_CHARS = 20_000
MAX_EXPAND_CHARS = 50_000
DEFAULT_SEARCH_RESULTS = 8
_SNIPPET_RADIUS = 160
_SEARCH_OUTPUT_CAP = 20_000


@dataclass
class HistoryRecall:
    """Engine-injected handle giving recall tools store access."""

    store: SessionStore
    session_id: str

    async def load_path(self) -> list[SessionNode]:
        return await self.store.load_path(self.session_id)


def parse_ref(ref: str) -> tuple[str, str] | None:
    """``(start, end)`` node ids for a valid ref, else None."""
    m = _REF_RE.match(ref.strip())
    if m is None:
        return None
    start = m.group(1)
    return start, m.group(2) or start


def _render_node(node: SessionNode) -> str:
    """Verbatim plain-text rendering of one stored node."""
    lines: list[str] = []
    role = node.message.role
    for block in node.message.content:
        if isinstance(block, Text):
            lines.append(f"{role} ({node.node_id}): {block.text}")
        elif isinstance(block, ToolUseBlock):
            lines.append(
                f"{role} ({node.node_id}) tool_use {block.name}: {block.input!r}"
            )
        elif isinstance(block, ToolResultBlock):
            status = "error" if block.is_error else "result"
            inner_text = "".join(
                b.text for b in block.content if isinstance(b, Text)
            )
            lines.append(
                f"{role} ({node.node_id}) tool_{status} "
                f"[{block.tool_use_id}]: {inner_text}"
            )
        # Thinking/Reasoning/Image blocks are skipped: reasoning is
        # model-bound state, and base64 payloads are useless as text.
    return "\n".join(lines)


def expand_ref(
    nodes: list[SessionNode],
    ref: str,
    *,
    offset: int = 0,
    max_chars: int = DEFAULT_EXPAND_CHARS,
) -> tuple[str, bool]:
    """Verbatim text for a node or node range on the path.

    Returns ``(text, is_error)``. ``offset``/``max_chars`` slice the rendered
    text so oversize originals (intake-stubbed attachments) are readable in
    windows.
    """
    parsed = parse_ref(ref)
    if parsed is None:
        return (
            f'bad ref "{ref}" — expected "node_<id>" or "node_<a>..node_<b>" '
            "(refs appear in digest and placeholder lines)",
            True,
        )
    start, end = parsed
    index = {n.node_id: i for i, n in enumerate(nodes)}
    if start not in index or end not in index:
        missing = start if start not in index else end
        return (f'ref "{missing}" is not on this session\'s current path', True)
    i, j = index[start], index[end]
    if j < i:
        i, j = j, i
    text = "\n".join(_render_node(n) for n in nodes[i : j + 1])
    max_chars = min(max(1, max_chars), MAX_EXPAND_CHARS)
    offset = max(0, offset)
    window = text[offset : offset + max_chars]
    header = f"[history {ref} — {len(text)} chars total"
    if offset or len(text) > offset + max_chars:
        shown_end = min(len(text), offset + max_chars)
        header += f"; showing {offset}..{shown_end}"
        if shown_end < len(text):
            header += f'; continue with offset={shown_end}'
    header += "]"
    return f"{header}\n{window}", False


@dataclass(frozen=True, kw_only=True)
class Hit:
    node_id: str
    role: str
    snippet: str
    matches: int


def _node_text(node: SessionNode) -> str:
    return _render_node(node)


def search_path(
    nodes: list[SessionNode],
    query: str,
    *,
    max_results: int = DEFAULT_SEARCH_RESULTS,
) -> list[Hit]:
    """Case-insensitive keyword search over every node on the path.

    Scores by matched-term count then total occurrences; one hit per node with
    a snippet around the first match. Searches the STORED nodes — content that
    compaction evicted from the rendered view is still found here.
    """
    terms = [t for t in query.lower().split() if t]
    if not terms:
        return []
    scored: list[tuple[int, int, int, Hit]] = []
    for pos, node in enumerate(nodes):
        text = _node_text(node)
        lowered = text.lower()
        matched = [t for t in terms if t in lowered]
        if not matched:
            continue
        occurrences = sum(lowered.count(t) for t in matched)
        first = min(lowered.find(t) for t in matched)
        lo = max(0, first - _SNIPPET_RADIUS)
        hi = min(len(text), first + _SNIPPET_RADIUS)
        snippet = ("…" if lo else "") + " ".join(
            text[lo:hi].split()
        ) + ("…" if hi < len(text) else "")
        scored.append((
            -len(matched), -occurrences, pos,
            Hit(
                node_id=node.node_id,
                role=node.message.role,
                snippet=snippet,
                matches=occurrences,
            ),
        ))
    scored.sort(key=lambda item: item[:3])
    return [hit for _t, _o, _p, hit in scored[: max(1, max_results)]]


def render_hits(hits: list[Hit], query: str) -> str:
    if not hits:
        return f'no matches for "{query}" anywhere in this session\'s history'
    lines = [
        f'{len(hits)} match(es) for "{query}" — re-read any with '
        'HistoryExpand("<node_id>"):'
    ]
    for hit in hits:
        lines.append(
            f"- {hit.node_id} ({hit.role}, {hit.matches} occurrence(s)): "
            f"{hit.snippet}"
        )
    out = "\n".join(lines)
    if len(out) > _SEARCH_OUTPUT_CAP:
        out = out[:_SEARCH_OUTPUT_CAP] + "\n[output capped — narrow your query]"
    return out
