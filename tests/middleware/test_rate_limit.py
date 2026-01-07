"""
Unit tests for rate limit middleware.

Tests cover:
- Skip paths for rate limiting (health, docs, static files)
- Rate limited prefixes (API endpoints)
- Rate limit enforcement and 429 responses
- User info extraction (session cookies, API keys, query params)
- Rate limit headers on responses
- Admin bypass functionality
- Error handling during request processing
"""

import hashlib
import pytest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

from starlette.responses import JSONResponse, Response
from starlette.datastructures import MutableHeaders

from app.middleware.rate_limit import (
    RateLimitMiddleware,
    SKIP_PATHS,
    SKIP_PREFIXES,
    RATE_LIMITED_PREFIXES,
)
from app.core.rate_limiter import RateLimitResult, RateLimitStatus, RateLimitConfig


def create_mock_response(content: bytes = b"ok") -> MagicMock:
    """Create a mock response with mutable headers."""
    response = MagicMock()
    response.body = content
    response.headers = {}
    return response


class TestSkipPaths:
    """Test the skip paths configuration."""

    def test_skip_paths_contains_health(self):
        """Health endpoints should be skipped."""
        assert "/health" in SKIP_PATHS
        assert "/api/v1/health" in SKIP_PATHS

    def test_skip_paths_contains_docs(self):
        """Documentation endpoints should be skipped."""
        assert "/docs" in SKIP_PATHS
        assert "/redoc" in SKIP_PATHS
        assert "/openapi.json" in SKIP_PATHS

    def test_skip_paths_contains_static_assets(self):
        """Static assets should be skipped."""
        assert "/favicon.ico" in SKIP_PATHS
        assert "/favicon.svg" in SKIP_PATHS


class TestSkipPrefixes:
    """Test the skip prefixes configuration."""

    def test_skip_prefixes_contains_app(self):
        """App static files should be skipped."""
        assert "/_app/" in SKIP_PREFIXES

    def test_skip_prefixes_contains_static(self):
        """Static directory should be skipped."""
        assert "/static/" in SKIP_PREFIXES


class TestRateLimitedPrefixes:
    """Test the rate limited prefixes configuration."""

    def test_rate_limited_prefixes_contains_api_endpoints(self):
        """API endpoints should be rate limited."""
        assert "/api/v1/query" in RATE_LIMITED_PREFIXES
        assert "/api/v1/conversation" in RATE_LIMITED_PREFIXES

    def test_rate_limited_prefixes_contains_websocket(self):
        """WebSocket endpoint should be rate limited."""
        assert "/ws/chat" in RATE_LIMITED_PREFIXES


class TestRateLimitMiddleware:
    """Test RateLimitMiddleware class."""

    @pytest.fixture
    def middleware(self):
        """Create a middleware instance."""
        # Create a mock app
        app = MagicMock()
        return RateLimitMiddleware(app)

    @pytest.fixture
    def mock_request(self):
        """Create a mock request."""
        request = MagicMock()
        request.url.path = "/api/v1/query"
        request.cookies.get.return_value = None
        request.headers.get.return_value = ""
        request.query_params.get.return_value = None
        return request


class TestDispatchSkipPaths:
    """Test dispatch method for skipped paths."""

    @pytest.fixture
    def middleware(self):
        """Create a middleware instance."""
        app = MagicMock()
        return RateLimitMiddleware(app)

    @pytest.mark.asyncio
    async def test_skip_health_endpoint(self, middleware):
        """Should skip rate limiting for health endpoint."""
        request = MagicMock()
        request.url.path = "/health"

        call_next = AsyncMock(return_value=Response(content=b"ok"))

        response = await middleware.dispatch(request, call_next)

        call_next.assert_called_once_with(request)
        assert response.body == b"ok"

    @pytest.mark.asyncio
    async def test_skip_api_health_endpoint(self, middleware):
        """Should skip rate limiting for API health endpoint."""
        request = MagicMock()
        request.url.path = "/api/v1/health"

        call_next = AsyncMock(return_value=Response(content=b"healthy"))

        response = await middleware.dispatch(request, call_next)

        call_next.assert_called_once_with(request)

    @pytest.mark.asyncio
    async def test_skip_docs_endpoint(self, middleware):
        """Should skip rate limiting for docs endpoint."""
        request = MagicMock()
        request.url.path = "/docs"

        call_next = AsyncMock(return_value=Response(content=b"docs"))

        response = await middleware.dispatch(request, call_next)

        call_next.assert_called_once_with(request)

    @pytest.mark.asyncio
    async def test_skip_openapi_endpoint(self, middleware):
        """Should skip rate limiting for openapi.json endpoint."""
        request = MagicMock()
        request.url.path = "/openapi.json"

        call_next = AsyncMock(return_value=Response(content=b"{}"))

        response = await middleware.dispatch(request, call_next)

        call_next.assert_called_once_with(request)

    @pytest.mark.asyncio
    async def test_skip_app_prefix(self, middleware):
        """Should skip rate limiting for _app static files."""
        request = MagicMock()
        request.url.path = "/_app/immutable/chunks/app.js"

        call_next = AsyncMock(return_value=Response(content=b"js code"))

        response = await middleware.dispatch(request, call_next)

        call_next.assert_called_once_with(request)

    @pytest.mark.asyncio
    async def test_skip_static_prefix(self, middleware):
        """Should skip rate limiting for static files."""
        request = MagicMock()
        request.url.path = "/static/images/logo.png"

        call_next = AsyncMock(return_value=Response(content=b"image"))

        response = await middleware.dispatch(request, call_next)

        call_next.assert_called_once_with(request)

    @pytest.mark.asyncio
    async def test_skip_redoc_endpoint(self, middleware):
        """Should skip rate limiting for redoc endpoint."""
        request = MagicMock()
        request.url.path = "/redoc"

        call_next = AsyncMock(return_value=Response(content=b"redoc"))

        response = await middleware.dispatch(request, call_next)

        call_next.assert_called_once_with(request)

    @pytest.mark.asyncio
    async def test_skip_favicon_ico(self, middleware):
        """Should skip rate limiting for favicon.ico."""
        request = MagicMock()
        request.url.path = "/favicon.ico"

        call_next = AsyncMock(return_value=Response(content=b"icon"))

        response = await middleware.dispatch(request, call_next)

        call_next.assert_called_once_with(request)

    @pytest.mark.asyncio
    async def test_skip_favicon_svg(self, middleware):
        """Should skip rate limiting for favicon.svg."""
        request = MagicMock()
        request.url.path = "/favicon.svg"

        call_next = AsyncMock(return_value=Response(content=b"<svg/>"))

        response = await middleware.dispatch(request, call_next)

        call_next.assert_called_once_with(request)


