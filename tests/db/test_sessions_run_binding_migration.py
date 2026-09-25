"""Migration tests for the per-session run binding (v79/v80/v81).

v79 adds three nullable TEXT columns — ``sessions.model``,
``sessions.effort``, ``sessions.permission_mode`` — v80 adds the nullable
INTEGER ``sessions.context_window`` selected alongside them, and v81 the
nullable BOOLEAN ``sessions.fast``.
Agents stopped pinning them, so the session row is where they live. Every
pre-existing row is NULL, which resolves through ``launched_model`` and then
the agent's legacy config — an upgrade reroutes nothing.

Mirrors tests/db/test_sessions_hidden.py (the v75 precedent these columns
follow exactly).
"""

from app.db import database
from tests.db.migration_helpers import column_names, schema_version


def test_fresh_db_has_binding_columns_and_version(mock_db):
    """A freshly built DB (create_postgres_schema) carries all three columns and
    the current schema version."""
    conn = mock_db
    assert {
        "model", "effort", "permission_mode", "context_window", "fast", "workspace_project_ids",
    } <= column_names(conn, "sessions")
    assert schema_version(conn) == database.SCHEMA_VERSION


def test_init_upgrades_pre_v79_db(mock_db):
    """EXISTING-DB path: a v78-shaped sessions table (no binding columns) is
    upgraded in place — the guarded ADD COLUMNs land and the version bumps, with
    the existing row preserved and NULL across all three."""
    conn = mock_db
    for column in ("model", "effort", "permission_mode", "context_window", "fast"):
        conn.execute(f"ALTER TABLE sessions DROP COLUMN {column}")
    conn.execute("DELETE FROM schema_version")
    conn.execute("INSERT INTO schema_version (version) VALUES (78)")
    conn.execute(
        "INSERT INTO sessions (id, profile_id, instance_id, launched_model) "
        "VALUES (?, ?, ?, ?)",
        ("s-legacy-binding", "default", "", "opus"),
    )
    conn.commit()

    assert "model" not in column_names(conn, "sessions")

    database.init_database()

    assert {
        "model", "effort", "permission_mode", "context_window", "fast",
    } <= column_names(conn, "sessions")
    assert schema_version(conn) == database.SCHEMA_VERSION

    row = database.get_session("s-legacy-binding")
    assert row["model"] is None
    assert row["effort"] is None
    assert row["permission_mode"] is None
    assert row["context_window"] is None
    # NULL, not False: an upgraded session runs standard without anyone having
    # picked that, and the client can still tell "never chosen" from an
    # explicit off.
    assert row["fast"] is None
    # The pre-v79 fallback the resolver reads is untouched.
    assert row["launched_model"] == "opus"


def test_session_model_exposes_the_binding():
    """The API response model surfaces the binding so a client can hydrate its
    controls from the row rather than from the agent."""
    from datetime import datetime, timezone
    from app.core.models import Session

    base = {
        "id": "s1",
        "profile_id": "default",
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }
    empty = Session(**base)
    assert (
        empty.model, empty.effort, empty.permission_mode, empty.context_window,
        empty.fast,
    ) == (None, None, None, None, None)

    bound = Session(
        **base,
        model="prov:op:openai/gpt-5.6",
        effort="high",
        permission_mode="plan",
        context_window=256_000,
        fast=True,
    )
    assert bound.model == "prov:op:openai/gpt-5.6"
    assert bound.effort == "high"
    assert bound.permission_mode == "plan"
    assert bound.context_window == 256_000
    assert bound.fast is True
