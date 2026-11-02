"""WebSocket authentication with an explicit, server-owned mobile principal."""
import hashlib
from typing import Optional
from fastapi import HTTPException, WebSocket, WebSocketException
from app.db import database
from app.db.async_database import run_sync
from app.core.pairing import PAIRED_SESSION_PREFIX
from app.core import mobile_tool_identity as identity


async def authenticate_websocket(websocket: WebSocket, token: Optional[str]) -> tuple[bool, Optional[dict]]:
    from app.core import owner_auth, preview_access
    # A preview cookie restricts whatever else the socket presents. One that
    # cannot be checked says "retry later" before any credential below can stand
    # in for it with the owner's authority.
    try:
        await run_sync(preview_access.settle, websocket)
    except Exception as exc:
        raise WebSocketException(code=1013, reason='Preview access could not be checked; retry later') from exc
    # The agent browser's session-scoped preview cookie. It limits this socket
    # whatever else it presents, so it is read before any credential is, and on
    # the same terms as HTTP (``preview_access.enforce_limits``).
    preview = preview_access.grant(websocket) is not None
    if not token and preview:
        try:
            owner_auth.same_origin(websocket)
        except Exception:
            return False, None
        websocket.state.mobile_tool_principal = identity.preview_principal()
        return True, None
    # Only the actual local listener can establish a passwordless owner:
    # loopback (with the desktop listener token when there is one), or the
    # LAN when the owner turned on trusted LAN. Browsers must send a matching
    # Origin, preventing cross-site WebSocket use.
    if not token and not websocket.cookies.get('session') and owner_auth.local_access(websocket):
        try:
            owner_auth.same_origin(websocket)
        except Exception:
            return False, None
        websocket.state.mobile_tool_principal = identity.principal(
            'admin', 'install', 'local-transport:' + owner_auth.settings.effective_instance_id)
        return True, None
    # Keep the legacy return contract; authority travels only on trusted state.
    websocket.state.mobile_tool_principal = None
    for candidate, ambient in ((token, False), (websocket.cookies.get('session'), True)):
        if not candidate:
            continue
        # Validate the transport actually supplying authority. An invalid query
        # credential must never exempt the fallback ambient cookie from Origin.
        if ambient:
            try:
                owner_auth.same_origin(websocket)
            except Exception:
                return False, None
        if candidate.startswith(PAIRED_SESSION_PREFIX):
            from app.api.auth import get_paired_client_from_token
            try:
                client = await run_sync(get_paired_client_from_token, candidate)
                if client:
                    value = await run_sync(identity.principal, 'paired', client['id'], candidate)
                    websocket.state.mobile_tool_principal = (
                        identity.preview_limited(value) if preview else value)
                    return True, None
            except HTTPException as exc:
                if exc.status_code >= 500:
                    raise WebSocketException(code=1013, reason='Account authorization unavailable; retry later') from exc
            except PermissionError:
                pass  # An explicit live-principal refusal is an auth failure.
            except Exception as exc:
                # A failed dependency check grants no authority, but does not
                # prove that the saved credential was revoked or expired.
                raise WebSocketException(code=1013, reason='Account authorization unavailable; retry later') from exc
            continue
        # An owner password session (cookie, or the token as a query credential).
        if await run_sync(owner_auth.owner_session, websocket, candidate):
            websocket.state.mobile_tool_principal = identity.principal(
                'admin', identity.PREVIEW_PRINCIPAL_ID if preview else 'install', candidate)
            return True, None
        user = await run_sync(
            database.get_api_user_by_key_hash, hashlib.sha256(candidate.encode()).hexdigest())
        if user and user.get('is_active', True):
            value = identity.principal('api_user', user['id'], candidate)
            websocket.state.mobile_tool_principal = (
                identity.preview_limited(value) if preview else value)
            return True, user
    return False, None