class TestDispatchNonRateLimited:
    """Test dispatch for non-rate-limited endpoints."""

    @pytest.fixture
    def middleware(self):
        """Create a middleware instance."""
        app = MagicMock()
        return RateLimitMiddleware(app)

    @pytest.mark.asyncio
    @patch("app.middleware.rate_limit.rate_limiter")
    @patch("app.middleware.rate_limit.database")
    async def test_non_rate_limited_endpoint_adds_headers(
        self, mock_db, mock_rate_limiter, middleware
    ):
        """Non-rate-limited endpoints should get headers but not enforce limits."""
        request = MagicMock()
        request.url.path = "/api/v1/profiles"  # Not in RATE_LIMITED_PREFIXES
        request.cookies.get.return_value = None
        request.headers.get.return_value = ""
        request.query_params.get.return_value = None

        mock_response = create_mock_response(b"profiles")
        call_next = AsyncMock(return_value=mock_response)

        mock_status = RateLimitStatus(
            minute_remaining=19,
            hour_remaining=199,
            day_remaining=999,
            minute_reset=datetime.utcnow() + timedelta(minutes=1),
            hour_reset=datetime.utcnow() + timedelta(hours=1),
            day_reset=datetime.utcnow() + timedelta(hours=24),
        )
        mock_rate_limiter.get_rate_limit_status.return_value = mock_status
        mock_rate_limiter.get_limit_config.return_value = RateLimitConfig()

        response = await middleware.dispatch(request, call_next)

        call_next.assert_called_once_with(request)
        assert "X-RateLimit-Limit" in response.headers
        assert "X-RateLimit-Remaining" in response.headers

    @pytest.mark.asyncio
    @patch("app.middleware.rate_limit.rate_limiter")
    @patch("app.middleware.rate_limit.database")
    async def test_non_rate_limited_endpoint_does_not_check_limits(
        self, mock_db, mock_rate_limiter, middleware
    ):
        """Non-rate-limited endpoints should not call check_rate_limit."""
        request = MagicMock()
        request.url.path = "/api/v1/profiles"
        request.cookies.get.return_value = None
        request.headers.get.return_value = ""
        request.query_params.get.return_value = None

        mock_response = create_mock_response(b"profiles")
        call_next = AsyncMock(return_value=mock_response)

        mock_status = RateLimitStatus(
            minute_remaining=19,
            minute_reset=datetime.utcnow() + timedelta(minutes=1),
            hour_reset=datetime.utcnow() + timedelta(hours=1),
            day_reset=datetime.utcnow() + timedelta(hours=24),
        )
        mock_rate_limiter.get_rate_limit_status.return_value = mock_status
        mock_rate_limiter.get_limit_config.return_value = RateLimitConfig()
        mock_rate_limiter.check_rate_limit = AsyncMock()

        await middleware.dispatch(request, call_next)

        # check_rate_limit should NOT be called for non-rate-limited endpoints
        mock_rate_limiter.check_rate_limit.assert_not_called()


