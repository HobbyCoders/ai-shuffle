"""
Authentication API routes
"""

import os
import hashlib
import time
import subprocess
import logging
import uuid as _uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Response, Request, Depends, status
from fastapi.responses import RedirectResponse

from app.core import owner_auth, preview_access
from app.core.auth import (
    auth_service,
    is_github_authenticated_cached,
    reset_github_auth_cache,
    set_github_auth_cached,
)
from app.core.oauth import direct_oauth
from app.core import audit_service
from app.core import auth as _auth_module
from app.db import database as db
from app.db.async_database import run_sync

logger = logging.getLogger(__name__)

# Unique server instance ID to help debug multi-instance issues
_SERVER_INSTANCE_ID = _uuid.uuid4().hex[:8]

# Share instance ID with auth service for consistent logging
_auth_module.set_server_instance_id(_SERVER_INSTANCE_ID)

router = APIRouter(prefix="/api/v1/auth", tags=["Authentication"])


def _sync_back_active_account_safe() -> None:
    """Fire the multi-account sync-back defensively (never breaks status)."""
    try:
        from app.core.claude_accounts import sync_back_active_account
        sync_back_active_account()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Claude account sync-back on status failed (ignored): {e}")


def _has_provider_credential_safe() -> bool:
    """Whether any enabled model-provider row carries a stored credential.

    Defensive by design: this only feeds the additive ``has_any_credential``
    status field, so a DB hiccup (pool not up, missing table on a rolling
    deploy) must degrade to "no provider credential" rather than break
    ``GET /auth/status`` app-wide.
    """
    try:
        return db.count_configured_model_providers() > 0
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Provider-credential probe failed (ignored): {e}")
        return False


def _seed_claude_account_safe() -> None:
    """Seed the default Claude account after the encryption key becomes available."""
    try:
        from app.core.claude_accounts import seed_from_credentials_file
        seed_from_credentials_file()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Claude account seed failed (ignored): {e}")


def get_session_token(request: Request) -> Optional[str]:
    """Extract session token from cookie"""
    return request.cookies.get("session")


def get_api_key(request: Request) -> Optional[str]:
    """Extract API key from Authorization header"""
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[7:]
    return None


def hash_api_key(api_key: str) -> str:
    """Hash an API key for lookup"""
    return hashlib.sha256(api_key.encode()).hexdigest()


def get_paired_client_from_token(token: Optional[str], *, touch: bool = True) -> Optional[dict]:
    """Resolve a live paired session, including verified managed-owner authority."""
    if not token:
        return None
    from app.core.pairing import PAIRED_SESSION_PREFIX, hash_controller_session

    if not token.startswith(PAIRED_SESSION_PREFIX):
        return None
    client = db.get_paired_client_by_session_hash(hash_controller_session(token))
    if client is not None and client.get("scopes") is not None:
        if "controller" not in client.get("scopes", []):
            return None
    if client and touch:
        db.touch_paired_client(client["id"])
    return client


def _set_paired_request_state(request: Request, token: str, client: dict) -> str:
    if request.cookies.get('session') == token and request.method not in ('GET', 'HEAD', 'OPTIONS'):
        owner_auth.same_origin(request)
    owner_admin = client.get('account_owner_admin') is True
    request.state.is_admin = owner_admin
    request.state.api_user = None
    request.state.paired_client = client
    request.state.auth_token = f"paired_client:{client['id']}"
    return f"paired_client:{client['id']}"


