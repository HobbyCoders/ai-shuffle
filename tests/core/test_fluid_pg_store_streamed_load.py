"""Cold hydration bounds libpq's raw copy, preserving the complete node tree."""
import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from app.core.provider import fluid_pg_store as module
from fluid.messages import Message, Text
from fluid.store.base import SessionNode


def test_load_uses_a_closed_server_cursor_not_a_buffered_result(monkeypatch):
    class Stream:
        closed = False
        def __enter__(self):
            return self
        def __exit__(self, *_):
            self.closed = True
        def execute(self, sql, params):
            assert "fluid_session_nodes" in sql and params == ("s",)
        def fetchall(self):
            pytest.fail("cold hydration buffered the whole transcript")
        def __iter__(self):
            assert self.itersize == 32
            for i in range(65):
                raw = Message(role="user", content=[Text(text=f"entry {i} café")]).to_dict()
                yield dict(node_id=str(i), parent_id=str(i-1) if i else None, ts=float(i),
                           message=raw if i % 2 else json.dumps(raw))
    stream = Stream()
    def cursor(*, name=None):
        if name is not None:
            assert name == "fluid_resume_nodes"
            return stream
        return SimpleNamespace(execute=lambda *_: None, fetchone=lambda: dict(
            session_id="s", head_node_id="64", created_at=0, cli_session_id=None, project_root=None))
    @contextmanager
    def db():
        yield SimpleNamespace(cursor=cursor)
        assert stream.closed  # close before commit/returning the connection
    monkeypatch.setattr(module, "get_db", db)
    result = module._load_session_sync("s")
    assert len(result["nodes"]) == 65
    assert result["meta"].head_node_id == "64"
    for key, node in result["nodes"].items():
        assert node.message.text() == f"entry {key} café"
        assert result["node_sizes"][key] == len(json.dumps(node.message.to_dict()))


@pytest.mark.asyncio
async def test_streamed_cold_tree_preserves_branches_pins_and_evicted_reads(mock_db):
    writer = module.PgFluidStore()
    await writer.create_session("stream-tree")
    nodes = [SessionNode(node_id=key, parent_id=parent,
                        message=Message(role="user", content=[Text(text=text)]))
             for key, parent, text in [("root", None, "task\nkeep café"),
                                       ("old", "root", "old text"),
                                       ("fork", "root", "other branch"),
                                       ("end", "old", "checkpoint"),
                                       ("tail", "end", "tail")]]
    nodes[0].message.pin_context("keep café")
    for node in nodes:
        await writer.append("stream-tree", node)
    cold = module.PgFluidStore()
    assert [n.node_id for n in await cold.load_path("stream-tree")] == ["root", "old", "end", "tail"]
    assert [n.node_id for n in await cold.load_path("stream-tree", "fork")] == ["root", "fork"]
    for node in nodes:
        assert (await cold.get_node("stream-tree", node.node_id)).message.to_dict() == node.message.to_dict()
    await cold.evict_prefix("stream-tree", "end", {"root"})
    assert [n.node_id for n in await cold.load_path("stream-tree", "fork")] == ["root", "fork"]
    assert (await cold.get_node("stream-tree", "root")).message.pinned_text() == "keep café"


@pytest.mark.asyncio
async def test_decode_failure_never_publishes_a_partial_tree_and_can_retry(mock_db, monkeypatch):
    writer = module.PgFluidStore()
    await writer.create_session("retry-stream")
    for key in ("a", "b"):
        await writer.append("retry-stream", SessionNode(node_id=key, parent_id=None,
            message=Message(role="user", content=[Text(text=key)])))
    cold = module.PgFluidStore()
    original = module.Message.from_dict
    def broken(raw):
        if raw["content"][0]["text"] == "b":
            raise ValueError("bad node")
        return original(raw)
    monkeypatch.setattr(module.Message, "from_dict", broken)
    with pytest.raises(ValueError, match="bad node"):
        await cold.get_session("retry-stream")
    assert "retry-stream" not in cold._loaded
    assert not cold._nodes
    monkeypatch.setattr(module.Message, "from_dict", original)
    assert await cold.get_session("retry-stream")
    assert set(cold._nodes["retry-stream"]) == {"a", "b"}
