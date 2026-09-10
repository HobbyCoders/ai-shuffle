"""
Extended unit tests for app/db/database.py.

These tests cover additional functions to increase coverage beyond the base tests.
"""

import pytest
from datetime import datetime, timezone

from app.db import database as db


@pytest.fixture
def test_db():
    """In-memory SQLite database built from the canonical PostgreSQL schema via
    the test shim (see tests/db/test_database.py::test_db)."""
    from tests.pg_shim import new_sqlite_connection
    conn = new_sqlite_connection()
    yield conn
    conn.close()


@pytest.fixture
def mock_db(test_db):
    """Route the app DB layer through the shim-wrapped test connection."""
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
# User Credential Policy Tests
# =============================================================================


class TestUserCredentialPolicies:
    """Test user credential policy operations."""

    def test_set_user_credential_policy(self, mock_db, setup_api_user):
        """set_user_credential_policy should create a policy."""
        # Valid policies: 'admin_provided', 'user_provided', 'optional'
        result = db.set_user_credential_policy("user-1", "openai_key", "user_provided")

        assert result is not None
        assert result["policy"] == "user_provided"

    def test_get_user_credential_policy(self, mock_db, setup_api_user):
        """get_user_credential_policy should return policy."""
        db.set_user_credential_policy("user-1", "openai_key", "user_provided")

        result = db.get_user_credential_policy("user-1", "openai_key")

        assert result is not None
        assert result["policy"] == "user_provided"

    def test_get_user_credential_policy_not_found(self, mock_db, setup_api_user):
        """get_user_credential_policy should return None for non-existent policy."""
        result = db.get_user_credential_policy("user-1", "openai_key")
        assert result is None

    def test_delete_user_credential_policy(self, mock_db, setup_api_user):
        """delete_user_credential_policy should remove policy."""
        db.set_user_credential_policy("user-1", "openai_key", "user_provided")

        result = db.delete_user_credential_policy("user-1", "openai_key")

        assert result is True
        assert db.get_user_credential_policy("user-1", "openai_key") is None

    def test_get_all_user_credential_policies(self, mock_db, setup_api_user):
        """get_all_user_credential_policies should return all policies for user."""
        initial_count = len(db.get_all_user_credential_policies("user-1"))

        db.set_user_credential_policy("user-1", "custom_key", "user_provided")
        db.set_user_credential_policy("user-1", "another_key", "optional")

        result = db.get_all_user_credential_policies("user-1")

        # Should have added 2 more policies
        assert len(result) == initial_count + 2


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

    def test_session_less_usage_counts_in_stats_and_trends(self, mock_db, setup_profile):
        """Internal generation spend has no session (usage_log.session_id NULL).
        Both the totals and the trend query LEFT JOIN sessions, so it must still
        be counted — an inner join silently dropped it from the trend chart."""
        db.log_usage(None, setup_profile, "claude-3", 100, 200, 0.10, 1000)

        stats = db.get_analytics_usage_stats()
        assert stats["total_tokens_in"] == 100
        assert stats["total_tokens_out"] == 200

        trends = db.get_analytics_usage_trends(interval="day")
        assert sum(t["query_count"] for t in trends) == 1
        assert sum(t["tokens_in"] for t in trends) == 100
        assert round(sum(t["cost_usd"] for t in trends), 6) == 0.10

    def test_get_analytics_top_sessions(self, mock_db, setup_profile, setup_project):
        """get_analytics_top_sessions should return top sessions by cost."""
        db.create_session("session-1", setup_profile, setup_project)
        db.update_session("session-1", cost_increment=0.50)

        result = db.get_analytics_top_sessions(limit=10)

        assert isinstance(result, list)


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

    @pytest.mark.skip(
        reason="search_sessions uses Postgres-native FTS (tsvector @@ plainto_tsquery, "
        "ts_headline, DISTINCT ON); not emulated by the sqlite test shim. No "
        "real-Postgres integration suite exists yet — the SQL's structure is "
        "guarded by TestSearchSessionsSql (test_database.py); behavior needs "
        "manual verification against a Postgres instance."
    )
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
