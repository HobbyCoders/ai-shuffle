"""Unit tests for app/core/history_backfill.py — lazy legacy-history backfill.

Covers the full frontend-message → contract-event mapping table (golden
shapes), the skip rows, timestamp/uuid/profile passthrough, the
turn_id='backfill' stamp, ensure_backfilled's fast path + empty memo + failure
tolerance, and the exclusive-create counter-gate race (one winner; backfill vs
recorder appends serialize).

All conversion tests call the pure converter directly; DB tests ride the
autouse SQLite shim (``mock_db``).
"""

import asyncio

import pytest

import app.core.history_backfill as hb
from app.core.history_backfill import convert_messages_to_events, ensure_backfilled
from app.db import session_events as se
from app.db import sessions as sessions_db

SID = "sess-backfill"


@pytest.fixture(autouse=True)
def _reset_memo():
    hb._reset_memo()
    yield
    hb._reset_memo()


def _one(msg):
    """Convert a single message, asserting exactly one event came out."""
    events = convert_messages_to_events(SID, [msg])
    assert len(events) == 1
    return events[0]


# ---------------------------------------------------------------------------
# mapping table — golden shapes
# ---------------------------------------------------------------------------

def test_user_plain_maps_to_message_user():
    ev = _one({
        "id": "m1", "role": "user", "type": "text", "content": "hello there",
        "uuid": "u-1", "profile_id": "prof-1",
        "metadata": {"timestamp": "2026-07-01T10:00:00Z"},
    })
    assert ev["type"] == "message.user"
    assert ev["turn_id"] == "backfill"
    assert ev["command_id"] is None
    p = ev["payload"]
    assert p["content"] == "hello there"
    assert p["kind"] == "user"
    assert p["uuid"] == "u-1"
    assert p["profile_id"] == "prof-1"
    assert p["timestamp"].startswith("2026-07-01T10:00:00")


def test_user_command_maps_to_message_user_command():
    ev = _one({
        "role": "user", "type": "command", "content": "/review src",
        "commandName": "/review", "commandArgs": "src",
    })
    assert ev["type"] == "message.user"
    p = ev["payload"]
    assert p["kind"] == "command"
    assert p["command_name"] == "/review"
    assert p["command_args"] == "src"
    assert p["content"] == "/review src"


def test_assistant_text_maps_to_message_assistant():
    ev = _one({
        "role": "assistant", "type": "text", "content": "the answer",
        "uuid": "a-1", "metadata": {"model": "claude-fable-5",
                                    "timestamp": "2026-07-01T11:00:00Z"},
    })
    assert ev["type"] == "message.assistant"
    p = ev["payload"]
    assert p["kind"] == "text"
    assert p["content"] == "the answer"
    assert p["model"] == "claude-fable-5"
    assert p["uuid"] == "a-1"
    assert p["timestamp"].startswith("2026-07-01T11:00:00")


def test_thinking_maps_to_message_assistant_thinking():
    ev = _one({"role": "assistant", "type": "thinking", "content": "hmm..."})
    assert ev["type"] == "message.assistant"
    assert ev["payload"]["kind"] == "thinking"
    assert ev["payload"]["content"] == "hmm..."


def test_tool_use_with_result_maps_to_use_plus_result():
    events = convert_messages_to_events(SID, [{
        "role": "assistant", "type": "tool_use",
        "toolName": "Bash", "toolId": "tu_1",
        "toolInput": {"command": "ls"},
        "toolResult": "file.txt", "toolStatus": "complete",
        "toolScreenshot": "data:image/png;base64,abc",
    }])
    assert [e["type"] for e in events] == ["tool.use", "tool.result"]
    use, result = events
    assert use["payload"]["tool_use_id"] == "tu_1"
    assert use["payload"]["name"] == "Bash"
    assert use["payload"]["input"] == {"command": "ls"}
    assert use["payload"]["screenshot"] == "data:image/png;base64,abc"
    assert result["payload"]["tool_use_id"] == "tu_1"
    assert result["payload"]["is_error"] is False
    assert result["payload"]["content_preview"] == "file.txt"
    assert all(e["turn_id"] == "backfill" for e in events)


def test_tool_use_error_result_sets_is_error():
    events = convert_messages_to_events(SID, [{
        "role": "assistant", "type": "tool_use",
        "toolName": "Bash", "toolId": "tu_2",
        "toolResult": "boom", "toolStatus": "error",
    }])
    assert events[1]["payload"]["is_error"] is True


