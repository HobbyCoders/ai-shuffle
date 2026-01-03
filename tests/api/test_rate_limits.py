"""
Comprehensive tests for the rate limits API endpoints.

Tests cover:
- Rate limit status endpoint (authenticated users/API keys)
- Queue status endpoint
- Admin endpoints for CRUD operations on rate limit configs
- Queue admin operations
- Authentication helpers (get_current_user, require_admin)
- Validation errors for input models
- Error handling (401, 404, 409)
"""

import pytest
import hashlib
import uuid
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock, AsyncMock
from dataclasses import dataclass, field
from typing import Optional


# =============================================================================
# Mock Data Classes (matching app/core/rate_limiter.py)
# =============================================================================

@dataclass
class MockRateLimitConfig:
    """Mock RateLimitConfig for testing."""
    requests_per_minute: int = 20
    requests_per_hour: int = 200
    requests_per_day: int = 1000
    concurrent_requests: int = 3
    priority: int = 0
    is_unlimited: bool = False


@dataclass
class MockRateLimitStatus:
    """Mock RateLimitStatus for testing."""
    minute_count: int = 5
    hour_count: int = 50
    day_count: int = 100
    concurrent_count: int = 1
    minute_remaining: int = 15
    hour_remaining: int = 150
    day_remaining: int = 900
    concurrent_remaining: int = 2
    minute_reset: datetime = field(default_factory=lambda: datetime.utcnow() + timedelta(minutes=1))
    hour_reset: datetime = field(default_factory=lambda: datetime.utcnow() + timedelta(hours=1))
    day_reset: datetime = field(default_factory=lambda: datetime.utcnow() + timedelta(days=1))
    is_limited: bool = False
    retry_after: int = 0


@dataclass
class MockQueueStatus:
    """Mock QueueStatus for testing."""
    position: int = 0
    estimated_wait_seconds: int = 0
    total_queued: int = 0
    is_queued: bool = False


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def mock_database():
    """Mock database module for rate limits API."""
    with patch("app.api.rate_limits.database") as mock_db:
        # Auth session methods
        mock_db.get_auth_session.return_value = None
        mock_db.get_api_user_by_key_hash.return_value = None
        mock_db.get_api_key_session.return_value = None
        mock_db.get_api_user.return_value = None

        # Rate limit methods
        mock_db.get_all_rate_limits.return_value = []
        mock_db.get_rate_limit.return_value = None
        mock_db.get_rate_limit_for_user.return_value = None
        mock_db.create_rate_limit.return_value = {
            "id": "test-rate-limit-id",
            "user_id": None,
            "api_key_id": None,
            "requests_per_minute": 20,
            "requests_per_hour": 200,
            "requests_per_day": 1000,
            "concurrent_requests": 3,
            "priority": 0,
            "is_unlimited": False,
            "created_at": datetime.utcnow().isoformat(),
            "updated_at": datetime.utcnow().isoformat(),
        }
        mock_db.update_rate_limit.return_value = {
            "id": "test-rate-limit-id",
            "user_id": None,
            "api_key_id": None,
            "requests_per_minute": 50,
            "requests_per_hour": 500,
            "requests_per_day": 2000,
            "concurrent_requests": 5,
            "priority": 1,
            "is_unlimited": False,
            "created_at": datetime.utcnow().isoformat(),
            "updated_at": datetime.utcnow().isoformat(),
        }
        mock_db.delete_rate_limit.return_value = True
        yield mock_db


@pytest.fixture
def mock_rate_limiter():
    """Mock rate limiter for testing."""
    with patch("app.api.rate_limits.rate_limiter") as mock_rl:
        mock_rl.get_limit_config.return_value = MockRateLimitConfig()
        mock_rl.get_rate_limit_status.return_value = MockRateLimitStatus()
        mock_rl.clear_cache.return_value = None
        yield mock_rl


@pytest.fixture
def mock_request_queue():
    """Mock request queue for testing."""
    with patch("app.api.rate_limits.request_queue") as mock_rq:
        mock_rq.get_position = AsyncMock(return_value=MockQueueStatus())
        mock_rq.get_queue_size = AsyncMock(return_value=5)
        mock_rq.clear = AsyncMock(return_value=3)
        mock_rq._max_size = 100
        mock_rq._process_time_estimate = 30
        yield mock_rq


@pytest.fixture
def admin_session_cookie():
    """Return a session cookie value for admin."""
    return "test-admin-session-token"


@pytest.fixture
def api_key_header():
    """Return an API key authorization header."""
    return "Bearer aih_test_api_key_12345"


@pytest.fixture
def api_session_header():
    """Return an API session authorization header."""
    return "Bearer test-api-session-token"


# =============================================================================
# Module Import Tests
# =============================================================================

class TestRateLimitsModuleImports:
    """Verify rate limits module can be imported correctly."""

    def test_rate_limits_module_imports(self):
        """Rate limits module should import without errors."""
        from app.api import rate_limits
        assert rate_limits is not None

    def test_rate_limits_router_exists(self):
        """Rate limits router should exist."""
        from app.api.rate_limits import router
        assert router is not None

    def test_rate_limits_router_prefix(self):
        """Rate limits router should have correct prefix."""
        from app.api.rate_limits import router
        assert router.prefix == "/api/v1/rate-limits"

    def test_rate_limits_router_tags(self):
        """Rate limits router should have correct tags."""
        from app.api.rate_limits import router
        assert "Rate Limits" in router.tags


# =============================================================================
# Pydantic Model Tests
# =============================================================================

