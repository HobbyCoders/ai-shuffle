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
import asyncio
import json
import tempfile
import shutil
from pathlib import Path
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
    """Mock database module.

    The sessions router is split across several modules (`sessions`,
    `sessions_helpers`, `sessions_export`, `sessions_fork`,
    `sessions_worktree`), each with its own `database`
    import. Patch all of them to the same mock so helpers like
    `require_session` and the fork/worktree endpoints resolve through the
    mock instead of a real pool.
    """
    with patch("app.api.sessions.database") as mock_db, \
            patch("app.api.sessions_helpers.database", mock_db), \
            patch("app.api.sessions_export.database", mock_db), \
            patch("app.api.sessions_fork.database", mock_db), \
            patch("app.api.sessions_worktree.database", mock_db):
        # Set up default returns
        mock_db.get_session_tags.return_value = []
        mock_db.session_has_forks.return_value = False
        # Cross-install enrichment (sessions_helpers._annotate_*): return real
        # containers so the endpoint's ``.get(...)`` chains resolve to plain
        # values instead of MagicMocks, which would fail response validation
        # (e.g. device_name must be a string or None, not a MagicMock).
        mock_db.get_installs_map.return_value = {}
        mock_db.get_project.return_value = None
        # Context-usage persisted-snapshot fallback: default to no snapshot so
        # the not-connected/failed-live paths keep returning available=False
        # unless a test explicitly populates the column.
        mock_db.get_session_context_usage_snapshot.return_value = None
        yield mock_db


@pytest.fixture
def mock_api_user():
    """Mock for get_api_user_from_request.

    Each sessions sub-module imports its own copy of the name, and
    check_session_access reads it from sessions_helpers. Patch every module
    to the same mock so one fixture governs access across all endpoints.
    """
    with patch("app.api.sessions.get_api_user_from_request") as mock, \
            patch("app.api.sessions_helpers.get_api_user_from_request", new=mock), \
            patch("app.api.sessions_fork.get_api_user_from_request", new=mock), \
            patch("app.api.sessions_worktree.get_api_user_from_request", new=mock), \
            patch("app.api.sessions_export.get_api_user_from_request", new=mock):
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
        with patch("app.api.sessions_helpers.get_api_user_from_request") as mock_api_user:
            mock_api_user.return_value = None  # Admin has no api_user
            # Should not raise
            check_session_access(request, {"id": "any-session", "project_id": "any-project"})

    def test_api_user_with_matching_project(self):
        """API user should access sessions in their assigned project."""
        request = MagicMock()
        with patch("app.api.sessions_helpers.get_api_user_from_request") as mock_api_user:
            mock_api_user.return_value = {
                "id": "user-1",
                "project_id": "test-project"
            }
            # Should not raise
            check_session_access(request, {"id": "session-1", "project_id": "test-project"})

    def test_api_user_denied_other_project(self):
        """API user should be denied access to sessions in other projects."""
        request = MagicMock()
        with patch("app.api.sessions_helpers.get_api_user_from_request") as mock_api_user:
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
        with patch("app.api.sessions_helpers.get_api_user_from_request") as mock_api_user:
            mock_api_user.return_value = {
                "id": "user-1",
                "profile_id": "test-profile"
            }
            # Should not raise
            check_session_access(request, {"id": "session-1", "profile_id": "test-profile"})

    def test_api_user_denied_other_profile(self):
        """API user should be denied access to sessions with other profiles."""
        request = MagicMock()
        with patch("app.api.sessions_helpers.get_api_user_from_request") as mock_api_user:
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
        """Should add tags and has_forks to sessions via batch lookups.

        Enrichment now uses batch queries (get_tags_for_sessions /
        get_fork_info_for_sessions — 2 queries total) instead of per-session
        round-trips, so mock those helpers rather than the old per-session
        database.get_session_tags / session_has_forks calls.
        """
        tags_by_session = {
            "session-1": [{"id": "tag-1", "name": "Bug"}],
            "session-2": [{"id": "tag-1", "name": "Bug"}],
        }
        fork_by_session = {
            "session-1": {"has_forks": True},
            "session-2": {"has_forks": True},
        }
        with patch("app.api.sessions_helpers.get_tags_for_sessions",
                   return_value=tags_by_session) as mock_tags, \
                patch("app.api.sessions_helpers.get_fork_info_for_sessions",
                      return_value=fork_by_session) as mock_forks:
            sessions = [{"id": "session-1"}, {"id": "session-2"}]
            result = enrich_sessions_with_tags(sessions)

        assert len(result) == 2
        assert result[0]["tags"] == [{"id": "tag-1", "name": "Bug"}]
        assert result[0]["has_forks"] is True
        # One batched query for all sessions, not one per session.
        mock_tags.assert_called_once_with(["session-1", "session-2"])
        mock_forks.assert_called_once_with(["session-1", "session-2"])

    def test_handles_empty_list(self, mock_database):
        """Should handle empty session list."""
        with patch("app.api.sessions_helpers.get_tags_for_sessions") as mock_tags:
            result = enrich_sessions_with_tags([])
        assert result == []
        mock_tags.assert_not_called()


