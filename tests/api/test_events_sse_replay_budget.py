"""Replay budget for opening a chat: turn.usage and subagent.progress stay small.

Opening a long chat replays its whole event log. Two event types made most of
the bytes (docs/perf/sprint-2026-10.md): every ``turn.usage`` carried the full
context-usage snapshot twice (40-45 KB each), and a subagent's text streamed
as one ``subagent.progress`` row per delta. The client keeps only the latest
snapshot and concatenates the deltas, so all of that was parsed and dropped.

The benchmark records a fixture session through the real TurnRecorder and
replays it through ``event_stream``. A second fixture, written in the old row
shape, is committed for the frontend (``reducer-replay-equivalence.test.ts``)
to prove the client builds the same state from the slim and the fat replay.
Regenerate it with ``UPDATE_REPLAY_FIXTURE=1``.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from app.api.events import event_stream
from app.core.turn_recorder import TurnRecorder
from app.db import session_events as se
from app.db import sessions as sessions_db
from tests.api.sse_helpers import parse_frame
from tests.shared_fixtures import reset_backfill_memo  # noqa: F401  (autouse fixture)

FIXTURE_PATH = (
    Path(__file__).resolve().parents[2]
    / "frontend/src/lib/stores/session/v2/__fixtures__/replay-usage-bloat.json"
)

TURNS = 24
USAGE_CALLS_PER_TURN = 3
CHILD_STEPS = 3          # each: a text stretch, a tool_use, a tool_result
CHUNKS_PER_STEP = 30     # subagent text deltas per text stretch

# Budgets for the recorded fixture. Before this change it replayed 5.4 MB in
# 2,520 frames, with 60 KB turn.usage frames and 96 progress frames per turn;
# after it, 270 KB in 432 frames.
REPLAY_BYTES_BUDGET = 320_000
USAGE_FRAME_MAX_BYTES = 2048


def fat_context_usage(total: int, *, grid_rows: int = 40, mcp_tools: int = 120,
                      skills: int = 25) -> dict:
    """A context-usage snapshot shaped like the SDK's, breakdown included."""
    names = [
        ("System prompt", "#8b5cf6"), ("System tools", "#3b82f6"),
        ("MCP tools", "#06b6d4"), ("Custom agents", "#10b981"),
        ("Memory files", "#f59e0b"), ("Skills", "#ef4444"),
        ("Messages", "#6366f1"), ("Free space", "#9ca3af"),
        ("Autocompact buffer", "#d1d5db"), ("MCP tools (deferred)", "#67e8f9"),
    ]
    categories = [
        {"name": n, "tokens": 1000 + i * 137 + total % 97, "color": c,
         **({"isDeferred": True} if "deferred" in n else {})}
        for i, (n, c) in enumerate(names)
    ]
    return {
        "categories": categories,
        "totalTokens": total,
        "maxTokens": 200_000,
        "rawMaxTokens": 200_000,
        "percentage": round(total / 2000, 1),
        "model": "claude-sonnet-4-6",
        "isAutoCompactEnabled": True,
        "gridRows": [
            [{"color": names[(r + c) % 10][1], "isFilled": (r + c) % 3 != 0,
              "categoryName": names[(r + c) % 10][0], "tokens": 500 + c,
              "percentage": 0.25} for c in range(4)]
            for r in range(grid_rows)
        ],
        "mcpTools": [
            {"name": f"mcp__server_{i % 7}__tool_number_{i}",
             "serverName": f"server_{i % 7}", "tokens": 120 + i}
            for i in range(mcp_tools)
        ],
        "skills": [
            {"name": f"skill-{i}", "source": "plugin", "tokens": 40 + i}
            for i in range(skills)
        ],
        "memoryFiles": [{"path": "/home/u/.claude/CLAUDE.md", "type": "User", "tokens": 812}],
        "agents": [{"agentType": "general-purpose", "source": "built-in", "tokens": 210}],
    }


def _usage(t: int, call: int, snapshot: dict) -> dict:
    return {
        "model_call_index": call,
        "input_tokens": 1200 + t * 10 + call,
        "output_tokens": 300 + call,
        "cache_read_input_tokens": 9000 + t * 100,
        "cache_creation_input_tokens": 200,
        "cost_usd": 0.0123,
        "model": "claude-sonnet-4-6",
        "context_usage": snapshot,
    }


