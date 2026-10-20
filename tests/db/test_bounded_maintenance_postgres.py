"""Retention SQL, commits, cancellation and index plans on disposable PostgreSQL 16."""
import threading
from contextlib import contextmanager
from uuid import uuid4

import psycopg2
from psycopg2.extras import RealDictCursor
import pytest

from app.db import (assistant, auth, automations, background_tasks, database,
                    error_logs, fluid_events, git_ops, interactions, mobile_tools,
                    network, retention, session_events, upload_blobs)
from app.core import startup_reconcile
from app.network import engine, session_runtime
from tests.db.schema_lifecycle_helpers import private_postgres, pg16_bindir

COMPACT_IDLE = session_events.compact_idle_session_deltas
PRUNE_FLUID = fluid_events.prune_fluid_delta_events


@pytest.fixture(scope="module")
def cluster(tmp_path_factory):
    bindir = pg16_bindir()
    if not bindir:
        pytest.skip("PostgreSQL 16 server binaries required")
    with private_postgres(bindir, tmp_path_factory.mktemp("maintenance-pg")) as descriptor:
        yield descriptor


@pytest.fixture
def pg(cluster, monkeypatch):
    schema = "retention_" + uuid4().hex
    args = dict(host="127.0.0.1", port=cluster["port"], user=cluster["user"],
                password=cluster["password"], dbname=cluster["database"])
    queries, commits = [], []
    stop = threading.Event()
    state = {"stop_after_commit": False}

    class Cursor(RealDictCursor):
        def execute(self, sql, params=None):
            result = super().execute(sql, params)
            queries.append(str(sql))
            if str(sql).startswith(("DELETE ", "UPDATE ")):
                changed = self.rowcount
                # Probe on this transaction, not a mocked cursor or SQL string.
                with self.connection.cursor(cursor_factory=RealDictCursor) as probe:
                    probe.execute("SHOW statement_timeout")
                    assert probe.fetchone()["statement_timeout"] == "30s"
                state["changed"] = state.get("changed", 0) + changed
            return result

    @contextmanager
    def connect(tracked=True):
        conn = psycopg2.connect(**args, options=f"-c search_path={schema}",
                               cursor_factory=Cursor if tracked else RealDictCursor)
        # Preserve the app-managed post-commit contract for producer calls.
        from app.db import base
        callbacks = []
        token = base._commit_callbacks.set((*base._commit_callbacks.get(), (conn, callbacks)))
        try:
            with conn:
                yield conn
            if tracked:
                commits.append(state.pop("changed", 0))
                if state["stop_after_commit"] and commits[-1]:
                    stop.set()
        finally:
            base._commit_callbacks.reset(token)
            conn.close()
        for callback in callbacks:
            callback()

    with psycopg2.connect(**args) as conn:
        conn.cursor().execute("CREATE SCHEMA " + schema)
    for module in (retention, network, session_events, database, auth, assistant,
                   background_tasks, mobile_tools, session_runtime):
        monkeypatch.setattr(module, "get_db", connect)
    monkeypatch.setattr(retention, "BATCH_SIZE", 2)
    monkeypatch.setattr(assistant, "RETENTION_BATCH", 2)
    monkeypatch.setattr(startup_reconcile, "record_error", lambda *a, **k: None)
    monkeypatch.setattr(session_events, "compact_idle_session_deltas", lambda **k: {"rows": 0})
    monkeypatch.setattr(fluid_events, "prune_fluid_delta_events", lambda *a, **k: 0)

    def sql(query, params=None):
        with connect(False) as conn:
            cur = conn.cursor()
            cur.execute(query, params)
            return cur.fetchall() if cur.description else []

    try:
        yield sql, queries, commits, stop, state, connect
    finally:
        with psycopg2.connect(**args) as conn:
            conn.cursor().execute("DROP SCHEMA " + schema + " CASCADE")


