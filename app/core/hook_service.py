"""
Unified Hook Service for AI Shuffle

This service provides a unified interface for handling hooks from multiple sources:
1. Plugin hooks - Shell/Python scripts defined in plugin hooks.json files
2. Database hooks - Custom hooks created via HooksCard
3. Plugin state handlers - State files created by plugins (e.g., .claude/ralph-loop.local.md)

IMPORTANT: The Claude Agent SDK does NOT auto-execute plugin hooks!
Unlike the terminal CLI which reads hooks.json and executes the scripts,
the SDK only executes Python callbacks we provide via options.hooks.

This service bridges that gap by:
1. Reading all enabled plugins' hooks.json files
2. Creating Python wrapper functions that execute the plugin scripts
3. Registering those wrappers with the SDK

This enables plugins like Ralph Loop, security-guidance, etc. to work
through AI Shuffle just like they do in the terminal CLI.
"""

import asyncio
import json
import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Callable, Awaitable

import yaml

from claude_agent_sdk.types import (
    HookInput, HookContext, HookMatcher, SyncHookJSONOutput,
    StopHookInput, PreToolUseHookInput, PostToolUseHookInput
)

from app.db import database
from app.core.hook_executor import execute_hook

logger = logging.getLogger(__name__)


# =============================================================================
# Plugin Hook Loader
# =============================================================================

def load_plugin_hooks(plugin_paths: List[str]) -> Dict[str, List[Dict[str, Any]]]:
    """
    Load hooks from all plugins' hooks.json files.

    Args:
        plugin_paths: List of plugin installation paths

    Returns:
        Dict mapping hook event names to list of hook definitions
        Each hook definition includes: command, matcher, timeout, plugin_path
    """
    all_hooks: Dict[str, List[Dict[str, Any]]] = {}

    for plugin_path in plugin_paths:
        hooks_file = Path(plugin_path) / "hooks" / "hooks.json"
        if not hooks_file.exists():
            continue

        try:
            with open(hooks_file, 'r') as f:
                data = json.load(f)

            plugin_hooks = data.get("hooks", {})
            for event_name, matchers in plugin_hooks.items():
                if event_name not in all_hooks:
                    all_hooks[event_name] = []

                for matcher_def in matchers:
                    matcher_pattern = matcher_def.get("matcher")  # Optional tool pattern
                    for hook_def in matcher_def.get("hooks", []):
                        if hook_def.get("type") == "command":
                            command = hook_def.get("command", "")
                            # Replace ${CLAUDE_PLUGIN_ROOT} with actual path
                            command = command.replace("${CLAUDE_PLUGIN_ROOT}", plugin_path)

                            all_hooks[event_name].append({
                                "command": command,
                                "matcher": matcher_pattern,
                                "timeout": hook_def.get("timeout", 30),
                                "plugin_path": plugin_path
                            })
                            logger.debug(f"Loaded {event_name} hook from {plugin_path}: {command}")

        except Exception as e:
            logger.warning(f"Failed to load hooks from {plugin_path}: {e}")

    return all_hooks


