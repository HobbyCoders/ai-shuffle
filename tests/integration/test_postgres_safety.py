"""PostgreSQL connection and transaction contracts against a disposable database."""
import os
from contextlib import closing

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("PG_SAFETY_TEST") != "1", reason="requires disposable PostgreSQL opt-in"),
]


@pytest.fixture
def pg(monkeypatch, tmp_path):
    if os.environ.get("TESTING") != "1" or os.environ.get("ALLOW_REAL_PG") != "1":
        pytest.fail("Explicit test-only PostgreSQL opt-in required")
    import psycopg2
    from pydantic_settings import DotEnvSettingsSource

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(DotEnvSettingsSource, "_read_env_files", lambda self: {})
    from app.db import base
    from app.core.config import settings

    # Migration probes explicitly inspect public; this database is test-only.
    schema = "public"
    kwargs = dict(host="127.0.0.1", hostaddr="127.0.0.1",
                  port=int(os.environ.get("SAFETY_PG_PORT", "5432")),
                  dbname="shuffle_safety_test", user="shuffle_safety_test", password="safety-test-only",
                  connect_timeout=5, sslmode="disable", options="")
    with closing(psycopg2.connect(**kwargs)) as admin, admin:
        admin.cursor().execute("DROP SCHEMA public CASCADE")
        admin.cursor().execute("CREATE SCHEMA public")
    kwargs["options"] = f"-c search_path={schema} -c statement_timeout=10000 -c lock_timeout=5000"
    pool = base._RetainingThreadedConnectionPool(1, 2, **kwargs)
    monkeypatch.setattr(base, "_pg_pool", pool)
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    try:
        yield base, pool, kwargs
    finally:
        pool.closeall()
        kwargs["options"] = ""
        with closing(psycopg2.connect(**kwargs)) as admin, admin:
            admin.cursor().execute("DROP SCHEMA public CASCADE")
            admin.cursor().execute("CREATE SCHEMA public")


def test_idle_probe_ends_its_transaction_before_checkout(pg, monkeypatch):
    base, pool, _ = pg
    monkeypatch.setattr(pool, "idle_seconds", lambda conn: 120.0)
    with base.get_db() as conn:
        # Setting autocommit during a transaction raises on real psycopg2.
        assert conn.info.transaction_status == 0
        conn.cursor().execute("CREATE TABLE probe_result (value integer)")
        conn.cursor().execute("INSERT INTO probe_result VALUES (42)")
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT value FROM probe_result")
        assert cur.fetchone()["value"] == 42
    assert not pool._used


def test_checkout_recovers_from_a_terminated_idle_backend(pg, monkeypatch):
    import psycopg2
    base, pool, kwargs = pg
    with base.get_db() as conn:
        old_pid = conn.get_backend_pid()
    with psycopg2.connect(**kwargs) as killer:
        killer.cursor().execute("SELECT pg_terminate_backend(%s)", (old_pid,))
    monkeypatch.setattr(pool, "idle_seconds", lambda conn: 120.0)
    with base.get_db() as conn:
        assert conn.get_backend_pid() != old_pid
        cur = conn.cursor()
        cur.execute("SELECT 1 AS alive")
        assert cur.fetchone()["alive"] == 1
    assert base.pressure_stats()["connections_stale"] == 1
    assert not pool._used


@pytest.mark.parametrize("scope", ["get_db", "transaction"])
def test_dead_socket_rollback_preserves_original_error(pg, scope):
    base, pool, _ = pg
    with pytest.raises(ValueError, match="original failure"):
        with getattr(base, scope)() as conn:
            conn.close()
            raise ValueError("original failure")
    assert not pool._used
    with base.get_db() as conn:
        conn.cursor().execute("SELECT 1")


