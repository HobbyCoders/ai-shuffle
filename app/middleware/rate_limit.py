"""
Rate limit middleware for FastAPI.

Checks rate limits before processing requests and adds rate limit headers to responses.
"""

import hashlib
import logging
import time
from typing import Optional, Tuple

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from app.core.rate_limiter import rate_limiter, RateLimitResult, RateLimitStatus
from app.db import database

logger = logging.getLogger(__name__)


# Endpoints to skip rate limiting
SKIP_PATHS = {
    "/health",
    "/api/v1/health",
    "/docs",
    "/redoc",
    "/openapi.json",
    "/favicon.ico",
    "/favicon.svg",
}

# Prefixes to skip (static files, etc)
SKIP_PREFIXES = (
    "/_app/",
    "/static/",
)

# Endpoints that are rate limited (API endpoints that cost resources)
RATE_LIMITED_PREFIXES = (
    "/api/v1/query",
    "/api/v1/conversation",
    "/ws/chat",
)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    Middleware that enforces rate limits on API requests.

    - Checks rate limits before processing requests
    - Returns 429 Too Many Requests when limits are exceeded
    - Adds X-RateLimit-* headers to responses
    - Skips rate limiting for health checks and static files
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        """Process request with rate limiting"""
        path = request.url.path

        # Skip rate limiting for excluded paths
        if path in SKIP_PATHS or path.startswith(SKIP_PREFIXES):
            return await call_next(request)

        # Only rate limit specific expensive endpoints
        should_rate_limit = any(path.startswith(prefix) for prefix in RATE_LIMITED_PREFIXES)

        if not should_rate_limit:
            # Still add rate limit headers but don't enforce
            response = await call_next(request)
            user_id, api_key_id, is_admin = await self._get_user_info(request)
            self._add_rate_limit_headers(response, user_id, api_key_id, is_admin)
            return response

        # Get user info from request
        user_id, api_key_id, is_admin = await self._get_user_info(request)

        # Check rate limits
        result, status = await rate_limiter.check_rate_limit(
            user_id=user_id,
            api_key_id=api_key_id,
            is_admin=is_admin
        )

        if result == RateLimitResult.DENIED:
            logger.warning(
                f"Rate limit exceeded for user={user_id}, api_key={api_key_id}, "
                f"minute={status.minute_count}, hour={status.hour_count}, day={status.day_count}"
            )
            return self._create_rate_limit_response(status)

        # Record the request
        start_time = time.time()
        request_id = await rate_limiter.record_request(
            user_id=user_id,
            api_key_id=api_key_id,
            endpoint=path,
            is_admin=is_admin
        )

        try:
            response = await call_next(request)
        finally:
            # Complete the request (decrease concurrent count)
            duration_ms = int((time.time() - start_time) * 1000)
            await rate_limiter.complete_request(
                user_id=user_id,
                api_key_id=api_key_id,
                request_id=request_id,
                duration_ms=duration_ms
            )

        # Add rate limit headers
        self._add_rate_limit_headers(response, user_id, api_key_id, is_admin)

        return response

    async def _get_user_info(self, request: Request) -> Tuple[Optional[str], Optional[str], bool]:
        """
        Extract user information from request.

        Returns:
            Tuple of (user_id, api_key_id, is_admin)
        """
        user_id = None
        api_key_id = None
        is_admin = False

        # Check session cookie (admin auth)
        session_token = request.cookies.get("session")
        if session_token:
            session = database.get_auth_session(session_token)
            if session:
                is_admin = True
                user_id = "admin"

        # Check API key header
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
            if token.startswith("aih_"):
                # API key authentication
                key_hash = hashlib.sha256(token.encode()).hexdigest()
                api_user = database.get_api_user_by_key_hash(key_hash)
                if api_user:
                    api_key_id = api_user["id"]
                    user_id = api_user.get("username") or api_user["name"]
            else:
                # Check if it's a session token
                api_session = database.get_api_key_session(token)
                if api_session:
                    api_user = database.get_api_user(api_session["api_user_id"])
                    if api_user:
                        api_key_id = api_user["id"]
                        user_id = api_user.get("username") or api_user["name"]

        # Check query parameter for WebSocket connections
        if not user_id:
            token = request.query_params.get("token")
            if token:
                # Check admin session
                session = database.get_auth_session(token)
                if session:
                    is_admin = True
                    user_id = "admin"
                else:
                    # Check API key session
                    api_session = database.get_api_key_session(token)
                    if api_session:
                        api_user = database.get_api_user(api_session["api_user_id"])
                        if api_user:
                            api_key_id = api_user["id"]
                            user_id = api_user.get("username") or api_user["name"]

        return user_id, api_key_id, is_admin

    def _add_rate_limit_headers(
        self,
        response: Response,
        user_id: Optional[str],
        api_key_id: Optional[str],
        is_admin: bool
    ) -> None:
        """Add rate limit headers to response"""
        status = rate_limiter.get_rate_limit_status(
            user_id=user_id,
            api_key_id=api_key_id,
            is_admin=is_admin
        )

        config = rate_limiter.get_limit_config(user_id, api_key_id)

        # Standard rate limit headers
        response.headers["X-RateLimit-Limit"] = str(config.requests_per_minute)
        response.headers["X-RateLimit-Remaining"] = str(status.minute_remaining)
        response.headers["X-RateLimit-Reset"] = str(int(status.minute_reset.timestamp()))

        # Extended headers
        response.headers["X-RateLimit-Limit-Hour"] = str(config.requests_per_hour)
        response.headers["X-RateLimit-Remaining-Hour"] = str(status.hour_remaining)
        response.headers["X-RateLimit-Limit-Day"] = str(config.requests_per_day)
        response.headers["X-RateLimit-Remaining-Day"] = str(status.day_remaining)

    def _create_rate_limit_response(self, status: RateLimitStatus) -> JSONResponse:
        """Create a 429 Too Many Requests response"""
        response = JSONResponse(
            status_code=429,
            content={
                "detail": "Rate limit exceeded",
                "retry_after": status.retry_after,
                "limits": {
                    "minute": {
                        "remaining": status.minute_remaining,
                        "reset": status.minute_reset.isoformat()
                    },
                    "hour": {
                        "remaining": status.hour_remaining,
                        "reset": status.hour_reset.isoformat()
                    },
                    "day": {
                        "remaining": status.day_remaining,
                        "reset": status.day_reset.isoformat()
                    }
                }
            }
        )

        # Add required headers
        response.headers["Retry-After"] = str(status.retry_after)
        response.headers["X-RateLimit-Remaining"] = "0"
        response.headers["X-RateLimit-Reset"] = str(int(status.minute_reset.timestamp()))

        return response