class TestRateLimitConfigModel:
    """Test RateLimitConfigModel validation."""

    def test_rate_limit_config_model_defaults(self):
        """RateLimitConfigModel should have correct defaults."""
        from app.api.rate_limits import RateLimitConfigModel
        model = RateLimitConfigModel(id="test-id")
        assert model.id == "test-id"
        assert model.user_id is None
        assert model.api_key_id is None
        assert model.requests_per_minute == 20
        assert model.requests_per_hour == 200
        assert model.requests_per_day == 1000
        assert model.concurrent_requests == 3
        assert model.priority == 0
        assert model.is_unlimited is False

    def test_rate_limit_config_model_with_user_id(self):
        """RateLimitConfigModel should accept user_id."""
        from app.api.rate_limits import RateLimitConfigModel
        model = RateLimitConfigModel(id="test-id", user_id="user-123")
        assert model.user_id == "user-123"


class TestRateLimitConfigCreate:
    """Test RateLimitConfigCreate validation."""

    def test_rate_limit_config_create_defaults(self):
        """RateLimitConfigCreate should have correct defaults."""
        from app.api.rate_limits import RateLimitConfigCreate
        model = RateLimitConfigCreate()
        assert model.user_id is None
        assert model.api_key_id is None
        assert model.requests_per_minute == 20
        assert model.requests_per_hour == 200
        assert model.requests_per_day == 1000
        assert model.concurrent_requests == 3
        assert model.priority == 0
        assert model.is_unlimited is False

    def test_rate_limit_config_create_validation_min(self):
        """RateLimitConfigCreate should enforce minimum values."""
        from app.api.rate_limits import RateLimitConfigCreate
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            RateLimitConfigCreate(requests_per_minute=0)

        with pytest.raises(ValidationError):
            RateLimitConfigCreate(concurrent_requests=0)

    def test_rate_limit_config_create_validation_max(self):
        """RateLimitConfigCreate should enforce maximum values."""
        from app.api.rate_limits import RateLimitConfigCreate
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            RateLimitConfigCreate(requests_per_minute=10001)

        with pytest.raises(ValidationError):
            RateLimitConfigCreate(requests_per_hour=100001)

    def test_rate_limit_config_create_priority_range(self):
        """RateLimitConfigCreate should enforce priority range."""
        from app.api.rate_limits import RateLimitConfigCreate
        from pydantic import ValidationError

        # Valid range
        model = RateLimitConfigCreate(priority=-100)
        assert model.priority == -100

        model = RateLimitConfigCreate(priority=100)
        assert model.priority == 100

        # Invalid range
        with pytest.raises(ValidationError):
            RateLimitConfigCreate(priority=-101)

        with pytest.raises(ValidationError):
            RateLimitConfigCreate(priority=101)


class TestRateLimitConfigUpdate:
    """Test RateLimitConfigUpdate validation."""

    def test_rate_limit_config_update_all_optional(self):
        """RateLimitConfigUpdate fields should all be optional."""
        from app.api.rate_limits import RateLimitConfigUpdate
        model = RateLimitConfigUpdate()
        assert model.requests_per_minute is None
        assert model.requests_per_hour is None
        assert model.requests_per_day is None
        assert model.concurrent_requests is None
        assert model.priority is None
        assert model.is_unlimited is None

    def test_rate_limit_config_update_partial(self):
        """RateLimitConfigUpdate should accept partial updates."""
        from app.api.rate_limits import RateLimitConfigUpdate
        model = RateLimitConfigUpdate(requests_per_minute=50)
        assert model.requests_per_minute == 50
        assert model.requests_per_hour is None


class TestRateLimitStatusResponse:
    """Test RateLimitStatusResponse model."""

    def test_rate_limit_status_response_fields(self):
        """RateLimitStatusResponse should have all required fields."""
        from app.api.rate_limits import RateLimitStatusResponse
        now = datetime.utcnow()
        model = RateLimitStatusResponse(
            minute_count=5,
            minute_limit=20,
            minute_remaining=15,
            minute_reset=now,
            hour_count=50,
            hour_limit=200,
            hour_remaining=150,
            hour_reset=now,
            day_count=100,
            day_limit=1000,
            day_remaining=900,
            day_reset=now,
            concurrent_count=1,
            concurrent_limit=3,
            concurrent_remaining=2,
            is_limited=False,
            is_unlimited=False,
            priority=0
        )
        assert model.minute_count == 5
        assert model.minute_limit == 20
        assert model.is_limited is False


class TestQueueStatusResponse:
    """Test QueueStatusResponse model."""

    def test_queue_status_response_fields(self):
        """QueueStatusResponse should have all required fields."""
        from app.api.rate_limits import QueueStatusResponse
        model = QueueStatusResponse(
            position=3,
            estimated_wait_seconds=90,
            total_queued=10,
            is_queued=True
        )
        assert model.position == 3
        assert model.estimated_wait_seconds == 90
        assert model.total_queued == 10
        assert model.is_queued is True


# =============================================================================
# Authentication Helper Tests
# =============================================================================