async def _record_turn(sid: str, t: int) -> None:
    """One turn through the real recorder: usage per call plus a subagent."""
    rec = await TurnRecorder.maybe_create(sid)
    assert rec is not None
    await rec.start(f"ask {t}")
    task = f"task-{t}"
    for call in range(1, USAGE_CALLS_PER_TURN + 1):
        await rec.on_event({
            "type": "turn_usage",
            "usage": _usage(t, call, fat_context_usage(20_000 + t * 500 + call)),
        })
        if call == 1:
            await rec.on_event({"type": "subagent_start", "tool_id": task,
                                "agent_id": task, "agent_type": "general-purpose",
                                "description": f"look into {t}", "prompt": "go"})
            for step in range(CHILD_STEPS):
                for i in range(CHUNKS_PER_STEP):
                    await rec.on_event({"type": "subagent_chunk", "agent_id": task,
                                        "content": f"w{step}.{i} "})
                tool_id = f"{task}-tu-{step}"
                await rec.on_event({"type": "subagent_tool_use", "agent_id": task,
                                    "name": "Read", "id": tool_id,
                                    "tool_use_id": tool_id,
                                    "input": {"file_path": f"/src/f{step}.py"}})
                await rec.on_event({"type": "subagent_tool_result", "agent_id": task,
                                    "tool_use_id": tool_id, "content": "ok"})
            await rec.on_event({"type": "subagent_done", "tool_id": task,
                                "agent_id": task, "result": "found it"})
    await rec.on_event({"type": "chunk", "content": f"answer {t}"})
    await rec.on_event({"type": "done", "metadata": {"tokens_in": 10, "tokens_out": 5}})
    await rec.close()


async def _replay(sid: str) -> list[str]:
    """Raw backfill frames up to (not including) the caught_up sentinel."""
    frames: list[str] = []

    async def _pump(agen):
        async for chunk in agen:
            if chunk.startswith(":"):
                continue
            if parse_frame(chunk).get("event") == "caught_up":
                return
            frames.append(chunk)

    agen = event_stream(sid, 0)
    try:
        await asyncio.wait_for(_pump(agen), timeout=30.0)
    finally:
        await agen.aclose()
    return frames


def _measure(frames: list[str]) -> dict:
    parsed = [parse_frame(f) for f in frames]
    usage = [len(f.encode()) for f, p in zip(frames, parsed) if p["event"] == "turn.usage"]
    return {
        "bytes": sum(len(f.encode()) for f in frames),
        "frames": len(frames),
        "usage_frames": len(usage),
        "usage_max": max(usage, default=0),
        "progress_frames": sum(1 for p in parsed if p["event"] == "subagent.progress"),
    }


async def test_recorded_chat_replays_within_budget(mock_db, gw):
    sid = "sess-replay-budget"
    sessions_db.create_session(sid, "default")
    for t in range(TURNS):
        await _record_turn(sid, t)

    m = _measure(await _replay(sid))
    print(f"replay budget fixture: {m}")

    # Every model call keeps its turn.usage row (turn_scheduler counts them).
    assert m["usage_frames"] == TURNS * USAGE_CALLS_PER_TURN
    assert m["usage_max"] <= USAGE_FRAME_MAX_BYTES, m
    # One frame per child step: a text stretch, a tool_use, a tool_result.
    assert m["progress_frames"] <= TURNS * CHILD_STEPS * 3, m
    assert m["bytes"] <= REPLAY_BYTES_BUDGET, m


async def test_recorded_usage_row_keeps_one_compact_snapshot(mock_db, gw):
    sid = "sess-usage-row"
    sessions_db.create_session(sid, "default")
    await _record_turn(sid, 0)
    rows = [r for r in se.get_events(sid) if r["event_type"] == "turn.usage"]
    payload = rows[-1]["payload"]
    assert "context_usage" not in payload["usage"]
    snap = payload["context_usage"]
    assert set(snap) <= {"categories", "totalTokens", "maxTokens", "rawMaxTokens",
                         "percentage", "model", "measured", "isAutoCompactEnabled"}
    assert snap["totalTokens"] == 20_000 + USAGE_CALLS_PER_TURN
    assert all(set(c) <= {"name", "tokens", "color", "isDeferred"}
               for c in snap["categories"])
    # Token accounting is untouched.
    assert payload["usage"]["input_tokens"] == 1200 + USAGE_CALLS_PER_TURN


