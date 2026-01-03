"""
Tests for WorktreeManager

Comprehensive test coverage for worktree lifecycle management including:
- Worktree creation (standalone and with session)
- Worktree deletion and cleanup
- Worktree listing and status checks
- Sync operations
- Error handling
- Concurrent operations
- Path sanitization
"""

import pytest
import uuid
from pathlib import Path
from unittest.mock import patch, MagicMock, PropertyMock
from typing import Dict, Any, List, Optional

# Import module under test
from app.core.worktree_manager import WorktreeManager, WorktreeError, worktree_manager
from app.db.database import WorktreeStatus


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def mock_settings(temp_dir):
    """Mock settings with temporary workspace directory"""
    with patch("app.core.worktree_manager.settings") as mock:
        mock.workspace_dir = temp_dir
        yield mock


@pytest.fixture
def mock_git_service():
    """Mock git service for testing without actual git operations"""
    with patch("app.core.worktree_manager.git_service") as mock:
        # Default behaviors - can be overridden in tests
        mock.is_git_repo.return_value = True
        mock.get_remote_url.return_value = "https://github.com/test/repo.git"
        mock.get_default_branch.return_value = "main"
        mock.list_branches.return_value = [
            {"name": "main", "is_remote": False, "is_current": True},
            {"name": "develop", "is_remote": False, "is_current": False},
        ]
        mock.add_worktree.return_value = (True, None)
        mock.remove_worktree.return_value = True
        mock.list_worktrees.return_value = []
        mock.get_status.return_value = {
            "is_git_repo": True,
            "current_branch": "main",
            "is_clean": True,
            "staged": [],
            "modified": [],
            "untracked": []
        }
        mock.delete_branch.return_value = True
        yield mock


@pytest.fixture
def mock_database():
    """Mock database module for testing without actual database operations"""
    with patch("app.core.worktree_manager.database") as mock:
        # Default project
        mock.get_project.return_value = {
            "id": "proj-test123",
            "name": "Test Project",
            "path": "test-project",
            "settings": {"default_profile_id": "profile-default"}
        }

        # Default repository
        mock.get_git_repository_by_project.return_value = {
            "id": "repo-test123",
            "project_id": "proj-test123",
            "remote_url": "https://github.com/test/repo.git",
            "default_branch": "main"
        }

        mock.get_git_repository.return_value = {
            "id": "repo-test123",
            "project_id": "proj-test123",
            "remote_url": "https://github.com/test/repo.git",
            "default_branch": "main"
        }

        # Default worktree behaviors
        mock.get_active_worktrees_for_repository.return_value = []
        mock.get_worktrees_for_repository.return_value = []
        mock.get_worktrees_with_sessions.return_value = []

        def create_worktree_side_effect(
            worktree_id, repository_id, branch_name, worktree_path,
            session_id=None, base_branch=None, status="active"
        ):
            return {
                "id": worktree_id,
                "repository_id": repository_id,
                "branch_name": branch_name,
                "worktree_path": worktree_path,
                "session_id": session_id,
                "base_branch": base_branch,
                "status": status,
                "created_at": "2024-01-01T00:00:00"
            }
        mock.create_worktree.side_effect = create_worktree_side_effect

        mock.get_worktree.return_value = None
        mock.update_worktree.return_value = True
        mock.delete_worktree.return_value = True

        # Profile/session defaults
        mock.get_all_profiles.return_value = [{"id": "profile-default", "name": "Default"}]

        def create_session_side_effect(
            session_id, profile_id, project_id, title, worktree_id=None
        ):
            return {
                "id": session_id,
                "profile_id": profile_id,
                "project_id": project_id,
                "title": title,
                "worktree_id": worktree_id,
                "status": "active",
                "created_at": "2024-01-01T00:00:00"
            }
        mock.create_session.side_effect = create_session_side_effect

        mock.get_session.return_value = None
        mock.get_sessions_for_worktree.return_value = []
        mock.create_git_repository.return_value = {
            "id": "repo-new123",
            "project_id": "proj-test123"
        }
        mock.get_worktree_by_branch.return_value = None

        yield mock


@pytest.fixture
def manager(mock_git_service, mock_database, mock_settings):
    """Create a WorktreeManager instance with mocked dependencies"""
    mgr = WorktreeManager()
    # Replace git_service with mock
    mgr.git_service = mock_git_service
    return mgr


# =============================================================================
# Path and Sanitization Tests
# =============================================================================

class TestPathSanitization:
    """Tests for path generation and branch name sanitization"""

    def test_sanitize_branch_name_with_slashes(self, manager):
        """Test branch names with forward slashes are sanitized"""
        result = manager._sanitize_branch_name("feature/new-feature")
        assert result == "feature-new-feature"

    def test_sanitize_branch_name_with_backslashes(self, manager):
        """Test branch names with backslashes are sanitized"""
        result = manager._sanitize_branch_name("feature\\new-feature")
        assert result == "feature-new-feature"

    def test_sanitize_branch_name_with_colons(self, manager):
        """Test branch names with colons are sanitized"""
        result = manager._sanitize_branch_name("fix:critical")
        assert result == "fix-critical"

    def test_sanitize_branch_name_with_asterisks(self, manager):
        """Test branch names with asterisks are sanitized"""
        result = manager._sanitize_branch_name("feature*test")
        assert result == "feature-test"

    def test_sanitize_branch_name_multiple_characters(self, manager):
        """Test branch names with multiple special characters"""
        result = manager._sanitize_branch_name("feature/test:fix*bug\\end")
        assert result == "feature-test-fix-bug-end"

    def test_sanitize_branch_name_no_special_chars(self, manager):
        """Test branch names without special characters pass through"""
        result = manager._sanitize_branch_name("simple-branch-name")
        assert result == "simple-branch-name"

    def test_get_worktree_path(self, manager, mock_settings, temp_dir):
        """Test worktree path generation"""
        result = manager._get_worktree_path("proj-123", "feature/test")
        expected = temp_dir / ".worktrees" / "proj-123" / "feature-test"
        assert result == expected

    def test_get_relative_worktree_path(self, manager):
        """Test relative worktree path generation"""
        result = manager._get_relative_worktree_path("proj-123", "feature/test")
        assert result == ".worktrees/proj-123/feature-test"

    def test_get_worktree_base_dir(self, manager, mock_settings, temp_dir):
        """Test worktree base directory generation"""
        result = manager._get_worktree_base_dir("proj-123")
        expected = temp_dir / ".worktrees" / "proj-123"
        assert result == expected