class TestControllableAnnotation:
    """Truth table for the cross-install ``controllable`` flag.

    Control (send/interrupt) rides the shared DB (runs lease +
    pending_interactions), so it depends ONLY on the owning install being
    online — never on its ``base_url`` (which is now used solely for artifact
    redirects). See ``_annotate_cluster_fields``.
    """

    def _annotate(self, sessions, installs_map, me="me-install"):
        from app.api import sessions_helpers

        mock_settings = MagicMock()
        mock_settings.effective_instance_id = me
        mock_settings.v2_placement = False
        with patch("app.api.sessions_helpers.database") as mock_db, \
                patch("app.core.config.settings", mock_settings), \
                patch("app.db.projects_v2.list_projects_v2", return_value=[]):
            mock_db.get_installs_map.return_value = installs_map
            mock_db.get_project.return_value = None
            sessions_helpers._annotate_cluster_fields(sessions)
        return sessions

    def test_remote_online_no_base_url_is_controllable(self):
        """Online owner without an advertised URL is STILL controllable (new)."""
        installs = {"B": {"online": True, "device_name": "Worker", "base_url": None}}
        sessions = [{"id": "s1", "instance_id": "B"}]
        self._annotate(sessions, installs)
        assert sessions[0]["is_remote"] is True
        assert sessions[0]["device_online"] is True
        assert sessions[0]["controllable"] is True

    def test_remote_online_with_base_url_is_controllable(self):
        """Online owner with a URL remains controllable."""
        installs = {"B": {"online": True, "device_name": "Worker",
                          "base_url": "http://10.0.0.200:8000"}}
        sessions = [{"id": "s1", "instance_id": "B"}]
        self._annotate(sessions, installs)
        assert sessions[0]["controllable"] is True

    def test_remote_offline_is_not_controllable(self):
        """Offline owner → history stays readable but read-only."""
        installs = {"B": {"online": False, "device_name": "Worker",
                          "base_url": "http://10.0.0.200:8000"}}
        sessions = [{"id": "s1", "instance_id": "B"}]
        self._annotate(sessions, installs)
        assert sessions[0]["device_online"] is False
        assert sessions[0]["controllable"] is False

    def test_remote_missing_install_is_not_controllable(self):
        """Owner absent from the registry → not controllable."""
        sessions = [{"id": "s1", "instance_id": "C"}]
        self._annotate(sessions, {})
        assert sessions[0]["controllable"] is False

    def test_local_session_is_controllable(self):
        """A session owned by this install is always controllable."""
        installs = {"me-install": {"online": True, "device_name": "Primary",
                                   "base_url": None}}
        sessions = [{"id": "s1", "instance_id": "me-install"}]
        self._annotate(sessions, installs)
        assert sessions[0]["is_remote"] is False
        assert sessions[0]["controllable"] is True


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

    def test_search_sessions_returns_is_favorite(self, client, mock_database):
        """Regression: a favorited session must surface is_favorite=True through
        the search endpoint. The DB layer's search_sessions CTEs previously
        omitted is_favorite from their SELECT lists, so the API response model
        defaulted it to False and starred sessions rendered unstarred in search
        results (cross-install favorites bug). This guards the response-model
        contract; the SQL-column fix is exercised by the DB layer against
        Postgres (no reachable PG in this sandbox — see report)."""
        mock_database.search_sessions.return_value = [
            {
                "id": "fav-session",
                "title": "Starred chat",
                "profile_id": "default",
                "status": "active",
                "is_favorite": True,
                "created_at": "2024-01-15T10:30:00",
                "updated_at": "2024-01-15T10:30:00",
                "match_type": "title",
            }
        ]

        response = client.get("/api/v1/sessions/search/query?q=starred")

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["is_favorite"] is True

    def test_search_sessions_passes_match_count(self, client, mock_database):
        """match_count from the DB layer must surface in the search response so
        the UI can show how many places a session matched (title/legacy/store)."""
        mock_database.search_sessions.return_value = [
            {
                "id": "s1",
                "title": "T",
                "profile_id": "default",
                "status": "active",
                "created_at": "2024-01-15T10:30:00",
                "updated_at": "2024-01-15T10:30:00",
                "match_type": "content",
                "match_count": 3,
            }
        ]

        response = client.get("/api/v1/sessions/search/query?q=test")

        assert response.status_code == 200
        assert response.json()[0]["match_count"] == 3

    def test_search_sessions_match_count_defaults_to_one(self, client, mock_database):
        """A result without match_count defaults to 1 (back-compat for callers
        that predate the field)."""
        mock_database.search_sessions.return_value = [
            {
                "id": "s1",
                "title": "T",
                "profile_id": "default",
                "status": "active",
                "created_at": "2024-01-15T10:30:00",
                "updated_at": "2024-01-15T10:30:00",
                "match_type": "title",
            }
        ]

        response = client.get("/api/v1/sessions/search/query?q=test")

        assert response.status_code == 200
        assert response.json()[0]["match_count"] == 1

    def test_search_paginated_passes_match_count(self, client, mock_database):
        """The paginated search endpoint must pass match_count through each item."""
        result = {
            "id": "s1",
            "title": "T",
            "profile_id": "default",
            "status": "active",
            "created_at": "2024-01-15T10:30:00",
            "updated_at": "2024-01-15T10:30:00",
            "match_type": "content",
            "match_count": 5,
        }
        mock_database.search_sessions_paginated.return_value = ([result], 1)

        with patch("app.api.sessions_helpers.get_tags_for_sessions", return_value={}), \
                patch("app.api.sessions_helpers.get_fork_info_for_sessions", return_value={}):
            response = client.get("/api/v1/sessions/search/paginated?q=test")

        assert response.status_code == 200
        data = response.json()
        assert data["items"][0]["match_count"] == 5

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
        mock_database.get_session_by_sdk_id.return_value = None  # Also mock fallback lookup

        response = client.get("/api/v1/sessions/non-existent")

        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()

    def test_get_session_by_sdk_session_id_fallback(self, client, mock_database, sample_session):
        """Should find session by SDK session ID if direct lookup fails."""
        # Direct lookup fails
        mock_database.get_session.return_value = None
        # But fallback lookup by SDK session ID succeeds
        mock_database.get_session_by_sdk_id.return_value = sample_session
        mock_database.get_session_messages.return_value = []
        mock_database.get_all_session_usage.return_value = []
        mock_database.get_session_tags.return_value = []
        mock_database.session_has_forks.return_value = False

        # Request using a UUID (which looks like an SDK session ID)
        response = client.get("/api/v1/sessions/417f9ae9-5012-47f9-b4a4-713c7f8e3cd5")

        # Should succeed because fallback lookup found the session
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == "test-session-id"

    def test_get_session_with_jsonl_messages(self, client, mock_database, sample_session):
        """Should load messages from the JSONL parser in mirror/disabled mode.

        Pins session_store_mode='disabled' so the read path uses the JSONL parser
        (the postgres_only cutover uses parse_session_history_via_store instead).
        The ambient SESSION_STORE_MODE env can be 'postgres_only' in a deployment
        sandbox, so the mode is set explicitly rather than relied upon. Pool-touching
        helpers (resolve_working_dir, cost_utils) are patched to stay off Postgres.
        """
        from app.core.config import settings
        sample_session["sdk_session_id"] = "sdk-session-123"
        mock_database.get_session.return_value = sample_session

        jsonl_messages = [
            {"id": 1, "role": "user", "content": "Hello", "type": "text", "metadata": {}},
            {"id": 2, "role": "assistant", "content": "Hi there!", "type": "text", "metadata": {}}
        ]

        with patch.object(settings, "session_store_mode", "disabled"), \
                patch("app.core.message_resolver.resolve_working_dir", return_value="/workspace"), \
                patch("app.core.cost_utils.get_cached_profile_map", return_value={}), \
                patch("app.core.cost_utils.attach_cost_metadata"), \
                patch("app.core.jsonl_parser.parse_session_history") as mock_parse:
            mock_parse.return_value = (jsonl_messages, {
                "cache_creation_tokens": 100,
                "cache_read_tokens": 50,
                "last_input_tokens": 200,
                "total_tokens_in": 500,
                "total_tokens_out": 300
            })

            response = client.get("/api/v1/sessions/test-session-id")

        assert response.status_code == 200
        data = response.json()
        assert len(data["messages"]) == 2

    def test_get_session_falls_back_to_database(self, client, mock_database, sample_session):
        """Should fall back to database messages when there is no JSONL sdk id.

        The legacy DB fallback (Fallback 3) only runs in mirror/disabled mode; the
        postgres_only cutover is a hard-cut that never falls through to the
        session_messages table. Pin the mode so the ambient SESSION_STORE_MODE env
        does not suppress the fallback under test.
        """
        from app.core.config import settings
        sample_session["sdk_session_id"] = None
        mock_database.get_session.return_value = sample_session
        mock_database.get_session_messages.return_value = [
            {"id": 1, "role": "user", "content": "Hello", "tool_name": None}
        ]

        with patch.object(settings, "session_store_mode", "disabled"), \
                patch("app.core.cost_utils.attach_cost_metadata"):
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
# Test per-message profile_id is persisted end-to-end through the read paths
# =============================================================================

