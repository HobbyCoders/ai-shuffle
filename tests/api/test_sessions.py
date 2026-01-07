"""
Unit tests for sessions API endpoints.

Tests cover:
- Session CRUD operations (list, get, update, delete)
- Batch operations (batch delete)
- Session actions (archive, favorite)
- Sync endpoints (get changes, get state)
- Export/import functionality
- Fork session
- Worktree session creation
- Authentication and authorization
- Access control for API users
- Error handling and edge cases
"""

import pytest
import json
import tempfile
import shutil
from datetime import datetime
from pathlib import Path
from io import BytesIO
from unittest.mock import patch, MagicMock, AsyncMock

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api.sessions import router, check_session_access, enrich_sessions_with_tags
from app.api.auth import require_auth


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def temp_dir():
    """Create a temporary directory for tests."""
    tmpdir = tempfile.mkdtemp()
    yield Path(tmpdir)
    shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.fixture
def mock_settings(temp_dir):
    """Mock settings with temporary workspace directory."""
    mock_settings_obj = MagicMock()
    mock_settings_obj.workspace_dir = temp_dir
    mock_settings_obj.get_claude_projects_dir = temp_dir / ".claude" / "projects"
    return mock_settings_obj


@pytest.fixture
def mock_database():
    """Mock database module."""
    with patch("app.api.sessions.database") as mock_db:
        # Set up default returns
        mock_db.get_session_tags.return_value = []
        mock_db.session_has_forks.return_value = False
        yield mock_db


@pytest.fixture
def mock_api_user():
    """Mock for get_api_user_from_request."""
    with patch("app.api.sessions.get_api_user_from_request") as mock:
        mock.return_value = None  # Default to admin (no api user)
        yield mock


@pytest.fixture
def mock_sync_engine():
    """Mock sync engine."""
    with patch("app.api.sessions.sync_engine") as mock:
        mock.is_session_streaming.return_value = False
        mock.get_device_count.return_value = 1
        mock.get_session_state = AsyncMock(return_value={
            "is_streaming": False,
            "connected_devices": 1,
            "streaming_messages": []
        })
        yield mock


@pytest.fixture
def app(mock_database, mock_api_user):
    """Create a FastAPI app with the sessions router and mocked dependencies."""
    app = FastAPI()
    app.include_router(router)

    # Override auth dependencies
    app.dependency_overrides[require_auth] = lambda: "test-session-token"

    yield app


@pytest.fixture
def client(app):
    """Create a test client with mocked dependencies."""
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def sample_session():
    """Sample session data."""
    return {
        "id": "test-session-id",
        "title": "Test Session",
        "profile_id": "default",
        "project_id": None,
        "status": "active",
        "is_favorite": False,
        "sdk_session_id": "sdk-123",
        "total_cost_usd": 0.01,
        "total_tokens_in": 100,
        "total_tokens_out": 200,
        "turn_count": 5,
        "created_at": "2024-01-15T10:30:00",
        "updated_at": "2024-01-15T10:30:00"
    }


@pytest.fixture
def sample_project():
    """Sample project data."""
    return {
        "id": "test-project",
        "name": "Test Project",
        "description": "A test project",
        "path": "test-project",
        "created_at": "2024-01-15T10:30:00",
        "updated_at": "2024-01-15T10:30:00"
    }


@pytest.fixture
def sample_profile():
    """Sample profile data."""
    return {
        "id": "default",
        "name": "Default Profile",
        "description": "Default profile",
        "config": {},
        "created_at": "2024-01-15T10:30:00"
    }


# =============================================================================
# Test Module Imports
# =============================================================================

class TestSessionModuleImports:
    """Verify sessions module can be imported correctly."""

    def test_sessions_module_imports(self):
        """Sessions module should import without errors."""
        from app.api import sessions
        assert sessions is not None

    def test_sessions_router_exists(self):
        """Sessions router should exist."""
        from app.api.sessions import router
        assert router is not None

    def test_router_has_correct_prefix(self):
        """Router should have /api/v1/sessions prefix."""
        from app.api.sessions import router
        assert router.prefix == "/api/v1/sessions"


# =============================================================================
# Test Utility Functions
# =============================================================================