def test_tool_use_without_result_emits_use_only():
    events = convert_messages_to_events(SID, [{
        "role": "assistant", "type": "tool_use",
        "toolName": "Read", "toolId": "tu_3", "toolInput": {"file_path": "x"},
    }])
    assert [e["type"] for e in events] == ["tool.use"]


def test_subagent_maps_to_started_progress_completed():
    events = convert_messages_to_events(SID, [{
        "role": "assistant", "type": "subagent",
        "toolId": "task_1", "agentId": "agent-9",
        "agentType": "Explore", "agentDescription": "search the repo",
        "agentPrompt": "find all callers", "agentStatus": "complete",
        "content": "found 3 callers",
        "agentUsage": {"totalTokensOut": 42},
        "agentChildren": [
            {"type": "text", "content": "scanning"},
            {"type": "tool_use", "toolName": "Grep"},
            {"type": "tool_result", "content": "hit"},
        ],
    }])
    assert [e["type"] for e in events] == [
        "subagent.started",
        "subagent.progress", "subagent.progress", "subagent.progress",
        "subagent.completed",
    ]
    started = events[0]["payload"]
    assert started["task_id"] == "task_1"
    assert started["agent_id"] == "agent-9"
    assert started["agent_type"] == "Explore"
    assert started["description"] == "search the repo"
    assert started["prompt_preview"] == "find all callers"
    kinds = [e["payload"]["kind"] for e in events[1:4]]
    assert kinds == ["chunk", "tool_use", "tool_result"]
    assert events[1]["payload"]["payload"]["content"] == "scanning"
    done = events[-1]["payload"]
    assert done["status"] == "completed"
    assert done["result_preview"] == "found 3 callers"
    assert done["usage"] == {"totalTokensOut": 42}


def test_subagent_children_payloads_translated_to_v2_shape():
    """Legacy children carry v1 camelCase keys (toolName/toolId/toolInput/
    toolResult/toolStatus); the converter must emit the snake_case payloads the
    v2 reducer reads (golden shape = team_transcript_watcher's live events)."""
    events = convert_messages_to_events(SID, [{
        "role": "assistant", "type": "subagent",
        "toolId": "task_2", "agentStatus": "complete",
        "agentChildren": [
            {"type": "tool_use", "toolName": "Grep", "toolId": "tu_c1",
             "toolInput": {"pattern": "foo"}},
            {"type": "tool_result", "toolId": "tu_c1",
             "toolResult": "3 hits", "toolStatus": "complete"},
            {"type": "tool_result", "toolId": "tu_c2",
             "content": "boom", "toolStatus": "error"},
        ],
    }])
    use, ok, err = [e["payload"] for e in events[1:4]]
    assert use["kind"] == "tool_use"
    assert use["payload"] == {
        "id": "tu_c1", "name": "Grep", "input": {"pattern": "foo"},
    }
    assert ok["kind"] == "tool_result"
    assert ok["payload"] == {
        "tool_use_id": "tu_c1", "output": "3 hits", "is_error": False,
    }
    # toolResult absent → the child's content is the output; error status maps.
    assert err["payload"] == {
        "tool_use_id": "tu_c2", "output": "boom", "is_error": True,
    }


def test_team_member_children_payloads_translated_to_v2_shape():
    events = convert_messages_to_events(SID, [{
        "role": "assistant", "type": "team_member",
        "teamName": "core", "teammateName": "researcher",
        "agentChildren": [
            {"type": "tool_use", "toolName": "Bash", "toolId": "tu_t1",
             "toolInput": {"command": "ls"}},
            {"type": "tool_result", "toolId": "tu_t1", "toolResult": "ok"},
        ],
    }])
    use, res = [e["payload"] for e in events[1:3]]
    assert use["payload"] == {
        "id": "tu_t1", "name": "Bash", "input": {"command": "ls"},
    }
    assert res["payload"] == {
        "tool_use_id": "tu_t1", "output": "ok", "is_error": False,
    }


@pytest.mark.parametrize("raw,mapped", [
    ("error", "failed"), ("failed", "failed"), ("killed", "killed"),
    ("complete", "completed"), (None, "completed"),
])
def test_subagent_terminal_status_mapping(raw, mapped):
    events = convert_messages_to_events(SID, [{
        "role": "assistant", "type": "subagent",
        "toolId": "t", "agentStatus": raw,
    }])
    assert events[-1]["payload"]["status"] == mapped