class TestDispatchRateLimited:
    """Test dispatch for rate-limited endpoints."""

    @pytest.fixture
    def middleware(self):
        """Create a middleware instance."""
        app = MagicMock()
        return RateLimitMiddleware(app)

    @pytest.mark.asyncio
    @patch("app.middleware.rate_limit.rate_limiter")
    @patch("app.middleware.rate_limit.database")
    async def test_rate_limited_endpoint_allowed(
        self, mock_db, mock_rate_limiter, middleware
    ):
        """Rate-limited endpoint should pass when under limit."""
        request = MagicMock()
        request.url.path = "/api/v1/query"
        request.cookies.get.return_value = None
        request.headers.get.return_value = ""
        request.query_params.get.return_value = None

        mock_response = create_mock_response(b"query response")
        call_next = AsyncMock(return_value=mock_response)

        mock_status = RateLimitStatus(
            minute_count=5,
            hour_count=50,
            day_count=100,
            minute_remaining=15,
            hour_remaining=150,
            day_remaining=900,
            minute_reset=datetime.utcnow() + timedelta(minutes=1),
            hour_reset=datetime.utcnow() + timedelta(hours=1),
            day_reset=datetime.utcnow() + timedelta(hours=24),
        )
        mock_rate_limiter.check_rate_limit = AsyncMock(
            return_value=(RateLimitResult.ALLOWED, mock_status)
        )
        mock_rate_limiter.record_request = AsyncMock(return_value="request-id-123")
        mock_rate_limiter.complete_request = AsyncMock()
        mock_rate_limiter.get_rate_limit_status.return_value = mock_status
        mock_rate_limiter.get_limit_config.return_value = RateLimitConfig()

        response = await middleware.dispatch(request, call_next)

        call_next.assert_called_once_with(request)
        mock_rate_limiter.record_request.assert_called_once()
        mock_rate_limiter.complete_request.assert_called_once()
        assert response.body == b"query response"

    @pytest.mark.asyncio
    @patch("app.middleware.rate_limit.rate_limiter")
    @patch("app.middleware.rate_limit.database")
    async def test_rate_limited_endpoint_denied(
        self, mock_db, mock_rate_limiter, middleware
    ):
        """Rate-limited endpoint should return 429 when over limit."""
        request = MagicMock()
        request.url.path = "/api/v1/query"
        request.cookies.get.return_value = None
        request.headers.get.return_value = ""
        request.query_params.get.return_value = None

        call_next = AsyncMock(return_value=Response(content=b"should not see"))

        mock_status = RateLimitStatus(
            minute_count=20,
            hour_count=50,
            day_count=100,
            minute_remaining=0,
            hour_remaining=150,
            day_remaining=900,
            is_limited=True,
            retry_after=60,
            minute_reset=datetime.utcnow() + timedelta(minutes=1),
            hour_reset=datetime.utcnow() + timedelta(hours=1),
            day_reset=datetime.utcnow() + timedelta(hours=24),
        )
        mock_rate_limiter.check_rate_limit = AsyncMock(
            return_value=(RateLimitResult.DENIED, mock_status)
        )

        response = await middleware.dispatch(request, call_next)

        # Should not call the next handler
        call_next.assert_not_called()
        assert response.status_code == 429
        assert "Retry-After" in response.headers

    @pytest.mark.asyncio
    @patch("app.middleware.rate_limit.rate_limiter")
    @patch("app.middleware.rate_limit.database")
    async def test_conversation_endpoint_rate_limited(
        self, mock_db, mock_rate_limiter, middleware
    ):
        """Conversation endpoint should be rate limited."""
        request = MagicMock()
        request.url.path = "/api/v1/conversation/123"
        request.cookies.get.return_value = None
        request.headers.get.return_value = ""
        request.query_params.get.return_value = None

        mock_status = RateLimitStatus(
            minute_remaining=15,
            minute_reset=datetime.utcnow() + timedelta(minutes=1),
            hour_reset=datetime.utcnow() + timedelta(hours=1),
            day_reset=datetime.utcnow() + timedelta(hours=24),
        )
        mock_rate_limiter.check_rate_limit = AsyncMock(
            return_value=(RateLimitResult.ALLOWED, mock_status)
        )
        mock_rate_limiter.record_request = AsyncMock(return_value="req-id")
        mock_rate_limiter.complete_request = AsyncMock()
        mock_rate_limiter.get_rate_limit_status.return_value = mock_status
        mock_rate_limiter.get_limit_config.return_value = RateLimitConfig()

        mock_response = create_mock_response(b"conversation")
        call_next = AsyncMock(return_value=mock_response)

        response = await middleware.dispatch(request, call_next)

        mock_rate_limiter.check_rate_limit.assert_called_once()

    @pytest.mark.asyncio
    @patch("app.middleware.rate_limit.rate_limiter")
    @patch("app.middleware.rate_limit.database")
    async def test_websocket_endpoint_rate_limited(
        self, mock_db, mock_rate_limiter, middleware
    ):
        """WebSocket endpoint should be rate limited."""
        request = MagicMock()
        request.url.path = "/ws/chat"
        request.cookies.get.return_value = None
        request.headers.get.return_value = ""
        request.query_params.get.return_value = None

        mock_status = RateLimitStatus(
            minute_remaining=15,
            minute_reset=datetime.utcnow() + timedelta(minutes=1),
            hour_reset=datetime.utcnow() + timedelta(hours=1),
            day_reset=datetime.utcnow() + timedelta(hours=24),
        )
        mock_rate_limiter.check_rate_limit = AsyncMock(
            return_value=(RateLimitResult.ALLOWED, mock_status)
        )
        mock_rate_limiter.record_request = AsyncMock(return_value="req-id")
        mock_rate_limiter.complete_request = AsyncMock()
        mock_rate_limiter.get_rate_limit_status.return_value = mock_status
        mock_rate_limiter.get_limit_config.return_value = RateLimitConfig()

        mock_response = create_mock_response(b"ws")
        call_next = AsyncMock(return_value=mock_response)

        response = await middleware.dispatch(request, call_next)

        mock_rate_limiter.check_rate_limit.assert_called_once()

    @pytest.mark.asyncio
    @patch("app.middleware.rate_limit.rate_limiter")
    @patch("app.middleware.rate_limit.database")
    async def test_query_subpath_rate_limited(
        self, mock_db, mock_rate_limiter, middleware
    ):
        """Query subpaths should be rate limited."""
        request = MagicMock()
        request.url.path = "/api/v1/query/stream"
        request.cookies.get.return_value = None
        request.headers.get.return_value = ""
        request.query_params.get.return_value = None

        mock_status = RateLimitStatus(
            minute_remaining=15,
            minute_reset=datetime.utcnow() + timedelta(minutes=1),
            hour_reset=datetime.utcnow() + timedelta(hours=1),
            day_reset=datetime.utcnow() + timedelta(hours=24),
        )
        mock_rate_limiter.check_rate_limit = AsyncMock(
            return_value=(RateLimitResult.ALLOWED, mock_status)
        )
        mock_rate_limiter.record_request = AsyncMock(return_value="req-id")
        mock_rate_limiter.complete_request = AsyncMock()
        mock_rate_limiter.get_rate_limit_status.return_value = mock_status
        mock_rate_limiter.get_limit_config.return_value = RateLimitConfig()

        mock_response = create_mock_response(b"stream")
        call_next = AsyncMock(return_value=mock_response)

        response = await middleware.dispatch(request, call_next)

        mock_rate_limiter.check_rate_limit.assert_called_once()


