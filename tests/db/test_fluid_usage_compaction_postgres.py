"""Idle-session Usage snapshot compaction in the Fluid event log (#1327), on real PostgreSQL 16.

The retention walk rewrites a long-idle session's ``Usage.context_usage`` to
exactly ``compact_context_usage(snapshot)`` and leaves everything else alone.
"""
import asyncio
import copy
import json
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import psycopg2
import pytest
from psycopg2.extras import Json, RealDictCursor

from app.core.transcript_replay import compact_context_usage
from app.db import database, fluid_events, retention, session_events
from tests.db.schema_lifecycle_helpers import pg16_bindir, private_postgres


@pytest.fixture(scope="module")
def cluster(tmp_path_factory):
    bindir = pg16_bindir()
    if not bindir:
        pytest.skip("PostgreSQL 16 binaries required")
    with private_postgres(bindir, tmp_path_factory.mktemp("usage-compaction-pg")) as descriptor:
        yield descriptor


@pytest.fixture
def pg(cluster, monkeypatch):
    args = dict(host="127.0.0.1", port=cluster["port"], user=cluster["user"],
                password=cluster["password"], dbname=cluster["database"],
                cursor_factory=RealDictCursor)

    def open_connection():
        return psycopg2.connect(**args)

    @contextmanager
    def connect():
        conn = open_connection()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    with connect() as conn:
        cur = conn.cursor()
        cur.execute("DROP TABLE IF EXISTS fluid_session_events, sessions")
        cur.execute("CREATE TABLE sessions(id TEXT PRIMARY KEY, sdk_session_id TEXT, updated_at TIMESTAMPTZ)")
        cur.execute("CREATE TABLE fluid_session_events(session_id TEXT, seq BIGINT, event JSONB, "
                    "PRIMARY KEY(session_id,seq))")
    monkeypatch.setattr(retention, "get_db", connect)
    monkeypatch.setattr(fluid_events, "BATCH_SIZE", 2)
    return connect, open_connection


def delegated_snapshot():
    """The delegated (Claude CLI /context) shape, with the heavy breakdown keys."""
    return {
        "categories": [
            {"name": "System prompt", "tokens": 3100, "color": "promptBorder", "isDeferred": False,
             "description": "extra", "items": [{"name": "x", "tokens": 1}]},
            {"name": "Messages", "tokens": 41000, "color": "purple_FOR_SUBAGENTS_ONLY"},
            {"name": "MCP tools", "tokens": 9000, "color": "cyan", "isDeferred": True, "tools": ["a"]},
        ],
        "totalTokens": 53100, "maxTokens": 200000, "rawMaxTokens": 200000, "percentage": 27,
        "model": "claude-opus-4", "isAutoCompactEnabled": True,
        "gridRows": [[{"color": "cyan", "isFilled": True, "categoryName": "MCP tools"}] * 20] * 10,
        "mcpTools": [{"name": f"mcp__srv__tool{i}", "serverName": "srv", "tokens": 300} for i in range(25)],
        "skills": {"totalSkills": 12, "tokens": 900,
                   "skillFrontmatter": [{"name": f"s{i}", "tokens": 70} for i in range(12)]},
        "messageBreakdown": {"toolCallTokens": 900, "toolResultTokens": 22000,
                             "toolCallsByType": [{"name": "Bash", "callTokens": 10}]},
        "apiUsage": {"input_tokens": 3, "cache_read_input_tokens": 50000},
        "memoryFiles": [{"path": "/x/CLAUDE.md", "type": "Project", "tokens": 400}],
        "agents": [], "slashCommands": {"totalCommands": 4, "tokens": 120},
    }


def native_snapshot():
    """The native engine shape (fluid.context.usage)."""
    return {
        "categories": [{"name": "System prompt", "tokens": 2500, "color": "promptBorder"},
                       {"name": "Messages", "tokens": 12000, "color": "purple"}],
        "totalTokens": 14500, "maxTokens": 400000, "percentage": 3.6, "model": "gpt-5",
        "measured": True, "compaction": {"tier": 1, "events": 2},
        "systemTools": [{"name": "Read", "tokens": 300}, {"name": "Edit", "tokens": 280}],
        "deferredBuiltinTools": [{"name": "WebFetch", "tokens": 90}],
    }


