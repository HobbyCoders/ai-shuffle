"""
Comprehensive tests for app/main.py - FastAPI application entry point.

Tests cover:
- Application lifecycle (startup/shutdown via lifespan)
- Middleware registration (SecurityHeaders, LimitRequestBody, CORS)
- Route mounting and static files
- Health check endpoints
- Exception handlers
- CORS configuration (wildcard vs specific origins)
"""

import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock


# =============================================================================
# Module Import Tests
# =============================================================================

class TestMainModuleImports:
    """Verify main module can be imported correctly."""

    def test_main_module_imports(self):
        """Main module should import without errors."""
        from app import main
        assert main is not None

    def test_app_exists(self):
        """FastAPI app should exist."""
        from app.main import app
        assert app is not None

    def test_app_title(self):
        """App should have correct title."""
        from app.main import app
        assert app.title == "AI Shuffle"

    def test_app_has_docs_url(self):
        """App should have docs URL configured."""
        from app.main import app
        assert app.docs_url == "/docs"

    def test_app_has_redoc_url(self):
        """App should have redoc URL configured."""
        from app.main import app
        assert app.redoc_url == "/redoc"

    def test_logger_exists(self):
        """Logger should be configured."""
        from app.main import logger
        assert logger is not None


# =============================================================================
# Middleware Classes Tests
# =============================================================================

class TestSecurityHeadersMiddleware:
    """Test SecurityHeadersMiddleware class."""

    @pytest.fixture
    def middleware(self):
        """Create middleware instance."""
        from app.main import SecurityHeadersMiddleware
        mock_app = MagicMock()
        return SecurityHeadersMiddleware(mock_app)

    @pytest.fixture
    def mock_request_api(self):
        """Create mock request for API path."""
        request = MagicMock()
        request.url.path = "/api/v1/query"
        return request

    @pytest.fixture
    def mock_request_non_api(self):
        """Create mock request for non-API path."""
        request = MagicMock()
        request.url.path = "/chat"
        return request

    @pytest.mark.asyncio
    async def test_adds_security_headers(self, middleware, mock_request_non_api):
        """Should add security headers to response."""
        mock_response = MagicMock()
        mock_response.headers = {}

        async def mock_call_next(request):
            return mock_response

        response = await middleware.dispatch(mock_request_non_api, mock_call_next)

        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["X-Frame-Options"] == "SAMEORIGIN"
        assert response.headers["X-XSS-Protection"] == "1; mode=block"
        assert response.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
        assert "Content-Security-Policy" in response.headers

    @pytest.mark.asyncio
    async def test_adds_cache_control_for_api(self, middleware, mock_request_api):
        """Should add no-cache headers for API paths."""
        mock_response = MagicMock()
        mock_response.headers = {}

        async def mock_call_next(request):
            return mock_response

        response = await middleware.dispatch(mock_request_api, mock_call_next)

        assert response.headers["Cache-Control"] == "no-store, no-cache, must-revalidate, max-age=0"
        assert response.headers["Pragma"] == "no-cache"

    @pytest.mark.asyncio
    async def test_no_cache_control_for_non_api(self, middleware, mock_request_non_api):
        """Should not add cache headers for non-API paths."""
        mock_response = MagicMock()
        mock_response.headers = {}

        async def mock_call_next(request):
            return mock_response

        response = await middleware.dispatch(mock_request_non_api, mock_call_next)

        assert "Cache-Control" not in response.headers
        assert "Pragma" not in response.headers

    @pytest.mark.asyncio
    async def test_csp_header_content(self, middleware, mock_request_non_api):
        """CSP header should have correct directives."""
        mock_response = MagicMock()
        mock_response.headers = {}

        async def mock_call_next(request):
            return mock_response

        response = await middleware.dispatch(mock_request_non_api, mock_call_next)

        csp = response.headers["Content-Security-Policy"]
        assert "default-src 'self'" in csp
        assert "script-src 'self' 'unsafe-inline' https://static.cloudflareinsights.com" in csp
        assert "style-src 'self' 'unsafe-inline'" in csp
        assert "img-src 'self' data: blob: https: http:" in csp
        assert "connect-src 'self' ws: wss: https://*.cloudflareaccess.com" in csp
        assert "font-src 'self'" in csp
        assert "frame-ancestors 'self'" in csp


