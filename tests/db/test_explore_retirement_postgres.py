"""Upgrade authorization and stored-wake contracts on disposable PostgreSQL."""
import pytest

from tests.db.postgres_harness import pg_schema, requires_real_pg  # noqa: F401

pytestmark = requires_real_pg

# Rows this module (and the SQLite tests it drives) creates by fixed id.
_SESSIONS = ("retirement-chat", "missing-wake-chat", "incoming-retired-chat")
_API_USERS = ("pre-upgrade-key", "retirement-only", "retirement-mixed", "retirement-open",
              "only-helper", "helper-and-other", "open-key")
_AGENTS = ("Explore", "Plan", "retirement-other", "wake-fallback", "helper", "other")


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
        cursor.execute("DELETE FROM agents WHERE id = ANY(%s)", (list(_AGENTS),))


@pytest.fixture(autouse=True)
def _isolated_rows():
    """The real-PostgreSQL CI step shares one database between modules and
    nothing resets it between runs: start from, and leave, no fixed-id rows.
    (The module-scoped ``pg_schema`` pool is set up before this runs.)"""
    _forget_fixed_rows()
    yield
    _forget_fixed_rows()


@pytest.mark.parametrize("retired_id", ["Explore", "Plan"])
async def test_upgrade_keeps_restricted_key_and_cancels_stored_wakes(retired_id):
    from app.db import base, database
    from tests.api.test_builtin_seeding import (
        test_retirement_preserves_key_scope_and_cancels_stored_continuations,
        test_missing_stored_wake_profile_drops_once_with_visible_reason,
    )

    # Restriction is already stored BEFORE the upgrade. Recreate the shipped
    # constraint, then drive the real startup migration and retirement.
    database.create_profile(retired_id, "Old built-in", None, {}, is_builtin=True)
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
    assert worker_refusal("pre-upgrade-key", "general-purpose") is not None
    await test_retirement_preserves_key_scope_and_cancels_stored_continuations(None, retired_id)
    await test_missing_stored_wake_profile_drops_once_with_visible_reason(None)
    base.close_pool()
    base.init_pool(min_conn=1, max_conn=6)
    assert worker_refusal("pre-upgrade-key", "general-purpose") is not None
    assert worker_refusal("retirement-mixed", "general-purpose") is not None
    assert worker_refusal("retirement-mixed", "retirement-other") is None


@pytest.mark.parametrize("retired_id", ["Explore", "Plan"])
@pytest.mark.parametrize("agent_state", ["missing", "builtin", "owner-created"])
def test_materialize_retired_session_reference_on_postgres(retired_id, agent_state):
    from app.core.builtin_subagents import seed_builtin_subagents
    from app.db import database
    from app.network import engine, policy

    seed_builtin_subagents()
    database.create_profile(retired_id, retired_id, None, {}, is_builtin=agent_state != "owner-created")
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