# =============================================================================
# Repository Record Tests
# =============================================================================

class TestEnsureRepositoryRecord:
    """Tests for _ensure_repository_record method"""

    def test_ensure_repository_record_not_git_repo(self, manager, mock_git_service, mock_database, mock_settings, temp_dir):
        """Test behavior when project is not a git repo"""
        mock_git_service.is_git_repo.return_value = False

        result = manager._ensure_repository_record("proj-123", "test-project")

        assert result is None
        mock_database.get_git_repository_by_project.assert_not_called()

    def test_ensure_repository_record_existing(self, manager, mock_git_service, mock_database, mock_settings, temp_dir):
        """Test returns existing repository record"""
        existing_repo = {"id": "repo-existing", "project_id": "proj-123"}
        mock_database.get_git_repository_by_project.return_value = existing_repo

        result = manager._ensure_repository_record("proj-123", "test-project")

        assert result == existing_repo
        mock_database.create_git_repository.assert_not_called()

    def test_ensure_repository_record_creates_new(self, manager, mock_git_service, mock_database, mock_settings, temp_dir):
        """Test creates new repository record when none exists"""
        mock_database.get_git_repository_by_project.return_value = None

        result = manager._ensure_repository_record("proj-123", "test-project")

        mock_database.create_git_repository.assert_called_once()
        call_kwargs = mock_database.create_git_repository.call_args
        assert call_kwargs.kwargs["project_id"] == "proj-123"

    def test_ensure_repository_record_extracts_github_ssh_url(self, manager, mock_git_service, mock_database, mock_settings, temp_dir):
        """Test extracts GitHub repo name from SSH URL"""
        mock_database.get_git_repository_by_project.return_value = None
        mock_git_service.get_remote_url.return_value = "git@github.com:owner/repo.git"

        manager._ensure_repository_record("proj-123", "test-project")

        call_kwargs = mock_database.create_git_repository.call_args
        assert call_kwargs.kwargs["github_repo_name"] == "owner/repo"

    def test_ensure_repository_record_extracts_github_https_url(self, manager, mock_git_service, mock_database, mock_settings, temp_dir):
        """Test extracts GitHub repo name from HTTPS URL"""
        mock_database.get_git_repository_by_project.return_value = None
        mock_git_service.get_remote_url.return_value = "https://github.com/owner/repo.git"

        manager._ensure_repository_record("proj-123", "test-project")

        call_kwargs = mock_database.create_git_repository.call_args
        assert call_kwargs.kwargs["github_repo_name"] == "owner/repo"

    def test_ensure_repository_record_non_github_url(self, manager, mock_git_service, mock_database, mock_settings, temp_dir):
        """Test handles non-GitHub URLs gracefully"""
        mock_database.get_git_repository_by_project.return_value = None
        mock_git_service.get_remote_url.return_value = "https://gitlab.com/owner/repo.git"

        manager._ensure_repository_record("proj-123", "test-project")

        call_kwargs = mock_database.create_git_repository.call_args
        assert call_kwargs.kwargs["github_repo_name"] is None


# =============================================================================
# Worktree Creation Tests
# =============================================================================