async def execute_plugin_hook(
    hook_def: Dict[str, Any],
    hook_input: Dict[str, Any],
    working_dir: str
) -> Optional[Dict[str, Any]]:
    """
    Execute a plugin hook command and return its output.

    This mimics how the CLI executes plugin hooks:
    1. Passes hook input as JSON via stdin
    2. Parses JSON output from stdout

    Args:
        hook_def: Hook definition with command, timeout, plugin_path
        hook_input: The hook input data to pass via stdin
        working_dir: The working directory for execution

    Returns:
        Parsed JSON output from the hook, or None on error
    """
    command = hook_def.get("command", "")
    timeout = hook_def.get("timeout", 30)
    plugin_path = hook_def.get("plugin_path", "")

    if not command:
        return None

    # Prepare environment with plugin root
    env = os.environ.copy()
    env["CLAUDE_PLUGIN_ROOT"] = plugin_path

    # Prepare stdin JSON
    stdin_data = json.dumps(hook_input)

    try:
        logger.debug(f"Executing plugin hook: {command}")

        # Run the command
        result = await asyncio.wait_for(
            asyncio.create_subprocess_shell(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=working_dir,
                env=env
            ),
            timeout=timeout
        )

        stdout, stderr = await asyncio.wait_for(
            result.communicate(stdin_data.encode()),
            timeout=timeout
        )

        if stderr:
            logger.debug(f"Plugin hook stderr: {stderr.decode()}")

        if stdout:
            output_str = stdout.decode().strip()
            if output_str:
                try:
                    return json.loads(output_str)
                except json.JSONDecodeError:
                    logger.debug(f"Plugin hook output not JSON: {output_str}")
                    return None

        return None

    except asyncio.TimeoutError:
        logger.warning(f"Plugin hook timed out after {timeout}s: {command}")
        return None
    except Exception as e:
        logger.warning(f"Plugin hook execution failed: {e}")
        return None


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

        # Parse frontmatter using yaml library for robust handling
        frontmatter_text = '\n'.join(lines[1:end_idx])
        state = yaml.safe_load(frontmatter_text) or {}

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
    project_id: Optional[str] = None,
    plugin_stop_hooks: Optional[List[Dict[str, Any]]] = None
) -> Callable[[HookInput, Optional[str], HookContext], Awaitable[SyncHookJSONOutput]]:
    """
    Create a unified Stop hook handler that supports:
    1. Plugin Stop hooks from hooks.json
    2. Plugin loop state detection (like Ralph Loop)
    3. Database Stop hooks

    This handler is registered with the SDK and called when Claude tries to stop.
    It can return {"decision": "block", "reason": "..."} to continue with a new prompt.

    Args:
        working_dir: The working directory for the session
        project_id: Optional project ID for database hooks
        plugin_stop_hooks: List of plugin Stop hook definitions

    Returns:
        Async hook handler function compatible with SDK
    """

    async def stop_hook_handler(
        hook_input: HookInput,
        tool_use_id: Optional[str],
        context: HookContext
    ) -> SyncHookJSONOutput:
        """Handle Stop hook for plugins, loops, and database hooks."""

        # Only handle Stop events (TypedDict doesn't support isinstance, check event name)
        if hook_input.get("hook_event_name") != "Stop":
            return SyncHookJSONOutput()

        logger.info(f"[HookService] Stop hook fired for {working_dir}")

        # Build the input dict for plugin scripts
        script_input = {
            "hook_event_name": "Stop",
            "session_id": context.get("session_id"),
            "cwd": working_dir,
            **hook_input
        }

        # 1. Execute plugin Stop hooks from hooks.json
        for hook_def in (plugin_stop_hooks or []):
            logger.info(f"[HookService] Executing plugin Stop hook: {hook_def.get('command')}")

            result = await execute_plugin_hook(hook_def, script_input, working_dir)

            if result:
                decision = result.get("decision")
                if decision:
                    logger.info(f"[HookService] Plugin Stop hook returned decision: {decision}")
                    return SyncHookJSONOutput(
                        decision=decision,
                        reason=result.get("reason", ""),
                        systemMessage=result.get("systemMessage", ""),
                        continue_=result.get("continue", False)
                    )

        # 2. Check for active plugin loops (run sync file I/O in thread to avoid blocking)
        # This is for plugins like Ralph Loop that use state files
        plugin_state = await asyncio.to_thread(detect_plugin_loop_state, working_dir)
        if plugin_state:
            plugin = plugin_state["plugin"]
            state = plugin_state["state"]

            logger.info(f"[HookService] Active {plugin} loop detected")

            # Handle Ralph Loop specifically (file I/O in thread)
            if plugin == "ralph-loop":
                result = await asyncio.to_thread(_handle_ralph_loop_stop, working_dir, state)
                if result:
                    return result

        # 3. Check for database Stop hooks
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

