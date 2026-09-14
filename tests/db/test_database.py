"""
Comprehensive unit tests for app/db/database.py.

Tests cover:
- Database connection and context management
- Admin operations (CRUD)
- Profile operations (CRUD, builtin flags)
- Project operations (CRUD)
- Session operations (CRUD, favorites, forks, tags)
- Session message operations (add, delete, search)
- API user operations (CRUD, credentials, GitHub config)
- Auth session operations (create, validate, cleanup)
- Usage logging and statistics
- Checkpoint operations
- User preferences
- Subagent operations
- Permission rules
- System settings
- Tag operations
- Template operations
- Webhook operations
- Rate limit operations
- Knowledge base operations
- Git repository and worktree operations
- Agent run, task, and log operations
- Analytics operations
- Audit logging
- Login attempts and lockout
- Sync log operations
"""

import logging
import pytest
import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from contextlib import contextmanager

from app.db import database as db


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def test_db():
    """Create an in-memory SQLite database whose schema is the canonical
    PostgreSQL schema, built via the test shim so it never drifts from the app.
    """
    from tests.pg_shim import new_sqlite_connection
    conn = new_sqlite_connection()
    yield conn
    conn.close()


@pytest.fixture
def mock_db(test_db):
    """Route the app DB layer through the shim-wrapped test connection.

    Domain code writes PostgreSQL SQL (``%s`` placeholders, ``NOW()``, ``JSONB``
    …) and reaches the DB via ``get_connection``. Wrapping the raw connection in
    a ``ShimConnection`` translates that SQL to SQLite and returns dict rows.
    The fixture still yields the raw connection so tests that run their own
    ``?``-placeholder SQL directly keep working.
    """
    from tests.pg_shim import route_db_to
    with route_db_to(test_db):
        yield test_db


@pytest.fixture
def setup_profile(mock_db):
    """Create a test profile for use in tests that require foreign key references."""
    profile = db.create_profile(
        "test-profile", "Test Profile", "A test profile",
        {"model": "claude-3"}, False, []
    )
    return profile["id"]


@pytest.fixture
def setup_project(mock_db):
    """Create a test project for use in tests that require foreign key references."""
    project = db.create_project(
        "test-project", "Test Project", "A test project",
        "/path/to/project", {}
    )
    return project["id"]


