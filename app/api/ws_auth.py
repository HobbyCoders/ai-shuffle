"""WebSocket authentication with an explicit, server-owned mobile principal."""
import hashlib
from typing import Optional
from fastapi import HTTPException, WebSocket, WebSocketException
from app.db import database
from app.db.async_database import run_sync
from app.core.pairing import PAIRED_SESSION_PREFIX
from app.core import mobile_tool_identity as identity


async def authenticate_websocket(websocket: WebSocket, token: Optional[str]) -> tuple[bool, Optional[dict]]:
    from app.core import owner_auth
    # Only the actual local listener can establish a passwordless LAN owner.
    # Browsers must send a matching Origin, preventing cross-site WebSocket use.
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
                    websocket.state.mobile_tool_principal = value
                    return True, None
            except HTTPException as exc:
                if exc.status_code >= 500:
                    raise WebSocketException(code=1013, reason='Account authorization unavailable; retry later') from exc
            except Exception:
                pass  # Includes live hub refusal; never fall through as admin.
            continue
        session = await run_sync(database.get_auth_session, candidate)
        if session and owner_auth.loopback_request(websocket):
            websocket.state.mobile_tool_principal = identity.principal('admin', 'install', candidate)
            return True, None
        key_session = await run_sync(database.get_api_key_session, candidate)
        user = (await run_sync(database.get_api_user, key_session['api_user_id']) if key_session
                else await run_sync(database.get_api_user_by_key_hash, hashlib.sha256(candidate.encode()).hexdigest()))
        if user and user.get('is_active', True):
            websocket.state.mobile_tool_principal = identity.principal('api_user', user['id'], candidate)
            return True, user
    return False, None
