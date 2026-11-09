"""Host identity and owner-only gap source: no live stores or private transcripts."""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.core.time_awareness import preference_identity, previous_owner_time, session_time_context


def row(source="user", **flags):
    return {"created_at": "2026-11-08T23:00:00+00:00", "payload": {"source": source, **flags}}


@pytest.mark.parametrize("source", ["user", "queue"])
def test_current_owner_message_is_not_its_own_previous_message(source):
    current = row(source)
    current["created_at"] = "2026-11-09T18:00:00+00:00"
    older = row()
    assert previous_owner_time([current, row("agent_message"), row("wakeup"), older], source) == older["created_at"]
    assert previous_owner_time([current], source) is None
    assert previous_owner_time([current, older], source, "pre-upgrade-queue") == older["created_at"]


@pytest.mark.parametrize("source", ["unpark", "wakeup", "agent_message", "automation", "handoff", "resume"])
def test_wakes_keep_last_real_owner_message(source):
    rows = [row("wakeup"), row("agent_message"), row(agent_wake=True), row(not_sent=True), row()]
    assert previous_owner_time(rows, source) == rows[-1]["created_at"]
    rows[-1]["payload"]["owner_input_id"] = "earlier-host-submit"
    assert previous_owner_time(rows, source, "earlier-host-submit") == rows[-1]["created_at"]


@pytest.fixture
def owner_event_lookup(monkeypatch):
    """Execute the production bounded query, not a prefiltered Python fixture."""
    import json
    import sqlite3
    from contextlib import contextmanager
    from app.core import time_awareness as host
    from app.db import database

    def lookup(events, source, current_input_id):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE session_events (session_id TEXT, seq INT, event_type TEXT, created_at TEXT, payload TEXT)")
        conn.execute("CREATE INDEX idx_session_events_session_seq ON session_events(session_id, seq)")
        conn.executemany("INSERT INTO session_events VALUES ('owner', ?, 'message.user', ?, ?)",
                         [(i, at, json.dumps(payload)) for i, (at, payload) in enumerate(events)])
        calls, plans = [], []
        class Cursor:
            rows = []
            def execute(self, sql, params=()):
                calls.append((sql, params))
                self.rows = []
                if "FROM session_events" in sql:
                    sql = sql.replace("%s", "?")
                    self.rows = [dict(r) for r in conn.execute(sql, params)]
                    plans.extend(r[3] for r in conn.execute("EXPLAIN QUERY PLAN " + sql, params))
            def fetchone(self):
                return None
            def fetchall(self):
                return self.rows
        cursor = Cursor()
        @contextmanager
        def db():
            yield SimpleNamespace(cursor=lambda: cursor)
        with monkeypatch.context() as patch:
            patch.setattr(database, "get_session", lambda _: {})
            patch.setattr(host, "get_db", db)
            patch.setattr(host, "preference_identity", lambda *_: ("admin", "fixture"))
            result = host.session_time_context("owner", source, current_input_id)
        conn.close()
        return result, calls, plans
    return lookup


@pytest.mark.parametrize("source, current_id, receipts", [
    ("user", None, [None, None]), ("queue", None, [None, None]),
    ("user", "current", [None, "current"]),
    ("queue", "current", ["older", "current"]),
    ("user", "current", [None, "current", "current"]),
    ("queue", "current", ["older", "current", "current"]),
    ("wakeup", "current", [None, "current", "current"]),
])
def test_owner_lookup_excludes_current_receipts_before_limit(owner_event_lookup, source, current_id, receipts):
    from fluid.context.time_awareness import as_utc, turn_time_lines
    previous, current = "2026-11-09T00:00:00Z", "2026-11-09T18:00:00Z"
    events = []
    for i, receipt in enumerate(receipts):
        payload = {"source": "user"}
        if receipt:
            payload.update(owner_input_id=receipt, owner_message_at=previous if i == 0 else current)
        events.append((previous if i == 0 else current, payload))
    result, calls, plans = owner_event_lookup(events, source, current_id)
    expected = current if source == "wakeup" else previous
    assert as_utc(result["last_owner_at"]) == as_utc(expected)
    if source != "wakeup":
        assert "18 hours ago" in turn_time_lines(zone="UTC", last_owner_at=result["last_owner_at"], now=as_utc(current))[1]
    sql, params = calls[-1]
    assert sql.endswith("ORDER BY seq DESC LIMIT 2")
    assert len(params) == (2 if current_id and source != "wakeup" else 1)
    assert calls[0][0] == "SET LOCAL statement_timeout = '250ms'"
    assert any("idx_session_events_session_seq" in plan for plan in plans)
    assert not any("TEMP B-TREE" in plan for plan in plans)


