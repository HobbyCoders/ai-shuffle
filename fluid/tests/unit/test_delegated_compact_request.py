"""A compaction the host asks for on the delegated leg (#1820).

The claude CLI owns its context, so the loop can only report what the CLI
did. These pin the three things that made a ``/compact`` look done when it
was not:

* the compaction event keeps the CLI's summary and the node it compacted
  after, read back from the CLI's transcript, so a CLI on another worker can
  be re-seeded from the compacted context instead of the whole tree;
* a ``/compact`` turn that ends without a boundary publishes a
  ``compaction_failed`` diagnostic with the CLI's reason;
* a turn that is not a ``/compact``, or one that did compact, publishes none.

Hermetic: the scripted fake CLI, no network.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import fluid.engine.delegated as delegated_mod
from fluid.engine.delegated import (
    DelegatedLoop,
    _is_compact_command,
    compaction_summary_in_transcript,
)
from fluid.events import ContextCompaction, TurnComplete, UserTurn
from fluid.events.model import DelegatedDiagnostic
from fluid.messages import Text
from fluid.store.base import MemoryStore

SCRIPTED = str(Path(__file__).parent.parent / "fixtures" / "fake_claude_script.py")
CLI_SID = "cli-compact-1"
_STATUS_ON = {"type": "system", "subtype": "status", "status": "compacting"}
_STATUS_OFF = {"type": "system", "subtype": "status", "status": None,
               "compact_result": "success"}


def _iso(epoch: float) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(epoch, tz=UTC).isoformat().replace("+00:00", "Z")


def _transcript(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")


def _cli_boundary(uuid: str, epoch: float) -> dict:
    return {"type": "system", "subtype": "compact_boundary", "uuid": uuid,
            "timestamp": _iso(epoch), "content": "Conversation compacted",
            "compactMetadata": {"trigger": "manual", "preTokens": 900_000}}


def _cli_summary(text: str) -> dict:
    return {"type": "user", "isCompactSummary": True,
            "message": {"role": "user", "content": text}}


def _loop(tmp_path: Path, turns: list[list[dict]], **kwargs) -> DelegatedLoop:
    script = tmp_path / "script.json"
    script.write_text(json.dumps({"turns": turns}), encoding="utf-8")
    return DelegatedLoop(
        store=kwargs.pop("store", None) or MemoryStore(), model="claude-fake",
        claude_cmd=[sys.executable, SCRIPTED],
        env={"FAKE_CLAUDE_SCRIPT": str(script),
             "CLAUDE_CONFIG_DIR": str(tmp_path / "config")},
        cwd=str(tmp_path), **kwargs,
    )


# ─── the summary in the CLI's transcript ─────────────────────────────────────

def test_the_summary_after_the_matching_boundary_is_read(tmp_path) -> None:
    now = time.time()
    path = tmp_path / "t.jsonl"
    _transcript(path, [
        _cli_boundary("old", now - 5000), _cli_summary("an older summary"),
        {"type": "user", "message": {"role": "user", "content": "more work"}},
        _cli_boundary("new", now - 2), _cli_summary("the new summary"),
    ])
    assert compaction_summary_in_transcript(str(path), "new", now=now) == "the new summary"
    assert compaction_summary_in_transcript(str(path), "old", now=now) == "an older summary"


def test_without_a_uuid_match_only_a_recent_last_boundary_counts(tmp_path) -> None:
    """A frame whose uuid the transcript does not carry takes the last
    boundary only when it was just written: an earlier compaction's summary
    must never be taken for the new one."""
    now = time.time()
    path = tmp_path / "t.jsonl"
    _transcript(path, [_cli_boundary("a", now - 3), _cli_summary("fresh")])
    assert compaction_summary_in_transcript(str(path), "elsewhere", now=now) == "fresh"
    _transcript(path, [_cli_boundary("a", now - 5000), _cli_summary("stale")])
    assert compaction_summary_in_transcript(str(path), "elsewhere", now=now) == ""
    assert compaction_summary_in_transcript(str(path), None, now=now) == ""


def test_a_boundary_whose_summary_is_not_written_yet_reads_empty(tmp_path) -> None:
    now = time.time()
    path = tmp_path / "t.jsonl"
    _transcript(path, [_cli_boundary("a", now - 1)])
    assert compaction_summary_in_transcript(str(path), "a", now=now) == ""


def test_a_compact_command_is_recognised_in_the_last_text_block() -> None:
    assert _is_compact_command([Text("/compact")])
    assert _is_compact_command([Text("  /compact focus on the tests")])
    assert _is_compact_command(["/compact"])
    assert not _is_compact_command([Text("/compaction is broken")])
    assert not _is_compact_command([Text("please /compact")])
    assert not _is_compact_command([Text("/compact"), Text("and then this")])


# ─── the loop ────────────────────────────────────────────────────────────────

async def test_a_compaction_keeps_the_cli_summary_and_its_cut_node(tmp_path) -> None:
    """The event carries the summary the CLI wrote and the last node before
    the boundary, which is the user's ``/compact`` node for a manual one."""
    _transcript(tmp_path / "config" / "projects" / "p" / f"{CLI_SID}.jsonl", [
        _cli_boundary("uuid-b1", time.time()), _cli_summary("Summary of the work so far."),
    ])
    store = MemoryStore()
    sid = (await store.create_session()).session_id
    loop = _loop(tmp_path, [[
        {"op": "frame", "frame": _STATUS_ON},
        {"op": "frame", "frame": _STATUS_OFF},
        {"op": "frame", "frame": {
            "type": "system", "subtype": "compact_boundary", "uuid": "uuid-b1",
            "session_id": CLI_SID,
            "compact_metadata": {"trigger": "manual", "pre_tokens": 900_000},
        }},
        {"op": "result", "result": ""},
    ]], store=store, session_id=sid)
    try:
        await loop.submit(UserTurn(content="/compact"))
        await loop.wait_turn()
        events = loop.bus.log(sid)
        path = await store.load_path(sid)
    finally:
        await loop.aclose()
    compactions = [e for e in events if isinstance(e, ContextCompaction)]
    assert len(compactions) == 1
    assert compactions[0].reason == "cli_manual_compact"
    assert compactions[0].summary == "Summary of the work so far."
    assert compactions[0].node_id == path[-1].node_id
    assert path[-1].message.role == "user"
    assert not any(isinstance(e, DelegatedDiagnostic) for e in events)
    stored = [e for e in await store.events_since(sid, 0) if isinstance(e, ContextCompaction)]
    assert stored[0].summary == "Summary of the work so far."


