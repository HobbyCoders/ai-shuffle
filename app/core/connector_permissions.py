"""Bridge permission requests from connector-linked sessions out to platforms.

When a session that a CONNECTOR conversation owns raises a tool permission
request, the pending-interaction row + WebSocket broadcast is not enough: the
conversation lives on Discord/Telegram/..., not in the web UI. The connector
protocol already defines the outbound half — ``send_permission_prompt`` posts
an interactive Approve/Deny message and the user's button press routes back
through ``ConnectorManager.handle_permission_response`` →
``permission_handler.respond`` — but nothing called it.

This module is that request-side bridge. It is called best-effort from the
permission request path: ANY failure (connector not running, platform send
error, lookup miss) must never break the permission flow itself — the durable
``pending_interactions`` row still resolves via the web UI, mobile push, or
the 300-second TTL deny.
"""

import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)


async def deliver_permission_prompt(
    session_id: str,
    request_id: str,
    tool_name: str,
    tool_input: Dict[str, Any],
    description: str,
) -> None:
    """Fan a permission request out to every connector linked to ``session_id``.

    Looks up the connector sessions bound to the AI session, resolves each
    RUNNING connector instance, and asks it to render the interactive prompt
    on the platform chat that owns the conversation. Fire-and-forget from the
    caller's perspective: errors are logged, never raised.
    """
    try:
        from app.core.connectors.base import PermissionPrompt
        from app.db import database
        from app.core.connectors.manager import connector_manager
    except Exception:  # noqa: BLE001 — imports must never break the flow
        logger.debug("connector_permissions: imports unavailable", exc_info=True)
        return

    try:
        connector_sessions = _lookup_connector_sessions(session_id)
    except Exception:  # noqa: BLE001
        logger.debug(
            "connector_permissions: connector-session lookup failed for %s",
            session_id, exc_info=True,
        )
        return

    if not connector_sessions:
        return

    prompt = PermissionPrompt(
        request_id=request_id,
        session_id=session_id,
        tool_name=tool_name,
        tool_input=tool_input,
        description=description,
    )

    for cs in connector_sessions:
        connector_id = cs.get("connector_id")
        platform_chat_id = cs.get("platform_chat_id")
        if not connector_id or not platform_chat_id:
            continue
        try:
            instance = connector_manager.get_running_instance(connector_id)
            if instance is None:
                logger.debug(
                    "connector_permissions: connector %s not running — "
                    "skipping prompt delivery", connector_id,
                )
                continue
            sent = await instance.send_permission_prompt(
                platform_chat_id, prompt
            )
            if not sent:
                logger.warning(
                    "connector_permissions: %s failed to deliver permission "
                    "prompt %s to chat %s",
                    connector_id, request_id, platform_chat_id,
                )
        except Exception:  # noqa: BLE001 — one platform's failure must not
            # stop the others (a session can be linked to several connectors).
            logger.warning(
                "connector_permissions: prompt delivery raised for connector "
                "%s chat %s", connector_id, platform_chat_id, exc_info=True,
            )


def _lookup_connector_sessions(session_id):
    """Resolve the connector-session rows for an AI session (sync DB read)."""
    from app.db import database

    return database.get_connector_sessions_for_ai_session(session_id)