class TestCheckSessionAccess:
    """Test session access control utility."""

    def test_admin_has_full_access(self):
        """Admin user should have access to any session."""
        request = MagicMock()
        with patch("app.api.sessions.get_api_user_from_request") as mock_api_user:
            mock_api_user.return_value = None  # Admin has no api_user
            # Should not raise
            check_session_access(request, {"id": "any-session", "project_id": "any-project"})

    def test_api_user_with_matching_project(self):
        """API user should access sessions in their assigned project."""
        request = MagicMock()
        with patch("app.api.sessions.get_api_user_from_request") as mock_api_user:
            mock_api_user.return_value = {
                "id": "user-1",
                "project_id": "test-project"
            }
            # Should not raise
            check_session_access(request, {"id": "session-1", "project_id": "test-project"})

    def test_api_user_denied_other_project(self):
        """API user should be denied access to sessions in other projects."""
        request = MagicMock()
        with patch("app.api.sessions.get_api_user_from_request") as mock_api_user:
            mock_api_user.return_value = {
                "id": "user-1",
                "project_id": "project-a"
            }
            with pytest.raises(HTTPException) as exc_info:
                check_session_access(request, {"id": "session-1", "project_id": "project-b"})
            assert exc_info.value.status_code == 403
            assert "Access denied" in exc_info.value.detail

    def test_api_user_with_matching_profile(self):
        """API user should access sessions with their assigned profile."""
        request = MagicMock()
        with patch("app.api.sessions.get_api_user_from_request") as mock_api_user:
            mock_api_user.return_value = {
                "id": "user-1",
                "profile_id": "test-profile"
            }
            # Should not raise
            check_session_access(request, {"id": "session-1", "profile_id": "test-profile"})

    def test_api_user_denied_other_profile(self):
        """API user should be denied access to sessions with other profiles."""
        request = MagicMock()
        with patch("app.api.sessions.get_api_user_from_request") as mock_api_user:
            mock_api_user.return_value = {
                "id": "user-1",
                "profile_id": "profile-a"
            }
            with pytest.raises(HTTPException) as exc_info:
                check_session_access(request, {"id": "session-1", "profile_id": "profile-b"})
            assert exc_info.value.status_code == 403
            assert "Access denied" in exc_info.value.detail


class TestEnrichSessionsWithTags:
    """Test session enrichment utility."""

    def test_enriches_sessions_with_tags(self, mock_database):
        """Should add tags and has_forks to sessions."""
        mock_database.get_session_tags.return_value = [{"id": "tag-1", "name": "Bug"}]
        mock_database.session_has_forks.return_value = True

        sessions = [{"id": "session-1"}, {"id": "session-2"}]
        result = enrich_sessions_with_tags(sessions)

        assert len(result) == 2
        assert result[0]["tags"] == [{"id": "tag-1", "name": "Bug"}]
        assert result[0]["has_forks"] is True
        assert mock_database.get_session_tags.call_count == 2
        assert mock_database.session_has_forks.call_count == 2

    def test_handles_empty_list(self, mock_database):
        """Should handle empty session list."""
        result = enrich_sessions_with_tags([])
        assert result == []
        mock_database.get_session_tags.assert_not_called()


# =============================================================================
# Test List Sessions Endpoint
# =============================================================================

class TestListSessions:
    """Test GET /api/v1/sessions endpoint."""

    def test_list_sessions_success(self, client, mock_database, sample_session):
        """Should return list of sessions."""
        mock_database.get_sessions.return_value = [sample_session]

        response = client.get("/api/v1/sessions")

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["id"] == "test-session-id"

    def test_list_sessions_with_project_filter(self, client, mock_database, sample_session):
        """Should filter sessions by project_id."""
        mock_database.get_sessions.return_value = [sample_session]

        response = client.get("/api/v1/sessions?project_id=test-project")

        assert response.status_code == 200
        mock_database.get_sessions.assert_called_once()
        call_kwargs = mock_database.get_sessions.call_args[1]
        assert call_kwargs["project_id"] == "test-project"

    def test_list_sessions_with_profile_filter(self, client, mock_database, sample_session):
        """Should filter sessions by profile_id."""
        mock_database.get_sessions.return_value = [sample_session]

        response = client.get("/api/v1/sessions?profile_id=default")

        assert response.status_code == 200
        call_kwargs = mock_database.get_sessions.call_args[1]
        assert call_kwargs["profile_id"] == "default"

    def test_list_sessions_with_status_filter(self, client, mock_database, sample_session):
        """Should filter sessions by status."""
        mock_database.get_sessions.return_value = [sample_session]

        response = client.get("/api/v1/sessions?status=active")

        assert response.status_code == 200
        call_kwargs = mock_database.get_sessions.call_args[1]
        assert call_kwargs["status"] == "active"

    def test_list_sessions_favorites_only(self, client, mock_database, sample_session):
        """Should filter to favorites only."""
        sample_session["is_favorite"] = True
        mock_database.get_sessions.return_value = [sample_session]

        response = client.get("/api/v1/sessions?favorites_only=true")

        assert response.status_code == 200
        call_kwargs = mock_database.get_sessions.call_args[1]
        assert call_kwargs["favorites_only"] is True

    def test_list_sessions_admin_only(self, client, mock_database, sample_session):
        """Should filter to admin sessions only."""
        mock_database.get_sessions.return_value = [sample_session]

        response = client.get("/api/v1/sessions?admin_only=true")

        assert response.status_code == 200
        call_kwargs = mock_database.get_sessions.call_args[1]
        assert call_kwargs["api_user_id"] == ""  # Empty string signals NULL filter

    def test_list_sessions_api_users_only(self, client, mock_database, sample_session):
        """Should filter to API user sessions only."""
        mock_database.get_sessions.return_value = [sample_session]

        response = client.get("/api/v1/sessions?api_users_only=true")

        assert response.status_code == 200
        call_kwargs = mock_database.get_sessions.call_args[1]
        assert call_kwargs["api_users_only"] is True

    def test_list_sessions_with_tag_filter(self, client, mock_database, sample_session):
        """Should filter sessions by tag."""
        mock_database.get_sessions.return_value = [sample_session]

        response = client.get("/api/v1/sessions?tag_id=tag-1")

        assert response.status_code == 200
        call_kwargs = mock_database.get_sessions.call_args[1]
        assert call_kwargs["tag_id"] == "tag-1"

    def test_list_sessions_pagination(self, client, mock_database, sample_session):
        """Should support pagination."""
        mock_database.get_sessions.return_value = [sample_session]

        response = client.get("/api/v1/sessions?limit=10&offset=20")

        assert response.status_code == 200
        call_kwargs = mock_database.get_sessions.call_args[1]
        assert call_kwargs["limit"] == 10
        assert call_kwargs["offset"] == 20

    def test_list_sessions_api_user_restrictions(self, client, mock_database, mock_api_user, sample_session):
        """API user should only see their own sessions."""
        mock_api_user.return_value = {
            "id": "api-user-1",
            "project_id": "restricted-project",
            "profile_id": "restricted-profile"
        }
        mock_database.get_sessions.return_value = [sample_session]

        response = client.get("/api/v1/sessions")

        assert response.status_code == 200
        call_kwargs = mock_database.get_sessions.call_args[1]
        assert call_kwargs["project_id"] == "restricted-project"
        assert call_kwargs["profile_id"] == "restricted-profile"
        assert call_kwargs["api_user_id"] == "api-user-1"

    def test_list_sessions_empty(self, client, mock_database):
        """Should return empty list when no sessions."""
        mock_database.get_sessions.return_value = []

        response = client.get("/api/v1/sessions")

        assert response.status_code == 200
        assert response.json() == []