def test_team_member_maps_to_member_events():
    events = convert_messages_to_events(SID, [{
        "role": "assistant", "type": "team_member",
        "teamName": "core", "teammateName": "researcher",
        "teammateStatus": "error", "content": "gave up",
        "agentChildren": [{"type": "text", "content": "working"}],
    }])
    assert [e["type"] for e in events] == [
        "team.member_started", "team.member_progress", "team.member_completed",
    ]
    for e in events:
        assert e["payload"]["team_name"] == "core"
        assert e["payload"]["teammate_name"] == "researcher"
    assert events[1]["payload"]["kind"] == "chunk"
    assert events[2]["payload"]["status"] == "failed"
    assert events[2]["payload"]["result_preview"] == "gave up"


def test_system_compaction_boundary_passthrough():
    ev = _one({
        "role": "system", "type": "system",
        "systemSubtype": "compact_boundary",
        "systemData": {"trigger": "auto"},
    })
    assert ev["type"] == "system.compaction_boundary"
    assert ev["payload"]["subtype"] == "compact_boundary"
    assert ev["payload"]["data"] == {"trigger": "auto"}


def test_system_other_maps_to_system_note_with_content():
    ev = _one({
        "role": "system", "type": "system", "content": "hook fired",
        "systemSubtype": "hook_event", "systemData": {"decision": "allow"},
    })
    assert ev["type"] == "system.note"
    assert ev["payload"]["subtype"] == "hook_event"
    assert ev["payload"]["data"]["decision"] == "allow"
    assert ev["payload"]["data"]["content"] == "hook fired"


# ---------------------------------------------------------------------------
# skip rows
# ---------------------------------------------------------------------------

def test_skips_noise_rows():
    events = convert_messages_to_events(SID, [
        {"role": "user", "type": "text",
         "content": "<task-notification>bg task done</task-notification>"},
        {"role": "assistant", "type": "rate_limit", "content": "slow down"},
        {"role": "user", "type": "queued", "content": "queued prompt"},
        {"role": "system", "type": "system", "systemSubtype": "thinking_tokens"},
        {"role": "system", "type": "system", "systemSubtype": "task_progress"},
        {"role": "assistant", "type": "wibble", "content": "unknown type"},
        "not-a-dict",
    ])
    assert events == []


def test_converter_survives_a_poison_message():
    events = convert_messages_to_events(SID, [
        {"role": "assistant", "type": "subagent", "agentChildren": 42,
         "toolId": None},
        {"role": "user", "type": "text", "content": "still converted"},
    ])
    # the poison row is dropped (or partially converted), the good row survives
    assert events[-1]["type"] == "message.user"
    assert events[-1]["payload"]["content"] == "still converted"


def test_all_events_are_backfill_stamped():
    events = convert_messages_to_events(SID, [
        {"role": "user", "type": "text", "content": "a"},
        {"role": "assistant", "type": "text", "content": "b"},
        {"role": "assistant", "type": "tool_use", "toolName": "T",
         "toolId": "x", "toolResult": "r"},
    ])
    assert len(events) == 4
    assert all(e["turn_id"] == "backfill" for e in events)


# ---------------------------------------------------------------------------
# ensure_backfilled
# ---------------------------------------------------------------------------

LEGACY = [
    {"role": "user", "type": "text", "content": "hi", "uuid": "u-1"},
    {"role": "assistant", "type": "text", "content": "hello", "uuid": "a-1"},
]


async def test_ensure_backfilled_appends_converted_history(mock_db, monkeypatch):
    monkeypatch.setattr(hb, "_load_legacy_history", lambda sid: list(LEGACY))
    await ensure_backfilled(SID)
    rows = se.get_events(SID)
    assert [r["event_type"] for r in rows] == ["message.user", "message.assistant"]
    assert [r["seq"] for r in rows] == [1, 2]
    assert all(r["turn_id"] == "backfill" for r in rows)
    assert all(r["run_id"] is None for r in rows)
    assert rows[0]["payload"]["uuid"] == "u-1"


async def test_fast_path_skips_loader_when_log_populated(mock_db, monkeypatch):
    se.append_events(SID, [{"type": "turn.started"}])
    calls = []
    monkeypatch.setattr(
        hb, "_load_legacy_history", lambda sid: calls.append(sid) or []
    )
    await ensure_backfilled(SID)
    assert calls == []
    assert len(se.get_events(SID)) == 1  # nothing added


async def test_empty_memo_suppresses_reload_within_ttl(mock_db, monkeypatch):
    calls = []
    monkeypatch.setattr(
        hb, "_load_legacy_history", lambda sid: calls.append(sid) or []
    )
    await ensure_backfilled(SID)
    await ensure_backfilled(SID)
    assert calls == [SID]  # second call memoed
    # expired memo → loads again
    hb._empty_memo[SID] -= hb._EMPTY_MEMO_TTL_SECONDS + 1
    await ensure_backfilled(SID)
    assert calls == [SID, SID]