class TestSessionMessageProfileId:
    """The chat UI labels each user bubble with the profile that produced it.

    That requires the GET /{session_id} response to carry a per-message profile
    id. Before the fix the SessionMessage response model omitted the field, so
    FastAPI stripped it from every message and the frontend fell back to the
    session's CURRENT profile for the whole transcript. These tests pin the
    per-message value on both response paths, including a mid-session profile
    switch (distinct prompts) and a repeated identical prompt under two
    profiles (ordered resolution).
    """

    USAGE = {
        "cache_creation_tokens": 0,
        "cache_read_tokens": 0,
        "last_input_tokens": 0,
        "total_tokens_in": 0,
        "total_tokens_out": 0,
    }

    def _get_jsonl(self, client, mock_database, sample_session, jsonl_messages, profile_map):
        """Drive the JSONL read path (disabled mode) with a fixed profile map."""
        from app.core.config import settings

        sample_session["sdk_session_id"] = "sdk-session-123"
        mock_database.get_session.return_value = sample_session

        with patch.object(settings, "session_store_mode", "disabled"), \
                patch("app.core.message_resolver.resolve_working_dir", return_value="/workspace"), \
                patch("app.core.cost_utils.get_cached_profile_map", return_value=profile_map), \
                patch("app.core.cost_utils.attach_cost_metadata"), \
                patch("app.core.jsonl_parser.parse_session_history") as mock_parse:
            mock_parse.return_value = (jsonl_messages, dict(self.USAGE))
            resp = client.get("/api/v1/sessions/test-session-id")
        return resp

    def test_jsonl_path_carries_per_message_profile_id(
        self, client, mock_database, sample_session
    ):
        """Both the user row AND its turn's assistant row carry the profile.

        The JSONL transcript stores no profile on assistant rows, so the turn's
        assistant/thinking bubbles inherit the profile resolved for the turn's
        user message. Without this the assistant bubble reaches the frontend with
        no profileId and falls back to the session's CURRENT selection — the
        "assistant messages re-label when you switch profile" bug.
        """
        jsonl_messages = [
            {"id": 1, "role": "user", "content": "Hello", "type": "text", "metadata": {}},
            {"id": 2, "role": "assistant", "content": "Hi!", "type": "text", "metadata": {}},
        ]
        resp = self._get_jsonl(
            client, mock_database, sample_session,
            jsonl_messages, {"Hello": ["profile-x"]},
        )

        assert resp.status_code == 200
        msgs = resp.json()["messages"]
        assert msgs[0]["profileId"] == "profile-x"
        assert msgs[0]["profile_id"] == "profile-x"
        # The assistant reply belongs to the same turn → same profile.
        assert msgs[1]["profileId"] == "profile-x"
        assert msgs[1]["profile_id"] == "profile-x"

    def test_jsonl_path_mid_session_profile_change(
        self, client, mock_database, sample_session
    ):
        """Each turn's user AND assistant rows keep that turn's own profile.

        This is the screenshot bug: after a mid-session switch to profile-b, the
        first turn's assistant reply ("a") must stay attributed to profile-a, not
        re-label to the now-current profile-b.
        """
        jsonl_messages = [
            {"id": 1, "role": "user", "content": "First", "type": "text", "metadata": {}},
            {"id": 2, "role": "assistant", "content": "a", "type": "text", "metadata": {}},
            {"id": 3, "role": "user", "content": "Second", "type": "text", "metadata": {}},
            {"id": 4, "role": "assistant", "content": "b", "type": "text", "metadata": {}},
        ]
        resp = self._get_jsonl(
            client, mock_database, sample_session,
            jsonl_messages,
            {"First": ["profile-a"], "Second": ["profile-b"]},
        )

        assert resp.status_code == 200
        msgs = resp.json()["messages"]
        assert msgs[0]["profileId"] == "profile-a"  # user, turn 1
        assert msgs[1]["profileId"] == "profile-a"  # assistant, turn 1 (stays a)
        assert msgs[2]["profileId"] == "profile-b"  # user, turn 2
        assert msgs[3]["profileId"] == "profile-b"  # assistant, turn 2

    def test_jsonl_path_thinking_and_tools_inherit_turn_profile(
        self, client, mock_database, sample_session
    ):
        """Thinking blocks and interleaved tool rows inherit the turn's profile.

        Assistant output for a turn is not just the final text — thinking blocks
        and tool cards render between the user prompt and the reply. All belong to
        the turn's profile so none fall back to the current selection.
        """
        jsonl_messages = [
            {"id": 1, "role": "user", "content": "Do it", "type": "text", "metadata": {}},
            {"id": 2, "role": "assistant", "content": "hmm", "type": "thinking", "metadata": {}},
            {"id": 3, "role": "assistant", "content": "", "type": "tool_use",
             "toolName": "Read", "metadata": {}},
            {"id": 4, "role": "assistant", "content": "done", "type": "text", "metadata": {}},
        ]
        resp = self._get_jsonl(
            client, mock_database, sample_session,
            jsonl_messages, {"Do it": ["profile-z"]},
        )

        assert resp.status_code == 200
        msgs = resp.json()["messages"]
        assert [m["profileId"] for m in msgs] == ["profile-z"] * 4

    def test_jsonl_path_repeated_prompt_resolves_in_order(
        self, client, mock_database, sample_session
    ):
        """Same prompt text twice under two profiles → ordered, not collapsed.

        A content-keyed single-value lookup (the old code) would give BOTH
        occurrences the last profile. The ordered queue pairs each occurrence to
        its own row.
        """
        jsonl_messages = [
            {"id": 1, "role": "user", "content": "repeat", "type": "text", "metadata": {}},
            {"id": 2, "role": "assistant", "content": "a", "type": "text", "metadata": {}},
            {"id": 3, "role": "user", "content": "repeat", "type": "text", "metadata": {}},
        ]
        resp = self._get_jsonl(
            client, mock_database, sample_session,
            jsonl_messages,
            {"repeat": ["profile-1", "profile-2"]},
        )

        assert resp.status_code == 200
        msgs = resp.json()["messages"]
        assert msgs[0]["profileId"] == "profile-1"
        assert msgs[2]["profileId"] == "profile-2"

    def test_legacy_db_path_carries_per_message_profile_id(
        self, client, mock_database, sample_session
    ):
        """Legacy session_messages rows expose each row's own profile_id.

        The legacy fallback runs when there is no JSONL sdk id (mirror/disabled).
        Each row already stores profile_id; the response must surface it (camel
        + snake), including a mid-session profile change across rows.
        """
        from app.core.config import settings

        sample_session["sdk_session_id"] = None
        mock_database.get_session.return_value = sample_session
        mock_database.get_session_messages.return_value = [
            {"id": 1, "role": "user", "content": "First", "tool_name": None, "profile_id": "profile-a"},
            {"id": 2, "role": "assistant", "content": "ok", "tool_name": None, "profile_id": None},
            {"id": 3, "role": "user", "content": "Second", "tool_name": None, "profile_id": "profile-b"},
        ]

        with patch.object(settings, "session_store_mode", "disabled"), \
                patch("app.core.cost_utils.attach_cost_metadata"):
            resp = client.get("/api/v1/sessions/test-session-id")

        assert resp.status_code == 200
        msgs = resp.json()["messages"]
        assert msgs[0]["profileId"] == "profile-a"
        assert msgs[0]["profile_id"] == "profile-a"
        assert msgs[2]["profileId"] == "profile-b"
        assert msgs[2]["profile_id"] == "profile-b"

    def test_legacy_db_path_carries_per_message_timestamp(
        self, client, mock_database, sample_session
    ):
        """Legacy ``session_messages`` rows surface each row's own created_at.

        Regression guard for the "every message shows the same wall-clock time
        after a reload" bug: the legacy fallback (Fallback 3, used in
        mirror/disabled when JSONL yields nothing, and in postgres_only for
        pre-cutover sessions) must expose each row's ``created_at`` as
        ``metadata.timestamp`` so the frontend renders the original send time
        instead of falling back to ``now``. Datetime columns must be normalized
        to ISO strings (mirroring db_message_loader) so the value survives JSON
        serialization identically to the /live path.
        """
        from datetime import datetime
        from app.core.config import settings

        ts1 = datetime(2024, 1, 15, 19, 50, 0)
        ts2 = datetime(2024, 1, 15, 20, 30, 0)
        ts3 = datetime(2024, 1, 15, 20, 57, 0)

        sample_session["sdk_session_id"] = None
        mock_database.get_session.return_value = sample_session
        mock_database.get_session_messages.return_value = [
            {"id": 1, "role": "user", "content": "First", "tool_name": None,
             "profile_id": None, "created_at": ts1},
            {"id": 2, "role": "assistant", "content": "ok", "tool_name": None,
             "profile_id": None, "created_at": ts2},
            {"id": 3, "role": "user", "content": "Second", "tool_name": None,
             "profile_id": None, "created_at": ts3},
        ]

        with patch.object(settings, "session_store_mode", "disabled"), \
                patch("app.core.cost_utils.attach_cost_metadata"):
            resp = client.get("/api/v1/sessions/test-session-id")

        assert resp.status_code == 200
        msgs = resp.json()["messages"]
        # Each message must carry its OWN original timestamp (not a shared "now").
        assert msgs[0]["metadata"]["timestamp"] == ts1.isoformat()
        assert msgs[1]["metadata"]["timestamp"] == ts2.isoformat()
        assert msgs[2]["metadata"]["timestamp"] == ts3.isoformat()
        # And they must all be distinct — the exact symptom was uniform stamps.
        stamps = {m["metadata"]["timestamp"] for m in msgs}
        assert len(stamps) == 3


