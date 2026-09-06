"""
CSRF (Cross-Site Request Forgery) protection middleware.

Validates that state-changing requests (POST, PUT, DELETE, PATCH) originate
from an allowed origin by checking the Origin and Referer headers.

This is defense-in-depth on top of SameSite=Lax cookies. Bearer-token
authenticated requests (API keys) are inherently CSRF-resistant and are
exempt from this check.
"""

import logging
from urllib.parse import urlparse
from typing import Set

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.core.config import settings

logger = logging.getLogger(__name__)

# HTTP methods that change state and require CSRF validation.
_UNSAFE_METHODS = frozenset({"POST", "PUT", "DELETE", "PATCH"})

# Paths that are exempt from CSRF checks (e.g. webhook receivers that use
# their own secret-based authentication, or OAuth callbacks).
_EXEMPT_PATHS = frozenset({
    "/api/v1/webhooks/receive",  # Inbound webhooks authenticate via secret
    # Native mobile device registration: the request that *obtains* the mobile
    # API key, so it has no Bearer token yet and no browser Origin header. It
    # uses no cookie auth and returns a fresh key (unreadable cross-origin via
    # CORS), so it carries no CSRF risk. Every other /api/v1/mobile/* endpoint
    # is a safe GET or already Bearer-authenticated (exempt below).
    "/api/v1/mobile/register",
    # Account enrollment is authenticated by a one-use ticket in its body,
    # never by a browser cookie. Native phones have no Origin header here.
    "/api/v1/mobile/hub/enroll",
    # Pair exchange authenticates with a one-time, high-entropy grant in the
    # request body and is called before the new device owns a session cookie.
    "/api/v1/pairing/exchange",
})

# Prefixes that are exempt from CSRF checks.
_EXEMPT_PREFIXES = (
    "/api/v1/connectors/webhook/",  # Connector webhook callbacks
)


def _get_allowed_origins() -> Set[str]:
    """Build the set of allowed origins from CORS configuration.

    Returns normalized origin strings (scheme + host + port, no trailing slash).
    """
    origins = set()
    for raw in settings.cors_origins.split(","):
        raw = raw.strip()
        if not raw or raw == "*":
            continue
        # Normalize: strip trailing slashes and paths
        parsed = urlparse(raw)
        if parsed.scheme and parsed.netloc:
            origins.add(f"{parsed.scheme}://{parsed.netloc}")
    return origins


def _extract_origin(header_value: str) -> str:
    """Extract the origin (scheme://host[:port]) from an Origin or Referer header."""
    parsed = urlparse(header_value)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return ""