class TestCreateWorktree:
    """Tests for create_worktree method"""

    def test_create_worktree_success(self, manager, mock_database, mock_git_service, mock_settings, temp_dir):
        """Test successful worktree creation"""
        result = manager.create_worktree(
            project_id="proj-test123",
            branch_name="develop"
        )

        assert result is not None
        assert result["branch_name"] == "develop"
        mock_git_service.add_worktree.assert_called_once()
        mock_database.create_worktree.assert_called_once()

    def test_create_worktree_project_not_found(self, manager, mock_database):
        """Test error when project not found"""
        mock_database.get_project.return_value = None

        with pytest.raises(WorktreeError) as excinfo:
            manager.create_worktree("proj-nonexistent", "main")

        assert "Project not found" in str(excinfo.value)

    def test_create_worktree_not_git_repo(self, manager, mock_database, mock_git_service, mock_settings, temp_dir):
        """Test error when project is not a git repository"""
        mock_git_service.is_git_repo.return_value = False
        mock_database.get_git_repository_by_project.return_value = None

        with pytest.raises(WorktreeError) as excinfo:
            manager.create_worktree("proj-test123", "main")

        assert "not a git repository" in str(excinfo.value)

    def test_create_worktree_branch_not_exists(self, manager, mock_database, mock_git_service, mock_settings, temp_dir):
        """Test error when branch does not exist"""
        mock_git_service.list_branches.return_value = [
            {"name": "main", "is_remote": False}
        ]
        # No remote branch either

        with pytest.raises(WorktreeError) as excinfo:
            manager.create_worktree("proj-test123", "nonexistent-branch")

        assert "does not exist" in str(excinfo.value)

    def test_create_worktree_new_branch(self, manager, mock_database, mock_git_service, mock_settings, temp_dir):
        """Test creating worktree with new branch"""
        result = manager.create_worktree(
            project_id="proj-test123",
            branch_name="new-feature",
            create_new_branch=True,
            base_branch="main"
        )

        assert result is not None
        mock_git_service.add_worktree.assert_called_once()
        call_kwargs = mock_git_service.add_worktree.call_args
        assert call_kwargs.kwargs["new_branch"] is True
        assert call_kwargs.kwargs["base_branch"] == "main"

    def test_create_worktree_new_branch_already_exists(self, manager, mock_database, mock_git_service, mock_settings, temp_dir):
        """Test error when trying to create branch that already exists"""
        mock_git_service.list_branches.return_value = [
            {"name": "main", "is_remote": False},
            {"name": "develop", "is_remote": False}
        ]

        with pytest.raises(WorktreeError) as excinfo:
            manager.create_worktree(
                project_id="proj-test123",
                branch_name="develop",
                create_new_branch=True
            )

        assert "already exists" in str(excinfo.value)

    def test_create_worktree_path_already_exists(self, manager, mock_database, mock_git_service, mock_settings, temp_dir):
        """Test error when worktree path already exists on disk"""
        # Create the directory first
        worktree_path = temp_dir / ".worktrees" / "proj-test123" / "develop"
        worktree_path.mkdir(parents=True)

        with pytest.raises(WorktreeError) as excinfo:
            manager.create_worktree("proj-test123", "develop")

        assert "already exists" in str(excinfo.value)

    def test_create_worktree_git_failure(self, manager, mock_database, mock_git_service, mock_settings, temp_dir):
        """Test error handling when git add_worktree fails"""
        mock_git_service.add_worktree.return_value = (False, "fatal: branch already checked out")

        with pytest.raises(WorktreeError) as excinfo:
            manager.create_worktree("proj-test123", "develop")

        assert "Git failed" in str(excinfo.value)

    def test_create_worktree_returns_existing_if_valid(self, manager, mock_database, mock_git_service, mock_settings, temp_dir):
        """Test returns existing worktree if branch already has valid worktree"""
        existing_worktree = {
            "id": "wt-existing",
            "branch_name": "develop",
            "worktree_path": ".worktrees/proj-test123/develop",
            "status": WorktreeStatus.ACTIVE
        }
        mock_database.get_active_worktrees_for_repository.return_value = [existing_worktree]

        # Create the path so it's considered valid
        worktree_full_path = temp_dir / ".worktrees" / "proj-test123" / "develop"
        worktree_full_path.mkdir(parents=True)

        result = manager.create_worktree("proj-test123", "develop")

        assert result == existing_worktree
        mock_git_service.add_worktree.assert_not_called()

    def test_create_worktree_cleans_stale_record(self, manager, mock_database, mock_git_service, mock_settings, temp_dir):
        """Test cleans up stale worktree record and allows creation"""
        stale_worktree = {
            "id": "wt-stale",
            "branch_name": "develop",
            "worktree_path": ".worktrees/proj-test123/develop-old",  # Path doesn't exist
            "status": WorktreeStatus.ACTIVE
        }
        mock_database.get_active_worktrees_for_repository.return_value = [stale_worktree]

        result = manager.create_worktree("proj-test123", "develop")

        # Should have cleaned up the stale record
        mock_database.update_worktree.assert_called_with("wt-stale", status=WorktreeStatus.DELETED)
        # And created a new one
        assert result is not None
        assert result["branch_name"] == "develop"

    def test_create_worktree_database_error_cleanup(self, manager, mock_database, mock_git_service, mock_settings, temp_dir):
        """Test git worktree is cleaned up if database creation fails"""
        mock_database.create_worktree.side_effect = Exception("Database error")

        with pytest.raises(WorktreeError):
            manager.create_worktree("proj-test123", "develop")

        # Git worktree should be removed
        mock_git_service.remove_worktree.assert_called_once()

    def test_create_worktree_concurrent_creation_handling(self, manager, mock_database, mock_git_service, mock_settings, temp_dir):
        """Test handles concurrent creation (unique constraint violation)"""
        mock_database.create_worktree.side_effect = Exception("UNIQUE constraint failed")
        mock_database.get_worktree_by_branch.return_value = {
            "id": "wt-concurrent",
            "branch_name": "develop",
            "status": WorktreeStatus.ACTIVE
        }

        result = manager.create_worktree("proj-test123", "develop")

        # Should return the existing worktree
        assert result["id"] == "wt-concurrent"


# =============================================================================
# Worktree Session Creation Tests
# =============================================================================