# =============================================================================
# Test session_store_mode read-cutover isolation (PR C.1)
# =============================================================================

class TestSessionStoreModeIsolation:
    """The postgres_only read cutover must be entered ONLY when
    settings.session_store_mode == 'postgres_only'. In mirror/disabled the read
    path stays byte-identical to today: it uses the JSONL parser and never calls
    the store-backed parser; in postgres_only it uses the store-backed parser
    and never falls through to the legacy DB table (Fallback-3 hard-cut).
    """

    JSONL_MSGS = [
        {"id": 1, "role": "user", "content": "Hello", "type": "text", "metadata": {}},
    ]
    STORE_MSGS = [
        {"id": 9, "role": "assistant", "content": "From store", "type": "text", "metadata": {}},
    ]
    USAGE = {
        "cache_creation_tokens": 0,
        "cache_read_tokens": 0,
        "last_input_tokens": 0,
        "total_tokens_in": 0,
        "total_tokens_out": 0,
    }

    def _run(self, client, mode):
        """Patch both parsers + the mode, GET the session, return (resp, mocks).

        ``resolve_working_dir`` is patched to avoid the DB pool (this sandbox has
        no Postgres); the cutover branch is purely about which parser runs.
        """
        from app.core.config import settings

        with patch.object(settings, "session_store_mode", mode), \
                patch("app.core.message_resolver.resolve_working_dir", return_value="/workspace"), \
                patch("app.core.cost_utils.get_cached_profile_map", return_value={}), \
                patch("app.core.cost_utils.attach_cost_metadata"), \
                patch("app.core.jsonl_parser.parse_session_history") as mock_file, \
                patch("app.api.sessions.parse_session_history_via_store") as mock_store:
            mock_file.return_value = (list(self.JSONL_MSGS), dict(self.USAGE))
            mock_store.return_value = (list(self.STORE_MSGS), dict(self.USAGE))
            resp = client.get("/api/v1/sessions/test-session-id")
        return resp, mock_file, mock_store

    @pytest.mark.parametrize("mode", ["disabled", "mirror"])
    def test_non_postgres_only_uses_jsonl_parser(
        self, client, mock_database, sample_session, mode
    ):
        """disabled/mirror: JSONL parser used, store parser NEVER called."""
        sample_session["sdk_session_id"] = "sdk-session-123"
        mock_database.get_session.return_value = sample_session

        resp, mock_file, mock_store = self._run(client, mode)

        assert resp.status_code == 200
        mock_file.assert_called_once()
        mock_store.assert_not_called()
        data = resp.json()
        assert data["messages"][0]["content"] == "Hello"

    def test_postgres_only_uses_store_parser(
        self, client, mock_database, sample_session
    ):
        """postgres_only: store parser used, JSONL parser NEVER called."""
        sample_session["sdk_session_id"] = "sdk-session-123"
        mock_database.get_session.return_value = sample_session

        resp, mock_file, mock_store = self._run(client, "postgres_only")

        assert resp.status_code == 200
        mock_store.assert_called_once()
        mock_file.assert_not_called()
        data = resp.json()
        assert data["messages"][0]["content"] == "From store"

    def test_postgres_only_empty_store_falls_back_to_legacy_db(
        self, client, mock_database, sample_session
    ):
        """Bug C regression: postgres_only + empty store must serve the legacy
        session_messages transcript. Pre-cutover sessions (created before the
        store existed, never bulk-backfilled, JSONL possibly on another
        install) hold their FULL transcript only in the legacy table — the
        original hard-cut rendered them as empty chats in production. The SDK
        JSONL disk reader (Fallback 2) stays hard-cut: only the legacy DB
        fallback is allowed through."""
        from app.core.config import settings

        sample_session["sdk_session_id"] = "sdk-session-123"
        mock_database.get_session.return_value = sample_session
        mock_database.get_session_messages.return_value = [
            {"id": 99, "role": "user", "content": "legacy transcript", "tool_name": None}
        ]

        with patch.object(settings, "session_store_mode", "postgres_only"), \
                patch("app.core.message_resolver.resolve_working_dir", return_value="/workspace"), \
                patch("app.core.cost_utils.attach_cost_metadata"), \
                patch("app.api.sessions.sdk_get_session_messages") as mock_sdk, \
                patch("app.api.sessions.parse_session_history_via_store") as mock_store:
            mock_store.return_value = ([], {})
            resp = client.get("/api/v1/sessions/test-session-id")

        assert resp.status_code == 200
        data = resp.json()
        assert len(data["messages"]) == 1
        assert data["messages"][0]["content"] == "legacy transcript"
        mock_database.get_session_messages.assert_called_once()
        mock_sdk.assert_not_called()

    def test_postgres_only_populated_store_never_touches_legacy_db(
        self, client, mock_database, sample_session
    ):
        """The legacy fallback fires ONLY on an empty store result. A populated
        store (every post-cutover session) must never consult session_messages —
        that table holds user-rows-only for post-cutover sessions and serving it
        was the 'assistant replies vanish' bug."""
        from app.core.config import settings

        sample_session["sdk_session_id"] = "sdk-session-123"
        mock_database.get_session.return_value = sample_session
        mock_database.get_session_messages.return_value = [
            {"id": 99, "role": "user", "content": "LEGACY TRAP", "tool_name": None}
        ]

        with patch.object(settings, "session_store_mode", "postgres_only"), \
                patch("app.core.message_resolver.resolve_working_dir", return_value="/workspace"), \
                patch("app.core.cost_utils.get_cached_profile_map", return_value={}), \
                patch("app.core.cost_utils.attach_cost_metadata"), \
                patch("app.api.sessions.parse_session_history_via_store") as mock_store:
            mock_store.return_value = (list(self.STORE_MSGS), dict(self.USAGE))
            resp = client.get("/api/v1/sessions/test-session-id")

        assert resp.status_code == 200
        data = resp.json()
        assert data["messages"][0]["content"] == "From store"
        mock_database.get_session_messages.assert_not_called()

    def test_postgres_only_raised_store_parse_serves_empty_not_legacy(
        self, client, mock_database, sample_session
    ):
        """A RAISED store parse (transient DB error) is not an EMPTY store.
        Falling through to the legacy table would render a post-cutover session
        as the user-rows-only legacy view (the 'vanishing replies' trap) —
        serve 200 + empty messages instead."""
        from app.core.config import settings

        sample_session["sdk_session_id"] = "sdk-session-123"
        mock_database.get_session.return_value = sample_session
        mock_database.get_session_messages.return_value = [
            {"id": 99, "role": "user", "content": "LEGACY TRAP", "tool_name": None}
        ]

        with patch.object(settings, "session_store_mode", "postgres_only"), \
                patch("app.core.message_resolver.resolve_working_dir", return_value="/workspace"), \
                patch("app.core.cost_utils.attach_cost_metadata"), \
                patch("app.api.sessions.parse_session_history_via_store") as mock_store:
            mock_store.side_effect = RuntimeError("db pool exhausted")
            resp = client.get("/api/v1/sessions/test-session-id")

        assert resp.status_code == 200
        assert resp.json()["messages"] == []
        mock_database.get_session_messages.assert_not_called()

    def test_disabled_empty_jsonl_falls_back_to_legacy_db(
        self, client, mock_database, sample_session
    ):
        """Mirror/disabled behavior is unchanged: an empty JSONL result still
        falls through to the legacy DB table (the contrast to the hard-cut)."""
        from app.core.config import settings

        sample_session["sdk_session_id"] = "sdk-session-123"
        mock_database.get_session.return_value = sample_session
        mock_database.get_session_messages.return_value = [
            {"id": 7, "role": "user", "content": "legacy", "tool_name": None}
        ]

        with patch.object(settings, "session_store_mode", "disabled"), \
                patch("app.core.message_resolver.resolve_working_dir", return_value="/workspace"), \
                patch("app.core.cost_utils.attach_cost_metadata"), \
                patch("app.core.jsonl_parser.parse_session_history") as mock_file, \
                patch("app.api.sessions.sdk_get_session_messages", None):
            mock_file.return_value = ([], {})
            resp = client.get("/api/v1/sessions/test-session-id")

        assert resp.status_code == 200
        data = resp.json()
        assert len(data["messages"]) == 1
        assert data["messages"][0]["content"] == "legacy"
        mock_database.get_session_messages.assert_called_once()


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