def require_auth(request: Request) -> str:
    """Dependency that requires authentication (cookie or API key)"""
    preview_access.enforce_limits(request)
    # First try cookie-based admin auth
    token = get_session_token(request)
    if token:
        paired_client = get_paired_client_from_token(token)
        if paired_client:
            return _set_paired_request_state(request, token, paired_client)

        # Check admin session
        if owner_auth.loopback_request(request) and auth_service.validate_session(token):
            if request.method not in ('GET', 'HEAD', 'OPTIONS'):
                owner_auth.same_origin(request)
            request.state.is_admin = True
            request.state.api_user = None
            request.state.auth_token = token
            return token

        # Diagnostic logging - session token exists but validation failed
        last4 = token[-4:] if len(token) >= 4 else token
        logger.warning(
            f"[{_SERVER_INSTANCE_ID}] Auth failed: token exists "
            f"(len={len(token)}, last4=...{last4}) "
            f"but validation failed. Path: {request.url.path}"
        )

    # Then try API key auth (Bearer token)
    api_key = get_api_key(request)
    if api_key:
        paired_client = get_paired_client_from_token(api_key)
        if paired_client:
            return _set_paired_request_state(request, api_key, paired_client)

        key_hash = hash_api_key(api_key)
        api_user = db.get_api_user_by_key_hash(key_hash)
        if api_user:
            # Update last used timestamp
            db.update_api_user_last_used(api_user["id"])
            # Store API user info in request state for later use
            request.state.is_admin = False
            request.state.api_user = api_user
            request.state.auth_token = f"api_key:{api_user['id']}"
            return f"api_key:{api_user['id']}"

    if owner_auth.local_access(request):
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            owner_auth.same_origin(request)
        request.state.is_admin = True
        request.state.api_user = None
        request.state.auth_token = 'local-owner'
        return 'local-owner'

    # The agent browser's session-scoped preview cookie (app.core.preview_access):
    # LAN peer on the local listener (a LAN dev proxy in between is fine — the
    # token is the credential), a live session behind the token, and never a
    # request that changes ownership or credentials.
    preview = preview_access.grant(request)
    if preview is not None:
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            owner_auth.same_origin(request)
        request.state.is_admin = True
        request.state.api_user = None
        request.state.auth_token = 'preview:' + preview.session_id
        return request.state.auth_token

    # Log what auth methods were attempted
    has_cookie = token is not None
    has_bearer = api_key is not None
    path = request.url.path
    logger.warning(
        f"[{_SERVER_INSTANCE_ID}] Auth failed completely: "
        f"cookie={has_cookie}, bearer={has_bearer}, path={path}"
    )

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Not authenticated"
    )


def require_admin(request: Request) -> str:
    """Require the install administrator or its live-authorized managed owner.

    A paired owner keeps its device-bound session and revocation path. Ordinary
    API users, mobile keys and directly paired controllers never qualify.
    """
    preview_access.enforce_limits(request)
    token = get_session_token(request)
    candidate = token or get_api_key(request)
    if candidate:
        client = get_paired_client_from_token(candidate, touch=False)
        if client:
            if client.get('account_owner_admin') is True:
                return _set_paired_request_state(request, candidate, client)
            raise HTTPException(403, 'Owner access required')
        elif token and owner_auth.loopback_request(request) and auth_service.validate_session(token):
            if getattr(request, 'method', 'GET') not in ('GET', 'HEAD', 'OPTIONS'):
                owner_auth.same_origin(request)
            request.state.is_admin = True
            request.state.api_user = None
            return token

    if owner_auth.local_access(request):
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            owner_auth.same_origin(request)
        request.state.is_admin = True
        request.state.api_user = None
        request.state.auth_token = 'local-owner'
        return 'local-owner'
    raise HTTPException(status_code=403, detail='Owner access required')


def get_api_user_from_request(request: Request) -> Optional[dict]:
    """Get API user from request state if authenticated via API key"""
    return getattr(request.state, "api_user", None)


def require_api_key(request: Request) -> dict:
    """
    Dependency that requires API key authentication only.
    Use this for /api/v1/ endpoints that should only be accessible via API keys,
    not admin sessions. Returns the API user dict.
    """
    preview_access.enforce_limits(request)
    api_key = get_api_key(request)
    if api_key:
        key_hash = hash_api_key(api_key)
        api_user = db.get_api_user_by_key_hash(key_hash)
        if api_user and api_user.get("is_active", True):
            db.update_api_user_last_used(api_user["id"])
            request.state.is_admin = False
            request.state.api_user = api_user
            return api_user

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="API key required. Use Bearer token."
    )


def is_admin_request(request: Request) -> bool:
    """Check if the current request is from an admin user"""
    return getattr(request.state, "is_admin", False)