def test_savepoint_recovers_after_real_sql_error(pg):
    import psycopg2
    from app.db.database import _savepoint
    base, _, _ = pg
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("CREATE TABLE saved (value integer)")
        cur.execute("INSERT INTO saved VALUES (1)")
        with pytest.raises(psycopg2.errors.DivisionByZero):
            with _savepoint(cur, "failed_step"):
                cur.execute("INSERT INTO saved VALUES (2)")
                cur.execute("SELECT 1 / 0")
        cur.execute("INSERT INTO saved VALUES (3)")
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT value FROM saved ORDER BY value")
        assert [r["value"] for r in cur.fetchall()] == [1, 3]


def test_maintenance_commits_later_steps_after_missing_table(pg):
    from app.db.database import run_database_maintenance
    base, _, _ = pg
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("CREATE TABLE audit_log (created_at timestamptz)")
        cur.execute("INSERT INTO audit_log VALUES (NOW() - INTERVAL '8 days')")
    result = run_database_maintenance(skip_vacuum=True)
    assert result["cleaned_sync_log"].startswith("error:")
    assert result["cleaned_audit_log"] == 1
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS n FROM audit_log")
        assert cur.fetchone()["n"] == 0


def test_vacuum_restores_session_setting(pg):
    from app.db.database import run_database_maintenance
    base, pool, _ = pg
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("SHOW max_parallel_maintenance_workers")
        before = next(iter(cur.fetchone().values()))
    result = run_database_maintenance()
    assert result["vacuum"] == "ok"
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("SHOW max_parallel_maintenance_workers")
        assert next(iter(cur.fetchone().values())) == before
    assert not pool._used


def test_fresh_schema_and_repeat_boot(pg):
    from app.db.database import init_database, SCHEMA_VERSION
    base, _, _ = pg
    init_database()
    init_database()
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT version FROM schema_version")
        assert cur.fetchone()["version"] == SCHEMA_VERSION

def test_mobile_expiry_without_a_sweep(pg, monkeypatch):
    from datetime import timedelta
    from app.db import mobile_tools as queue
    base, _, _ = pg
    with base.get_db() as conn:
        conn.cursor().execute("CREATE TABLE mobile_devices (device_id text PRIMARY KEY)")
        conn.cursor().execute("INSERT INTO mobile_devices VALUES ('test-phone')")
    queue.init_schema()
    queue._reset_purge_throttle()
    queue.capabilities("test-phone", True, ["open_url"])
    queue.claim("test-phone")
    command = queue.enqueue("test-phone", "test-session", "open_url", '{"url":"https://example.com"}', 20)
    assert "id" in command
    assert [row["id"] for row in queue.claim("test-phone")] == [command["id"]]
    future = queue.now() + timedelta(seconds=21)
    monkeypatch.setattr(queue, "now", lambda: future)
    # The real queue API produced the row; only time advances, not the sweep clock.
    assert queue.outcome(command["id"], "test-session") == {"status": "expired", "result": None}
    assert queue.claim("test-phone") == []
    assert queue.submit_result("test-phone", command["id"], '{}') == "expired"
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT arguments, result FROM mobile_tool_commands WHERE id=%s", (command["id"],))
        assert dict(cur.fetchone()) == {"arguments": "{}", "result": None}