class TestGetUserInfo:
    """Test _get_user_info method."""

    @pytest.fixture
    def middleware(self):
        """Create a middleware instance."""
        app = MagicMock()
        return RateLimitMiddleware(app)

    @pytest.mark.asyncio
    @patch("app.middleware.rate_limit.database")
    async def test_get_user_info_no_auth(self, mock_db, middleware):
        """Should return None values when no auth present."""
        request = MagicMock()
        request.cookies.get.return_value = None
        request.headers.get.return_value = ""
        request.query_params.get.return_value = None

        user_id, api_key_id, is_admin = await middleware._get_user_info(request)

        assert user_id is None
        assert api_key_id is None
        assert is_admin is False

    @pytest.mark.asyncio
    @patch("app.middleware.rate_limit.database")
    async def test_get_user_info_with_session_cookie(self, mock_db, middleware):
        """Should identify admin from session cookie."""
        request = MagicMock()
        request.cookies.get.return_value = "valid-session-token"
        request.headers.get.return_value = ""
        request.query_params.get.return_value = None

        mock_db.get_auth_session.return_value = {"id": "session-1", "token": "valid-session-token"}

        user_id, api_key_id, is_admin = await middleware._get_user_info(request)

        assert user_id == "admin"
        assert api_key_id is None
        assert is_admin is True

    @pytest.mark.asyncio
    @patch("app.middleware.rate_limit.database")
    async def test_get_user_info_with_invalid_session_cookie(self, mock_db, middleware):
        """Should not authenticate with invalid session cookie."""
        request = MagicMock()
        request.cookies.get.return_value = "invalid-session-token"
        request.headers.get.return_value = ""
        request.query_params.get.return_value = None

        mock_db.get_auth_session.return_value = None

        user_id, api_key_id, is_admin = await middleware._get_user_info(request)

        assert user_id is None
        assert api_key_id is None
        assert is_admin is False

    @pytest.mark.asyncio
    @patch("app.middleware.rate_limit.database")
    async def test_get_user_info_with_api_key(self, mock_db, middleware):
        """Should identify user from API key header."""
        api_key = "aih_test_api_key_12345"
        key_hash = hashlib.sha256(api_key.encode()).hexdigest()

        request = MagicMock()
        request.cookies.get.return_value = None
        request.headers.get.return_value = f"Bearer {api_key}"
        request.query_params.get.return_value = None

        mock_db.get_api_user_by_key_hash.return_value = {
            "id": "api-user-123",
            "username": "testuser",
            "name": "Test User"
        }

        user_id, api_key_id, is_admin = await middleware._get_user_info(request)

        mock_db.get_api_user_by_key_hash.assert_called_once_with(key_hash)
        assert user_id == "testuser"
        assert api_key_id == "api-user-123"
        assert is_admin is False

    @pytest.mark.asyncio
    @patch("app.middleware.rate_limit.database")
    async def test_get_user_info_with_api_key_no_username(self, mock_db, middleware):
        """Should fall back to name when username is not set."""
        api_key = "aih_another_key"
        key_hash = hashlib.sha256(api_key.encode()).hexdigest()

        request = MagicMock()
        request.cookies.get.return_value = None
        request.headers.get.return_value = f"Bearer {api_key}"
        request.query_params.get.return_value = None

        mock_db.get_api_user_by_key_hash.return_value = {
            "id": "api-user-456",
            "username": None,
            "name": "API User Name"
        }

        user_id, api_key_id, is_admin = await middleware._get_user_info(request)

        assert user_id == "API User Name"
        assert api_key_id == "api-user-456"

    @pytest.mark.asyncio
    @patch("app.middleware.rate_limit.database")
    async def test_get_user_info_with_bearer_session_token(self, mock_db, middleware):
        """Should identify user from bearer session token (non-API key)."""
        request = MagicMock()
        request.cookies.get.return_value = None
        request.headers.get.return_value = "Bearer session-token-123"
        request.query_params.get.return_value = None

        mock_db.get_api_user_by_key_hash.return_value = None  # Not an API key
        mock_db.get_api_key_session.return_value = {
            "api_user_id": "api-user-789"
        }
        mock_db.get_api_user.return_value = {
            "id": "api-user-789",
            "username": "session_user",
            "name": "Session User"
        }

        user_id, api_key_id, is_admin = await middleware._get_user_info(request)

        assert user_id == "session_user"
        assert api_key_id == "api-user-789"
        assert is_admin is False

    @pytest.mark.asyncio
    @patch("app.middleware.rate_limit.database")
    async def test_get_user_info_with_query_param_admin_session(self, mock_db, middleware):
        """Should identify admin from query param token (WebSocket)."""
        request = MagicMock()
        request.cookies.get.return_value = None
        request.headers.get.return_value = ""
        request.query_params.get.return_value = "ws-auth-token"

        mock_db.get_auth_session.return_value = {"id": "admin-session"}

        user_id, api_key_id, is_admin = await middleware._get_user_info(request)

        assert user_id == "admin"
        assert is_admin is True

    @pytest.mark.asyncio
    @patch("app.middleware.rate_limit.database")
    async def test_get_user_info_with_query_param_api_session(self, mock_db, middleware):
        """Should identify user from query param API session token."""
        request = MagicMock()
        request.cookies.get.return_value = None
        request.headers.get.return_value = ""
        request.query_params.get.return_value = "api-session-token"

        mock_db.get_auth_session.return_value = None
        mock_db.get_api_key_session.return_value = {"api_user_id": "api-user-abc"}
        mock_db.get_api_user.return_value = {
            "id": "api-user-abc",
            "username": "query_user",
            "name": "Query User"
        }

        user_id, api_key_id, is_admin = await middleware._get_user_info(request)

        assert user_id == "query_user"
        assert api_key_id == "api-user-abc"
        assert is_admin is False

    @pytest.mark.asyncio
    @patch("app.middleware.rate_limit.database")
    async def test_get_user_info_query_param_no_username(self, mock_db, middleware):
        """Should fall back to name when username not set for query param auth."""
        request = MagicMock()
        request.cookies.get.return_value = None
        request.headers.get.return_value = ""
        request.query_params.get.return_value = "api-session-token"

        mock_db.get_auth_session.return_value = None
        mock_db.get_api_key_session.return_value = {"api_user_id": "api-user-xyz"}
        mock_db.get_api_user.return_value = {
            "id": "api-user-xyz",
            "username": "",  # Empty string
            "name": "Fallback Name"
        }

        user_id, api_key_id, is_admin = await middleware._get_user_info(request)

        assert user_id == "Fallback Name"

    @pytest.mark.asyncio
    @patch("app.middleware.rate_limit.database")
    async def test_get_user_info_invalid_bearer_session(self, mock_db, middleware):
        """Should handle invalid bearer session token gracefully."""
        request = MagicMock()
        request.cookies.get.return_value = None
        request.headers.get.return_value = "Bearer invalid-session"
        request.query_params.get.return_value = None

        mock_db.get_api_user_by_key_hash.return_value = None
        mock_db.get_api_key_session.return_value = None

        user_id, api_key_id, is_admin = await middleware._get_user_info(request)

        assert user_id is None
        assert api_key_id is None
        assert is_admin is False

    @pytest.mark.asyncio
    @patch("app.middleware.rate_limit.database")
    async def test_get_user_info_bearer_session_user_not_found(self, mock_db, middleware):
        """Should handle case where session exists but user not found."""
        request = MagicMock()
        request.cookies.get.return_value = None
        request.headers.get.return_value = "Bearer session-token"
        request.query_params.get.return_value = None

        mock_db.get_api_user_by_key_hash.return_value = None
        mock_db.get_api_key_session.return_value = {"api_user_id": "missing-user"}
        mock_db.get_api_user.return_value = None  # User not found

        user_id, api_key_id, is_admin = await middleware._get_user_info(request)

        assert user_id is None
        assert api_key_id is None
        assert is_admin is False

    @pytest.mark.asyncio
    @patch("app.middleware.rate_limit.database")
    async def test_get_user_info_query_param_user_not_found(self, mock_db, middleware):
        """Should handle case where query param session exists but user not found."""
        request = MagicMock()
        request.cookies.get.return_value = None
        request.headers.get.return_value = ""
        request.query_params.get.return_value = "session-token"

        mock_db.get_auth_session.return_value = None
        mock_db.get_api_key_session.return_value = {"api_user_id": "missing-user"}
        mock_db.get_api_user.return_value = None  # User not found

        user_id, api_key_id, is_admin = await middleware._get_user_info(request)

        assert user_id is None
        assert api_key_id is None
        assert is_admin is False

    @pytest.mark.asyncio
    @patch("app.middleware.rate_limit.database")
    async def test_get_user_info_query_param_no_api_session(self, mock_db, middleware):
        """Should handle case where query param token has no session."""
        request = MagicMock()
        request.cookies.get.return_value = None
        request.headers.get.return_value = ""
        request.query_params.get.return_value = "invalid-token"

        mock_db.get_auth_session.return_value = None
        mock_db.get_api_key_session.return_value = None

        user_id, api_key_id, is_admin = await middleware._get_user_info(request)

        assert user_id is None
        assert api_key_id is None
        assert is_admin is False

    @pytest.mark.asyncio
    @patch("app.middleware.rate_limit.database")
    async def test_get_user_info_invalid_api_key(self, mock_db, middleware):
        """Should handle case where API key is not found."""
        api_key = "aih_invalid_key"

        request = MagicMock()
        request.cookies.get.return_value = None
        request.headers.get.return_value = f"Bearer {api_key}"
        request.query_params.get.return_value = None

        mock_db.get_api_user_by_key_hash.return_value = None

        user_id, api_key_id, is_admin = await middleware._get_user_info(request)

        # Should not have user info since API key is invalid
        assert api_key_id is None