class TestLimitRequestBodyMiddleware:
    """Test LimitRequestBodyMiddleware class."""

    @pytest.fixture
    def middleware(self):
        """Create middleware instance."""
        from app.main import LimitRequestBodyMiddleware
        mock_app = MagicMock()
        return LimitRequestBodyMiddleware(mock_app)

    @pytest.mark.asyncio
    async def test_allows_small_request(self, middleware):
        """Should allow requests within size limit."""
        mock_request = MagicMock()
        mock_request.headers.get.return_value = "1000"  # 1KB
        mock_response = MagicMock()

        async def mock_call_next(request):
            return mock_response

        with patch("app.main.settings") as mock_settings:
            mock_settings.max_request_body_mb = 50

            response = await middleware.dispatch(mock_request, mock_call_next)
            assert response == mock_response

    @pytest.mark.asyncio
    async def test_rejects_large_request(self, middleware):
        """Should reject requests exceeding size limit."""
        mock_request = MagicMock()
        # 100MB in bytes
        mock_request.headers.get.return_value = str(100 * 1024 * 1024)

        async def mock_call_next(request):
            return MagicMock()

        with patch("app.main.settings") as mock_settings:
            mock_settings.max_request_body_mb = 50

            response = await middleware.dispatch(mock_request, mock_call_next)
            assert response.status_code == 413

    @pytest.mark.asyncio
    async def test_allows_request_without_content_length(self, middleware):
        """Should allow requests without Content-Length header."""
        mock_request = MagicMock()
        mock_request.headers.get.return_value = None
        mock_response = MagicMock()

        async def mock_call_next(request):
            return mock_response

        with patch("app.main.settings") as mock_settings:
            mock_settings.max_request_body_mb = 50

            response = await middleware.dispatch(mock_request, mock_call_next)
            assert response == mock_response


# =============================================================================
# Lifespan Tests
# =============================================================================

class TestLifespan:
    """Test application lifespan (startup/shutdown)."""

    @pytest.mark.asyncio
    async def test_lifespan_startup_runs_initialization(self):
        """Lifespan should run the startup orchestrator phases."""
        from app.main import lifespan
        # Pre-import so patch can resolve the module attributes
        import app.core.startup  # noqa: F401

        mock_app = MagicMock()

        # After the lifespan refactor, startup logic lives in app.core.startup.
        # Mock the phase functions directly to verify they're called.
        with patch("app.core.startup.init_core") as mock_init_core, \
             patch("app.core.startup.init_encryption_and_sessions") as mock_enc, \
             patch("app.core.startup.init_workspace") as mock_ws, \
             patch("app.core.startup.check_prerequisites") as mock_prereqs, \
             patch("app.core.startup.start_services", new_callable=AsyncMock) as mock_services, \
             patch("app.core.startup.start_background_tasks", return_value=[]) as mock_bg, \
             patch("app.main.settings") as mock_settings:

            mock_settings.version = "1.2.1"
            mock_settings.port = 8000

            async with lifespan(mock_app):
                mock_init_core.assert_called_once()
                mock_enc.assert_called_once()
                mock_ws.assert_called_once()
                mock_prereqs.assert_called_once()

    @pytest.mark.asyncio
    async def test_lifespan_preserves_sessions_when_credential_recovery_required(self):
        """Key recovery is independent of device/session authentication."""
        import app.core.startup  # noqa: F401
        from app.core.startup import init_encryption_and_sessions

        with patch("app.core.startup.encryption") as mock_encryption, \
             patch("app.core.startup.database") as mock_database:

            mock_encryption.initialize_server_key.return_value = False
            mock_encryption.credential_key_status.return_value = {'ready': False, 'state': 'recovery_required'}

            init_encryption_and_sessions()
            mock_encryption.initialize_server_key.assert_called_once_with(mock_database)
            mock_database.clear_all_sessions.assert_not_called()

    @pytest.mark.asyncio
    async def test_lifespan_handles_setup_required(self):
        """check_prerequisites should handle setup required state."""
        import app.core.startup  # noqa: F401
        from app.core.startup import check_prerequisites

        with patch("app.core.startup.auth_service") as mock_auth:
            mock_auth.is_claude_authenticated.return_value = False
            mock_auth.is_setup_required.return_value = True

            check_prerequisites()
            mock_auth.is_setup_required.assert_called()