# ---------------------------------------------------------------------------
# Old rows: written before this change, or mirrored from an older worker.
# ---------------------------------------------------------------------------

def legacy_rows() -> list[dict]:
    """A small log in the OLD row shape, covering the reducer's edge cases.

    Fat duplicated snapshots; one progress row per text delta, two subagents
    whose deltas interleave, empty deltas, ``note``/``text`` variants, a late
    delta after the card completed, and a tool_result with no tool_use.
    """
    rows: list[dict] = []

    def add(t: str, payload: dict, turn: str) -> None:
        rows.append({"type": t, "payload": payload, "turn_id": turn})

    def usage(turn: str, n: int, call: int) -> None:
        snap = fat_context_usage(n, grid_rows=3, mcp_tools=4, skills=2)
        add("turn.usage", {"usage": {**_usage(0, call, snap), "input_tokens": n},
                           "context_usage": snap}, turn)

    def chunk(turn: str, task: str, content=None, **extra) -> None:
        inner = {} if content is None else {"content": content}
        add("subagent.progress", {"task_id": task, "kind": "chunk",
                                  "payload": {**inner, **extra.pop("inner", {})},
                                  "note": extra.pop("note", None), "tokens": None}, turn)

    def step(turn: str, task: str, kind: str, **inner) -> None:
        add("subagent.progress", {"task_id": task, "kind": kind, "payload": inner,
                                  "note": None, "tokens": None}, turn)

    for t in range(3):
        turn = f"turn-{t}"
        a, b = f"a{t}", f"b{t}"
        add("turn.started", {"profile_id": None}, turn)
        add("message.user", {"content": f"ask {t}", "profile_id": None}, turn)
        usage(turn, 10_000 + t, 1)
        for task in (a, b):
            add("subagent.started", {"task_id": task, "agent_id": task,
                                     "agent_type": "general-purpose",
                                     "description": task}, turn)
        chunk(turn, a, "")
        for i in range(6):
            chunk(turn, a, f"a{i} ")
        chunk(turn, a, "")
        chunk(turn, a, None, note="noted ")
        chunk(turn, a, None, inner={"text": "texted "})
        chunk(turn, a, 42)
        # Interleaved parallel streams.
        for i in range(4):
            chunk(turn, a, f"x{i} ")
            chunk(turn, b, f"y{i} ")
        step(turn, a, "tool_use", name="Read", id=f"{a}-1", tool_use_id=f"{a}-1",
             input={"file_path": "/x"})
        for i in range(5):
            chunk(turn, b, f"z{i} ")
        step(turn, a, "tool_result", tool_use_id=f"{a}-1", content="file body")
        step(turn, b, "tool_result", tool_use_id="orphan", content="no use")
        for i in range(3):
            chunk(turn, a, f"after{i} ")
        usage(turn, 11_000 + t, 2)
        add("subagent.completed", {"task_id": a, "status": "completed",
                                   "result_preview": "done a"}, turn)
        # Late deltas after the card settled re-open it.
        chunk(turn, a, "late ")
        chunk(turn, a, "later ")
        add("subagent.completed", {"task_id": b, "status": "completed",
                                   "result_preview": "done b"}, turn)
        chunk(turn, "unknown-task", "nobody ")
        chunk(turn, "unknown-task", "listens ")
        add("assistant.text_delta", {"text": f"answer {t}"}, turn)
        usage(turn, 12_000 + t, 3)
        add("turn.completed", {"usage": {"tokens_in": 1}}, turn)
    # The log ends inside a run of deltas.
    chunk("turn-2", "b2", "tail1 ")
    chunk("turn-2", "b2", "")
    return rows


async def _wire_events(sid: str) -> list[dict]:
    return [parse_frame(f)["data"] for f in await _replay(sid)]