class TestAddRateLimitHeaders:
    """Test _add_rate_limit_headers method."""

    @pytest.fixture
    def middleware(self):
        """Create a middleware instance."""
        app = MagicMock()
        return RateLimitMiddleware(app)

    @patch("app.middleware.rate_limit.rate_limiter")
    def test_add_rate_limit_headers(self, mock_rate_limiter, middleware):
        """Should add all rate limit headers to response."""
        response = create_mock_response()

        now = datetime.utcnow()
        mock_status = RateLimitStatus(
            minute_remaining=15,
            hour_remaining=150,
            day_remaining=900,
            minute_reset=now + timedelta(minutes=1),
            hour_reset=now + timedelta(hours=1),
            day_reset=now + timedelta(hours=24),
        )
        mock_rate_limiter.get_rate_limit_status.return_value = mock_status
        mock_rate_limiter.get_limit_config.return_value = RateLimitConfig(
            requests_per_minute=20,
            requests_per_hour=200,
            requests_per_day=1000
        )

        middleware._add_rate_limit_headers(response, "user-1", None, False)

        assert response.headers["X-RateLimit-Limit"] == "20"
        assert response.headers["X-RateLimit-Remaining"] == "15"
        assert "X-RateLimit-Reset" in response.headers
        assert response.headers["X-RateLimit-Limit-Hour"] == "200"
        assert response.headers["X-RateLimit-Remaining-Hour"] == "150"
        assert response.headers["X-RateLimit-Limit-Day"] == "1000"
        assert response.headers["X-RateLimit-Remaining-Day"] == "900"

    @patch("app.middleware.rate_limit.rate_limiter")
    def test_add_rate_limit_headers_for_admin(self, mock_rate_limiter, middleware):
        """Should add headers for admin users."""
        response = create_mock_response()

        mock_status = RateLimitStatus(is_limited=False)
        mock_rate_limiter.get_rate_limit_status.return_value = mock_status
        mock_rate_limiter.get_limit_config.return_value = RateLimitConfig()

        middleware._add_rate_limit_headers(response, "admin", None, True)

        mock_rate_limiter.get_rate_limit_status.assert_called_once_with(
            user_id="admin",
            api_key_id=None,
            is_admin=True
        )

    @patch("app.middleware.rate_limit.rate_limiter")
    def test_add_rate_limit_headers_with_api_key(self, mock_rate_limiter, middleware):
        """Should add headers with correct config for API key user."""
        response = create_mock_response()

        now = datetime.utcnow()
        mock_status = RateLimitStatus(
            minute_remaining=10,
            hour_remaining=90,
            day_remaining=450,
            minute_reset=now + timedelta(minutes=1),
            hour_reset=now + timedelta(hours=1),
            day_reset=now + timedelta(hours=24),
        )
        mock_rate_limiter.get_rate_limit_status.return_value = mock_status
        mock_rate_limiter.get_limit_config.return_value = RateLimitConfig(
            requests_per_minute=15,
            requests_per_hour=100,
            requests_per_day=500
        )

        middleware._add_rate_limit_headers(response, "api-user", "api-key-123", False)

        mock_rate_limiter.get_limit_config.assert_called_once_with("api-user", "api-key-123")
        assert response.headers["X-RateLimit-Limit"] == "15"