class TestGetCurrentUser:
    """Test get_current_user authentication helper."""

    @pytest.mark.asyncio
    async def test_get_current_user_no_auth(self, mock_database):
        """get_current_user should return None values when no auth provided."""
        from app.api.rate_limits import get_current_user

        user_id, api_key_id, is_admin = await get_current_user(None, None)

        assert user_id is None
        assert api_key_id is None
        assert is_admin is False

    @pytest.mark.asyncio
    async def test_get_current_user_valid_session(self, mock_database):
        """get_current_user should return admin for valid session."""
        from app.api.rate_limits import get_current_user

        mock_database.get_auth_session.return_value = {
            "id": "session-id",
            "token": "test-token",
            "expires_at": (datetime.utcnow() + timedelta(hours=1)).isoformat()
        }

        user_id, api_key_id, is_admin = await get_current_user("test-token", None)

        assert user_id == "admin"
        assert api_key_id is None
        assert is_admin is True

    @pytest.mark.asyncio
    async def test_get_current_user_invalid_session(self, mock_database):
        """get_current_user should return None for invalid session."""
        from app.api.rate_limits import get_current_user

        mock_database.get_auth_session.return_value = None

        user_id, api_key_id, is_admin = await get_current_user("invalid-token", None)

        assert user_id is None
        assert api_key_id is None
        assert is_admin is False

    @pytest.mark.asyncio
    async def test_get_current_user_api_key(self, mock_database):
        """get_current_user should return user for valid API key."""
        from app.api.rate_limits import get_current_user

        mock_database.get_api_user_by_key_hash.return_value = {
            "id": "api-user-id",
            "username": "testuser",
            "name": "Test User"
        }

        user_id, api_key_id, is_admin = await get_current_user(None, "Bearer aih_test_key")

        assert user_id == "testuser"
        assert api_key_id == "api-user-id"
        assert is_admin is False

    @pytest.mark.asyncio
    async def test_get_current_user_api_key_with_name_fallback(self, mock_database):
        """get_current_user should use name when username not provided."""
        from app.api.rate_limits import get_current_user

        mock_database.get_api_user_by_key_hash.return_value = {
            "id": "api-user-id",
            "name": "Test User"
        }

        user_id, api_key_id, is_admin = await get_current_user(None, "Bearer aih_test_key")

        assert user_id == "Test User"
        assert api_key_id == "api-user-id"

    @pytest.mark.asyncio
    async def test_get_current_user_api_session(self, mock_database):
        """get_current_user should return user for valid API session."""
        from app.api.rate_limits import get_current_user

        mock_database.get_api_key_session.return_value = {
            "api_user_id": "api-user-id",
            "token": "session-token"
        }
        mock_database.get_api_user.return_value = {
            "id": "api-user-id",
            "username": "sessionuser",
            "name": "Session User"
        }

        user_id, api_key_id, is_admin = await get_current_user(None, "Bearer session-token")

        assert user_id == "sessionuser"
        assert api_key_id == "api-user-id"
        assert is_admin is False

    @pytest.mark.asyncio
    async def test_get_current_user_api_session_no_user(self, mock_database):
        """get_current_user should return None when API session user not found."""
        from app.api.rate_limits import get_current_user

        mock_database.get_api_key_session.return_value = {
            "api_user_id": "api-user-id",
            "token": "session-token"
        }
        mock_database.get_api_user.return_value = None

        user_id, api_key_id, is_admin = await get_current_user(None, "Bearer session-token")

        assert user_id is None
        assert api_key_id is None

    @pytest.mark.asyncio
    async def test_get_current_user_invalid_auth_header(self, mock_database):
        """get_current_user should ignore malformed auth header."""
        from app.api.rate_limits import get_current_user

        user_id, api_key_id, is_admin = await get_current_user(None, "InvalidHeader")

        assert user_id is None
        assert api_key_id is None
        assert is_admin is False


class TestRequireAdmin:
    """Test require_admin authentication helper."""

    @pytest.mark.asyncio
    async def test_require_admin_no_session(self, mock_database):
        """require_admin should raise 401 when no session cookie."""
        from app.api.rate_limits import require_admin
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            await require_admin(None)

        assert exc_info.value.status_code == 401
        assert "Authentication required" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_require_admin_invalid_session(self, mock_database):
        """require_admin should raise 401 for invalid session."""
        from app.api.rate_limits import require_admin
        from fastapi import HTTPException

        mock_database.get_auth_session.return_value = None

        with pytest.raises(HTTPException) as exc_info:
            await require_admin("invalid-session")

        assert exc_info.value.status_code == 401
        assert "Invalid session" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_require_admin_valid_session(self, mock_database):
        """require_admin should return True for valid session."""
        from app.api.rate_limits import require_admin

        mock_database.get_auth_session.return_value = {
            "id": "session-id",
            "token": "valid-token"
        }

        result = await require_admin("valid-token")

        assert result is True


# =============================================================================
# User Endpoint Tests
# =============================================================================