def create_plugin_hook_handler(
    event_name: str,
    hook_defs: List[Dict[str, Any]],
    working_dir: str
) -> Callable[[HookInput, Optional[str], HookContext], Awaitable[SyncHookJSONOutput]]:
    """
    Create a handler that executes all plugin hooks for an event.

    This is the key integration point - it wraps plugin shell/Python scripts
    in a Python async function that the SDK can call.

    Args:
        event_name: The hook event name (PreToolUse, PostToolUse, etc.)
        hook_defs: List of hook definitions to execute
        working_dir: Working directory for execution

    Returns:
        Async hook handler function compatible with SDK
    """

    async def handler(
        hook_input: HookInput,
        tool_use_id: Optional[str],
        context: HookContext
    ) -> SyncHookJSONOutput:
        """Execute all plugin hooks for this event."""
        logger.info(f"[HookService] {event_name} hook fired for {working_dir}")

        # For PreToolUse/PostToolUse, check tool name against matcher
        tool_name = None
        if event_name in ("PreToolUse", "PostToolUse"):
            tool_name = hook_input.get("tool_name", "")

        # Build the input dict that gets passed to plugin scripts via stdin
        script_input = {
            "hook_event_name": event_name,
            "session_id": context.get("session_id"),
            "cwd": working_dir,
            **hook_input
        }

        # Execute each hook
        for hook_def in hook_defs:
            matcher_pattern = hook_def.get("matcher")

            # Skip if matcher doesn't match tool name
            if matcher_pattern and tool_name:
                if not re.match(matcher_pattern, tool_name, re.IGNORECASE):
                    continue

            logger.debug(f"[HookService] Executing {event_name} hook: {hook_def.get('command')}")

            result = await execute_plugin_hook(hook_def, script_input, working_dir)

            if result:
                # Check for decisions (block, allow, etc.)
                decision = result.get("decision")
                if decision:
                    logger.info(f"[HookService] {event_name} hook returned decision: {decision}")
                    return SyncHookJSONOutput(
                        decision=decision,
                        reason=result.get("reason", ""),
                        systemMessage=result.get("systemMessage", ""),
                        continue_=result.get("continue", False)
                    )

        return SyncHookJSONOutput()

    return handler


def build_hooks_config(
    working_dir: str,
    project_id: Optional[str] = None,
    plugin_paths: Optional[List[str]] = None
) -> Optional[Dict[str, List[HookMatcher]]]:
    """
    Build SDK hooks configuration from all sources.

    This creates the hooks dict to pass to ClaudeAgentOptions, combining:
    1. Plugin hooks from hooks.json files
    2. Plugin state detection (for loops like Ralph Loop)
    3. Database hooks

    IMPORTANT: The SDK does NOT auto-execute plugin hooks from hooks.json!
    This function bridges that gap by creating Python handlers that execute
    the plugin scripts.

    Args:
        working_dir: Working directory for the session
        project_id: Optional project ID for database hooks
        plugin_paths: List of enabled plugin paths

    Returns:
        Dict of hook configurations for SDK, or None if no hooks needed
    """
    hooks_config: Dict[str, List[HookMatcher]] = {}

    # Load hooks from all enabled plugins
    plugin_hooks = load_plugin_hooks(plugin_paths or [])

    # Log what hooks we found
    for event_name, hooks in plugin_hooks.items():
        logger.info(f"[HookService] Found {len(hooks)} {event_name} hook(s) from plugins")

    # Create handlers for each hook event type
    for event_name, hook_defs in plugin_hooks.items():
        if event_name == "Stop":
            # Stop hooks are special - we combine plugin hooks with our state detection
            continue  # Handled below

        if hook_defs:
            handler = create_plugin_hook_handler(event_name, hook_defs, working_dir)
            hooks_config[event_name] = [HookMatcher(hooks=[handler])]
            logger.info(f"[HookService] Registered {event_name} handler with {len(hook_defs)} plugin hook(s)")

    # ALWAYS register the Stop hook handler.
    # This is critical because:
    # 1. Plugin loops (like Ralph Loop) create their state files DURING execution
    # 2. Plugin Stop hooks from hooks.json also need to be executed
    # The handler dynamically checks for plugin states and executes scripts when Stop fires.
    stop_plugin_hooks = plugin_hooks.get("Stop", [])
    stop_handler = create_stop_hook_handler(working_dir, project_id, stop_plugin_hooks)
    hooks_config["Stop"] = [HookMatcher(hooks=[stop_handler])]
    logger.info(f"[HookService] Registered Stop hook handler with {len(stop_plugin_hooks)} plugin hook(s)")

    return hooks_config
