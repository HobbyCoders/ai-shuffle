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

import asyncio
import re
from dataclasses import dataclass, field

from fluid.context.trim import image_source_path
from fluid.events.model import ContextArchived
from fluid.messages import Image, Message, Text, ToolResultBlock, ToolUseBlock
from fluid.store.base import SessionNode, SessionStore, iter_events_of_class

# A ref is one node id or an inclusive range: "node_ab12" / "node_a..node_b".
_REF_RE = re.compile(r"^(node_[0-9a-f]+)(?:\.\.(node_[0-9a-f]+))?$")
_REMINDER_RE = re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL)

DEFAULT_EXPAND_CHARS = 20_000
MAX_EXPAND_CHARS = 50_000
DEFAULT_SEARCH_RESULTS = 8
_SNIPPET_RADIUS = 160
_SEARCH_OUTPUT_CAP = 20_000


@dataclass
class HistoryRecall:
    """Engine-injected handle giving recall tools store access.

    The engine keeps one per session, so archived text is decoded once: each
    later read asks the store only for ``ContextArchived`` events newer than
    the last one it saw (#727), instead of decoding every archived entry on
    every search.
    """

    store: SessionStore
    session_id: str
    _archived: list[SessionNode] = field(default_factory=list, init=False, repr=False)
    _archived_keys: set[tuple[str, str]] = field(default_factory=set, init=False, repr=False)
    _archived_ids: set[str] = field(default_factory=set, init=False, repr=False)
    _aliases: dict[str, SessionNode] = field(default_factory=dict, init=False, repr=False)
    _archived_seq: int = field(default=0, init=False, repr=False)
    _archived_lock: asyncio.Lock | None = field(default=None, init=False, repr=False)

    async def load_path(self) -> list[SessionNode]:
        return await self.store.load_path(self.session_id)

    async def archived(self) -> list[SessionNode]:
        """Text a compaction took out of context that the path does not hold
        (:class:`~fluid.events.model.ContextArchived`), one detached node per
        entry, oldest first. Empty for a session that never archived any.

        The same text archived twice (before archive ids were content-derived,
        a compaction Claude Code never applied left it to be archived again)
        is listed once, under its first id."""
        if self._archived_lock is None:
            self._archived_lock = asyncio.Lock()
        async with self._archived_lock:
            fresh: list[ContextArchived] = []
            sequenced = True
            async for event in iter_events_of_class(
                self.store, self.session_id, self._archived_seq, (ContextArchived,),
            ):
                fresh.append(event)
                sequenced = sequenced and event.seq > self._archived_seq
            if not sequenced:
                # A store whose events carry no sequence (tests, an in-memory
                # store fed directly) cannot be read incrementally: start over.
                self._archived, self._archived_keys, self._archived_ids = [], set(), set()
                self._aliases = {}
            for event in fresh:
                self._add_archived(event)
                if sequenced:
                    self._archived_seq = max(self._archived_seq, event.seq)
            return list(self._archived)

    async def archived_ids(self) -> set[str]:
        """Every id an archived entry is cited by."""
        await self.archived()
        return set(self._archived_ids)

    def archived_alias(self, node_id: str) -> SessionNode | None:
        """The entry a duplicate id (left out of :meth:`archived`) names, so a
        note citing it still expands. Call after :meth:`archived`."""
        return self._aliases.get(node_id)

    def _add_archived(self, event: ContextArchived) -> None:
        for entry in event.entries:
            node_id, role, text = (entry.get(k) for k in ("node_id", "role", "text"))
            if not (isinstance(node_id, str) and role in ("user", "assistant") and isinstance(text, str)):
                continue
            if node_id in self._archived_ids:
                continue
            self._archived_ids.add(node_id)
            node = SessionNode(node_id, None, Message(role, [Text(text)]))
            if (role, text) in self._archived_keys:
                self._aliases[node_id] = node
                continue
            self._archived_keys.add((role, text))
            self._archived.append(node)


async def recall_nodes(recall: HistoryRecall, ref: str | None = None) -> list[SessionNode]:
    """The path a recall tool reads: the stored path, and archived text after
    it. With ``ref``, archived text only when the ref is not on the path, so
    an ordinary expand costs no extra read."""
    nodes = await recall.load_path()
    archived = getattr(recall, "archived", None)
    if not callable(archived):
        return nodes
    parsed = None
    if ref is not None:
        parsed = parse_ref(ref)
        ids = {n.node_id for n in nodes}
        if parsed is None or (parsed[0] in ids and parsed[1] in ids):
            return nodes
    extra = await archived()
    alias = getattr(recall, "archived_alias", None)
    if parsed is not None and parsed[0] == parsed[1] and callable(alias):
        node = alias(parsed[0])
        if node is not None:
            extra = extra + [node]
    return nodes + extra


def parse_ref(ref: str) -> tuple[str, str] | None:
    """``(start, end)`` node ids for a valid ref, else None."""
    m = _REF_RE.match(ref.strip())
    if m is None:
        return None
    start = m.group(1)
    return start, m.group(2) or start