class TestGetCurrentRateLimits:
    """Test GET /api/v1/rate-limits endpoint."""

    def test_get_rate_limits_no_auth(self, mock_database, mock_rate_limiter):
        """Should return 401 when not authenticated."""
        from fastapi.testclient import TestClient
        from app.main import app

        with patch("app.api.rate_limits.database", mock_database):
            with patch("app.api.rate_limits.rate_limiter", mock_rate_limiter):
                with TestClient(app) as client:
                    response = client.get("/api/v1/rate-limits")

        assert response.status_code == 401
        assert "Authentication required" in response.json()["detail"]

    def test_get_rate_limits_with_session(self, mock_database, mock_rate_limiter, admin_session_cookie):
        """Should return rate limits for authenticated session."""
        from fastapi.testclient import TestClient
        from app.main import app

        mock_database.get_auth_session.return_value = {
            "id": "session-id",
            "token": admin_session_cookie
        }

        with patch("app.api.rate_limits.database", mock_database):
            with patch("app.api.rate_limits.rate_limiter", mock_rate_limiter):
                with TestClient(app) as client:
                    response = client.get(
                        "/api/v1/rate-limits",
                        cookies={"session": admin_session_cookie}
                    )

        assert response.status_code == 200
        data = response.json()
        assert "minute_count" in data
        assert "minute_limit" in data
        assert "minute_remaining" in data
        assert "is_limited" in data
        assert "is_unlimited" in data
        assert "priority" in data

    def test_get_rate_limits_with_api_key(self, mock_database, mock_rate_limiter, api_key_header):
        """Should return rate limits for API key authentication."""
        from fastapi.testclient import TestClient
        from app.main import app

        mock_database.get_api_user_by_key_hash.return_value = {
            "id": "api-user-id",
            "username": "apiuser",
            "name": "API User"
        }

        with patch("app.api.rate_limits.database", mock_database):
            with patch("app.api.rate_limits.rate_limiter", mock_rate_limiter):
                with TestClient(app) as client:
                    response = client.get(
                        "/api/v1/rate-limits",
                        headers={"Authorization": api_key_header}
                    )

        assert response.status_code == 200
        data = response.json()
        assert data["minute_count"] == 5
        assert data["minute_limit"] == 20


class TestGetQueueStatus:
    """Test GET /api/v1/rate-limits/queue endpoint."""

    def test_get_queue_status_no_auth(self, mock_database, mock_request_queue):
        """Should return 401 when not authenticated."""
        from fastapi.testclient import TestClient
        from app.main import app

        with patch("app.api.rate_limits.database", mock_database):
            with patch("app.api.rate_limits.request_queue", mock_request_queue):
                with TestClient(app) as client:
                    response = client.get("/api/v1/rate-limits/queue")

        assert response.status_code == 401

    def test_get_queue_status_authenticated(self, mock_database, mock_request_queue, admin_session_cookie):
        """Should return queue status for authenticated user."""
        from fastapi.testclient import TestClient
        from app.main import app

        mock_database.get_auth_session.return_value = {"id": "session-id"}

        with patch("app.api.rate_limits.database", mock_database):
            with patch("app.api.rate_limits.request_queue", mock_request_queue):
                with TestClient(app) as client:
                    response = client.get(
                        "/api/v1/rate-limits/queue",
                        cookies={"session": admin_session_cookie}
                    )

        assert response.status_code == 200
        data = response.json()
        assert "position" in data
        assert "estimated_wait_seconds" in data
        assert "total_queued" in data
        assert "is_queued" in data

    def test_get_queue_status_queued_user(self, mock_database, mock_request_queue, admin_session_cookie):
        """Should return correct position for queued user."""
        from fastapi.testclient import TestClient
        from app.main import app

        mock_database.get_auth_session.return_value = {"id": "session-id"}
        mock_request_queue.get_position.return_value = MockQueueStatus(
            position=3,
            estimated_wait_seconds=90,
            total_queued=10,
            is_queued=True
        )

        with patch("app.api.rate_limits.database", mock_database):
            with patch("app.api.rate_limits.request_queue", mock_request_queue):
                with TestClient(app) as client:
                    response = client.get(
                        "/api/v1/rate-limits/queue",
                        cookies={"session": admin_session_cookie}
                    )

        assert response.status_code == 200
        data = response.json()
        assert data["position"] == 3
        assert data["estimated_wait_seconds"] == 90
        assert data["is_queued"] is True


# =============================================================================
# Admin Endpoint Tests
# =============================================================================

class TestListRateLimitConfigs:
    """Test GET /api/v1/rate-limits/config endpoint."""

    def test_list_configs_no_auth(self, mock_database):
        """Should return 401 when not authenticated."""
        from fastapi.testclient import TestClient
        from app.main import app

        with patch("app.api.rate_limits.database", mock_database):
            with TestClient(app) as client:
                response = client.get("/api/v1/rate-limits/config")

        assert response.status_code == 401

    def test_list_configs_authenticated(self, mock_database, admin_session_cookie):
        """Should return list of configs for admin."""
        from fastapi.testclient import TestClient
        from app.main import app

        mock_database.get_auth_session.return_value = {"id": "session-id"}
        mock_database.get_all_rate_limits.return_value = [
            {
                "id": "config-1",
                "user_id": None,
                "api_key_id": None,
                "requests_per_minute": 20,
                "requests_per_hour": 200,
                "requests_per_day": 1000,
                "concurrent_requests": 3,
                "priority": 0,
                "is_unlimited": False,
                "created_at": datetime.utcnow().isoformat(),
                "updated_at": datetime.utcnow().isoformat(),
            }
        ]

        with patch("app.api.rate_limits.database", mock_database):
            with TestClient(app) as client:
                response = client.get(
                    "/api/v1/rate-limits/config",
                    cookies={"session": admin_session_cookie}
                )

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["id"] == "config-1"

    def test_list_configs_empty(self, mock_database, admin_session_cookie):
        """Should return empty list when no configs exist."""
        from fastapi.testclient import TestClient
        from app.main import app

        mock_database.get_auth_session.return_value = {"id": "session-id"}
        mock_database.get_all_rate_limits.return_value = []

        with patch("app.api.rate_limits.database", mock_database):
            with TestClient(app) as client:
                response = client.get(
                    "/api/v1/rate-limits/config",
                    cookies={"session": admin_session_cookie}
                )

        assert response.status_code == 200
        assert response.json() == []


