"""WebSocket endpoint for interactive CLI bridge (e.g., /rewind)."""

import asyncio
import logging
from typing import Optional, Dict

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query
from fastapi.websockets import WebSocketState

from app.core.config import settings
from app.core.cli_bridge import CLIBridge, RewindParser
from app.core.project_utils import get_project_working_dir
from app.db import database
from app.db.async_database import run_sync
from app.api.ws_auth import authenticate_websocket

logger = logging.getLogger(__name__)

router = APIRouter(tags=["WebSocket"])

# Track active CLI bridges
_active_cli_bridges: Dict[str, CLIBridge] = {}


@router.websocket("/ws/cli/{session_id}")
async def cli_websocket(
    websocket: WebSocket,
    session_id: str,
    token: Optional[str] = Query(None, description="Authentication token")
):
    """
    WebSocket endpoint for interactive CLI commands like /rewind.

    This creates a PTY bridge to the Claude CLI, allowing the frontend
    to render a terminal and interact with commands that require
    keyboard input (arrow keys, Enter, etc.).

    Message types FROM server:
    - output: Terminal output data (may include ANSI codes)
    - ready: CLI is ready for input
    - exit: CLI process has exited
    - error: Error occurred
    - rewind_complete: Rewind operation completed with checkpoint info

    Message types TO server:
    - input: Raw text input
    - key: Special key (up, down, enter, escape, etc.)
    - resize: Terminal resize {cols, rows}
    - start: Start CLI with command {command: "/rewind"}
    - stop: Stop the CLI process
    """
    await websocket.accept()

    is_authenticated, _ = await authenticate_websocket(websocket, token)
    if not is_authenticated:
        await websocket.close(code=4001, reason="Authentication failed")
        return

    # Get session info
    session = await run_sync(database.get_session, session_id)
    if not session:
        await websocket.close(code=4004, reason="Session not found")
        return

    sdk_session_id = session.get("sdk_session_id")
    if not sdk_session_id:
        await websocket.send_json({
            "type": "error",
            "message": "Session has no SDK session ID - cannot use CLI commands"
        })
        await websocket.close(code=4005, reason="No SDK session")
        return

    # Get working directory from project or profile
    working_dir = str(settings.workspace_dir)
    project_id = session.get("project_id")
    if project_id:
        project = await run_sync(database.get_project, project_id)
        if project:
            working_dir = get_project_working_dir(project)

    logger.debug(f"CLI WebSocket connected for session {session_id}, sdk_session={sdk_session_id}")

    cli_bridge: Optional[CLIBridge] = None
    output_buffer = ""  # For parsing rewind output
    MAX_OUTPUT_BUFFER_SIZE = 1024 * 1024  # 1MB max buffer size

    async def on_output(data: str):
        """Handle CLI output"""
        nonlocal output_buffer
        output_buffer += data
        # Enforce buffer size limit: keep the most recent data
        if len(output_buffer) > MAX_OUTPUT_BUFFER_SIZE:
            output_buffer = output_buffer[-MAX_OUTPUT_BUFFER_SIZE:]

        if websocket.client_state == WebSocketState.CONNECTED:
            await websocket.send_json({
                "type": "output",
                "data": data
            })

            # Check if rewind completed
            if RewindParser.is_rewind_complete(output_buffer):
                checkpoint_msg = RewindParser.get_selected_checkpoint_message(output_buffer)
                selected_option = RewindParser.parse_selected_option(output_buffer)

                await websocket.send_json({
                    "type": "rewind_complete",
                    "checkpoint_message": checkpoint_msg,
                    "selected_option": selected_option,
                    "options": {
                        1: "Restore code and conversation",
                        2: "Restore conversation",
                        3: "Restore code",
                        4: "Never mind"
                    }
                })

    async def on_exit(exit_code: int):
        """Handle CLI exit"""
        if websocket.client_state == WebSocketState.CONNECTED:
            await websocket.send_json({
                "type": "exit",
                "exit_code": exit_code
            })

    try:
        while True:
            try:
                data = await asyncio.wait_for(
                    websocket.receive_json(),
                    timeout=300.0  # 5 minute timeout for CLI operations
                )

                msg_type = data.get("type")

                if msg_type == "start":
                    # Start CLI with specified command
                    command = data.get("command", "/rewind")

                    if cli_bridge and cli_bridge.is_running:
                        await cli_bridge.stop()

                    output_buffer = ""  # Reset buffer

                    cli_bridge = CLIBridge(
                        session_id=session_id,
                        sdk_session_id=sdk_session_id,
                        working_dir=working_dir,
                        on_output=on_output,
                        on_exit=on_exit
                    )

                    success = await cli_bridge.start(command)
                    if success:
                        _active_cli_bridges[session_id] = cli_bridge
                        await websocket.send_json({
                            "type": "ready",
                            "command": command
                        })
                    else:
                        await websocket.send_json({
                            "type": "error",
                            "message": "Failed to start CLI"
                        })

                elif msg_type == "input":
                    # Send raw input
                    if cli_bridge and cli_bridge.is_running:
                        await cli_bridge.send_input(data.get("data", ""))

                elif msg_type == "key":
                    # Send special key
                    if cli_bridge and cli_bridge.is_running:
                        await cli_bridge.send_key(data.get("key", ""))

                elif msg_type == "resize":
                    # Resize terminal
                    if cli_bridge and cli_bridge.is_running:
                        cols = data.get("cols", 80)
                        rows = data.get("rows", 24)
                        await cli_bridge.resize(cols, rows)

                elif msg_type == "stop":
                    # Stop CLI
                    if cli_bridge:
                        await cli_bridge.stop()
                        cli_bridge = None

                elif msg_type == "pong":
                    pass

            except asyncio.TimeoutError:
                if websocket.client_state != WebSocketState.CONNECTED:
                    break
                # Send ping
                await websocket.send_json({"type": "ping"})

    except WebSocketDisconnect:
        logger.debug(f"CLI WebSocket disconnected for session {session_id}")

    except Exception as e:
        logger.error(f"CLI WebSocket error for session {session_id}: {e}", exc_info=True)

    finally:
        # Clean up CLI bridge
        if cli_bridge:
            await cli_bridge.stop()
        if session_id in _active_cli_bridges:
            del _active_cli_bridges[session_id]