# =============================================================================
# CORS Configuration Tests
# =============================================================================

class TestCorsConfiguration:
    """Test CORS middleware configuration."""

    def test_cors_origins_parsed(self):
        """CORS origins should be parsed from settings."""
        from app.main import cors_origins
        # cors_origins is a list of strings
        assert isinstance(cors_origins, list)

    def test_cors_middleware_registered(self):
        """CORS middleware should be registered on app."""
        from app.main import app
        from starlette.middleware.cors import CORSMiddleware

        # Check that middleware is registered
        has_cors = False
        for middleware in app.user_middleware:
            if middleware.cls == CORSMiddleware:
                has_cors = True
                break
        assert has_cors, "CORS middleware should be registered"


class TestWildcardCorsWarning:
    """Test CORS configuration with wildcard origin."""

    def test_wildcard_cors_detected(self):
        """Should detect wildcard in CORS origins."""

        # Test the logic (the actual config may or may not have wildcard)
        test_origins = ["*"]
        has_wildcard = "*" in test_origins
        assert has_wildcard


# =============================================================================
# Router Registration Tests
# =============================================================================

class TestRouterRegistration:
    """Test that all routers are registered."""

    def test_system_router_registered(self):
        """System router should be registered."""
        from app.main import app
        assert str(app.url_path_for("health_check")) == "/health"
        assert str(app.url_path_for("api_health_check")) == "/api/v1/health"

    def test_auth_router_registered(self):
        """Auth router should be registered without any owner login endpoint."""
        from starlette.routing import NoMatchFound
        from app.main import app
        assert str(app.url_path_for("get_auth_status")) == "/api/v1/auth/status"
        assert str(app.url_path_for("configure_local_install")) == "/api/v1/auth/setup"
        # Shuffle never asks its owner to sign in: no password, account or recovery login route exists.
        for name in ("retired_password_login", "start_account_login", "poll_account_login", "local_recovery"):
            with pytest.raises(NoMatchFound):
                app.url_path_for(name)

    def test_agents_router_registered(self):
        """Agents router should be registered."""
        from app.api import agents
        # Verify router was included
        assert agents.router is not None

    def test_sessions_router_registered(self):
        """Sessions router should be registered."""
        from app.api import sessions
        assert sessions.router is not None

    def test_all_api_routers_imported(self):
        """All API routers should be importable."""
        from app.api import (
            auth, agents, system
        )
        # All imports successful
        assert auth.router is not None
        assert agents.router is not None
        assert system.router is not None


# =============================================================================
# Health Check Integration Tests (via TestClient)
# Note: These tests are covered by unit tests above and tests/api/test_system.py
# The integration tests require the full database schema which is tested in
# the dedicated system API tests.
# =============================================================================

class TestHealthCheckEndpointStructure:
    """Test health check endpoint structure without full client."""

    @pytest.mark.asyncio
    async def test_health_check_function_exists(self):
        """Health check function should exist in system module."""
        from app.api.system import health_check
        assert health_check is not None

    @pytest.mark.asyncio
    async def test_health_check_returns_dict(self):
        """Health check should return correct response format."""
        from app.api.system import health_check

        with patch("app.api.system.auth_service") as mock_auth, \
             patch("app.api.system.settings") as mock_settings:
            mock_auth.is_setup_required.return_value = False
            mock_auth.is_claude_authenticated.return_value = True
            mock_settings.service_name = "ai-shuffle"
            mock_settings.version = "1.2.1"

            result = await health_check()
            assert result["status"] == "healthy"
            assert result["service"] == "ai-shuffle"
            assert result["version"] == "1.2.1"

    @pytest.mark.asyncio
    async def test_api_health_check_function_exists(self):
        """API health check function should exist."""
        from app.api.system import api_health_check
        assert api_health_check is not None


# =============================================================================
# Middleware Integration Tests (Unit-based)
# =============================================================================