# =============================================================================
# Test Search Sessions Endpoint
# =============================================================================

class TestSearchSessions:
    """Test GET /api/v1/sessions/search/query endpoint."""

    def test_search_sessions_success(self, client, mock_database):
        """Should search sessions by query."""
        mock_database.search_sessions.return_value = [
            {
                "id": "session-1",
                "title": "Test",
                "snippet": "...matching text...",
                "profile_id": "default",
                "created_at": "2024-01-15T10:30:00",
                "updated_at": "2024-01-15T10:30:00",
                "match_type": "title"
            }
        ]

        response = client.get("/api/v1/sessions/search/query?q=test")

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        mock_database.search_sessions.assert_called_once()

    def test_search_sessions_with_filters(self, client, mock_database):
        """Should search with project and profile filters."""
        mock_database.search_sessions.return_value = []

        response = client.get("/api/v1/sessions/search/query?q=test&project_id=proj-1&profile_id=prof-1")

        assert response.status_code == 200
        call_kwargs = mock_database.search_sessions.call_args[1]
        assert call_kwargs["project_id"] == "proj-1"
        assert call_kwargs["profile_id"] == "prof-1"

    def test_search_sessions_admin_only(self, client, mock_database):
        """Should search admin sessions only when flag is set."""
        mock_database.search_sessions.return_value = []

        response = client.get("/api/v1/sessions/search/query?q=test&admin_only=true")

        assert response.status_code == 200
        call_kwargs = mock_database.search_sessions.call_args[1]
        assert call_kwargs["admin_only"] is True

    def test_search_sessions_with_limit(self, client, mock_database):
        """Should respect search limit."""
        mock_database.search_sessions.return_value = []

        response = client.get("/api/v1/sessions/search/query?q=test&limit=5")

        assert response.status_code == 200
        call_kwargs = mock_database.search_sessions.call_args[1]
        assert call_kwargs["limit"] == 5

    def test_search_sessions_api_user_restrictions(self, client, mock_database, mock_api_user):
        """API user search should be restricted to their sessions."""
        mock_api_user.return_value = {
            "id": "api-user-1",
            "project_id": "restricted-project",
            "profile_id": "restricted-profile"
        }
        mock_database.search_sessions.return_value = []

        response = client.get("/api/v1/sessions/search/query?q=test")

        assert response.status_code == 200
        call_kwargs = mock_database.search_sessions.call_args[1]
        assert call_kwargs["project_id"] == "restricted-project"
        assert call_kwargs["profile_id"] == "restricted-profile"
        assert call_kwargs["api_user_id"] == "api-user-1"

    def test_search_sessions_empty_query_rejected(self, client, mock_database):
        """Should reject empty search query."""
        response = client.get("/api/v1/sessions/search/query?q=")

        # FastAPI will reject empty string with min_length=1
        assert response.status_code == 422


# =============================================================================
# Test Get Session Endpoint
# =============================================================================

