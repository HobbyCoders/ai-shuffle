"""
Unified Hook Service for AI Shuffle

This service provides a unified interface for handling hooks from multiple sources:
1. Plugin hooks - Shell/Python scripts defined in plugin hooks.json files
2. Database hooks - Custom hooks created via HooksCard

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

from claude_agent_sdk.types import (
    HookInput, HookContext, HookMatcher, SyncHookJSONOutput
)

from app.db import database
from app.core.hook_executor import execute_hook

logger = logging.getLogger(__name__)

# Type alias for stderr callback
# Callback receives (plugin_name, stderr_output, severity) where severity is "warning" or "info"
StderrCallback = Optional[Callable[[str, str, str], Awaitable[None]]]


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
            logger.debug(f"No hooks.json found at {hooks_file}")
            continue
        logger.debug(f"[HookService] Found hooks.json at {hooks_file}")

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
                            logger.debug(f"[HookService] Loaded {event_name} hook: {command[:80]}")

        except Exception as e:
            logger.warning(f"Failed to load hooks from {plugin_path}: {e}")

    return all_hooks


def _fix_python_command_for_windows(command: str) -> str:
    """
    Fix Python commands for Windows compatibility.

    On Windows, 'python3' often doesn't work (opens MS Store instead).
    This function replaces 'python3' with the current Python executable.

    Args:
        command: The original command string

    Returns:
        Fixed command string for the current platform
    """
    if sys.platform == "win32":
        # Replace python3 with the current Python executable
        # This handles cases like "python3 script.py" -> "C:\...\python.exe script.py"
        if command.startswith("python3 "):
            python_exe = sys.executable
            command = f'"{python_exe}" {command[8:]}'  # 8 = len("python3 ")
            logger.debug(f"Fixed Windows Python command: {command}")
    return command


async def execute_plugin_hook(
    hook_def: Dict[str, Any],
    hook_input: Dict[str, Any],
    working_dir: str,
    stderr_callback: StderrCallback = None
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
        stderr_callback: Optional callback to broadcast stderr to frontend

    Returns:
        Parsed JSON output from the hook, or None on error
    """
    command = hook_def.get("command", "")
    timeout = hook_def.get("timeout", 30)
    plugin_path = hook_def.get("plugin_path", "")

    if not command:
        return None

    # Fix python3 -> python on Windows
    command = _fix_python_command_for_windows(command)

    # Prepare environment with plugin root
    env = os.environ.copy()
    env["CLAUDE_PLUGIN_ROOT"] = plugin_path

    # On Windows, add common jq install locations to PATH for shell scripts
    # This is needed because Git Bash subprocess doesn't always inherit Windows PATH properly
    if sys.platform == "win32":
        import shutil
        # Check if jq is already in PATH
        if not shutil.which("jq"):
            # Common jq installation paths on Windows
            user_home = os.path.expanduser("~")
            common_jq_paths = [
                os.path.join(user_home, "AppData", "Local", "Microsoft", "WinGet", "Packages"),
                os.path.join(user_home, "scoop", "shims"),
                r"C:\ProgramData\chocolatey\bin",
                r"C:\tools",
            ]
            # Search for jq.exe in common paths
            for base_path in common_jq_paths:
                if os.path.exists(base_path):
                    # For WinGet, search subdirectories
                    if "WinGet" in base_path:
                        for root, dirs, files in os.walk(base_path):
                            if "jq.exe" in files:
                                env["PATH"] = root + os.pathsep + env.get("PATH", "")
                                logger.debug(f"Added jq path to env: {root}")
                                break
                    elif os.path.exists(os.path.join(base_path, "jq.exe")):
                        env["PATH"] = base_path + os.pathsep + env.get("PATH", "")
                        logger.debug(f"Added jq path to env: {base_path}")

    # Prepare stdin JSON
    stdin_data = json.dumps(hook_input)

    try:
        logger.debug(f"Executing plugin hook: {command}")

        # Run the command
        process = await asyncio.wait_for(
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
            process.communicate(stdin_data.encode()),
            timeout=timeout
        )

        logger.debug(f"[HookService] Hook return code: {process.returncode}")

        # Handle stderr - broadcast to frontend if callback provided
        if stderr:
            stderr_str = stderr.decode().strip()
            if stderr_str:
                # Extract plugin name from path for display
                plugin_name = Path(plugin_path).name if plugin_path else "unknown-plugin"

                # Determine severity based on return code
                # Return code 2 typically indicates a warning in CLI hooks
                severity = "warning" if process.returncode == 2 else "info"

                logger.debug(f"[HookService] Hook stderr ({severity}): {stderr_str[:200]}")

                # Call the stderr callback to broadcast to frontend
                if stderr_callback:
                    try:
                        await stderr_callback(plugin_name, stderr_str, severity)
                        logger.debug("[HookService] Broadcasted stderr to frontend")
                    except Exception as e:
                        logger.warning(f"[HookService] Failed to broadcast stderr: {e}")
                else:
                    logger.warning("[HookService] No stderr_callback provided!")
        else:
            logger.debug("[HookService] Hook produced no stderr")

        if stdout:
            output_str = stdout.decode().strip()
            if output_str:
                logger.debug(f"[HookService] Hook stdout: {output_str[:200]}")
                try:
                    return json.loads(output_str)
                except json.JSONDecodeError:
                    logger.debug(f"Plugin hook output not valid JSON: {output_str[:100]}")
                    return None

        return None

    except asyncio.TimeoutError:
        logger.warning(f"Plugin hook timed out after {timeout}s: {command}")
        try:
            process.kill()
            await process.wait()
        except Exception:
            pass
        return None
    except Exception as e:
        logger.warning(f"Plugin hook execution failed: {e}")
        return None


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
    plugin_stop_hooks: Optional[List[Dict[str, Any]]] = None,
    stderr_callback: StderrCallback = None
) -> Callable[[HookInput, Optional[str], HookContext], Awaitable[SyncHookJSONOutput]]:
    """
    Create a unified Stop hook handler that supports:
    1. Plugin Stop hooks from hooks.json
    2. Database Stop hooks

    This handler is registered with the SDK and called when Claude tries to stop.
    It can return {"decision": "block", "reason": "..."} to continue with a new prompt.

    Args:
        working_dir: The working directory for the session
        project_id: Optional project ID for database hooks
        plugin_stop_hooks: List of plugin Stop hook definitions
        stderr_callback: Optional async callback for broadcasting stderr to frontend

    Returns:
        Async hook handler function compatible with SDK
    """

    async def stop_hook_handler(
        hook_input: HookInput,
        tool_use_id: Optional[str],
        context: HookContext
    ) -> SyncHookJSONOutput:
        """Handle Stop hook for plugins and database hooks."""

        # Only handle Stop events (TypedDict doesn't support isinstance, check event name)
        if hook_input.get("hook_event_name") != "Stop":
            return SyncHookJSONOutput()

        logger.debug(f"Stop hook fired for {working_dir}")

        # Build the input dict for plugin scripts
        script_input = {
            "hook_event_name": "Stop",
            "session_id": context.get("session_id"),
            "cwd": working_dir,
            **hook_input
        }

        # 1. Execute plugin Stop hooks from hooks.json
        for hook_def in (plugin_stop_hooks or []):
            logger.debug(f"Executing plugin Stop hook: {hook_def.get('command')}")

            result = await execute_plugin_hook(hook_def, script_input, working_dir, stderr_callback)

            if result:
                decision = result.get("decision")
                if decision:
                    logger.debug(f"Plugin Stop hook returned decision: {decision}")
                    # When decision is "block", default continue_ to True (to continue the loop)
                    # The script may not include "continue" field explicitly
                    default_continue = decision == "block"
                    return SyncHookJSONOutput(
                        decision=decision,
                        reason=result.get("reason", ""),
                        systemMessage=result.get("systemMessage", ""),
                        continue_=result.get("continue", default_continue)
                    )

        # 2. Check for database Stop hooks
        db_hooks = await get_database_hooks_for_event(project_id, "Stop")
        for hook in db_hooks:
            logger.debug(f"Executing database Stop hook: {hook.get('name')}")

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
        logger.debug("No blocking hooks, allowing stop")
        return SyncHookJSONOutput()

    return stop_hook_handler


