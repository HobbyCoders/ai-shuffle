"""Upgrade authorization and stored-wake contracts on disposable PostgreSQL."""
import pytest
from tests.builtin_fixtures import create_unchanged_builtin

from tests.db.postgres_harness import pg_schema, requires_real_pg  # noqa: F401

pytestmark = requires_real_pg

# Rows this module (and the SQLite tests it drives) creates by fixed id.
_SESSIONS = ("retirement-chat", "missing-wake-chat", "incoming-retired-chat")
_API_USERS = ("pre-upgrade-key", "retirement-only", "retirement-mixed", "retirement-open",
              "only-helper", "helper-and-other", "open-key")
_AGENTS = ("Explore", "Plan", "general-purpose", "frontend-designer", "retirement-other", "wake-fallback", "helper", "other")


def _forget_fixed_rows():
    from app.core import wakeup_service
    from app.db import base

    wakeup_service._retry_attempts.clear()
    with base.get_db() as conn:
        cursor = conn.cursor()
        # Left behind if an earlier run stopped before the upgrade dropped it.
        cursor.execute("ALTER TABLE api_user_profiles DROP CONSTRAINT IF EXISTS api_user_profiles_profile_id_fkey")
        for table in ("session_events", "turns", "pending_wakeups", "sessions"):
            column = "id" if table == "sessions" else "session_id"
            cursor.execute(f"DELETE FROM {table} WHERE {column} = ANY(%s)", (list(_SESSIONS),))
        cursor.execute("DELETE FROM api_user_profiles WHERE api_user_id = ANY(%s)", (list(_API_USERS),))
        cursor.execute("DELETE FROM api_users WHERE id = ANY(%s)", (list(_API_USERS),))
        cursor.execute("DELETE FROM automations WHERE id = %s", ("retirement-job",))
        cursor.execute("DELETE FROM agents WHERE id = ANY(%s)", (list(_AGENTS),))


@pytest.fixture(autouse=True)
def _isolated_rows():
    """The real-PostgreSQL CI step shares one database between modules and
    nothing resets it between runs: start from, and leave, no fixed-id rows.
    (The module-scoped ``pg_schema`` pool is set up before this runs.)"""
    _forget_fixed_rows()
    yield
    _forget_fixed_rows()


def test_concurrent_starter_installs_are_atomic_and_idempotent():
    from concurrent.futures import ThreadPoolExecutor
    from app.core.agent_templates import install_starter_team
    from app.db import database

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: install_starter_team(), range(2)))
    ids = {agent["existing_agent_id"] for result in results for agent in result["agents"]}
    try:
        assert results[0] == results[1]
        assert len(ids) == 2
        assert all(not database.get_profile(agent_id)["is_builtin"] for agent_id in ids)
    finally:
        for agent_id in ids:
            database.delete_profile(agent_id)


@pytest.mark.parametrize("retired_id", ["Explore", "Plan", "general-purpose", "frontend-designer"])
def test_upgrade_unlinks_required_automation_agent(retired_id):
    from app.db import base, database
    from app.core.builtin_subagents import seed_builtin_subagents

    create_unchanged_builtin(retired_id)
    database.create_automation("retirement-job", "Saved job", "interval", {"seconds": 300},
                               retired_id, prompt="Keep this prompt", enabled=True)
    with base.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("ALTER TABLE automations ALTER COLUMN profile_id SET NOT NULL")
        cursor.execute("DELETE FROM system_settings WHERE key = %s", (database.MIGRATIONS_FINGERPRINT_KEY,))
    database.init_database()
    seed_builtin_subagents()
    row = database.get_automation("retirement-job")
    assert row["profile_id"] is None
    assert not row["is_enabled"]
    assert row["prompt"] == "Keep this prompt"
    assert retired_id in row["last_error"]
    assert database.get_profile(retired_id) is None


@pytest.mark.parametrize("retired_id", ["Explore", "Plan", "general-purpose", "frontend-designer"])
async def test_upgrade_keeps_restricted_key_and_cancels_stored_wakes(retired_id):
    from app.db import base, database
    from tests.api.test_builtin_seeding import (
        test_retirement_preserves_key_scope_and_cancels_stored_continuations,
        test_missing_stored_wake_profile_drops_once_with_visible_reason,
    )

    # Restriction is already stored BEFORE the upgrade. Recreate the shipped
    # constraint, then drive the real startup migration and retirement.
    create_unchanged_builtin(retired_id)
    database.create_api_user("pre-upgrade-key", "Existing restricted key", "pre-upgrade-hash", profile_ids=[retired_id])
    with base.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("ALTER TABLE api_user_profiles ADD CONSTRAINT api_user_profiles_profile_id_fkey "
                       "FOREIGN KEY (profile_id) REFERENCES agents(id) ON DELETE CASCADE")
        # Each parameter recreates a pre-upgrade schema; do not let the previous
        # case's current fingerprint skip the upgrade's column migrations.
        cursor.execute("DELETE FROM system_settings WHERE key = %s",
                       (database.MIGRATIONS_FINGERPRINT_KEY,))
    database.init_database()
    from app.core.builtin_subagents import seed_builtin_subagents
    from app.core.api_user_scope import worker_refusal
    seed_builtin_subagents()
    assert database.get_profile(retired_id) is None
    assert database.get_api_user("pre-upgrade-key")["profile_ids"] == [retired_id]
    assert worker_refusal("pre-upgrade-key", "unrelated-agent") is not None
    await test_retirement_preserves_key_scope_and_cancels_stored_continuations(None, retired_id)
    await test_missing_stored_wake_profile_drops_once_with_visible_reason(None)
    base.close_pool()
    base.init_pool(min_conn=1, max_conn=6)
    assert worker_refusal("pre-upgrade-key", "unrelated-agent") is not None
    assert worker_refusal("retirement-mixed", "unrelated-agent") is not None
    assert worker_refusal("retirement-mixed", "retirement-other") is None