class TestCreateWorktreeSession:
    """Tests for create_worktree_session method"""

    def test_create_worktree_session_success(self, manager, mock_database, mock_git_service, mock_settings, temp_dir):
        """Test successful worktree + session creation"""
        worktree, session = manager.create_worktree_session(
            project_id="proj-test123",
            branch_name="develop"
        )

        assert worktree is not None
        assert session is not None
        assert worktree["branch_name"] == "develop"
        assert session["title"] == "Branch: develop"
        mock_database.create_worktree.assert_called_once()
        mock_database.create_session.assert_called_once()

    def test_create_worktree_session_with_profile(self, manager, mock_database, mock_git_service, mock_settings, temp_dir):
        """Test worktree session creation with specific profile"""
        worktree, session = manager.create_worktree_session(
            project_id="proj-test123",
            branch_name="develop",
            profile_id="profile-custom"
        )

        call_kwargs = mock_database.create_session.call_args
        assert call_kwargs.kwargs["profile_id"] == "profile-custom"

    def test_create_worktree_session_uses_project_default_profile(self, manager, mock_database, mock_git_service, mock_settings, temp_dir):
        """Test uses project default profile when none specified"""
        mock_database.get_project.return_value = {
            "id": "proj-test123",
            "name": "Test Project",
            "path": "test-project",
            "settings": {"default_profile_id": "profile-project-default"}
        }

        worktree, session = manager.create_worktree_session(
            project_id="proj-test123",
            branch_name="develop"
        )

        call_kwargs = mock_database.create_session.call_args
        assert call_kwargs.kwargs["profile_id"] == "profile-project-default"

    def test_create_worktree_session_no_profiles_error(self, manager, mock_database, mock_git_service, mock_settings, temp_dir):
        """Test error when no profiles available"""
        mock_database.get_project.return_value = {
            "id": "proj-test123",
            "name": "Test Project",
            "path": "test-project",
            "settings": {}
        }
        mock_database.get_all_profiles.return_value = []

        with pytest.raises(WorktreeError) as excinfo:
            manager.create_worktree_session(
                project_id="proj-test123",
                branch_name="develop"
            )

        assert "No profiles available" in str(excinfo.value)

    def test_create_worktree_session_project_not_found(self, manager, mock_database):
        """Test error when project not found"""
        mock_database.get_project.return_value = None

        with pytest.raises(WorktreeError) as excinfo:
            manager.create_worktree_session("proj-nonexistent", "main")

        assert "Project not found" in str(excinfo.value)

    def test_create_worktree_session_existing_worktree_error(self, manager, mock_database, mock_git_service, mock_settings, temp_dir):
        """Test error when branch already has active worktree"""
        existing_worktree = {
            "id": "wt-existing",
            "branch_name": "develop",
            "worktree_path": ".worktrees/proj-test123/develop"
        }
        mock_database.get_active_worktrees_for_repository.return_value = [existing_worktree]

        # Create the path so it's valid
        (temp_dir / ".worktrees" / "proj-test123" / "develop").mkdir(parents=True)

        with pytest.raises(WorktreeError) as excinfo:
            manager.create_worktree_session("proj-test123", "develop")

        assert "already has an active worktree" in str(excinfo.value)

    def test_create_worktree_session_cleanup_on_session_failure(self, manager, mock_database, mock_git_service, mock_settings, temp_dir):
        """Test cleanup when session creation fails"""
        mock_database.create_session.side_effect = Exception("Session creation failed")

        with pytest.raises(WorktreeError):
            manager.create_worktree_session("proj-test123", "develop")

        # Git worktree should be removed
        mock_git_service.remove_worktree.assert_called()
        # Worktree record should be deleted
        mock_database.delete_worktree.assert_called()


# =============================================================================
# Worktree Validity Tests
# =============================================================================

class TestIsWorktreeValid:
    """Tests for is_worktree_valid method"""

    def test_is_worktree_valid_with_existing_path(self, manager, mock_settings, temp_dir):
        """Test returns True when worktree path exists"""
        # Create the worktree directory
        worktree_path = temp_dir / "test-worktree"
        worktree_path.mkdir()

        worktree = {"worktree_path": "test-worktree"}
        result = manager.is_worktree_valid(worktree)

        assert result is True

    def test_is_worktree_valid_with_missing_path(self, manager, mock_settings, temp_dir):
        """Test returns False when worktree path doesn't exist"""
        worktree = {"worktree_path": "nonexistent-worktree"}
        result = manager.is_worktree_valid(worktree)

        assert result is False

    def test_is_worktree_valid_with_none(self, manager):
        """Test returns False when worktree is None"""
        result = manager.is_worktree_valid(None)
        assert result is False

    def test_is_worktree_valid_with_empty_dict(self, manager, mock_settings, temp_dir):
        """Test handles empty worktree dict"""
        # This would raise KeyError without proper handling
        worktree = {}
        try:
            result = manager.is_worktree_valid(worktree)
        except KeyError:
            pytest.fail("is_worktree_valid should handle missing worktree_path")


# =============================================================================
# Sync Worktrees Tests
# =============================================================================

class TestSyncWorktreesForProject:
    """Tests for sync_worktrees_for_project method"""

    def test_sync_worktrees_project_not_found(self, manager, mock_database):
        """Test sync returns error for nonexistent project"""
        mock_database.get_project.return_value = None

        result = manager.sync_worktrees_for_project("proj-nonexistent")

        assert result["error"] == "Project not found"
        assert result["cleaned_up"] == []
        assert result["active"] == 0

    def test_sync_worktrees_not_git_repo(self, manager, mock_database):
        """Test sync returns result indicating not a git repo"""
        mock_database.get_git_repository_by_project.return_value = None

        result = manager.sync_worktrees_for_project("proj-test123")

        assert result["is_git_repo"] is False
        assert result["cleaned_up"] == []
        assert result["active"] == 0

    def test_sync_worktrees_cleans_stale_records(self, manager, mock_database, mock_settings, temp_dir):
        """Test sync cleans up stale worktree records"""
        stale_worktree = {
            "id": "wt-stale",
            "branch_name": "stale-branch",
            "worktree_path": ".worktrees/proj-test123/stale-branch"  # Path doesn't exist
        }
        mock_database.get_active_worktrees_for_repository.return_value = [stale_worktree]

        result = manager.sync_worktrees_for_project("proj-test123")

        assert len(result["cleaned_up"]) == 1
        assert result["cleaned_up"][0]["id"] == "wt-stale"
        assert result["active"] == 0
        mock_database.update_worktree.assert_called_with("wt-stale", status=WorktreeStatus.DELETED)

    def test_sync_worktrees_counts_active(self, manager, mock_database, mock_settings, temp_dir):
        """Test sync counts valid active worktrees"""
        # Create valid worktree path
        valid_path = temp_dir / ".worktrees" / "proj-test123" / "valid-branch"
        valid_path.mkdir(parents=True)

        valid_worktree = {
            "id": "wt-valid",
            "branch_name": "valid-branch",
            "worktree_path": ".worktrees/proj-test123/valid-branch"
        }
        mock_database.get_active_worktrees_for_repository.return_value = [valid_worktree]

        result = manager.sync_worktrees_for_project("proj-test123")

        assert result["active"] == 1
        assert len(result["cleaned_up"]) == 0
        mock_database.update_worktree.assert_not_called()


