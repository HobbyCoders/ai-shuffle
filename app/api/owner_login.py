"""Owner password: first-run creation, sign-in, change, LAN trust and the
desktop's "Open in Browser" handoff.

Routes (all under ``/api/v1/owner``):

- ``GET  /status``        public; what the web UI shows (sign-in, setup, app).
- ``POST /password``      create the first password (a direct local-network
                          browser, or any owner) or change it (owner plus the
                          current password).
- ``POST /login``         password sign-in; sets the ``session`` cookie and,
                          on request, returns the token for Bearer use.
- ``PUT  /trusted-lan``   the "trusted LAN, no login" toggle (owner only).
- ``POST /handoff``       desktop app only: a one-time URL for the browser.
- ``GET  /handoff``       redeems that URL into a browser session.

Sign-out is the existing ``POST /api/v1/auth/logout``. Password checks are
rate limited per client address and in total.
"""
from __future__ import annotations

import ipaddress
import logging
import secrets
import threading
import time
from collections import defaultdict, deque
from typing import Deque, Dict, Optional
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from app.core import audit_service, desktop_listener, owner_auth
from app.core.config import settings
from app.db.async_database import run_sync

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/owner", tags=["Owner access"])

COOKIE_NAME = "session"
HANDOFF_TTL_SECONDS = 60


# ── Rate limit ──────────────────────────────────────────────────────────────

class FailureLimiter:
    """Counts failed password checks per client address and in total.

    After ``per_client`` failures from one address within ``window_seconds``
    that address waits out the window; after ``total`` failures from all
    addresses together, every password check waits. A success clears the
    address's count. In-process by design: one worker, one listener.
    """

    def __init__(self, *, per_client: int = 5, total: int = 50, window_seconds: float = 900.0):
        self.per_client = per_client
        self.total = total
        self.window_seconds = window_seconds
        self._failures: Dict[str, Deque[float]] = defaultdict(deque)
        self._all: Deque[float] = deque()
        self._lock = threading.Lock()

    def _prune(self, now: float) -> None:
        cutoff = now - self.window_seconds
        while self._all and self._all[0] <= cutoff:
            self._all.popleft()
        for key in list(self._failures):
            entries = self._failures[key]
            while entries and entries[0] <= cutoff:
                entries.popleft()
            if not entries:
                del self._failures[key]

    def retry_after(self, key: str) -> Optional[int]:
        """Seconds until ``key`` may try again, or None when it may now."""
        now = time.monotonic()
        with self._lock:
            self._prune(now)
            waits = []
            entries = self._failures.get(key)
            if entries and len(entries) >= self.per_client:
                waits.append(entries[0] + self.window_seconds - now)
            if len(self._all) >= self.total:
                waits.append(self._all[0] + self.window_seconds - now)
            return max(1, int(max(waits)) + 1) if waits else None

    def failure(self, key: str) -> None:
        now = time.monotonic()
        with self._lock:
            self._failures[key].append(now)
            self._all.append(now)

    def success(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)

    def reset(self) -> None:
        with self._lock:
            self._failures.clear()
            self._all.clear()


limiter = FailureLimiter()


def _client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _check_limit(request: Request) -> str:
    key = _client_key(request)
    wait = limiter.retry_after(key)
    if wait is not None:
        raise HTTPException(429, "Too many wrong passwords. Wait a few minutes and try again.",
                            headers={"Retry-After": str(wait)})
    return key


async def _check_password(request: Request, password: str) -> None:
    key = _check_limit(request)
    if await run_sync(owner_auth.verify_password, password):
        limiter.success(key)
        return
    limiter.failure(key)
    await run_sync(_audit, request, audit_service.AuditEventType.LOGIN_FAILURE, {"reason": "invalid_password"})
    raise HTTPException(401, "Wrong password.")


# ── Helpers ─────────────────────────────────────────────────────────────────

def _audit(request, event_type, details=None):
    try:
        audit_service.log_event(event_type, request=request, user_id=owner_auth.preference_identity(),
                                user_type="admin", details=details)
    except Exception:
        logger.warning("Owner access audit recording failed", exc_info=True)


def _local_only(request: Request) -> None:
    if request.scope.get("shuffle.ingress") != "local":
        raise HTTPException(403, "Password sign-in is only available on the local network listener.")


def _loopback_host(request: Request) -> bool:
    host = urlsplit("http://" + request.headers.get("host", "")).hostname or ""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _set_session_cookie(request: Request, response: Response, token: str, expires) -> None:
    # Browsers keep Secure cookies from http://localhost; a LAN address over
    # plain http needs COOKIE_SECURE=false, which the install docs set.
    secure = settings.cookie_secure and not _loopback_host(request)
    response.set_cookie(COOKIE_NAME, token, max_age=max(1, int(expires.timestamp() - time.time())),
                        httponly=True, samesite="lax", secure=secure, path="/")


def _optional_owner(request: Request) -> bool:
    """Whether the caller is already the owner (any owner credential)."""
    from app.api.auth import require_admin
    try:
        require_admin(request)
        return True
    except HTTPException:
        return False


# ── Status ──────────────────────────────────────────────────────────────────

@router.get("/status")
async def owner_status(request: Request):
    result = await run_sync(owner_auth.status, request)
    if result["password_set"] and await run_sync(owner_auth.owner_session, request, request.cookies.get(COOKIE_NAME)):
        # A signed-in LAN browser knows Docker's published host and port, which
        # the container cannot discover itself (the LAN-trust owner teaches it
        # through /auth/status).
        from app.core import cluster
        try:
            await run_sync(cluster.observe_local_advertise_url, request)
        except Exception:
            logger.warning("Could not learn the local worker address", exc_info=True)
    return result