@pytest.mark.parametrize("retired_id", ["Explore", "Plan", "general-purpose", "frontend-designer"])
@pytest.mark.parametrize("agent_state", ["missing", "builtin", "owner-created"])
def test_materialize_retired_session_reference_on_postgres(retired_id, agent_state):
    from app.core.builtin_subagents import seed_builtin_subagents
    from app.db import database
    from app.network import engine, policy

    seed_builtin_subagents()
    if agent_state == "owner-created":
        database.create_profile(retired_id, retired_id, None, {})
    else:
        create_unchanged_builtin(retired_id)
    record = dict(database.create_session("retirement-chat", retired_id, title="Historical session"))
    if agent_state == "missing":
        seed_builtin_subagents()
        assert database.get_profile(retired_id) is None
    record["id"] = "incoming-retired-chat"
    with database.get_db() as conn:
        mirror = engine.materialize(conn.cursor(), policy.collection("sessions"), record["id"], record)
    assert mirror["profile_id"] == (retired_id if agent_state == "owner-created" else None)
    assert database.get_session(record["id"])["title"] == "Historical session"
    # Normalization must not mutate the signed source record.
    assert record["profile_id"] == retired_id


def test_recreated_agent_id_does_not_inherit_grants_on_postgres():
    from tests.api.test_api_user_scope_regressions import (
        test_recreated_agent_id_does_not_inherit_the_deleted_agents_key_grants,
    )

    test_recreated_agent_id_does_not_inherit_the_deleted_agents_key_grants(None)


@pytest.mark.parametrize("queued", [False, True])
@pytest.mark.parametrize("retire", [False, True])
async def test_stale_action_only_approval_on_postgres(monkeypatch, queued, retire):
    from tests.api.test_agent_templates import test_stale_action_only_approval_is_cancelled

    await test_stale_action_only_approval_is_cancelled(None, monkeypatch, queued, retire)


def test_starter_install_invalidation_on_postgres(monkeypatch):
    from app.db import database
    from tests.api.test_agent_templates import test_install_publishes_shared_agents_only_after_commit

    before = {agent["id"] for agent in database.get_all_profiles()}
    try:
        test_install_publishes_shared_agents_only_after_commit(None, monkeypatch)
    finally:
        # Don't let the notification-time assertion observe cleanup mutations.
        monkeypatch.undo()
        for agent in database.get_all_profiles():
            if agent["id"] not in before:
                database.delete_profile(agent["id"])


@pytest.mark.parametrize("failure", ["insert", "commit"])
def test_starter_install_rollback_invalidation_on_postgres(monkeypatch, failure):
    from tests.api.test_agent_templates import test_failed_install_does_not_publish_agents

    test_failed_install_does_not_publish_agents(None, monkeypatch, failure)


from tests.core import test_builtin_preservation as preservation


@pytest.mark.parametrize("role", preservation.ROLES)
@pytest.mark.parametrize("revision", [None, 1, 2])
@pytest.mark.parametrize("migrated", [False, True])
def test_known_defaults_retire_on_postgres(role, revision, migrated):
    preservation.test_known_defaults_retire(None, role, revision, migrated)


@pytest.mark.parametrize("role", preservation.ROLES)
@pytest.mark.parametrize("edit", preservation.EDITS)
def test_customized_builtin_preserved_on_postgres(role, edit):
    preservation.test_customized_builtin_keeps_identity_and_references(None, role, edit)


@pytest.mark.parametrize("role", preservation.ROLES)
def test_preserved_renamed_role_install_on_postgres(role):
    preservation.test_preserved_renamed_role_satisfies_template_without_overwrite(None, role)


@pytest.mark.parametrize("role", preservation.ROLES)
def test_old_builtin_snapshot_preserves_customized_role_on_postgres(role):
    preservation.test_old_builtin_snapshot_cannot_overwrite_preserved_role(None, role)