class TestClearSession:
    """Test POST /api/v1/sessions/{session_id}/clear endpoint.

    Clears a session's conversation IN PLACE — the row survives; only its
    transcript/messages are reset (contrast with DELETE, which removes the row).
    """

    def test_clear_session_success(self, client, mock_database, sample_session):
        """Should clear the conversation and KEEP the row (no delete_session)."""
        mock_database.get_session.return_value = sample_session
        mock_database.clear_session_conversation.return_value = None

        response = client.post("/api/v1/sessions/test-session-id/clear")

        assert response.status_code == 200
        assert response.json()["session_id"] == "test-session-id"
        mock_database.clear_session_conversation.assert_called_once_with("test-session-id")
        # In-place clear must NEVER delete the session row.
        mock_database.delete_session.assert_not_called()

    def test_clear_session_not_found(self, client, mock_database):
        """Should return 404 for a non-existent session."""
        mock_database.get_session.return_value = None

        response = client.post("/api/v1/sessions/non-existent/clear")

        assert response.status_code == 404

    def test_clear_session_access_denied(self, client, mock_database, mock_api_user, sample_session):
        """Should deny clear for an unauthorized API user."""
        mock_api_user.return_value = {
            "id": "api-user-1",
            "project_id": "other-project"
        }
        sample_session["project_id"] = "test-project"
        mock_database.get_session.return_value = sample_session

        response = client.post("/api/v1/sessions/test-session-id/clear")

        assert response.status_code == 403
        mock_database.clear_session_conversation.assert_not_called()

    # DELETED (V2 Phase-1 deletion): test_clear_session_purges_live_overlay and
    # test_clear_session_skips_overlay_purge_when_absent. Their entire subject —
    # the in-memory live_session_store overlay purge + the sync_engine
    # clear_streaming_state remnant — is deleted with the live store: the chat
    # is served from the persisted transcript (nothing can shadow the cleared
    # DB), and busy truth is the durable runs table (has_active_run gates the
    # clear — see test_clear_session_blocked_while_streaming below).
    def test_clear_session_succeeds_without_live_state(self, client, mock_database, sample_session):
        """V2 success path: with no active run, the clear resets the DB and
        returns 200 — no live overlay exists to purge."""
        mock_database.get_session.return_value = sample_session
        mock_database.clear_session_conversation.return_value = "sdk-123"

        with patch("app.core.streaming_status.has_active_run", return_value=False):
            response = client.post("/api/v1/sessions/test-session-id/clear")

        assert response.status_code == 200
        mock_database.clear_session_conversation.assert_called_once_with("test-session-id")

    def test_clear_session_blocked_while_streaming(self, client, mock_database, sample_session):
        """Never wipe a session mid-stream: an in-flight turn is live.

        Returns 409 and does NOT touch the DB — clearing under an in-flight turn
        corrupts the transcript (the streaming buffer keeps appending and
        re-persists the old conversation on end_streaming). Busy truth is a
        lease-valid active run in the durable ``runs`` table; because that
        table lives in the SHARED database, the same read also blocks a turn
        live on another install.
        """
        mock_database.get_session.return_value = sample_session

        with patch(
            "app.core.streaming_status.has_active_run", return_value=True
        ):
            response = client.post("/api/v1/sessions/test-session-id/clear")

        assert response.status_code == 409
        mock_database.clear_session_conversation.assert_not_called()


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

        with patch("app.core.sync_engine.sync_engine") as mock_sync, \
                patch(
                    "app.core.streaming_status.has_active_run",
                    return_value=True,
                ):
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

    def test_export_session_jsonl(self, client, mock_database, sample_session, tmp_path):
        """Should export the raw Claude session history (.jsonl) file verbatim."""
        mock_database.get_session.return_value = sample_session

        raw = (
            '{"type":"user","message":{"role":"user","content":"Hello"},"uuid":"u1"}\n'
            '{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"Hi"}]},"uuid":"a1"}\n'
        )
        jsonl_file = tmp_path / "sdk-123.jsonl"
        jsonl_file.write_text(raw, encoding="utf-8")

        with patch("app.core.jsonl_parser.get_session_jsonl_path", return_value=jsonl_file):
            response = client.get("/api/v1/sessions/test-session-id/export?format=jsonl")

        assert response.status_code == 200
        assert "application/x-ndjson" in response.headers["content-type"]
        cd = response.headers["content-disposition"]
        assert "attachment" in cd
        assert cd.endswith('.jsonl"')
        # Content must be the file bytes verbatim, not re-serialized.
        assert response.text == raw

    def test_export_session_jsonl_no_file(self, client, mock_database, sample_session):
        """Should return 404 when no JSONL history file exists for the session."""
        mock_database.get_session.return_value = sample_session

        with patch("app.core.jsonl_parser.get_session_jsonl_path", return_value=None):
            response = client.get("/api/v1/sessions/test-session-id/export?format=jsonl")

        assert response.status_code == 404
        assert "JSONL" in response.json()["detail"]


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

    def test_fork_whole_session_when_index_omitted(self, client, mock_database, sample_session, temp_dir):
        """Omitting message_index forks the entire session (board 'Fork' action)."""
        sample_session["sdk_session_id"] = "sdk-123"
        sample_session["project_id"] = None
        mock_database.get_session.return_value = sample_session
        mock_database.create_session.return_value = {"id": "forked-session-id"}

        jsonl_entries = [
            {"type": "user", "message": {"role": "user", "content": "Hello"}},
            {"type": "assistant", "message": {"role": "assistant", "content": "Hi!"}},
            {"type": "user", "message": {"role": "user", "content": "More"}},
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
                    (temp_dir / ".claude" / "projects").mkdir(parents=True, exist_ok=True)

                    # No body at all → message_index defaults to None → whole session.
                    response = client.post("/api/v1/sessions/test-session-id/fork")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        # Fork point is the LAST display message (index 2 of 3).
        assert data["fork_point_message_index"] == 2
        assert data["message_count"] == 3

    def test_fork_whole_empty_session_rejected(self, client, mock_database, sample_session, temp_dir):
        """Forking a session with no display messages (no index) is a 400."""
        sample_session["sdk_session_id"] = "sdk-123"
        mock_database.get_session.return_value = sample_session

        with patch("app.core.config.settings") as mock_settings:
            mock_settings.workspace_dir = temp_dir

            with patch("app.core.jsonl_parser.get_session_jsonl_path") as mock_jsonl_path:
                jsonl_file = temp_dir / "test.jsonl"
                jsonl_file.write_text("")
                mock_jsonl_path.return_value = jsonl_file

                with patch("app.core.jsonl_parser.parse_jsonl_file") as mock_parse:
                    mock_parse.return_value = iter([])
                    response = client.post("/api/v1/sessions/test-session-id/fork", json={})

        assert response.status_code == 400
        assert "empty session" in response.json()["detail"].lower()


# =============================================================================
# Test Adopt Session Endpoint ("Resume here" cross-device)
# =============================================================================

class TestAdoptSession:
    """Test POST /api/v1/sessions/{session_id}/adopt endpoint."""

    def test_adopt_local_session_is_noop(self, client, mock_database, sample_session):
        """A session already owned locally (NULL instance_id) → adopted=False, no write."""
        sample_session["instance_id"] = None
        mock_database.get_session.return_value = sample_session

        response = client.post("/api/v1/sessions/test-session-id/adopt")

        assert response.status_code == 200
        data = response.json()
        assert data["adopted"] is False
        assert data["status"] == "already_local"
        mock_database.adopt_session.assert_not_called()

    def test_adopt_foreign_offline_session_succeeds(self, client, mock_database, sample_session):
        """A foreign session whose owner is offline is re-pointed to this install."""
        from app.core.config import settings

        sample_session["instance_id"] = "other-install-xyz"
        mock_database.get_session.return_value = sample_session
        mock_database.get_install.return_value = {
            "device_name": "Studio Mac",
            "online": False,
        }
        adopted_row = {**sample_session, "instance_id": settings.effective_instance_id}
        mock_database.adopt_session.return_value = adopted_row

        # Not streaming (no active run in the runs table) → adopt.
        response = client.post("/api/v1/sessions/test-session-id/adopt")

        assert response.status_code == 200
        data = response.json()
        assert data["adopted"] is True
        assert data["status"] == "adopted"
        assert data["instance_id"] == settings.effective_instance_id
        mock_database.adopt_session.assert_called_once_with(
            "test-session-id", settings.effective_instance_id
        )

    def test_adopt_foreign_online_but_idle_session_succeeds(self, client, mock_database, sample_session):
        """Owner install ONLINE but the session is NOT streaming → adopt succeeds.

        A 24/7 install must not block adoption of its idle sessions: the gate is
        per-session streaming state, not the install-level online heartbeat.
        """
        from app.core.config import settings

        sample_session["instance_id"] = "other-install-xyz"
        mock_database.get_session.return_value = sample_session
        mock_database.get_install.return_value = {
            "device_name": "Studio Mac",
            "online": True,
        }
        adopted_row = {**sample_session, "instance_id": settings.effective_instance_id}
        mock_database.adopt_session.return_value = adopted_row

        # Owner online, but this idle session has no lease-valid active run.
        with patch(
            "app.core.streaming_status.has_active_run",
            return_value=False,
        ):
            response = client.post("/api/v1/sessions/test-session-id/adopt")

        assert response.status_code == 200
        data = response.json()
        assert data["adopted"] is True
        assert data["status"] == "adopted"
        assert data["instance_id"] == settings.effective_instance_id
        mock_database.adopt_session.assert_called_once_with(
            "test-session-id", settings.effective_instance_id
        )

    def test_adopt_foreign_streaming_session_conflicts(self, client, mock_database, sample_session):
        """Refuse to adopt while the session is actively streaming on its owner."""
        sample_session["instance_id"] = "other-install-xyz"
        mock_database.get_session.return_value = sample_session
        mock_database.get_install.return_value = {
            "device_name": "Studio Mac",
            "online": True,
        }

        with patch(
            "app.core.streaming_status.has_active_run",
            return_value=True,
        ):
            response = client.post("/api/v1/sessions/test-session-id/adopt")

        assert response.status_code == 409
        assert "Studio Mac" in response.json()["detail"]
        mock_database.adopt_session.assert_not_called()

    def test_adopt_streaming_session_missing_install_conflicts(self, client, mock_database, sample_session):
        """Streaming + no install row → still 409, message falls back to 'another device'."""
        sample_session["instance_id"] = "other-install-xyz"
        mock_database.get_session.return_value = sample_session
        mock_database.get_install.return_value = None

        with patch(
            "app.core.streaming_status.has_active_run",
            return_value=True,
        ):
            response = client.post("/api/v1/sessions/test-session-id/adopt")

        assert response.status_code == 409
        assert "another device" in response.json()["detail"]
        mock_database.adopt_session.assert_not_called()

    def test_adopt_session_not_found(self, client, mock_database):
        """Unknown session → 404."""
        mock_database.get_session.return_value = None

        response = client.post("/api/v1/sessions/non-existent/adopt")

        assert response.status_code == 404

    def test_adopt_blocks_when_project_not_checked_out_here(self, client, mock_database, sample_session):
        """Preflight (resolver-only): the session's project has no BASE checkout on
        this runner -> 409, no adopt write. has_local_base_checkout is the single
        gate (base availability, NOT the branch checkout)."""
        sample_session["instance_id"] = "other-install-xyz"
        sample_session["project_id"] = "proj-1"
        mock_database.get_session.return_value = sample_session
        mock_database.get_install.return_value = {"device_name": "Studio Mac", "online": False}

        with patch(
            "app.core.project_resolver.has_local_base_checkout", return_value=False
        ) as gate:
            response = client.post("/api/v1/sessions/test-session-id/adopt")

        assert response.status_code == 409
        assert "checked out on this device" in response.json()["detail"]
        gate.assert_called_once()
        mock_database.adopt_session.assert_not_called()

    def test_adopt_succeeds_when_checkout_present(self, client, mock_database, sample_session):
        """Base checkout present on this runner -> adopt proceeds (ownership flip
        only); project_id is NEVER rewritten under the resolver-only path. A branch
        session adopts on base availability alone (branch materializes at spawn)."""
        from app.core.config import settings

        sample_session["instance_id"] = "other-install-xyz"
        sample_session["project_id"] = "proj-1"
        sample_session["branch"] = "feature-x"  # branch session: base gate suffices
        mock_database.get_session.return_value = sample_session
        mock_database.get_install.return_value = {"device_name": "Studio Mac", "online": False}
        mock_database.adopt_session.return_value = {
            **sample_session, "instance_id": settings.effective_instance_id
        }

        with patch(
            "app.core.project_resolver.has_local_base_checkout", return_value=True
        ):
            response = client.post("/api/v1/sessions/test-session-id/adopt")

        assert response.status_code == 200
        data = response.json()
        assert data["adopted"] is True
        assert data["instance_id"] == settings.effective_instance_id
        # No cross-install project rebind under the resolver-only path.
        mock_database.set_session_project.assert_not_called()
        mock_database.adopt_session.assert_called_once_with(
            "test-session-id", settings.effective_instance_id
        )

    def test_adopt_preflight_error_fails_open(self, client, mock_database, sample_session):
        """A preflight bug must never make an adoptable session un-adoptable: a
        resolver error (non-409) fails open and the adopt still proceeds."""
        from app.core.config import settings

        sample_session["instance_id"] = "other-install-xyz"
        sample_session["project_id"] = "proj-1"
        mock_database.get_session.return_value = sample_session
        mock_database.get_install.return_value = {"device_name": "Studio Mac", "online": False}
        mock_database.adopt_session.return_value = {
            **sample_session, "instance_id": settings.effective_instance_id
        }

        with patch(
            "app.core.project_resolver.has_local_base_checkout",
            side_effect=RuntimeError("preflight boom"),
        ):
            response = client.post("/api/v1/sessions/test-session-id/adopt")

        assert response.status_code == 200
        assert response.json()["adopted"] is True
        mock_database.adopt_session.assert_called_once()


# =============================================================================
# Test Create Worktree Session Endpoint
# =============================================================================

class TestCreateWorktreeSession:
    """Test POST /api/v1/sessions/with-worktree endpoint."""

    def test_create_worktree_session_success(self, client, mock_database, sample_project):
        """The endpoint delegates to the v2 branch-intake helper (PR-B removed the
        legacy worktree fork); the helper's response is returned verbatim with a
        NULL worktree_id. The helper's internals are covered by
        tests/api/test_v2_branch_intake.py."""
        from app.api.sessions_helpers import WorktreeSessionResponse

        mock_database.get_project.return_value = sample_project

        v2_response = WorktreeSessionResponse(
            session_id="session-123",
            worktree_id=None,
            branch_name="feature/test",
            base_branch="main",
            worktree_path="/workspace/checkouts/test-project/feature-test",
            status="active",
            query_started=False,
        )
        with patch(
            "app.api.sessions_worktree._create_v2_branch_session",
            new_callable=AsyncMock,
            return_value=v2_response,
        ) as v2_helper:
            response = client.post(
                "/api/v1/sessions/with-worktree",
                json={
                    "project_id": "test-project",
                    "branch_name": "feature/test",
                    "base_branch": "main"
                }
            )

        v2_helper.assert_awaited_once()
        assert response.status_code == 200
        data = response.json()
        assert data["session_id"] == "session-123"
        assert data["worktree_id"] is None
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


# =============================================================================
# Test GET /{session_id}/context-usage
# =============================================================================

class TestGetSessionContextUsage:
    """Test GET /api/v1/sessions/{session_id}/context-usage endpoint."""

    def _make_state(self, *, is_connected=True, usage_return=None, hang=False):
        """Build a fake pooled SessionState with a mocked adapter."""
        state = MagicMock()
        state.is_connected = is_connected
        if hang:
            async def _hang():
                await asyncio.sleep(2)
            state.adapter.get_context_usage = _hang
        else:
            state.adapter.get_context_usage = AsyncMock(return_value=usage_return)
        return state

    def _patch_pool(self, state):
        """Patch the source get_session_pool so peek() returns ``state``.

        The endpoint imports get_session_pool from app.core.session_pool at
        call time, so patching the source binding (not a frozen from-import)
        governs it.
        """
        mock_pool = MagicMock()
        mock_pool.peek.return_value = state
        return patch("app.core.session_pool.get_session_pool", return_value=mock_pool)

    def test_context_usage_unknown_session_404(self, client, mock_database):
        """Unknown session should 404 before touching the pool."""
        mock_database.get_session.return_value = None
        mock_database.get_session_by_sdk_id.return_value = None

        response = client.get("/api/v1/sessions/non-existent/context-usage")

        assert response.status_code == 404

    def test_context_usage_not_pooled(self, client, mock_database, sample_session):
        """Session exists but pool.peek returns None -> not_connected."""
        mock_database.get_session.return_value = sample_session

        with self._patch_pool(None):
            response = client.get("/api/v1/sessions/test-session-id/context-usage")

        assert response.status_code == 200
        assert response.json() == {"available": False, "reason": "not_connected"}

    def test_context_usage_state_not_connected(self, client, mock_database, sample_session):
        """State present but is_connected False -> not_connected."""
        mock_database.get_session.return_value = sample_session
        state = self._make_state(is_connected=False)

        with self._patch_pool(state):
            response = client.get("/api/v1/sessions/test-session-id/context-usage")

        assert response.status_code == 200
        assert response.json() == {"available": False, "reason": "not_connected"}

    def test_context_usage_connected_returns_usage(self, client, mock_database, sample_session):
        """Connected + adapter returns a dict -> available True, usage passed through."""
        mock_database.get_session.return_value = sample_session
        usage = {
            "categories": [{"name": "system", "tokens": 1200}],
            "totalTokens": 1200,
            "maxTokens": 200000,
            "percentage": 0.6,
        }
        state = self._make_state(usage_return=usage)

        with self._patch_pool(state):
            response = client.get("/api/v1/sessions/test-session-id/context-usage")

        assert response.status_code == 200
        assert response.json() == {"available": True, "source": "live", "usage": usage}

    def test_context_usage_connected_unsupported(self, client, mock_database, sample_session):
        """Connected + adapter returns None -> unsupported, NO persisted fallback.

        A snapshot exists but must be ignored on a connected session: the
        frontend falls back to its live estimate, not a stale snapshot.
        """
        mock_database.get_session.return_value = sample_session
        mock_database.get_session_context_usage_snapshot.return_value = {
            "captured_at": "2024-01-15T10:30:00+00:00",
            "data": {"totalTokens": 999, "maxTokens": 200000},
        }
        state = self._make_state(usage_return=None)

        with self._patch_pool(state):
            response = client.get("/api/v1/sessions/test-session-id/context-usage")

        assert response.status_code == 200
        assert response.json() == {"available": False, "reason": "unsupported"}

    def test_context_usage_timeout(self, client, mock_database, sample_session):
        """Connected + adapter call hangs past the bound -> timeout, NO fallback.

        A snapshot exists but must be ignored on a connected session.
        """
        mock_database.get_session.return_value = sample_session
        mock_database.get_session_context_usage_snapshot.return_value = {
            "captured_at": "2024-01-15T10:30:00+00:00",
            "data": {"totalTokens": 999, "maxTokens": 200000},
        }
        state = self._make_state(hang=True)

        with self._patch_pool(state), \
                patch(
                    "app.core.query_persistence.SDK_CONTEXT_USAGE_TIMEOUT_SECONDS",
                    0.05,
                ):
            response = client.get("/api/v1/sessions/test-session-id/context-usage")

        assert response.status_code == 200
        assert response.json() == {"available": False, "reason": "timeout"}

    def test_context_usage_persisted_when_not_connected(
        self, client, mock_database, sample_session
    ):
        """Not connected + column populated -> persisted snapshot, source=persisted."""
        mock_database.get_session.return_value = sample_session
        usage = {
            "categories": [{"name": "system", "tokens": 1200}],
            "totalTokens": 1200,
            "maxTokens": 200000,
            "percentage": 0.6,
        }
        mock_database.get_session_context_usage_snapshot.return_value = {
            "captured_at": "2024-01-15T10:30:00+00:00",
            "data": usage,
        }

        # Session exists but is not in the pool (evicted after finishing).
        with self._patch_pool(None):
            response = client.get("/api/v1/sessions/test-session-id/context-usage")

        assert response.status_code == 200
        assert response.json() == {
            "available": True,
            "source": "persisted",
            "captured_at": "2024-01-15T10:30:00+00:00",
            "usage": usage,
        }

    def test_context_usage_not_connected_empty_column(
        self, client, mock_database, sample_session
    ):
        """Not connected + no persisted snapshot -> available False, not_connected."""
        mock_database.get_session.return_value = sample_session
        mock_database.get_session_context_usage_snapshot.return_value = None

        with self._patch_pool(None):
            response = client.get("/api/v1/sessions/test-session-id/context-usage")

        assert response.status_code == 200
        assert response.json() == {"available": False, "reason": "not_connected"}

