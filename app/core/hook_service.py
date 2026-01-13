"""
Unified Hook Service for AI Shuffle

This service provides a unified interface for handling hooks from multiple sources:
1. Plugin hooks - State files created by plugins (e.g., .claude/ralph-loop.local.md)
2. Database hooks - Custom hooks created via HooksCard
3. SDK hooks - Programmatic hooks passed to the Claude SDK

The key feature is the Stop hook handler which enables plugins like Ralph Loop
to work properly through AI Shuffle by intercepting Claude's stop events and
returning continuation prompts when appropriate.
"""

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Callable, Awaitable

from claude_agent_sdk.types import (
    HookInput, HookContext, HookMatcher, SyncHookJSONOutput,
    StopHookInput, PreToolUseHookInput, PostToolUseHookInput
)

from app.db import database
from app.core.hook_executor import execute_hook

logger = logging.getLogger(__name__)


# =============================================================================
# Plugin State Detection
# =============================================================================

def detect_plugin_loop_state(working_dir: str) -> Optional[Dict[str, Any]]:
    """
    Detect if any plugin has an active loop state.

    This checks for known plugin state files that indicate a loop is active.
    Currently supports:
    - Ralph Loop: .claude/ralph-loop.local.md

    The design is extensible - add new plugin detectors as needed.

    Args:
        working_dir: The working directory to check

    Returns:
        Dict with plugin info and state, or None if no active loop
    """
    # Check for Ralph Loop state
    ralph_state = _read_ralph_loop_state(working_dir)
    if ralph_state:
        return {
            "plugin": "ralph-loop",
            "state": ralph_state
        }

    # Add more plugin detectors here as needed
    # e.g., check for other plugins that use Stop hooks

    return None


def _read_ralph_loop_state(working_dir: str) -> Optional[Dict[str, Any]]:
    """
    Read Ralph Loop state from .claude/ralph-loop.local.md

    Returns dict with:
        - iteration: Current iteration number
        - max_iterations: Max iterations (0 = unlimited)
        - completion_promise: Promise phrase or None
        - prompt: The task prompt
    Or None if no active Ralph Loop
    """
    state_file = Path(working_dir) / ".claude" / "ralph-loop.local.md"
    if not state_file.exists():
        return None

    try:
        content = state_file.read_text()

        # Parse YAML frontmatter (between --- markers)
        lines = content.split('\n')
        if not lines or lines[0].strip() != '---':
            return None

        # Find the closing ---
        end_idx = None
        for i, line in enumerate(lines[1:], 1):
            if line.strip() == '---':
                end_idx = i
                break

        if end_idx is None:
            return None

        # Parse frontmatter
        frontmatter = '\n'.join(lines[1:end_idx])
        state = {}
        for line in frontmatter.split('\n'):
            if ':' in line:
                key, value = line.split(':', 1)
                key = key.strip()
                value = value.strip()
                # Remove quotes from values
                if value.startswith('"') and value.endswith('"'):
                    value = value[1:-1]
                elif value == 'null':
                    value = None
                elif value.isdigit():
                    value = int(value)
                state[key] = value

        # Get prompt (everything after closing ---)
        prompt = '\n'.join(lines[end_idx + 1:]).strip()
        state['prompt'] = prompt

        return state
    except Exception as e:
        logger.warning(f"Failed to read Ralph Loop state: {e}")
        return None


def _update_plugin_iteration(working_dir: str, plugin: str, new_iteration: int) -> bool:
    """Update the iteration count in a plugin's state file."""
    if plugin == "ralph-loop":
        state_file = Path(working_dir) / ".claude" / "ralph-loop.local.md"
        if not state_file.exists():
            return False

        try:
            content = state_file.read_text()
            new_content = re.sub(
                r'^iteration:\s*\d+',
                f'iteration: {new_iteration}',
                content,
                flags=re.MULTILINE
            )
            state_file.write_text(new_content)
            return True
        except Exception as e:
            logger.warning(f"Failed to update {plugin} iteration: {e}")
            return False

    return False


def _remove_plugin_state(working_dir: str, plugin: str) -> bool:
    """Remove a plugin's state file (when loop completes)."""
    if plugin == "ralph-loop":
        state_file = Path(working_dir) / ".claude" / "ralph-loop.local.md"
        if state_file.exists():
            try:
                state_file.unlink()
                return True
            except Exception as e:
                logger.warning(f"Failed to remove {plugin} state: {e}")
    return False


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
# Unified Stop Hook Handler
# =============================================================================

