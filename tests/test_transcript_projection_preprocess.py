from __future__ import annotations

from datetime import datetime, timezone
import json

import pytest

from app.api import events as serving
from app.core.transcript_views import preprocess


def rows_for_corpus():
    rows = []
    def add(kind, payload, turn="turn"):
        seq = len(rows) + 1
        rows.append({"id": f"source-{seq}", "session_id": "session", "seq": seq,
                     "event_type": kind, "turn_id": turn, "run_id": "run", "command_id": None,
                     "created_at": datetime(2026, 10, 30, tzinfo=timezone.utc), "payload": payload})
    add("message.user", {"content": "Request", "seq": 999})
    add("assistant.text_delta", {"text": "One"})
    add("assistant.text_delta", {"text": " two"})
    add("tool.input_delta", {"tool_use_id": "tool", "partial_json": "{"})
    add("tool.input_delta", {"tool_use_id": "tool", "partial_json": "}"})
    add("tool.use", {"tool_use_id": "tool", "name": "Bash", "input": {"command": "pwd"}})
    add("subagent.progress", {"task_id": "task", "tool_use_id": "launch", "kind": "text", "text": "A"})
    add("subagent.progress", {"task_id": "task", "tool_use_id": "launch", "kind": "text", "text": "B"})
    add("subagent.progress", {"task_id": "task", "kind": "tool_use", "text": "", "tool_name": "Read"})
    add("subagent.progress", {"task_id": "task", "kind": "text", "text": ""})
    add("subagent.progress", {"task_id": "task", "kind": "text", "text": "C"})
    add("subagent.progress", {"task_id": "other", "kind": "text", "text": "D"})
    add("turn.usage", {"input_tokens": 3, "output_tokens": 4, "context_usage": {"entries": [{"tokens": 3}]}})
    add("turn.completed", {})
    add("message.user", {"content": "Next"}, "next")
    add("tool.input_delta", {"tool_use_id": "unresolved", "partial_json": "{"}, "next")
    add("assistant.text_delta", {"text": "Tail"}, "next")
    return rows


async def serving_frames(monkeypatch, rows, uses):
    monkeypatch.setattr(serving, "get_events", lambda sid, from_seq, limit: [r for r in rows if r["seq"] > from_seq][:limit])
    monkeypatch.setattr(serving, "get_tool_use_seqs", lambda sid, from_seq: uses)
    frames = []
    async for _, frame in serving._replay_from("session", 0):
        data = next(line[6:] for line in frame.splitlines() if line.startswith("data: "))
        frames.append(json.loads(data))
    return frames


@pytest.mark.asyncio
@pytest.mark.parametrize("progress", ["0", "1"])
@pytest.mark.parametrize("usage", ["0", "1"])
async def test_resume_every_cut_matches_real_cold_serving_preprocessing(monkeypatch, progress, usage):
    monkeypatch.setenv(serving._COALESCE_PROGRESS_ENV, progress)
    monkeypatch.setenv(serving._SLIM_USAGE_ENV, usage)
    rows = rows_for_corpus()
    uses = {("turn", "tool"): 6}
    lookups = {preprocess.lookup_key(*key): seq for key, seq in uses.items()}
    expected = await serving_frames(monkeypatch, rows, uses)
    for cut in range(len(rows) + 1):
        tail = preprocess.ReplayTail()
        tail.append(rows[:cut], lookups)
        tail = preprocess.ReplayTail.restore(tail.dump())
        tail.append(rows[cut:], lookups)
        assert tail.frames() == expected
        assert tail.frames() == expected  # EOF preview must not consume pending runs.
        assert any(frame["created_at"] == "2026-10-30T00:00:00+00:00" for frame in tail.frames())


@pytest.mark.asyncio
async def test_later_tool_use_rechecks_only_raw_mutable_attempt(monkeypatch):
    rows = rows_for_corpus()[:6]
    tail = preprocess.ReplayTail()
    tail.append(rows[:5], {})
    assert any(frame["type"] == "tool.input_delta" for frame in tail.frames())
    lookups = {preprocess.lookup_key("turn", "tool"): 6}
    tail.reevaluate(lookups)
    tail.append(rows[5:], lookups)
    expected = await serving_frames(monkeypatch, rows, {("turn", "tool"): 6})
    assert tail.frames() == expected
    assert not any(frame["type"] == "tool.input_delta" for frame in tail.frames())


def test_acknowledge_boundary_preserves_pending_suffix_without_approximation():
    rows = rows_for_corpus()
    tail = preprocess.ReplayTail()
    tail.append(rows, {preprocess.lookup_key("turn", "tool"): 6})
    expected = [frame for frame in tail.frames() if frame["seq"] > 15]
    tail.acknowledge(15)
    restored = preprocess.ReplayTail.restore(tail.dump())
    assert restored.frames() == expected
    assert [row["seq"] for row in restored.raw] == [16, 17]


def test_unsafe_checkpoint_and_changed_flags_fail_closed(monkeypatch):
    tail = preprocess.ReplayTail()
    tail.append(rows_for_corpus()[:3], {})
    with pytest.raises(preprocess.TailUnavailable, match="unsafe_preprocessing_boundary"):
        tail.acknowledge(2)
    text = preprocess.ReplayTail().dump()
    monkeypatch.setenv(serving._COALESCE_PROGRESS_ENV, "0" if preprocess.replay_flags()["progress"] else "1")
    with pytest.raises(preprocess.TailUnavailable, match="preprocessing_changed"):
        preprocess.ReplayTail.restore(text)


def test_limits_reject_instead_of_clipping_source(monkeypatch):
    monkeypatch.setattr(preprocess, "MAX_RAW_ROWS", 2)
    tail = preprocess.ReplayTail()
    with pytest.raises(preprocess.TailUnavailable, match="mutable_tail_limit"):
        tail.append(rows_for_corpus()[:3], {})
    assert len(tail.raw) == 3  # no suffix is silently discarded or truncated