class TestSyncWorktrees:
    """Tests for sync_worktrees method (legacy sync)"""

    def test_sync_worktrees_project_not_found(self, manager, mock_database):
        """Test sync returns error for nonexistent project"""
        mock_database.get_project.return_value = None

        result = manager.sync_worktrees("proj-nonexistent")

        assert any("Project not found" in err for err in result["errors"])

    def test_sync_worktrees_no_repo(self, manager, mock_database):
        """Test sync returns error when no git repo"""
        mock_database.get_git_repository_by_project.return_value = None

        result = manager.sync_worktrees("proj-test123")

        assert "No git repository" in result["errors"][0]

    def test_sync_worktrees_marks_orphaned(self, manager, mock_database, mock_git_service, mock_settings, temp_dir):
        """Test sync marks missing worktrees as orphaned"""
        orphaned_worktree = {
            "id": "wt-orphaned",
            "worktree_path": ".worktrees/proj-test123/orphaned-branch",
            "status": WorktreeStatus.ACTIVE
        }
        mock_database.get_worktrees_for_repository.return_value = [orphaned_worktree]
        mock_git_service.list_worktrees.return_value = []  # Not in git

        result = manager.sync_worktrees("proj-test123")

        assert result["orphaned"] == 1
        mock_database.update_worktree.assert_called_with("wt-orphaned", status=WorktreeStatus.ORPHANED)

    def test_sync_worktrees_cleans_deleted_records(self, manager, mock_database, mock_git_service, mock_settings, temp_dir):
        """Test sync cleans up deleted/orphaned records"""
        deleted_worktree = {
            "id": "wt-deleted",
            "worktree_path": ".worktrees/proj-test123/deleted-branch",
            "status": WorktreeStatus.DELETED
        }
        mock_database.get_worktrees_for_repository.return_value = [deleted_worktree]
        mock_git_service.list_worktrees.return_value = []

        result = manager.sync_worktrees("proj-test123")

        assert result["cleaned_up"] == 1
        mock_database.delete_worktree.assert_called_with("wt-deleted")

    def test_sync_worktrees_counts_synced(self, manager, mock_database, mock_git_service, mock_settings, temp_dir):
        """Test sync counts worktrees that exist in both git and database"""
        worktree_path = str(temp_dir / ".worktrees" / "proj-test123" / "synced-branch")

        db_worktree = {
            "id": "wt-synced",
            "worktree_path": ".worktrees/proj-test123/synced-branch",
            "status": WorktreeStatus.ACTIVE
        }
        mock_database.get_worktrees_for_repository.return_value = [db_worktree]
        mock_git_service.list_worktrees.return_value = [
            {"path": worktree_path, "is_main": False}
        ]

        result = manager.sync_worktrees("proj-test123")

        assert result["synced"] == 1


# =============================================================================
# Get Available Worktrees Tests
# =============================================================================

class TestGetAvailableWorktrees:
    """Tests for get_available_worktrees method"""

    def test_get_available_worktrees_no_repo(self, manager, mock_database):
        """Test returns empty list when no repo"""
        mock_database.get_git_repository_by_project.return_value = None

        result = manager.get_available_worktrees("proj-test123")

        assert result == []

    def test_get_available_worktrees_filters_missing(self, manager, mock_database, mock_settings, temp_dir):
        """Test filters out worktrees where path doesn't exist"""
        missing_worktree = {
            "id": "wt-missing",
            "worktree_path": ".worktrees/proj-test123/missing",
            "sessions": []
        }
        mock_database.get_active_worktrees_for_repository.return_value = [missing_worktree]

        result = manager.get_available_worktrees("proj-test123")

        assert len(result) == 0

    def test_get_available_worktrees_enriches_with_sessions(self, manager, mock_database, mock_settings, temp_dir):
        """Test enriches worktrees with session info"""
        # Create valid path
        valid_path = temp_dir / ".worktrees" / "proj-test123" / "feature"
        valid_path.mkdir(parents=True)

        worktree = {
            "id": "wt-feature",
            "worktree_path": ".worktrees/proj-test123/feature"
        }
        mock_database.get_active_worktrees_for_repository.return_value = [worktree]
        mock_database.get_sessions_for_worktree.return_value = [
            {"id": "ses-1", "status": "completed"},
            {"id": "ses-2", "status": WorktreeStatus.ACTIVE}
        ]

        result = manager.get_available_worktrees("proj-test123")

        assert len(result) == 1
        assert result[0]["session_count"] == 2
        assert result[0]["active_session"]["id"] == "ses-2"


# =============================================================================
# Get Worktrees For Project Tests
# =============================================================================

class TestGetWorktreesForProject:
    """Tests for get_worktrees_for_project method"""

    def test_get_worktrees_for_project_no_repo(self, manager, mock_database):
        """Test returns empty list when no repo"""
        mock_database.get_git_repository_by_project.return_value = None

        result = manager.get_worktrees_for_project("proj-test123")

        assert result == []

    def test_get_worktrees_for_project_checks_exists(self, manager, mock_database, mock_settings, temp_dir):
        """Test adds exists flag to worktrees"""
        # Create one valid path
        valid_path = temp_dir / ".worktrees" / "proj-test123" / "exists"
        valid_path.mkdir(parents=True)

        worktrees = [
            {"id": "wt-exists", "worktree_path": ".worktrees/proj-test123/exists", "sessions": []},
            {"id": "wt-missing", "worktree_path": ".worktrees/proj-test123/missing", "sessions": []}
        ]
        mock_database.get_worktrees_with_sessions.return_value = worktrees

        result = manager.get_worktrees_for_project("proj-test123")

        exists_wt = next(w for w in result if w["id"] == "wt-exists")
        missing_wt = next(w for w in result if w["id"] == "wt-missing")

        assert exists_wt["exists"] is True
        assert missing_wt["exists"] is False

    def test_get_worktrees_for_project_legacy_session_id(self, manager, mock_database, mock_settings, temp_dir):
        """Test handles legacy worktrees.session_id field"""
        worktree = {
            "id": "wt-legacy",
            "worktree_path": ".worktrees/proj-test123/legacy",
            "sessions": [],
            "session_id": "ses-legacy"
        }
        mock_database.get_worktrees_with_sessions.return_value = [worktree]
        mock_database.get_session.return_value = {
            "id": "ses-legacy",
            "title": "Legacy Session",
            "status": "active",
            "updated_at": "2024-01-01"
        }

        result = manager.get_worktrees_for_project("proj-test123")

        assert len(result[0]["sessions"]) == 1
        assert result[0]["sessions"][0]["id"] == "ses-legacy"