def get_current_api_user(request: Request) -> dict:
    """
    Dependency that requires an API user to be authenticated via API key.
    Returns the API user dict.
    """
    preview_access.enforce_limits(request)
    api_key = get_api_key(request)
    if api_key:
        key_hash = hash_api_key(api_key)
        api_user = db.get_api_user_by_key_hash(key_hash)
        if api_user and api_user.get("is_active", True):
            db.update_api_user_last_used(api_user["id"])
            request.state.is_admin = False
            request.state.api_user = api_user
            return api_user

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="API key authentication required."
    )


@router.get("/status")
async def get_auth_status(request: Request, response: Response = None):
    """Get complete authentication status"""
    token = get_session_token(request)
    is_admin_authenticated = False
    is_paired_authenticated = False
    username = None

    if token:
        paired_client = await run_sync(get_paired_client_from_token, token)
        if paired_client:
            is_paired_authenticated = True
            is_admin_authenticated = paired_client.get('account_owner_admin') is True
            username = paired_client.get("device_name") or "Paired device"
        elif owner_auth.loopback_request(request) and auth_service.validate_session(token):
            is_admin_authenticated = True
            username = auth_service.get_admin_username()

    # The app-bundled UI sends its paired session as a Bearer and omits
    # cookies. Match require_auth's cookie-first, Bearer-fallback order so a
    # working mobile session is not reported as expired. Only paired sessions
    # qualify here, not mobile enrollment keys or integration API keys.
    api_key = get_api_key(request)
    if api_key and not (is_admin_authenticated or is_paired_authenticated):
        paired_client = await run_sync(get_paired_client_from_token, api_key)
        if paired_client:
            is_paired_authenticated = True
            is_admin_authenticated = paired_client.get('account_owner_admin') is True
            username = paired_client.get("device_name") or "Paired device"

    local = owner_auth.local_access(request) and not is_paired_authenticated
    preview_grant = preview_access.grant(request)
    if preview_grant is not None and not (local or is_paired_authenticated):
        # The agent browser holding this session's preview cookie sees the UI
        # exactly as the local owner does; the mutation limits are enforced by
        # require_auth per request, not by hiding anything here.
        local = True
        is_admin_authenticated = True
        username = 'Preview (agent browser)'
    elif local:
        is_admin_authenticated = True
        username = 'Local owner'
        # A direct LAN browser knows Docker's published host/port; the container
        # cannot discover them from its own network namespace.
        from app.core import cluster
        try:
            await run_sync(cluster.observe_local_advertise_url, request)
        except Exception:
            logger.warning("Could not learn the local worker address", exc_info=True)
        if token and not owner_auth.local_session(token) and response is not None:
            # Retired cookies must not strand the local browser's WebSocket.
            response.delete_cookie('session')
    # Self-heal an expiring OAuth token before reporting status, so the UI doesn't
    # flip to "unauthorized" when a valid refresh token could silently renew it.
    await direct_oauth.ensure_fresh_credentials()
    # Observe the on-disk credentials file back into the active account row. This
    # catches CLI subprocesses self-refreshing the shared file (which bypasses
    # the post-write hook). The mtime gate makes it a stat() no-op normally.
    _sync_back_active_account_safe()

    claude_authenticated = auth_service.is_claude_authenticated()

    # ``gh auth status`` is a subprocess with a 10s timeout and this endpoint is
    # polled — run it off the event loop, and only when the shared TTL cache has
    # expired (finding G1). Explicit logins/logouts below seed the cache, so the
    # signal is still immediate after a real auth change.
    github_authenticated = await run_sync(
        is_github_authenticated_cached, auth_service.is_github_authenticated
    )

    preference_principal = None
    if is_paired_authenticated:
        from app.db import mobile_hub
        owner = await run_sync(mobile_hub.ownership, paired_client_id=paired_client['id'])
        preference_principal = {'kind': 'paired', 'principal_id': paired_client['id'],
                                'account_id': owner['account_id'] if owner else None}
    elif is_admin_authenticated:
        preference_principal = {'kind': 'admin'}
    from app.core.preference_identity import principal_preference_identity
    pref_identity = (await run_sync(principal_preference_identity, preference_principal)
                     if preference_principal else None)

    return {
        "authenticated": is_admin_authenticated or is_paired_authenticated,
        "local_access": local,
        "preview_access": preview_grant is not None,
        "auth_mode": 'local' if local else 'account',
        "owner_linked": owner_auth.linked_binding() is not None,
        "credential_storage": owner_auth.storage_status(),
        "is_admin": is_admin_authenticated,
        "paired_client": is_paired_authenticated,
        "preference_identity": {
            "user_type": pref_identity[0], "user_id": pref_identity[1],
        } if pref_identity else None,
        "setup_required": auth_service.is_setup_required(),
        "claude_authenticated": claude_authenticated,
        # PA10 (docs/PROVIDER_AGNOSTIC_PLAN.md): "does this install have ANY
        # usable credential" — the claude subscription login OR at least one
        # enabled model-provider row with a stored credential. The composer and
        # first-run setup gate on THIS, not on claude auth alone; Claude-CLI
        # auth remains required only for claude-subscription agents.
        "has_any_credential": claude_authenticated or _has_provider_credential_safe(),
        # Honest token-validity signal: True only when the on-disk Claude OAuth
        # token is present AND not expired. Lets the UI surface a "token expired —
        # reconnect" state even though `claude_authenticated` stays True (file
        # exists) for back-compat.
        "claude_token_valid": auth_service.get_claude_token_status()["token_valid"],
        "github_authenticated": github_authenticated,
        "onboarding_complete": auth_service.is_onboarding_complete(),
        "username": username,
    }