@pytest.mark.asyncio
@pytest.mark.parametrize("already_recorded", [False, True])
async def test_old_queue_id_is_not_an_identified_current_receipt(mock_db, owner_event_lookup, already_recorded):
    from app.db import turns
    from app.core import session_runtime
    from tests.core.test_send_steering import _seed_session, _pump_env
    sid = _seed_session("old-queue-time")
    turn = turns.enqueue_turn(sid, "user", prompt="tomorrow",
                              payload={"user_message_already_recorded": already_recorded})
    # An actual pre-upgrade queue row has an ID but no host receipt in payload.
    turn["payload"].pop("owner_input_id")
    turn["payload"].pop("owner_message_at")
    # execute() reclaims the durable row; changing only the local dict would
    # accidentally test a modern receipt after the refresh.
    import json
    from app.db.base import get_db
    with get_db() as conn:
        conn.cursor().execute("UPDATE turns SET payload=%s WHERE id=%s", (json.dumps(turn["payload"]), turn["id"]))
    captured = {}
    async def pump(**kwargs):
        captured.update(kwargs["overrides"])
        yield {"type": "done", "metadata": {}}
    with _pump_env(pump) as env:
        await session_runtime.SessionRuntime(turn).execute()
    assert turn["id"]
    assert captured["owner_input_id"] is None
    assert env["recorder"].start.call_args.kwargs["owner_receipt"]["owner_input_id"] == turn["id"]
    events = [("2026-11-09T00:00:00Z", {"source": "user"}),
              ("2026-11-09T18:00:00Z", {"source": "user"})]
    result, _, _ = owner_event_lookup(events, "user", captured["owner_input_id"])
    assert result["last_owner_at"] == events[0][0]


def test_preference_identity_is_same_namespace_as_settings():
    from app.core.preference_identity import principal_preference_identity
    owner = principal_preference_identity({"kind": "admin"})
    assert preference_identity({}) == owner
    assert preference_identity({"principal_type": "paired", "principal_id": "device"}) == owner
    assert preference_identity({"api_user_id": "api-id"}) == principal_preference_identity({"kind": "api_user", "principal_id": "api-id"})
    paired = {"kind": "paired", "principal_id": "phone", "account_id": "owner-account"}
    assert preference_identity({}, paired) == principal_preference_identity(paired)
    assert preference_identity({"provenance_owner": "account:owner-account"}) == principal_preference_identity(paired)


def test_host_read_failure_is_quiet_fallback(monkeypatch):
    from app.db import database
    monkeypatch.setattr(database, "get_session", lambda _: (_ for _ in ()).throw(RuntimeError("unavailable")))
    assert session_time_context("fixture", "unpark") == {}


@pytest.mark.parametrize("preset", [True, False])
def test_each_delegated_leg_has_time_and_standing_rule(monkeypatch, tmp_path, preset):
    from app.core.provider.harness_adapter import HarnessAdapter
    from app.core.provider.types import SessionOptions
    from fluid.context.time_awareness import RELATIVE_TIME_RULE
    adapter = HarnessAdapter()
    adapter._options = SessionOptions(cwd=str(tmp_path), model="fake-model")
    adapter._is_delegated = True
    adapter._delegated_replaces_preset = not preset
    monkeypatch.setattr(adapter, "_time_context", lambda source="user": {"zone": "America/New_York"})
    note = adapter._env_note()
    assert "Current time:" in note and "America/New_York" in note
    assert (RELATIVE_TIME_RULE in note) is preset  # replaced-preset rule lives in the fixed compiler section

@pytest.mark.parametrize("worker", [False, True])
@pytest.mark.parametrize("explicit,resolved,expected", [
    ("America/New_York", "Asia/Tokyo", "America/New_York"),
    ("local", "America/New_York", "America/New_York"),
    (None, None, None),
])
def test_host_store_read_timezone_precedence_and_worker_owner_lineage(monkeypatch, worker, explicit, resolved, expected):
    from contextlib import contextmanager
    import app.core.time_awareness as host
    from app.db import database

    calls = []
    class Cursor:
        sql = ""
        def execute(self, sql, params=()):
            self.sql = sql
            calls.append((sql, params))
        def fetchone(self):
            return {"id": "owner-session", "is_worker": False} if "WITH RECURSIVE" in self.sql else None
        def fetchall(self):
            if "user_preferences" in self.sql:
                return [{"key": "timezone", "value": explicit}, {"key": "timezone_resolved", "value": resolved}]
            return [row(), {"created_at": "2026-11-07T23:00:00+00:00", "payload": {"source": "user"}}]
    cursor = Cursor()
    @contextmanager
    def db():
        yield SimpleNamespace(cursor=lambda: cursor)
    monkeypatch.setattr(database, "get_session", lambda _: {"is_worker": worker})
    monkeypatch.setattr(host, "get_db", db)
    monkeypatch.setattr(host, "preference_identity", lambda *_: ("admin", "fixture"))
    result = host.session_time_context("child" if worker else "owner-session", "user")
    assert result["zone"] == expected
    assert result["last_owner_at"] == ("2026-11-08T23:00:00+00:00" if worker else "2026-11-07T23:00:00+00:00")
    assert calls[-1][1] == ("owner-session",)
    assert "LIMIT 2" in calls[-1][0] and "statement_timeout" in calls[0][0]