# =============================================================================
# Get Worktree Details Tests
# =============================================================================

class TestGetWorktreeDetails:
    """Tests for get_worktree_details method"""

    def test_get_worktree_details_not_found(self, manager, mock_database):
        """Test returns None when worktree not found"""
        mock_database.get_worktree.return_value = None

        result = manager.get_worktree_details("wt-nonexistent")

        assert result is None

    def test_get_worktree_details_with_git_status(self, manager, mock_database, mock_git_service, mock_settings, temp_dir):
        """Test includes git status when worktree exists"""
        # Create worktree path
        worktree_path = temp_dir / ".worktrees" / "proj-test123" / "feature"
        worktree_path.mkdir(parents=True)

        mock_database.get_worktree.return_value = {
            "id": "wt-feature",
            "worktree_path": ".worktrees/proj-test123/feature",
            "repository_id": "repo-test123"
        }
        mock_database.get_sessions_for_worktree.return_value = []

        result = manager.get_worktree_details("wt-feature")

        assert result["exists"] is True
        assert "git_status" in result
        mock_git_service.get_status.assert_called()

    def test_get_worktree_details_without_git_status(self, manager, mock_database, mock_git_service, mock_settings, temp_dir):
        """Test excludes git status when worktree doesn't exist"""
        mock_database.get_worktree.return_value = {
            "id": "wt-missing",
            "worktree_path": ".worktrees/proj-test123/missing",
            "repository_id": "repo-test123"
        }
        mock_database.get_sessions_for_worktree.return_value = []

        result = manager.get_worktree_details("wt-missing")

        assert result["exists"] is False
        assert "git_status" not in result

    def test_get_worktree_details_with_sessions(self, manager, mock_database, mock_settings, temp_dir):
        """Test includes session history"""
        mock_database.get_worktree.return_value = {
            "id": "wt-feature",
            "worktree_path": ".worktrees/proj-test123/feature",
            "repository_id": "repo-test123"
        }
        mock_database.get_sessions_for_worktree.return_value = [
            {"id": "ses-1", "title": "Session 1", "status": "completed", "updated_at": "2024-01-01"},
            {"id": "ses-2", "title": "Session 2", "status": WorktreeStatus.ACTIVE, "updated_at": "2024-01-02"}
        ]

        result = manager.get_worktree_details("wt-feature")

        assert result["session_count"] == 2
        assert result["active_session"]["id"] == "ses-2"


# =============================================================================
# Cleanup Worktree Tests
# =============================================================================

class TestCleanupWorktree:
    """Tests for cleanup_worktree method"""

    def test_cleanup_worktree_not_found(self, manager, mock_database):
        """Test returns False when worktree not found"""
        mock_database.get_worktree.return_value = None

        result = manager.cleanup_worktree("wt-nonexistent")

        assert result is False

    def test_cleanup_worktree_no_repo(self, manager, mock_database):
        """Test handles missing repository gracefully"""
        mock_database.get_worktree.return_value = {
            "id": "wt-orphan",
            "repository_id": "repo-missing"
        }
        mock_database.get_git_repository.return_value = None

        result = manager.cleanup_worktree("wt-orphan")

        # Should still succeed (just cleans up database record)
        assert result is True
        mock_database.delete_worktree.assert_called_with("wt-orphan")

    def test_cleanup_worktree_no_project(self, manager, mock_database):
        """Test handles missing project gracefully"""
        mock_database.get_worktree.return_value = {
            "id": "wt-orphan",
            "repository_id": "repo-test123"
        }
        mock_database.get_git_repository.return_value = {
            "id": "repo-test123",
            "project_id": "proj-missing"
        }
        mock_database.get_project.return_value = None

        result = manager.cleanup_worktree("wt-orphan")

        assert result is True
        mock_database.delete_worktree.assert_called_with("wt-orphan")

    def test_cleanup_worktree_success(self, manager, mock_database, mock_git_service, mock_settings, temp_dir):
        """Test successful worktree cleanup"""
        worktree_path = temp_dir / ".worktrees" / "proj-test123" / "feature"
        worktree_path.mkdir(parents=True)

        mock_database.get_worktree.return_value = {
            "id": "wt-feature",
            "repository_id": "repo-test123",
            "branch_name": "feature",
            "worktree_path": ".worktrees/proj-test123/feature"
        }

        result = manager.cleanup_worktree("wt-feature")

        assert result is True
        mock_git_service.remove_worktree.assert_called_once()
        mock_database.update_worktree.assert_called_with("wt-feature", status=WorktreeStatus.DELETED)
        mock_database.delete_worktree.assert_called_with("wt-feature")

    def test_cleanup_worktree_keep_branch(self, manager, mock_database, mock_git_service, mock_settings, temp_dir):
        """Test cleanup keeps branch by default"""
        mock_database.get_worktree.return_value = {
            "id": "wt-feature",
            "repository_id": "repo-test123",
            "branch_name": "feature",
            "worktree_path": ".worktrees/proj-test123/feature"
        }

        manager.cleanup_worktree("wt-feature", keep_branch=True)

        mock_git_service.delete_branch.assert_not_called()

    def test_cleanup_worktree_delete_branch(self, manager, mock_database, mock_git_service, mock_settings, temp_dir):
        """Test cleanup can delete branch"""
        mock_database.get_worktree.return_value = {
            "id": "wt-feature",
            "repository_id": "repo-test123",
            "branch_name": "feature",
            "worktree_path": ".worktrees/proj-test123/feature"
        }
        mock_database.get_git_repository.return_value = {
            "id": "repo-test123",
            "project_id": "proj-test123",
            "default_branch": "main"
        }

        manager.cleanup_worktree("wt-feature", keep_branch=False)

        # Verify delete_branch was called with correct branch name and force flag
        mock_git_service.delete_branch.assert_called_once()
        call_args = mock_git_service.delete_branch.call_args
        assert call_args.args[1] == "feature"  # branch_name
        assert call_args.kwargs["force"] is True

    def test_cleanup_worktree_protect_default_branch(self, manager, mock_database, mock_git_service, mock_settings, temp_dir):
        """Test cleanup doesn't delete default branch"""
        mock_database.get_worktree.return_value = {
            "id": "wt-main",
            "repository_id": "repo-test123",
            "branch_name": "main",  # Default branch
            "worktree_path": ".worktrees/proj-test123/main"
        }
        mock_database.get_git_repository.return_value = {
            "id": "repo-test123",
            "project_id": "proj-test123",
            "default_branch": "main"
        }

        manager.cleanup_worktree("wt-main", keep_branch=False)

        # Should not delete main branch
        mock_git_service.delete_branch.assert_not_called()

    def test_cleanup_worktree_handles_git_failure(self, manager, mock_database, mock_git_service, mock_settings, temp_dir):
        """Test cleanup continues even if git removal fails"""
        worktree_path = temp_dir / ".worktrees" / "proj-test123" / "feature"
        worktree_path.mkdir(parents=True)

        mock_database.get_worktree.return_value = {
            "id": "wt-feature",
            "repository_id": "repo-test123",
            "branch_name": "feature",
            "worktree_path": ".worktrees/proj-test123/feature"
        }
        mock_git_service.remove_worktree.return_value = False  # Git failure

        result = manager.cleanup_worktree("wt-feature")

        # Should still succeed (database cleanup happens)
        assert result is True
        mock_database.update_worktree.assert_called()
        mock_database.delete_worktree.assert_called()


