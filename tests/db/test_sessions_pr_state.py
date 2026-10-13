"""Persisted session PR lifecycle (v85)."""

import uuid

from app.db import database
from tests.db.migration_helpers import column_names, schema_version


def _mk(session_id=None, **kwargs):
    session_id = session_id or str(uuid.uuid4())
    database.create_session(session_id=session_id, profile_id="default", **kwargs)
    return session_id


def test_fresh_db_has_pr_columns_and_version(mock_db):
    conn = mock_db
    cols = column_names(conn, "sessions")
    assert "pr_number" in cols
    assert "pr_url" in cols
    assert "pr_state" in cols
    assert "pull_requests" in cols
    assert schema_version(conn) == database.SCHEMA_VERSION


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

    assert "pr_state" not in column_names(conn, "sessions")

    database.init_database()

    cols = column_names(conn, "sessions")
    assert "pr_number" in cols
    assert "pr_url" in cols
    assert "pr_state" in cols
    assert schema_version(conn) == database.SCHEMA_VERSION
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


def test_init_adds_pull_requests_and_keeps_single_pr_rows(mock_db):
    """An existing single-PR row survives the migration and keeps rendering."""
    conn = mock_db
    conn.execute("ALTER TABLE sessions DROP COLUMN pull_requests")
    conn.execute(
        "INSERT INTO sessions (id, profile_id, instance_id, pr_number, pr_url, pr_state)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        ("s-one-pr", "default", "", 5, "https://github.com/o/r/pull/5", "open"),
    )
    conn.commit()
    assert "pull_requests" not in column_names(conn, "sessions")

    database.init_database()

    assert "pull_requests" in column_names(conn, "sessions")
    row = database.get_session("s-one-pr")
    assert (row["pr_number"], row["pr_state"]) == (5, "open")
    assert database.decode_pull_requests(row["pull_requests"]) == []

    # The first new PR keeps the old one in the list instead of replacing it.
    row = database.update_session_pr_state(
        "s-one-pr", pr_number=6, pr_url="https://github.com/o/r/pull/6", pr_state="open"
    )
    assert row["pr_number"] == 6
    assert [(e["number"], e["state"]) for e in database.decode_pull_requests(row["pull_requests"])] == [
        (5, "open"), (6, "open"),
    ]


def test_update_session_pr_state_lists_every_pr(mock_db):
    sid = _mk()
    database.update_session_pr_state(
        sid, pr_number=1, pr_url="https://github.com/o/r/pull/1", pr_state="open",
        pr_title="First", pr_branch="feat/a",
    )
    database.update_session_pr_state(
        sid, pr_number=2, pr_url="https://github.com/o/r/pull/2", pr_state="open",
        pr_title="Second",
    )
    # A merge flip with no number targets the primary PR and keeps its title.
    row = database.update_session_pr_state(sid, pr_state="merged")
    entries = database.decode_pull_requests(row["pull_requests"])
    assert [(e["number"], e["state"], e["title"]) for e in entries] == [
        (1, "open", "First"), (2, "merged", "Second"),
    ]
    assert entries[0]["branch"] == "feat/a"
    assert (row["pr_number"], row["pr_state"]) == (2, "merged")


def test_non_primary_update_only_touches_the_list(mock_db):
    sid = _mk()
    database.update_session_pr_state(sid, pr_number=3, pr_url="https://github.com/o/r/pull/3", pr_state="open")
    row = database.update_session_pr_state(
        sid, pr_number=40, pr_url="https://github.com/o/r/pull/40", pr_state="open",
        pr_source="subagent", primary=False,
    )
    assert (row["pr_number"], row["pr_state"]) == (3, "open")
    entries = database.decode_pull_requests(row["pull_requests"])
    assert [(e["number"], e["source"]) for e in entries] == [(3, "session"), (40, "subagent")]
    # A later update by a different path keeps the first-recorded source.
    row = database.update_session_pr_state(sid, pr_number=40, pr_state="merged", primary=False)
    assert database.decode_pull_requests(row["pull_requests"])[1] == {
        "number": 40, "url": "https://github.com/o/r/pull/40", "state": "merged",
        "title": None, "branch": None, "source": "subagent",
    }


def test_same_number_in_another_repo_is_a_different_pr(mock_db):
    sid = _mk()
    database.update_session_pr_state(sid, pr_number=7, pr_url="https://github.com/o/a/pull/7", pr_state="open")
    row = database.update_session_pr_state(
        sid, pr_number=7, pr_url="https://github.com/o/b/pull/7", pr_state="merged", primary=False,
    )
    entries = database.decode_pull_requests(row["pull_requests"])
    assert [(e["url"], e["state"]) for e in entries] == [
        ("https://github.com/o/a/pull/7", "open"), ("https://github.com/o/b/pull/7", "merged"),
    ]
    listing = database.list_sessions_listing_pr(7, "https://github.com/o/b/pull/7")
    assert [r["id"] for r in listing] == [sid]
    assert database.list_sessions_listing_pr(70) == []


def test_list_sessions_listing_pr_matches_exact_number(mock_db):
    a = _mk()
    b = _mk()
    database.update_session_pr_state(a, pr_number=12, pr_url="https://github.com/o/r/pull/12", pr_state="open")
    database.update_session_pr_state(b, pr_number=3, pr_url="https://github.com/o/r/pull/3", pr_state="open")
    database.update_session_pr_state(b, pr_number=123, pr_url="https://github.com/o/r/pull/123", pr_state="open")
    assert {r["id"] for r in database.list_sessions_listing_pr(12)} == {a}
    assert {r["id"] for r in database.list_sessions_listing_pr(123)} == {b}
    assert {r["id"] for r in database.list_sessions_listing_pr(3)} == {b}


def test_pull_request_list_is_capped(mock_db):
    sid = _mk()
    from app.db.sessions import MAX_SESSION_PULL_REQUESTS
    for n in range(1, MAX_SESSION_PULL_REQUESTS + 6):
        database.update_session_pr_state(sid, pr_number=n, pr_state="open")
    entries = database.decode_pull_requests(database.get_session(sid)["pull_requests"])
    assert len(entries) == MAX_SESSION_PULL_REQUESTS
    assert entries[-1]["number"] == MAX_SESSION_PULL_REQUESTS + 5