async def test_a_compaction_with_no_transcript_still_reports_without_waiting(
    tmp_path, monkeypatch,
) -> None:
    """No transcript on this machine: the card still comes, with no summary,
    and the loop does not sit out the retry budget."""
    monkeypatch.setattr(delegated_mod, "COMPACTION_SUMMARY_INTERVAL", 5.0)
    loop = _loop(tmp_path, [[
        {"op": "frame", "frame": _STATUS_ON},
        {"op": "frame", "frame": _STATUS_OFF},
        {"op": "frame", "frame": {"type": "system", "subtype": "compact_boundary",
                                  "session_id": "nowhere", "uuid": "x",
                                  "compact_metadata": {"trigger": "manual"}}},
        {"op": "result"},
    ]])
    started = time.monotonic()
    try:
        await loop.submit(UserTurn(content="/compact"))
        await loop.wait_turn()
        events = loop.bus.log(loop.session_id)
    finally:
        await loop.aclose()
    assert time.monotonic() - started < 4.0
    compactions = [e for e in events if isinstance(e, ContextCompaction)]
    assert len(compactions) == 1 and compactions[0].summary == ""


async def test_a_compact_turn_without_a_boundary_reports_the_cli_reason(tmp_path) -> None:
    """The CLI refuses a /compact with its output in the result text: the
    loop says the compaction did not happen, before the turn ends."""
    loop = _loop(tmp_path, [[
        {"op": "result", "result": "Not enough messages to compact."},
    ]])
    try:
        await loop.submit(UserTurn(content="/compact"))
        await loop.wait_turn()
        events = loop.bus.log(loop.session_id)
    finally:
        await loop.aclose()
    failures = [e for e in events if isinstance(e, DelegatedDiagnostic)]
    assert [(f.kind, f.message) for f in failures] == [
        ("compaction_failed", "Not enough messages to compact."),
    ]
    assert not any(isinstance(e, ContextCompaction) for e in events)
    assert events.index(failures[0]) < events.index(
        next(e for e in events if isinstance(e, TurnComplete))
    )


async def test_a_failed_compaction_reports_the_cli_compact_error(tmp_path) -> None:
    loop = _loop(tmp_path, [[
        {"op": "frame", "frame": _STATUS_ON},
        {"op": "frame", "frame": {**_STATUS_OFF, "compact_result": "failed",
                                  "compact_error": "Prompt is too long"}},
        {"op": "result", "result": "Error during compaction: Prompt is too long"},
    ]])
    try:
        await loop.submit(UserTurn(content="/compact"))
        await loop.wait_turn()
        events = loop.bus.log(loop.session_id)
    finally:
        await loop.aclose()
    failures = [e for e in events if isinstance(e, DelegatedDiagnostic)]
    assert [(f.kind, f.message) for f in failures] == [
        ("compaction_failed", "Prompt is too long"),
    ]


async def test_an_ordinary_turn_reports_no_compaction_failure(tmp_path) -> None:
    loop = _loop(tmp_path, [[
        {"op": "text", "text": "hello"}, {"op": "result"},
    ]])
    try:
        await loop.submit(UserTurn(content="say hello"))
        await loop.wait_turn()
        events = loop.bus.log(loop.session_id)
    finally:
        await loop.aclose()
    assert not any(isinstance(e, DelegatedDiagnostic) for e in events)
