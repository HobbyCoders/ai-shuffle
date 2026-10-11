"""WebSocket endpoints for session and global synchronization."""

import asyncio
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query
from fastapi.websockets import WebSocketState

from app.core.sync_engine import sync_engine
from app.db import database
from app.db.async_database import run_sync
from app.api.sessions_helpers import check_session_access_for_api_user, socket_may_read_transcript
from app.api.ws_auth import authenticate_websocket
from app.api.ws_key_watch import watch_api_key
from app.api.ws_utils import ping_loop

logger = logging.getLogger(__name__)

router = APIRouter(tags=["WebSocket"])


@router.websocket("/ws/sessions/{session_id}")
async def session_sync_websocket(
    websocket: WebSocket,
    session_id: str,
    device_id: str = Query(..., description="Unique device identifier"),
    token: str = Query(..., description="Authentication token")
):
    """
    WebSocket endpoint for real-time session synchronization.

    Connect to this endpoint to:
    - Receive real-time message updates from other devices
    - Get notified of session state changes

    (V2: live render deltas ride the session event feed (SSE), not this socket.)

    Query Parameters:
        - device_id: Unique identifier for this device (generated on frontend)
        - token: Authentication token (session cookie or API key)

    Messages from server:
        - message_added: A new message was added
        - session_updated: Session metadata changed
        - state: Current session state (sent on connect)
        - ping: Keep-alive message

    Messages to server:
        - pong: Response to ping
        - request_state: Re-send the state snapshot
    """
    # Accept connection first (must accept before close per WebSocket protocol)
    await websocket.accept()

    # Authenticate
    auth_ok, api_user = await authenticate_websocket(websocket, token)
    if not auth_ok:
        await websocket.close(code=4001, reason="Authentication failed")
        return

    # Verify session exists and the caller may see it (same rule as the REST
    # session routes: a restricted API user is confined to its project/agent).
    session = await run_sync(database.get_session, session_id)
    if not session:
        await websocket.close(code=4004, reason="Session not found")
        return
    try:
        check_session_access_for_api_user(api_user, session)
        # The state frame carries every message: a paired controller gets the
        # transcript rule of the REST routes (#1877). An error refuses.
        readable = await run_sync(socket_may_read_transcript,
                                  getattr(websocket.state, "mobile_tool_principal", None), session)
    except Exception:  # noqa: BLE001 - any failure refuses, never admits
        readable = False
    if not readable:
        await websocket.close(code=4003, reason="Access denied")
        return
    logger.debug(f"WebSocket connected: device={device_id}, session={session_id}")

    # Register device with sync engine
    connection = await sync_engine.register_device(
        device_id=device_id,
        session_id=session_id,
        websocket=websocket
    )

    try:
        # Send initial state
        state = await sync_engine.get_session_state(session_id)
        state["session"] = session
        state["messages"] = [
            {"seq": None, **message}
            for message in await run_sync(database.get_session_messages, session_id)
        ]

        await websocket.send_json({
            "event_type": "state",
            "session_id": session_id,
            "data": state,
            "timestamp": None
        })

        # Keep connection alive and handle client messages
        ping_task = asyncio.create_task(ping_loop(websocket))

        try:
            # A revoked API key loses the socket (app.api.ws_key_watch).
            async with watch_api_key(websocket):
                # Until this side closes it (a revoked key) or the client goes.
                while websocket.application_state == WebSocketState.CONNECTED:
                    try:
                        # Wait for messages from client
                        data = await asyncio.wait_for(
                            websocket.receive_json(),
                            timeout=60.0  # 60 second timeout
                        )

                        # Handle client messages
                        msg_type = data.get("type")

                        if msg_type == "pong":
                            # Client responding to ping - connection is alive
                            connection.last_activity = datetime.now(timezone.utc)

                        elif msg_type == "request_state":
                            # Client requesting current state
                            state = await sync_engine.get_session_state(session_id)
                            state["session"] = await run_sync(database.get_session, session_id)
                            state["messages"] = [
                                {"seq": None, **message}
                                for message in await run_sync(database.get_session_messages, session_id)
                            ]
                            await websocket.send_json({
                                "event_type": "state",
                                "session_id": session_id,
                                "data": state,
                                "timestamp": None
                            })

                    except asyncio.TimeoutError:
                        # No message received, check if connection is still alive
                        if websocket.client_state != WebSocketState.CONNECTED:
                            break
                        continue

        finally:
            ping_task.cancel()
            try:
                await ping_task
            except asyncio.CancelledError:
                pass

    except WebSocketDisconnect:
        logger.debug(f"WebSocket disconnected: device={device_id}, session={session_id}")

    except Exception as e:
        logger.error(f"WebSocket error for device={device_id}: {e}")

    finally:
        # Unregister device
        await sync_engine.unregister_device(device_id, session_id, websocket=websocket)

        # V2: no in-memory streaming state to purge — busy truth is the
        # durable runs table and a stale run lease self-heals via TTL.
        if sync_engine.get_device_count(session_id) == 0:
            logger.debug(f"Last device disconnected from legacy sync session {session_id}")


@router.websocket("/ws/global")
async def global_sync_websocket(
    websocket: WebSocket,
    device_id: str = Query(..., description="Unique device identifier"),
    token: str | None = Query(None, description="Authentication token"),
):
    from app.core.preference_identity import principal_preference_identity
    from app.core.global_events import global_events

    auth_ok, api_user = await authenticate_websocket(websocket, token)
    await websocket.accept()
    if not auth_ok:
        await websocket.close(code=4001, reason="Authentication failed")
        return
    user_type, user_id = await run_sync(
        principal_preference_identity, websocket.state.mobile_tool_principal
    )
    socket_id = global_events.register(websocket, user_type, user_id)
    try:
        async with watch_api_key(websocket):
            # Until this side closes it (a revoked key) or the client goes.
            while websocket.application_state == WebSocketState.CONNECTED:
                try:
                    message = await asyncio.wait_for(websocket.receive_json(), timeout=30)
                    if message.get("type") == "ping":
                        connection = global_events.connections.get(socket_id)
                        if connection is None or not connection[3].enqueue('{"type":"pong"}'):
                            break
                except asyncio.TimeoutError:
                    connection = global_events.connections.get(socket_id)
                    if connection is None or not connection[3].enqueue('{"type":"ping"}'):
                        break
    except WebSocketDisconnect:
        pass
    finally:
        await global_events.unregister(socket_id)
