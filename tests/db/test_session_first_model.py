"""``sessions.first_model`` records what a session STARTED on.

``launched_model`` is rewritten on every mid-session model switch, so it
answers "what is this on now" and nothing answers "what did it start on". An
audited session ran turns 1-6 on grok-4.5 and 7-16 on grok-4.6 and was recorded
as grok-4.6 only — its first six turns could not be attributed to the model
that actually served them.

``first_model`` is written by the same statement that writes
``launched_model``, under a COALESCE, so it is set once by whichever spawn path
happens to be first and no later write can move it.
"""

from __future__ import annotations

import sqlite3
from unittest.mock import patch

import pytest

import app.db.sessions as sessions_mod


def _create_schema(cursor):
    cursor.execute(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            profile_id TEXT,
            title TEXT,
            status TEXT DEFAULT 'active',
            sdk_session_id TEXT,
            total_cost_usd REAL DEFAULT 0,
            total_tokens_in BIGINT DEFAULT 0,
            total_tokens_out BIGINT DEFAULT 0,
            total_cache_read_tokens BIGINT DEFAULT 0,
            total_cache_creation_tokens BIGINT DEFAULT 0,
            last_input_tokens INTEGER DEFAULT 0,
            cache_creation_tokens INTEGER DEFAULT 0,
            cache_read_tokens INTEGER DEFAULT 0,
            turn_count INTEGER DEFAULT 0,
            total_model_calls INTEGER DEFAULT 0,
            launched_model TEXT,
            first_model TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


@pytest.fixture
def test_db():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    _create_schema(conn.cursor())
    conn.commit()
    yield conn
    conn.close()


@pytest.fixture
def db(test_db, monkeypatch):
    """Point ``app.db.sessions`` at the in-memory schema above."""
    class _Ctx:
        def __enter__(self):
            return test_db

        def __exit__(self, *a):
            test_db.commit()
            return False

    monkeypatch.setattr(sessions_mod, "get_db", lambda: _Ctx())
    monkeypatch.setattr(sessions_mod, "get_cursor", lambda conn: conn.cursor())
    monkeypatch.setattr(sessions_mod, "ph", lambda: "?")
    test_db.execute("INSERT INTO sessions (id) VALUES ('s1')")
    test_db.commit()
    return test_db


def _row(db):
    return db.execute("SELECT * FROM sessions WHERE id = 's1'").fetchone()


def test_first_launch_sets_both_columns(db):
    sessions_mod.update_session("s1", launched_model="grok-4.5")

    row = _row(db)
    assert row["launched_model"] == "grok-4.5"
    assert row["first_model"] == "grok-4.5"


def test_a_mid_session_switch_moves_only_launched_model(db):
    sessions_mod.update_session("s1", launched_model="grok-4.5")
    sessions_mod.update_session("s1", launched_model="grok-4.6")

    row = _row(db)
    assert row["launched_model"] == "grok-4.6"
    assert row["first_model"] == "grok-4.5"


def test_repeated_switches_never_move_it(db):
    for model in ("grok-4.5", "grok-4.6", "claude-opus-5", "grok-4.6"):
        sessions_mod.update_session("s1", launched_model=model)

    row = _row(db)
    assert row["first_model"] == "grok-4.5"
    assert row["launched_model"] == "grok-4.6"


def test_an_update_without_a_model_touches_neither(db):
    sessions_mod.update_session("s1", launched_model="grok-4.5")
    sessions_mod.update_session("s1", turn_increment=1, model_calls_increment=3)

    row = _row(db)
    assert row["first_model"] == "grok-4.5"
    assert row["launched_model"] == "grok-4.5"


def test_a_pre_existing_row_is_not_back_stamped(db):
    """A row that never ran through the new write path stays NULL rather than
    claiming the model it happens to be on now."""
    row = _row(db)
    assert row["first_model"] is None


def test_it_is_in_the_row_contract():
    assert "first_model" in sessions_mod.SessionRow.__annotations__


def test_it_is_in_the_schema_and_the_migration():
    from pathlib import Path

    schema = Path("app/db/schema_postgres.py").read_text(encoding="utf-8")
    assert "first_model TEXT," in schema

    migrations = Path("app/db/database.py").read_text(encoding="utf-8")
    assert 'column_exists(cursor, "sessions", "first_model")' in migrations
    assert "ALTER TABLE sessions ADD COLUMN first_model TEXT" in migrations


def test_it_is_exported():
    """Attribution nobody can read is not attribution."""
    from app.core.models import Session

    assert "first_model" in Session.model_fields

    from pathlib import Path

    export = Path("app/api/sessions_export.py").read_text(encoding="utf-8")
    assert '"first_model": session.get("first_model")' in export
