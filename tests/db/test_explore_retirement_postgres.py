"""Upgrade authorization and stored-wake contracts on disposable PostgreSQL."""
import pytest

from tests.db.postgres_harness import pg_schema, requires_real_pg  # noqa: F401

pytestmark = requires_real_pg

# Rows this module (and the SQLite tests it drives) creates by fixed id.
_SESSIONS = ("retirement-chat", "missing-wake-chat")
_API_USERS = ("pre-upgrade-key", "retirement-only", "retirement-mixed", "retirement-open",
              "only-helper", "helper-and-other", "open-key")
_AGENTS = ("Explore", "retirement-other", "wake-fallback", "helper", "other")


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


async def test_upgrade_keeps_restricted_key_and_cancels_stored_wakes():
    from app.db import base, database
    from tests.api.test_builtin_seeding import (
        test_retirement_preserves_key_scope_and_cancels_stored_continuations,
        test_missing_stored_wake_profile_drops_once_with_visible_reason,
    )

    # Restriction is already stored BEFORE the upgrade. Recreate the shipped
    # constraint, then drive the real startup migration and retirement.
    database.create_profile("Explore", "Old built-in", None, {}, is_builtin=True)
    database.create_api_user("pre-upgrade-key", "Existing restricted key", "pre-upgrade-hash", profile_ids=["Explore"])
    with base.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("ALTER TABLE api_user_profiles ADD CONSTRAINT api_user_profiles_profile_id_fkey "
                       "FOREIGN KEY (profile_id) REFERENCES agents(id) ON DELETE CASCADE")
    database.init_database()
    from app.core.builtin_subagents import seed_builtin_subagents
    from app.core.api_user_scope import worker_refusal
    seed_builtin_subagents()
    assert database.get_profile("Explore") is None
    assert database.get_api_user("pre-upgrade-key")["profile_ids"] == ["Explore"]
    assert worker_refusal("pre-upgrade-key", "general-purpose") is not None
    await test_retirement_preserves_key_scope_and_cancels_stored_continuations(None)
    await test_missing_stored_wake_profile_drops_once_with_visible_reason(None)
    base.close_pool()
    base.init_pool(min_conn=1, max_conn=6)
    assert worker_refusal("pre-upgrade-key", "general-purpose") is not None
    assert worker_refusal("retirement-mixed", "general-purpose") is not None
    assert worker_refusal("retirement-mixed", "retirement-other") is None


def test_recreated_agent_id_does_not_inherit_grants_on_postgres():
    from tests.api.test_api_user_scope_regressions import (
        test_recreated_agent_id_does_not_inherit_the_deleted_agents_key_grants,
    )

    test_recreated_agent_id_does_not_inherit_the_deleted_agents_key_grants(None)