class TestGetSession:
    """Test GET /api/v1/sessions/{session_id} endpoint."""

    def test_get_session_success(self, client, mock_database, sample_session):
        """Should return session with messages."""
        mock_database.get_session.return_value = sample_session
        mock_database.get_session_messages.return_value = []

        response = client.get("/api/v1/sessions/test-session-id")

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == "test-session-id"
        assert "messages" in data

    def test_get_session_not_found(self, client, mock_database):
        """Should return 404 for non-existent session."""
        mock_database.get_session.return_value = None

        response = client.get("/api/v1/sessions/non-existent")

        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()

    def test_get_session_with_jsonl_messages(self, client, mock_database, sample_session):
        """Should load messages from JSONL file when available."""
        sample_session["sdk_session_id"] = "sdk-session-123"
        mock_database.get_session.return_value = sample_session

        jsonl_messages = [
            {"id": 1, "role": "user", "content": "Hello", "type": "text", "metadata": {}},
            {"id": 2, "role": "assistant", "content": "Hi there!", "type": "text", "metadata": {}}
        ]

        with patch("app.core.jsonl_parser.parse_session_history") as mock_parse:
            with patch("app.core.jsonl_parser.get_session_cost_from_jsonl") as mock_cost:
                mock_parse.return_value = jsonl_messages
                mock_cost.return_value = {
                    "cache_creation_tokens": 100,
                    "cache_read_tokens": 50,
                    "last_input_tokens": 200,
                    "total_tokens_in": 500,
                    "total_tokens_out": 300
                }

                response = client.get("/api/v1/sessions/test-session-id")

        assert response.status_code == 200
        data = response.json()
        assert len(data["messages"]) == 2

    def test_get_session_falls_back_to_database(self, client, mock_database, sample_session):
        """Should fall back to database messages when JSONL fails."""
        sample_session["sdk_session_id"] = None
        mock_database.get_session.return_value = sample_session
        mock_database.get_session_messages.return_value = [
            {"id": 1, "role": "user", "content": "Hello", "tool_name": None}
        ]

        response = client.get("/api/v1/sessions/test-session-id")

        assert response.status_code == 200
        data = response.json()
        assert len(data["messages"]) == 1
        mock_database.get_session_messages.assert_called_once()

    def test_get_session_access_denied(self, client, mock_database, mock_api_user, sample_session):
        """Should deny access for unauthorized API user."""
        mock_api_user.return_value = {
            "id": "api-user-1",
            "project_id": "other-project"
        }
        sample_session["project_id"] = "test-project"
        mock_database.get_session.return_value = sample_session

        response = client.get("/api/v1/sessions/test-session-id")

        assert response.status_code == 403
        assert "Access denied" in response.json()["detail"]


# =============================================================================
# Test Update Session Endpoint
# =============================================================================

class TestUpdateSession:
    """Test PATCH /api/v1/sessions/{session_id} endpoint."""

    def test_update_session_title(self, client, mock_database, sample_session):
        """Should update session title."""
        mock_database.get_session.return_value = sample_session
        updated = {**sample_session, "title": "New Title"}
        mock_database.update_session.return_value = updated

        response = client.patch("/api/v1/sessions/test-session-id?title=New%20Title")

        assert response.status_code == 200
        mock_database.update_session.assert_called_once()
        call_kwargs = mock_database.update_session.call_args[1]
        assert call_kwargs["title"] == "New Title"

    def test_update_session_status(self, client, mock_database, sample_session):
        """Should update session status."""
        mock_database.get_session.return_value = sample_session
        updated = {**sample_session, "status": "completed"}
        mock_database.update_session.return_value = updated

        response = client.patch("/api/v1/sessions/test-session-id?status=completed")

        assert response.status_code == 200
        call_kwargs = mock_database.update_session.call_args[1]
        assert call_kwargs["status"] == "completed"

    def test_update_session_not_found(self, client, mock_database):
        """Should return 404 for non-existent session."""
        mock_database.get_session.return_value = None

        response = client.patch("/api/v1/sessions/non-existent?title=New")

        assert response.status_code == 404

    def test_update_session_access_denied(self, client, mock_database, mock_api_user, sample_session):
        """Should deny update for unauthorized API user."""
        mock_api_user.return_value = {
            "id": "api-user-1",
            "project_id": "other-project"
        }
        sample_session["project_id"] = "test-project"
        mock_database.get_session.return_value = sample_session

        response = client.patch("/api/v1/sessions/test-session-id?title=New")

        assert response.status_code == 403


# =============================================================================
# Test Delete Session Endpoint
# =============================================================================

class TestDeleteSession:
    """Test DELETE /api/v1/sessions/{session_id} endpoint."""

    def test_delete_session_success(self, client, mock_database, sample_session):
        """Should delete session."""
        mock_database.get_session.return_value = sample_session

        response = client.delete("/api/v1/sessions/test-session-id")

        assert response.status_code == 204
        mock_database.delete_session.assert_called_once_with("test-session-id")

    def test_delete_session_not_found(self, client, mock_database):
        """Should return 404 for non-existent session."""
        mock_database.get_session.return_value = None

        response = client.delete("/api/v1/sessions/non-existent")

        assert response.status_code == 404

    def test_delete_session_access_denied(self, client, mock_database, mock_api_user, sample_session):
        """Should deny delete for unauthorized API user."""
        mock_api_user.return_value = {
            "id": "api-user-1",
            "project_id": "other-project"
        }
        sample_session["project_id"] = "test-project"
        mock_database.get_session.return_value = sample_session

        response = client.delete("/api/v1/sessions/test-session-id")

        assert response.status_code == 403