# Each case invokes the actual maintenance entry point, not just the helper.
CASES = [
    ("conflicts", "sync_conflicts", "expires_at TIMESTAMPTZ", "expires_at", network.expire_conflicts, None),
    ("joins", "network_joins", "expires_at TIMESTAMPTZ, status TEXT, updated_at TIMESTAMPTZ", "expires_at", network.expire_joins, "status"),
    ("git_ops", "git_ops", "updated_at TIMESTAMPTZ, status TEXT", "updated_at", git_ops.purge_terminal, None),
    ("uploads", "upload_blobs", "created_at TIMESTAMPTZ", "created_at", upload_blobs.purge_old, None),
    ("interactions", "pending_interactions", "expires_at TIMESTAMPTZ, state TEXT, session_id TEXT, kind TEXT, payload TEXT", "expires_at", interactions.expire_due_interactions_detailed, "state"),
]


@pytest.mark.parametrize("case", CASES, ids=lambda c: c[0])
@pytest.mark.parametrize("stopping", [False, True])
def test_retention_entry_points_page_and_stop(pg, case, stopping):
    sql, queries, commits, stop, state, _ = pg
    name, table, extra, timestamp, function, updated = case
    sql(f"CREATE TABLE {table} (id TEXT PRIMARY KEY, {extra})")
    status_col = "state" if name == "interactions" else "status" if name in {"joins", "git_ops"} else None
    status_value = "done" if name == "git_ops" else "pending"
    for i in range(6):
        cols, values = ["id", timestamp], [str(i), "2000-01-01" if i < 5 else "2999-01-01"]
        if status_col:
            cols.append(status_col)
            values.append(status_value)
        sql(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))})", values)
    state["stop_after_commit"] = stopping
    result = function(stop=stop)
    expected = 2 if stopping or name == "interactions" else 5
    assert (len(result) if isinstance(result, list) else result) == expected
    assert all(count <= 2 for count in commits)
    assert sum(commits) == expected
    if updated:
        assert len(sql(f"SELECT id FROM {table} WHERE {updated}='expired'")) == expected
    else:
        assert len(sql(f"SELECT id FROM {table}")) == 6 - expected
    if expected == 5:
        assert sum("ORDER BY id LIMIT" in q for q in queries) == 3
        assert sum("AND (id) >" in q for q in queries) == 2
    assert sql(f"SELECT id FROM {table} WHERE id='5'")
    stop.set()
    before = len(queries)
    assert function(stop=stop) in (0, [])
    assert len(queries) == before