class TestCreateRateLimitResponse:
    """Test _create_rate_limit_response method."""

    @pytest.fixture
    def middleware(self):
        """Create a middleware instance."""
        app = MagicMock()
        return RateLimitMiddleware(app)

    def test_create_rate_limit_response_status_code(self, middleware):
        """Should return 429 status code."""
        now = datetime.utcnow()
        status = RateLimitStatus(
            minute_remaining=0,
            hour_remaining=150,
            day_remaining=900,
            retry_after=60,
            minute_reset=now + timedelta(minutes=1),
            hour_reset=now + timedelta(hours=1),
            day_reset=now + timedelta(hours=24),
        )

        response = middleware._create_rate_limit_response(status)

        assert response.status_code == 429

    def test_create_rate_limit_response_headers(self, middleware):
        """Should include required headers."""
        now = datetime.utcnow()
        status = RateLimitStatus(
            minute_remaining=0,
            hour_remaining=150,
            day_remaining=900,
            retry_after=60,
            minute_reset=now + timedelta(minutes=1),
            hour_reset=now + timedelta(hours=1),
            day_reset=now + timedelta(hours=24),
        )

        response = middleware._create_rate_limit_response(status)

        assert response.headers["Retry-After"] == "60"
        assert response.headers["X-RateLimit-Remaining"] == "0"
        assert "X-RateLimit-Reset" in response.headers

    def test_create_rate_limit_response_body(self, middleware):
        """Should include rate limit info in body."""
        import json

        now = datetime.utcnow()
        status = RateLimitStatus(
            minute_remaining=0,
            hour_remaining=150,
            day_remaining=900,
            retry_after=60,
            minute_reset=now + timedelta(minutes=1),
            hour_reset=now + timedelta(hours=1),
            day_reset=now + timedelta(hours=24),
        )

        response = middleware._create_rate_limit_response(status)
        body = json.loads(response.body.decode())

        assert body["detail"] == "Rate limit exceeded"
        assert body["retry_after"] == 60
        assert "limits" in body
        assert "minute" in body["limits"]
        assert "hour" in body["limits"]
        assert "day" in body["limits"]
        assert body["limits"]["minute"]["remaining"] == 0
        assert body["limits"]["hour"]["remaining"] == 150
        assert body["limits"]["day"]["remaining"] == 900

    def test_create_rate_limit_response_retry_after_large(self, middleware):
        """Should handle large retry_after values."""
        now = datetime.utcnow()
        status = RateLimitStatus(
            minute_remaining=0,
            hour_remaining=0,
            day_remaining=0,
            retry_after=86400,  # 24 hours
            minute_reset=now + timedelta(minutes=1),
            hour_reset=now + timedelta(hours=1),
            day_reset=now + timedelta(hours=24),
        )

        response = middleware._create_rate_limit_response(status)

        assert response.headers["Retry-After"] == "86400"


class TestDispatchErrorHandling:
    """Test error handling in dispatch method."""

    @pytest.fixture
    def middleware(self):
        """Create a middleware instance."""
        app = MagicMock()
        return RateLimitMiddleware(app)

    @pytest.mark.asyncio
    @patch("app.middleware.rate_limit.rate_limiter")
    @patch("app.middleware.rate_limit.database")
    async def test_complete_request_called_on_exception(
        self, mock_db, mock_rate_limiter, middleware
    ):
        """Should complete request even if handler raises exception."""
        request = MagicMock()
        request.url.path = "/api/v1/query"
        request.cookies.get.return_value = None
        request.headers.get.return_value = ""
        request.query_params.get.return_value = None

        mock_status = RateLimitStatus(
            minute_remaining=15,
            minute_reset=datetime.utcnow() + timedelta(minutes=1),
            hour_reset=datetime.utcnow() + timedelta(hours=1),
            day_reset=datetime.utcnow() + timedelta(hours=24),
        )
        mock_rate_limiter.check_rate_limit = AsyncMock(
            return_value=(RateLimitResult.ALLOWED, mock_status)
        )
        mock_rate_limiter.record_request = AsyncMock(return_value="request-id")
        mock_rate_limiter.complete_request = AsyncMock()
        mock_rate_limiter.get_rate_limit_status.return_value = mock_status
        mock_rate_limiter.get_limit_config.return_value = RateLimitConfig()

        # Simulate exception in handler
        call_next = AsyncMock(side_effect=Exception("Handler error"))

        with pytest.raises(Exception, match="Handler error"):
            await middleware.dispatch(request, call_next)

        # complete_request should still be called
        mock_rate_limiter.complete_request.assert_called_once()