def _audit_owner_action(request, event_type, method):
    """Record immutable actor IDs and fixed action names."""
    try:
        audit_service.log_event(event_type, request=request, user_id=owner_auth.preference_identity(),
                                user_type='admin', details={'method': method})
    except Exception:
        # Audit failures must not lose the owner's response; the audit sink
        # itself records persistence failures safely.
        logger.warning('Owner action audit recording failed', exc_info=True)


@router.post('/setup')
async def configure_local_install(request: Request, _: str = Depends(require_admin)):
    owner_auth.same_origin(request)
    await run_sync(owner_auth.configure_installation)
    await run_sync(_audit_owner_action, request, audit_service.AuditEventType.INSTALLATION_SETUP, 'local_setup')
    return {'status': 'ok'}


@router.get("/preview-access", include_in_schema=False)
async def preview_access_redirect(request: Request):
    """Turn a session-scoped preview token into the cookie the UI rides on.

    The agent browser opens this URL once; every later request of the visit
    carries the HttpOnly cookie and is admitted by ``require_auth`` under the
    limits documented in :mod:`app.core.preview_access`. The token is accepted
    from the query string HERE and nowhere else.
    """
    token = request.query_params.get(preview_access.QUERY_PARAM)
    if not preview_access.transport_ok(request):
        raise HTTPException(403, 'Preview access is only honoured on the local network listener.')
    found = preview_access.verify(token)
    if found is None:
        raise HTTPException(403, 'Preview token is invalid, expired, or bound to a session that no longer exists.')
    target = preview_access.safe_next(request.query_params.get('next'))
    response = RedirectResponse(target, status_code=303)
    response.set_cookie(
        preview_access.COOKIE_NAME, token, max_age=max(1, found.expires_at - int(time.time())),
        httponly=True, samesite='lax', path='/',
    )
    return response


@router.post("/logout")
async def logout(request: Request, response: Response):
    """Logout and invalidate session"""
    owner_auth.same_origin(request)
    token = get_session_token(request)
    if token:
        paired_client = await run_sync(get_paired_client_from_token, token, touch=False)
        if paired_client:
            from app.core.pairing import hash_controller_session
            await run_sync(db.clear_paired_client_session, paired_client["id"], hash_controller_session(token))
            await run_sync(audit_service.log_logout, request, paired_client["id"], "paired_client")
        else:
            valid_session = await run_sync(db.get_auth_session, token)
            auth_service.logout(token)
            if valid_session:
                try:
                    username = await run_sync(auth_service.get_admin_username)
                    await run_sync(audit_service.log_logout, request, username)
                except Exception:
                    logger.warning("Logout audit recording failed", exc_info=True)

    response.delete_cookie(key="session")
    return {"status": "ok", "message": "Logged out"}


