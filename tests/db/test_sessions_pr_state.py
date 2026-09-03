"""Persisted session PR lifecycle (v85)."""

import uuid

from app.db import database


def _column_names(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _schema_version(conn):
    return conn.execute("SELECT version FROM schema_version").fetchone()[0]


def _mk(session_id=None, **kwargs):
    session_id = session_id or str(uuid.uuid4())
    database.create_session(session_id=session_id, profile_id="default", **kwargs)
    return session_id


def test_fresh_db_has_pr_columns_and_version(mock_db):
    conn = mock_db
    cols = _column_names(conn, "sessions")
    assert "pr_number" in cols
    assert "pr_url" in cols
    assert "pr_state" in cols
    assert _schema_version(conn) == database.SCHEMA_VERSION


def test_init_upgrades_pre_v85_db(mock_db):
    conn = mock_db
    for col in ("pr_number", "pr_url", "pr_state"):
        conn.execute(f"ALTER TABLE sessions DROP COLUMN {col}")
    conn.execute("DELETE FROM schema_version")
    conn.execute("INSERT INTO schema_version (version) VALUES (84)")
    conn.execute(
        "INSERT INTO sessions (id, profile_id, instance_id) VALUES (?, ?, ?)",
        ("s-legacy-pr", "default", ""),
    )
    conn.commit()

    assert "pr_state" not in _column_names(conn, "sessions")

    database.init_database()

    cols = _column_names(conn, "sessions")
    assert "pr_number" in cols
    assert "pr_url" in cols
    assert "pr_state" in cols
    assert _schema_version(conn) == database.SCHEMA_VERSION
    row = conn.execute(
        "SELECT pr_number, pr_url, pr_state FROM sessions WHERE id = 's-legacy-pr'"
    ).fetchone()
    assert row[0] is None
    assert row[1] is None
    assert row[2] is None


def test_update_session_pr_state_writes_fields_and_keeps_active(mock_db):
    sid = _mk()
    database.set_session_active(sid, True)

    row = database.update_session_pr_state(
        sid, pr_number=12, pr_url="https://github.com/o/r/pull/12", pr_state="merged"
    )
    assert row["pr_number"] == 12
    assert row["pr_url"] == "https://github.com/o/r/pull/12"
    assert row["pr_state"] == "merged"
    assert row["is_active"]

    again = database.get_session(sid)
    assert again["pr_state"] == "merged"
    assert again["is_active"]


def test_list_sessions_for_branch_matches_worktree_or_logical(mock_db):
    database.create_project("p1", "P1", None, "/tmp/p1")
    database.create_project("p2", "P2", None, "/tmp/p2")
    a = _mk(project_id="p1", worktree_branch="feat/x")
    b = _mk(project_id="p1")
    database.set_session_branch(b, "feat/x")
    _mk(project_id="p1", worktree_branch="other")
    _mk(project_id="p2", worktree_branch="feat/x")

    ids = {s["id"] for s in database.list_sessions_for_branch("p1", "feat/x")}
    assert ids == {a, b}


def test_list_sessions_by_pr_number(mock_db):
    a = _mk()
    b = _mk()
    database.update_session_pr_state(a, pr_number=7, pr_state="open")
    database.update_session_pr_state(b, pr_number=8, pr_state="open")
    ids = {s["id"] for s in database.list_sessions_by_pr_number(7)}
    assert ids == {a}
