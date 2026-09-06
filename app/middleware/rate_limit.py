"""
Rate limit middleware for FastAPI.

Checks rate limits before processing requests and adds rate limit headers to responses.
"""

import hashlib
import logging
import time
from collections import OrderedDict
from typing import Callable, Optional, Tuple

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from app.core.rate_limiter import rate_limiter, RateLimitResult, RateLimitStatus
from app.db import database
from app.db.async_database import run_sync

logger = logging.getLogger(__name__)


UserInfo = Tuple[Optional[str], Optional[str], bool]


class _AuthLookupCache:
    """Short-TTL positive cache for the middleware's per-request auth lookup.

    Why: ``_get_user_info`` runs on *every* request and, uncached, issues one
    or more ``run_sync(database.get_auth_session, ...)`` round-trips. Each runs
    on an executor thread that checks out its own PG connection, so under
    concurrency this per-request threaded auth lookup transiently exhausts the
    20-slot pool (observed PoolError). This cache collapses repeat lookups for
    the same credentials into a single DB hit per ``ttl`` window.

    Scope: this only feeds *rate-limit tier classification*. It does NOT affect
    authentication enforcement — ``require_auth`` validates the session live on
    every protected endpoint (see PR notes).

    Positive-only: anonymous / failed lookups are never stored, so a
    just-created (or just-revoked-then-recreated) session takes effect
    immediately rather than being masked by a cached negative.

    Thread-safety: every access happens on the asyncio event loop (from the
    middleware's async ``dispatch``), so no lock is needed.
    """

    def __init__(
        self,
        ttl: float = 5.0,
        max_entries: int = 4096,
        time_func: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl
        self._max = max_entries
        self._now = time_func
        # key -> (expires_at, user_info); ordered for cheap LRU eviction.
        self._store: "OrderedDict[str, Tuple[float, UserInfo]]" = OrderedDict()

    def get(self, key: str) -> Optional[UserInfo]:
        entry = self._store.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if expires_at <= self._now():
            self._store.pop(key, None)
            return None
        self._store.move_to_end(key)
        return value

    def set(self, key: str, value: UserInfo) -> None:
        now = self._now()
        self._store[key] = (now + self._ttl, value)
        self._store.move_to_end(key)
        self._prune(now)

    def invalidate(self, key: str) -> None:
        self._store.pop(key, None)

    def clear(self) -> None:
        self._store.clear()

    def _prune(self, now: float) -> None:
        # Drop expired entries from the front, then hard-cap the size.
        while self._store:
            oldest_key = next(iter(self._store))
            expires_at, _ = self._store[oldest_key]
            if expires_at <= now:
                self._store.popitem(last=False)
            else:
                break
        while len(self._store) > self._max:
            self._store.popitem(last=False)


# Module-level singleton shared by the middleware and the logout purge helper.
_auth_lookup_cache = _AuthLookupCache()


def _auth_cache_key(
    session_token: Optional[str],
    header_token: Optional[str],
    query_token: Optional[str],
) -> str:
    """Hash the credentials that determine a lookup result into a cache key.

    We never key on the raw secret; the digest keeps tokens out of the map.
    All three sources are folded in so distinct credential combinations can't
    collide onto one another's cached result.
    """
    h = hashlib.sha256()
    for part in (session_token, header_token, query_token):
        h.update((part or "").encode("utf-8"))
        h.update(b"\x1f")
    return h.hexdigest()


def invalidate_auth_cache_for_session(session_token: Optional[str]) -> None:
    """Purge the cached auth lookup for a logged-out/revoked session cookie.

    Covers the common cookie-only request (no Bearer/query token); any other
    credential combination simply expires within the TTL. Safe no-op if absent.
    """
    if not session_token:
        return
    _auth_lookup_cache.invalidate(_auth_cache_key(session_token, None, None))


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

        Reuses auth info already resolved by the auth dependency (stored on
        request.state) to avoid redundant DB queries.  Falls back to its own
        resolution when request.state has not been populated yet.

        Returns:
            Tuple of (user_id, api_key_id, is_admin)
        """
        # Reuse auth info if already resolved by the auth dependency
        if getattr(request.state, "user_info", None) is not None:
            return request.state.user_info
        from app.core import owner_auth
        if owner_auth.local_access(request):
            return ('local-owner', None, True)

        # Extract every credential source up front so we can (a) build a stable
        # cache key and (b) short-circuit the threaded DB lookups on a hit.
        session_token = request.cookies.get("session")
        auth_header = request.headers.get("Authorization", "")
        header_token = auth_header[7:] if auth_header.startswith("Bearer ") else None
        query_token = request.query_params.get("token")

        cache_key: Optional[str] = None
        if session_token or header_token or query_token:
            cache_key = _auth_cache_key(session_token, header_token, query_token)
            cached = _auth_lookup_cache.get(cache_key)
            if cached is not None:
                request.state.user_info = cached
                return cached

        user_id = None
        api_key_id = None
        is_admin = False

        # Check session cookie (admin auth)
        if session_token:
            from app.core.pairing import PAIRED_SESSION_PREFIX, hash_controller_session
            if session_token.startswith(PAIRED_SESSION_PREFIX):
                client = await run_sync(
                    database.get_paired_client_by_session_hash,
                    hash_controller_session(session_token),
                )
                if client:
                    api_key_id = client["id"]
                    user_id = client.get("device_name") or "paired-device"
            else:
                session = await run_sync(database.get_auth_session, session_token)
                if session:
                    is_admin = True
                    user_id = "admin"

        # Check API key header
        if header_token:
            token = header_token
            from app.core.pairing import PAIRED_SESSION_PREFIX, hash_controller_session
            if token.startswith(PAIRED_SESSION_PREFIX):
                client = await run_sync(
                    database.get_paired_client_by_session_hash,
                    hash_controller_session(token),
                )
                if client:
                    api_key_id = client["id"]
                    user_id = client.get("device_name") or "paired-device"
            elif token.startswith("aih_"):
                # API key authentication
                key_hash = hashlib.sha256(token.encode()).hexdigest()
                api_user = await run_sync(database.get_api_user_by_key_hash, key_hash)
                if api_user:
                    api_key_id = api_user["id"]
                    user_id = api_user.get("username") or api_user["name"]
            else:
                # Check if it's a session token
                api_session = await run_sync(database.get_api_key_session, token)
                if api_session:
                    api_user = await run_sync(database.get_api_user, api_session["api_user_id"])
                    if api_user:
                        api_key_id = api_user["id"]
                        user_id = api_user.get("username") or api_user["name"]

        # Check query parameter for WebSocket connections
        if not user_id and query_token:
            token = query_token
            from app.core.pairing import PAIRED_SESSION_PREFIX, hash_controller_session
            if token.startswith(PAIRED_SESSION_PREFIX):
                client = await run_sync(
                    database.get_paired_client_by_session_hash,
                    hash_controller_session(token),
                )
                if client:
                    api_key_id = client["id"]
                    user_id = client.get("device_name") or "paired-device"
            else:
                # Check admin session
                session = await run_sync(database.get_auth_session, token)
                if session:
                    is_admin = True
                    user_id = "admin"
                else:
                    # Check API key session
                    api_session = await run_sync(database.get_api_key_session, token)
                    if api_session:
                        api_user = await run_sync(database.get_api_user, api_session["api_user_id"])
                        if api_user:
                            api_key_id = api_user["id"]
                            user_id = api_user.get("username") or api_user["name"]

        result = (user_id, api_key_id, is_admin)
        # Cache POSITIVE results only — never store anonymous/failed lookups so a
        # freshly created session works immediately (see _AuthLookupCache).
        if cache_key is not None and user_id is not None:
            _auth_lookup_cache.set(cache_key, result)
        # Cache on request.state so downstream code can reuse within this request
        request.state.user_info = result
        return result

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