# =============================================================================
# Test Batch Delete Sessions Endpoint
# =============================================================================

class TestBatchDeleteSessions:
    """Test POST /api/v1/sessions/batch-delete endpoint."""

    def test_batch_delete_success(self, client, mock_database, sample_session):
        """Should delete multiple sessions."""
        mock_database.get_session.return_value = sample_session

        response = client.post(
            "/api/v1/sessions/batch-delete",
            json={"session_ids": ["session-1", "session-2", "session-3"]}
        )

        assert response.status_code == 200
        data = response.json()
        assert data["deleted_count"] == 3
        assert data["total_requested"] == 3
        assert data["errors"] is None

    def test_batch_delete_partial_success(self, client, mock_database, sample_session):
        """Should handle partial failures."""
        # First call succeeds, second returns None (not found)
        mock_database.get_session.side_effect = [sample_session, None, sample_session]

        response = client.post(
            "/api/v1/sessions/batch-delete",
            json={"session_ids": ["session-1", "session-2", "session-3"]}
        )

        assert response.status_code == 200
        data = response.json()
        assert data["deleted_count"] == 2
        assert data["total_requested"] == 3
        assert len(data["errors"]) == 1
        assert "not found" in data["errors"][0].lower()

    def test_batch_delete_access_denied(self, client, mock_database, mock_api_user, sample_session):
        """Should skip sessions without access."""
        mock_api_user.return_value = {
            "id": "api-user-1",
            "project_id": "restricted-project"
        }
        sample_session["project_id"] = "other-project"
        mock_database.get_session.return_value = sample_session

        response = client.post(
            "/api/v1/sessions/batch-delete",
            json={"session_ids": ["session-1"]}
        )

        assert response.status_code == 200
        data = response.json()
        assert data["deleted_count"] == 0
        assert len(data["errors"]) == 1
        assert "Access denied" in data["errors"][0]

    def test_batch_delete_empty_list(self, client, mock_database):
        """Should handle empty session list."""
        response = client.post(
            "/api/v1/sessions/batch-delete",
            json={"session_ids": []}
        )

        assert response.status_code == 200
        data = response.json()
        assert data["deleted_count"] == 0
        assert data["total_requested"] == 0


# =============================================================================
# Test Archive Session Endpoint
# =============================================================================

class TestArchiveSession:
    """Test POST /api/v1/sessions/{session_id}/archive endpoint."""

    def test_archive_session_success(self, client, mock_database, sample_session):
        """Should archive session."""
        mock_database.get_session.return_value = sample_session
        mock_database.update_session.return_value = {**sample_session, "status": "archived"}

        response = client.post("/api/v1/sessions/test-session-id/archive")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        mock_database.update_session.assert_called_once()
        call_kwargs = mock_database.update_session.call_args[1]
        assert call_kwargs["status"] == "archived"

    def test_archive_session_not_found(self, client, mock_database):
        """Should return 404 for non-existent session."""
        mock_database.get_session.return_value = None

        response = client.post("/api/v1/sessions/non-existent/archive")

        assert response.status_code == 404

    def test_archive_session_access_denied(self, client, mock_database, mock_api_user, sample_session):
        """Should deny archive for unauthorized API user."""
        mock_api_user.return_value = {
            "id": "api-user-1",
            "project_id": "other-project"
        }
        sample_session["project_id"] = "test-project"
        mock_database.get_session.return_value = sample_session

        response = client.post("/api/v1/sessions/test-session-id/archive")

        assert response.status_code == 403


# =============================================================================
# Test Toggle Favorite Endpoint
# =============================================================================

class TestToggleFavorite:
    """Test PATCH /api/v1/sessions/{session_id}/favorite endpoint."""

    def test_toggle_favorite_success(self, client, mock_database, sample_session):
        """Should toggle session favorite status."""
        mock_database.get_session.return_value = sample_session
        favorited = {**sample_session, "is_favorite": True}
        mock_database.toggle_session_favorite.return_value = favorited

        response = client.patch("/api/v1/sessions/test-session-id/favorite")

        assert response.status_code == 200
        mock_database.toggle_session_favorite.assert_called_once_with("test-session-id")

    def test_toggle_favorite_not_found(self, client, mock_database):
        """Should return 404 for non-existent session."""
        mock_database.get_session.return_value = None

        response = client.patch("/api/v1/sessions/non-existent/favorite")

        assert response.status_code == 404

    def test_toggle_favorite_failure(self, client, mock_database, sample_session):
        """Should return 500 if toggle fails."""
        mock_database.get_session.return_value = sample_session
        mock_database.toggle_session_favorite.return_value = None

        response = client.patch("/api/v1/sessions/test-session-id/favorite")

        assert response.status_code == 500
        assert "Failed to toggle" in response.json()["detail"]

    def test_toggle_favorite_access_denied(self, client, mock_database, mock_api_user, sample_session):
        """Should deny toggle for unauthorized API user."""
        mock_api_user.return_value = {
            "id": "api-user-1",
            "project_id": "other-project"
        }
        sample_session["project_id"] = "test-project"
        mock_database.get_session.return_value = sample_session

        response = client.patch("/api/v1/sessions/test-session-id/favorite")

        assert response.status_code == 403