EDGE_SNAPSHOTS = {
    "unknown_extra": {"model": "m", "totalTokens": 5, "brandNewKey": {"deep": [1, 2]}},
    "categories_not_list": {"model": "m", "categories": {"name": "odd"}, "gridRows": []},
    "mixed_categories": {"categories": [{"name": "A", "tokens": 1, "x": 2}, "loose", 7, None, []],
                         "mcpTools": []},
    "empty_categories": {"categories": [], "skills": {}},
    "category_without_kept_keys": {"categories": [{"zzz": 1}], "model": "m"},
    "no_kept_keys": {"gridRows": [[1]], "mcpTools": [1]},
}


def usage_event(session, seq, snapshot, *, missing=False):
    event = {
        "_cls": "Usage", "_v": 1, "session_id": session, "seq": seq, "ts": 1731000000.5 + seq,
        "turn_id": f"turn-{seq}", "node_id": f"node-{seq}", "input_tokens": 3,
        "output_tokens": 410, "cache_read_tokens": 50000, "cache_creation_tokens": 1200,
        "raw": {"cost_usd": 0.0123, "prefix_hashes": {"system": "ab", "tools": "cd"},
                "reuse_floor_tokens": 1024, "service_tier": "standard"},
        "future_field": {"kept": True},
    }
    if not missing:
        event["context_usage"] = snapshot
    return event


def seed_session(connect, name, events, *, idle_days=30):
    with connect() as conn:
        cur = conn.cursor()
        cur.execute("INSERT INTO sessions VALUES (%s,%s,%s)",
                    (name, name, datetime.now(timezone.utc) - timedelta(days=idle_days)))
        for seq, event in enumerate(events):
            cur.execute("INSERT INTO fluid_session_events VALUES (%s,%s,%s)", (name, seq, Json(event)))


def stored(connect, session=None):
    """{(session, seq): (event, xmin, stored bytes)}; xmin changes only when a row is rewritten."""
    with connect() as conn:
        cur = conn.cursor()
        where = " WHERE session_id = %s" if session else ""
        cur.execute("SELECT session_id, seq, event, xmin::text AS xmin, "
                    "pg_column_size(event) AS bytes FROM fluid_session_events" + where
                    + " ORDER BY session_id, seq", (session,) if session else None)
        return {(r["session_id"], r["seq"]): (r["event"], r["xmin"], r["bytes"]) for r in cur.fetchall()}


def short_timeout(cur):
    cur.execute("SET LOCAL statement_timeout = '100ms'")
    cur.execute("SET LOCAL lock_timeout = 0")


def test_idle_usage_snapshots_become_exactly_the_compact_form(pg):
    connect, _ = pg
    snapshots = {"delegated": delegated_snapshot(), "native": native_snapshot(), **EDGE_SNAPSHOTS}
    events = [usage_event("idle", seq, copy.deepcopy(snap)) for seq, snap in enumerate(snapshots.values())]
    seed_session(connect, "idle", events)
    before = stored(connect)

    result = fluid_events.maintain_fluid_event_log()

    assert result == {"pruned": 0, "compacted": len(snapshots)}
    after = stored(connect)
    for seq, (name, snapshot) in enumerate(snapshots.items()):
        event = after[("idle", seq)][0]
        expected = dict(events[seq], context_usage=compact_context_usage(snapshot))
        # Exactly the Python compact form (jsonb key order aside), every other key intact.
        assert event == expected, name
        assert {k: v for k, v in event.items() if k != "context_usage"} == \
            {k: v for k, v in before[("idle", seq)][0].items() if k != "context_usage"}
    # The heavy snapshots shrink: stored (compressed) bytes, and decoded bytes by far.
    for seq in (0, 1):
        assert after[("idle", seq)][2] < before[("idle", seq)][2]
    decoded = [len(json.dumps(rows[("idle", 0)][0])) for rows in (before, after)]
    assert decoded[1] < decoded[0] / 4


def test_compacted_rows_are_never_rewritten_again(pg):
    connect, _ = pg
    seed_session(connect, "idle", [usage_event("idle", 0, delegated_snapshot()),
                                   usage_event("idle", 1, native_snapshot())])
    assert fluid_events.maintain_fluid_event_log()["compacted"] == 2
    first = stored(connect)

    assert fluid_events.maintain_fluid_event_log() == {"pruned": 0, "compacted": 0}
    # Same row versions: the skip predicate matched nothing, so nothing was rewritten.
    assert stored(connect) == first