# =============================================================================
# Get Worktree By Session Tests
# =============================================================================

class TestGetWorktreeBySession:
    """Tests for get_worktree_by_session method"""

    def test_get_worktree_by_session(self, manager, mock_database):
        """Test delegates to database function"""
        expected = {"id": "wt-123", "branch_name": "feature"}
        mock_database.get_worktree_by_session.return_value = expected

        result = manager.get_worktree_by_session("ses-123")

        assert result == expected
        mock_database.get_worktree_by_session.assert_called_with("ses-123")


# =============================================================================
# Singleton Instance Tests
# =============================================================================

class TestSingleton:
    """Tests for module-level singleton instance"""

    def test_singleton_exists(self):
        """Test worktree_manager singleton is available"""
        assert worktree_manager is not None
        assert isinstance(worktree_manager, WorktreeManager)


# =============================================================================
# Error Class Tests
# =============================================================================

class TestWorktreeError:
    """Tests for WorktreeError exception"""

    def test_worktree_error_message(self):
        """Test WorktreeError stores message"""
        error = WorktreeError("Custom error message")
        assert str(error) == "Custom error message"

    def test_worktree_error_inheritance(self):
        """Test WorktreeError inherits from Exception"""
        assert issubclass(WorktreeError, Exception)


# =============================================================================
# Edge Cases and Integration-like Tests
# =============================================================================

class TestEdgeCases:
    """Tests for edge cases and complex scenarios"""

    def test_create_worktree_with_remote_branch(self, manager, mock_database, mock_git_service, mock_settings, temp_dir):
        """Test creating worktree for a remote-only branch"""
        # Branch not in local branches
        mock_git_service.list_branches.side_effect = [
            [{"name": "main", "is_remote": False}],  # First call: local only
            [{"name": "main", "is_remote": False}, {"name": "origin/remote-feature", "is_remote": True}]  # Second call: with remote
        ]

        result = manager.create_worktree("proj-test123", "remote-feature")

        assert result is not None

    def test_concurrent_worktree_creation_race(self, manager, mock_database, mock_git_service, mock_settings, temp_dir):
        """Test handling of race condition in concurrent creation"""
        # First call to get_active_worktrees returns empty
        # But create_worktree fails due to unique constraint (another request won)
        mock_database.get_active_worktrees_for_repository.return_value = []
        mock_database.create_worktree.side_effect = Exception("unique constraint failed")
        mock_database.get_worktree_by_branch.return_value = {
            "id": "wt-winner",
            "branch_name": "develop",
            "status": WorktreeStatus.ACTIVE
        }

        result = manager.create_worktree("proj-test123", "develop")

        # Should return the worktree created by the other request
        assert result["id"] == "wt-winner"
        # Should have cleaned up the git worktree we created
        mock_git_service.remove_worktree.assert_called()

    def test_create_worktree_mkdir_creates_parents(self, manager, mock_database, mock_git_service, mock_settings, temp_dir):
        """Test worktree creation creates parent directories"""
        # Base directory doesn't exist yet
        base_dir = temp_dir / ".worktrees" / "proj-test123"
        assert not base_dir.exists()

        manager.create_worktree("proj-test123", "develop")

        # Parent directory should have been created
        assert base_dir.exists()

    def test_get_worktree_details_legacy_session(self, manager, mock_database, mock_settings, temp_dir):
        """Test get_worktree_details handles legacy session_id relationship"""
        mock_database.get_worktree.return_value = {
            "id": "wt-legacy",
            "worktree_path": ".worktrees/proj-test123/legacy",
            "repository_id": "repo-test123",
            "session_id": "ses-legacy"  # Legacy field
        }
        mock_database.get_sessions_for_worktree.return_value = []  # No sessions via new relationship
        mock_database.get_session.return_value = {
            "id": "ses-legacy",
            "title": "Legacy Session",
            "status": WorktreeStatus.ACTIVE,
            "updated_at": "2024-01-01"
        }

        result = manager.get_worktree_details("wt-legacy")

        # Should include legacy session
        assert result["session_count"] == 1
        assert result["sessions"][0]["id"] == "ses-legacy"
        assert result["active_session"]["id"] == "ses-legacy"