class TestDispatchWithDuration:
    """Test duration tracking in dispatch."""

    @pytest.fixture
    def middleware(self):
        """Create a middleware instance."""
        app = MagicMock()
        return RateLimitMiddleware(app)

    @pytest.mark.asyncio
    @patch("app.middleware.rate_limit.rate_limiter")
    @patch("app.middleware.rate_limit.database")
    @patch("app.middleware.rate_limit.time")
    async def test_duration_passed_to_complete_request(
        self, mock_time, mock_db, mock_rate_limiter, middleware
    ):
        """Should calculate and pass duration to complete_request."""
        request = MagicMock()
        request.url.path = "/api/v1/query"
        request.cookies.get.return_value = None
        request.headers.get.return_value = ""
        request.query_params.get.return_value = None

        mock_status = RateLimitStatus(
            minute_remaining=15,
            minute_reset=datetime.utcnow() + timedelta(minutes=1),
            hour_reset=datetime.utcnow() + timedelta(hours=1),
            day_reset=datetime.utcnow() + timedelta(hours=24),
        )
        mock_rate_limiter.check_rate_limit = AsyncMock(
            return_value=(RateLimitResult.ALLOWED, mock_status)
        )
        mock_rate_limiter.record_request = AsyncMock(return_value="req-123")
        mock_rate_limiter.complete_request = AsyncMock()
        mock_rate_limiter.get_rate_limit_status.return_value = mock_status
        mock_rate_limiter.get_limit_config.return_value = RateLimitConfig()

        # Simulate 100ms request
        mock_time.time.side_effect = [1000.0, 1000.1]

        mock_response = create_mock_response(b"ok")
        call_next = AsyncMock(return_value=mock_response)

        await middleware.dispatch(request, call_next)

        # Should pass duration_ms = 100
        complete_call = mock_rate_limiter.complete_request.call_args
        assert complete_call.kwargs["duration_ms"] == 100

    @pytest.mark.asyncio
    @patch("app.middleware.rate_limit.rate_limiter")
    @patch("app.middleware.rate_limit.database")
    @patch("app.middleware.rate_limit.time")
    async def test_duration_passed_on_exception(
        self, mock_time, mock_db, mock_rate_limiter, middleware
    ):
        """Should pass duration even when exception occurs."""
        request = MagicMock()
        request.url.path = "/api/v1/query"
        request.cookies.get.return_value = None
        request.headers.get.return_value = ""
        request.query_params.get.return_value = None

        mock_status = RateLimitStatus(
            minute_remaining=15,
            minute_reset=datetime.utcnow() + timedelta(minutes=1),
            hour_reset=datetime.utcnow() + timedelta(hours=1),
            day_reset=datetime.utcnow() + timedelta(hours=24),
        )
        mock_rate_limiter.check_rate_limit = AsyncMock(
            return_value=(RateLimitResult.ALLOWED, mock_status)
        )
        mock_rate_limiter.record_request = AsyncMock(return_value="req-123")
        mock_rate_limiter.complete_request = AsyncMock()

        # Simulate 500ms request that errors
        mock_time.time.side_effect = [1000.0, 1000.5]

        call_next = AsyncMock(side_effect=Exception("Handler error"))

        with pytest.raises(Exception):
            await middleware.dispatch(request, call_next)

        # Should still pass duration_ms = 500
        complete_call = mock_rate_limiter.complete_request.call_args
        assert complete_call.kwargs["duration_ms"] == 500


class TestRateLimitLogging:
    """Test logging in rate limit middleware."""

    @pytest.fixture
    def middleware(self):
        """Create a middleware instance."""
        app = MagicMock()
        return RateLimitMiddleware(app)

    @pytest.mark.asyncio
    @patch("app.middleware.rate_limit.logger")
    @patch("app.middleware.rate_limit.rate_limiter")
    @patch("app.middleware.rate_limit.database")
    async def test_logs_rate_limit_exceeded(
        self, mock_db, mock_rate_limiter, mock_logger, middleware
    ):
        """Should log when rate limit is exceeded."""
        request = MagicMock()
        request.url.path = "/api/v1/query"
        request.cookies.get.return_value = None
        request.headers.get.return_value = ""
        request.query_params.get.return_value = None

        mock_status = RateLimitStatus(
            minute_count=21,
            hour_count=50,
            day_count=100,
            minute_remaining=0,
            is_limited=True,
            retry_after=60,
            minute_reset=datetime.utcnow() + timedelta(minutes=1),
            hour_reset=datetime.utcnow() + timedelta(hours=1),
            day_reset=datetime.utcnow() + timedelta(hours=24),
        )
        mock_rate_limiter.check_rate_limit = AsyncMock(
            return_value=(RateLimitResult.DENIED, mock_status)
        )

        call_next = AsyncMock()

        await middleware.dispatch(request, call_next)

        mock_logger.warning.assert_called_once()
        log_msg = mock_logger.warning.call_args[0][0]
        assert "Rate limit exceeded" in log_msg

    @pytest.mark.asyncio
    @patch("app.middleware.rate_limit.logger")
    @patch("app.middleware.rate_limit.rate_limiter")
    @patch("app.middleware.rate_limit.database")
    async def test_log_includes_user_info(
        self, mock_db, mock_rate_limiter, mock_logger, middleware
    ):
        """Log message should include user and API key info."""
        request = MagicMock()
        request.url.path = "/api/v1/query"
        request.cookies.get.return_value = None
        request.headers.get.return_value = "Bearer aih_test"
        request.query_params.get.return_value = None

        mock_db.get_api_user_by_key_hash.return_value = {
            "id": "api-user-123",
            "username": "testuser",
            "name": "Test"
        }

        mock_status = RateLimitStatus(
            minute_count=21,
            hour_count=50,
            day_count=100,
            minute_remaining=0,
            is_limited=True,
            retry_after=60,
            minute_reset=datetime.utcnow() + timedelta(minutes=1),
            hour_reset=datetime.utcnow() + timedelta(hours=1),
            day_reset=datetime.utcnow() + timedelta(hours=24),
        )
        mock_rate_limiter.check_rate_limit = AsyncMock(
            return_value=(RateLimitResult.DENIED, mock_status)
        )

        call_next = AsyncMock()

        await middleware.dispatch(request, call_next)

        log_msg = mock_logger.warning.call_args[0][0]
        assert "testuser" in log_msg
        assert "api-user-123" in log_msg


