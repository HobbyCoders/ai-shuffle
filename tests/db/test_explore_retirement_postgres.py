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