async def test_loader_failure_degrades_to_noop(mock_db, monkeypatch):
    def _boom(sid):
        raise RuntimeError("legacy store unavailable")
    monkeypatch.setattr(hb, "_load_legacy_history", _boom)
    await ensure_backfilled(SID)  # must not raise
    assert se.get_events(SID) == []


async def test_lost_race_is_silent(mock_db, monkeypatch):
    monkeypatch.setattr(hb, "_load_legacy_history", lambda sid: list(LEGACY))
    monkeypatch.setattr(
        se, "append_backfill_events", lambda sid, events: None
    )
    await ensure_backfilled(SID)  # must not raise
    assert se.get_events(SID) == []


# ---------------------------------------------------------------------------
# fluid (flag-on) sessions — the transcript no legacy loader can see
# ---------------------------------------------------------------------------

FLUID_SID = "sess-fluid-backfill"
FLUID_SDK_ID = "ses_backfill01"


def _fluid_message(role, text):
    return {
        "role": role,
        "content": [{"kind": "text", "text": text}],
        "provider_meta": {},
    }


def _seed_fluid_session(app_session_id=FLUID_SID, sdk_session_id=FLUID_SDK_ID):
    """A session row bound to a two-node fluid transcript (user + assistant).

    Rows are written exactly as ``PgFluidStore`` writes them (see
    tests/core/test_fluid_transcript.py, which pins the fixture shape against
    fluid's real serializer).
    """
    from psycopg2.extras import Json

    from app.db.base import get_cursor, get_db

    sessions_db.create_session(app_session_id, "default")
    sessions_db.update_session(app_session_id, sdk_session_id=sdk_session_id)

    nodes = [
        ("n1", None, _fluid_message("user", "fluid prompt")),
        ("n2", "n1", _fluid_message("assistant", "fluid reply")),
    ]
    with get_db() as conn:
        cursor = get_cursor(conn)
        cursor.execute(
            "INSERT INTO fluid_sessions (session_id, head_node_id, created_at) "
            "VALUES (%s, %s, %s)",
            (sdk_session_id, nodes[-1][0], 1000.0),
        )
        for i, (node_id, parent_id, message) in enumerate(nodes):
            cursor.execute(
                "INSERT INTO fluid_session_nodes "
                "(session_id, node_id, parent_id, ts, message) "
                "VALUES (%s, %s, %s, %s, %s)",
                (sdk_session_id, node_id, parent_id, 1000.0 + i, Json(message)),
            )
    return app_session_id


async def test_fluid_session_seeds_the_event_feed_from_the_fluid_store(mock_db):
    """The V2 frontend renders from the event feed, so a fluid session whose
    backfill found nothing rendered as an EMPTY conversation (forked/reloaded
    fluid sessions in production). The loader must read fluid's node tree."""
    _seed_fluid_session()

    await ensure_backfilled(FLUID_SID)

    rows = se.get_events(FLUID_SID)
    assert [r["event_type"] for r in rows] == [
        "message.user", "message.assistant",
    ]
    assert [r["payload"]["content"] for r in rows] == [
        "fluid prompt", "fluid reply",
    ]
    assert all(r["turn_id"] == "backfill" for r in rows)
    assert all(r["run_id"] is None for r in rows)


def test_fluid_branch_short_circuits_the_db_loader_fast_path(mock_db, monkeypatch):
    """The fluid lookup is hoisted ABOVE the db_message_loader fast path: for an
    fluid session the legacy table only ever holds a partial (user-rows-only)
    view, and letting it win would shadow the real transcript."""
    _seed_fluid_session()
    import app.core.db_message_loader as dml
    monkeypatch.setattr(
        dml, "load_messages_from_db",
        lambda sid: ([{"role": "user", "type": "text", "content": "LEGACY TRAP"}], True),
    )

    messages = hb._load_legacy_history(FLUID_SID)

    assert [m["content"] for m in messages] == ["fluid prompt", "fluid reply"]


