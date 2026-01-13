"""
Hook Service for AI Shuffle

This service builds SDK hooks configuration to enable Claude CLI's hook system.
By passing a hooks config to the SDK, the CLI enables ALL hook processing including:
1. Plugin hooks - defined in plugin hooks.json, executed by CLI natively
2. Database hooks - custom hooks created via AI Shuffle's HooksCard

The key insight is that passing ANY hooks config (even minimal) to the SDK enables
the CLI's entire hook system. Without it, the CLI skips all hook processing.

Supported hook events:
- PreToolUse: Approve/deny/modify tool calls
- PostToolUse: Observe/modify tool results
- Stop: Block Claude from stopping (used by plugins like Ralph Loop)
- SubagentStop: When a subagent stops
- PreCompact: Before context compaction
- UserPromptSubmit: Before user prompt is processed
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

# All supported hook events
SUPPORTED_HOOK_EVENTS = [
    "PreToolUse",
    "PostToolUse",
    "Stop",
    "SubagentStop",
    "PreCompact",
    "UserPromptSubmit",
]


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
# Generic Hook Handler Factory
# =============================================================================

def create_hook_handler(
    event: str,
    working_dir: str,
    project_id: Optional[str] = None
) -> Callable[[HookInput, Optional[str], HookContext], Awaitable[SyncHookJSONOutput]]:
    """
    Create a hook handler for the specified event type.

    This handler executes database hooks (from HooksCard) for the given event.
    Plugin hooks are handled natively by the CLI - we don't need to implement them.

    Args:
        event: The hook event type (e.g., "Stop", "PreToolUse")
        working_dir: The working directory for the session
        project_id: Optional project ID for database hooks

    Returns:
        Async hook handler function compatible with SDK
    """

    async def hook_handler(
        hook_input: HookInput,
        tool_use_id: Optional[str],
        context: HookContext
    ) -> SyncHookJSONOutput:
        """Handle hook event by executing database hooks."""

        # Verify this is the correct event type
        if hook_input.get("hook_event_name") != event:
            return SyncHookJSONOutput()

        logger.debug(f"[HookService] {event} hook fired for {working_dir}")

        # Execute database hooks for this event
        db_hooks = await get_database_hooks_for_event(project_id, event)

        for hook in db_hooks:
            hook_name = hook.get('name', 'unnamed')
            logger.info(f"[HookService] Executing database {event} hook: {hook_name}")

            # Build event data to pass to the hook
            event_data = {
                "event": event,
                "working_dir": working_dir,
                "project_id": project_id,
                "tool_use_id": tool_use_id,
                "hook_input": dict(hook_input),
            }

            # Execute the hook
            result = await execute_hook(hook, event_data)

            # If hook output contains a decision, honor it
            if result.get("success") and result.get("output"):
                try:
                    output = json.loads(result["output"])

                    # Handle block decision (for Stop, SubagentStop hooks)
                    if output.get("decision") == "block":
                        return SyncHookJSONOutput(
                            decision="block",
                            reason=output.get("reason", ""),
                            systemMessage=output.get("systemMessage", ""),
                            continue_=True
                        )

                    # Handle deny decision (for PreToolUse hooks)
                    if output.get("decision") == "deny":
                        return SyncHookJSONOutput(
                            decision="deny",
                            reason=output.get("reason", ""),
                        )

                    # Handle approve decision (for PreToolUse hooks)
                    if output.get("decision") == "approve":
                        return SyncHookJSONOutput(
                            decision="approve",
                        )

                except (json.JSONDecodeError, TypeError):
                    pass

        # No blocking/denying - allow default behavior
        return SyncHookJSONOutput()

    return hook_handler


# =============================================================================
# Hook Configuration Builder
# =============================================================================

def build_hooks_config(
    working_dir: str,
    project_id: Optional[str] = None
) -> Optional[Dict[str, List[HookMatcher]]]:
    """
    Build SDK hooks configuration to enable the CLI's hook system.

    IMPORTANT: Passing ANY hooks config to the SDK enables the CLI's entire
    hook processing system. Without it, the CLI skips ALL hooks (including plugins).

    This function:
    1. Always returns a hooks config (to enable CLI hook processing)
    2. Includes handlers for database hooks (from HooksCard)
    3. Plugin hooks are handled by the CLI natively once enabled

    Args:
        working_dir: Working directory for the session
        project_id: Optional project ID for database hooks

    Returns:
        Dict of hook configurations for SDK (never None - we always enable hooks)
    """
    hooks_config: Dict[str, List[HookMatcher]] = {}

    # Check which events have database hooks configured
    db_hook_events = set()
    if project_id:
        for event in SUPPORTED_HOOK_EVENTS:
            try:
                db_hooks = database.get_hooks_for_project_and_event(project_id, event)
                if db_hooks:
                    db_hook_events.add(event)
            except Exception as e:
                logger.debug(f"Failed to check for database {event} hooks: {e}")

    # Register handlers for events that have database hooks
    for event in db_hook_events:
        handler = create_hook_handler(event, working_dir, project_id)
        hooks_config[event] = [HookMatcher(hooks=[handler])]
        logger.info(f"[HookService] Registered {event} hook for database hooks")

    # CRITICAL: We must return a non-empty hooks config to enable the CLI's hook system.
    # If no database hooks exist, register a minimal handler that does nothing.
    #
    # IMPORTANT: We use PreCompact (not Stop!) to avoid interfering with plugin Stop hooks
    # like Ralph Loop. Using Stop would conflict with plugins that need to block stopping.
    if not hooks_config:
        async def noop_handler(
            hook_input: HookInput,
            tool_use_id: Optional[str],
            context: HookContext
        ) -> SyncHookJSONOutput:
            """No-op handler to enable CLI hook system."""
            return SyncHookJSONOutput()

        # Use PreCompact as it's rarely used and won't interfere with plugins
        hooks_config["PreCompact"] = [HookMatcher(hooks=[noop_handler])]
        logger.info(f"[HookService] Registered minimal PreCompact hook to enable CLI hook system")

    return hooks_config