# =============================================================================
# Hook Configuration Builder
# =============================================================================

def create_plugin_hook_handler(
    event_name: str,
    hook_defs: List[Dict[str, Any]],
    working_dir: str,
    stderr_callback: StderrCallback = None
) -> Callable[[HookInput, Optional[str], HookContext], Awaitable[SyncHookJSONOutput]]:
    """
    Create a handler that executes all plugin hooks for an event.

    This is the key integration point - it wraps plugin shell/Python scripts
    in a Python async function that the SDK can call.

    Args:
        event_name: The hook event name (PreToolUse, PostToolUse, etc.)
        hook_defs: List of hook definitions to execute
        working_dir: Working directory for execution
        stderr_callback: Optional async callback for broadcasting stderr to frontend

    Returns:
        Async hook handler function compatible with SDK
    """

    async def handler(
        hook_input: HookInput,
        tool_use_id: Optional[str],
        context: HookContext
    ) -> SyncHookJSONOutput:
        """Execute all plugin hooks for this event."""
        # For PreToolUse/PostToolUse, get the tool name from the input
        tool_name = None
        if event_name in ("PreToolUse", "PostToolUse"):
            # The SDK provides tool_name in the hook input
            tool_name = hook_input.get("tool_name", "")

        logger.debug(f"[HookService] {event_name} hook fired for tool={tool_name}")

        # Build the input dict that gets passed to plugin scripts via stdin
        # On Windows, normalize paths to Unix-style for cross-platform compatibility
        # (Many plugins like security-guidance check for patterns like ".github/workflows/")
        normalized_input = dict(hook_input)
        if sys.platform == "win32" and "tool_input" in normalized_input and isinstance(normalized_input["tool_input"], dict):
            tool_input = dict(normalized_input["tool_input"])
            if "file_path" in tool_input:
                # Convert Windows backslashes to forward slashes
                tool_input["file_path"] = tool_input["file_path"].replace("\\", "/")
            normalized_input["tool_input"] = tool_input

        script_input = {
            "hook_event_name": event_name,
            "session_id": context.get("session_id"),
            "cwd": working_dir,
            **normalized_input
        }

        # Execute each hook
        for hook_def in hook_defs:
            matcher_pattern = hook_def.get("matcher")

            # Skip if matcher doesn't match tool name
            if matcher_pattern and tool_name:
                if not re.match(matcher_pattern, tool_name, re.IGNORECASE):
                    logger.debug(f"[HookService] Skipping - tool '{tool_name}' doesn't match pattern '{matcher_pattern}'")
                    continue

            logger.debug(f"[HookService] Executing hook: {hook_def.get('command')[:80]}")

            result = await execute_plugin_hook(hook_def, script_input, working_dir, stderr_callback)

            if result:
                logger.debug(f"{event_name} hook returned: {result}")

                # Check for any meaningful response (decision, systemMessage, reason)
                decision = result.get("decision")
                system_message = result.get("systemMessage", "")
                reason = result.get("reason", "")
                continue_ = result.get("continue", False)

                # Return response if hook returned anything meaningful
                if decision or system_message or reason:
                    return SyncHookJSONOutput(
                        decision=decision,
                        reason=reason,
                        systemMessage=system_message,
                        continue_=continue_
                    )

        return SyncHookJSONOutput()

    return handler


