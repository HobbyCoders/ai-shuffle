"""Upgrade authorization and stored-wake contracts on disposable PostgreSQL."""
from tests.db.postgres_harness import pg_schema, requires_real_pg  # noqa: F401

pytestmark = requires_real_pg


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