async def test_legacy_rows_replay_slim(mock_db, gw):
    sid = "sess-legacy-slim"
    se.append_events(sid, legacy_rows())
    events = await _wire_events(sid)
    for ev in events:
        if ev["type"] == "turn.usage":
            assert "context_usage" not in ev["usage"]
            assert "gridRows" not in ev["context_usage"]
            assert "mcpTools" not in ev["context_usage"]
            assert "skills" not in ev["context_usage"]
    seqs = [ev["seq"] for ev in events]
    assert seqs == sorted(set(seqs))
    # The final row is still the replay's last seq (the resume cursor).
    assert seqs[-1] == se.get_max_seq(sid)


async def test_kill_switches_restore_the_old_replay(mock_db, gw, monkeypatch):
    sid = "sess-legacy-fat"
    rows = legacy_rows()
    se.append_events(sid, rows)
    monkeypatch.setenv("AI_SHUFFLE_PERF_SLIM_USAGE_REPLAY", "0")
    monkeypatch.setenv("AI_SHUFFLE_PERF_COALESCE_SUBAGENT_PROGRESS", "0")
    events = await _wire_events(sid)
    progress = [r for r in rows if r["type"] == "subagent.progress"]
    assert sum(1 for e in events if e["type"] == "subagent.progress") == len(progress)
    usage = [e for e in events if e["type"] == "turn.usage"]
    assert all("gridRows" in e["usage"]["context_usage"] for e in usage)


async def test_progress_coalescing_merges_only_consecutive_rows(mock_db, gw):
    # A frame's seq is the client's resume cursor: every row at or below it
    # must be in that frame or an earlier one, so interleaved streams from two
    # subagents are merged only within their consecutive stretches.
    sid = "sess-progress-cursor"
    se.append_events(sid, legacy_rows())
    events = await _wire_events(sid)
    rows = se.get_events(sid)
    by_seq = {r["seq"]: r for r in rows}
    covered = 0
    for ev in events:
        # Every row between the previous frame and this one belongs to it.
        span = [by_seq[s] for s in range(covered + 1, ev["seq"] + 1)]
        if len(span) > 1:
            assert {r["event_type"] for r in span} == {"subagent.progress"}
            assert len({r["payload"]["task_id"] for r in span}) == 1
        covered = ev["seq"]


async def test_replay_fixture_matches_the_committed_copy(mock_db, gw, monkeypatch):
    """The frontend equivalence test reads both replays from this fixture."""
    sid = "sess-replay-fixture"
    se.append_events(sid, legacy_rows())
    slim = await _wire_events(sid)
    monkeypatch.setenv("AI_SHUFFLE_PERF_SLIM_USAGE_REPLAY", "0")
    monkeypatch.setenv("AI_SHUFFLE_PERF_COALESCE_SUBAGENT_PROGRESS", "0")
    fat = await _wire_events(sid)

    # Row ids and timestamps differ per run: name each after its row's seq, so
    # a merged frame still names the row whose envelope it kept.
    row_seq = {r["id"]: r["seq"] for r in se.get_events(sid)}

    def stable(events: list[dict]) -> list[dict]:
        return [
            {**e, "id": f"ev-{row_seq[e['id']]}",
             "created_at": f"2026-10-06T00:00:00.{row_seq[e['id']]:06d}+00:00"}
            for e in events
        ]

    fixture = {"fat": stable(fat), "slim": stable(slim)}
    # One event per line keeps the committed file small and its diffs readable.
    text = "{\n" + ",\n".join(
        f'"{name}": [\n' + ",\n".join(json.dumps(e, sort_keys=True) for e in events) + "\n]"
        for name, events in fixture.items()
    ) + "\n}\n"
    if os.environ.get("UPDATE_REPLAY_FIXTURE") == "1":
        FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
        FIXTURE_PATH.write_text(text)
    if not FIXTURE_PATH.exists():
        pytest.fail(f"missing {FIXTURE_PATH}; run with UPDATE_REPLAY_FIXTURE=1")
    assert json.loads(FIXTURE_PATH.read_text()) == fixture, (
        "replay fixture drifted; rerun with UPDATE_REPLAY_FIXTURE=1"
    )
    assert len(slim) < len(fat)