def build_hooks_config(
    working_dir: str,
    project_id: Optional[str] = None,
    plugin_paths: Optional[List[str]] = None,
    stderr_callback: StderrCallback = None
) -> Optional[Dict[str, List[HookMatcher]]]:
    """
    Build SDK hooks configuration from all sources.

    This creates the hooks dict to pass to ClaudeAgentOptions, combining:
    1. Plugin hooks from hooks.json files
    2. Database hooks

    IMPORTANT: The SDK does NOT auto-execute plugin hooks from hooks.json!
    This function bridges that gap by creating Python handlers that execute
    the plugin scripts.

    Args:
        working_dir: Working directory for the session
        project_id: Optional project ID for database hooks
        plugin_paths: List of enabled plugin paths
        stderr_callback: Optional async callback for broadcasting stderr to frontend

    Returns:
        Dict of hook configurations for SDK, or None if no hooks needed
    """
    hooks_config: Dict[str, List[HookMatcher]] = {}

    # Load hooks from all enabled plugins
    plugin_hooks = load_plugin_hooks(plugin_paths or [])

    # Log what hooks we found
    total_hooks = sum(len(hooks) for hooks in plugin_hooks.values())
    logger.debug(f"[HookService] Loaded {total_hooks} hooks from {len(plugin_paths or [])} plugins: {list(plugin_hooks.keys())}")

    # Create handlers for each hook event type
    for event_name, hook_defs in plugin_hooks.items():
        if event_name == "Stop":
            # Stop hooks are handled separately below
            continue

        if hook_defs:
            handler = create_plugin_hook_handler(event_name, hook_defs, working_dir, stderr_callback)
            hooks_config[event_name] = [HookMatcher(hooks=[handler])]
            logger.debug(f"[HookService] Registered {event_name} handler with {len(hook_defs)} hook(s)")

    # ALWAYS register the Stop hook handler for database hooks support
    stop_plugin_hooks = plugin_hooks.get("Stop", [])
    stop_handler = create_stop_hook_handler(working_dir, project_id, stop_plugin_hooks, stderr_callback)
    hooks_config["Stop"] = [HookMatcher(hooks=[stop_handler])]
    logger.debug(f"[HookService] Registered Stop handler with {len(stop_plugin_hooks)} plugin hook(s)")

    return hooks_config
