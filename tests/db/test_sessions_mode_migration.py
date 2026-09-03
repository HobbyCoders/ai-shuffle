"""Migration-ordering test for the session interaction-mode column (v74,
voice-mode ruling VM4 — docs/VOICE_MODE_PLAN.md).

v74 adds ONE nullable ``sessions.mode TEXT`` column: 'voice' for an OpenAI
Realtime call, NULL (= 'chat') for everything that came before. Additive/
nullable with NO index, so it is immune to the schema-pass-before-column-
migrations ordering trap; the guarded ADD COLUMN in ``_run_column_migrations``
is the upgrade path and the fresh ``create_postgres_schema`` CREATE TABLE
carries it.

Mirrors tests/db/test_sessions_provider_binding_migration.py (the v66
provider_id precedent this column follows exactly).
"""

from app.db import database


def _column_names(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _schema_version(conn):
    return conn.execute("SELECT version FROM schema_version").fetchone()[0]


def test_fresh_db_has_mode_column_and_version(mock_db):
    """A freshly built DB (create_postgres_schema) carries sessions.mode and the
    current schema version."""
    conn = mock_db
    assert "mode" in _column_names(conn, "sessions")
    assert _schema_version(conn) == database.SCHEMA_VERSION


def test_init_upgrades_pre_v74_db(mock_db):
    """EXISTING-DB path: a v73-shaped sessions table (no mode) is upgraded in
    place — the guarded ADD COLUMN lands mode and the version bumps, with the
    existing row preserved (its mode NULL → chat) and no exception."""
    conn = mock_db
    conn.execute("ALTER TABLE sessions DROP COLUMN mode")
    conn.execute("DELETE FROM schema_version")
    conn.execute("INSERT INTO schema_version (version) VALUES (73)")
    conn.execute(
        "INSERT INTO sessions (id, profile_id, instance_id) VALUES (?, ?, ?)",
        ("s-legacy-mode", "default", ""),
    )
    conn.commit()

    assert "mode" not in _column_names(conn, "sessions")

    database.init_database()

    assert "mode" in _column_names(conn, "sessions")
    assert _schema_version(conn) == database.SCHEMA_VERSION
    row = conn.execute(
        "SELECT mode FROM sessions WHERE id = 's-legacy-mode'"
    ).fetchone()
    assert row[0] is None


def test_column_migration_is_idempotent(mock_db):
    """The guarded ADD COLUMN re-runs cleanly when the column already exists."""
    conn = mock_db
    database.init_database()
    assert "mode" in _column_names(conn, "sessions")
    database.init_database()
    assert "mode" in _column_names(conn, "sessions")
    assert _schema_version(conn) == database.SCHEMA_VERSION


def test_create_session_stores_mode_and_defaults_null(mock_db):
    """``create_session`` persists an explicit mode and leaves it NULL otherwise."""
    code = database.create_session(session_id="s-code", profile_id="default")
    assert code["mode"] is None

    voice = database.create_session(
        session_id="s-voice", profile_id="default", mode="voice"
    )
    assert voice["mode"] == "voice"

    stored = mock_db.execute(
        "SELECT mode FROM sessions WHERE id = 's-voice'"
    ).fetchone()
    assert stored[0] == "voice"

    chat = database.create_session(
        session_id="s-chat", profile_id="default", mode="chat"
    )
    assert chat["mode"] == "chat"

    # 'code' is the NULL case, so asking for it explicitly stores nothing new.
    explicit_code = database.create_session(
        session_id="s-code-explicit", profile_id="default", mode="code"
    )
    assert explicit_code["mode"] is None

    # Blank/whitespace/unknown normalises to NULL rather than being stored.
    blank = database.create_session(
        session_id="s-blank", profile_id="default", mode="  "
    )
    assert blank["mode"] is None
    bogus = database.create_session(
        session_id="s-bogus", profile_id="default", mode="telepathy"
    )
    assert bogus["mode"] is None


def test_session_model_coalesces_null_mode_to_code():
    """The API model reads NULL/blank/unknown mode as 'code' and passes the
    other two modes through."""
    from datetime import datetime, timezone
    from app.core.models import Session

    base = {
        "id": "s1",
        "profile_id": "default",
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }
    assert Session(**base, mode=None).mode == "code"
    assert Session(**base).mode == "code"
    assert Session(**base, mode="").mode == "code"
    assert Session(**base, mode="telepathy").mode == "code"
    assert Session(**base, mode="chat").mode == "chat"
    assert Session(**base, mode="voice").mode == "voice"