class TestCreateRateLimitConfig:
    """Test POST /api/v1/rate-limits/config endpoint."""

    def test_create_config_no_auth(self, mock_database, mock_rate_limiter):
        """Should return 401 when not authenticated."""
        from fastapi.testclient import TestClient
        from app.main import app

        with patch("app.api.rate_limits.database", mock_database):
            with patch("app.api.rate_limits.rate_limiter", mock_rate_limiter):
                with TestClient(app) as client:
                    response = client.post("/api/v1/rate-limits/config", json={})

        assert response.status_code == 401

    def test_create_config_default_values(self, mock_database, mock_rate_limiter, admin_session_cookie):
        """Should create config with default values."""
        from fastapi.testclient import TestClient
        from app.main import app

        mock_database.get_auth_session.return_value = {"id": "session-id"}
        mock_database.get_rate_limit_for_user.return_value = None

        with patch("app.api.rate_limits.database", mock_database):
            with patch("app.api.rate_limits.rate_limiter", mock_rate_limiter):
                with TestClient(app) as client:
                    response = client.post(
                        "/api/v1/rate-limits/config",
                        json={},
                        cookies={"session": admin_session_cookie}
                    )

        assert response.status_code == 201
        data = response.json()
        assert data["requests_per_minute"] == 20
        assert data["requests_per_hour"] == 200
        mock_rate_limiter.clear_cache.assert_called_once()

    def test_create_config_custom_values(self, mock_database, mock_rate_limiter, admin_session_cookie):
        """Should create config with custom values."""
        from fastapi.testclient import TestClient
        from app.main import app

        mock_database.get_auth_session.return_value = {"id": "session-id"}
        mock_database.get_rate_limit_for_user.return_value = None
        mock_database.create_rate_limit.return_value = {
            "id": "new-config-id",
            "user_id": "custom-user",
            "api_key_id": None,
            "requests_per_minute": 50,
            "requests_per_hour": 500,
            "requests_per_day": 5000,
            "concurrent_requests": 10,
            "priority": 5,
            "is_unlimited": False,
            "created_at": datetime.utcnow().isoformat(),
            "updated_at": datetime.utcnow().isoformat(),
        }

        with patch("app.api.rate_limits.database", mock_database):
            with patch("app.api.rate_limits.rate_limiter", mock_rate_limiter):
                with TestClient(app) as client:
                    response = client.post(
                        "/api/v1/rate-limits/config",
                        json={
                            "user_id": "custom-user",
                            "requests_per_minute": 50,
                            "requests_per_hour": 500,
                            "requests_per_day": 5000,
                            "concurrent_requests": 10,
                            "priority": 5
                        },
                        cookies={"session": admin_session_cookie}
                    )

        assert response.status_code == 201
        data = response.json()
        assert data["user_id"] == "custom-user"
        assert data["requests_per_minute"] == 50

    def test_create_config_conflict(self, mock_database, mock_rate_limiter, admin_session_cookie):
        """Should return 409 when config already exists."""
        from fastapi.testclient import TestClient
        from app.main import app

        mock_database.get_auth_session.return_value = {"id": "session-id"}
        mock_database.get_rate_limit_for_user.return_value = {
            "id": "existing-config",
            "user_id": "existing-user",
            "api_key_id": None
        }

        with patch("app.api.rate_limits.database", mock_database):
            with patch("app.api.rate_limits.rate_limiter", mock_rate_limiter):
                with TestClient(app) as client:
                    response = client.post(
                        "/api/v1/rate-limits/config",
                        json={"user_id": "existing-user"},
                        cookies={"session": admin_session_cookie}
                    )

        assert response.status_code == 409
        assert "already exists" in response.json()["detail"]

    def test_create_config_validation_error(self, mock_database, mock_rate_limiter, admin_session_cookie):
        """Should return 422 for invalid values."""
        from fastapi.testclient import TestClient
        from app.main import app

        mock_database.get_auth_session.return_value = {"id": "session-id"}

        with patch("app.api.rate_limits.database", mock_database):
            with patch("app.api.rate_limits.rate_limiter", mock_rate_limiter):
                with TestClient(app) as client:
                    response = client.post(
                        "/api/v1/rate-limits/config",
                        json={"requests_per_minute": 0},
                        cookies={"session": admin_session_cookie}
                    )

        assert response.status_code == 422

    def test_create_config_with_unlimited(self, mock_database, mock_rate_limiter, admin_session_cookie):
        """Should create config with is_unlimited=True."""
        from fastapi.testclient import TestClient
        from app.main import app

        mock_database.get_auth_session.return_value = {"id": "session-id"}
        mock_database.get_rate_limit_for_user.return_value = None
        mock_database.create_rate_limit.return_value = {
            "id": "unlimited-config",
            "user_id": "vip-user",
            "api_key_id": None,
            "requests_per_minute": 20,
            "requests_per_hour": 200,
            "requests_per_day": 1000,
            "concurrent_requests": 3,
            "priority": 100,
            "is_unlimited": True,
            "created_at": datetime.utcnow().isoformat(),
            "updated_at": datetime.utcnow().isoformat(),
        }

        with patch("app.api.rate_limits.database", mock_database):
            with patch("app.api.rate_limits.rate_limiter", mock_rate_limiter):
                with TestClient(app) as client:
                    response = client.post(
                        "/api/v1/rate-limits/config",
                        json={
                            "user_id": "vip-user",
                            "is_unlimited": True,
                            "priority": 100
                        },
                        cookies={"session": admin_session_cookie}
                    )

        assert response.status_code == 201
        data = response.json()
        assert data["is_unlimited"] is True