@pytest.mark.parametrize("role", preservation.ROLES)
@pytest.mark.parametrize("incoming_clock", [5, 15], ids=["stale", "newer"])
def test_legacy_owner_snapshot_keeps_original_order_on_postgres(role, incoming_clock):
    from app.core.builtin_subagents import seed_builtin_subagents
    from app.db import database, network as store
    from app.network import engine, policy

    defaults = create_unchanged_builtin(role)
    database.update_profile(role, name="Existing owner edit", allow_builtin=True)
    seed_builtin_subagents()
    coll = policy.collection("agents")
    incoming = engine.serialize_row(coll, {**defaults, "name": "Legacy peer owner edit"})
    item = {"record_id": role, "clock": incoming_clock, "origin_device_id": "legacy-peer",
            "deleted": False, "record": incoming}
    with database.get_db() as conn:
        cursor = conn.cursor()
        try:
            current = engine.serialize_row(coll, engine._read_row(cursor, coll, role))
            store.put_record(cursor, "agents", role, clock=10, origin_device_id="upgraded-peer",
                             content_hash=engine.content_hash(coll, current), deleted=False)
            applied, _ = engine.apply_snapshot_items(cursor, coll, [item], ctx=None)
            assert applied == int(incoming_clock > 10)
            row = engine._read_row(cursor, coll, role)
            assert row["name"] == ("Legacy peer owner edit" if incoming_clock > 10 else "Existing owner edit")
            assert not row["is_builtin"]
            synced = store.get_record(cursor, "agents", role)
            assert synced["clock"] == max(10, incoming_clock)
            assert synced["origin_device_id"] == ("legacy-peer" if incoming_clock > 10 else "upgraded-peer")
            assert synced["content_hash"] == engine.content_hash(coll, engine.serialize_row(coll, row))
            assert item["record"]["is_builtin"]  # no mutation of incoming wire data
            # Even a later default cannot replace the retained owner edit.
            untouched = {**item, "clock": 100, "record": engine.serialize_row(coll, defaults)}
            assert engine.apply_snapshot_items(cursor, coll, [untouched], ctx=None)[0] == 0
            assert engine._read_row(cursor, coll, role) == row
        finally:
            conn.rollback()  # Keep shared real-PG sync tables unchanged.


@pytest.mark.parametrize("role", preservation.ROLES)
@pytest.mark.parametrize("delete_rule", [False, True])
def test_permission_preservation_snapshot_frontier_on_postgres(role, delete_rule):
    from app.core.builtin_subagents import seed_builtin_subagents
    from app.db import database, network as store
    from app.network import engine, policy

    engine.reset_for_tests()
    assert store.get_state() is None
    ctx = engine.create_network("Disposable permission-only preservation")
    try:
        create_unchanged_builtin(role)
        engine.scan_collection("agents")
        before_permission = store.local_watermarks()
        with database.get_db() as conn:
            original = store.get_record(conn.cursor(), "agents", role)
        rule = database.add_permission_rule(role, "Bash", "*", "deny")
        engine.scan_collection("permission_rules")
        assert not store.requires_snapshot(before_permission)
        seed_builtin_subagents()
        engine.scan_collection("agents")
        if delete_rule:
            database.delete_permission_rule(rule["id"])
            engine.scan_collection("permission_rules")
        assert store.exchange_batch(before_permission)[2]
        assert not store.requires_snapshot(before_permission, exclude_origin=ctx.device_id)
        assert not store.exchange_batch(store.local_watermarks())[2]
        agent_items = engine.open_snapshot_chunk(engine.snapshot_chunk("agents"), ctx=ctx)["items"]
        permission_items = engine.open_snapshot_chunk(engine.snapshot_chunk("permission_rules"), ctx=ctx)["items"]
        agent_item = next(item for item in agent_items if item["record_id"] == role)
        assert not agent_item["record"]["is_builtin"]
        assert (agent_item["clock"], agent_item["origin_device_id"]) == (
            original["clock"], original["origin_device_id"])
        # Model the retired receiver, applying the real producer's signed copy
        # in dependency order, without leaving fixtures in shared sync tables.
        with database.get_db() as conn:
            cursor = conn.cursor()
            try:
                cursor.execute("DELETE FROM permission_rules WHERE profile_id=%s", (role,))
                cursor.execute("DELETE FROM agents WHERE id=%s", (role,))
                cursor.execute("DELETE FROM sync_records WHERE collection='agents' AND record_id=%s", (role,))
                cursor.execute("DELETE FROM sync_records WHERE collection='permission_rules' AND record_id=%s", (rule["id"],))
                engine.apply_snapshot_items(cursor, policy.collection("agents"), agent_items, ctx=ctx)
                engine.apply_snapshot_items(cursor, policy.collection("permission_rules"), permission_items, ctx=ctx)
                assert not engine._read_row(cursor, policy.collection("agents"), role)["is_builtin"]
                cursor.execute("SELECT decision FROM permission_rules WHERE profile_id=%s", (role,))
                assert [row["decision"] for row in cursor.fetchall()] == ([] if delete_rule else ["deny"])
                held = store.get_record(cursor, "agents", role)
                assert (held["clock"], held["origin_device_id"]) == (original["clock"], original["origin_device_id"])
            finally:
                conn.rollback()
    finally:
        engine.leave_network()
        engine.reset_for_tests()