def test_usage_provider_breakdown_on_real_postgres(pg, monkeypatch):
    """#1614: the per-provider breakdown, its daily split, the indexed date
    range and the usage ledger's 400-day retention on real PostgreSQL (the
    SQLite shim drops ``::date`` and has no planner to check)."""
    from app.core import claude_auth_mode
    from app.db import database
    from app.db.database import init_database, run_database_maintenance

    base, _, _ = pg
    init_database()
    monkeypatch.setattr(claude_auth_mode, "current_mode",
                        lambda: claude_auth_mode.MODE_SUBSCRIPTION)
    database.create_model_provider("p-codex", "My Codex", "chatgpt", "https://example.invalid")
    database.create_api_user("u-1", "User", "hash-u-1")
    database.create_session(session_id="s-mine", profile_id=None, api_user_id="u-1")
    database.create_session(session_id="s-old", profile_id=None, provider_id="p-codex")

    def book(sid, key, **kw):
        row = dict(profile_id=None, model="m", tokens_in=100, tokens_out=10,
                   cost_usd=0.01, model_calls=1, turn_increment=1)
        row.update(kw)
        assert database.settle_turn_usage(sid, key, **row)

    book("s-mine", "a", provider_type="claude", billing="subscription", usage_estimated=True)
    book("s-mine", "b", provider_type="claude", billing="subscription")
    book("s-mine", "c", provider_id="p-codex", provider_type="chatgpt",
         billing="subscription", cost_usd=0)
    # Booked before attribution: inferred from the session's binding.
    database.log_usage("s-old", None, "gpt", 10, 1, 0.0, 1)
    database.log_usage(None, None, "haiku", 10, 1, 0.001, 1)
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE usage_log SET created_at = '2026-09-01 08:00:00' WHERE settlement_key = 'a'")
        cur.execute("UPDATE usage_log SET created_at = '2026-09-01 20:00:00' WHERE settlement_key = 'b'")
        cur.execute("UPDATE usage_log SET created_at = '2026-09-03 12:00:00' "
                    "WHERE settlement_key = 'c' OR settlement_key IS NULL")

    items = {i["key"]: i for i in database.get_analytics_provider_breakdown("2026-09-01", "2026-09-30")}
    assert set(items) == {"claude", "p-codex", "unattributed"}
    assert items["claude"]["query_count"] == 2
    assert items["claude"]["estimated_query_count"] == 1
    assert items["claude"]["billing"] == "subscription"
    assert items["p-codex"]["query_count"] == 2
    assert items["p-codex"]["inferred_query_count"] == 1
    assert items["p-codex"]["cost_status"] == "unknown"
    assert items["p-codex"]["billing"] == "subscription"

    daily = database.get_analytics_provider_breakdown("2026-09-01", "2026-09-30", by_date=True)
    assert [(i["date"], i["key"], i["query_count"]) for i in daily if i["key"] != "unattributed"] == [
        ("2026-09-01", "claude", 2), ("2026-09-03", "p-codex", 2),
    ]
    mine = database.get_analytics_provider_breakdown("2026-09-01", "2026-09-30", api_user_id="u-1")
    assert sorted((i["key"], i["query_count"]) for i in mine) == [("claude", 2), ("p-codex", 1)]

    # Both end days are whole days: the filter compares raw timestamps
    # against day boundaries rather than casting with ::date.
    assert database.get_analytics_usage_stats("2026-09-01", "2026-09-01")["query_count"] == 2
    assert database.get_analytics_usage_stats("2026-09-02", "2026-09-03")["query_count"] == 3
    assert database.get_analytics_usage_stats("2026-09-02", "2026-09-02")["query_count"] == 0

    # That form is what lets the Stats range use idx_usage_log_created.
    from app.db.analytics import _created_at_range
    where, params = _created_at_range("ul.created_at", "2026-09-01", "2026-09-30")
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("SET LOCAL enable_seqscan = off")
        cur.execute(f"EXPLAIN SELECT COUNT(*) FROM usage_log ul WHERE 1=1 {where}", params)
        plan = "\n".join(next(iter(dict(r).values())) for r in cur.fetchall())
    # An Index Cond on created_at, not a whole-index walk filtered by ::date.
    assert "idx_usage_log_created" in plan, plan
    assert any("Index Cond" in line and "created_at" in line
               for line in plan.splitlines()), plan

    # Retention: 400 days are kept, older rows go.
    database.log_usage(None, None, "expired", 10, 1, 0.001, 1)
    with base.get_db() as conn:
        conn.cursor().execute(
            "UPDATE usage_log SET created_at = NOW() - INTERVAL '401 days' WHERE model = 'expired'")
    result = run_database_maintenance(skip_vacuum=True)
    assert result["cleaned_usage_log"] == 1
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS n FROM usage_log")
        assert cur.fetchone()["n"] == 5

