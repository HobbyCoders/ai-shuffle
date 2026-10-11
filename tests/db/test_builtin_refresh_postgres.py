"""Start-up refresh of uncustomised built-in text on disposable PostgreSQL.

Runs the SQLite-shim seeding tests against a real database: JSONB config,
``SELECT ... FOR UPDATE`` and the row comparisons behave as in production.
"""
from tests.db.postgres_harness import pg_schema, requires_real_pg  # noqa: F401

pytestmark = requires_real_pg


def _drop_builtins():
    from app.core.builtin_subagents import get_builtin_subagent_ids
    from app.db import base

    with base.get_db() as conn:
        conn.cursor().execute("DELETE FROM agents WHERE id = ANY(%s)", (get_builtin_subagent_ids(),))


def test_refresh_contracts_on_postgres(monkeypatch):
    from tests.api import test_builtin_seeding as seeding

    _drop_builtins()
    seeding.test_fresh_install_seeds_current_defaults_with_a_record(None)
    for test in (
        seeding.test_startup_refreshes_unmodified_old_general_purpose,
        seeding.test_startup_refreshes_unmodified_old_frontend_designer_prompt,
        seeding.test_second_startup_changes_nothing,
        seeding.test_customised_prompt_is_kept_while_untouched_description_moves,
        seeding.test_customised_description_is_kept_while_untouched_prompt_moves,
        seeding.test_fully_customised_row_is_not_written,
        seeding.test_record_from_a_newer_build_is_never_downgraded,
        seeding.test_owner_text_matching_an_older_default_is_kept_once_recorded,
    ):
        test(None)
    _drop_builtins()
    seeding.test_plan_retirement_refreshes_recorded_general_purpose_description(None, monkeypatch)
    _drop_builtins()
    with monkeypatch.context() as patched:
        seeding.test_future_default_change_refreshes_through_the_record(None, patched)