class TestUpdateRateLimitConfig:
    """Test PATCH /api/v1/rate-limits/config/{config_id} endpoint."""

    def test_update_config_no_auth(self, mock_database, mock_rate_limiter):
        """Should return 401 when not authenticated."""
        from fastapi.testclient import TestClient
        from app.main import app

        with patch("app.api.rate_limits.database", mock_database):
            with patch("app.api.rate_limits.rate_limiter", mock_rate_limiter):
                with TestClient(app) as client:
                    response = client.patch(
                        "/api/v1/rate-limits/config/test-id",
                        json={"requests_per_minute": 50}
                    )

        assert response.status_code == 401

    def test_update_config_not_found(self, mock_database, mock_rate_limiter, admin_session_cookie):
        """Should return 404 when config not found."""
        from fastapi.testclient import TestClient
        from app.main import app

        mock_database.get_auth_session.return_value = {"id": "session-id"}
        mock_database.get_rate_limit.return_value = None

        with patch("app.api.rate_limits.database", mock_database):
            with patch("app.api.rate_limits.rate_limiter", mock_rate_limiter):
                with TestClient(app) as client:
                    response = client.patch(
                        "/api/v1/rate-limits/config/nonexistent-id",
                        json={"requests_per_minute": 50},
                        cookies={"session": admin_session_cookie}
                    )

        assert response.status_code == 404
        assert "not found" in response.json()["detail"]

    def test_update_config_success(self, mock_database, mock_rate_limiter, admin_session_cookie):
        """Should update config successfully."""
        from fastapi.testclient import TestClient
        from app.main import app

        mock_database.get_auth_session.return_value = {"id": "session-id"}
        mock_database.get_rate_limit.return_value = {
            "id": "config-to-update",
            "requests_per_minute": 20
        }

        with patch("app.api.rate_limits.database", mock_database):
            with patch("app.api.rate_limits.rate_limiter", mock_rate_limiter):
                with TestClient(app) as client:
                    response = client.patch(
                        "/api/v1/rate-limits/config/config-to-update",
                        json={"requests_per_minute": 50, "priority": 1},
                        cookies={"session": admin_session_cookie}
                    )

        assert response.status_code == 200
        mock_rate_limiter.clear_cache.assert_called_once()

    def test_update_config_partial(self, mock_database, mock_rate_limiter, admin_session_cookie):
        """Should update only specified fields."""
        from fastapi.testclient import TestClient
        from app.main import app

        mock_database.get_auth_session.return_value = {"id": "session-id"}
        mock_database.get_rate_limit.return_value = {
            "id": "config-to-update",
            "requests_per_minute": 20,
            "requests_per_hour": 200
        }
        mock_database.update_rate_limit.return_value = {
            "id": "config-to-update",
            "user_id": None,
            "api_key_id": None,
            "requests_per_minute": 100,
            "requests_per_hour": 200,
            "requests_per_day": 1000,
            "concurrent_requests": 3,
            "priority": 0,
            "is_unlimited": False,
            "created_at": datetime.utcnow().isoformat(),
            "updated_at": datetime.utcnow().isoformat(),
        }

        with patch("app.api.rate_limits.database", mock_database):
            with patch("app.api.rate_limits.rate_limiter", mock_rate_limiter):
                with TestClient(app) as client:
                    response = client.patch(
                        "/api/v1/rate-limits/config/config-to-update",
                        json={"requests_per_minute": 100},
                        cookies={"session": admin_session_cookie}
                    )

        assert response.status_code == 200
        data = response.json()
        assert data["requests_per_minute"] == 100
        assert data["requests_per_hour"] == 200  # Unchanged


class TestDeleteRateLimitConfig:
    """Test DELETE /api/v1/rate-limits/config/{config_id} endpoint."""

    def test_delete_config_no_auth(self, mock_database, mock_rate_limiter):
        """Should return 401 when not authenticated."""
        from fastapi.testclient import TestClient
        from app.main import app

        with patch("app.api.rate_limits.database", mock_database):
            with patch("app.api.rate_limits.rate_limiter", mock_rate_limiter):
                with TestClient(app) as client:
                    response = client.delete("/api/v1/rate-limits/config/test-id")

        assert response.status_code == 401

    def test_delete_config_not_found(self, mock_database, mock_rate_limiter, admin_session_cookie):
        """Should return 404 when config not found."""
        from fastapi.testclient import TestClient
        from app.main import app

        mock_database.get_auth_session.return_value = {"id": "session-id"}
        mock_database.get_rate_limit.return_value = None

        with patch("app.api.rate_limits.database", mock_database):
            with patch("app.api.rate_limits.rate_limiter", mock_rate_limiter):
                with TestClient(app) as client:
                    response = client.delete(
                        "/api/v1/rate-limits/config/nonexistent-id",
                        cookies={"session": admin_session_cookie}
                    )

        assert response.status_code == 404

    def test_delete_config_success(self, mock_database, mock_rate_limiter, admin_session_cookie):
        """Should delete config successfully."""
        from fastapi.testclient import TestClient
        from app.main import app

        mock_database.get_auth_session.return_value = {"id": "session-id"}
        mock_database.get_rate_limit.return_value = {"id": "config-to-delete"}

        with patch("app.api.rate_limits.database", mock_database):
            with patch("app.api.rate_limits.rate_limiter", mock_rate_limiter):
                with TestClient(app) as client:
                    response = client.delete(
                        "/api/v1/rate-limits/config/config-to-delete",
                        cookies={"session": admin_session_cookie}
                    )

        assert response.status_code == 204
        mock_database.delete_rate_limit.assert_called_once_with("config-to-delete")
        mock_rate_limiter.clear_cache.assert_called_once()