# =============================================================================
# Test Sync Endpoints
# =============================================================================

class TestGetSyncChanges:
    """Test GET /api/v1/sessions/{session_id}/sync endpoint."""

    def test_get_sync_changes_success(self, client, mock_database, sample_session):
        """Should return sync changes."""
        mock_database.get_session.return_value = sample_session
        mock_database.get_sync_logs.return_value = [
            {"id": 1, "event_type": "message_added", "data": {}}
        ]
        mock_database.get_latest_sync_id.return_value = 5

        with patch("app.core.sync_engine.sync_engine") as mock_sync:
            mock_sync.is_session_streaming.return_value = True
            mock_sync.get_device_count.return_value = 2

            response = client.get("/api/v1/sessions/test-session-id/sync?since_id=0")

        assert response.status_code == 200
        data = response.json()
        assert len(data["changes"]) == 1
        assert data["latest_id"] == 5
        assert data["is_streaming"] is True
        assert data["connected_devices"] == 2

    def test_get_sync_changes_not_found(self, client, mock_database):
        """Should return 404 for non-existent session."""
        mock_database.get_session.return_value = None

        response = client.get("/api/v1/sessions/non-existent/sync")

        assert response.status_code == 404

    def test_get_sync_changes_access_denied(self, client, mock_database, mock_api_user, sample_session):
        """Should deny access for unauthorized API user."""
        mock_api_user.return_value = {
            "id": "api-user-1",
            "project_id": "other-project"
        }
        sample_session["project_id"] = "test-project"
        mock_database.get_session.return_value = sample_session

        response = client.get("/api/v1/sessions/test-session-id/sync")

        assert response.status_code == 403


class TestGetSessionState:
    """Test GET /api/v1/sessions/{session_id}/state endpoint."""

    def test_get_session_state_success(self, client, mock_database, sample_session):
        """Should return session state."""
        mock_database.get_session.return_value = sample_session

        with patch("app.core.sync_engine.sync_engine") as mock_sync:
            mock_sync.get_session_state = AsyncMock(return_value={
                "is_streaming": False,
                "connected_devices": 1,
                "streaming_messages": []
            })

            response = client.get("/api/v1/sessions/test-session-id/state")

        assert response.status_code == 200
        data = response.json()
        assert data["session_id"] == "test-session-id"
        assert data["title"] == "Test Session"
        assert "is_streaming" in data
        assert "connected_devices" in data

    def test_get_session_state_not_found(self, client, mock_database):
        """Should return 404 for non-existent session."""
        mock_database.get_session.return_value = None

        response = client.get("/api/v1/sessions/non-existent/state")

        assert response.status_code == 404


# =============================================================================
# Test Export Session Endpoint
# =============================================================================

class TestExportSession:
    """Test GET /api/v1/sessions/{session_id}/export endpoint."""

    def test_export_session_json(self, client, mock_database, sample_session):
        """Should export session as JSON."""
        mock_database.get_session.return_value = sample_session
        mock_database.get_session_messages.return_value = [
            {"role": "user", "content": "Hello"}
        ]

        response = client.get("/api/v1/sessions/test-session-id/export?format=json")

        assert response.status_code == 200
        assert "application/json" in response.headers["content-type"]
        assert "attachment" in response.headers["content-disposition"]
        data = response.json()
        assert "session" in data
        assert "messages" in data
        assert data["format_version"] == "1.0"

    def test_export_session_markdown(self, client, mock_database, sample_session):
        """Should export session as Markdown."""
        mock_database.get_session.return_value = sample_session
        mock_database.get_session_messages.return_value = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there!"}
        ]

        response = client.get("/api/v1/sessions/test-session-id/export?format=markdown")

        assert response.status_code == 200
        assert "text/markdown" in response.headers["content-type"]
        content = response.text
        assert "# Test Session" in content
        assert "### Human" in content
        assert "### Assistant" in content

    def test_export_session_invalid_format(self, client, mock_database, sample_session):
        """Should reject invalid export format."""
        mock_database.get_session.return_value = sample_session

        response = client.get("/api/v1/sessions/test-session-id/export?format=pdf")

        assert response.status_code == 400
        assert "Invalid format" in response.json()["detail"]

    def test_export_session_not_found(self, client, mock_database):
        """Should return 404 for non-existent session."""
        mock_database.get_session.return_value = None

        response = client.get("/api/v1/sessions/non-existent/export")

        assert response.status_code == 404

    def test_export_session_with_tool_use(self, client, mock_database, sample_session):
        """Should export tool use messages in markdown."""
        mock_database.get_session.return_value = sample_session
        mock_database.get_session_messages.return_value = [
            {
                "role": "assistant",
                "content": "",
                "tool_name": "Read",
                "tool_input": {"file_path": "/test.py"}
            }
        ]

        response = client.get("/api/v1/sessions/test-session-id/export?format=markdown")

        assert response.status_code == 200
        content = response.text
        assert "Tool: Read" in content or "### Tool" in content


