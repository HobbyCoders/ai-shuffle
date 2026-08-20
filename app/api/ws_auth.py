"""WebSocket authentication helpers."""

import hashlib
import logging
from typing import Optional

from fastapi import WebSocket

from app.db import database
from app.db.async_database import run_sync
from app.core.pairing import PAIRED_SESSION_PREFIX, hash_controller_session

logger = logging.getLogger(__name__)


async def authenticate_websocket(websocket: WebSocket, token: Optional[str]) -> tuple[bool, Optional[dict]]:
    """
    Validate authentication token for WebSocket connection.

    Returns:
        tuple[bool, Optional[dict]]: (is_authenticated, api_user_or_none)
        - For admin sessions: (True, None)
        - For API user sessions: (True, api_user_dict)
        - For failed auth: (False, None)
    """
    # First try the token from query parameter
    # All DB calls use run_sync to avoid blocking the event loop during auth.
    if token:
        if token.startswith(PAIRED_SESSION_PREFIX):
            client = await run_sync(
                database.get_paired_client_by_session_hash,
                hash_controller_session(token),
            )
            if client and (
                client.get("scopes") is None or "controller" in client.get("scopes", [])
            ):
                await run_sync(database.touch_paired_client, client["id"])
                return (True, None)

        # Check admin session token
        session = await run_sync(database.get_auth_session, token)
        if session:
            return (True, None)  # Admin user

        # Check API key web session token
        api_key_session = await run_sync(database.get_api_key_session, token)
        if api_key_session:
            api_user = await run_sync(database.get_api_user, api_key_session["api_user_id"])
            if api_user and api_user.get("is_active", True):
                return (True, api_user)

        # Check raw API key (hashed)
        key_hash = hashlib.sha256(token.encode()).hexdigest()
        api_user = await run_sync(database.get_api_user_by_key_hash, key_hash)
        if api_user and api_user.get("is_active", True):
            return (True, api_user)

    # Also check the cookie directly (for httpOnly cookies that JS can't read)
    cookie_token = websocket.cookies.get("session")
    if cookie_token:
        if cookie_token.startswith(PAIRED_SESSION_PREFIX):
            client = await run_sync(
                database.get_paired_client_by_session_hash,
                hash_controller_session(cookie_token),
            )
            if client and (
                client.get("scopes") is None or "controller" in client.get("scopes", [])
            ):
                await run_sync(database.touch_paired_client, client["id"])
                return (True, None)

        # Check admin session
        session = await run_sync(database.get_auth_session, cookie_token)
        if session:
            return (True, None)  # Admin user

        # Check API key web session
        api_key_session = await run_sync(database.get_api_key_session, cookie_token)
        if api_key_session:
            api_user = await run_sync(database.get_api_user, api_key_session["api_user_id"])
            if api_user and api_user.get("is_active", True):
                return (True, api_user)

    return (False, None)