@router.get("/claude/status")
async def claude_auth_status():
    """Get Claude CLI authentication status"""
    # Renew an expiring OAuth token (using the refresh token) before reporting,
    # so a short-lived access token doesn't surface as "expired" unnecessarily.
    await direct_oauth.ensure_fresh_credentials()
    _sync_back_active_account_safe()

    info = auth_service.get_claude_auth_info()
    # Extend with multi-account fields (keeping every existing field intact).
    try:
        from app.core import claude_accounts as _accounts
        info["active_account"] = _accounts.get_active_account_summary()
        info["accounts_count"] = await run_sync(db.count_claude_accounts)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Could not attach Claude account info to status: {e}")
        info.setdefault("active_account", None)
        info.setdefault("accounts_count", 0)
    return info


@router.get("/claude/validate")
async def validate_claude_credentials(token: str = Depends(require_admin)):
    """
    Validate Claude credentials by testing the CLI.
    Use this to check if OAuth token is still valid (not just if file exists).

    Returns:
        - valid: boolean - True if credentials work
        - authenticated: boolean - True if credentials file exists
        - error: string - Error message if validation failed
    """
    return auth_service.validate_claude_credentials()


@router.get("/claude/login-instructions")
async def claude_login_instructions():
    """Get Claude CLI login instructions"""
    return auth_service.get_login_instructions()


@router.post("/claude/login")
async def claude_login(request: Request, token: str = Depends(require_admin)):
    """
    Start Claude Code OAuth login process.
    Returns an OAuth URL that the user should open in their browser.

    Accepts optional JSON body with:
    - force_reauth: boolean - If true, delete existing credentials and force re-authentication.
                             Use when OAuth token has expired or user wants to reconnect.
    """
    force_reauth = False
    try:
        body = await request.json()
        force_reauth = body.get("force_reauth", False)
    except Exception:
        pass  # No body or invalid JSON, use defaults

    return auth_service.start_claude_oauth_login(force_reauth=force_reauth)


@router.get("/claude/login/poll")
async def claude_login_poll(token: str = Depends(require_admin)):
    """
    Poll for Claude authentication status after user completes OAuth flow.
    Returns when authentication is detected or after a short timeout.
    """
    # Short poll - check once with a brief delay for UI responsiveness
    import asyncio
    await asyncio.sleep(1)
    if auth_service.is_claude_authenticated():
        return {
            "success": True,
            "authenticated": True,
            "message": "Successfully authenticated with Claude Code"
        }
    return {
        "success": False,
        "authenticated": False,
        "message": "Not yet authenticated. Continue polling or try again."
    }


@router.post("/claude/complete")
async def claude_login_complete(request: Request, token: str = Depends(require_admin)):
    """
    Complete the Claude OAuth login by providing the authorization code and state.

    After starting the login with /auth/claude/login and getting an OAuth URL,
    the user visits the URL in their browser, authenticates, and receives a code.
    They then call this endpoint with that code and the state to complete the login flow.

    Expects JSON body with:
    - 'code': The authorization code from the OAuth callback
    - 'state': The state parameter from the original /auth/claude/login response
    """
    try:
        body = await request.json()
        auth_code = body.get("code")
        state = body.get("state")

        if not auth_code:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Authorization code is required"
            )
        if not state:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="State parameter is required"
            )

        return await auth_service.complete_claude_oauth_login(auth_code, state)
    except HTTPException:
        raise
    except Exception:
        logger.exception("Claude complete login error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to complete Claude login"
        )


@router.post("/claude/logout")
async def claude_logout(token: str = Depends(require_admin)):
    """Logout from Claude CLI"""
    return auth_service.claude_logout()


# =========================================================================
# GitHub CLI Authentication
# =========================================================================

def _invalidate_github_auth_caches(value: Optional[bool] = None) -> None:
    """Propagate an explicit GitHub auth change to every cached copy.

    Two caches hold ``gh auth status``: the module-level one in
    ``app.core.auth`` (prompt builder, ``/auth/status``) and the one inside
    ``github_service`` (every PR/actions endpoint). Neither may keep serving a
    stale answer across a deliberate login or logout, so both are updated here
    rather than waiting out their TTLs. Pass *value* when the new state is
    known first-hand; otherwise both are simply dropped.
    """
    from app.core.github_service import github_service

    if value is None:
        reset_github_auth_cache()
    else:
        set_github_auth_cached(value)
    github_service.invalidate_auth_cache(value)