def test_null_missing_and_already_compact_snapshots_are_untouched(pg):
    connect, _ = pg
    compact = compact_context_usage(delegated_snapshot())
    seed_session(connect, "idle", [
        usage_event("idle", 0, None),
        usage_event("idle", 1, None, missing=True),
        usage_event("idle", 2, compact),
        usage_event("idle", 3, "not an object"),
        usage_event("idle", 4, [1, 2]),
    ])
    before = stored(connect)
    assert fluid_events.maintain_fluid_event_log() == {"pruned": 0, "compacted": 0}
    assert stored(connect) == before


def test_recent_sessions_and_other_classes_are_untouched(pg):
    connect, _ = pg
    seed_session(connect, "recent", [usage_event("recent", 0, delegated_snapshot())], idle_days=1)
    other = {"_cls": "ToolResult", "context_usage": delegated_snapshot(), "content": "x"}
    seed_session(connect, "idle", [
        {"_cls": "TextDelta", "text": "a"},
        dict(other),
        usage_event("idle", 2, delegated_snapshot()),
        {"_cls": "TurnComplete", "context_usage": delegated_snapshot()},
    ])
    before = stored(connect)

    assert fluid_events.maintain_fluid_event_log() == {"pruned": 1, "compacted": 1}

    after = stored(connect)
    assert after[("recent", 0)] == before[("recent", 0)]
    assert ("idle", 0) not in after  # The delta prune still does what it did.
    assert after[("idle", 1)] == before[("idle", 1)]
    assert after[("idle", 3)] == before[("idle", 3)]
    assert after[("idle", 2)][0]["context_usage"] == compact_context_usage(delegated_snapshot())


def test_prune_only_entry_never_compacts(pg):
    connect, _ = pg
    seed_session(connect, "idle", [usage_event("idle", 0, delegated_snapshot())])
    before = stored(connect)
    assert fluid_events.prune_fluid_delta_events() == 0
    assert stored(connect) == before


def test_compacted_event_loads_as_usage_and_native_rehydration_reads_model(pg):
    from fluid.engine.native import NativeLoop
    from fluid.events.model import Usage, event_from_dict

    connect, _ = pg
    seed_session(connect, "idle", [usage_event("idle", 0, delegated_snapshot())])
    fluid_events.maintain_fluid_event_log()
    raw = stored(connect)[("idle", 0)][0]

    event = event_from_dict(raw)
    assert isinstance(event, Usage)
    assert event.context_usage == compact_context_usage(delegated_snapshot())
    assert (event.input_tokens, event.cache_read_tokens, event.cache_creation_tokens) == (3, 50000, 1200)

    class Store:
        async def iter_events_of_class(self, session_id, seq, classes):
            if isinstance(event, classes):
                yield event

    engine = SimpleNamespace(
        store=Store(), _usage_rehydrated=set(), _unbooked_usage={}, _last_call_usage={},
        _last_call_model={}, _last_call_state={},
        _seed_p_cold=lambda sid, events: None, _retry_unbooked_usage=lambda: None,
    )
    recorded = asyncio.run(NativeLoop._rehydrate_last_call_usage(
        engine, "idle", [SimpleNamespace(node_id="node-0")]))
    assert recorded is not None and recorded[0] == "node-0"
    assert engine._last_call_model["idle"] == "claude-opus-4"


@pytest.mark.parametrize("window", [0, -1])
def test_disabled_window_does_nothing(pg, window):
    connect, _ = pg
    seed_session(connect, "idle", [usage_event("idle", 0, delegated_snapshot())], idle_days=365)
    before = stored(connect)
    assert fluid_events.maintain_fluid_event_log(window) == {"pruned": 0, "compacted": 0}
    assert stored(connect) == before


def test_stop_mid_pass_stops_cleanly_and_the_next_pass_finishes(pg, monkeypatch):
    connect, _ = pg
    for name in ("a", "b"):
        seed_session(connect, name, [usage_event(name, s, delegated_snapshot()) for s in range(4)])
    stop = threading.Event()
    original = retention.get_db

    @contextmanager
    def stop_after_first_compaction():
        with original() as conn:
            yield conn
        # A range transaction committed a compaction: request a stop.
        if any(row[0]["context_usage"].get("gridRows") is None for row in stored(connect).values()):
            stop.set()

    monkeypatch.setattr(retention, "get_db", stop_after_first_compaction)
    assert fluid_events.maintain_fluid_event_log(stop=stop) == {"pruned": 0, "compacted": 2}
    rows = stored(connect)
    compacted = sorted(k for k, v in rows.items() if "gridRows" not in v[0]["context_usage"])
    assert compacted == [("a", 0), ("a", 1)]  # One whole range, nothing half-done.

    monkeypatch.setattr(retention, "get_db", original)
    assert fluid_events.maintain_fluid_event_log() == {"pruned": 0, "compacted": 6}