def test_legacy_session_still_uses_the_db_loader(mock_db, monkeypatch):
    """Flag-off safety: a CLI-UUID session never enters the fluid branch."""
    sessions_db.create_session("sess-legacy-bf", "default")
    sessions_db.update_session(
        "sess-legacy-bf", sdk_session_id="8f1c2a34-0000-4000-8000-000000000001"
    )
    import app.core.db_message_loader as dml
    monkeypatch.setattr(
        dml, "load_messages_from_db",
        lambda sid: ([
            {"role": "user", "type": "text", "content": "legacy row"},
            {"role": "assistant", "type": "text", "content": "legacy reply"},
        ], True),
    )

    messages = hb._load_legacy_history("sess-legacy-bf")

    assert [m["content"] for m in messages] == ["legacy row", "legacy reply"]


# ---------------------------------------------------------------------------
# pending-prompt exclusion (first-message duplication fix)
# ---------------------------------------------------------------------------

def _stub_db_loader(monkeypatch, msgs):
    import app.core.db_message_loader as dml
    monkeypatch.setattr(dml, "load_messages_from_db", lambda sid: (msgs, True))


def test_user_only_transcript_is_not_migrated(mock_db, monkeypatch):
    """The pre-submit dual-write is not legacy history: a transcript with only
    user-side rows (the just-sent prompt) yields NO backfill, so the recorder's
    own message.user is the single copy."""
    sessions_db.create_session("sess-pending-1", "default")
    _stub_db_loader(monkeypatch, [
        {"role": "user", "type": None, "content": "hello there"},
    ])

    assert hb._load_legacy_history("sess-pending-1") == []


def test_pending_turn_prompt_is_trimmed_from_real_history(mock_db, monkeypatch):
    """A legacy session with real history: the trailing row matching the
    queued/live turn's prompt is trimmed; the history itself survives."""
    sessions_db.create_session("sess-pending-2", "default")
    _stub_db_loader(monkeypatch, [
        {"role": "user", "type": None, "content": "old question"},
        {"role": "assistant", "type": "text", "content": "old answer"},
        {"role": "user", "type": None, "content": "new prompt"},
    ])
    import app.db.turns as turns_db
    monkeypatch.setattr(turns_db, "get_queued_turns", lambda sid: [])
    monkeypatch.setattr(
        turns_db, "get_live_turn", lambda sid: {"prompt": "new prompt"}
    )

    messages = hb._load_legacy_history("sess-pending-2")

    assert [m["content"] for m in messages] == ["old question", "old answer"]


def test_identical_earlier_content_is_not_trimmed(mock_db, monkeypatch):
    """Only TRAILING rows are trimmed — a historical message that happens to
    equal the pending prompt stays."""
    sessions_db.create_session("sess-pending-3", "default")
    _stub_db_loader(monkeypatch, [
        {"role": "user", "type": None, "content": "same text"},
        {"role": "assistant", "type": "text", "content": "answered"},
    ])
    import app.db.turns as turns_db
    monkeypatch.setattr(turns_db, "get_queued_turns", lambda sid: [])
    monkeypatch.setattr(
        turns_db, "get_live_turn", lambda sid: {"prompt": "same text"}
    )

    messages = hb._load_legacy_history("sess-pending-3")

    assert [m["content"] for m in messages] == ["same text", "answered"]


# ---------------------------------------------------------------------------
# counter-gate race
# ---------------------------------------------------------------------------

def _bf_events(n=2, tag="bf"):
    return [
        {"type": "message.user", "turn_id": "backfill",
         "payload": {"content": f"{tag}-{i}"}}
        for i in range(n)
    ]


async def test_concurrent_backfills_exactly_one_wins(mock_db):
    results = await asyncio.gather(
        asyncio.to_thread(se.append_backfill_events, SID, _bf_events(2, "a")),
        asyncio.to_thread(se.append_backfill_events, SID, _bf_events(2, "b")),
    )
    assert sorted(r is None for r in results) == [False, True]
    winner = next(r for r in results if r is not None)
    assert winner == (1, 2)
    rows = se.get_events(SID)
    assert len(rows) == 2  # the loser wrote NOTHING
    tags = {r["payload"]["content"][0] for r in rows}
    assert len(tags) == 1  # no interleaving of the two batches


async def test_backfill_aborts_after_recorder_append(mock_db):
    se.append_events(SID, [{"type": "turn.started"}])
    assert se.append_backfill_events(SID, _bf_events()) is None
    assert [r["event_type"] for r in se.get_events(SID)] == ["turn.started"]


async def test_recorder_append_continues_after_backfill(mock_db):
    assert se.append_backfill_events(SID, _bf_events(3)) == (1, 3)
    assert se.append_events(SID, [{"type": "turn.started"}]) == (4, 4)
    assert [r["seq"] for r in se.get_events(SID)] == [1, 2, 3, 4]
