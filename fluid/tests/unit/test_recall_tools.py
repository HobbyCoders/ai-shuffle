"""HistoryExpand / HistorySearch: the recall path back into evicted history.

Recall reads the STORED nodes — content compaction digested away is still
findable and re-readable verbatim — and recall output is an ordinary tool
result, so tier 1 trims it later (recall cannot leak context)."""

from fluid.context.compaction import CompactionConfig, Compactor
from fluid.context.recall import HistoryRecall, expand_ref, search_path
from fluid.context.trim import TrimConfig
from fluid.messages import Message, Text, ToolResultBlock, ToolUseBlock
from fluid.store.base import MemoryStore, SessionNode
from fluid.tools.builtins.history_expand import handler as expand_handler
from fluid.tools.builtins.history_search import handler as search_handler
from fluid.tools.registry import ToolCtx


def _node(nid: str, role: str, blocks: list) -> SessionNode:
    return SessionNode(node_id=nid, parent_id=None,
                       message=Message(role=role, content=blocks))


def _nodes() -> list[SessionNode]:
    return [
        _node("node_aa11", "user", [Text("find the flag CRIMSON-42 in config")]),
        _node("node_bb22", "assistant", [
            Text("checking"),
            ToolUseBlock(id="c1", name="Read", input={"file_path": "cfg.toml"}),
        ]),
        _node("node_cc33", "user", [
            ToolResultBlock(tool_use_id="c1", content=[Text("flag = CRIMSON-42\nport = 8080")]),
        ]),
        _node("node_dd44", "assistant", [Text("found it")]),
    ]


# ─── expand_ref ──────────────────────────────────────────────────────────────


def test_expand_single_node_verbatim() -> None:
    text, is_error = expand_ref(_nodes(), "node_cc33")
    assert not is_error
    assert "flag = CRIMSON-42" in text and "port = 8080" in text


def test_expand_range() -> None:
    text, is_error = expand_ref(_nodes(), "node_aa11..node_dd44")
    assert not is_error
    assert "CRIMSON-42 in config" in text and "found it" in text


def test_expand_bad_ref_and_off_path() -> None:
    text, is_error = expand_ref(_nodes(), "definitely-not-a-ref")
    assert is_error and "bad ref" in text
    text, is_error = expand_ref(_nodes(), "node_9999")
    assert is_error and "not on this session" in text


def test_expand_offset_windows_large_content() -> None:
    nodes = [_node("node_aa11", "user", [Text("Z" * 60_000)])]
    first, is_error = expand_ref(nodes, "node_aa11", max_chars=1_000)
    assert not is_error
    assert "continue with offset=" in first
    tail, is_error = expand_ref(nodes, "node_aa11", offset=59_000, max_chars=5_000)
    assert not is_error
    assert tail.endswith("Z" * 100)           # reaches the end of the text
    assert "continue with offset" not in tail  # nothing left to page


# ─── search_path ─────────────────────────────────────────────────────────────


def test_search_finds_and_ranks() -> None:
    hits = search_path(_nodes(), "crimson-42")
    assert hits and hits[0].node_id in ("node_aa11", "node_cc33")
    assert "CRIMSON-42" in hits[0].snippet


def test_search_finds_content_only_inside_digested_span() -> None:
    """The whole point: after compaction hides a turn behind a digest, search
    still reaches the original text via the store."""
    nodes = _nodes() + [
        _node(f"node_p{i}", "user", [Text(f"filler turn {i} " + "pad " * 200)])
        for i in range(4)
    ]
    c = Compactor(config=CompactionConfig(
        trigger_tokens=100, target_ratio=0.5, keep_recent_turns=1,
        trim=TrimConfig(trigger_tokens=100, keep_recent_tool_results=0,
                        min_result_chars=50),
    ))
    rendered, promos = c.apply(nodes)
    joined = "\n".join(
        "".join(getattr(b, "text", "") for b in m.content) for m in rendered
    )
    assert "flag = CRIMSON-42" not in joined      # evicted from the view
    hits = search_path(nodes, "crimson-42")       # but recall sees the store
    assert hits


# ─── Builtin handlers ────────────────────────────────────────────────────────


async def _seeded_ctx() -> ToolCtx:
    store = MemoryStore()
    meta = await store.create_session("ses_recall")
    parent = None
    for node in _nodes():
        node.parent_id = parent
        await store.append(meta.session_id, node)
        parent = node.node_id
    ctx = ToolCtx(session_id=meta.session_id)
    ctx.extra["history_recall"] = HistoryRecall(store=store, session_id=meta.session_id)
    return ctx


async def test_expand_handler_roundtrip() -> None:
    ctx = await _seeded_ctx()
    out = await expand_handler({"ref": "node_cc33"}, ctx)
    assert not out.is_error and "flag = CRIMSON-42" in out.text


async def test_search_handler_roundtrip() -> None:
    ctx = await _seeded_ctx()
    out = await search_handler({"query": "crimson"}, ctx)
    assert not out.is_error and "node_" in out.text and "HistoryExpand" in out.text


async def test_handlers_error_cleanly_without_store() -> None:
    ctx = ToolCtx()
    out = await expand_handler({"ref": "node_aa11"}, ctx)
    assert out.is_error and "unavailable" in out.text
    out = await search_handler({"query": "x"}, ctx)
    assert out.is_error and "unavailable" in out.text


def test_recall_results_are_trim_eligible() -> None:
    """A big expanded result is an ordinary tool result: tier 1 reclaims it
    later, so recall cannot permanently re-inflate the window."""
    nodes = [
        _node(f"node_h{i}", "user", [
            ToolResultBlock(tool_use_id=f"t{i}", content=[Text("recall output " + "y" * 5_000)]),
        ])
        for i in range(3)
    ]
    c = Compactor(config=CompactionConfig(
        trigger_tokens=1_000, keep_recent_turns=5,  # tier 2 stays out of it
        trim=TrimConfig(trigger_tokens=1_000, keep_recent_tool_results=1,
                        min_result_chars=500),
    ))
    rendered, promos = c.apply(nodes)
    assert any(p.tier == 1 and p.trimmed for p in promos)
    joined = "\n".join(
        "".join(getattr(cb, "text", "") for b in m.content
                for cb in (getattr(b, "content", None) or []))
        for m in rendered
    )
    assert "trimmed to save context" in joined
