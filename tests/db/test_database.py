"""
Comprehensive unit tests for app/db/database.py.

Tests cover:
- Database connection and context management
- Admin operations (CRUD, 2FA)
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

import json
import pytest
import sqlite3
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock
from contextlib import contextmanager

from app.db import database as db


# =============================================================================
# Fixtures
# =============================================================================

def _create_test_schema(cursor: sqlite3.Cursor):
    """Create database schema in correct order for testing.

    This creates all tables in an order that avoids the worktrees migration
    dependency issue in the main _create_schema function. The schema matches
    the actual database.py schema.
    """
    # System settings
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS system_settings (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Admin user
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS admin (
            id INTEGER PRIMARY KEY DEFAULT 1,
            username TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            totp_secret TEXT,
            totp_enabled BOOLEAN DEFAULT FALSE,
            totp_verified_at TIMESTAMP,
            recovery_codes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            CHECK (id = 1)
        )
    """)

    # Agent profiles
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS profiles (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT,
            is_builtin BOOLEAN DEFAULT FALSE,
            config JSON NOT NULL DEFAULT '{}',
            mcp_tools JSON DEFAULT '[]',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Projects (workspaces)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT,
            path TEXT NOT NULL,
            settings JSON DEFAULT '{}',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Git repositories (must be created before worktrees)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS git_repositories (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL UNIQUE,
            remote_url TEXT,
            default_branch TEXT DEFAULT 'main',
            github_repo_name TEXT,
            last_synced_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
        )
    """)

    # Worktrees (must be created before sessions reference it)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS worktrees (
            id TEXT PRIMARY KEY,
            repository_id TEXT NOT NULL,
            session_id TEXT,
            branch_name TEXT NOT NULL,
            worktree_path TEXT NOT NULL,
            base_branch TEXT,
            status TEXT DEFAULT 'active',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (repository_id) REFERENCES git_repositories(id) ON DELETE CASCADE
        )
    """)

    # API users (must be created before sessions reference it)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS api_users (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            username TEXT UNIQUE,
            password_hash TEXT,
            api_key_hash TEXT NOT NULL,
            project_id TEXT,
            profile_id TEXT,
            is_active BOOLEAN DEFAULT TRUE,
            web_login_allowed BOOLEAN DEFAULT TRUE,
            description TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_used_at TIMESTAMP,
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE SET NULL,
            FOREIGN KEY (profile_id) REFERENCES profiles(id) ON DELETE SET NULL
        )
    """)

    # Sessions (conversations)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            project_id TEXT,
            profile_id TEXT NOT NULL,
            api_user_id TEXT,
            sdk_session_id TEXT,
            worktree_id TEXT,
            parent_session_id TEXT,
            fork_point_message_index INTEGER,
            title TEXT,
            status TEXT DEFAULT 'active',
            is_favorite BOOLEAN DEFAULT FALSE,
            total_cost_usd REAL DEFAULT 0,
            total_tokens_in INTEGER DEFAULT 0,
            total_tokens_out INTEGER DEFAULT 0,
            turn_count INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE SET NULL,
            FOREIGN KEY (profile_id) REFERENCES profiles(id),
            FOREIGN KEY (api_user_id) REFERENCES api_users(id) ON DELETE SET NULL,
            FOREIGN KEY (worktree_id) REFERENCES worktrees(id) ON DELETE SET NULL
        )
    """)

    # Session messages
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS session_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            tool_name TEXT,
            tool_input JSON,
            metadata JSON,
            version INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        )
    """)

    # Sync log
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sync_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            entity_type TEXT NOT NULL,
            entity_id TEXT,
            data JSON,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        )
    """)

    # Auth sessions
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS auth_sessions (
            token TEXT PRIMARY KEY,
            expires_at TIMESTAMP NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # API key sessions
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS api_key_sessions (
            token TEXT PRIMARY KEY,
            api_user_id TEXT NOT NULL,
            expires_at TIMESTAMP NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (api_user_id) REFERENCES api_users(id) ON DELETE CASCADE
        )
    """)

    # Usage log
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS usage_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            profile_id TEXT,
            model TEXT,
            tokens_in INTEGER,
            tokens_out INTEGER,
            cost_usd REAL,
            duration_ms INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # API user profiles junction table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS api_user_profiles (
            api_user_id TEXT NOT NULL,
            profile_id TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (api_user_id, profile_id),
            FOREIGN KEY (api_user_id) REFERENCES api_users(id) ON DELETE CASCADE,
            FOREIGN KEY (profile_id) REFERENCES profiles(id) ON DELETE CASCADE
        )
    """)

    # Login attempts
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS login_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ip_address TEXT NOT NULL,
            username TEXT,
            success BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Account lockouts
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS account_lockouts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ip_address TEXT,
            username TEXT,
            locked_until TIMESTAMP NOT NULL,
            reason TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Checkpoints
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS checkpoints (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            sdk_session_id TEXT NOT NULL,
            message_uuid TEXT NOT NULL,
            message_preview TEXT,
            message_index INTEGER DEFAULT 0,
            git_ref TEXT,
            git_available BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        )
    """)

    # User preferences
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_preferences (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_type TEXT NOT NULL,
            user_id TEXT NOT NULL,
            key TEXT NOT NULL,
            value JSON,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_type, user_id, key)
        )
    """)

    # Subagents
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS subagents (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT NOT NULL,
            prompt TEXT NOT NULL,
            tools JSON,
            model TEXT,
            is_builtin BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Permission rules
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS permission_rules (
            id TEXT PRIMARY KEY,
            profile_id TEXT,
            tool_name TEXT NOT NULL,
            tool_pattern TEXT,
            decision TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (profile_id) REFERENCES profiles(id) ON DELETE CASCADE
        )
    """)

    # Tags
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tags (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            color TEXT NOT NULL DEFAULT '#6366f1',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Session tags junction table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS session_tags (
            session_id TEXT NOT NULL,
            tag_id TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (session_id, tag_id),
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE,
            FOREIGN KEY (tag_id) REFERENCES tags(id) ON DELETE CASCADE
        )
    """)

    # Templates
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS templates (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT,
            prompt TEXT NOT NULL,
            profile_id TEXT,
            icon TEXT,
            category TEXT,
            is_builtin BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (profile_id) REFERENCES profiles(id) ON DELETE SET NULL
        )
    """)

    # Webhooks
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS webhooks (
            id TEXT PRIMARY KEY,
            url TEXT NOT NULL,
            secret TEXT,
            events JSON NOT NULL DEFAULT '[]',
            is_active BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_triggered_at TIMESTAMP,
            failure_count INTEGER DEFAULT 0
        )
    """)

    # Audit log
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS audit_log (
            id TEXT PRIMARY KEY,
            user_id TEXT,
            user_type TEXT DEFAULT 'admin',
            event_type TEXT NOT NULL,
            ip_address TEXT,
            user_agent TEXT,
            details JSON,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Knowledge documents
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS knowledge_documents (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            filename TEXT NOT NULL,
            content TEXT NOT NULL,
            content_type TEXT DEFAULT 'text/plain',
            file_size INTEGER DEFAULT 0,
            chunk_count INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
        )
    """)

    # Knowledge chunks
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS knowledge_chunks (
            id TEXT PRIMARY KEY,
            document_id TEXT NOT NULL,
            chunk_index INTEGER NOT NULL,
            content TEXT NOT NULL,
            metadata JSON DEFAULT '{}',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (document_id) REFERENCES knowledge_documents(id) ON DELETE CASCADE
        )
    """)

    # Pending 2FA sessions
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS pending_2fa_sessions (
            token TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            expires_at TIMESTAMP NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Rate limits
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS rate_limits (
            id TEXT PRIMARY KEY,
            user_id TEXT,
            api_key_id TEXT,
            requests_per_minute INTEGER DEFAULT 20,
            requests_per_hour INTEGER DEFAULT 200,
            requests_per_day INTEGER DEFAULT 1000,
            concurrent_requests INTEGER DEFAULT 3,
            priority INTEGER DEFAULT 0,
            is_unlimited BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (api_key_id) REFERENCES api_users(id) ON DELETE CASCADE
        )
    """)

    # Request log
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS request_log (
            id TEXT PRIMARY KEY,
            user_id TEXT,
            api_key_id TEXT,
            endpoint TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            duration_ms INTEGER,
            status TEXT DEFAULT 'success'
        )
    """)

    # Agent runs
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS agent_runs (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            prompt TEXT NOT NULL,
            status TEXT DEFAULT 'queued',
            progress REAL DEFAULT 0,
            profile_id TEXT,
            project_id TEXT,
            worktree_id TEXT,
            branch TEXT,
            base_branch TEXT,
            pr_url TEXT,
            auto_branch BOOLEAN DEFAULT TRUE,
            auto_pr BOOLEAN DEFAULT FALSE,
            auto_merge BOOLEAN DEFAULT FALSE,
            auto_review BOOLEAN DEFAULT FALSE,
            max_duration_minutes INTEGER DEFAULT 30,
            sdk_session_id TEXT,
            started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            completed_at TIMESTAMP,
            error TEXT,
            result_summary TEXT,
            FOREIGN KEY (profile_id) REFERENCES profiles(id) ON DELETE SET NULL,
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE SET NULL,
            FOREIGN KEY (worktree_id) REFERENCES worktrees(id) ON DELETE SET NULL
        )
    """)

    # Agent tasks
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS agent_tasks (
            id TEXT PRIMARY KEY,
            agent_run_id TEXT NOT NULL,
            parent_task_id TEXT,
            name TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            order_index INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (agent_run_id) REFERENCES agent_runs(id) ON DELETE CASCADE,
            FOREIGN KEY (parent_task_id) REFERENCES agent_tasks(id) ON DELETE CASCADE
        )
    """)

    # Agent logs
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS agent_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_run_id TEXT NOT NULL,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            level TEXT DEFAULT 'info',
            message TEXT NOT NULL,
            metadata JSON,
            FOREIGN KEY (agent_run_id) REFERENCES agent_runs(id) ON DELETE CASCADE
        )
    """)

    # Credential policies
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS credential_policies (
            id TEXT PRIMARY KEY,
            policy TEXT NOT NULL DEFAULT 'admin_provided',
            description TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # API user credentials
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS api_user_credentials (
            id TEXT PRIMARY KEY,
            api_user_id TEXT NOT NULL,
            credential_type TEXT NOT NULL,
            encrypted_value TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (api_user_id) REFERENCES api_users(id) ON DELETE CASCADE,
            UNIQUE(api_user_id, credential_type)
        )
    """)

    # API user GitHub config
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS api_user_github_config (
            id TEXT PRIMARY KEY,
            api_user_id TEXT NOT NULL UNIQUE,
            github_username TEXT,
            github_avatar_url TEXT,
            default_repo TEXT,
            default_branch TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (api_user_id) REFERENCES api_users(id) ON DELETE CASCADE
        )
    """)

    # User credential policies
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_credential_policies (
            id TEXT PRIMARY KEY,
            api_user_id TEXT NOT NULL,
            credential_type TEXT NOT NULL,
            policy TEXT NOT NULL DEFAULT 'optional',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (api_user_id) REFERENCES api_users(id) ON DELETE CASCADE,
            UNIQUE(api_user_id, credential_type)
        )
    """)

    # Schema version
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER PRIMARY KEY
        )
    """)
    cursor.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (25)")


@pytest.fixture
def test_db():
    """Create an in-memory SQLite database for testing with full schema."""
    # Create in-memory database
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")

    # Create schema
    cursor = conn.cursor()
    _create_test_schema(cursor)
    conn.commit()

    yield conn

    conn.close()


@pytest.fixture
def mock_db(test_db):
    """Patch database module to use test database."""
    @contextmanager
    def mock_get_db():
        try:
            yield test_db
            test_db.commit()
        except Exception:
            test_db.rollback()
            raise

    def mock_get_connection():
        return test_db

    # Keep the effective_data_dir patch active for the duration of the test
    with patch.object(db, "get_connection", mock_get_connection):
        with patch.object(db, "get_db", mock_get_db):
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

    def test_update_admin_totp_enable(self, mock_db):
        """update_admin_totp should enable 2FA."""
        db.create_admin("admin", "hash")
        result = db.update_admin_totp("secret123", True, recovery_codes="codes")

        assert result is True
        admin = db.get_admin()
        assert admin["totp_secret"] == "secret123"
        assert admin["totp_enabled"] == 1
        assert admin["recovery_codes"] == "codes"

    def test_update_admin_totp_disable(self, mock_db):
        """update_admin_totp should disable 2FA."""
        db.create_admin("admin", "hash")
        db.update_admin_totp("secret123", True, recovery_codes="codes")
        db.update_admin_totp(None, False)

        admin = db.get_admin()
        assert admin["totp_secret"] is None
        assert admin["totp_enabled"] == 0

    def test_get_admin_2fa_status_disabled(self, mock_db):
        """get_admin_2fa_status should return disabled status."""
        db.create_admin("admin", "hash")
        status = db.get_admin_2fa_status()

        assert status["enabled"] is False

    def test_get_admin_2fa_status_enabled(self, mock_db):
        """get_admin_2fa_status should return enabled status."""
        db.create_admin("admin", "hash")
        db.update_admin_totp("secret", True, recovery_codes="codes")
        status = db.get_admin_2fa_status()

        assert status["enabled"] is True
        assert status["has_recovery_codes"] is True

    def test_update_admin_recovery_codes(self, mock_db):
        """update_admin_recovery_codes should update codes."""
        db.create_admin("admin", "hash")
        result = db.update_admin_recovery_codes("new_codes")

        assert result is True


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

    def test_get_sessions_with_filters(self, mock_db, setup_profile, setup_project):
        """get_sessions should filter by various criteria."""
        db.create_session("session-1", setup_profile, project_id=setup_project)
        db.create_session("session-2", setup_profile)

        result = db.get_sessions(project_id=setup_project)

        assert len(result) == 1
        assert result[0]["id"] == "session-1"

    def test_get_sessions_favorites_only(self, mock_db, setup_profile):
        """get_sessions should filter favorites."""
        db.create_session("session-1", setup_profile)
        db.create_session("session-2", setup_profile)
        db.set_session_favorite("session-1", True)

        result = db.get_sessions(favorites_only=True)

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

    def test_delete_session(self, mock_db, setup_profile):
        """delete_session should delete session."""
        db.create_session("session-1", setup_profile)
        result = db.delete_session("session-1")

        assert result is True
        assert db.get_session("session-1") is None

    def test_toggle_session_favorite(self, mock_db, setup_profile):
        """toggle_session_favorite should toggle is_favorite."""
        db.create_session("session-1", setup_profile)

        result = db.toggle_session_favorite("session-1")
        assert result["is_favorite"] == 1

        result = db.toggle_session_favorite("session-1")
        assert result["is_favorite"] == 0

    def test_set_session_favorite(self, mock_db, setup_profile):
        """set_session_favorite should set is_favorite to specific value."""
        db.create_session("session-1", setup_profile)

        result = db.set_session_favorite("session-1", True)
        assert result["is_favorite"] == 1

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

    def test_search_sessions_by_title(self, mock_db, setup_profile):
        """search_sessions should find sessions by title."""
        db.create_session("session-1", setup_profile, title="Python debugging help")
        db.create_session("session-2", setup_profile, title="JavaScript tutorial")

        result = db.search_sessions("Python")

        assert len(result) == 1
        assert result[0]["id"] == "session-1"

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


# =============================================================================
# Auth Session Operations Tests
# =============================================================================

class TestAuthSessionOperations:
    """Test auth session operations."""

    def test_create_auth_session(self, mock_db):
        """create_auth_session should create auth token."""
        expires = datetime.utcnow() + timedelta(hours=24)
        result = db.create_auth_session("token123", expires)

        assert result["token"] == "token123"
        assert result["expires_at"] == expires

    def test_get_auth_session_valid(self, mock_db):
        """get_auth_session should return valid session."""
        expires = datetime.utcnow() + timedelta(hours=24)
        db.create_auth_session("token123", expires)

        result = db.get_auth_session("token123")

        assert result is not None
        assert result["token"] == "token123"

    def test_get_auth_session_expired(self, mock_db):
        """get_auth_session should not return expired session."""
        expires = datetime.utcnow() - timedelta(hours=1)
        db.create_auth_session("token123", expires)

        result = db.get_auth_session("token123")

        assert result is None

    def test_delete_auth_session(self, mock_db):
        """delete_auth_session should delete session."""
        expires = datetime.utcnow() + timedelta(hours=24)
        db.create_auth_session("token123", expires)
        db.delete_auth_session("token123")

        assert db.get_auth_session("token123") is None

    def test_cleanup_expired_sessions(self, mock_db):
        """cleanup_expired_sessions should remove expired sessions."""
        db.create_auth_session("valid", datetime.utcnow() + timedelta(hours=24))
        db.create_auth_session("expired", datetime.utcnow() - timedelta(hours=1))

        db.cleanup_expired_sessions()

        # Check directly in database
        cursor = mock_db.cursor()
        cursor.execute("SELECT COUNT(*) as count FROM auth_sessions")
        assert cursor.fetchone()["count"] == 1


# =============================================================================
# API User Operations Tests
# =============================================================================

class TestAPIUserOperations:
    """Test API user operations."""

    def test_create_api_user(self, mock_db, setup_profile):
        """create_api_user should create API user."""
        result = db.create_api_user(
            "user-1", "Test User", "hash123",
            profile_ids=[setup_profile],
            description="Test API user",
            username="testuser",
            password_hash="pwdhash"
        )

        assert result["id"] == "user-1"
        assert result["name"] == "Test User"
        assert result["profile_ids"] == [setup_profile]

    def test_get_api_user(self, mock_db):
        """get_api_user should return user by ID."""
        db.create_api_user("user-1", "Test", "hash")
        result = db.get_api_user("user-1")

        assert result is not None
        assert result["id"] == "user-1"

    def test_get_api_user_by_key_hash(self, mock_db):
        """get_api_user_by_key_hash should return user by API key hash."""
        db.create_api_user("user-1", "Test", "unique_hash")
        result = db.get_api_user_by_key_hash("unique_hash")

        assert result is not None
        assert result["id"] == "user-1"

    def test_get_all_api_users(self, mock_db):
        """get_all_api_users should return all users."""
        db.create_api_user("user-1", "User 1", "hash1")
        db.create_api_user("user-2", "User 2", "hash2")

        result = db.get_all_api_users()

        assert len(result) == 2

    def test_update_api_user(self, mock_db, setup_profile):
        """update_api_user should update user fields."""
        db.create_api_user("user-1", "Old Name", "hash")
        result = db.update_api_user(
            "user-1",
            name="New Name",
            profile_ids=[setup_profile],
            is_active=False
        )

        assert result["name"] == "New Name"
        assert result["profile_ids"] == [setup_profile]
        assert result["is_active"] == 0

    def test_update_api_user_key(self, mock_db):
        """update_api_user_key should update API key hash."""
        db.create_api_user("user-1", "Test", "old_hash")
        result = db.update_api_user_key("user-1", "new_hash")

        assert result is not None

    def test_update_api_user_last_used(self, mock_db):
        """update_api_user_last_used should update timestamp."""
        db.create_api_user("user-1", "Test", "hash")
        db.update_api_user_last_used("user-1")

        user = db.get_api_user("user-1")
        assert user["last_used_at"] is not None

    def test_delete_api_user(self, mock_db):
        """delete_api_user should delete user."""
        db.create_api_user("user-1", "Test", "hash")
        result = db.delete_api_user("user-1")

        assert result is True
        assert db.get_api_user("user-1") is None

    def test_get_api_user_by_username(self, mock_db):
        """get_api_user_by_username should return user by username."""
        db.create_api_user("user-1", "Test", "hash", username="testuser")
        result = db.get_api_user_by_username("testuser")

        assert result is not None
        assert result["id"] == "user-1"

    def test_is_api_key_claimed(self, mock_db):
        """is_api_key_claimed should detect claimed keys."""
        db.create_api_user("user-1", "Test", "hash")
        assert db.is_api_key_claimed("hash") is False

        db.claim_api_key("hash", "username", "pwdhash")
        assert db.is_api_key_claimed("hash") is True

    def test_claim_api_key(self, mock_db):
        """claim_api_key should set username and password."""
        db.create_api_user("user-1", "Test", "hash")
        result = db.claim_api_key("hash", "claimed_user", "pwdhash")

        assert result is not None
        assert result["username"] == "claimed_user"

    def test_is_username_taken(self, mock_db):
        """is_username_taken should check username availability."""
        assert db.is_username_taken("newuser") is False

        db.create_api_user("user-1", "Test", "hash", username="newuser")
        assert db.is_username_taken("newuser") is True


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
# Agent Run Operations Tests
# =============================================================================

class TestAgentRunOperations:
    """Test agent run operations."""

    def test_create_agent_run(self, mock_db):
        """create_agent_run should create agent run."""
        result = db.create_agent_run(
            "run-1", "Test Run", "Build a feature",
            auto_branch=True,
            auto_pr=True,
            max_duration_minutes=60
        )

        assert result["id"] == "run-1"
        assert result["name"] == "Test Run"
        assert result["status"] == "queued"

    def test_get_agent_run(self, mock_db):
        """get_agent_run should return run by ID."""
        db.create_agent_run("run-1", "Test", "Prompt")
        result = db.get_agent_run("run-1")

        assert result is not None
        assert result["id"] == "run-1"

    def test_update_agent_run(self, mock_db):
        """update_agent_run should update run fields."""
        db.create_agent_run("run-1", "Test", "Prompt")
        result = db.update_agent_run(
            "run-1",
            status="running",
            progress=0.5,
            branch="feature-x"
        )

        assert result["status"] == "running"
        assert result["progress"] == 0.5
        assert result["branch"] == "feature-x"

    def test_delete_agent_run(self, mock_db):
        """delete_agent_run should delete run."""
        db.create_agent_run("run-1", "Test", "Prompt")
        result = db.delete_agent_run("run-1")

        assert result is True
        assert db.get_agent_run("run-1") is None


# =============================================================================
# Agent Task Operations Tests
# =============================================================================

class TestAgentTaskOperations:
    """Test agent task operations."""

    def test_create_agent_task(self, mock_db):
        """create_agent_task should create task."""
        db.create_agent_run("run-1", "Test", "Prompt")
        result = db.create_agent_task(
            "task-1", "run-1", "Analyze code",
            status="in_progress",
            order_index=0
        )

        assert result["id"] == "task-1"
        assert result["name"] == "Analyze code"
        assert result["status"] == "in_progress"

    def test_get_agent_tasks(self, mock_db):
        """get_agent_tasks should return tasks for run."""
        db.create_agent_run("run-1", "Test", "Prompt")
        db.create_agent_task("task-1", "run-1", "Task 1", order_index=0)
        db.create_agent_task("task-2", "run-1", "Task 2", order_index=1)

        result = db.get_agent_tasks("run-1")

        assert len(result) == 2
        assert result[0]["order_index"] == 0

    def test_delete_agent_task(self, mock_db):
        """delete_agent_task should delete task."""
        db.create_agent_run("run-1", "Test", "Prompt")
        db.create_agent_task("task-1", "run-1", "Task")

        result = db.delete_agent_task("task-1")

        assert result is True
        assert db.get_agent_task("task-1") is None


# =============================================================================
# Agent Log Operations Tests
# =============================================================================

class TestAgentLogOperations:
    """Test agent log operations."""

    def test_add_agent_log(self, mock_db):
        """add_agent_log should add log entry."""
        db.create_agent_run("run-1", "Test", "Prompt")
        result = db.add_agent_log(
            "run-1", "Starting execution",
            level="info",
            metadata={"step": 1}
        )

        assert result["message"] == "Starting execution"
        assert result["level"] == "info"
        assert result["metadata"] == {"step": 1}

    def test_get_agent_logs(self, mock_db):
        """get_agent_logs should return logs for run."""
        db.create_agent_run("run-1", "Test", "Prompt")
        db.add_agent_log("run-1", "Log 1", level="info")
        db.add_agent_log("run-1", "Log 2", level="error")

        result = db.get_agent_logs("run-1")

        assert len(result) == 2

    def test_clear_agent_logs(self, mock_db):
        """clear_agent_logs should remove all logs for run."""
        db.create_agent_run("run-1", "Test", "Prompt")
        db.add_agent_log("run-1", "Log 1")
        db.add_agent_log("run-1", "Log 2")

        deleted = db.clear_agent_logs("run-1")

        assert deleted == 2
        assert db.get_agent_logs_count("run-1") == 0


# =============================================================================
# Clear All Sessions Test
# =============================================================================

class TestClearAllSessions:
    """Test clearing all sessions."""

    def test_clear_all_sessions(self, mock_db):
        """clear_all_sessions should delete all auth and API key sessions."""
        # Create some auth sessions
        expires = datetime.utcnow() + timedelta(hours=24)
        db.create_auth_session("auth-token-1", expires)
        db.create_auth_session("auth-token-2", expires)

        # Create API user and sessions
        db.create_api_user("user-1", "Test", "hash")
        db.create_api_key_session("api-token-1", "user-1", expires)

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


# =============================================================================
# API Key Session Tests
# =============================================================================

class TestAPIKeySessions:
    """Test API key session operations."""

    def test_create_api_key_session(self, mock_db):
        """create_api_key_session should create session."""
        db.create_api_user("user-1", "Test", "hash")
        expires = datetime.utcnow() + timedelta(hours=24)
        result = db.create_api_key_session("token123", "user-1", expires)

        assert result["token"] == "token123"
        assert result["api_user_id"] == "user-1"

    def test_get_api_key_session_valid(self, mock_db):
        """get_api_key_session should return valid session."""
        db.create_api_user("user-1", "Test", "hash")
        expires = datetime.utcnow() + timedelta(hours=24)
        db.create_api_key_session("token123", "user-1", expires)

        result = db.get_api_key_session("token123")

        assert result is not None
        assert result["token"] == "token123"

    def test_get_api_key_session_expired(self, mock_db):
        """get_api_key_session should not return expired session."""
        db.create_api_user("user-1", "Test", "hash")
        expires = datetime.utcnow() - timedelta(hours=1)
        db.create_api_key_session("token123", "user-1", expires)

        result = db.get_api_key_session("token123")

        assert result is None

    def test_delete_api_key_session(self, mock_db):
        """delete_api_key_session should delete session."""
        db.create_api_user("user-1", "Test", "hash")
        expires = datetime.utcnow() + timedelta(hours=24)
        db.create_api_key_session("token123", "user-1", expires)
        db.delete_api_key_session("token123")

        assert db.get_api_key_session("token123") is None

    def test_delete_api_key_sessions_for_user(self, mock_db):
        """delete_api_key_sessions_for_user should delete all sessions for user."""
        db.create_api_user("user-1", "Test", "hash")
        expires = datetime.utcnow() + timedelta(hours=24)
        db.create_api_key_session("token1", "user-1", expires)
        db.create_api_key_session("token2", "user-1", expires)

        db.delete_api_key_sessions_for_user("user-1")

        assert db.get_api_key_session("token1") is None
        assert db.get_api_key_session("token2") is None


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

    def test_set_user_credential(self, mock_db):
        """set_user_credential should store encrypted credential."""
        db.create_api_user("user-1", "Test", "hash")
        result = db.set_user_credential("user-1", "openai_api_key", "enc:encrypted_value")

        assert result["credential_type"] == "openai_api_key"
        assert result["encrypted_value"] == "enc:encrypted_value"

    def test_get_user_credential(self, mock_db):
        """get_user_credential should return credential."""
        db.create_api_user("user-1", "Test", "hash")
        db.set_user_credential("user-1", "openai_api_key", "enc:value")

        result = db.get_user_credential("user-1", "openai_api_key")

        assert result is not None
        assert result["encrypted_value"] == "enc:value"

    def test_get_all_user_credentials(self, mock_db):
        """get_all_user_credentials should return all credentials for user."""
        db.create_api_user("user-1", "Test", "hash")
        db.set_user_credential("user-1", "openai_api_key", "enc:val1")
        db.set_user_credential("user-1", "github_pat", "enc:val2")

        result = db.get_all_user_credentials("user-1")

        assert len(result) == 2

    def test_delete_user_credential(self, mock_db):
        """delete_user_credential should delete credential."""
        db.create_api_user("user-1", "Test", "hash")
        db.set_user_credential("user-1", "openai_api_key", "enc:value")

        result = db.delete_user_credential("user-1", "openai_api_key")

        assert result is True
        assert db.get_user_credential("user-1", "openai_api_key") is None

    def test_user_has_credential(self, mock_db):
        """user_has_credential should check credential existence."""
        db.create_api_user("user-1", "Test", "hash")

        assert db.user_has_credential("user-1", "openai_api_key") is False

        db.set_user_credential("user-1", "openai_api_key", "enc:value")

        assert db.user_has_credential("user-1", "openai_api_key") is True


# =============================================================================
# Rate Limit Tests
# =============================================================================

class TestRateLimitOperations:
    """Test rate limit operations."""

    def test_create_rate_limit(self, mock_db):
        """create_rate_limit should create rate limit config."""
        db.create_api_user("user-1", "Test", "hash")
        result = db.create_rate_limit(
            "rl-1", api_key_id="user-1",
            requests_per_minute=10,
            requests_per_hour=100
        )

        assert result["id"] == "rl-1"
        assert result["requests_per_minute"] == 10

    def test_get_rate_limit(self, mock_db):
        """get_rate_limit should return rate limit by ID."""
        db.create_api_user("user-1", "Test", "hash")
        db.create_rate_limit("rl-1", api_key_id="user-1")

        result = db.get_rate_limit("rl-1")

        assert result is not None
        assert result["id"] == "rl-1"

    def test_delete_rate_limit(self, mock_db):
        """delete_rate_limit should delete rate limit."""
        db.create_api_user("user-1", "Test", "hash")
        db.create_rate_limit("rl-1", api_key_id="user-1")

        result = db.delete_rate_limit("rl-1")

        assert result is True
        assert db.get_rate_limit("rl-1") is None


# =============================================================================
# Knowledge Document Tests
# =============================================================================

class TestKnowledgeDocuments:
    """Test knowledge document operations."""

    def test_create_knowledge_document(self, mock_db, setup_project):
        """create_knowledge_document should create document."""
        result = db.create_knowledge_document(
            "doc-1", setup_project, "readme.md", "# Hello\n\nContent here",
            content_type="text/markdown"
        )

        assert result["id"] == "doc-1"
        assert result["filename"] == "readme.md"

    def test_get_knowledge_document(self, mock_db, setup_project):
        """get_knowledge_document should return document by ID."""
        db.create_knowledge_document("doc-1", setup_project, "file.txt", "content")

        result = db.get_knowledge_document("doc-1")

        assert result is not None
        assert result["id"] == "doc-1"

    def test_get_knowledge_documents(self, mock_db, setup_project):
        """get_knowledge_documents should return all documents for project."""
        db.create_knowledge_document("doc-1", setup_project, "f1.txt", "content1")
        db.create_knowledge_document("doc-2", setup_project, "f2.txt", "content2")

        result = db.get_knowledge_documents(setup_project)

        assert len(result) == 2

    def test_delete_knowledge_document(self, mock_db, setup_project):
        """delete_knowledge_document should delete document."""
        db.create_knowledge_document("doc-1", setup_project, "file.txt", "content")

        result = db.delete_knowledge_document("doc-1")

        assert result is True
        assert db.get_knowledge_document("doc-1") is None


# =============================================================================
# Request Log Tests
# =============================================================================

class TestRequestLog:
    """Test request logging operations."""

    def test_log_request(self, mock_db):
        """log_request should log API request."""
        db.create_api_user("user-1", "Test", "hash")
        # Signature: request_id, user_id, api_key_id, endpoint, status, duration_ms
        result = db.log_request(
            "req-1", None, "user-1",
            "/api/v1/chat",
            duration_ms=150
        )

        assert result["id"] == "req-1"
        assert result["endpoint"] == "/api/v1/chat"

    def test_get_request_count(self, mock_db):
        """get_request_count should return count of requests."""
        db.create_api_user("user-1", "Test", "hash")
        db.log_request("req-1", None, "user-1", "/api")
        db.log_request("req-2", None, "user-1", "/api")

        # Get count for last hour - signature: user_id, api_key_id, since, endpoint
        since = datetime.utcnow() - timedelta(hours=1)
        count = db.get_request_count(None, "user-1", since)

        assert count == 2


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
# Agent Run Filtering Tests
# =============================================================================

class TestAgentRunFiltering:
    """Test agent run filtering operations."""

    def test_get_agent_runs(self, mock_db):
        """get_agent_runs should return runs with filters."""
        db.create_agent_run("run-1", "Run 1", "Prompt")
        db.update_agent_run("run-1", status="running")
        db.create_agent_run("run-2", "Run 2", "Prompt")

        # Get only running
        result = db.get_agent_runs(status="running")
        assert len(result) == 1
        assert result[0]["id"] == "run-1"

    def test_get_running_agent_runs(self, mock_db):
        """get_running_agent_runs should return only running runs."""
        db.create_agent_run("run-1", "Run 1", "Prompt")
        db.update_agent_run("run-1", status="running")
        db.create_agent_run("run-2", "Run 2", "Prompt")

        result = db.get_running_agent_runs()

        assert len(result) == 1
        assert result[0]["status"] == "running"

    def test_get_queued_agent_runs(self, mock_db):
        """get_queued_agent_runs should return only queued runs."""
        db.create_agent_run("run-1", "Run 1", "Prompt")  # Status is 'queued' by default
        db.create_agent_run("run-2", "Run 2", "Prompt")
        db.update_agent_run("run-2", status="running")

        result = db.get_queued_agent_runs()

        assert len(result) == 1
        assert result[0]["id"] == "run-1"


# =============================================================================
# Knowledge Chunk Tests
# =============================================================================

class TestKnowledgeChunks:
    """Test knowledge chunk operations."""

    def test_create_knowledge_chunk(self, mock_db, setup_project):
        """create_knowledge_chunk should create chunk."""
        db.create_knowledge_document("doc-1", setup_project, "file.txt", "content")

        result = db.create_knowledge_chunk("chunk-1", "doc-1", 0, "First chunk")

        assert result["id"] == "chunk-1"
        assert result["chunk_index"] == 0

    def test_get_knowledge_chunks(self, mock_db, setup_project):
        """get_knowledge_chunks should return chunks for document."""
        db.create_knowledge_document("doc-1", setup_project, "file.txt", "content")
        db.create_knowledge_chunk("chunk-1", "doc-1", 0, "First chunk")
        db.create_knowledge_chunk("chunk-2", "doc-1", 1, "Second chunk")

        result = db.get_knowledge_chunks("doc-1")

        assert len(result) == 2

    def test_delete_knowledge_chunks_for_document(self, mock_db, setup_project):
        """delete_knowledge_chunks_for_document should delete all chunks."""
        db.create_knowledge_document("doc-1", setup_project, "file.txt", "content")
        db.create_knowledge_chunk("chunk-1", "doc-1", 0, "First")
        db.create_knowledge_chunk("chunk-2", "doc-1", 1, "Second")

        deleted = db.delete_knowledge_chunks_for_document("doc-1")

        assert deleted == 2
        assert len(db.get_knowledge_chunks("doc-1")) == 0


# =============================================================================
# Agent Task Update Tests
# =============================================================================

class TestAgentTaskUpdates:
    """Test agent task update operations."""

    def test_update_agent_task(self, mock_db):
        """update_agent_task should update task fields."""
        db.create_agent_run("run-1", "Run", "Prompt")
        db.create_agent_task("task-1", "run-1", "Task 1")

        result = db.update_agent_task("task-1", status="completed")

        assert result["status"] == "completed"

    def test_get_agent_task(self, mock_db):
        """get_agent_task should return task by ID."""
        db.create_agent_run("run-1", "Run", "Prompt")
        db.create_agent_task("task-1", "run-1", "Task 1")

        result = db.get_agent_task("task-1")

        assert result is not None
        assert result["id"] == "task-1"


# =============================================================================
# Agent Logs Count Tests
# =============================================================================

class TestAgentLogsCount:
    """Test agent logs count operations."""

    def test_get_agent_logs_count(self, mock_db):
        """get_agent_logs_count should return count of logs."""
        db.create_agent_run("run-1", "Run", "Prompt")
        db.add_agent_log("run-1", "Log 1", level="info")
        db.add_agent_log("run-1", "Log 2", level="error")
        db.add_agent_log("run-1", "Log 3", level="info")

        total = db.get_agent_logs_count("run-1")
        errors = db.get_agent_logs_count("run-1", level="error")

        assert total == 3
        assert errors == 1


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
# Cleanup Expired Lockouts Tests
# =============================================================================

class TestCleanupOperations:
    """Test cleanup operations."""

    def test_cleanup_expired_lockouts(self, mock_db):
        """cleanup_expired_lockouts should remove expired lockouts."""
        # Create an expired lockout by manually inserting with past date
        mock_db.execute("""
            INSERT INTO account_lockouts (ip_address, username, locked_until, reason, created_at)
            VALUES ('192.168.1.1', NULL, '2020-01-01T00:00:00', 'Test', '2020-01-01T00:00:00')
        """)
        mock_db.commit()

        db.cleanup_expired_lockouts()

        assert db.is_ip_locked("192.168.1.1") is None

    def test_cleanup_old_login_attempts(self, mock_db):
        """cleanup_old_login_attempts should remove old attempts."""
        # Add an old attempt manually
        mock_db.execute("""
            INSERT INTO login_attempts (ip_address, username, success, created_at)
            VALUES ('192.168.1.1', 'test', 0, '2020-01-01T00:00:00')
        """)
        mock_db.commit()

        db.cleanup_old_login_attempts(max_age_hours=1)

        # Recent attempts should be counted, old ones removed
        count = db.get_failed_attempts_count("192.168.1.1")
        assert count == 0

    def test_cleanup_expired_api_key_sessions(self, mock_db):
        """cleanup_expired_api_key_sessions should remove expired sessions."""
        db.create_api_user("user-1", "Test", "hash")
        # Create expired session manually
        mock_db.execute("""
            INSERT INTO api_key_sessions (token, api_user_id, expires_at, created_at)
            VALUES ('expired-token', 'user-1', '2020-01-01T00:00:00', '2020-01-01T00:00:00')
        """)
        mock_db.commit()

        db.cleanup_expired_api_key_sessions()

        # The expired token should be gone
        result = mock_db.execute(
            "SELECT * FROM api_key_sessions WHERE token = 'expired-token'"
        ).fetchone()
        assert result is None