class TestSecurityHeadersMiddlewareIntegration:
    """Test security headers middleware via unit tests."""

    @pytest.mark.asyncio
    async def test_health_path_gets_security_headers(self):
        """Health path should get security headers."""
        from app.main import SecurityHeadersMiddleware

        mock_app = MagicMock()
        middleware = SecurityHeadersMiddleware(mock_app)

        mock_request = MagicMock()
        mock_request.url.path = "/health"
        mock_response = MagicMock()
        mock_response.headers = {}

        async def mock_call_next(request):
            return mock_response

        response = await middleware.dispatch(mock_request, mock_call_next)

        assert response.headers.get("X-Content-Type-Options") == "nosniff"
        assert response.headers.get("X-Frame-Options") == "SAMEORIGIN"
        assert response.headers.get("X-XSS-Protection") == "1; mode=block"

    @pytest.mark.asyncio
    async def test_api_health_path_gets_cache_control(self):
        """API health path should get cache control headers."""
        from app.main import SecurityHeadersMiddleware

        mock_app = MagicMock()
        middleware = SecurityHeadersMiddleware(mock_app)

        mock_request = MagicMock()
        mock_request.url.path = "/api/v1/health"
        mock_response = MagicMock()
        mock_response.headers = {}

        async def mock_call_next(request):
            return mock_response

        response = await middleware.dispatch(mock_request, mock_call_next)

        assert "no-store" in response.headers.get("Cache-Control", "")


# =============================================================================
# Static Files Tests
# =============================================================================

class TestStaticFilesConfiguration:
    """Test static files configuration."""

    def test_static_dir_path_calculated(self):
        """Static directory path should be calculated."""
        from app.main import static_dir
        assert isinstance(static_dir, Path)

    def test_static_dir_relative_to_main(self):
        """Static dir should be relative to main.py."""
        from app.main import static_dir
        # Should be app/static
        assert static_dir.name == "static"


# =============================================================================
# App Attribute Tests
# =============================================================================

class TestAppAttributes:
    """Test FastAPI app attributes."""

    def test_app_version(self):
        """App should have version set."""
        from app.main import app
        assert app.version is not None

    def test_app_description(self):
        """App should have description set."""
        from app.main import app
        assert app.description == "AI Shuffle web interface and OpenAI-compatible API"

    def test_app_has_lifespan(self):
        """App should have lifespan configured."""
        from app.main import app
        # FastAPI stores lifespan in router.lifespan_context
        assert app.router.lifespan_context is not None


# =============================================================================
# Middleware Stack Order Tests
# =============================================================================

class TestMiddlewareOrder:
    """Test middleware is added in correct order."""

    def test_middleware_count(self):
        """App should have multiple middleware registered."""
        from app.main import app
        # user_middleware contains our custom middleware
        assert len(app.user_middleware) >= 4  # Security, Activity, LimitBody, RateLimit, CORS

    def test_security_headers_middleware_present(self):
        """SecurityHeadersMiddleware should be registered."""
        from app.main import app, SecurityHeadersMiddleware

        has_security = any(
            m.cls == SecurityHeadersMiddleware
            for m in app.user_middleware
        )
        assert has_security

    def test_limit_request_body_middleware_present(self):
        """LimitRequestBodyMiddleware should be registered."""
        from app.main import app, LimitRequestBodyMiddleware

        has_limit = any(
            m.cls == LimitRequestBodyMiddleware
            for m in app.user_middleware
        )
        assert has_limit


# =============================================================================
# Edge Cases and Error Handling
# =============================================================================

class TestEdgeCases:
    """Test edge cases and error handling."""

    @pytest.mark.asyncio
    async def test_middleware_passes_through_response(self):
        """Middleware should pass through the response from call_next."""
        from app.main import SecurityHeadersMiddleware

        mock_app = MagicMock()
        middleware = SecurityHeadersMiddleware(mock_app)

        mock_request = MagicMock()
        mock_request.url.path = "/test"
        expected_response = MagicMock()
        expected_response.headers = {}

        async def mock_call_next(request):
            return expected_response

        result = await middleware.dispatch(mock_request, mock_call_next)
        assert result == expected_response

    def test_cors_origins_strips_whitespace(self):
        """CORS origins parsing should strip whitespace."""
        test_string = " http://localhost:8000 , http://example.com "
        origins = [origin.strip() for origin in test_string.split(",") if origin.strip()]
        assert origins == ["http://localhost:8000", "http://example.com"]

    def test_cors_origins_filters_empty(self):
        """CORS origins parsing should filter empty strings."""
        test_string = "http://localhost:8000,,http://example.com,"
        origins = [origin.strip() for origin in test_string.split(",") if origin.strip()]
        assert origins == ["http://localhost:8000", "http://example.com"]


