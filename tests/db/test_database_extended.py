"""
Extended unit tests for app/db/database.py.

These tests cover additional functions to increase coverage beyond the base tests.
"""

import pytest
from datetime import datetime, timedelta
from unittest.mock import patch
from contextlib import contextmanager
import sqlite3

from app.db import database as db


# Import fixtures from main test file
from tests.db.test_database import _create_test_schema


@pytest.fixture
def test_db():
    """Create an in-memory SQLite database for testing with full schema."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
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
# User Credential Policy Tests
# =============================================================================


class TestUserCredentialPolicies:
    """Test user credential policy operations."""

    def test_set_user_credential_policy(self, mock_db):
        """set_user_credential_policy should create a policy."""
        db.create_api_user("user-1", "Test", "hash")

        # Valid policies: 'admin_provided', 'user_provided', 'optional'
        result = db.set_user_credential_policy("user-1", "openai_key", "user_provided")

        assert result is not None
        assert result["policy"] == "user_provided"

    def test_get_user_credential_policy(self, mock_db):
        """get_user_credential_policy should return policy."""
        db.create_api_user("user-1", "Test", "hash")
        db.set_user_credential_policy("user-1", "openai_key", "user_provided")

        result = db.get_user_credential_policy("user-1", "openai_key")

        assert result is not None
        assert result["policy"] == "user_provided"

    def test_get_user_credential_policy_not_found(self, mock_db):
        """get_user_credential_policy should return None for non-existent policy."""
        db.create_api_user("user-1", "Test", "hash")
        result = db.get_user_credential_policy("user-1", "openai_key")
        assert result is None

    def test_delete_user_credential_policy(self, mock_db):
        """delete_user_credential_policy should remove policy."""
        db.create_api_user("user-1", "Test", "hash")
        db.set_user_credential_policy("user-1", "openai_key", "user_provided")

        result = db.delete_user_credential_policy("user-1", "openai_key")

        assert result is True
        assert db.get_user_credential_policy("user-1", "openai_key") is None

    def test_get_all_user_credential_policies(self, mock_db):
        """get_all_user_credential_policies should return all policies for user."""
        db.create_api_user("user-1", "Test", "hash")
        # create_api_user may auto-create some default policies
        initial_count = len(db.get_all_user_credential_policies("user-1"))

        db.set_user_credential_policy("user-1", "custom_key", "user_provided")
        db.set_user_credential_policy("user-1", "another_key", "optional")

        result = db.get_all_user_credential_policies("user-1")

        # Should have added 2 more policies
        assert len(result) == initial_count + 2


# =============================================================================
# GitHub Config Tests
# =============================================================================


class TestUserGitHubConfig:
    """Test user GitHub config operations."""

    def test_set_user_github_config(self, mock_db):
        """set_user_github_config should create config."""
        db.create_api_user("user-1", "Test", "hash")

        result = db.set_user_github_config(
            "user-1",
            github_username="testuser",
            github_avatar_url="https://github.com/avatar.png",
            default_repo="test/repo",
            default_branch="main"
        )

        assert result is not None
        assert result["github_username"] == "testuser"

    def test_get_user_github_config(self, mock_db):
        """get_user_github_config should return config for user."""
        db.create_api_user("user-1", "Test", "hash")
        db.set_user_github_config("user-1", github_username="testuser")

        result = db.get_user_github_config("user-1")

        assert result is not None
        assert result["github_username"] == "testuser"

    def test_get_user_github_config_not_found(self, mock_db):
        """get_user_github_config should return None for non-existent config."""
        db.create_api_user("user-1", "Test", "hash")
        result = db.get_user_github_config("user-1")
        assert result is None

    def test_delete_user_github_config(self, mock_db):
        """delete_user_github_config should remove config."""
        db.create_api_user("user-1", "Test", "hash")
        db.set_user_github_config("user-1", github_username="testuser")

        result = db.delete_user_github_config("user-1")

        assert result is True
        assert db.get_user_github_config("user-1") is None


# =============================================================================
# Analytics Operations Tests
# =============================================================================


class TestAnalyticsOperations:
    """Test analytics operations."""

    def test_get_analytics_usage_stats(self, mock_db, setup_profile):
        """get_analytics_usage_stats should return usage statistics."""
        db.create_session("session-1", setup_profile)
        db.log_usage("session-1", setup_profile, "gpt-4", 100, 200, 0.10, 1000)

        result = db.get_analytics_usage_stats()

        assert result is not None
        assert "total_tokens_in" in result
        assert result["total_tokens_in"] == 100

    def test_get_analytics_cost_breakdown(self, mock_db, setup_profile):
        """get_analytics_cost_breakdown should return cost by profile."""
        db.create_session("session-1", setup_profile)
        db.log_usage("session-1", setup_profile, "gpt-4", 100, 200, 0.10, 1000)

        result = db.get_analytics_cost_breakdown(group_by="profile")

        assert isinstance(result, list)

    def test_get_analytics_usage_trends(self, mock_db, setup_profile):
        """get_analytics_usage_trends should return daily trends."""
        db.create_session("session-1", setup_profile)
        db.log_usage("session-1", setup_profile, "gpt-4", 100, 200, 0.10, 1000)

        result = db.get_analytics_usage_trends(interval="day")

        assert isinstance(result, list)

    def test_get_analytics_top_sessions(self, mock_db, setup_profile, setup_project):
        """get_analytics_top_sessions should return top sessions by cost."""
        db.create_session("session-1", setup_profile, setup_project)
        db.update_session("session-1", cost_increment=0.50)

        result = db.get_analytics_top_sessions(limit=10)

        assert isinstance(result, list)


# =============================================================================
# Rate Limit Extended Tests
# =============================================================================


class TestRateLimitExtended:
    """Test extended rate limit operations."""

    def test_update_rate_limit(self, mock_db):
        """update_rate_limit should update existing limit."""
        db.create_api_user("user-1", "Test", "hash")
        db.create_rate_limit("rl-1", api_key_id="user-1", requests_per_minute=10)

        result = db.update_rate_limit("rl-1", requests_per_minute=20)

        assert result is not None
        assert result["requests_per_minute"] == 20


# =============================================================================
# Git Repository Extended Tests
# =============================================================================


class TestGitRepositoryExtended:
    """Test extended git repository operations."""

    def test_update_git_repository(self, mock_db, setup_project):
        """update_git_repository should update repo fields."""
        db.create_git_repository("repo-1", setup_project, remote_url="https://old.com")

        result = db.update_git_repository("repo-1", remote_url="https://new.com")

        assert result is not None
        assert result["remote_url"] == "https://new.com"

    def test_update_git_repository_synced(self, mock_db, setup_project):
        """update_git_repository_synced should update last_synced_at."""
        db.create_git_repository("repo-1", setup_project)

        result = db.update_git_repository_synced("repo-1")

        assert result is not None
        assert result["last_synced_at"] is not None


# =============================================================================
# Worktree Extended Tests
# =============================================================================


class TestWorktreeExtended:
    """Test extended worktree operations."""

    @pytest.fixture
    def setup_repository(self, mock_db, setup_project):
        """Create a test repository."""
        repo = db.create_git_repository("repo-1", setup_project)
        return repo["id"]

    def test_get_worktree_by_branch(self, mock_db, setup_repository):
        """get_worktree_by_branch should find worktree by branch name."""
        db.create_worktree("wt-1", setup_repository, "feature-branch", "/path")

        result = db.get_worktree_by_branch(setup_repository, "feature-branch")

        assert result is not None
        assert result["branch_name"] == "feature-branch"

    def test_get_worktree_by_branch_not_found(self, mock_db, setup_repository):
        """get_worktree_by_branch should return None for non-existent branch."""
        result = db.get_worktree_by_branch(setup_repository, "nonexistent")
        assert result is None


# =============================================================================
# Pending 2FA Session Tests
# =============================================================================


class TestPending2FASessions:
    """Test pending 2FA session operations."""

    def test_create_pending_2fa_session(self, mock_db):
        """create_pending_2fa_session should create a pending session."""
        expires = datetime.utcnow() + timedelta(minutes=10)

        result = db.create_pending_2fa_session("token123", "admin", expires)

        assert result["token"] == "token123"
        assert result["username"] == "admin"

    def test_get_pending_2fa_session(self, mock_db):
        """get_pending_2fa_session should return valid session."""
        expires = datetime.utcnow() + timedelta(minutes=10)
        db.create_pending_2fa_session("token123", "admin", expires)

        result = db.get_pending_2fa_session("token123")

        assert result is not None
        assert result["username"] == "admin"

    def test_get_pending_2fa_session_expired(self, mock_db):
        """get_pending_2fa_session should not return expired session."""
        expires = datetime.utcnow() - timedelta(minutes=10)
        db.create_pending_2fa_session("token123", "admin", expires)

        result = db.get_pending_2fa_session("token123")

        assert result is None

    def test_delete_pending_2fa_session(self, mock_db):
        """delete_pending_2fa_session should remove session."""
        expires = datetime.utcnow() + timedelta(minutes=10)
        db.create_pending_2fa_session("token123", "admin", expires)

        db.delete_pending_2fa_session("token123")
        result = db.get_pending_2fa_session("token123")

        assert result is None


# =============================================================================
# Template Extended Tests
# =============================================================================


class TestTemplateExtended:
    """Test extended template operations."""

    def test_get_all_templates(self, mock_db, setup_profile):
        """get_all_templates should return all templates."""
        db.create_template("t-1", "Template 1", "Desc1", "prompt1", profile_id=setup_profile)
        db.create_template("t-2", "Template 2", "Desc2", "prompt2", profile_id=setup_profile)

        result = db.get_all_templates()

        assert len(result) == 2

    def test_get_all_templates_with_profile_filter(self, mock_db, setup_profile):
        """get_all_templates should filter by profile."""
        # Create another profile
        profile2 = db.create_profile("profile-2", "Profile 2", None, {})

        db.create_template("t-1", "Template 1", "Desc", "prompt", profile_id=setup_profile)
        db.create_template("t-2", "Template 2", "Desc", "prompt", profile_id=profile2["id"])

        result = db.get_all_templates(profile_id=setup_profile)

        assert len(result) == 1
        assert result[0]["profile_id"] == setup_profile

    def test_get_all_templates_with_category_filter(self, mock_db):
        """get_all_templates should filter by category."""
        db.create_template("t-1", "Template 1", "Desc", "prompt", category="coding")
        db.create_template("t-2", "Template 2", "Desc", "prompt", category="writing")

        result = db.get_all_templates(category="coding")

        assert len(result) == 1
        assert result[0]["category"] == "coding"


# =============================================================================
# Session Update Extended Tests
# =============================================================================


class TestSessionUpdateExtended:
    """Test extended session update operations."""

    def test_update_session_sdk_session_id(self, mock_db, setup_profile):
        """update_session should update sdk_session_id field."""
        db.create_session("session-1", setup_profile)

        result = db.update_session("session-1", sdk_session_id="sdk-12345")

        assert result["sdk_session_id"] == "sdk-12345"

    def test_increment_session_turn_count(self, mock_db, setup_profile):
        """update_session should increment turn count."""
        db.create_session("session-1", setup_profile)

        # Parameter is turn_increment, not turn_count_increment
        result = db.update_session("session-1", turn_increment=1)

        assert result["turn_count"] == 1


# =============================================================================
# Agent Run Extended Tests
# =============================================================================


class TestAgentRunExtended:
    """Test extended agent run operations."""

    def test_complete_agent_run(self, mock_db):
        """update_agent_run should set completion status and timestamp."""
        db.create_agent_run("run-1", "Test", "Prompt")

        result = db.update_agent_run(
            "run-1",
            status="completed",
            progress=1.0,
            result_summary="Task completed successfully"
        )

        assert result["status"] == "completed"
        assert result["progress"] == 1.0
        assert result["result_summary"] == "Task completed successfully"

    def test_fail_agent_run(self, mock_db):
        """update_agent_run should set error status."""
        db.create_agent_run("run-1", "Test", "Prompt")

        result = db.update_agent_run(
            "run-1",
            status="failed",
            error="Something went wrong"
        )

        assert result["status"] == "failed"
        assert result["error"] == "Something went wrong"

    def test_update_agent_run_with_pr_url(self, mock_db):
        """update_agent_run should update PR URL."""
        db.create_agent_run("run-1", "Test", "Prompt")

        result = db.update_agent_run("run-1", pr_url="https://github.com/test/repo/pull/1")

        assert result["pr_url"] == "https://github.com/test/repo/pull/1"


# =============================================================================
# Message Extended Tests
# =============================================================================


class TestMessageExtended:
    """Test extended message operations."""

    def test_update_message_increments_version(self, mock_db, setup_profile):
        """update_message_content should increment version."""
        db.create_session("session-1", setup_profile)
        msg = db.add_session_message("session-1", "assistant", "Original")

        result = db.update_message_content(msg["id"], "Updated content")

        assert result["version"] == 2


# =============================================================================
# Agent Task Hierarchy Tests
# =============================================================================


class TestAgentTaskHierarchy:
    """Test agent task hierarchy operations."""

    def test_create_child_task(self, mock_db):
        """create_agent_task should support parent-child relationship."""
        db.create_agent_run("run-1", "Run", "Prompt")
        db.create_agent_task("task-parent", "run-1", "Parent Task")
        child = db.create_agent_task(
            "task-child", "run-1", "Child Task",
            parent_task_id="task-parent"
        )

        assert child["parent_task_id"] == "task-parent"


# =============================================================================
# Webhook Extended Tests
# =============================================================================


class TestWebhookExtended:
    """Test extended webhook operations."""

    def test_update_webhook_events(self, mock_db):
        """update_webhook should update events."""
        db.create_webhook("wh-1", "https://example.com", ["event.one"])

        result = db.update_webhook("wh-1", events=["event.one", "event.two"])

        assert result is not None
        assert "event.two" in result["events"]

    def test_update_webhook_secret(self, mock_db):
        """update_webhook should update secret."""
        db.create_webhook("wh-1", "https://example.com", ["event"])

        result = db.update_webhook("wh-1", secret="new_secret")

        assert result["secret"] == "new_secret"


# =============================================================================
# Cleanup Extended Tests
# =============================================================================


class TestCleanupExtended:
    """Test extended cleanup operations."""

    def test_cleanup_expired_pending_2fa_sessions(self, mock_db):
        """Expired pending 2FA sessions should be cleaned up."""
        # Create expired session manually
        mock_db.execute("""
            INSERT INTO pending_2fa_sessions (token, username, expires_at, created_at)
            VALUES ('expired-token', 'admin', '2020-01-01T00:00:00', '2020-01-01T00:00:00')
        """)
        mock_db.commit()

        # The expired session should not be returned
        result = db.get_pending_2fa_session("expired-token")
        assert result is None


# =============================================================================
# Subagent Extended Tests
# =============================================================================


class TestSubagentExtended:
    """Test extended subagent operations."""

    def test_update_subagent_model(self, mock_db):
        """update_subagent should update model."""
        db.create_subagent("sub-1", "Test", "Description", "Prompt", model="gpt-4")

        result = db.update_subagent("sub-1", model="claude-3")

        assert result["model"] == "claude-3"


# =============================================================================
# Session Search Extended Tests
# =============================================================================


class TestSessionSearchExtended:
    """Test extended session search operations."""

    def test_search_sessions_with_limit(self, mock_db, setup_profile):
        """search_sessions should respect limit."""
        for i in range(5):
            db.create_session(f"session-{i}", setup_profile, title=f"Python topic {i}")

        result = db.search_sessions("Python", limit=2)

        assert len(result) == 2


# =============================================================================
# Profile Extended Tests
# =============================================================================


class TestProfileExtended:
    """Test extended profile operations."""

    def test_update_profile_description(self, mock_db):
        """update_profile should update description."""
        db.create_profile("profile-1", "Test", "Old description", {})

        result = db.update_profile("profile-1", description="New description")

        assert result["description"] == "New description"

    def test_update_profile_config(self, mock_db):
        """update_profile should update config."""
        db.create_profile("profile-1", "Test", None, {"old": "value"})

        result = db.update_profile("profile-1", config={"new": "config"})

        assert result["config"] == {"new": "config"}