# =============================================================================
# Additional create_worktree_session Tests for Coverage
# =============================================================================

class TestCreateWorktreeSessionAdditional:
    """Additional tests for create_worktree_session to improve coverage"""

    def test_create_worktree_session_not_git_repo(self, manager, mock_database, mock_git_service, mock_settings, temp_dir):
        """Test error when project is not a git repository"""
        mock_git_service.is_git_repo.return_value = False
        mock_database.get_git_repository_by_project.return_value = None

        with pytest.raises(WorktreeError) as excinfo:
            manager.create_worktree_session("proj-test123", "main")

        assert "not a git repository" in str(excinfo.value)

    def test_create_worktree_session_cleans_stale_worktree(self, manager, mock_database, mock_git_service, mock_settings, temp_dir):
        """Test cleaning up stale worktree record in create_worktree_session"""
        stale_worktree = {
            "id": "wt-stale",
            "branch_name": "develop",
            "worktree_path": ".worktrees/proj-test123/develop-old"  # Path doesn't exist
        }
        mock_database.get_active_worktrees_for_repository.return_value = [stale_worktree]

        worktree, session = manager.create_worktree_session("proj-test123", "develop")

        # Should have cleaned up the stale record
        mock_database.update_worktree.assert_called_with("wt-stale", status=WorktreeStatus.DELETED)
        # And created a new worktree
        assert worktree is not None
        assert session is not None

    def test_create_worktree_session_branch_not_exists(self, manager, mock_database, mock_git_service, mock_settings, temp_dir):
        """Test error when branch does not exist in create_worktree_session"""
        mock_git_service.list_branches.return_value = [
            {"name": "main", "is_remote": False}
        ]

        with pytest.raises(WorktreeError) as excinfo:
            manager.create_worktree_session("proj-test123", "nonexistent-branch")

        assert "does not exist" in str(excinfo.value)

    def test_create_worktree_session_new_branch_already_exists(self, manager, mock_database, mock_git_service, mock_settings, temp_dir):
        """Test error when trying to create new branch that already exists"""
        mock_git_service.list_branches.return_value = [
            {"name": "main", "is_remote": False},
            {"name": "develop", "is_remote": False}
        ]

        with pytest.raises(WorktreeError) as excinfo:
            manager.create_worktree_session(
                "proj-test123",
                "develop",
                create_new_branch=True
            )

        assert "already exists" in str(excinfo.value)

    def test_create_worktree_session_path_already_exists(self, manager, mock_database, mock_git_service, mock_settings, temp_dir):
        """Test error when worktree path already exists on disk"""
        worktree_path = temp_dir / ".worktrees" / "proj-test123" / "develop"
        worktree_path.mkdir(parents=True)

        with pytest.raises(WorktreeError) as excinfo:
            manager.create_worktree_session("proj-test123", "develop")

        assert "already exists" in str(excinfo.value)

    def test_create_worktree_session_git_failure(self, manager, mock_database, mock_git_service, mock_settings, temp_dir):
        """Test error handling when git add_worktree fails"""
        mock_git_service.add_worktree.return_value = (False, "fatal: worktree already exists")

        with pytest.raises(WorktreeError) as excinfo:
            manager.create_worktree_session("proj-test123", "develop")

        assert "Git failed" in str(excinfo.value)

    def test_create_worktree_session_uses_first_profile(self, manager, mock_database, mock_git_service, mock_settings, temp_dir):
        """Test uses first available profile when no default"""
        mock_database.get_project.return_value = {
            "id": "proj-test123",
            "name": "Test Project",
            "path": "test-project",
            "settings": {}  # No default profile
        }
        mock_database.get_all_profiles.return_value = [
            {"id": "profile-first", "name": "First Profile"}
        ]

        worktree, session = manager.create_worktree_session("proj-test123", "develop")

        call_kwargs = mock_database.create_session.call_args
        assert call_kwargs.kwargs["profile_id"] == "profile-first"

    def test_create_worktree_session_concurrent_creation(self, manager, mock_database, mock_git_service, mock_settings, temp_dir):
        """Test handling of concurrent creation in create_worktree_session"""
        mock_database.create_worktree.side_effect = Exception("UNIQUE constraint failed")

        with pytest.raises(WorktreeError) as excinfo:
            manager.create_worktree_session("proj-test123", "develop")

        assert "already has an active worktree" in str(excinfo.value)
        # Git worktree should be removed
        mock_git_service.remove_worktree.assert_called()

    def test_create_worktree_session_with_new_branch(self, manager, mock_database, mock_git_service, mock_settings, temp_dir):
        """Test creating worktree session with new branch"""
        worktree, session = manager.create_worktree_session(
            project_id="proj-test123",
            branch_name="new-feature",
            create_new_branch=True,
            base_branch="main"
        )

        assert worktree is not None
        assert session is not None
        mock_git_service.add_worktree.assert_called_once()
        call_kwargs = mock_git_service.add_worktree.call_args
        assert call_kwargs.kwargs["new_branch"] is True
        assert call_kwargs.kwargs["base_branch"] == "main"


# =============================================================================
# Additional GitHub URL Parsing Tests
# =============================================================================

class TestGitHubUrlParsing:
    """Tests for GitHub URL parsing edge cases"""

    def test_ensure_repository_record_invalid_url_parsing(self, manager, mock_git_service, mock_database, mock_settings, temp_dir):
        """Test handling of malformed GitHub URLs"""
        mock_database.get_git_repository_by_project.return_value = None
        # URL that might cause parsing issues
        mock_git_service.get_remote_url.return_value = "git@github.com:"  # Missing repo path

        # Should not raise, just set github_repo_name to None or empty
        manager._ensure_repository_record("proj-123", "test-project")

        mock_database.create_git_repository.assert_called_once()