def test_locked_usage_row_skips_its_range_and_is_compacted_next_pass(pg, monkeypatch, caplog):
    """Mirrors #1422: a blocked range must not stop later ranges or later sessions."""
    connect, open_connection = pg
    seed_session(connect, "a", [
        usage_event("a", 0, delegated_snapshot()), usage_event("a", 1, delegated_snapshot()),
        {"_cls": "ToolArgsDelta", "delta": "x"}, usage_event("a", 3, delegated_snapshot()),
        usage_event("a", 4, delegated_snapshot()), usage_event("a", 5, delegated_snapshot()),
    ])
    seed_session(connect, "b", [usage_event("b", 0, delegated_snapshot())])
    monkeypatch.setattr(fluid_events, "set_timeout", short_timeout)
    blocker = open_connection()
    try:
        blocker.cursor().execute(
            "SELECT seq FROM fluid_session_events WHERE session_id='a' AND seq=3 FOR UPDATE")
        with pytest.raises(RuntimeError, match=r"1 session.*pruned 0 rows and compacted 5 Usage.*will retry"):
            fluid_events.maintain_fluid_event_log()
        assert "skipping to the next range" in caplog.text
        rows = stored(connect)
        # The blocked range [2,3] rolled back whole: its delta and its Usage row stay fully old.
        assert rows[("a", 2)][0]["_cls"] == "ToolArgsDelta"
        assert rows[("a", 3)][0]["context_usage"] == delegated_snapshot()
        for key in (("a", 0), ("a", 1), ("a", 4), ("a", 5), ("b", 0)):
            assert rows[key][0]["context_usage"] == compact_context_usage(delegated_snapshot()), key
    finally:
        blocker.rollback()
        blocker.close()
    assert fluid_events.maintain_fluid_event_log() == {"pruned": 1, "compacted": 1}
    assert stored(connect)[("a", 3)][0]["context_usage"] == compact_context_usage(delegated_snapshot())


def test_update_rechecks_idleness(pg, monkeypatch):
    connect, _ = pg
    seed_session(connect, "a", [usage_event("a", 0, delegated_snapshot())])
    before = stored(connect)
    original = fluid_events.set_timeout
    calls = 0

    def resume_between_selection_and_update(cur):
        nonlocal calls
        original(cur)
        calls += 1
        if calls == 2:  # The range transaction: the session wakes up before its UPDATE.
            with connect() as writer:
                writer.cursor().execute("UPDATE sessions SET updated_at=now() WHERE id='a'")

    monkeypatch.setattr(fluid_events, "set_timeout", resume_between_selection_and_update)
    assert fluid_events.maintain_fluid_event_log() == {"pruned": 0, "compacted": 0}
    assert stored(connect) == before


def test_maintenance_job_compacts_and_reports(pg, monkeypatch):
    """The retention job drives the step and reports its count beside the delta prune."""
    from app.core.config import settings
    from app.db import base

    connect, open_connection = pg
    seed_session(connect, "idle", [{"_cls": "TextDelta", "text": "a"},
                                   usage_event("idle", 1, delegated_snapshot())])
    monkeypatch.setattr(settings, "fluid_event_retention_days", 7, raising=False)
    # Neighbouring passes and the pool are not under test; ANALYZE/VACUUM run on
    # this disposable cluster, never the process-global pool.
    monkeypatch.setattr(retention, "mutate_batches", lambda *a, **k: 0)
    monkeypatch.setattr(database, "_prune_notifications_cap", lambda *a, **k: 0)
    monkeypatch.setattr(session_events, "compact_idle_session_deltas", lambda **k: {"rows": 0})
    monkeypatch.setattr(database, "get_db", connect)
    monkeypatch.setattr(database, "get_connection", open_connection)
    monkeypatch.setattr(database, "exempt_from_start_pressure", lambda conn: None)
    monkeypatch.setattr(base, "_close_connection", lambda conn: conn.close())

    results = database.run_database_maintenance(skip_vacuum=True)

    assert stored(connect)[("idle", 1)][0]["context_usage"] == compact_context_usage(delegated_snapshot())
    assert results["cleaned_fluid_delta_events"] == 1
    assert results["compacted_fluid_usage_snapshots"] == 1