class TestIntegrationScenarios:
    """Integration-style tests for common scenarios."""

    @pytest.fixture
    def middleware(self):
        """Create a middleware instance."""
        app = MagicMock()
        return RateLimitMiddleware(app)

    @pytest.mark.asyncio
    @patch("app.middleware.rate_limit.rate_limiter")
    @patch("app.middleware.rate_limit.database")
    async def test_admin_session_bypasses_rate_limit(
        self, mock_db, mock_rate_limiter, middleware
    ):
        """Admin with session cookie should bypass rate limits."""
        request = MagicMock()
        request.url.path = "/api/v1/query"
        request.cookies.get.return_value = "admin-session-token"
        request.headers.get.return_value = ""
        request.query_params.get.return_value = None

        mock_db.get_auth_session.return_value = {"id": "admin-session"}

        # Rate limit check should return allowed for admin
        mock_status = RateLimitStatus(is_limited=False)
        mock_rate_limiter.check_rate_limit = AsyncMock(
            return_value=(RateLimitResult.ALLOWED, mock_status)
        )
        mock_rate_limiter.record_request = AsyncMock(return_value="req-id")
        mock_rate_limiter.complete_request = AsyncMock()
        mock_rate_limiter.get_rate_limit_status.return_value = mock_status
        mock_rate_limiter.get_limit_config.return_value = RateLimitConfig()

        mock_response = create_mock_response(b"admin response")
        call_next = AsyncMock(return_value=mock_response)

        response = await middleware.dispatch(request, call_next)

        # Should have been called with is_admin=True
        check_call = mock_rate_limiter.check_rate_limit.call_args
        assert check_call.kwargs["is_admin"] is True
        assert response.body == b"admin response"

    @pytest.mark.asyncio
    @patch("app.middleware.rate_limit.rate_limiter")
    @patch("app.middleware.rate_limit.database")
    async def test_api_user_rate_limited(
        self, mock_db, mock_rate_limiter, middleware
    ):
        """API user should be subject to rate limits."""
        api_key = "aih_test_key"

        request = MagicMock()
        request.url.path = "/api/v1/query"
        request.cookies.get.return_value = None
        request.headers.get.return_value = f"Bearer {api_key}"
        request.query_params.get.return_value = None

        mock_db.get_api_user_by_key_hash.return_value = {
            "id": "api-user-1",
            "username": "apiuser",
            "name": "API User"
        }

        mock_status = RateLimitStatus(
            minute_count=20,
            minute_remaining=0,
            is_limited=True,
            retry_after=60,
            minute_reset=datetime.utcnow() + timedelta(minutes=1),
            hour_reset=datetime.utcnow() + timedelta(hours=1),
            day_reset=datetime.utcnow() + timedelta(hours=24),
        )
        mock_rate_limiter.check_rate_limit = AsyncMock(
            return_value=(RateLimitResult.DENIED, mock_status)
        )

        call_next = AsyncMock()

        response = await middleware.dispatch(request, call_next)

        assert response.status_code == 429
        check_call = mock_rate_limiter.check_rate_limit.call_args
        assert check_call.kwargs["api_key_id"] == "api-user-1"
        assert check_call.kwargs["is_admin"] is False

    @pytest.mark.asyncio
    @patch("app.middleware.rate_limit.rate_limiter")
    @patch("app.middleware.rate_limit.database")
    async def test_websocket_auth_via_query_param(
        self, mock_db, mock_rate_limiter, middleware
    ):
        """WebSocket should authenticate via query param token."""
        request = MagicMock()
        request.url.path = "/ws/chat"
        request.cookies.get.return_value = None
        request.headers.get.return_value = ""
        request.query_params.get.return_value = "ws-session-token"

        mock_db.get_auth_session.return_value = {"id": "ws-session"}

        mock_status = RateLimitStatus(is_limited=False)
        mock_rate_limiter.check_rate_limit = AsyncMock(
            return_value=(RateLimitResult.ALLOWED, mock_status)
        )
        mock_rate_limiter.record_request = AsyncMock(return_value="req-id")
        mock_rate_limiter.complete_request = AsyncMock()
        mock_rate_limiter.get_rate_limit_status.return_value = mock_status
        mock_rate_limiter.get_limit_config.return_value = RateLimitConfig()

        mock_response = create_mock_response(b"ws response")
        call_next = AsyncMock(return_value=mock_response)

        await middleware.dispatch(request, call_next)

        check_call = mock_rate_limiter.check_rate_limit.call_args
        assert check_call.kwargs["user_id"] == "admin"
        assert check_call.kwargs["is_admin"] is True

    @pytest.mark.asyncio
    @patch("app.middleware.rate_limit.rate_limiter")
    @patch("app.middleware.rate_limit.database")
    async def test_full_request_lifecycle(
        self, mock_db, mock_rate_limiter, middleware
    ):
        """Test complete request lifecycle: check, record, process, complete."""
        request = MagicMock()
        request.url.path = "/api/v1/query"
        request.cookies.get.return_value = None
        request.headers.get.return_value = ""
        request.query_params.get.return_value = None

        mock_status = RateLimitStatus(
            minute_remaining=15,
            minute_reset=datetime.utcnow() + timedelta(minutes=1),
            hour_reset=datetime.utcnow() + timedelta(hours=1),
            day_reset=datetime.utcnow() + timedelta(hours=24),
        )
        mock_rate_limiter.check_rate_limit = AsyncMock(
            return_value=(RateLimitResult.ALLOWED, mock_status)
        )
        mock_rate_limiter.record_request = AsyncMock(return_value="request-123")
        mock_rate_limiter.complete_request = AsyncMock()
        mock_rate_limiter.get_rate_limit_status.return_value = mock_status
        mock_rate_limiter.get_limit_config.return_value = RateLimitConfig()

        mock_response = create_mock_response(b"success")
        call_next = AsyncMock(return_value=mock_response)

        response = await middleware.dispatch(request, call_next)

        # Verify lifecycle
        mock_rate_limiter.check_rate_limit.assert_called_once()
        mock_rate_limiter.record_request.assert_called_once()
        call_next.assert_called_once()
        mock_rate_limiter.complete_request.assert_called_once()

        # Verify record_request was called with correct args
        record_call = mock_rate_limiter.record_request.call_args
        assert record_call.kwargs["endpoint"] == "/api/v1/query"

        # Verify complete_request was called with request_id
        complete_call = mock_rate_limiter.complete_request.call_args
        assert complete_call.kwargs["request_id"] == "request-123"

        assert response.body == b"success"