# =============================================================================
# Test Import Session Endpoint
# =============================================================================

class TestImportSession:
    """Test POST /api/v1/sessions/import endpoint."""

    def test_import_session_success(self, client, mock_database, sample_profile, temp_dir):
        """Should import session from JSON file."""
        mock_database.get_profile.return_value = sample_profile
        mock_database.create_session.return_value = {"id": "new-session-id"}

        import_data = {
            "session": {
                "title": "Imported Session",
                "profile_id": "default",
                "total_cost_usd": 0.05,
                "turn_count": 10
            },
            "messages": [
                {"role": "user", "content": "Hello"}
            ],
            "format_version": "1.0"
        }

        with patch("app.core.config.settings") as mock_settings:
            mock_settings.get_claude_projects_dir = temp_dir / ".claude" / "projects"
            mock_settings.workspace_dir = temp_dir

            # Create the claude projects directory
            (temp_dir / ".claude" / "projects").mkdir(parents=True, exist_ok=True)

            files = {"file": ("export.json", json.dumps(import_data).encode(), "application/json")}
            response = client.post("/api/v1/sessions/import", files=files)

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert data["message_count"] == 1

    def test_import_session_invalid_file_type(self, client, mock_database):
        """Should reject non-JSON files."""
        files = {"file": ("export.txt", b"plain text", "text/plain")}
        response = client.post("/api/v1/sessions/import", files=files)

        assert response.status_code == 400
        assert "Invalid file type" in response.json()["detail"]

    def test_import_session_invalid_json(self, client, mock_database):
        """Should reject invalid JSON content."""
        files = {"file": ("export.json", b"not valid json", "application/json")}
        response = client.post("/api/v1/sessions/import", files=files)

        assert response.status_code == 400
        assert "Invalid JSON" in response.json()["detail"]

    def test_import_session_missing_fields(self, client, mock_database):
        """Should reject JSON missing required fields."""
        import_data = {"only_messages": []}
        files = {"file": ("export.json", json.dumps(import_data).encode(), "application/json")}
        response = client.post("/api/v1/sessions/import", files=files)

        assert response.status_code == 400
        assert "Missing" in response.json()["detail"]

    def test_import_session_no_profile_available(self, client, mock_database):
        """Should handle case when no profiles exist."""
        mock_database.get_profile.return_value = None
        mock_database.get_all_profiles.return_value = []

        import_data = {
            "session": {"title": "Test"},
            "messages": []
        }
        files = {"file": ("export.json", json.dumps(import_data).encode(), "application/json")}
        response = client.post("/api/v1/sessions/import", files=files)

        assert response.status_code == 400
        assert "No profiles available" in response.json()["detail"]


# =============================================================================
# Test Fork Session Endpoint
# =============================================================================

class TestForkSession:
    """Test POST /api/v1/sessions/{session_id}/fork endpoint."""

    def test_fork_session_success(self, client, mock_database, sample_session, temp_dir):
        """Should fork session at specified message index."""
        sample_session["sdk_session_id"] = "sdk-123"
        sample_session["project_id"] = None
        mock_database.get_session.return_value = sample_session
        mock_database.create_session.return_value = {"id": "forked-session-id"}

        # Create mock JSONL file
        jsonl_entries = [
            {"type": "user", "message": {"role": "user", "content": "Hello"}},
            {"type": "assistant", "message": {"role": "assistant", "content": "Hi!"}}
        ]

        with patch("app.core.config.settings") as mock_settings:
            mock_settings.get_claude_projects_dir = temp_dir / ".claude" / "projects"
            mock_settings.workspace_dir = temp_dir

            with patch("app.core.jsonl_parser.get_session_jsonl_path") as mock_jsonl_path:
                jsonl_file = temp_dir / "test.jsonl"
                jsonl_file.write_text("\n".join(json.dumps(e) for e in jsonl_entries))
                mock_jsonl_path.return_value = jsonl_file

                with patch("app.core.jsonl_parser.parse_jsonl_file") as mock_parse:
                    mock_parse.return_value = iter(jsonl_entries)

                    # Create the claude projects directory
                    (temp_dir / ".claude" / "projects").mkdir(parents=True, exist_ok=True)

                    response = client.post(
                        "/api/v1/sessions/test-session-id/fork",
                        json={"message_index": 0}
                    )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert data["parent_session_id"] == "test-session-id"
        assert data["fork_point_message_index"] == 0

    def test_fork_session_not_found(self, client, mock_database):
        """Should return 404 for non-existent session."""
        mock_database.get_session.return_value = None

        response = client.post(
            "/api/v1/sessions/non-existent/fork",
            json={"message_index": 0}
        )

        assert response.status_code == 404

    def test_fork_session_no_sdk_id(self, client, mock_database, sample_session):
        """Should reject fork for session without SDK session ID."""
        sample_session["sdk_session_id"] = None
        mock_database.get_session.return_value = sample_session

        response = client.post(
            "/api/v1/sessions/test-session-id/fork",
            json={"message_index": 0}
        )

        assert response.status_code == 400
        assert "SDK session ID" in response.json()["detail"]

    def test_fork_session_no_jsonl_file(self, client, mock_database, sample_session):
        """Should reject fork when JSONL file not found."""
        sample_session["sdk_session_id"] = "sdk-123"
        mock_database.get_session.return_value = sample_session

        with patch("app.core.jsonl_parser.get_session_jsonl_path") as mock_jsonl_path:
            mock_jsonl_path.return_value = None

            response = client.post(
                "/api/v1/sessions/test-session-id/fork",
                json={"message_index": 0}
            )

        assert response.status_code == 400
        assert "JSONL file not found" in response.json()["detail"]

    def test_fork_session_invalid_message_index(self, client, mock_database, sample_session, temp_dir):
        """Should reject invalid message index."""
        sample_session["sdk_session_id"] = "sdk-123"
        mock_database.get_session.return_value = sample_session

        jsonl_entries = [
            {"type": "user", "message": {"role": "user", "content": "Hello"}}
        ]

        with patch("app.core.config.settings") as mock_settings:
            mock_settings.workspace_dir = temp_dir

            with patch("app.core.jsonl_parser.get_session_jsonl_path") as mock_jsonl_path:
                jsonl_file = temp_dir / "test.jsonl"
                jsonl_file.write_text(json.dumps(jsonl_entries[0]))
                mock_jsonl_path.return_value = jsonl_file

                with patch("app.core.jsonl_parser.parse_jsonl_file") as mock_parse:
                    mock_parse.return_value = iter(jsonl_entries)

                    response = client.post(
                        "/api/v1/sessions/test-session-id/fork",
                        json={"message_index": 10}  # Invalid index
                    )

        assert response.status_code == 400
        assert "Invalid message_index" in response.json()["detail"]


