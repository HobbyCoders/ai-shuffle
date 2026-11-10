"""
Connector status tools for the app-config SDK MCP server.

Provides read-heavy operations for AI Shuffle connectors (Telegram,
Discord, Slack, etc.). Full setup requires secrets via the Settings UI.
"""

import logging

from app.mcp.config_helpers import _tool_result, _error, _sanitize_connector

logger = logging.getLogger(__name__)


def _create_connector_tools(is_admin: bool) -> list:
    """Create connector status tools."""
    from app.mcp.inproc import tool
    from app.db import database
    from app.db.async_database import run_sync

    _is_admin = is_admin
    tools = []

    # -- listConnectors ---------------------------------------------------

    @tool(
        "listConnectors",
        "List all configured connectors with their type, status, and events.",
        {"type": "object", "properties": {}},
    )
    async def list_connectors(args: dict) -> dict:
        connectors = await run_sync(database.get_all_connectors)
        return _tool_result({
            "connectors": [
                {
                    "id": c.get("id"),
                    "name": c.get("name"),
                    "type": c.get("type"),
                    "is_active": c.get("is_active", False),
                    "events": c.get("events", []),
                    "last_activity_at": c.get("last_activity_at"),
                }
                for c in connectors
            ],
            "count": len(connectors),
            "note": "To create or configure connectors (which require API tokens/secrets), use Settings > Plugins in the UI.",
        })

    tools.append(list_connectors)

    # -- getConnectorStatus -----------------------------------------------

    @tool(
        "getConnectorStatus",
        "Get runtime status of a specific connector.",
        {
            "type": "object",
            "properties": {
                "connector_id": {"type": "string"},
            },
            "required": ["connector_id"],
        },
    )
    async def get_connector_status(args: dict) -> dict:
        # Single isolated read; the runtime-status lookup below is in-memory
        # state owned by the connector manager on this loop, so it stays here.
        connector = await run_sync(database.get_connector, args["connector_id"])
        if not connector:
            return _error(f"Connector not found: {args['connector_id']}")

        sanitized = _sanitize_connector(connector)

        # Get runtime status if running
        try:
            from app.core.connectors.manager import connector_manager
            runtime = connector_manager.get_connector_status(args["connector_id"])
            if runtime:
                sanitized["runtime_status"] = runtime
        except Exception:
            pass

        return _tool_result(sanitized)

    tools.append(get_connector_status)

    # -- toggleConnector --------------------------------------------------

    @tool(
        "toggleConnector",
        "Start or stop a connector.",
        {
            "type": "object",
            "properties": {
                "connector_id": {"type": "string"},
                "active": {"type": "boolean", "description": "True to start, false to stop."},
            },
            "required": ["connector_id", "active"],
        },
    )
    async def toggle_connector(args: dict) -> dict:
        if not _is_admin:
            return _error("App configuration tools require admin access.")

        # The activate and deactivate routes' rule (#1382): save the switch,
        # and start or stop the live client only on the worker that runs it.
        from fastapi import HTTPException

        from app.api import connectors as routes

        connector_id = args["connector_id"]
        try:
            if args["active"]:
                row = await routes.activate_connector(connector_id, token=None)
            else:
                row = await routes.deactivate_connector(connector_id, token=None)
        except HTTPException as e:
            if e.status_code == 404:
                return _error(str(e.detail))
            return _error(f"Failed to toggle connector: {e.detail}")
        except Exception as e:
            return _error(f"Failed to toggle connector: {str(e)}")

        done = "started" if args["active"] else "stopped"
        msg = f"Connector '{row.get('name')}' {done}."
        result = {"success": True, "message": msg}
        if "applies_on" in row:  # saved here; another worker (or install) runs the client
            from app.network import ownership
            owner = row["applies_on"]
            switched = "switched on" if args["active"] else "switched off"
            if owner is None:
                where = "No worker runs it yet; the worker that claims it applies the change."
            elif ownership.enrolled():
                where = f"It runs on worker {ownership.owner_label(owner)}, which applies the change."
            else:
                where = f"It runs on install {owner}, which applies the change when it restarts."
            result = {"success": True, "message": f"Connector '{row.get('name')}' {switched}. {where}",
                      "applies_on": owner}
        return _tool_result(result)

    tools.append(toggle_connector)

    return tools