# ── Password ────────────────────────────────────────────────────────────────

class PasswordBody(BaseModel):
    password: str = Field(max_length=owner_auth.PASSWORD_MAX_LENGTH)
    current_password: Optional[str] = Field(default=None, max_length=owner_auth.PASSWORD_MAX_LENGTH)


@router.post("/password")
async def set_owner_password(body: PasswordBody, request: Request, response: Response):
    """Create the first owner password, or change it."""
    owner_auth.same_origin(request)
    has_password = await run_sync(owner_auth.password_set, True)
    is_owner = await run_sync(_optional_owner, request)
    if has_password:
        if not is_owner:
            raise HTTPException(403, "Owner access required")
        if not body.current_password:
            raise HTTPException(400, "Enter the current password.")
        await _check_password(request, body.current_password)
    elif not (is_owner or await run_sync(owner_auth.bootstrap_access, request)):
        raise HTTPException(403, "Open AI Shuffle from your local network to create the owner password.")
    try:
        await run_sync(owner_auth.set_password, body.password, source="settings" if has_password else "setup")
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    await run_sync(_audit, request, audit_service.AuditEventType.PASSWORD_CHANGED,
                   {"method": "change" if has_password else "create"})
    # Every owner browser was signed out; keep this one signed in, unless it
    # rides on a paired device's cookie, which must not be replaced.
    current = request.cookies.get(COOKIE_NAME)
    if request.scope.get("shuffle.ingress") == "local" and (not current or current.startswith(owner_auth.SESSION_PREFIX)):
        token, expires = await run_sync(owner_auth.issue_session)
        _set_session_cookie(request, response, token, expires)
    return {"status": "ok"}


# ── Sign-in ─────────────────────────────────────────────────────────────────

class LoginBody(BaseModel):
    password: str = Field(max_length=owner_auth.PASSWORD_MAX_LENGTH)
    # Return the token in the body for use as a Bearer (scripts); browsers
    # rely on the HttpOnly cookie instead.
    bearer: bool = False


@router.post("/login")
async def owner_login(body: LoginBody, request: Request, response: Response):
    _local_only(request)
    if not body.bearer:
        # A cookie is ambient authority: only a same-origin page may obtain one.
        owner_auth.same_origin(request)
    if not await run_sync(owner_auth.password_set, True):
        raise HTTPException(409, "No owner password is set yet.")
    await _check_password(request, body.password)
    token, expires = await run_sync(owner_auth.issue_session)
    await run_sync(_audit, request, audit_service.AuditEventType.LOGIN_SUCCESS, {"method": "password"})
    if body.bearer:
        return {"status": "ok", "token": token, "expires_at": expires.isoformat()}
    _set_session_cookie(request, response, token, expires)
    return {"status": "ok", "expires_at": expires.isoformat()}


# ── Trusted LAN ─────────────────────────────────────────────────────────────

def _require_owner(request: Request) -> str:
    from app.api.auth import require_admin
    return require_admin(request)


class TrustedLanBody(BaseModel):
    enabled: bool


@router.put("/trusted-lan")
async def set_trusted_lan(body: TrustedLanBody, request: Request, _: str = Depends(_require_owner)):
    owner_auth.same_origin(request)
    await run_sync(owner_auth.set_trusted_lan, body.enabled)
    await run_sync(_audit, request, audit_service.AuditEventType.SETTINGS_CHANGED,
                   {"setting": "trusted_lan", "enabled": body.enabled})
    return {"trusted_lan": body.enabled}


# ── Desktop "Open in Browser" handoff ───────────────────────────────────────

_handoffs: Dict[str, float] = {}
_handoff_lock = threading.Lock()


def _prune_handoffs(now: float) -> None:
    for key in [k for k, expiry in _handoffs.items() if expiry <= now]:
        del _handoffs[key]


@router.post("/handoff")
async def create_handoff(request: Request):
    """A one-time, short-lived URL that signs the default browser in.

    Only the desktop app itself can ask: loopback carrying the listener token
    this worker was launched with.
    """
    if not desktop_listener.configured():
        raise HTTPException(404, "Not available")
    if not owner_auth.loopback_request(request):
        raise HTTPException(403, "Desktop listener token required")
    token = secrets.token_urlsafe(32)
    now = time.monotonic()
    with _handoff_lock:
        _prune_handoffs(now)
        _handoffs[owner_auth.digest(token)] = now + HANDOFF_TTL_SECONDS
    return {"path": "/api/v1/owner/handoff?token=" + token, "expires_in": HANDOFF_TTL_SECONDS}


@router.get("/handoff", include_in_schema=False)
async def redeem_handoff(request: Request, token: str = ""):
    if not desktop_listener.configured():
        raise HTTPException(404, "Not available")
    peer = request.client.host if request.client else ""
    try:
        peer_loopback = ipaddress.ip_address(peer).is_loopback
    except ValueError:
        peer_loopback = False
    if (request.scope.get("shuffle.ingress") != "local" or not peer_loopback or not _loopback_host(request)
            or any(h in request.headers for h in owner_auth.PROXY_HEADERS)):
        raise HTTPException(403, "Open this link on the computer running AI Shuffle.")
    now = time.monotonic()
    with _handoff_lock:
        _prune_handoffs(now)
        expiry = _handoffs.pop(owner_auth.digest(token), None) if token else None
    if expiry is None:
        raise HTTPException(403, "This link has expired or was already used. Choose Open in Browser again.")
    session, expires = await run_sync(owner_auth.issue_session, "desktop-browser", owner_auth.HANDOFF_SESSION_DAYS)
    response = RedirectResponse("/", status_code=303)
    _set_session_cookie(request, response, session, expires)
    return response