class CSRFProtectionMiddleware(BaseHTTPMiddleware):
    """Reject state-changing requests that don't originate from an allowed origin.

    Checks:
    1. Skips safe methods (GET, HEAD, OPTIONS).
    2. Skips requests authenticated via Bearer token (API keys are CSRF-safe).
    3. Skips WebSocket upgrade requests (authenticated via token query param).
    4. Skips exempt paths (webhooks with their own auth).
    5. Extracts Origin (or Referer) from request headers.
    6. Allows requests with no Origin from localhost clients.
    7. Allows same-origin requests (Origin matches Host header) — covers
       Docker and reverse-proxy deployments without explicit CORS_ORIGINS.
    8. Validates Origin against the allowed origins from CORS_ORIGINS config.

    If CORS_ORIGINS contains "*" (development mode), CSRF checks are
    disabled entirely with a warning logged at startup.
    """

    def __init__(self, app, **kwargs):
        super().__init__(app, **kwargs)
        self._allowed_origins = _get_allowed_origins()
        self._wildcard_mode = "*" in settings.cors_origins.split(",")

        if self._wildcard_mode:
            logger.warning(
                "CSRF protection disabled: CORS_ORIGINS contains wildcard '*'. "
                "This is only safe for local development."
            )
        elif self._allowed_origins:
            logger.info(f"CSRF protection enabled for origins: {self._allowed_origins}")
        else:
            logger.warning("CSRF protection: no valid origins configured in CORS_ORIGINS")

    @staticmethod
    def _is_same_origin(request: Request, request_origin: str) -> bool:
        """Check if the request Origin matches the Host header.

        When the frontend is served by the same backend (SPA served from
        FastAPI static files), every legitimate browser request is same-origin.
        Comparing Origin against Host is the standard same-origin verification
        and is inherently CSRF-safe — a cross-origin attacker cannot forge the
        Origin header to match the victim's Host.

        Handles both direct access (http://host:port) and reverse-proxy setups
        where X-Forwarded-Host / X-Forwarded-Proto are set.
        """
        # Prefer forwarded headers (set by reverse proxies like nginx/traefik)
        forwarded_host = request.headers.get("x-forwarded-host", "")
        forwarded_proto = request.headers.get("x-forwarded-proto", "")
        host_header = request.headers.get("host", "")

        # Determine scheme: prefer X-Forwarded-Proto (set by reverse proxies
        # and Cloudflare Tunnel), fall back to the actual request scheme.
        scheme = forwarded_proto or request.url.scheme or "http"

        # Build candidate origins to check against. Some reverse proxies
        # (e.g. Unraid, nginx) may send comma-separated X-Forwarded-Host
        # values when there are multiple proxy hops.
        candidates = set()
        if forwarded_host:
            for host in forwarded_host.split(","):
                host = host.strip()
                if host:
                    candidates.add(f"{scheme}://{host}")
        if host_header:
            candidates.add(f"{scheme}://{host_header}")

        if not candidates:
            return False

        is_match = request_origin in candidates
        if not is_match and candidates:
            logger.debug(
                f"CSRF same-origin mismatch: request Origin {request_origin!r} "
                f"does not match any host candidate: {candidates}"
            )
        return is_match

    async def dispatch(self, request: Request, call_next) -> Response:
        # 1. Safe methods — no CSRF risk
        if request.method not in _UNSAFE_METHODS:
            return await call_next(request)

        # 2. Development mode (wildcard CORS) — skip checks
        if self._wildcard_mode:
            return await call_next(request)

        # 3. Bearer-token auth (API keys) — inherently CSRF-resistant.
        #    Reuse auth info from request.state if already resolved by the
        #    auth dependency, avoiding a redundant header check.
        if getattr(request.state, "auth_token", None) and not getattr(request.state, "is_admin", False):
            # API-key authenticated request — CSRF-safe
            return await call_next(request)
        auth_header = request.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            return await call_next(request)

        # 4. WebSocket upgrade — handled by WS-specific auth
        if request.headers.get("upgrade", "").lower() == "websocket":
            return await call_next(request)

        # 5. Exempt paths
        path = request.url.path
        if path in _EXEMPT_PATHS or path.startswith(_EXEMPT_PREFIXES):
            return await call_next(request)

        # 6. Validate Origin or Referer
        origin_header = request.headers.get("origin", "")
        referer_header = request.headers.get("referer", "")

        request_origin = ""
        if origin_header:
            request_origin = _extract_origin(origin_header)
        elif referer_header:
            request_origin = _extract_origin(referer_header)

        if not request_origin:
            # No Origin or Referer header — could be a same-origin request from
            # older browsers or tools like curl. Allow requests from localhost
            # when no origin header is present (common for dev tools and Postman).
            # In production behind a reverse proxy, Origin should always be present.
            client_host = request.client.host if request.client else ""
            if client_host in ("127.0.0.1", "::1", "localhost"):
                return await call_next(request)

            logger.warning(
                f"CSRF blocked: no Origin/Referer header on {request.method} {path} "
                f"from {client_host}"
            )
            return JSONResponse(
                status_code=403,
                content={"detail": "CSRF validation failed: missing Origin header"},
            )

        # 7. Same-origin check — if the Origin matches the Host the browser
        #    connected to, this is by definition a same-origin request and is
        #    CSRF-safe.  This covers Docker / reverse-proxy deployments where
        #    the user accesses the app on an origin not listed in CORS_ORIGINS
        #    (e.g. http://192.168.1.50:8000 or https://my-server.local).
        if self._is_same_origin(request, request_origin):
            return await call_next(request)

        if request_origin not in self._allowed_origins:
            host_header = request.headers.get("host", "")
            forwarded_host = request.headers.get("x-forwarded-host", "")
            logger.warning(
                f"CSRF blocked: origin {request_origin!r} not in allowed origins "
                f"{self._allowed_origins or '{}'} for {request.method} {path}. "
                f"Host={host_header!r}, X-Forwarded-Host={forwarded_host!r}. "
                f"Set CORS_ORIGINS to include {request_origin} to fix this."
            )
            return JSONResponse(
                status_code=403,
                content={
                    "detail": (
                        f"CSRF validation failed: origin {request_origin} not allowed. "
                        f"Server sees Host={host_header!r}, X-Forwarded-Host={forwarded_host!r}. "
                        f"Ensure CORS_ORIGINS includes {request_origin} or fix your reverse proxy "
                        f"to forward the correct Host header."
                    )
                },
            )

        return await call_next(request)