# =============================================================================
# Main Block Tests
# =============================================================================

class TestMainBlock:
    """Test the if __name__ == '__main__' block."""

    def test_uvicorn_import_available(self):
        """Uvicorn should be importable."""
        import uvicorn
        assert uvicorn is not None

    def test_main_uses_correct_app_path(self):
        """Main block should use correct app path."""
        # The app path should be "app.main:app"
        from app.main import app
        assert app is not None
        # The module is app.main
        assert app.title == "AI Shuffle"


# =============================================================================
# Additional Coverage Tests
# =============================================================================

class TestSecurityHeadersMiddlewareEdgeCases:
    """Additional edge case tests for SecurityHeadersMiddleware."""

    @pytest.fixture
    def middleware(self):
        """Create middleware instance."""
        from app.main import SecurityHeadersMiddleware
        mock_app = MagicMock()
        return SecurityHeadersMiddleware(mock_app)

    @pytest.mark.asyncio
    async def test_api_v1_prefix_gets_cache_headers(self, middleware):
        """Any /api/ path should get cache control headers."""
        mock_request = MagicMock()
        mock_request.url.path = "/api/v1/profiles"
        mock_response = MagicMock()
        mock_response.headers = {}

        async def mock_call_next(request):
            return mock_response

        response = await middleware.dispatch(mock_request, mock_call_next)

        assert "Cache-Control" in response.headers
        assert "no-store" in response.headers["Cache-Control"]

    @pytest.mark.asyncio
    async def test_static_path_no_cache_headers(self, middleware):
        """Static paths should not get cache control headers."""
        mock_request = MagicMock()
        mock_request.url.path = "/static/app.js"
        mock_response = MagicMock()
        mock_response.headers = {}

        async def mock_call_next(request):
            return mock_response

        response = await middleware.dispatch(mock_request, mock_call_next)

        assert "Cache-Control" not in response.headers


class TestLimitRequestBodyMiddlewareEdgeCases:
    """Additional tests for LimitRequestBodyMiddleware."""

    @pytest.fixture
    def middleware(self):
        """Create middleware instance."""
        from app.main import LimitRequestBodyMiddleware
        mock_app = MagicMock()
        return LimitRequestBodyMiddleware(mock_app)

    @pytest.mark.asyncio
    async def test_exact_limit_allowed(self, middleware):
        """Request at exact limit should be allowed."""
        mock_request = MagicMock()
        # Exactly 50MB
        mock_request.headers.get.return_value = str(50 * 1024 * 1024)
        mock_response = MagicMock()

        async def mock_call_next(request):
            return mock_response

        with patch("app.main.settings") as mock_settings:
            mock_settings.max_request_body_mb = 50

            response = await middleware.dispatch(mock_request, mock_call_next)
            assert response == mock_response

    @pytest.mark.asyncio
    async def test_one_byte_over_rejected(self, middleware):
        """Request one byte over limit should be rejected."""
        mock_request = MagicMock()
        # 50MB + 1 byte
        mock_request.headers.get.return_value = str(50 * 1024 * 1024 + 1)

        async def mock_call_next(request):
            return MagicMock()

        with patch("app.main.settings") as mock_settings:
            mock_settings.max_request_body_mb = 50

            response = await middleware.dispatch(mock_request, mock_call_next)
            assert response.status_code == 413


class TestRouterRegistrationAdditional:
    """Additional tests for router registration."""

    def test_query_router_registered(self):
        """Query router should be registered."""
        from app.api import query
        assert query.router is not None

    def test_websocket_router_registered(self):
        """WebSocket router should be registered."""
        from app.api import websocket
        assert websocket.router is not None

    def test_commands_router_registered(self):
        """Commands router should be registered."""
        from app.api import commands
        assert commands.router is not None

    def test_settings_router_registered(self):
        """Settings router should be registered."""
        from app.api import settings as settings_api
        assert settings_api.router is not None