class TestGetDefaultLimits:
    """Test GET /api/v1/rate-limits/config/defaults endpoint."""

    def test_get_defaults_no_auth(self, mock_database):
        """Should return 401 when not authenticated."""
        from fastapi.testclient import TestClient
        from app.main import app

        with patch("app.api.rate_limits.database", mock_database):
            with TestClient(app) as client:
                response = client.get("/api/v1/rate-limits/config/defaults")

        assert response.status_code == 401

    def test_get_defaults_success(self, mock_database, admin_session_cookie):
        """Should return default rate limit values."""
        from fastapi.testclient import TestClient
        from app.main import app

        mock_database.get_auth_session.return_value = {"id": "session-id"}

        with patch("app.api.rate_limits.database", mock_database):
            with TestClient(app) as client:
                response = client.get(
                    "/api/v1/rate-limits/config/defaults",
                    cookies={"session": admin_session_cookie}
                )

        assert response.status_code == 200
        data = response.json()
        assert data["requests_per_minute"] == 20
        assert data["requests_per_hour"] == 200
        assert data["requests_per_day"] == 1000
        assert data["concurrent_requests"] == 3
        assert data["priority"] == 0
        assert data["is_unlimited"] is False


class TestGetQueueAdminInfo:
    """Test GET /api/v1/rate-limits/queue/admin endpoint."""

    def test_get_queue_admin_no_auth(self, mock_database, mock_request_queue):
        """Should return 401 when not authenticated."""
        from fastapi.testclient import TestClient
        from app.main import app

        with patch("app.api.rate_limits.database", mock_database):
            with patch("app.api.rate_limits.request_queue", mock_request_queue):
                with TestClient(app) as client:
                    response = client.get("/api/v1/rate-limits/queue/admin")

        assert response.status_code == 401

    def test_get_queue_admin_success(self, mock_database, mock_request_queue, admin_session_cookie):
        """Should return queue statistics for admin."""
        from fastapi.testclient import TestClient
        from app.main import app

        mock_database.get_auth_session.return_value = {"id": "session-id"}

        with patch("app.api.rate_limits.database", mock_database):
            with patch("app.api.rate_limits.request_queue", mock_request_queue):
                with TestClient(app) as client:
                    response = client.get(
                        "/api/v1/rate-limits/queue/admin",
                        cookies={"session": admin_session_cookie}
                    )

        assert response.status_code == 200
        data = response.json()
        assert "queue_size" in data
        assert "max_size" in data
        assert "process_time_estimate" in data
        assert data["queue_size"] == 5
        assert data["max_size"] == 100
        assert data["process_time_estimate"] == 30


class TestClearQueue:
    """Test POST /api/v1/rate-limits/queue/clear endpoint."""

    def test_clear_queue_no_auth(self, mock_database, mock_request_queue):
        """Should return 401 when not authenticated."""
        from fastapi.testclient import TestClient
        from app.main import app

        with patch("app.api.rate_limits.database", mock_database):
            with patch("app.api.rate_limits.request_queue", mock_request_queue):
                with TestClient(app) as client:
                    response = client.post("/api/v1/rate-limits/queue/clear")

        assert response.status_code == 401

    def test_clear_queue_success(self, mock_database, mock_request_queue, admin_session_cookie):
        """Should clear queue and return 204."""
        from fastapi.testclient import TestClient
        from app.main import app

        mock_database.get_auth_session.return_value = {"id": "session-id"}

        with patch("app.api.rate_limits.database", mock_database):
            with patch("app.api.rate_limits.request_queue", mock_request_queue):
                with TestClient(app) as client:
                    response = client.post(
                        "/api/v1/rate-limits/queue/clear",
                        cookies={"session": admin_session_cookie}
                    )

        assert response.status_code == 204
        mock_request_queue.clear.assert_called_once()


# =============================================================================
# Edge Case Tests
# =============================================================================