def test_pg_recall_accumulator_keeps_the_message_timestamp():
    from app.core.provider.fluid_pg_store import _accumulate
    acc = {}
    _accumulate(acc, "fixture", "user", ["ExampleWorker tomorrow"], 123.0, ["exampleworker"])
    _accumulate(acc, "fixture", "user", ["ExampleWorker today"], 456.0, ["exampleworker"])
    assert [(item[2], item[3]) for item in acc["fixture"]["texts"]] == [
        ("ExampleWorker tomorrow", 123.0), ("ExampleWorker today", 456.0)]

@pytest.mark.parametrize("source", ["user", "queue"])
def test_queued_acceptance_receipt_survives_delivery_and_retry(mock_db, monkeypatch, source):
    from app.db import turns
    from app.core.time_awareness import owner_message_receipt
    from fluid.context.time_awareness import turn_time_lines

    accepted = datetime.fromisoformat("2026-11-09T04:55:00+00:00").timestamp()
    delivered = datetime.fromisoformat("2026-11-09T05:10:00+00:00")
    monkeypatch.setattr(turns.time, "time", lambda: accepted)
    queued = turns.enqueue_turn("receipt", source, prompt="tomorrow",
                               payload={"owner_message_at": 1, "owner_input_id": "forged"})
    assert queued["payload"]["owner_message_at"] == accepted
    assert queued["payload"]["owner_input_id"] == queued["id"]
    # A new connection/process can recover the receipt from the durable queue.
    recovered = turns.get_turn(queued["id"])
    receipt = owner_message_receipt(recovered)
    assert receipt == {"owner_message_at": accepted, "owner_input_id": queued["id"]}
    monkeypatch.setattr(turns.time, "time", lambda: delivered.timestamp())
    retry = turns.enqueue_turn("receipt", source, prompt="tomorrow", at_front=True,
                               payload={"user_message_already_recorded": True}, _owner_receipt=receipt)
    assert owner_message_receipt(retry) == receipt
    event = {"created_at": delivered, "payload": {"source": source, **receipt}}
    last = previous_owner_time([event], "wakeup", receipt["owner_input_id"])
    lines = turn_time_lines(zone="America/New_York", last_owner_at=last,
                            now=datetime.fromisoformat("2026-11-09T05:20:00+00:00"))
    assert "Sun Nov 8, 2026, 11:55 PM EST (25 minutes ago)" in lines[1]
    assert previous_owner_time([event], source, receipt["owner_input_id"]) is None
    # A later concurrent receipt must not be skipped in place of this input.
    newer = {"created_at": delivered, "payload": {"source": "user", "owner_input_id": "other"}}
    assert previous_owner_time([newer, event], source, receipt["owner_input_id"]) == delivered
    # No accepted-at metadata was present before this upgrade.
    assert owner_message_receipt({"id": "old", "created_at": delivered})["owner_message_at"] == delivered.timestamp()