def create_stop_hook_handler(
    working_dir: str,
    project_id: Optional[str] = None
) -> Callable[[HookInput, Optional[str], HookContext], Awaitable[SyncHookJSONOutput]]:
    """
    Create a unified Stop hook handler that supports:
    1. Plugin loops (like Ralph Loop)
    2. Database Stop hooks

    This handler is registered with the SDK and called when Claude tries to stop.
    It can return {"decision": "block", "reason": "..."} to continue with a new prompt.

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
        """Handle Stop hook for plugin loops and database hooks."""

        # Only handle Stop events
        if not isinstance(hook_input, StopHookInput):
            return SyncHookJSONOutput()

        logger.info(f"[HookService] Stop hook fired for {working_dir}")

        # 1. Check for active plugin loops
        plugin_state = detect_plugin_loop_state(working_dir)
        if plugin_state:
            plugin = plugin_state["plugin"]
            state = plugin_state["state"]

            logger.info(f"[HookService] Active {plugin} detected")

            # Handle Ralph Loop specifically
            if plugin == "ralph-loop":
                result = _handle_ralph_loop_stop(working_dir, state)
                if result:
                    return result

        # 2. Check for database Stop hooks
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
        logger.debug("[HookService] No blocking hooks, allowing stop")
        return SyncHookJSONOutput()

    return stop_hook_handler


def _handle_ralph_loop_stop(working_dir: str, state: Dict[str, Any]) -> Optional[SyncHookJSONOutput]:
    """
    Handle Stop hook for Ralph Loop plugin.

    Args:
        working_dir: Working directory
        state: Ralph Loop state from state file

    Returns:
        SyncHookJSONOutput to block and continue, or None to allow stop
    """
    iteration = state.get('iteration', 1)
    max_iterations = state.get('max_iterations', 0)
    completion_promise = state.get('completion_promise')
    prompt = state.get('prompt', '')

    logger.info(f"[RalphLoop] Stop hook - iteration {iteration}, max={max_iterations}")

    # Check if max iterations reached
    if max_iterations > 0 and iteration >= max_iterations:
        logger.info(f"[RalphLoop] Max iterations ({max_iterations}) reached, stopping")
        _remove_plugin_state(working_dir, "ralph-loop")
        return None

    # TODO: Check completion promise in last assistant message
    # For now, we rely on the plugin's own completion promise detection
    # which happens in the shell script hook

    # Continue loop with same prompt
    next_iteration = iteration + 1
    _update_plugin_iteration(working_dir, "ralph-loop", next_iteration)

    # Build system message
    if completion_promise:
        system_msg = f"🔄 Ralph iteration {next_iteration} | To stop: output <promise>{completion_promise}</promise>"
    else:
        system_msg = f"🔄 Ralph iteration {next_iteration} | No completion promise set"

    logger.info(f"[RalphLoop] Continuing to iteration {next_iteration}")

    return SyncHookJSONOutput(
        decision="block",
        reason=prompt,  # This becomes the next prompt
        systemMessage=system_msg,
        continue_=True
    )


# =============================================================================
# Hook Configuration Builder
# =============================================================================

def build_hooks_config(
    working_dir: str,
    project_id: Optional[str] = None
) -> Optional[Dict[str, List[HookMatcher]]]:
    """
    Build SDK hooks configuration from all sources.

    This creates the hooks dict to pass to ClaudeAgentOptions, combining:
    1. Plugin state detection (for loops)
    2. Database hooks

    Args:
        working_dir: Working directory for the session
        project_id: Optional project ID for database hooks

    Returns:
        Dict of hook configurations for SDK, or None if no hooks needed
    """
    hooks_config: Dict[str, List[HookMatcher]] = {}

    # Check if any plugin loops are active OR if there are database Stop hooks
    has_plugin_loop = detect_plugin_loop_state(working_dir) is not None

    # Check for database Stop hooks (sync check for now)
    has_db_stop_hooks = False
    if project_id:
        try:
            db_hooks = database.get_hooks_for_project_and_event(project_id, "Stop")
            has_db_stop_hooks = bool(db_hooks)
        except Exception:
            pass

    # Register Stop hook handler if needed
    if has_plugin_loop or has_db_stop_hooks:
        stop_handler = create_stop_hook_handler(working_dir, project_id)
        hooks_config["Stop"] = [HookMatcher(hooks=[stop_handler])]
        logger.info(f"[HookService] Registered Stop hook (plugin_loop={has_plugin_loop}, db_hooks={has_db_stop_hooks})")

    return hooks_config if hooks_config else None
