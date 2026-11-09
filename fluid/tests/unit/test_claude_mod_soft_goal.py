"""Compaction's size goal preserves recent work; only a window risk warns."""
import logging

import pytest

from fluid.engine import delegated
from fluid.engine.claude_mod_compaction import compact_messages, project
from fluid.engine.delegated import DelegatedLoop
from fluid.messages import Message, Text, ToolResultBlock, ToolUseBlock
from fluid.store.base import MemoryStore, SessionNode, new_id
from fluid.tools.builtins import register_builtins
from fluid.tools.registry import ToolRegistry


@pytest.mark.parametrize("window,after,level", [
    (200_000, 39_770, logging.INFO),
    (200_000, 200_000, logging.WARNING),
    (None, 39_770, logging.INFO),
])
async def test_overshoot_log_is_informational_unless_at_selected_window(monkeypatch, caplog, window, after, level):
    def compact(rows, nodes, **kwargs):
        kwargs["report"].update(met=False, after_tokens=after, target_tokens=30_000,
                                contributors={"stored_kept": after})
        return rows

    monkeypatch.setattr(delegated, "compact_messages", compact)
    registry = ToolRegistry()
    register_builtins(registry)
    loop = DelegatedLoop(store=MemoryStore(), model="test-model", registry=registry,
                         compaction_window=window)
    await loop._ensure_session()
    await loop._start_bundled_server()
    try:
        with caplog.at_level(logging.INFO, logger="fluid.engine.delegated"):
            result = await loop._compact_mod_messages({"v": 1, "trigger": "manual", "messages": []})
        assert result["messages"] == []
        assert result["compaction_id"].startswith("cmp_")
        records = [r for r in caplog.records if "soft goal" in r.message]
        assert len(records) == 1 and records[0].levelno == level
        assert "stored_kept" in records[0].message
    finally:
        await loop.aclose()


def test_oversized_recent_stored_result_can_exceed_soft_goal():
    nodes, rows = [], []
    for turn in range(12):
        messages = [Message("user", [Text(f"Request {turn}: retain instructions.")]),
                    Message("assistant", [ToolUseBlock(f"t{turn}", "Read", {"file_path": "sample"})]),
                    Message("user", [ToolResultBlock(f"t{turn}", [Text("recent-result " * 15_000)])]),
                    Message("assistant", [Text(f"Finished {turn}.")])]
        for message in messages:
            node = SessionNode(new_id("node"), nodes[-1].node_id if nodes else None, message)
            nodes.append(node)
            rows.append({**project(message), "handle": new_id("handle")})
    report = {}
    output = compact_messages(rows, nodes, context_window=200_000, report=report)
    assert report["after_tokens"] > report["target_tokens"]
    assert report["met"] is False
    assert any("Request 11: retain instructions." in row["text"] for row in output)
    assert any(row.get("handle") == rows[-2]["handle"] for row in output)