class TestEdgeCases:
    """Test edge cases and boundary conditions."""

    def test_create_config_with_api_key_id(self, mock_database, mock_rate_limiter, admin_session_cookie):
        """Should create config with api_key_id."""
        from fastapi.testclient import TestClient
        from app.main import app

        mock_database.get_auth_session.return_value = {"id": "session-id"}
        mock_database.get_rate_limit_for_user.return_value = None
        mock_database.create_rate_limit.return_value = {
            "id": "api-key-config",
            "user_id": None,
            "api_key_id": "specific-api-key",
            "requests_per_minute": 20,
            "requests_per_hour": 200,
            "requests_per_day": 1000,
            "concurrent_requests": 3,
            "priority": 0,
            "is_unlimited": False,
            "created_at": datetime.utcnow().isoformat(),
            "updated_at": datetime.utcnow().isoformat(),
        }

        with patch("app.api.rate_limits.database", mock_database):
            with patch("app.api.rate_limits.rate_limiter", mock_rate_limiter):
                with TestClient(app) as client:
                    response = client.post(
                        "/api/v1/rate-limits/config",
                        json={"api_key_id": "specific-api-key"},
                        cookies={"session": admin_session_cookie}
                    )

        assert response.status_code == 201
        data = response.json()
        assert data["api_key_id"] == "specific-api-key"

    def test_existing_config_different_user(self, mock_database, mock_rate_limiter, admin_session_cookie):
        """Should not return 409 when existing config is for different user."""
        from fastapi.testclient import TestClient
        from app.main import app

        mock_database.get_auth_session.return_value = {"id": "session-id"}
        # Return an existing config for a different user (fallback default)
        mock_database.get_rate_limit_for_user.return_value = {
            "id": "default-config",
            "user_id": None,  # Default config (different from requested)
            "api_key_id": None
        }

        with patch("app.api.rate_limits.database", mock_database):
            with patch("app.api.rate_limits.rate_limiter", mock_rate_limiter):
                with TestClient(app) as client:
                    response = client.post(
                        "/api/v1/rate-limits/config",
                        json={"user_id": "new-user"},
                        cookies={"session": admin_session_cookie}
                    )

        # Should not conflict since it's a different user
        assert response.status_code == 201

    def test_max_boundary_values(self, mock_database, mock_rate_limiter, admin_session_cookie):
        """Should accept maximum allowed values."""
        from fastapi.testclient import TestClient
        from app.main import app

        mock_database.get_auth_session.return_value = {"id": "session-id"}
        mock_database.get_rate_limit_for_user.return_value = None
        mock_database.create_rate_limit.return_value = {
            "id": "max-config",
            "user_id": "max-user",
            "api_key_id": None,
            "requests_per_minute": 10000,
            "requests_per_hour": 100000,
            "requests_per_day": 1000000,
            "concurrent_requests": 100,
            "priority": 100,
            "is_unlimited": False,
            "created_at": datetime.utcnow().isoformat(),
            "updated_at": datetime.utcnow().isoformat(),
        }

        with patch("app.api.rate_limits.database", mock_database):
            with patch("app.api.rate_limits.rate_limiter", mock_rate_limiter):
                with TestClient(app) as client:
                    response = client.post(
                        "/api/v1/rate-limits/config",
                        json={
                            "user_id": "max-user",
                            "requests_per_minute": 10000,
                            "requests_per_hour": 100000,
                            "requests_per_day": 1000000,
                            "concurrent_requests": 100,
                            "priority": 100
                        },
                        cookies={"session": admin_session_cookie}
                    )

        assert response.status_code == 201

    def test_min_boundary_values(self, mock_database, mock_rate_limiter, admin_session_cookie):
        """Should accept minimum allowed values."""
        from fastapi.testclient import TestClient
        from app.main import app

        mock_database.get_auth_session.return_value = {"id": "session-id"}
        mock_database.get_rate_limit_for_user.return_value = None
        mock_database.create_rate_limit.return_value = {
            "id": "min-config",
            "user_id": "min-user",
            "api_key_id": None,
            "requests_per_minute": 1,
            "requests_per_hour": 1,
            "requests_per_day": 1,
            "concurrent_requests": 1,
            "priority": -100,
            "is_unlimited": False,
            "created_at": datetime.utcnow().isoformat(),
            "updated_at": datetime.utcnow().isoformat(),
        }

        with patch("app.api.rate_limits.database", mock_database):
            with patch("app.api.rate_limits.rate_limiter", mock_rate_limiter):
                with TestClient(app) as client:
                    response = client.post(
                        "/api/v1/rate-limits/config",
                        json={
                            "user_id": "min-user",
                            "requests_per_minute": 1,
                            "requests_per_hour": 1,
                            "requests_per_day": 1,
                            "concurrent_requests": 1,
                            "priority": -100
                        },
                        cookies={"session": admin_session_cookie}
                    )

        assert response.status_code == 201


# =============================================================================
# Integration Tests (using both mocks together)
# =============================================================================

class TestRateLimitFlow:
    """Test complete rate limit management flow."""

    def test_full_crud_cycle(self, mock_database, mock_rate_limiter, admin_session_cookie):
        """Test create, read, update, delete cycle."""
        from fastapi.testclient import TestClient
        from app.main import app

        mock_database.get_auth_session.return_value = {"id": "session-id"}
        mock_database.get_rate_limit_for_user.return_value = None

        config_id = str(uuid.uuid4())

        # Create config data
        created_config = {
            "id": config_id,
            "user_id": "test-user",
            "api_key_id": None,
            "requests_per_minute": 30,
            "requests_per_hour": 300,
            "requests_per_day": 3000,
            "concurrent_requests": 5,
            "priority": 1,
            "is_unlimited": False,
            "created_at": datetime.utcnow().isoformat(),
            "updated_at": datetime.utcnow().isoformat(),
        }
        mock_database.create_rate_limit.return_value = created_config

        with patch("app.api.rate_limits.database", mock_database):
            with patch("app.api.rate_limits.rate_limiter", mock_rate_limiter):
                with TestClient(app) as client:
                    # Create
                    create_response = client.post(
                        "/api/v1/rate-limits/config",
                        json={
                            "user_id": "test-user",
                            "requests_per_minute": 30,
                            "requests_per_hour": 300,
                            "requests_per_day": 3000,
                            "concurrent_requests": 5,
                            "priority": 1
                        },
                        cookies={"session": admin_session_cookie}
                    )
                    assert create_response.status_code == 201

                    # Setup for update
                    mock_database.get_rate_limit.return_value = created_config
                    updated_config = created_config.copy()
                    updated_config["requests_per_minute"] = 60
                    mock_database.update_rate_limit.return_value = updated_config

                    # Update
                    update_response = client.patch(
                        f"/api/v1/rate-limits/config/{config_id}",
                        json={"requests_per_minute": 60},
                        cookies={"session": admin_session_cookie}
                    )
                    assert update_response.status_code == 200
                    assert update_response.json()["requests_per_minute"] == 60

                    # Delete
                    delete_response = client.delete(
                        f"/api/v1/rate-limits/config/{config_id}",
                        cookies={"session": admin_session_cookie}
                    )
                    assert delete_response.status_code == 204