@pytest.mark.parametrize("stopping", [False, True])
def test_automation_composite_key_and_retention(pg, stopping):
    sql, queries, commits, stop, state, _ = pg
    sql("CREATE TABLE automation_fires (automation_id TEXT, fire_bucket TEXT, claimed_at TIMESTAMPTZ, PRIMARY KEY(automation_id,fire_bucket))")
    for i in range(6):
        sql("INSERT INTO automation_fires VALUES (%s,%s,%s)", (str(i // 2), str(i), "2000-01-01" if i < 5 else "2999-01-01"))
    state["stop_after_commit"] = stopping
    assert automations.prune_automation_fires(stop=stop) == (2 if stopping else 5)
    assert max(commits) == 2
    assert len(sql("SELECT * FROM automation_fires")) == (4 if stopping else 1)
    stop.set()
    before = len(queries)
    assert automations.prune_automation_fires(stop=stop) == 0
    assert len(queries) == before


AUTH = [("auth_sessions", "token", "expires_at"), ("api_key_sessions", "token", "expires_at"),
        ("account_lockouts", "id", "locked_until"), ("login_attempts", "id", "created_at")]


@pytest.mark.parametrize("target,key,ts", AUTH)
@pytest.mark.parametrize("stopping", [False, True])
def test_auth_retention_each_table(pg, target, key, ts, stopping):
    sql, queries, commits, stop, state, _ = pg
    for table, pk, column in AUTH:
        sql(f"CREATE TABLE {table} ({pk} TEXT PRIMARY KEY, {column} TIMESTAMPTZ)")
    for i in range(6):
        sql(f"INSERT INTO {target} VALUES (%s,%s)", (str(i), "2000-01-01" if i < 5 else "2999-01-01"))
    state["stop_after_commit"] = stopping
    result = auth.cleanup_expired_sessions(stop=stop)
    assert result[target] == (2 if stopping else 5)
    assert sum(result.values()) == result[target]
    assert max(commits) == 2
    assert sql(f"SELECT {key} FROM {target} WHERE {key}='5'")
    stop.set()
    before = len(queries)
    assert not any(auth.cleanup_expired_sessions(stop=stop).values())
    assert len(queries) == before


MAINTENANCE = ["usage_log", "audit_log", "sync_log", "connector_event_log", "git_ops", "upload_blobs", "notifications_age", "notifications_cap"]


@pytest.mark.parametrize("target", MAINTENANCE)
@pytest.mark.parametrize("stopping", [False, True])
def test_general_maintenance_retention_each_delete(pg, target, stopping):
    sql, queries, commits, stop, state, _ = pg
    for table in MAINTENANCE[:6] + ["notifications"]:
        sql(f"CREATE TABLE {table} (id TEXT PRIMARY KEY, created_at TIMESTAMPTZ, updated_at TIMESTAMPTZ, status TEXT, user_id TEXT)")
    table = "notifications" if target.startswith("notifications") else target
    count = 506 if target == "notifications_cap" else 6
    for i in range(count):
        age = "2999-01-01" if i == count - 1 else "2000-01-01"
        if target == "notifications_cap":
            # All recent; unique dates preserve the pre-existing tie semantics.
            age = f"2998-01-01 00:{i // 60:02d}:{i % 60:02d}+00"
        sql(f"INSERT INTO {table} VALUES (%s,%s,%s,'done','owner')", (f"{i:04d}", age, age))
    state["stop_after_commit"] = stopping
    result = database.run_database_maintenance(skip_vacuum=True, stop=stop)
    assert result["cleaned_" + target] == (2 if stopping else 6 if target == "notifications_cap" else 5)
    assert max(commits) == 2
    assert all(not str(v).startswith("error:") for v in result.values()), result
    stop.set()
    before = len(queries)
    database.run_database_maintenance(skip_vacuum=True, stop=stop)
    assert len(queries) == before


@pytest.mark.parametrize("kind", ["age", "warning", "cap"])
@pytest.mark.parametrize("stopping", [False, True])
def test_error_log_retention(pg, monkeypatch, kind, stopping):
    sql, queries, commits, stop, state, _ = pg
    sql("CREATE TABLE error_log (id TEXT PRIMARY KEY, updated_at TIMESTAMPTZ, level TEXT)")
    for i in range(7):
        sql("INSERT INTO error_log VALUES (%s,%s,%s)", (str(i), "2000-01-01" if kind == "age" and i < 5 else f"2999-01-01 00:00:0{i}", "WARNING" if kind == "warning" else "ERROR"))
    monkeypatch.setattr(error_logs, "_max_warning_rows", lambda: 2)
    state["stop_after_commit"] = stopping
    expected = 2 if stopping else 5
    assert error_logs.prune_errors(max_rows=2 if kind == "cap" else 100, stop=stop) == expected
    assert len(sql("SELECT id FROM error_log")) == 7 - expected
    assert max(commits) == 2
    stop.set()
    before = len(queries)
    assert error_logs.prune_errors(stop=stop) == 0
    assert len(queries) == before


def test_index_upgrade_and_real_explain(pg):
    sql, _, _, _, _, _ = pg
    with pg[-1](False) as conn:
        network.create_schema(conn.cursor())
    sql("CREATE INDEX idx_sync_ops_record ON sync_ops(collection, record_id)")
    with pg[-1](False) as conn:
        network.create_schema(conn.cursor())  # actual upgrade path
        network.create_schema(conn.cursor())  # idempotence
    assert not sql("SELECT 1 FROM pg_indexes WHERE schemaname=current_schema() AND indexname='idx_sync_ops_record'")
    sql("INSERT INTO sync_ops(origin_device_id,incarnation,seq,collection,record_id,op,clock,envelope) SELECT 'worker',1,n,CASE WHEN n % 10000=0 THEN 'membership' WHEN n % 10=0 THEN 'other' ELSE 'tags' END, 'r' || (n % 10000), 'upsert',n,'{}'::jsonb FROM generate_series(1,100000) n")
    sql("ANALYZE sync_ops")
    for query in (
        "SELECT * FROM sync_ops WHERE collection='tags' AND record_id='r42'",
        "SELECT * FROM sync_ops WHERE collection='tags' AND record_id IN ('r42','r43')",
        "SELECT record_id, incarnation, seq FROM sync_ops WHERE origin_device_id='worker' AND collection='membership' AND (incarnation > 1 OR (incarnation=1 AND seq > 0)) ORDER BY incarnation, seq",
        "SELECT * FROM sync_ops WHERE collection='tags' AND record_id='r42' AND ingest_id > 100 ORDER BY ingest_id",
        "SELECT ingest_id FROM sync_ops o WHERE created_at < NOW() AND ingest_id > 0 AND EXISTS (SELECT 1 FROM sync_ops n WHERE n.collection=o.collection AND n.record_id=o.record_id AND n.ingest_id>o.ingest_id) ORDER BY ingest_id LIMIT 100",
    ):
        plan = "\n".join(row["QUERY PLAN"] for row in sql("EXPLAIN (ANALYZE, BUFFERS) " + query))
        print(query + "\n" + plan)
        assert "idx_sync_ops_record_ingest" in plan


@pytest.mark.parametrize("kind", ["scrub", "delete"])
@pytest.mark.parametrize("stopping", [False, True])
def test_mobile_retention_separately_commits_and_stops(pg, kind, stopping):
    sql, queries, commits, stop, state, _ = pg
    sql("CREATE TABLE mobile_tool_commands(id TEXT PRIMARY KEY, expires_at TIMESTAMPTZ, status TEXT, arguments TEXT, result TEXT)")
    for i in range(6):
        sql("INSERT INTO mobile_tool_commands VALUES (%s,%s,%s,'private','result')",
            (str(i), "2000-01-01" if i < 5 else "2999-01-01", "expired" if kind == "delete" else "pending"))
    # For scrub-only, expired less than a day ago and therefore not deletable.
    if kind == "scrub":
        sql("UPDATE mobile_tool_commands SET expires_at=NOW() - interval '1 hour' WHERE id <> '5'")
    mobile_tools._purge_requested.set()
    state["stop_after_commit"] = stopping
    assert mobile_tools.purge_requested_commands(stop=stop) == (2 if stopping else 5)
    if kind == "delete":
        assert len(sql("SELECT id FROM mobile_tool_commands")) == (4 if stopping else 1)
    else:
        assert len(sql("SELECT id FROM mobile_tool_commands WHERE arguments='{}' AND result IS NULL")) == (2 if stopping else 5)
    assert max(commits) == 2
    stop.set()
    mobile_tools._purge_requested.set()
    before = len(queries)
    assert mobile_tools.purge_requested_commands(stop=stop) == 0
    assert len(queries) == before


@pytest.mark.parametrize("step", ["events_deleted", "runs_deleted", "events_scrubbed", "remote_costs_deleted", "limit_stops_deleted"])
@pytest.mark.parametrize("stopping", [False, True])
def test_assistant_retains_per_pass_budget_and_protected_rows(pg, step, stopping):
    sql, queries, commits, stop, state, _ = pg
    sql("CREATE TABLE assistant_events(id TEXT PRIMARY KEY, assistant_id TEXT, status TEXT, observed_at TIMESTAMPTZ, payload TEXT, run_id TEXT)")
    sql("CREATE TABLE assistant_runs(id TEXT PRIMARY KEY, assistant_id TEXT, created_at TIMESTAMPTZ, kind TEXT, status TEXT, session_id TEXT)")
    sql("CREATE TABLE assistant_remote_costs(id TEXT PRIMARY KEY, assistant_id TEXT, booked_at TIMESTAMPTZ)")
    sql("CREATE TABLE assistant_limit_stops(turn_id TEXT PRIMARY KEY, assistant_id TEXT, created_at TIMESTAMPTZ)")
    table = {"events_deleted": "assistant_events", "events_scrubbed": "assistant_events", "runs_deleted": "assistant_runs", "remote_costs_deleted": "assistant_remote_costs", "limit_stops_deleted": "assistant_limit_stops"}[step]
    for i in range(6):
        age = "2999-01-01" if i == 5 else "2000-01-01"
        if step == "events_scrubbed" and i < 5:
            # Past payload horizon, inside history horizon.
            from datetime import timedelta
            age = assistant.now() - timedelta(days=assistant.EVENT_PAYLOAD_RETENTION_DAYS + 1)
        if table == "assistant_events":
            sql("INSERT INTO assistant_events VALUES (%s,'a','completed',%s,'private',NULL)", (str(i), age))
        elif table == "assistant_runs":
            sql("INSERT INTO assistant_runs VALUES (%s,'a',%s,'proactive','completed',NULL)", (str(i), age))
        else:
            sql(f"INSERT INTO {table} VALUES (%s,'a',%s)", (str(i), age))
    state["stop_after_commit"] = stopping
    assert assistant.retention_sweep("a", stop=stop)[step] == 2
    assert max(commits) == 2
    if not stopping:
        assert assistant.retention_sweep("a", stop=stop)[step] == 2
        assert assistant.retention_sweep("a", stop=stop)[step] == 1
        assert assistant.retention_sweep("a", stop=stop)[step] == 0
    stop.set()
    before = len(queries)
    assert not any(assistant.retention_sweep("a", stop=stop).values())
    assert len(queries) == before
    key = "turn_id" if table == "assistant_limit_stops" else "id"
    assert sql(f"SELECT {key} FROM {table} WHERE {key}='5'")


@pytest.mark.parametrize("kind", ["parked", "status", "tasks", "runtime"])
@pytest.mark.parametrize("stopping", [False, True])
def test_boot_and_runtime_cleanup_are_bounded_and_scoped(pg, monkeypatch, kind, stopping):
    from types import SimpleNamespace
    from app.core.config import settings
    sql, queries, commits, stop, state, _ = pg
    monkeypatch.setattr(settings, "instance_id", "this-worker")
    monkeypatch.setattr(engine, "context", lambda: SimpleNamespace(device_id="this-worker"))
    sql("CREATE TABLE sessions(id TEXT PRIMARY KEY, instance_id TEXT, parked_task_kind TEXT, status TEXT)")
    sql("CREATE TABLE background_tasks(task_id TEXT, session_id TEXT, instance_id TEXT, remote_device_id TEXT, status TEXT, updated_at TIMESTAMPTZ, reason TEXT, summary TEXT, tool_use_id TEXT, title TEXT, kind TEXT, agent TEXT, roster_name TEXT, model TEXT, started_at TIMESTAMPTZ, terminal_notified BOOLEAN, PRIMARY KEY(task_id,session_id))")
    sql("CREATE TABLE network_session_runtime(session_id TEXT PRIMARY KEY, owner_device_id TEXT)")
    for i in range(6):
        owner = "peer" if i == 5 else "this-worker"
        if kind in {"parked", "status"}:
            sql("INSERT INTO sessions VALUES (%s,%s,'Task','running')", (str(i), owner))
        elif kind == "tasks":
            sql("INSERT INTO background_tasks(task_id,session_id,instance_id,status) VALUES (%s,'session',%s,'running')", (str(i), owner))
        else:
            sql("INSERT INTO network_session_runtime VALUES (%s,%s)", (str(i), owner))
    function = {"parked": startup_reconcile._clear_stale_parked_markers, "status": startup_reconcile._reset_orphaned_session_statuses, "tasks": background_tasks.mark_stale_background_tasks_killed, "runtime": session_runtime.prune_owned}[kind]
    state["stop_after_commit"] = stopping
    result = function(stop=stop)
    assert (len(result) if isinstance(result, list) else result) == (2 if stopping else 5)
    assert max(commits) == 2
    stop.set()
    before = len(queries)
    result = function(stop=stop)
    assert result in (0, [])
    assert len(queries) == before


def test_targeted_runtime_cleanup_keeps_other_orphans_and_live_rows(pg):
    from types import SimpleNamespace
    sql, queries, commits, stop, state, _ = pg
    sql("CREATE TABLE sessions(id TEXT PRIMARY KEY)")
    sql("CREATE TABLE network_session_runtime(session_id TEXT PRIMARY KEY, owner_device_id TEXT)")
    sql("INSERT INTO sessions VALUES ('live')")
    for sid, owner in [('a', 'us'), ('b', 'us'), ('c', 'us'), ('live', 'us'),
                       ('other-orphan', 'us'), ('peer', 'them')]:
        sql("INSERT INTO network_session_runtime VALUES (%s,%s)", (sid, owner))
    assert session_runtime.prune_owned(ctx=SimpleNamespace(device_id='us'),
        session_ids=['a', 'b', 'c', 'live', 'peer'], stop=stop) == 3
    assert max(commits) == 2
    assert {r['session_id'] for r in sql("SELECT session_id FROM network_session_runtime")} == {
        'live', 'other-orphan', 'peer'}
    before = len(queries)
    assert session_runtime.prune_owned(ctx=SimpleNamespace(device_id='us'), session_ids=[]) == 0
    assert len(queries) == before


@pytest.mark.parametrize("stopping", [False, True])
def test_compaction_uses_real_append_output_and_commits_per_session(pg, monkeypatch, stopping):
    from app.db.schema_postgres import create_postgres_schema
    sql, queries, commits, stop, state, connect = pg
    with connect(False) as conn:
        create_postgres_schema(conn.cursor())
    monkeypatch.setattr(session_events, "_compaction_cursor", "")
    for sid in ("a", "b", "fresh"):
        sql("INSERT INTO sessions(id,title,status,updated_at) VALUES (%s,%s,'completed',%s)",
            (sid, sid, "2999-01-01" if sid == "fresh" else "2000-01-01"))
        with connect(False) as conn:
            session_events.append_events(sid, [
                {"type": "assistant.text_delta", "payload": {"text": str(i)}, "turn_id": "turn"}
                for i in range(6)], _conn=conn)
    state["stop_after_commit"] = stopping
    result = COMPACT_IDLE(keep_tail_events=0, stop=stop)
    assert result == {"sessions": 1 if stopping else 2, "runs": 1 if stopping else 2, "rows": 5 if stopping else 10}
    assert len(sql("SELECT seq FROM session_events WHERE session_id='fresh'")) == 6
    for sid in ("a",) if stopping else ("a", "b"):
        rows = sql("SELECT payload FROM session_events WHERE session_id=%s", (sid,))
        assert len(rows) == 1 and rows[0]["payload"]["text"] == "012345"
    # Each session is atomic (delete + survivor update); its connection is
    # returned before acquiring the next. Candidate selection also releases.
    assert commits == [0] + [6] * (1 if stopping else 2)
    stop.set()
    before = len(queries)
    assert COMPACT_IDLE(keep_tail_events=0, stop=stop)["rows"] == 0
    assert len(queries) == before


@pytest.mark.parametrize("stopping", [False, True])
def test_fluid_prune_honours_stop_on_real_postgres(pg, monkeypatch, stopping):
    sql, queries, commits, stop, state, connect = pg
    monkeypatch.setattr(fluid_events, "BATCH_SIZE", 2)
    sql("CREATE TABLE sessions(id TEXT PRIMARY KEY, sdk_session_id TEXT, updated_at TIMESTAMPTZ)")
    sql("CREATE TABLE fluid_session_events(session_id TEXT, seq BIGINT, event JSONB, PRIMARY KEY(session_id,seq))")
    sql("INSERT INTO sessions VALUES ('idle','sdk','2000-01-01')")
    for seq in range(6):
        sql("INSERT INTO fluid_session_events VALUES ('sdk',%s,%s)", (seq, '{"_cls":"ToolArgsDelta"}' if seq < 5 else '{"_cls":"Usage"}'))
    state["stop_after_commit"] = stopping
    assert PRUNE_FLUID(stop=stop) == (2 if stopping else 5)
    assert max(commits) == 2
    assert sql("SELECT seq FROM fluid_session_events WHERE seq=5")
    stop.set()
    before = len(queries)
    assert PRUNE_FLUID(stop=stop) == 0
    assert len(queries) == before