@router.get("/github/status")
async def github_auth_status(token: str = Depends(require_auth)):
    """Get GitHub CLI authentication status"""
    # Runs `gh auth status` + `gh api user` — never on the event loop.
    info = await run_sync(auth_service.get_github_auth_info)
    # This endpoint is the completion poll for the device-code flow, so its
    # first-hand answer is the freshest one anybody has: seed the caches with it
    # instead of letting them expire on their own schedule.
    _invalidate_github_auth_caches(bool(info.get("authenticated")))
    return info


@router.post("/github/login")
async def github_login(request: Request, token: str = Depends(require_admin)):
    """
    Login to GitHub CLI using a personal access token.
    Expects JSON body with 'token' field containing the GitHub PAT.
    """
    try:
        body = await request.json()
        gh_token = body.get("token") if isinstance(body, dict) else None
        if not isinstance(gh_token, str) or not gh_token.strip():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="GitHub token is required"
            )
        result = await run_sync(auth_service.github_login_with_token, gh_token.strip())
        _invalidate_github_auth_caches(True if result.get("success") else None)
        return result
    except HTTPException:
        raise
    except Exception:
        logger.exception("GitHub login error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to complete GitHub login"
        )


@router.post("/github/login/web")
async def github_login_web(request: Request, token: str = Depends(require_admin)):
    """
    Start GitHub CLI device flow authentication (gh auth login --web).
    Returns a one-time code and verification URL for the user.
    Poll GET /auth/github/status for completion.
    """
    try:
        result = await auth_service.github_login_web()
        # The device flow finishes out-of-band; drop the cached answer so the
        # first status poll after completion sees the real state.
        _invalidate_github_auth_caches()
        return result
    except Exception:
        logger.exception("GitHub web login error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to start GitHub web login"
        )


@router.post("/github/login/web/cancel")
async def github_cancel_web_login(token: str = Depends(require_admin)):
    return await run_sync(auth_service.cancel_github_web_login)


@router.post("/github/logout")
async def github_logout(token: str = Depends(require_admin)):
    """Logout from GitHub CLI"""
    result = await run_sync(auth_service.github_logout)
    _invalidate_github_auth_caches(False if result.get("success") else None)
    return result


# =========================================================================
# Onboarding
# =========================================================================

@router.post("/onboarding/complete")
async def complete_onboarding(token: str = Depends(require_admin)):
    """Mark the onboarding flow as complete"""
    auth_service.set_onboarding_complete()
    return {"status": "ok", "message": "Onboarding completed"}


@router.get("/diagnostics")
async def auth_diagnostics(token: str = Depends(require_admin)):
    """Run diagnostic checks for authentication issues (Admin only)"""
    diagnostics = {}

    # Check HOME environment variable
    diagnostics["home_env"] = os.environ.get("HOME", "NOT SET")

    # Check if claude command exists
    def _which_claude() -> str:
        result = subprocess.run(
            ['which', 'claude'],
            capture_output=True,
            text=True,
            timeout=5
        )
        return result.stdout.strip() if result.returncode == 0 else "NOT FOUND"

    try:
        diagnostics["claude_path"] = await run_sync(_which_claude)
    except Exception as e:
        diagnostics["claude_path"] = f"ERROR: {str(e)}"

    # Check config locations
    home_dir = Path(os.environ.get('HOME', '/home/appuser'))

    # ~/.claude/ (newer Claude Code versions)
    claude_dir = home_dir / '.claude'
    diagnostics["claude_dir"] = str(claude_dir)
    diagnostics["claude_dir_exists"] = claude_dir.exists()

    if claude_dir.exists():
        creds_file = claude_dir / '.credentials.json'
        diagnostics["claude_credentials_exists"] = creds_file.exists()
        if creds_file.exists():
            diagnostics["credentials_file_size"] = creds_file.stat().st_size

    # Check process user
    try:
        import pwd
        diagnostics["process_user"] = pwd.getpwuid(os.getuid()).pw_name
        diagnostics["process_uid"] = os.getuid()
    except Exception as e:
        diagnostics["process_user_error"] = str(e)

    return diagnostics