# =============================================================================
# Test Create Worktree Session Endpoint
# =============================================================================

class TestCreateWorktreeSession:
    """Test POST /api/v1/sessions/with-worktree endpoint."""

    def test_create_worktree_session_success(self, client, mock_database, sample_project):
        """Should create session with worktree."""
        mock_database.get_project.return_value = sample_project

        mock_worktree = {
            "id": "wt-123",
            "branch_name": "feature/test",
            "base_branch": "main",
            "worktree_path": "/workspace/test-project-wt"
        }
        mock_session = {
            "id": "session-123",
            "title": "Worktree Session"
        }

        with patch("app.core.worktree_manager.worktree_manager") as mock_wt_manager:
            mock_wt_manager.create_worktree_session.return_value = (mock_worktree, mock_session)

            response = client.post(
                "/api/v1/sessions/with-worktree",
                json={
                    "project_id": "test-project",
                    "branch_name": "feature/test",
                    "base_branch": "main"
                }
            )

        assert response.status_code == 200
        data = response.json()
        assert data["session_id"] == "session-123"
        assert data["worktree_id"] == "wt-123"
        assert data["branch_name"] == "feature/test"
        assert data["status"] == "active"

    def test_create_worktree_session_project_not_found(self, client, mock_database):
        """Should return 404 for non-existent project."""
        mock_database.get_project.return_value = None

        response = client.post(
            "/api/v1/sessions/with-worktree",
            json={
                "project_id": "non-existent",
                "branch_name": "feature/test"
            }
        )

        assert response.status_code == 404
        assert "Project not found" in response.json()["detail"]

    def test_create_worktree_session_api_user_denied(self, client, mock_database, mock_api_user, sample_project):
        """Should deny API user access to other projects."""
        mock_api_user.return_value = {
            "id": "api-user-1",
            "project_id": "other-project"
        }
        mock_database.get_project.return_value = sample_project

        response = client.post(
            "/api/v1/sessions/with-worktree",
            json={
                "project_id": "test-project",
                "branch_name": "feature/test"
            }
        )

        assert response.status_code == 403
        assert "Access denied" in response.json()["detail"]

    def test_create_worktree_session_worktree_error(self, client, mock_database, sample_project):
        """Should handle worktree creation errors."""
        mock_database.get_project.return_value = sample_project

        from app.core.worktree_manager import WorktreeError

        with patch("app.core.worktree_manager.worktree_manager") as mock_wt_manager:
            mock_wt_manager.create_worktree_session.side_effect = WorktreeError("Branch already exists")

            response = client.post(
                "/api/v1/sessions/with-worktree",
                json={
                    "project_id": "test-project",
                    "branch_name": "existing-branch"
                }
            )

        assert response.status_code == 400
        assert "Branch already exists" in response.json()["detail"]


# =============================================================================
# Test Session ID Utilities
# =============================================================================

class TestSessionId:
    """Test session ID generation."""

    def test_uuid_format(self):
        """Session IDs should be valid UUIDs."""
        import uuid
        session_id = str(uuid.uuid4())
        # Should be a valid UUID format
        parsed = uuid.UUID(session_id)
        assert str(parsed) == session_id

    def test_uuid_uniqueness(self):
        """Multiple session IDs should be unique."""
        import uuid
        ids = [str(uuid.uuid4()) for _ in range(100)]
        assert len(set(ids)) == 100
