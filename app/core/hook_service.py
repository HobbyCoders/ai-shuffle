"""
Hook Service for AI Shuffle

This service provides SDK hook support for database hooks created via AI Shuffle's
HooksCard feature. Plugin hooks (like Ralph Loop) are handled natively by Claude CLI
and don't require any AI Shuffle intervention.

Database hooks allow users to create custom hooks through the AI Shuffle UI that
execute scripts or commands when certain events fire (Stop, PreToolUse, etc.).
"""

import json
import logging
from typing import Any, Dict, List, Optional, Callable, Awaitable

from claude_agent_sdk.types import (
    HookInput, HookContext, HookMatcher, SyncHookJSONOutput,
)

from app.db import database
from app.core.hook_executor import execute_hook

logger = logging.getLogger(__name__)


# =============================================================================
# Database Hook Support
# =============================================================================

async def get_database_hooks_for_event(
    project_id: Optional[str],
    event: str
) -> List[Dict[str, Any]]:
    """
    Get all enabled database hooks for a project and event type.

    Args:
        project_id: The project ID (can be None if no project)
        event: The event type (e.g., "Stop", "PreToolUse")

    Returns:
        List of hook configurations from database
    """
    if not project_id:
        return []

    try:
        hooks = database.get_hooks_for_project_and_event(project_id, event)
        return hooks or []
    except Exception as e:
        logger.warning(f"Failed to get database hooks: {e}")
        return []


# =============================================================================
# Stop Hook Handler (Database Hooks Only)
# =============================================================================

def create_stop_hook_handler(
    working_dir: str,
    project_id: Optional[str] = None
) -> Callable[[HookInput, Optional[str], HookContext], Awaitable[SyncHookJSONOutput]]:
    """
    Create a Stop hook handler for database hooks.

    Note: Plugin hooks (like Ralph Loop) are handled natively by Claude CLI.
    This handler only processes custom hooks created via AI Shuffle's HooksCard.

    Args:
        working_dir: The working directory for the session
        project_id: Optional project ID for database hooks

    Returns:
        Async hook handler function compatible with SDK
    """

    async def stop_hook_handler(
        hook_input: HookInput,
        tool_use_id: Optional[str],
        context: HookContext
    ) -> SyncHookJSONOutput:
        """Handle Stop hook for database hooks."""

        # Only handle Stop events
        if hook_input.get("hook_event_name") != "Stop":
            return SyncHookJSONOutput()

        logger.debug(f"[HookService] Stop hook fired for {working_dir}")

        # Execute database Stop hooks
        db_hooks = await get_database_hooks_for_event(project_id, "Stop")
        for hook in db_hooks:
            logger.info(f"[HookService] Executing database Stop hook: {hook.get('name')}")

            # Execute the hook and check if it wants to block
            result = await execute_hook(hook, {
                "event": "Stop",
                "working_dir": working_dir,
                "project_id": project_id
            })

            # If hook output contains a block decision, honor it
            if result.get("success") and result.get("output"):
                try:
                    output = json.loads(result["output"])
                    if output.get("decision") == "block":
                        return SyncHookJSONOutput(
                            decision="block",
                            reason=output.get("reason", ""),
                            systemMessage=output.get("systemMessage", ""),
                            continue_=True
                        )
                except (json.JSONDecodeError, TypeError):
                    pass

        # No blocking - allow stop
        return SyncHookJSONOutput()

    return stop_hook_handler


# =============================================================================
# Hook Configuration Builder
# =============================================================================

def build_hooks_config(
    working_dir: str,
    project_id: Optional[str] = None
) -> Optional[Dict[str, List[HookMatcher]]]:
    """
    Build SDK hooks configuration for database hooks.

    Note: Plugin hooks are handled by Claude CLI natively. This only sets up
    SDK hooks for custom database hooks created via AI Shuffle's HooksCard.

    Args:
        working_dir: Working directory for the session
        project_id: Optional project ID for database hooks

    Returns:
        Dict of hook configurations for SDK, or None if no hooks needed
    """
    hooks_config: Dict[str, List[HookMatcher]] = {}

    # Check if there are any database Stop hooks
    has_db_stop_hooks = False
    if project_id:
        try:
            db_hooks = database.get_hooks_for_project_and_event(project_id, "Stop")
            has_db_stop_hooks = bool(db_hooks)
        except Exception as e:
            logger.debug(f"Failed to check for database Stop hooks: {e}")

    # Only register SDK hooks if there are database hooks to handle
    if has_db_stop_hooks:
        stop_handler = create_stop_hook_handler(working_dir, project_id)
        hooks_config["Stop"] = [HookMatcher(hooks=[stop_handler])]
        logger.info(f"[HookService] Registered Stop hook for database hooks")

    return hooks_config if hooks_config else None