def test_accepted_receipt_gap_across_dst_fold():
    from fluid.context.time_awareness import turn_time_lines
    last = previous_owner_time([row(owner_message_at="2026-11-01T04:30:00Z")], "wakeup")
    lines = turn_time_lines(zone="America/New_York", last_owner_at=last,
                            now=datetime.fromisoformat("2026-11-01T06:30:00+00:00"))
    assert "12:30 AM EDT (2 hours ago)" in lines[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("delegated", [False, True])
async def test_away_resume_is_a_non_owner_submit_on_both_legs(monkeypatch, tmp_path, delegated):
    from app.core.provider.harness_adapter import HarnessAdapter
    from app.core.provider.types import SessionOptions
    from app.core.away_mode import OWNER_BACK_NOTE
    from unittest.mock import AsyncMock
    adapter = HarnessAdapter()
    adapter._options = SessionOptions(cwd=str(tmp_path), model="fake-model")
    adapter._is_delegated = delegated
    adapter._engine = SimpleNamespace()
    monkeypatch.setattr(adapter, "_away_note", lambda: OWNER_BACK_NOTE)
    sent = AsyncMock()
    monkeypatch.setattr(adapter, "send_query", sent)
    await adapter.send_resume()
    assert sent.call_args.kwargs == {"_source": "wakeup"}
    assert previous_owner_time([row()], sent.call_args.kwargs["_source"]) == row()["created_at"]


@pytest.mark.asyncio
async def test_recorder_persists_acceptance_time_for_opening_upload_and_steer(monkeypatch):
    from app.core.turn_recorder import TurnRecorder
    from unittest.mock import AsyncMock
    from app.core.time_awareness import owner_message_receipt
    recorder = TurnRecorder("receipt-session", "receipt-turn", "run", 1, "runner")
    events = []
    async def append(rows):
        events.extend(rows)
    monkeypatch.setattr(recorder, "_append", append)
    receipt = owner_message_receipt({"id": "input", "created_at": "2026-11-09T04:55:00Z"})
    await recorder.start("tomorrow", owner_receipt=receipt)
    await recorder.record_user_message("upload tomorrow", extra_payload=receipt)
    await recorder.record_steered_user_message("steer tomorrow", owner_receipt=receipt)
    messages = [event for event in events if event.get("type") == "message.user"]
    assert len(messages) == 3
    for event in messages:
        assert all(event["payload"][key] == value for key, value in receipt.items())


@pytest.mark.asyncio
async def test_unsent_deferred_upload_keeps_host_receipt(mock_db):
    from app.db import turns
    from app.core import session_runtime
    from app.core.time_awareness import owner_message_receipt
    from tests.core.test_send_steering import _seed_session, _pump_env, _pump
    _seed_session("unsent-time")
    turn = turns.enqueue_turn("unsent-time", "user", prompt="tomorrow",
                              payload={"defer_user_record": True, "upload_blob_ids": ["pending"]})
    # This pump exits before preparing the upload. Recoverable words remain
    # not_sent, but their host acceptance time must not become delivery time.
    with _pump_env(_pump([{"type": "done", "metadata": {}}])) as env:
        await session_runtime.SessionRuntime(turn).execute()
    extra = env["recorder"].record_user_message.call_args.kwargs["extra_payload"]
    assert extra["not_sent"] is True
    for key, value in owner_message_receipt(turn).items():
        assert extra[key] == value


@pytest.mark.parametrize("key", ["timezone", "timezone_resolved"])
def test_timezone_sync_materializes_in_receiving_worker_owner_namespace(monkeypatch, key):
    # Exercise the existing shared user_preferences wire hooks, including the
    # different local admin identities on ExampleWorker and OtherWorker. No new protocol.
    from app.network import policy
    monkeypatch.setattr(policy, "_owner_identity", lambda: "exampleworker-owner")
    wire = policy._serialize_preference({"user_type": "admin", "user_id": "exampleworker-owner",
                                         "key": key, "value": "America/New_York"})
    assert wire["user_id"] == policy.OWNER_PLACEHOLDER
    monkeypatch.setattr(policy, "_owner_identity", lambda: "otherworker-owner")
    received = policy._materialize_preference(wire)
    assert received == {"user_type": "admin", "user_id": "otherworker-owner", "key": key,
                        "value": "America/New_York"}


@pytest.mark.parametrize("preset", [True, False])
def test_post_compaction_note_uses_internal_turn_source(monkeypatch, tmp_path, preset):
    from app.core.provider.harness_adapter import HarnessAdapter
    from app.core.provider.types import SessionOptions
    adapter = HarnessAdapter()
    adapter._options = SessionOptions(cwd=str(tmp_path), model="fake-model")
    adapter._is_delegated = True
    adapter._delegated_replaces_preset = not preset
    adapter._time_source = "user"  # stale source of the earlier host submit
    adapter._engine = SimpleNamespace(ctx=SimpleNamespace(extra={"time_turn_source": "unpark"}))
    seen = []
    def context(source):
        seen.append(source)
        return {"zone": "America/New_York", "last_owner_at": "2026-11-08T23:00:00Z"}
    monkeypatch.setattr(adapter, "_time_context", context)
    for name in ("_memory_section", "_working_record_block", "_away_note", "_scratch_note", "_plan_note"):
        monkeypatch.setattr(adapter, name, lambda: None)
    sections = adapter._compact_note()
    assert seen == ["unpark"]
    assert sections[0].name == "environment"
    assert "Current time:" in sections[0].text and "Owner's previous message:" in sections[0].text