@pytest.fixture
def setup_api_user(mock_db):
    """Insert a test API user directly into the database."""
    now = datetime.now(timezone.utc).isoformat()
    mock_db.execute(
        """INSERT INTO api_users (id, name, api_key_hash, is_active, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        ("user-1", "Test", "hash", True, now, now)
    )
    mock_db.commit()
    return "user-1"


# =============================================================================
# Helper Functions Tests
# =============================================================================

class TestHelperFunctions:
    """Test helper functions."""

    def test_row_to_dict_with_row(self, test_db):
        """row_to_dict should convert sqlite3.Row to dict."""
        cursor = test_db.cursor()
        cursor.execute("SELECT 1 as a, 'test' as b")
        row = cursor.fetchone()
        result = db.row_to_dict(row)

        assert result == {"a": 1, "b": "test"}

    def test_row_to_dict_with_none(self):
        """row_to_dict should return None for None input."""
        assert db.row_to_dict(None) is None

    def test_rows_to_list(self, test_db):
        """rows_to_list should convert list of sqlite3.Row to list of dicts."""
        cursor = test_db.cursor()
        cursor.execute("SELECT 1 as x UNION SELECT 2 as x")
        rows = cursor.fetchall()
        result = db.rows_to_list(rows)

        assert len(result) == 2
        assert all(isinstance(r, dict) for r in result)


class TestWorktreeStatus:
    """Test WorktreeStatus constants."""

    def test_worktree_status_values(self):
        """WorktreeStatus should have correct status values."""
        assert db.WorktreeStatus.ACTIVE == "active"
        assert db.WorktreeStatus.DELETED == "deleted"
        assert db.WorktreeStatus.ORPHANED == "orphaned"


# =============================================================================
# Admin Operations Tests
# =============================================================================

class TestAdminOperations:
    """Test admin-related database operations."""

    def test_is_setup_required_true_when_no_admin(self, mock_db):
        """is_setup_required should return True when no admin exists."""
        assert db.is_setup_required() is True

    def test_is_setup_required_false_when_admin_exists(self, mock_db):
        """is_setup_required should return False when admin exists."""
        db.create_admin("admin", "hashed_password")
        assert db.is_setup_required() is False

    def test_create_admin(self, mock_db):
        """create_admin should create admin user."""
        result = db.create_admin("admin", "hashed_password")

        assert result["id"] == 1
        assert result["username"] == "admin"

    def test_get_admin(self, mock_db):
        """get_admin should return admin user."""
        db.create_admin("admin", "hashed_password")
        result = db.get_admin()

        assert result is not None
        assert result["username"] == "admin"

    def test_get_admin_none_when_not_exists(self, mock_db):
        """get_admin should return None when no admin exists."""
        assert db.get_admin() is None

    def test_update_admin_password(self, mock_db):
        """update_admin_password should update password hash."""
        db.create_admin("admin", "old_hash")
        result = db.update_admin_password("new_hash")

        assert result is True
        admin = db.get_admin()
        assert admin["password_hash"] == "new_hash"



# =============================================================================
# Profile Operations Tests
# =============================================================================

class TestProfileOperations:
    """Test profile-related database operations."""

    def test_create_profile(self, mock_db):
        """create_profile should create a new profile."""
        config = {"model": "claude-3", "max_tokens": 4096}
        result = db.create_profile(
            "profile-1", "Test Profile", "Description", config, False, ["tool1"]
        )

        assert result["id"] == "profile-1"
        assert result["name"] == "Test Profile"
        assert result["config"] == config
        assert result["mcp_tools"] == ["tool1"]

    def test_get_profile(self, mock_db):
        """get_profile should return profile by ID."""
        db.create_profile("profile-1", "Test", None, {})
        result = db.get_profile("profile-1")

        assert result is not None
        assert result["id"] == "profile-1"

    def test_get_profile_not_found(self, mock_db):
        """get_profile should return None for non-existent profile."""
        assert db.get_profile("nonexistent") is None

    def test_get_all_profiles(self, mock_db):
        """get_all_profiles should return all profiles ordered by builtin then name."""
        db.create_profile("profile-b", "B Profile", None, {}, False)
        db.create_profile("profile-a", "A Profile", None, {}, True)

        result = db.get_all_profiles()

        assert len(result) == 2
        assert result[0]["is_builtin"]  # Builtin first (SQLite returns 1 for True)
        assert result[1]["name"] == "B Profile"

    def test_update_profile(self, mock_db):
        """update_profile should update profile fields."""
        db.create_profile("profile-1", "Old Name", None, {"key": "value"})
        result = db.update_profile("profile-1", name="New Name", config={"key": "new"})

        assert result["name"] == "New Name"
        assert result["config"] == {"key": "new"}

    def test_update_profile_builtin_blocked(self, mock_db):
        """update_profile should block updates to builtin profiles."""
        db.create_profile("profile-1", "Builtin", None, {}, True)
        result = db.update_profile("profile-1", name="Changed")

        assert result is None

    def test_update_profile_builtin_allowed(self, mock_db):
        """update_profile should allow updates to builtin with allow_builtin=True."""
        db.create_profile("profile-1", "Builtin", None, {}, True)
        result = db.update_profile("profile-1", name="Changed", allow_builtin=True)

        assert result["name"] == "Changed"

    def test_delete_profile(self, mock_db):
        """delete_profile should delete profile."""
        db.create_profile("profile-1", "Test", None, {})
        result = db.delete_profile("profile-1")

        assert result is True
        assert db.get_profile("profile-1") is None

    def test_delete_profile_not_found(self, mock_db):
        """delete_profile should return False for non-existent profile."""
        assert db.delete_profile("nonexistent") is False

    def test_set_profile_builtin(self, mock_db):
        """set_profile_builtin should update is_builtin flag."""
        db.create_profile("profile-1", "Test", None, {}, False)
        result = db.set_profile_builtin("profile-1", True)

        assert result is True
        profile = db.get_profile("profile-1")
        assert profile["is_builtin"]  # SQLite returns 1 for True


# =============================================================================
# Project Operations Tests
# =============================================================================

class TestProjectOperations:
    """Test project-related database operations."""

    def test_create_project(self, mock_db):
        """create_project should create a new project."""
        settings = {"git_enabled": True}
        result = db.create_project(
            "project-1", "Test Project", "Description", "/path/to/project", settings
        )

        assert result["id"] == "project-1"
        assert result["name"] == "Test Project"
        assert result["path"] == "/path/to/project"
        assert result["settings"] == settings

    def test_get_project(self, mock_db):
        """get_project should return project by ID."""
        db.create_project("project-1", "Test", None, "/path")
        result = db.get_project("project-1")

        assert result is not None
        assert result["id"] == "project-1"

    def test_get_all_projects(self, mock_db):
        """get_all_projects should return all projects ordered by name."""
        db.create_project("proj-b", "B Project", None, "/b")
        db.create_project("proj-a", "A Project", None, "/a")

        result = db.get_all_projects()

        assert len(result) == 2
        assert result[0]["name"] == "A Project"

    def test_update_project(self, mock_db):
        """update_project should update project fields."""
        db.create_project("project-1", "Old Name", None, "/path")
        result = db.update_project("project-1", name="New Name", settings_dict={"key": "val"})

        assert result["name"] == "New Name"
        assert result["settings"] == {"key": "val"}

    def test_delete_project(self, mock_db):
        """delete_project should delete project."""
        db.create_project("project-1", "Test", None, "/path")
        result = db.delete_project("project-1")

        assert result is True
        assert db.get_project("project-1") is None

    def test_get_projects_by_ids_matches_the_per_id_path(self, mock_db):
        """The batched reader must agree with ``get_project`` per id, on every
        field the session-list enrichment reads (name / path / link_group_id)."""
        for i in range(3):
            db.create_project(f"proj-{i}", f"P{i}", None, f"/p{i}")
        db.set_project_link_group("proj-1", "git:group")
        db.create_project("proj-other", "Other", None, "/other")  # not requested

        wanted = ["proj-0", "proj-1", "proj-2"]
        batched = db.get_projects_by_ids(wanted)
        assert sorted(batched) == wanted
        for pid in wanted:
            single = db.get_project(pid)
            for field in ("id", "name", "path", "link_group_id"):
                assert batched[pid][field] == single[field], (pid, field)

    def test_get_projects_by_ids_omits_missing_ids(self, mock_db):
        """A missing id is simply absent — same as ``get_project`` returning
        None — never a placeholder row."""
        db.create_project("proj-0", "P0", None, "/p0")
        batched = db.get_projects_by_ids(["proj-0", "missing"])
        assert list(batched) == ["proj-0"]
        assert db.get_project("missing") is None

    def test_get_projects_by_ids_empty_input_short_circuits(self, mock_db):
        """``WHERE id IN ()`` is a Postgres syntax error — return early."""
        db.create_project("proj-0", "P0", None, "/p0")
        assert db.get_projects_by_ids([]) == {}
        assert db.get_projects_by_ids([None, ""]) == {}
        assert db.get_projects_by_ids(iter([])) == {}

    def test_get_projects_by_ids_is_not_instance_filtered(self, mock_db):
        """Deliberate: mirrors ``get_project``, not ``get_all_projects``, so a
        session owned by ANOTHER install still resolves its project name."""
        db.create_project("proj-0", "P0", None, "/p0")
        from app.db.base import get_db

        with get_db() as conn:
            conn.cursor().execute(
                "UPDATE projects SET instance_id = %s WHERE id = %s",
                ("some-other-install", "proj-0"),
            )

        assert db.get_all_projects() == []  # instance-filtered: invisible here
        assert db.get_projects_by_ids(["proj-0"])["proj-0"]["name"] == "P0"

    def test_get_projects_by_ids_falls_back_to_per_id_reads(self, mock_db):
        """A failed batch read degrades to one ``get_project`` per DISTINCT id,
        never to no data at all."""
        from app.db import projects as projects_db

        rows = {
            "proj-0": {"id": "proj-0", "name": "P0"},
            "proj-1": {"id": "proj-1", "name": "P1"},
        }
        with patch.object(projects_db, "get_db",
                          side_effect=RuntimeError("batch read down")), \
                patch.object(projects_db, "get_project",
                             side_effect=rows.get) as per_id:
            resolved = projects_db.get_projects_by_ids(
                ["proj-0", "proj-0", "proj-1", "missing"]
            )

        assert sorted(resolved) == ["proj-0", "proj-1"]
        assert resolved["proj-1"]["name"] == "P1"
        # Per DISTINCT id, never per requested id.
        assert [c.args[0] for c in per_id.call_args_list] == [
            "proj-0", "proj-1", "missing",
        ]

    def test_get_projects_by_ids_fallback_survives_a_single_bad_id(self, mock_db):
        """One raising ``get_project`` must not lose the other rows."""
        from app.db import projects as projects_db

        def flaky(pid):
            if pid == "boom":
                raise RuntimeError("row read failed")
            return {"id": pid, "name": "P0"}

        with patch.object(projects_db, "get_db",
                          side_effect=RuntimeError("batch read down")), \
                patch.object(projects_db, "get_project", side_effect=flaky):
            resolved = projects_db.get_projects_by_ids(["boom", "proj-0"])
        assert list(resolved) == ["proj-0"]


# =============================================================================
# Session Operations Tests
# =============================================================================

class TestSessionOperations:
    """Test session-related database operations."""

    def test_create_session(self, mock_db, setup_profile):
        """create_session should create a new session."""
        result = db.create_session(
            "session-1", setup_profile, title="Test Session"
        )

        assert result["id"] == "session-1"
        assert result["profile_id"] == setup_profile
        assert result["title"] == "Test Session"
        assert result["status"] == "active"

    def test_get_session(self, mock_db, setup_profile):
        """get_session should return session by ID."""
        db.create_session("session-1", setup_profile)
        result = db.get_session("session-1")

        assert result is not None
        assert result["id"] == "session-1"

    def test_create_session_automation_provenance(self, mock_db, setup_profile):
        """automation_id persists on create and defaults to NULL."""
        auto = db.create_session(
            "session-auto", setup_profile, automation_id="auto-nightly"
        )
        assert auto["automation_id"] == "auto-nightly"
        stored = db.get_session("session-auto")
        assert stored is not None
        assert stored["automation_id"] == "auto-nightly"

        plain = db.create_session("session-plain", setup_profile)
        assert plain["automation_id"] is None

    def test_get_sessions_with_filters(self, mock_db, setup_profile, setup_project):
        """get_sessions should filter by various criteria."""
        db.create_session("session-1", setup_profile, project_id=setup_project)
        db.create_session("session-2", setup_profile)

        result = db.get_sessions(project_id=setup_project)

        assert len(result) == 1
        assert result[0]["id"] == "session-1"

    def test_get_sessions_active_only(self, mock_db, setup_profile):
        """get_sessions should filter to active sessions."""
        db.create_session("session-1", setup_profile)
        db.create_session("session-2", setup_profile)
        db.set_session_active("session-1", True)

        result = db.get_sessions(active_only=True)

        assert len(result) == 1
        assert result[0]["id"] == "session-1"

    def test_update_session(self, mock_db, setup_profile):
        """update_session should update session fields."""
        db.create_session("session-1", setup_profile)
        result = db.update_session(
            "session-1",
            title="Updated Title",
            status="archived",
            cost_increment=0.05,
            tokens_in_increment=100,
            tokens_out_increment=200
        )

        assert result["title"] == "Updated Title"
        assert result["status"] == "archived"
        assert result["total_cost_usd"] == 0.05
        assert result["total_tokens_in"] == 100
        assert result["total_tokens_out"] == 200

    def test_update_session_ignores_none_increments(self, mock_db, setup_profile):
        """None increments must be skipped entirely.

        A None passes `!= 0`, so the old guards emitted
        `total_cost_usd = total_cost_usd + NULL`, which NULLs the column for
        good and then fails pydantic validation on GET /sessions/cursor.
        Providers that report no cost/usage (e.g. the fluid delegated leg) hit
        this on every turn.
        """
        db.create_session("session-1", setup_profile)
        db.update_session(
            "session-1",
            cost_increment=0.25,
            tokens_in_increment=10,
            tokens_out_increment=20,
            turn_increment=1,
        )

        result = db.update_session(
            "session-1",
            cost_increment=None,
            tokens_in_increment=None,
            tokens_out_increment=None,
            turn_increment=None,
        )

        assert result["total_cost_usd"] == 0.25
        assert result["total_tokens_in"] == 10
        assert result["total_tokens_out"] == 20
        assert result["turn_count"] == 1

    def test_delete_session(self, mock_db, setup_profile):
        """delete_session should delete session."""
        db.create_session("session-1", setup_profile)
        result = db.delete_session("session-1")

        assert result is True
        assert db.get_session("session-1") is None

    def test_set_session_active(self, mock_db, setup_profile):
        """set_session_active should set is_active to a specific value in both
        directions."""
        db.create_session("session-1", setup_profile)

        result = db.set_session_active("session-1", True)
        assert result["is_active"] == 1

        result = db.set_session_active("session-1", False)
        assert result["is_active"] == 0

    def test_create_session_is_active(self, mock_db, setup_profile):
        """create_session should persist is_active=True when passed, and default
        to False (inactive) when the param is omitted."""
        db.create_session("session-active", setup_profile, is_active=True)
        assert db.get_session("session-active")["is_active"] == 1

        db.create_session("session-default", setup_profile)
        assert db.get_session("session-default")["is_active"] == 0

    def test_session_has_forks(self, mock_db, setup_profile):
        """session_has_forks should detect forked sessions."""
        db.create_session("session-1", setup_profile)
        assert db.session_has_forks("session-1") is False

        db.create_session("session-2", setup_profile, parent_session_id="session-1")
        assert db.session_has_forks("session-1") is True

    def test_get_session_forks(self, mock_db, setup_profile):
        """get_session_forks should return forked sessions."""
        db.create_session("session-1", setup_profile)
        db.create_session("session-2", setup_profile, parent_session_id="session-1")

        result = db.get_session_forks("session-1")

        assert len(result) == 1
        assert result[0]["id"] == "session-2"

    def test_clear_session_conversation(self, mock_db, setup_profile):
        """clear_session_conversation wipes transcript + message rows + usage
        counters and unlinks sdk_session_id, but KEEPS the session row — the
        in-place "Clear session" reset. Returns the old sdk_session_id.

        The v2 event log (``session_events`` + its seq counter) must go too:
        the SSE feed replays it from seq 0 as the transcript authority, so
        rows left behind resurrect the "cleared" conversation on every fresh
        hydration regardless of the other wipes."""
        from app.db import session_events as se

        db.create_session("session-1", setup_profile)
        # Link an SDK session and rack up usage / chat history / transcript rows.
        db.update_session(
            "session-1", sdk_session_id="sdk-1",
            cost_increment=0.5, tokens_in_increment=100,
            tokens_out_increment=200, turn_increment=3,
        )
        db.add_session_message("session-1", "user", "hello")
        db.add_session_message("session-1", "assistant", "hi there")
        # …and a v2 event log with its seq counter.
        se.append_events("session-1", [
            {"type": "turn.started"},
            {"type": "assistant.text_delta", "payload": {"text": "hi there"}},
            {"type": "turn.completed"},
        ])
        assert se.get_max_seq("session-1") == 3
        mock_db.execute(
            "INSERT INTO session_store_entries (project_key, session_id, subpath, entry) "
            "VALUES (?, ?, ?, ?)",
            ("proj", "sdk-1", None, '{"role":"user"}'),
        )
        mock_db.execute(
            "INSERT INTO session_store_summaries (project_key, session_id, data, mtime_ms) "
            "VALUES (?, ?, ?, ?)",
            ("proj", "sdk-1", "{}", 1),
        )
        mock_db.commit()

        prev_sdk = db.clear_session_conversation("session-1")

        # Returned the old SDK id (so the caller can clean its JSONL/subprocess).
        assert prev_sdk == "sdk-1"
        # Session row SURVIVES, unlinked and zeroed.
        s = db.get_session("session-1")
        assert s is not None
        assert s["sdk_session_id"] is None
        assert s["total_cost_usd"] == 0
        assert s["total_tokens_in"] == 0
        assert s["total_tokens_out"] == 0
        assert s["turn_count"] == 0
        # Chat history + SDK transcript mirror wiped.
        assert db.get_session_messages("session-1") == []
        cur = mock_db.execute(
            "SELECT COUNT(*) FROM session_store_entries WHERE session_id = ?", ("sdk-1",)
        )
        assert cur.fetchone()[0] == 0
        cur = mock_db.execute(
            "SELECT COUNT(*) FROM session_store_summaries WHERE session_id = ?", ("sdk-1",)
        )
        assert cur.fetchone()[0] == 0
        # v2 event log wiped WITH its counter row.
        cur = mock_db.execute(
            "SELECT COUNT(*) FROM session_events WHERE session_id = ?", ("session-1",)
        )
        assert cur.fetchone()[0] == 0
        cur = mock_db.execute(
            "SELECT COUNT(*) FROM session_event_counters WHERE session_id = ?",
            ("session-1",),
        )
        assert cur.fetchone()[0] == 0
        # The counter reset puts the session in a brand-new session's state:
        # max_seq reads 0 and the next conversation allocates from seq 1.
        assert se.get_max_seq("session-1") == 0
        assert se.append_events("session-1", [{"type": "turn.started"}]) == (1, 1)

    def test_clear_session_conversation_no_sdk_session(self, mock_db, setup_profile):
        """With no SDK session linked yet there is no transcript to wipe — the
        message rows still go, the row stays, and the return is None."""
        db.create_session("session-1", setup_profile)
        db.add_session_message("session-1", "user", "hello")

        prev_sdk = db.clear_session_conversation("session-1")

        assert prev_sdk is None
        assert db.get_session_messages("session-1") == []
        assert db.get_session("session-1") is not None


# =============================================================================
# Session Message Operations Tests
# =============================================================================

class TestSessionMessageOperations:
    """Test session message operations."""

    def test_add_session_message(self, mock_db, setup_profile):
        """add_session_message should add message to session."""
        db.create_session("session-1", setup_profile)

        result = db.add_session_message(
            "session-1", "user", "Hello!",
            tool_name="test_tool",
            tool_input={"param": "value"},
            metadata={"key": "val"}
        )

        assert result["role"] == "user"
        assert result["content"] == "Hello!"
        assert result["tool_name"] == "test_tool"
        assert result["tool_input"] == {"param": "value"}
        assert result["metadata"] == {"key": "val"}

    def test_get_session_messages(self, mock_db, setup_profile):
        """get_session_messages should return all messages for session."""
        db.create_session("session-1", setup_profile)
        db.add_session_message("session-1", "user", "First")
        db.add_session_message("session-1", "assistant", "Second")

        result = db.get_session_messages("session-1")

        assert len(result) == 2
        assert result[0]["content"] == "First"
        assert result[1]["content"] == "Second"

    def test_delete_session_message(self, mock_db, setup_profile):
        """delete_session_message should delete specific message."""
        db.create_session("session-1", setup_profile)
        msg = db.add_session_message("session-1", "user", "Test")

        result = db.delete_session_message("session-1", msg["id"])

        assert result is True
        assert len(db.get_session_messages("session-1")) == 0

    def test_delete_session_messages_after(self, mock_db, setup_profile):
        """delete_session_messages_after should delete messages after given ID."""
        db.create_session("session-1", setup_profile)
        msg1 = db.add_session_message("session-1", "user", "First")
        db.add_session_message("session-1", "assistant", "Second")
        db.add_session_message("session-1", "user", "Third")

        deleted = db.delete_session_messages_after("session-1", msg1["id"])

        assert deleted == 2
        msgs = db.get_session_messages("session-1")
        assert len(msgs) == 1

    def test_update_message_content(self, mock_db, setup_profile):
        """update_message_content should update message content."""
        db.create_session("session-1", setup_profile)
        msg = db.add_session_message("session-1", "assistant", "Original")

        result = db.update_message_content(msg["id"], "Updated", metadata={"streaming": False})

        assert result["content"] == "Updated"
        assert result["metadata"]["streaming"] is False


class TestSearchSessions:
    """Test session search functionality."""

    @pytest.mark.skip(
        reason="search_sessions uses Postgres-native FTS (tsvector @@ plainto_tsquery, "
        "ts_headline, DISTINCT ON); not emulated by the sqlite test shim. No "
        "real-Postgres integration suite exists yet — the SQL's structure is "
        "guarded by TestSearchSessionsSql below; behavior needs manual "
        "verification against a Postgres instance."
    )
    def test_search_sessions_by_title(self, mock_db, setup_profile):
        """search_sessions should find sessions by title."""
        db.create_session("session-1", setup_profile, title="Python debugging help")
        db.create_session("session-2", setup_profile, title="JavaScript tutorial")

        result = db.search_sessions("Python")

        assert len(result) == 1
        assert result[0]["id"] == "session-1"

    @pytest.mark.skip(
        reason="search_sessions uses Postgres-native FTS (tsvector @@ plainto_tsquery, "
        "ts_headline, DISTINCT ON); not emulated by the sqlite test shim. No "
        "real-Postgres integration suite exists yet — the SQL's structure is "
        "guarded by TestSearchSessionsSql below; behavior needs manual "
        "verification against a Postgres instance."
    )
    def test_search_sessions_by_content(self, mock_db, setup_profile):
        """search_sessions should find sessions by message content."""
        db.create_session("session-1", setup_profile, title="Session 1")
        db.add_session_message("session-1", "user", "How do I use React hooks?")

        result = db.search_sessions("React hooks")

        assert len(result) == 1
        assert result[0]["id"] == "session-1"

    def test_search_sessions_empty_query(self, mock_db, setup_profile):
        """search_sessions should return empty for empty query."""
        db.create_session("session-1", setup_profile, title="Test")

        result = db.search_sessions("")

        assert result == []

    _STORE_SKIP = (
        "store-backed FTS uses Postgres-native constructs (sse_entry_text() "
        "IMMUTABLE function, to_tsvector/ts_headline/plainto_tsquery over the "
        "session_store_entries JSONB, DISTINCT ON, COUNT(*) OVER); not emulated "
        "by the sqlite test shim. Structure is guarded by TestSearchSessionsSql "
        "below; behavior needs a real Postgres instance."
    )

    @pytest.mark.skip(reason=_STORE_SKIP)
    def test_search_finds_store_content(self, mock_db, setup_profile):
        """A query hitting only the JSONB store transcript (not session_messages)
        should surface the session as a 'content' match — this is the whole point
        of store-backed search under postgres_only."""
        db.create_session("session-1", setup_profile, title="Session 1")
        # Under real PG: insert a session_store_entries row whose entry text
        # contains the query, with sessions.sdk_session_id joining it.
        result = db.search_sessions("needle-in-the-store")
        assert any(r["id"] == "session-1" for r in result)

    @pytest.mark.skip(reason=_STORE_SKIP)
    def test_search_dedupes_title_legacy_store(self, mock_db, setup_profile):
        """A session matching in title, legacy message content, AND the store must
        appear exactly once, with title winning as the retained match_type."""
        db.create_session("session-1", setup_profile, title="python")
        db.add_session_message("session-1", "user", "python question")
        result = db.search_sessions("python")
        ids = [r["id"] for r in result]
        assert ids.count("session-1") == 1
        row = next(r for r in result if r["id"] == "session-1")
        assert row["match_type"] == "title"

    @pytest.mark.skip(reason=_STORE_SKIP)
    def test_search_reports_match_count(self, mock_db, setup_profile):
        """match_count reflects how many of {title, legacy-content, store-content}
        hit for a session (counted before the final dedupe)."""
        db.create_session("session-1", setup_profile, title="python")
        db.add_session_message("session-1", "user", "python question")
        result = db.search_sessions("python")
        row = next(r for r in result if r["id"] == "session-1")
        assert row["match_count"] >= 2

    @pytest.mark.skip(reason=_STORE_SKIP)
    def test_search_excludes_archived(self, mock_db, setup_profile):
        """Archived sessions must be excluded from store matches just like the
        title/legacy CTEs."""
        db.create_session("session-1", setup_profile, title="keep")
        db.update_session("session-1", status="archived")
        result = db.search_sessions("keep")
        assert all(r["id"] != "session-1" for r in result)

    @pytest.mark.skip(reason=_STORE_SKIP)
    def test_sse_entry_text_extraction_matrix(self, mock_db):
        """Documents the sse_entry_text() contract (run against real Postgres):

        * user/assistant + string content    -> the string as-is
        * user/assistant + array content      -> string_agg of type='text' blocks
        * isMeta true / isSidechain true       -> NULL (skipped)
        * type NOT IN (user, assistant)        -> NULL (tool/system/summary skipped)
        * tool_use / tool_result array blocks  -> NULL (only 'text' blocks kept)
        """
        assert True  # Placeholder — the real matrix requires a PG session.


class TestSearchSessionsSql:
    """Always-on structural guard for the Postgres-native search SQL.

    The FTS behavior itself can't run under the sqlite shim (see the skips
    above), but the query's most fragile property is mechanical: the
    title_matches and content_matches CTEs feed ``SELECT * ... UNION ALL``,
    which silently mispairs values — or errors at runtime on Postgres only —
    the moment one CTE's column list drifts from the other (how a per-CTE
    column like is_active can be missed). Capture the SQL without a database
    and assert the
    two column lists stay symmetric.
    """

    @staticmethod
    def _split_top_level(cols: str) -> list:
        """Split a column list on commas, ignoring commas inside parens
        (ts_headline's arguments and options string contain both)."""
        parts, depth, buf = [], 0, []
        for ch in cols:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            if ch == "," and depth == 0:
                parts.append("".join(buf).strip())
                buf = []
            else:
                buf.append(ch)
        tail = "".join(buf).strip()
        if tail:
            parts.append(tail)
        return parts

    @staticmethod
    def _output_name(col: str) -> str:
        """The name a column contributes to the CTE's output row: its alias
        when present, else the bare name after the table qualifier."""
        lowered = col.lower()
        if " as " in lowered:
            return col[lowered.rindex(" as ") + 4:].strip()
        return col.rsplit(".", 1)[-1].strip()

    def _captured_search_sql(self) -> str:
        from unittest.mock import MagicMock

        captured = []
        cursor = MagicMock()
        cursor.fetchall.return_value = []
        cursor.execute.side_effect = lambda sql, params=None: captured.append(sql)

        with patch("app.db.sessions.get_db"), \
             patch("app.db.sessions.get_cursor", return_value=cursor):
            from app.db import sessions as db_sessions
            db_sessions.search_sessions("react hooks")

        assert captured, "search_sessions should have executed its query"
        return captured[0]

    def test_union_ctes_stay_column_symmetric(self):
        sql = self._captured_search_sql()

        title_cols = sql.split("WITH title_matches AS (", 1)[1]
        title_cols = title_cols.split("SELECT", 1)[1].split("FROM sessions s", 1)[0]
        content_cols = sql.split("content_matches AS (", 1)[1]
        content_cols = content_cols.split("SELECT DISTINCT ON (s.id)", 1)[1]
        content_cols = content_cols.split("FROM session_messages sm", 1)[0]

        title_names = [
            self._output_name(c) for c in self._split_top_level(title_cols)
        ]
        content_names = [
            self._output_name(c) for c in self._split_top_level(content_cols)
        ]

        assert title_names == content_names
        # UNION ALL pairs by position; duplicate output names would mask a
        # positional drift, so require them unique too.
        assert len(title_names) == len(set(title_names))

    def test_store_matches_cte_column_symmetric(self):
        """The store_matches CTE feeds the same UNION ALL, so its column list
        must stay position-symmetric with title_matches / content_matches or the
        UNION silently mispairs values (or errors on Postgres)."""
        sql = self._captured_search_sql()

        title_cols = sql.split("WITH title_matches AS (", 1)[1]
        title_cols = title_cols.split("SELECT", 1)[1].split("FROM sessions s", 1)[0]
        store_cols = sql.split("store_matches AS (", 1)[1]
        store_cols = store_cols.split("SELECT DISTINCT ON (s.id)", 1)[1]
        store_cols = store_cols.split("FROM session_store_entries sse", 1)[0]

        title_names = [
            self._output_name(c) for c in self._split_top_level(title_cols)
        ]
        store_names = [
            self._output_name(c) for c in self._split_top_level(store_cols)
        ]

        assert title_names == store_names
        assert len(store_names) == len(set(store_names))

    def test_store_cte_structural_invariants(self):
        """Guard the store CTE's load-bearing predicates: it joins sessions on
        sdk_session_id, restricts to main transcripts (subpath IS NULL), matches
        via sse_entry_text()'s tsvector, and excludes archived + failed
        sessions."""
        sql = self._captured_search_sql()
        store = sql.split("store_matches AS (", 1)[1].split("all_matches AS (", 1)[0]

        assert "s.sdk_session_id = sse.session_id" in store
        assert "sse.subpath IS NULL" in store
        assert "sse_entry_text(sse.entry)" in store
        assert "to_tsvector('english', sse_entry_text(sse.entry)) @@ plainto_tsquery" in store
        assert "s.status NOT IN ('archived','failed')" in store

    def test_every_cte_excludes_archived_and_failed(self):
        """All four CTEs (title / legacy / SDK / native content) drop
        ``status IN ('archived','failed')``. ``'failed'`` is ProviderAgent's
        verdict on a worker session whose dispatch errored: the transcript stays
        reachable from the session LISTS (visibility is deliberately unchanged),
        but a dead worker must not compete with real conversations for search
        hits. A new turn on such a session revives it to ``'active'`` and it
        becomes searchable again."""
        sql = self._captured_search_sql()

        assert sql.count("s.status NOT IN ('archived','failed')") == 4
        assert "s.status != 'archived'" not in sql

    def test_match_count_window_before_dedupe(self):
        """match_count is a COUNT(*) OVER (PARTITION BY id) computed in the pre-
        dedupe union, so each surviving row carries how many places matched."""
        sql = self._captured_search_sql()
        assert "COUNT(*) OVER (PARTITION BY id) as match_count" in sql
        # The window sits in all_matches (before the DISTINCT ON dedupe).
        all_matches = sql.split("all_matches AS (", 1)[1].split("SELECT DISTINCT ON (id) *", 1)[0]
        assert "match_count" in all_matches

    def test_scoping_predicates_preserved(self):
        """API-user / project scoping must survive the CTE rework: with filters
        supplied, the outer WHERE still appends the scoping predicates and the
        per-install isolation clause is threaded through each CTE unchanged."""
        from unittest.mock import MagicMock

        captured = []
        cursor = MagicMock()
        cursor.fetchall.return_value = []
        cursor.execute.side_effect = lambda sql, params=None: captured.append((sql, params))

        with patch("app.db.sessions.get_db"), \
             patch("app.db.sessions.get_cursor", return_value=cursor):
            from app.db import sessions as db_sessions
            db_sessions.search_sessions(
                "react hooks", project_id="p1", api_user_id="u1"
            )

        sql, params = captured[0]
        assert "AND project_id = %s" in sql
        assert "AND api_user_id = %s" in sql
        # Trailing filter params appended after the CTE params in order.
        assert params[-3:] == ["p1", "u1", 20]


class TestSearchSessionsPaginatedSql:
    """Structural guard for the paginated variant's store CTE + match_count."""

    def _captured_paginated_sql(self) -> list:
        from unittest.mock import MagicMock

        captured = []
        cursor = MagicMock()
        cursor.fetchall.return_value = []
        cursor.fetchone.return_value = {"cnt": 0}
        cursor.execute.side_effect = lambda sql, params=None: captured.append(sql)

        with patch("app.db.sessions.get_db"), \
             patch("app.db.sessions.get_cursor", return_value=cursor):
            from app.db import sessions as db_sessions
            db_sessions.search_sessions_paginated("react hooks")

        assert captured, "search_sessions_paginated should have executed queries"
        # Both count + data queries share the base CTE; assert on the data query.
        return captured

    def test_paginated_has_store_cte_and_match_count(self):
        for sql in self._captured_paginated_sql():
            assert "store_matches AS (" in sql
            assert "session_store_entries sse" in sql
            assert "s.sdk_session_id = sse.session_id" in sql
            assert "COUNT(*) OVER (PARTITION BY id) as match_count" in sql

    def test_paginated_excludes_archived_and_failed_in_every_cte(self):
        """Same exclusion as the non-paginated variant — a failed worker session
        must not surface in either search surface."""
        for sql in self._captured_paginated_sql():
            assert sql.count("s.status NOT IN ('archived','failed')") == 4
            assert "s.status != 'archived'" not in sql


# =============================================================================
# Auth Session Operations Tests
# =============================================================================

class TestAuthSessionOperations:
    """Test auth session operations."""

    def test_create_auth_session(self, mock_db):
        """create_auth_session should create auth token."""
        expires = datetime.now(timezone.utc) + timedelta(hours=24)
        result = db.create_auth_session("token123", expires)

        assert result["token"] == "token123"
        assert result["expires_at"] == expires

    def test_legacy_auth_session_never_grants_access(self, mock_db):
        """Even an unexpired row from the old password flow stays retired."""
        expires = datetime.now(timezone.utc) + timedelta(hours=24)
        db.create_auth_session("token123", expires)

        result = db.get_auth_session("token123")

        assert result is None

    def test_get_auth_session_expired(self, mock_db):
        """get_auth_session should not return expired session."""
        expires = datetime.now(timezone.utc) - timedelta(hours=1)
        db.create_auth_session("token123", expires)

        result = db.get_auth_session("token123")

        assert result is None

    def test_delete_auth_session(self, mock_db):
        """delete_auth_session should delete session."""
        expires = datetime.now(timezone.utc) + timedelta(hours=24)
        db.create_auth_session("token123", expires)
        db.delete_auth_session("token123")

        assert db.get_auth_session("token123") is None


# =============================================================================
# System Settings Operations Tests
# =============================================================================

class TestSystemSettingsOperations:
    """Test system settings operations."""

    def test_set_system_setting(self, mock_db):
        """set_system_setting should store setting."""
        db.set_system_setting("theme", "dark")
        result = db.get_system_setting("theme")

        assert result == "dark"

    def test_get_system_setting_not_found(self, mock_db):
        """get_system_setting should return None for non-existent key."""
        result = db.get_system_setting("nonexistent")
        assert result is None

    def test_delete_system_setting(self, mock_db):
        """delete_system_setting should remove setting."""
        db.set_system_setting("theme", "dark")
        result = db.delete_system_setting("theme")

        assert result is True
        assert db.get_system_setting("theme") is None

    def test_get_all_system_settings(self, mock_db):
        """get_all_system_settings should return all settings."""
        db.set_system_setting("key1", "value1")
        db.set_system_setting("key2", "value2")

        result = db.get_all_system_settings()

        assert result["key1"] == "value1"
        assert result["key2"] == "value2"


# =============================================================================
# Tag Operations Tests
# =============================================================================

class TestTagOperations:
    """Test tag operations."""

    def test_create_tag(self, mock_db):
        """create_tag should create tag."""
        result = db.create_tag("tag-1", "Bug Fix", "#ff0000")

        assert result["id"] == "tag-1"
        assert result["name"] == "Bug Fix"
        assert result["color"] == "#ff0000"

    def test_get_tag(self, mock_db):
        """get_tag should return tag by ID."""
        db.create_tag("tag-1", "Test")
        result = db.get_tag("tag-1")

        assert result is not None
        assert result["id"] == "tag-1"

    def test_get_all_tags(self, mock_db):
        """get_all_tags should return all tags ordered by name."""
        db.create_tag("tag-b", "B Tag")
        db.create_tag("tag-a", "A Tag")

        result = db.get_all_tags()

        assert len(result) == 2
        assert result[0]["name"] == "A Tag"

    def test_update_tag(self, mock_db):
        """update_tag should update tag fields."""
        db.create_tag("tag-1", "Old Name", "#000000")
        result = db.update_tag("tag-1", name="New Name", color="#ffffff")

        assert result["name"] == "New Name"
        assert result["color"] == "#ffffff"

    def test_delete_tag(self, mock_db):
        """delete_tag should delete tag."""
        db.create_tag("tag-1", "Test")
        result = db.delete_tag("tag-1")

        assert result is True
        assert db.get_tag("tag-1") is None


# =============================================================================
# Session Tag Operations Tests
# =============================================================================

class TestSessionTagOperations:
    """Test session tag operations."""

    def test_add_session_tag(self, mock_db, setup_profile):
        """add_session_tag should add tag to session."""
        db.create_session("session-1", setup_profile)
        db.create_tag("tag-1", "Test")

        result = db.add_session_tag("session-1", "tag-1")

        assert result is True

    def test_get_session_tags(self, mock_db, setup_profile):
        """get_session_tags should return tags for session."""
        db.create_session("session-1", setup_profile)
        db.create_tag("tag-1", "Tag 1")
        db.create_tag("tag-2", "Tag 2")
        db.add_session_tag("session-1", "tag-1")
        db.add_session_tag("session-1", "tag-2")

        result = db.get_session_tags("session-1")

        assert len(result) == 2

    def test_remove_session_tag(self, mock_db, setup_profile):
        """remove_session_tag should remove tag from session."""
        db.create_session("session-1", setup_profile)
        db.create_tag("tag-1", "Test")
        db.add_session_tag("session-1", "tag-1")

        result = db.remove_session_tag("session-1", "tag-1")

        assert result is True
        assert len(db.get_session_tags("session-1")) == 0


# =============================================================================
# Subagent Operations Tests
# =============================================================================

class TestSubagentOperations:
    """Test subagent operations."""

    def test_create_subagent(self, mock_db):
        """create_subagent should create subagent."""
        result = db.create_subagent(
            "subagent-1", "Test Agent", "A test subagent",
            "You are a helpful assistant.",
            tools=["tool1", "tool2"],
            model="claude-3"
        )

        assert result["id"] == "subagent-1"
        assert result["name"] == "Test Agent"
        assert result["tools"] == ["tool1", "tool2"]

    def test_get_subagent(self, mock_db):
        """get_subagent should return subagent by ID."""
        db.create_subagent("subagent-1", "Test", "Desc", "Prompt")
        result = db.get_subagent("subagent-1")

        assert result is not None
        assert result["id"] == "subagent-1"

    def test_get_all_subagents(self, mock_db):
        """get_all_subagents should return all subagents."""
        db.create_subagent("sub-a", "A Agent", "Desc", "Prompt")
        db.create_subagent("sub-b", "B Agent", "Desc", "Prompt")

        result = db.get_all_subagents()

        assert len(result) == 2

    def test_update_subagent(self, mock_db):
        """update_subagent should update subagent fields."""
        db.create_subagent("subagent-1", "Old Name", "Old Desc", "Old Prompt")
        result = db.update_subagent(
            "subagent-1",
            name="New Name",
            description="New Desc",
            tools=["new_tool"]
        )

        assert result["name"] == "New Name"
        assert result["description"] == "New Desc"
        assert result["tools"] == ["new_tool"]

    def test_delete_subagent(self, mock_db):
        """delete_subagent should delete subagent."""
        db.create_subagent("subagent-1", "Test", "Desc", "Prompt")
        result = db.delete_subagent("subagent-1")

        assert result is True
        assert db.get_subagent("subagent-1") is None


# =============================================================================
# Template Operations Tests
# =============================================================================

class TestTemplateOperations:
    """Test template operations."""

    def test_create_template(self, mock_db):
        """create_template should create template."""
        result = db.create_template(
            "template-1", "Code Review",
            "Please review this code...",
            description="Template for code reviews",
            category="development"
        )

        assert result["id"] == "template-1"
        assert result["name"] == "Code Review"
        assert result["category"] == "development"

    def test_get_template(self, mock_db):
        """get_template should return template by ID."""
        db.create_template("template-1", "Test", "Prompt")
        result = db.get_template("template-1")

        assert result is not None
        assert result["id"] == "template-1"

    def test_update_template(self, mock_db):
        """update_template should update template fields."""
        db.create_template("template-1", "Old Name", "Old Prompt")
        result = db.update_template(
            "template-1",
            name="New Name",
            prompt="New Prompt"
        )

        assert result["name"] == "New Name"
        assert result["prompt"] == "New Prompt"

    def test_delete_template(self, mock_db):
        """delete_template should delete template."""
        db.create_template("template-1", "Test", "Prompt")
        result = db.delete_template("template-1")

        assert result is True
        assert db.get_template("template-1") is None


# =============================================================================
# Webhook Operations Tests
# =============================================================================

class TestWebhookOperations:
    """Test webhook operations."""

    def test_create_webhook(self, mock_db):
        """create_webhook should create webhook."""
        result = db.create_webhook(
            "webhook-1",
            "https://example.com/webhook",
            ["session.created", "session.completed"],
            secret="secret123"
        )

        assert result["id"] == "webhook-1"
        assert result["url"] == "https://example.com/webhook"
        assert result["events"] == ["session.created", "session.completed"]

    def test_get_webhook(self, mock_db):
        """get_webhook should return webhook by ID."""
        db.create_webhook("webhook-1", "https://example.com", ["event"])
        result = db.get_webhook("webhook-1")

        assert result is not None
        assert result["id"] == "webhook-1"

    def test_get_all_webhooks(self, mock_db):
        """get_all_webhooks should return all webhooks."""
        db.create_webhook("w-1", "https://a.com", ["event"])
        db.create_webhook("w-2", "https://b.com", ["event"])

        result = db.get_all_webhooks()

        assert len(result) == 2

    def test_delete_webhook(self, mock_db):
        """delete_webhook should delete webhook."""
        db.create_webhook("webhook-1", "https://example.com", ["event"])
        result = db.delete_webhook("webhook-1")

        assert result is True
        assert db.get_webhook("webhook-1") is None


# =============================================================================
# Clear All Sessions Test
# =============================================================================

class TestClearAllSessions:
    """Test clearing all sessions."""

    def test_clear_all_sessions(self, mock_db, setup_api_user):
        """clear_all_sessions should delete all auth and API key sessions."""
        # Create some auth sessions
        expires = datetime.now(timezone.utc) + timedelta(hours=24)
        db.create_auth_session("auth-token-1", expires)
        db.create_auth_session("auth-token-2", expires)

        # Create API key session directly
        mock_db.execute(
            "INSERT INTO api_key_sessions (token, api_user_id, expires_at) VALUES (?, ?, ?)",
            ("api-token-1", "user-1", expires.isoformat())
        )
        mock_db.commit()

        admin_count, api_count = db.clear_all_sessions()

        assert admin_count == 2
        assert api_count == 1


# =============================================================================
# Usage Logging Tests
# =============================================================================

class TestUsageLogging:
    """Test usage logging operations."""

    def test_log_usage(self, mock_db, setup_profile):
        """log_usage should record usage entry."""
        db.create_session("session-1", setup_profile)
        db.log_usage(
            session_id="session-1",
            profile_id=setup_profile,
            model="claude-3-opus",
            tokens_in=100,
            tokens_out=200,
            cost_usd=0.05,
            duration_ms=1000
        )

        # Verify through get_usage_stats
        stats = db.get_usage_stats()
        assert stats["total_queries"] == 1
        assert stats["total_tokens_in"] == 100
        assert stats["total_tokens_out"] == 200

    def test_get_usage_stats(self, mock_db, setup_profile):
        """get_usage_stats should return aggregated stats."""
        db.create_session("session-1", setup_profile)
        db.log_usage("session-1", setup_profile, "model", 50, 100, 0.01, 500)
        db.log_usage("session-1", setup_profile, "model", 50, 100, 0.02, 500)

        stats = db.get_usage_stats()

        assert stats["total_queries"] == 2
        assert stats["total_tokens_in"] == 100
        assert stats["total_tokens_out"] == 200
        assert stats["total_cost_usd"] == 0.03

    def test_get_last_session_usage(self, mock_db, setup_profile):
        """get_last_session_usage should return the most recent usage entry."""
        db.create_session("session-1", setup_profile)
        # Add two usage entries
        db.log_usage("session-1", setup_profile, "model-1", 50, 100, 0.01, 500)
        db.log_usage("session-1", setup_profile, "model-2", 75, 150, 0.025, 750)

        result = db.get_last_session_usage("session-1")

        assert result is not None
        # Should return the most recent entry (model-2)
        assert result["total_cost_usd"] == 0.025
        assert result["duration_ms"] == 750
        assert result["tokens_in"] == 75
        assert result["tokens_out"] == 150
        assert result["model"] == "model-2"

    def test_get_last_session_usage_not_found(self, mock_db, setup_profile):
        """get_last_session_usage should return None if no usage exists."""
        db.create_session("session-1", setup_profile)

        result = db.get_last_session_usage("session-1")

        assert result is None

    def test_get_last_session_usage_nonexistent_session(self, mock_db):
        """get_last_session_usage should return None for nonexistent session."""
        result = db.get_last_session_usage("nonexistent-session")

        assert result is None

    def test_get_all_session_usage_empty(self, mock_db, setup_profile):
        """get_all_session_usage should return empty list if no usage exists."""
        db.create_session("session-1", setup_profile)

        result = db.get_all_session_usage("session-1")

        assert result == []

    def test_get_all_session_usage_single_turn(self, mock_db, setup_profile):
        """get_all_session_usage should return single entry with cumulative cost."""
        db.create_session("session-1", setup_profile)
        db.log_usage("session-1", setup_profile, "model-1", 50, 100, 0.01, 500)

        result = db.get_all_session_usage("session-1")

        assert len(result) == 1
        assert result[0]["cost_usd"] == 0.01
        assert result[0]["cumulative_cost_usd"] == 0.01
        assert result[0]["duration_ms"] == 500
        assert result[0]["tokens_in"] == 50
        assert result[0]["tokens_out"] == 100
        assert result[0]["model"] == "model-1"
        assert result[0]["turn_index"] == 0

    def test_get_all_session_usage_multiple_turns(self, mock_db, setup_profile):
        """get_all_session_usage should return all entries with cumulative costs."""
        db.create_session("session-1", setup_profile)
        db.log_usage("session-1", setup_profile, "model-1", 50, 100, 0.01, 500)
        db.log_usage("session-1", setup_profile, "model-1", 75, 150, 0.02, 750)
        db.log_usage("session-1", setup_profile, "model-1", 100, 200, 0.03, 1000)

        result = db.get_all_session_usage("session-1")

        assert len(result) == 3
        # First turn
        assert result[0]["cost_usd"] == 0.01
        assert result[0]["cumulative_cost_usd"] == 0.01
        assert result[0]["turn_index"] == 0
        # Second turn
        assert result[1]["cost_usd"] == 0.02
        assert result[1]["cumulative_cost_usd"] == 0.03  # 0.01 + 0.02
        assert result[1]["turn_index"] == 1
        # Third turn
        assert result[2]["cost_usd"] == 0.03
        assert result[2]["cumulative_cost_usd"] == 0.06  # 0.01 + 0.02 + 0.03
        assert result[2]["turn_index"] == 2

    def test_get_all_session_usage_nonexistent_session(self, mock_db):
        """get_all_session_usage should return empty list for nonexistent session."""
        result = db.get_all_session_usage("nonexistent-session")

        assert result == []


# =============================================================================
# Login Attempts Tests
# =============================================================================

class TestLoginAttempts:
    """Test login attempt tracking."""

    def test_record_login_attempt_success(self, mock_db):
        """record_login_attempt should record successful attempt."""
        db.record_login_attempt("192.168.1.1", "admin", True)

        # Should not count as failed
        count = db.get_failed_attempts_count("192.168.1.1")
        assert count == 0

    def test_record_login_attempt_failure(self, mock_db):
        """record_login_attempt should record failed attempt."""
        db.record_login_attempt("192.168.1.1", "admin", False)

        count = db.get_failed_attempts_count("192.168.1.1")
        assert count == 1

    def test_get_failed_attempts_for_username(self, mock_db):
        """get_failed_attempts_for_username should count failures."""
        db.record_login_attempt("192.168.1.1", "admin", False)
        db.record_login_attempt("192.168.1.2", "admin", False)

        count = db.get_failed_attempts_for_username("admin")
        assert count == 2


# =============================================================================
# Account Lockout Tests
# =============================================================================

class TestAccountLockout:
    """Test account lockout operations."""

    def test_create_lockout(self, mock_db):
        """create_lockout should create lockout entry."""
        # duration_minutes is the 3rd argument (not a datetime)
        db.create_lockout("192.168.1.1", None, 30, "Too many failed attempts")

        lockout = db.is_ip_locked("192.168.1.1")
        assert lockout is not None

    def test_is_ip_locked(self, mock_db):
        """is_ip_locked should detect locked IP."""
        db.create_lockout("192.168.1.1", None, 30, "Too many attempts")

        assert db.is_ip_locked("192.168.1.1") is not None
        assert db.is_ip_locked("192.168.1.2") is None

    def test_is_username_locked(self, mock_db):
        """is_username_locked should detect locked username."""
        db.create_lockout(None, "admin", 30, "Too many attempts")

        assert db.is_username_locked("admin") is not None
        assert db.is_username_locked("other") is None


# =============================================================================
# User Preferences Tests
# =============================================================================

class TestUserPreferences:
    """Test user preference operations."""

    def test_set_user_preference(self, mock_db):
        """set_user_preference should store preference."""
        result = db.set_user_preference("admin", "user-1", "theme", "dark")

        assert result["key"] == "theme"
        assert result["value"] == "dark"

    def test_get_user_preference(self, mock_db):
        """get_user_preference should return preference."""
        db.set_user_preference("admin", "user-1", "theme", "dark")

        result = db.get_user_preference("admin", "user-1", "theme")

        assert result is not None
        assert result["value"] == "dark"

    def test_get_user_preference_not_found(self, mock_db):
        """get_user_preference should return None for non-existent key."""
        result = db.get_user_preference("admin", "user-1", "nonexistent")
        assert result is None

    def test_delete_user_preference(self, mock_db):
        """delete_user_preference should delete preference."""
        db.set_user_preference("admin", "user-1", "theme", "dark")

        result = db.delete_user_preference("admin", "user-1", "theme")

        assert result is True
        assert db.get_user_preference("admin", "user-1", "theme") is None

    def test_get_all_user_preferences(self, mock_db):
        """get_all_user_preferences should return all preferences for user."""
        db.set_user_preference("admin", "user-1", "theme", "dark")
        db.set_user_preference("admin", "user-1", "language", "en")

        result = db.get_all_user_preferences("admin", "user-1")

        assert len(result) == 2

    def test_set_user_preference_stores_valid_json(self, mock_db):
        """set_user_preference should store a JSON-encoded value, not a bare string."""
        db.set_user_preference("admin", "Admin", "timezone", "America/New_York")

        cursor = mock_db.execute(
            "SELECT value FROM user_preferences "
            "WHERE user_type = ? AND user_id = ? AND key = ?",
            ("admin", "Admin", "timezone"),
        )
        raw = cursor.fetchone()[0]
        # Stored form must be valid JSON (a quoted string), so the read path
        # round-trips cleanly without hitting the legacy fallback.
        assert raw == '"America/New_York"'
        import json as _json
        assert _json.loads(raw) == "America/New_York"

    def _insert_legacy_bare_string_pref(self, mock_db, user_type, user_id, key, value):
        """Insert a legacy bare-string preference (pre-JSON-encoding) directly."""
        now = datetime.now(timezone.utc).isoformat()
        mock_db.execute(
            """INSERT INTO user_preferences (user_type, user_id, key, value, updated_at)
               VALUES (?, ?, ?, ?, ?)""",
            (user_type, user_id, key, value, now),
        )
        mock_db.commit()

    def test_get_user_preference_legacy_bare_string(self, mock_db, caplog):
        """A legacy bare-string value should be returned as-is with no warning."""
        self._insert_legacy_bare_string_pref(
            mock_db, "admin", "Admin", "timezone", "America/New_York"
        )

        with caplog.at_level(logging.WARNING):
            result = db.get_user_preference("admin", "Admin", "timezone")

        assert result is not None
        assert result["value"] == "America/New_York"
        assert not any(
            "Invalid JSON in preference" in rec.getMessage() for rec in caplog.records
        )

    def test_get_all_user_preferences_legacy_bare_string(self, mock_db, caplog):
        """get_all should tolerate a legacy bare-string value with no warning."""
        self._insert_legacy_bare_string_pref(
            mock_db, "admin", "Admin", "timezone", "America/New_York"
        )

        with caplog.at_level(logging.WARNING):
            result = db.get_all_user_preferences("admin", "Admin")

        assert len(result) == 1
        assert result[0]["value"] == "America/New_York"
        assert not any(
            "Invalid JSON in preference" in rec.getMessage() for rec in caplog.records
        )


# =============================================================================
# Sync Log Tests
# =============================================================================

class TestSyncLog:
    """Test sync log operations."""

    def test_add_sync_log(self, mock_db, setup_profile):
        """add_sync_log should add sync entry."""
        db.create_session("session-1", setup_profile)
        result = db.add_sync_log(
            "session-1", "message_added", "message", "msg-1",
            data={"content": "Hello"}
        )

        assert result["event_type"] == "message_added"
        assert result["entity_type"] == "message"

    def test_get_sync_logs(self, mock_db, setup_profile):
        """get_sync_logs should return logs since given ID."""
        db.create_session("session-1", setup_profile)
        db.add_sync_log("session-1", "event1", "type1", "id1")
        log2 = db.add_sync_log("session-1", "event2", "type2", "id2")

        result = db.get_sync_logs("session-1", log2["id"] - 1)

        assert len(result) == 1
        assert result[0]["event_type"] == "event2"

    def test_get_latest_sync_id(self, mock_db, setup_profile):
        """get_latest_sync_id should return most recent ID."""
        db.create_session("session-1", setup_profile)
        db.add_sync_log("session-1", "event1", "type1", "id1")
        log2 = db.add_sync_log("session-1", "event2", "type2", "id2")

        result = db.get_latest_sync_id("session-1")

        assert result == log2["id"]


# =============================================================================
# Permission Rules Tests
# =============================================================================

class TestPermissionRules:
    """Test permission rule operations."""

    def test_add_permission_rule(self, mock_db, setup_profile):
        """add_permission_rule should add rule."""
        # Function signature: profile_id, tool_name, tool_pattern, decision
        result = db.add_permission_rule(setup_profile, "bash", "rm *", "deny")

        assert result["tool_name"] == "bash"
        assert result["decision"] == "deny"

    def test_get_permission_rule(self, mock_db, setup_profile):
        """get_permission_rule should return rule by ID."""
        result = db.add_permission_rule(setup_profile, "bash", None, "allow")

        fetched = db.get_permission_rule(result["id"])

        assert fetched is not None
        assert fetched["id"] == result["id"]

    def test_get_permission_rules_for_profile(self, mock_db, setup_profile):
        """get_permission_rules should return rules for profile."""
        db.add_permission_rule(setup_profile, "bash", None, "allow")
        db.add_permission_rule(setup_profile, "read", None, "allow")

        result = db.get_permission_rules(profile_id=setup_profile)

        assert len(result) == 2

    def test_delete_permission_rule(self, mock_db, setup_profile):
        """delete_permission_rule should delete rule."""
        created = db.add_permission_rule(setup_profile, "bash", None, "allow")

        result = db.delete_permission_rule(created["id"])

        assert result is True
        assert db.get_permission_rule(created["id"]) is None


# =============================================================================
# Git Repository Tests
# =============================================================================

class TestGitRepositoryOperations:
    """Test git repository operations."""

    def test_create_git_repository(self, mock_db, setup_project):
        """create_git_repository should create repository."""
        result = db.create_git_repository(
            "repo-1", setup_project,
            remote_url="https://github.com/test/repo",
            default_branch="main"
        )

        assert result["id"] == "repo-1"
        assert result["project_id"] == setup_project
        assert result["default_branch"] == "main"

    def test_get_git_repository(self, mock_db, setup_project):
        """get_git_repository should return repository by ID."""
        db.create_git_repository("repo-1", setup_project)

        result = db.get_git_repository("repo-1")

        assert result is not None
        assert result["id"] == "repo-1"

    def test_get_git_repository_by_project(self, mock_db, setup_project):
        """get_git_repository_by_project should return repository for project."""
        db.create_git_repository("repo-1", setup_project)

        result = db.get_git_repository_by_project(setup_project)

        assert result is not None
        assert result["id"] == "repo-1"

    def test_delete_git_repository(self, mock_db, setup_project):
        """delete_git_repository should delete repository."""
        db.create_git_repository("repo-1", setup_project)

        result = db.delete_git_repository("repo-1")

        assert result is True
        assert db.get_git_repository("repo-1") is None


# =============================================================================
# Worktree Tests
# =============================================================================

class TestWorktreeOperations:
    """Test worktree operations."""

    @pytest.fixture
    def setup_repository(self, mock_db, setup_project):
        """Create a test repository."""
        repo = db.create_git_repository("repo-1", setup_project)
        return repo["id"]

    def test_create_worktree(self, mock_db, setup_repository):
        """create_worktree should create worktree."""
        result = db.create_worktree(
            "wt-1", setup_repository, "feature-branch",
            "/path/to/worktree", base_branch="main"
        )

        assert result["id"] == "wt-1"
        assert result["branch_name"] == "feature-branch"
        assert result["status"] == "active"

    def test_get_worktree(self, mock_db, setup_repository):
        """get_worktree should return worktree by ID."""
        db.create_worktree("wt-1", setup_repository, "branch", "/path")

        result = db.get_worktree("wt-1")

        assert result is not None
        assert result["id"] == "wt-1"

    def test_get_worktrees_for_repository(self, mock_db, setup_repository):
        """get_worktrees_for_repository should return all worktrees."""
        db.create_worktree("wt-1", setup_repository, "branch1", "/path1")
        db.create_worktree("wt-2", setup_repository, "branch2", "/path2")

        result = db.get_worktrees_for_repository(setup_repository)

        assert len(result) == 2

    def test_update_worktree(self, mock_db, setup_repository):
        """update_worktree should update worktree fields."""
        db.create_worktree("wt-1", setup_repository, "branch", "/path")

        result = db.update_worktree("wt-1", status="deleted")

        assert result["status"] == "deleted"

    def test_delete_worktree(self, mock_db, setup_repository):
        """delete_worktree should delete worktree."""
        db.create_worktree("wt-1", setup_repository, "branch", "/path")

        result = db.delete_worktree("wt-1")

        assert result is True
        assert db.get_worktree("wt-1") is None


# =============================================================================
# Audit Log Tests
# =============================================================================

class TestAuditLog:
    """Test audit log operations."""

    def test_create_audit_log(self, mock_db):
        """create_audit_log should create log entry."""
        # Signature: event_id, event_type, user_id=None, user_type="admin", ...
        result = db.create_audit_log(
            "log-1", "login",
            user_id="user-1",
            user_type="admin",
            ip_address="192.168.1.1",
            user_agent="Mozilla/5.0",
            details={"success": True}
        )

        assert result["id"] == "log-1"
        assert result["event_type"] == "login"

    def test_get_audit_logs(self, mock_db):
        """get_audit_logs should return logs."""
        db.create_audit_log("log-1", "login", user_id="user-1")
        db.create_audit_log("log-2", "logout", user_id="user-1")

        result = db.get_audit_logs(limit=10)

        assert len(result) == 2

    def test_get_audit_logs_by_user(self, mock_db):
        """get_audit_logs should filter by user."""
        db.create_audit_log("log-1", "login", user_id="user-1")
        db.create_audit_log("log-2", "login", user_id="user-2")

        result = db.get_audit_logs(user_id="user-1")

        assert len(result) == 1

    def test_get_audit_log_count(self, mock_db):
        """get_audit_log_count should return count."""
        db.create_audit_log("log-1", "login", user_id="user-1")
        db.create_audit_log("log-2", "logout", user_id="user-1")

        count = db.get_audit_log_count()

        assert count == 2


# =============================================================================
# Credential Policies Tests
# =============================================================================

class TestCredentialPolicies:
    """Test credential policy operations."""

    def test_get_all_credential_policies(self, mock_db):
        """get_all_credential_policies should return all policies."""
        # Initialize default policies first
        mock_db.execute("""
            INSERT OR IGNORE INTO credential_policies (id, policy, description)
            VALUES ('test_key', 'optional', 'Test API key')
        """)
        mock_db.commit()

        result = db.get_all_credential_policies()

        assert len(result) >= 1

    def test_get_credential_policy(self, mock_db):
        """get_credential_policy should return policy by ID."""
        mock_db.execute("""
            INSERT INTO credential_policies (id, policy, description)
            VALUES ('test_key', 'optional', 'Test')
        """)
        mock_db.commit()

        result = db.get_credential_policy("test_key")

        assert result is not None
        assert result["policy"] == "optional"

    def test_update_credential_policy(self, mock_db):
        """update_credential_policy should update policy."""
        mock_db.execute("""
            INSERT INTO credential_policies (id, policy, description)
            VALUES ('test_key', 'optional', 'Test')
        """)
        mock_db.commit()

        result = db.update_credential_policy("test_key", "admin_provided", "Updated")

        assert result["policy"] == "admin_provided"


# =============================================================================
# User Credential Operations Tests
# =============================================================================

class TestUserCredentials:
    """Test user credential operations."""

    def test_set_user_credential(self, mock_db, setup_api_user):
        """set_user_credential should store encrypted credential."""
        result = db.set_user_credential("user-1", "openai_api_key", "enc:encrypted_value")

        assert result["credential_type"] == "openai_api_key"
        assert result["encrypted_value"] == "enc:encrypted_value"

    def test_get_user_credential(self, mock_db, setup_api_user):
        """get_user_credential should return credential."""
        db.set_user_credential("user-1", "openai_api_key", "enc:value")

        result = db.get_user_credential("user-1", "openai_api_key")

        assert result is not None
        assert result["encrypted_value"] == "enc:value"

    def test_get_all_user_credentials(self, mock_db, setup_api_user):
        """get_all_user_credentials should return all credentials for user."""
        db.set_user_credential("user-1", "openai_api_key", "enc:val1")
        db.set_user_credential("user-1", "github_pat", "enc:val2")

        result = db.get_all_user_credentials("user-1")

        assert len(result) == 2

    def test_delete_user_credential(self, mock_db, setup_api_user):
        """delete_user_credential should delete credential."""
        db.set_user_credential("user-1", "openai_api_key", "enc:value")

        result = db.delete_user_credential("user-1", "openai_api_key")

        assert result is True
        assert db.get_user_credential("user-1", "openai_api_key") is None

    def test_user_has_credential(self, mock_db, setup_api_user):
        """user_has_credential should check credential existence."""

        assert db.user_has_credential("user-1", "openai_api_key") is False

        db.set_user_credential("user-1", "openai_api_key", "enc:value")

        assert db.user_has_credential("user-1", "openai_api_key") is True


# =============================================================================
# Webhook Update Tests
# =============================================================================

class TestWebhookUpdates:
    """Test webhook update operations."""

    def test_update_webhook(self, mock_db):
        """update_webhook should update webhook fields."""
        db.create_webhook("wh-1", "https://old.com", ["event"])

        result = db.update_webhook("wh-1", url="https://new.com", is_active=False)

        assert result["url"] == "https://new.com"
        assert not result["is_active"]

    def test_get_active_webhooks(self, mock_db):
        """get_active_webhooks should return only active webhooks."""
        db.create_webhook("wh-1", "https://a.com", ["event"])
        db.create_webhook("wh-2", "https://b.com", ["event"])
        db.update_webhook("wh-2", is_active=False)

        result = db.get_active_webhooks()

        assert len(result) == 1
        assert result[0]["id"] == "wh-1"

    def test_get_webhooks_for_event(self, mock_db):
        """get_webhooks_for_event should return webhooks for specific event."""
        db.create_webhook("wh-1", "https://a.com", ["session.created", "session.updated"])
        db.create_webhook("wh-2", "https://b.com", ["session.deleted"])

        result = db.get_webhooks_for_event("session.created")

        assert len(result) == 1
        assert result[0]["id"] == "wh-1"


# =============================================================================
# Template Categories Tests
# =============================================================================

class TestTemplateCategories:
    """Test template category operations."""

    def test_get_template_categories(self, mock_db, setup_profile):
        """get_template_categories should return distinct categories."""
        db.create_template("t-1", "Template 1", "T1", "prompt", category="coding")
        db.create_template("t-2", "Template 2", "T2", "prompt", category="writing")
        db.create_template("t-3", "Template 3", "T3", "prompt", category="coding")

        result = db.get_template_categories()

        assert "coding" in result
        assert "writing" in result


# =============================================================================
# Subagent Builtin Tests
# =============================================================================

class TestSubagentBuiltin:
    """Test subagent builtin flag operations."""

    def test_set_subagent_builtin(self, mock_db):
        """set_subagent_builtin should update is_builtin flag."""
        db.create_subagent("sub-1", "Test", "Description", "Prompt")

        result = db.set_subagent_builtin("sub-1", True)

        assert result is True
        agent = db.get_subagent("sub-1")
        assert agent["is_builtin"]


# =============================================================================
# Additional Session Tests
# =============================================================================

class TestSessionWorktreeRelations:
    """Test session-worktree relationship operations."""

    @pytest.fixture
    def setup_worktree(self, mock_db, setup_project):
        """Create a worktree for tests."""
        repo = db.create_git_repository("repo-1", setup_project)
        wt = db.create_worktree("wt-1", repo["id"], "main", "/path")
        return wt["id"]

    def test_get_worktree_by_session(self, mock_db, setup_profile, setup_worktree):
        """get_worktree_by_session should return worktree for session."""
        # Create session with worktree
        db.create_session("session-1", setup_profile, worktree_id=setup_worktree)

        result = db.get_worktree_by_session("session-1")

        assert result is not None
        assert result["id"] == setup_worktree

    def test_get_sessions_for_worktree(self, mock_db, setup_profile, setup_worktree):
        """get_sessions_for_worktree should return sessions for worktree."""
        db.create_session("session-1", setup_profile, worktree_id=setup_worktree)
        db.create_session("session-2", setup_profile, worktree_id=setup_worktree)

        result = db.get_sessions_for_worktree(setup_worktree)

        assert len(result) == 2


# =============================================================================
# Session by Tag Tests
# =============================================================================

class TestSessionsByTag:
    """Test getting sessions by tag."""

    def test_get_sessions_by_tag(self, mock_db, setup_profile):
        """get_sessions_by_tag should return sessions with specific tag."""
        db.create_session("session-1", setup_profile)
        db.create_session("session-2", setup_profile)
        db.create_tag("tag-1", "Important", "#ff0000")
        db.add_session_tag("session-1", "tag-1")

        result = db.get_sessions_by_tag("tag-1")

        assert len(result) == 1
        assert result[0]["id"] == "session-1"


# =============================================================================
# Database Maintenance Tests
# =============================================================================

@pytest.mark.skip(
    reason="run_database_maintenance was migrated to Postgres semantics "
    "(ANALYZE + VACUUM ANALYZE outside a txn, notifications retention). These "
    "tests assert the old SQLite contract (wal_checkpoint/optimize/PRAGMA "
    "checkpointed_pages) and VACUUM ANALYZE isn't valid sqlite. No "
    "real-Postgres integration suite exists yet — verify maintenance manually "
    "against a Postgres instance."
)
class TestRunDatabaseMaintenance:
    """Test run_database_maintenance cleanup and results."""

    @pytest.fixture
    def setup_log_tables(self, mock_db, setup_profile):
        """Add missing log tables and seed old + recent rows for maintenance tests."""
        cursor = mock_db.cursor()

        # Create a session for FK references
        db.create_session("s1", setup_profile)

        # connector_event_log requires connectors table for FK
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS connectors (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                connector_type TEXT NOT NULL,
                is_active BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS connector_event_log (
                id TEXT PRIMARY KEY,
                connector_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                direction TEXT NOT NULL DEFAULT 'outbound',
                payload JSON,
                status TEXT DEFAULT 'pending',
                error TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (connector_id) REFERENCES connectors(id) ON DELETE CASCADE
            )
        """)
        # Insert a connector for FK reference
        cursor.execute(
            "INSERT INTO connectors (id, name, connector_type) VALUES (?, ?, ?)",
            ("conn-1", "Test Connector", "slack"),
        )
        mock_db.commit()

        now = datetime.now(timezone.utc)
        old_ts = (now - timedelta(days=10)).isoformat()
        recent_ts = (now - timedelta(days=1)).isoformat()

        # usage_log: old + recent
        cursor.execute(
            "INSERT INTO usage_log (session_id, model, tokens_in, tokens_out, cost_usd, duration_ms, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("s1", "claude-3", 100, 200, 0.01, 500, old_ts),
        )
        cursor.execute(
            "INSERT INTO usage_log (session_id, model, tokens_in, tokens_out, cost_usd, duration_ms, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("s1", "claude-3", 150, 250, 0.02, 600, recent_ts),
        )

        # audit_log: old + recent
        cursor.execute(
            "INSERT INTO audit_log (id, event_type, created_at) VALUES (?, ?, ?)",
            ("audit-old", "login", old_ts),
        )
        cursor.execute(
            "INSERT INTO audit_log (id, event_type, created_at) VALUES (?, ?, ?)",
            ("audit-new", "login", recent_ts),
        )

        # sync_log: old + recent (needs a session FK)
        cursor.execute(
            "INSERT INTO sync_log (session_id, event_type, entity_type, created_at) VALUES (?, ?, ?, ?)",
            ("s1", "create", "message", old_ts),
        )
        cursor.execute(
            "INSERT INTO sync_log (session_id, event_type, entity_type, created_at) VALUES (?, ?, ?, ?)",
            ("s1", "create", "message", recent_ts),
        )

        # connector_event_log: old + recent
        cursor.execute(
            "INSERT INTO connector_event_log (id, connector_id, event_type, created_at) VALUES (?, ?, ?, ?)",
            ("evt-old", "conn-1", "message", old_ts),
        )
        cursor.execute(
            "INSERT INTO connector_event_log (id, connector_id, event_type, created_at) VALUES (?, ?, ?, ?)",
            ("evt-new", "conn-1", "message", recent_ts),
        )

        mock_db.commit()
        return mock_db

    def test_maintenance_cleans_old_rows_keeps_recent(self, setup_log_tables):
        """run_database_maintenance should delete rows older than 7 days and keep recent ones."""
        conn = setup_log_tables

        # Verify precondition: 2 rows in each table before maintenance
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM usage_log")
        assert cursor.fetchone()[0] == 2
        cursor.execute("SELECT COUNT(*) FROM audit_log")
        assert cursor.fetchone()[0] == 2

        results = db.run_database_maintenance()

        # Note: after maintenance, the test connection may be closed by VACUUM's
        # conn.close() since mock_get_connection returns the same connection.
        # We verify behavior via the results dict instead.

        # Should return a dict with keys for each step
        assert isinstance(results, dict)
        assert "wal_checkpoint" in results
        assert "optimize" in results
        assert "vacuum" in results

        # usage_log: 1 old deleted, 1 recent kept
        assert results["cleaned_usage_log"] == 1

        # audit_log: 1 old deleted, 1 recent kept
        assert results["cleaned_audit_log"] == 1

        # sync_log: 1 old deleted, 1 recent kept
        assert results["cleaned_sync_log"] == 1

        # connector_event_log: 1 old deleted, 1 recent kept
        assert results["cleaned_connector_event_log"] == 1

    def test_maintenance_returns_wal_checkpoint_info(self, setup_log_tables):
        """run_database_maintenance should return WAL checkpoint details."""
        results = db.run_database_maintenance()

        wal = results["wal_checkpoint"]
        # In-memory DB won't use WAL mode, but the PRAGMA still returns a row
        assert isinstance(wal, dict)
        assert "busy" in wal
        assert "log_pages" in wal
        assert "checkpointed_pages" in wal

    def test_maintenance_with_no_old_rows(self, mock_db):
        """run_database_maintenance should report 0 cleaned when no old rows exist."""
        # Add tables that are missing from test schema
        cursor = mock_db.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS connectors (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                connector_type TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS connector_event_log (
                id TEXT PRIMARY KEY,
                connector_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (connector_id) REFERENCES connectors(id) ON DELETE CASCADE
            )
        """)
        mock_db.commit()

        results = db.run_database_maintenance()

        assert results["cleaned_usage_log"] == 0
        assert results["cleaned_audit_log"] == 0
        assert results["cleaned_sync_log"] == 0
        assert results["cleaned_connector_event_log"] == 0
        assert results["optimize"] == "ok"
        assert results["vacuum"] == "ok"