def _image_note(count: int, caption: str) -> str:
    """Say out loud that recall cannot return an image, and what can.

    Recall is a TEXT path: an expanded node with a screenshot in it came back
    as a caption and nothing else, and a model read that as "the visual is
    unrecoverable" and worked from memory instead. It is recoverable — by
    re-Reading the file, not by expanding history — so say which.
    """
    plural = "image" if count == 1 else f"{count} images"
    path = image_source_path(caption)
    recovery = (
        f're-Read "{path}" to see it again'
        if path
        else "re-run the tool that produced it to see it again"
    )
    return (
        f" [{plural} NOT restored here — HistoryExpand recovers text only; "
        f"{recovery}]"
    )


def _user_text_for_recall(text: str) -> str:
    """User text with its <system-reminder> blocks left out.

    Reminders appended to a user turn are harness plumbing (tool catalogs,
    ledger recitations) that the harness re-injects on fresh turns; one
    expanded request once came back carrying 6k chars of them. A text that is
    nothing but reminders stays verbatim, because a wake that fluid delivers
    as a single reminder block holds the only copy of its task reports.
    """
    removed = sum(len(m.group(0)) for m in _REMINDER_RE.finditer(text))
    if not removed:
        return text
    kept = _REMINDER_RE.sub("", text).strip()
    if not kept:
        return text
    return f"{kept} [{removed} chars of <system-reminder> harness text omitted]"


def _render_node(node: SessionNode) -> str:
    """Verbatim plain-text rendering of one stored node."""
    lines: list[str] = []
    role = node.message.role
    loose_images = 0
    for block in node.message.content:
        if isinstance(block, Text):
            text = (
                _user_text_for_recall(block.text) if role == "user" else block.text
            )
            lines.append(f"{role} ({node.node_id}): {text}")
        elif isinstance(block, ToolUseBlock):
            lines.append(
                f"{role} ({node.node_id}) tool_use {block.name}: {block.input!r}"
            )
        elif isinstance(block, ToolResultBlock):
            status = "error" if block.is_error else "result"
            inner_text = "".join(
                b.text for b in block.content if isinstance(b, Text)
            )
            images = sum(
                1 for b in block.content or () if isinstance(b, Image)
            )
            lines.append(
                f"{role} ({node.node_id}) tool_{status} "
                f"[{block.tool_use_id}]: {inner_text}"
                + (_image_note(images, inner_text) if images else "")
            )
        elif isinstance(block, Image):
            loose_images += 1
        # Thinking/Reasoning blocks are skipped: reasoning is model-bound
        # state. Image payloads are useless as text, so they are reported
        # rather than rendered.
    if loose_images:
        lines.append(f"{role} ({node.node_id}){_image_note(loose_images, '')}")
    return "\n".join(lines)


def steer_body_offset(node: SessionNode, body: str) -> int | None:
    """Where ``body`` starts in ``HistoryExpand(node_id)`` output, or None.

    A steer receipt (:mod:`fluid.context.steer_render`) names this offset so
    the model can page straight to a long notice. None means the stored node
    does not carry the text, and the steer must then stay verbatim.
    """
    if not body:
        return None
    pos = _render_node(node).find(body)
    return pos if pos >= 0 else None


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


def calling_node_ids(
    nodes: list[SessionNode], tool_use_id: str | None = None
) -> set[str]:
    """The nodes that hold the call being answered right now.

    That is the newest assistant node when one of its tool calls has no
    result on the path yet (the in-flight call or code cell), plus the node
    holding ``tool_use_id`` when one is given. A search must not return them:
    the calling cell spells out the query, so it would rank as its own best
    hit.
    """
    answered = {
        block.tool_use_id
        for node in nodes if node.message.role == "user"
        for block in node.message.content if isinstance(block, ToolResultBlock)
    }
    out: set[str] = set()
    newest = next(
        (n for n in reversed(nodes) if n.message.role == "assistant"), None
    )
    for node in nodes:
        if node.message.role != "assistant":
            continue
        for block in node.message.content:
            if not isinstance(block, ToolUseBlock):
                continue
            if block.id == tool_use_id or (
                node is newest and block.id not in answered
            ):
                out.add(node.node_id)
                break
    return out


def search_path(
    nodes: list[SessionNode],
    query: str,
    *,
    max_results: int = DEFAULT_SEARCH_RESULTS,
    exclude: set[str] | frozenset[str] = frozenset(),
) -> list[Hit]:
    """Case-insensitive keyword search over every node on the path.

    Scores by matched-term count then total occurrences; one hit per node with
    a snippet around the first match. Searches the STORED nodes — content that
    compaction evicted from the rendered view is still found here. Node ids in
    ``exclude`` are skipped (the caller's own in-flight node).
    """
    terms = [t for t in query.lower().split() if t]
    if not terms:
        return []
    scored: list[tuple[int, int, int, Hit]] = []
    for pos, node in enumerate(nodes):
        if node.node_id in exclude:
            continue
        text = _render_node(node)
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
