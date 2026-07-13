"""V2 Phase-1 deletion (Commit 4): the team transcript watcher records
``team.member_*`` contract events onto the parent session's event log
(unfenced) instead of mutating live_session_store / broadcasting poll ticks.

Drives the watcher's internals directly (``_dispatch_event`` / ``_tail`` /
``_finalize``) with real TeammateRecord objects; the event log is asserted
through the per-test SQLite shim. No SSE / ASGITransport anywhere.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from app.core import team_transcript_watcher as tw
from app.core.team_registry import TeammateRecord, team_registry
from app.core.team_transcript_watcher import (
    TeamTranscriptWatcher,
    _children_from_events,
    _completed_status,
    _persist_team_member,
)
from app.db import session_events as se
from app.db import sessions as sessions_db


SID = "sess-team"


def _mk_session(session_id: str = SID, v2: bool = False) -> None:
    # `v2` is vestigial (create_session always stamps v2_stream TRUE): team
    # appends are UNCONDITIONAL (not gated on the flag) — team rendering must
    # survive on any session.
    sessions_db.create_session(session_id, "default")


def _rec(**overrides) -> TeammateRecord:
    fields = dict(
        team_name="alpha",
        teammate_name="scout",
        teammate_session_id="tm-sess-1",
        pid=4242,
        last_prompt="find the bug",
    )
    fields.update(overrides)
    return TeammateRecord(**fields)


def _rows(session_id: str = SID):
    return se.get_events(session_id)


def _types(session_id: str = SID):
    return [r["event_type"] for r in _rows(session_id)]


async def _dispatch(rec, ev, seen=None):
    watcher = TeamTranscriptWatcher()
    await watcher._dispatch_event(SID, rec, ev, seen if seen is not None else set())


# ─── _dispatch_event → team.member_progress ─────────────────────────────────


async def test_text_block_appends_chunk_progress(mock_db):
    _mk_session()
    await _dispatch(_rec(), {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": "scanning files"}]},
    })
    rows = _rows()
    assert [r["event_type"] for r in rows] == ["team.member_progress"]
    payload = rows[0]["payload"]
    assert payload["team_name"] == "alpha"
    assert payload["teammate_name"] == "scout"
    assert payload["kind"] == "chunk"
    assert payload["payload"] == {"content": "scanning files"}
    # Unfenced: the watcher runs outside any run lease.
    assert rows[0]["run_id"] is None


async def test_string_content_appends_chunk_progress(mock_db):
    _mk_session()
    await _dispatch(_rec(), {
        "type": "assistant",
        "message": {"content": "plain string reply"},
    })
    rows = _rows()
    assert [r["event_type"] for r in rows] == ["team.member_progress"]
    assert rows[0]["payload"]["payload"] == {"content": "plain string reply"}


async def test_tool_use_and_result_progress_payloads(mock_db):
    _mk_session()
    rec = _rec()
    seen = set()
    await _dispatch(rec, {
        "type": "assistant",
        "message": {"content": [{
            "type": "tool_use", "id": "tu_9", "name": "Grep",
            "input": {"pattern": "bug"},
        }]},
    }, seen)
    await _dispatch(rec, {
        "type": "user",
        "message": {"content": [{
            "type": "tool_result", "tool_use_id": "tu_9",
            "content": [{"type": "text", "text": "3 matches"}],
            "is_error": False,
        }]},
    }, seen)

    rows = _rows()
    assert [r["event_type"] for r in rows] == [
        "team.member_progress", "team.member_progress",
    ]
    use = rows[0]["payload"]
    assert use["kind"] == "tool_use"
    assert use["payload"] == {"id": "tu_9", "name": "Grep", "input": {"pattern": "bug"}}
    result = rows[1]["payload"]
    assert result["kind"] == "tool_result"
    assert result["payload"] == {
        "tool_use_id": "tu_9", "output": "3 matches", "is_error": False,
    }

    # Duplicate tool_use ids are still deduped by the watcher.
    await _dispatch(rec, {
        "type": "assistant",
        "message": {"content": [{
            "type": "tool_use", "id": "tu_9", "name": "Grep", "input": {},
        }]},
    }, seen)
    assert len(_rows()) == 2


async def test_usage_progress_carries_tokens(mock_db):
    _mk_session()
    rec = _rec()
    await _dispatch(rec, {
        "type": "assistant",
        "message": {
            "usage": {"input_tokens": 100, "output_tokens": 20},
            "content": [],
        },
    })
    rows = _rows()
    assert [r["event_type"] for r in rows] == ["team.member_progress"]
    payload = rows[0]["payload"]
    assert payload["kind"] is None
    assert payload["tokens"] == 120
    assert payload["payload"]["usage"]["input_tokens"] == 100
    assert rec.usage["total_tokens"] == 120


# ─── _tail seed + cancellation, _finalize ───────────────────────────────────


async def test_tail_seeds_member_started_and_cancel_records_killed(mock_db, tmp_path):
    _mk_session()
    rec = _rec(jsonl_path=tmp_path / "missing.jsonl", pid=None)
    watcher = TeamTranscriptWatcher()

    # Signal once the tailer has ENTERED its try/while loop: cancelling during
    # the seed append itself lands outside the loop's CancelledError handler
    # (same as the pre-V2 structure), which is not what this test pins.
    loop_entered = asyncio.Event()
    orig_consume = watcher._consume_new_lines

    async def _consume(*a, **k):
        loop_entered.set()
        return await orig_consume(*a, **k)

    watcher._consume_new_lines = _consume  # type: ignore[method-assign]

    task = asyncio.create_task(watcher._tail(SID, rec))
    await asyncio.wait_for(loop_entered.wait(), timeout=5.0)
    assert _types() == ["team.member_started"]
    started = _rows()[0]["payload"]
    assert started["team_name"] == "alpha"
    assert started["teammate_name"] == "scout"
    assert started["prompt"] == "find the bug"

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert _types() == ["team.member_started", "team.member_completed"]
    completed = _rows()[-1]["payload"]
    assert completed["status"] == "killed"


async def test_tail_crash_records_failed(mock_db, tmp_path):
    _mk_session()
    rec = _rec(jsonl_path=tmp_path / "missing.jsonl")
    watcher = TeamTranscriptWatcher()
    with patch.object(
        watcher, "_consume_new_lines",
        new=AsyncMock(side_effect=RuntimeError("tailer boom")),
    ):
        await watcher._tail(SID, rec)  # crash path swallows after recording

    assert _types() == ["team.member_started", "team.member_completed"]
    assert _rows()[-1]["payload"]["status"] == "failed"


async def test_finalize_records_completed_and_persists(mock_db):
    _mk_session()
    rec = _rec(usage={"total_tokens": 50})
    watcher = TeamTranscriptWatcher()
    upsert = MagicMock()

    from app.db import database
    with patch.object(team_registry, "update_teammate", new=AsyncMock()) as upd, \
         patch.object(database, "upsert_team_member_message", new=upsert):
        await watcher._finalize(SID, rec)

    assert _types() == ["team.member_completed"]
    payload = _rows()[0]["payload"]
    assert payload["status"] == "completed"
    assert payload["usage"] == {"total_tokens": 50}
    upd.assert_awaited_once()
    upsert.assert_called_once()
    kwargs = upsert.call_args.kwargs
    assert kwargs["session_id"] == SID
    assert kwargs["team_name"] == "alpha"
    assert kwargs["teammate_name"] == "scout"
    assert kwargs["metadata"]["teammate_status"] == "completed"


async def test_finalize_maps_error_status_to_failed(mock_db):
    _mk_session()
    rec = _rec(status="error")
    watcher = TeamTranscriptWatcher()

    from app.db import database
    with patch.object(team_registry, "update_teammate", new=AsyncMock()), \
         patch.object(database, "upsert_team_member_message", new=MagicMock()):
        await watcher._finalize(SID, rec)

    assert _rows()[-1]["payload"]["status"] == "failed"


def test_completed_status_mapping():
    assert _completed_status("error") == "failed"
    assert _completed_status("failed") == "failed"
    assert _completed_status("killed") == "killed"
    assert _completed_status("cancelled") == "killed"
    assert _completed_status("completed") == "completed"
    assert _completed_status(None) == "completed"
    assert _completed_status("anything-else") == "completed"


# ─── persistence re-sourced from the event log ──────────────────────────────


async def test_persist_team_member_rebuilds_children_from_event_log(mock_db):
    _mk_session()
    rec = _rec()
    watcher = TeamTranscriptWatcher()
    seen = set()

    # Replay a small transcript through the dispatcher (writes the log).
    await watcher._dispatch_event(SID, rec, {
        "type": "assistant",
        "message": {"content": [
            {"type": "text", "text": "looking... "},
            {"type": "tool_use", "id": "tu_1", "name": "Read",
             "input": {"file": "a.py"}},
        ]},
    }, seen)
    await watcher._dispatch_event(SID, rec, {
        "type": "user",
        "message": {"content": [{
            "type": "tool_result", "tool_use_id": "tu_1",
            "content": "contents", "is_error": True,
        }]},
    }, seen)
    await watcher._dispatch_event(SID, rec, {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": "found it"}]},
    }, seen)

    upsert = MagicMock()
    from app.db import database
    with patch.object(database, "upsert_team_member_message", new=upsert):
        await _persist_team_member(SID, rec, "completed")

    upsert.assert_called_once()
    kwargs = upsert.call_args.kwargs
    assert kwargs["content"] == ""
    metadata = kwargs["metadata"]
    assert metadata["team_name"] == "alpha"
    assert metadata["teammate_name"] == "scout"
    assert metadata["teammate_session_id"] == "tm-sess-1"
    assert metadata["teammate_pid"] == 4242
    assert metadata["teammate_status"] == "completed"
    assert metadata["agent_prompt"] == "find the bug"

    children = metadata["agent_children"]
    assert [c["type"] for c in children] == ["text", "tool_use", "text"]
    assert children[0]["content"] == "looking... "
    tool = children[1]
    assert tool["toolName"] == "Read"
    assert tool["toolId"] == "tu_1"
    assert tool["toolInput"] == {"file": "a.py"}
    assert tool["toolResult"] == "contents"
    assert tool["toolStatus"] == "error"
    assert children[2]["content"] == "found it"
    assert all(c["streaming"] is False for c in children)


async def test_persist_ignores_other_teammates_events(mock_db):
    _mk_session()
    rec_a = _rec()
    rec_b = _rec(teammate_name="builder", teammate_session_id="tm-sess-2")
    watcher = TeamTranscriptWatcher()

    await watcher._dispatch_event(SID, rec_a, {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": "A text"}]},
    }, set())
    await watcher._dispatch_event(SID, rec_b, {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": "B text"}]},
    }, set())

    upsert = MagicMock()
    from app.db import database
    with patch.object(database, "upsert_team_member_message", new=upsert):
        await _persist_team_member(SID, rec_a, "completed")

    children = upsert.call_args.kwargs["metadata"]["agent_children"]
    assert len(children) == 1
    assert children[0]["content"] == "A text"


def test_children_from_events_coalesces_chunks_and_matches_tools():
    payloads = [
        {"kind": "chunk", "payload": {"content": "one "}},
        {"kind": "chunk", "payload": {"content": "two"}},
        {"kind": "tool_use", "payload": {"id": "t1", "name": "Bash",
                                         "input": {"command": "ls"}}},
        {"kind": "chunk", "payload": {"content": "after tool"}},
        {"kind": "tool_result", "payload": {"tool_use_id": "t1",
                                            "output": "ok", "is_error": False}},
        # Duplicate tool id and unmatched result are ignored.
        {"kind": "tool_use", "payload": {"id": "t1", "name": "Bash", "input": {}}},
        {"kind": "tool_result", "payload": {"tool_use_id": "t-unknown",
                                            "output": "x", "is_error": True}},
        # Usage-only progress rows (kind None) contribute nothing.
        {"kind": None, "payload": {"usage": {"total_tokens": 5}}},
    ]
    children = _children_from_events("alpha", "scout", payloads)
    assert [c["type"] for c in children] == ["text", "tool_use", "text"]
    assert children[0]["content"] == "one two"
    assert children[1]["toolStatus"] == "complete"
    assert children[1]["toolResult"] == "ok"
    assert children[2]["content"] == "after tool"
    # Ids are stable + namespaced per teammate.
    assert children[0]["id"] == "alpha-scout-text-1"
    assert children[1]["id"] == "alpha-scout-tool-t1"


async def test_no_poll_tick_broadcast_remains():
    """The team_member_update poll-tick broadcast was deleted: the watcher
    module must not touch sync_engine or live_session_store at all."""
    import inspect
    src = inspect.getsource(tw)
    assert "broadcast_stream_chunk" not in src
    assert "live_session_store" not in src
    assert "sync_engine" not in src
