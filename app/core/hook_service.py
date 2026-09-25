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

from app.core.agent_types import (
    HookInput, HookContext, HookMatcher, SyncHookJSONOutput,
    PreToolUseHookSpecificOutput, PostToolUseHookSpecificOutput,
)

from app.db import database
from app.db.async_database import run_sync
from app.core.hook_executor import execute_hook, kill_hook_process, spawn_hook_shell

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

                            # Stop hooks process the full transcript, which grows with
                            # session length. Give them a longer default timeout (120s)
                            # to avoid timeouts on long chats (e.g., ralph-loop).
                            default_timeout = 120 if event_name == "Stop" else 30
                            all_hooks[event_name].append({
                                "command": command,
                                "matcher": matcher_pattern,
                                "timeout": hook_def.get("timeout", default_timeout),
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


def _get_jq_shim_env(env: dict) -> dict:
    """
    Add the self-contained Python jq shim to PATH.

    This ensures shell scripts can call 'jq' and have it handled
    by our Python implementation, eliminating the need for external
    jq installation on any platform.

    The jq shim scripts (bin/jq and bin/jq.bat) are fully self-contained
    with all Python code embedded inline - no external module imports needed.
    This makes them portable and independent of any specific project folder.

    Args:
        env: The current environment dict

    Returns:
        Modified environment dict with jq shim in PATH
    """
    # Get the path to the jq shim bin directory
    shim_dir = Path(__file__).parent / "jq_shim" / "bin"

    # Prepend to PATH (so our shim takes precedence over any system jq)
    current_path = env.get("PATH", "")
    env["PATH"] = str(shim_dir) + os.pathsep + current_path

    # Point the shim at a real interpreter. On Windows `python3` resolves to
    # the Microsoft Store stub and Git Bash can't feed /dev/fd/3 to a native
    # Python, so the bash shim uses JQ_SHIM_PYTHON when set (issue #572).
    env.setdefault("JQ_SHIM_PYTHON", sys.executable)

    return env


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

    # Add Python jq shim to PATH (cross-platform)
    # This eliminates the need for external jq installation
    env = _get_jq_shim_env(env)

    # Prepare stdin JSON
    stdin_data = json.dumps(hook_input)

    try:
        logger.debug(f"Executing plugin hook: {command}")

        # Run the command (through bash on Windows — issue #572)
        process = await asyncio.wait_for(
            spawn_hook_shell(
                command,
                stdin=subprocess.PIPE,
                cwd=working_dir,
                env=env,
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
        logger.warning(f"Plugin hook timed out after {timeout}s: {command} (cwd={working_dir})")
        logger.warning(f"If this is a Stop hook (e.g., ralph-loop), the transcript may be too large for the timeout.")
        try:
            await kill_hook_process(process)
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
        hooks = await run_sync(database.get_hooks_for_project_and_event, project_id, event)
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

        # Build the input dict for plugin scripts.
        # NOTE: HookContext only carries `signal` at runtime — session_id comes
        # from hook_input (every *HookInput TypedDict carries session_id: str).
        # context.get(...) is a defensive fallback only. Since **hook_input is
        # spread after "session_id" here, hook_input's value already wins when
        # present; the explicit .get(...) covers the case where it's missing.
        script_input = {
            "hook_event_name": "Stop",
            "session_id": hook_input.get("session_id") or context.get("session_id"),
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

    Output type routing (SDK 0.1.81+):
        - PreToolUse  → PreToolUseHookSpecificOutput  (permissionDecision, permissionDecisionReason)
        - PostToolUse → PostToolUseHookSpecificOutput  (updatedToolOutput)
          Plugin scripts may include "updated_output" in their JSON stdout to rewrite tool output.
        - All other events → SyncHookJSONOutput (decision, reason, systemMessage, continue_)
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

        # NOTE: HookContext only carries `signal` at runtime — session_id comes
        # from hook_input (every *HookInput TypedDict carries session_id: str).
        # context.get(...) is a defensive fallback only. Since **normalized_input
        # is spread after "session_id" here, hook_input's value already wins when
        # present; the explicit .get(...) covers the case where it's missing.
        script_input = {
            "hook_event_name": event_name,
            "session_id": normalized_input.get("session_id") or context.get("session_id"),
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

                if event_name == "PreToolUse":
                    # Use PreToolUseHookSpecificOutput for permission-aware responses.
                    # Valid permissionDecision values: "allow", "deny", "ask", "defer".
                    decision = result.get("decision")
                    reason = result.get("reason", "")
                    if decision or reason:
                        output: PreToolUseHookSpecificOutput = {
                            "hookEventName": "PreToolUse",
                        }
                        if decision:
                            output["permissionDecision"] = decision
                        if reason:
                            output["permissionDecisionReason"] = reason
                        # The permission decision MUST be nested under
                        # hookSpecificOutput. A bare top-level dict is silently
                        # ignored by the CLI schema (lab-confirmed fail-open).
                        return SyncHookJSONOutput(hookSpecificOutput=output)

                elif event_name == "PostToolUse":
                    # Use PostToolUseHookSpecificOutput.
                    # Plugin scripts may set "updated_output" to rewrite the tool result
                    # seen by the model; omitting the key leaves tool output unchanged.
                    updated_output = result.get("updated_output")
                    additional_context = result.get("additionalContext", "")
                    if updated_output is not None or additional_context:
                        output_post: PostToolUseHookSpecificOutput = {
                            "hookEventName": "PostToolUse",
                        }
                        if updated_output is not None:
                            output_post["updatedToolOutput"] = updated_output
                        if additional_context:
                            output_post["additionalContext"] = additional_context
                        # Nest under hookSpecificOutput (same contract as
                        # PreToolUse); a bare top-level dict is ignored by the CLI.
                        return SyncHookJSONOutput(hookSpecificOutput=output_post)

                else:
                    # All other events: Stop, SessionStart/End, UserPromptSubmit,
                    # PreCompact, SubagentStop, Notification — use SyncHookJSONOutput.
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


# =============================================================================
# Database PreToolUse Hook Handler (block-capable)
# =============================================================================

async def _execute_db_bash_hook(
    hook: Dict[str, Any],
    script_input: Dict[str, Any],
    working_dir: str,
) -> tuple[str, Optional[int], Optional[str], Optional[str]]:
    """
    Execute a DB `bash` hook command, passing the CLI hook contract on stdin as JSON.

    Mirrors the CLI/plugin contract: the command receives a JSON object on stdin
    (hook_event_name, tool_name, tool_input, session_id, cwd). Env vars from the
    legacy `execute_bash_hook` path are also supplied for backward compatibility.

    Returns a tuple (status, returncode, stdout, stderr):
        status == "ok"      → process ran to completion; returncode/stdout/stderr set
        status == "timeout" → process exceeded its timeout (killed)
        status == "error"   → process could not be spawned or crashed while reading
    On timeout/error the returncode/stdout/stderr are None. Callers MUST treat
    both as "no opinion" (do not block) — a broken/slow user hook must never
    hard-fail a tool call.
    """
    command = hook.get("command") or ""
    if not command:
        return ("error", None, None, None)

    # `timeout` is stored in milliseconds in the project_hooks table.
    timeout_ms = hook.get("timeout") or 30000
    timeout_seconds = timeout_ms / 1000

    command = _fix_python_command_for_windows(command)

    # Env vars mirror the legacy execute_bash_hook contract (HOOK_* keys) so
    # existing bash hooks that read env still work; stdin JSON is the primary path.
    env = os.environ.copy()
    env["HOOK_EVENT"] = "PreToolUse"
    env["HOOK_TOOL_NAME"] = str(script_input.get("tool_name", "") or "")
    env["HOOK_DATA"] = json.dumps(script_input)

    stdin_data = json.dumps(script_input)

    try:
        # Spawn through bash on Windows: cmd.exe mangles bash quoting, the JSON
        # decision never parses, and a blocking guard fails OPEN (issue #572).
        process = await spawn_hook_shell(
            command,
            stdin=subprocess.PIPE,
            cwd=working_dir,
            env=env,
        )
    except Exception as e:
        logger.warning(f"DB PreToolUse hook failed to spawn ({command[:80]}): {e}")
        return ("error", None, None, None)

    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(stdin_data.encode()),
            timeout=timeout_seconds,
        )
    except asyncio.TimeoutError:
        logger.warning(
            f"DB PreToolUse hook timed out after {timeout_ms}ms ({command[:80]}); "
            f"treating as no-opinion (tool not blocked)"
        )
        try:
            await kill_hook_process(process)
        except Exception:
            pass
        return ("timeout", None, None, None)
    except Exception as e:
        logger.warning(f"DB PreToolUse hook crashed while reading output: {e}")
        return ("error", None, None, None)

    return (
        "ok",
        process.returncode,
        stdout.decode("utf-8", errors="replace") if stdout else "",
        stderr.decode("utf-8", errors="replace") if stderr else "",
    )


def _map_db_pretooluse_decision(
    returncode: Optional[int],
    stdout_str: Optional[str],
    stderr_str: Optional[str],
    hook_id: Optional[str] = None,
) -> Optional[PreToolUseHookSpecificOutput]:
    """
    Map a DB bash hook's result to a PreToolUse permission decision.

    Semantics (matching the bundled CLI's hook contract):
      - Parseable JSON stdout:
          * hookSpecificOutput.permissionDecision (or top-level permissionDecision)
            in {allow, deny, ask}  → mapped directly, with permissionDecisionReason.
          * root "decision": "block"           → deny (reason passthrough)
          * root "decision": "approve"/"allow" → allow (reason passthrough)
      - No parseable decision:
          * exit code 2  → deny; stderr is surfaced as the block reason.
          * exit code 0  → no opinion (proceed to normal permission flow).
          * other codes  → no opinion (a failed hook must not block).

    Returns a PreToolUseHookSpecificOutput dict, or None for "no opinion".
    """
    parsed: Any = None
    if stdout_str and stdout_str.strip():
        try:
            parsed = json.loads(stdout_str.strip())
        except json.JSONDecodeError:
            # A hook that emitted output but no parseable JSON gets NO say in
            # the decision — if it was meant to block, it just failed OPEN.
            # Make that loud instead of a silent debug-level fallthrough.
            logger.warning(
                f"DB PreToolUse hook {hook_id or '<unknown>'} produced unparseable "
                f"stdout ({stdout_str.strip()[:120]!r}); treating as no-opinion — "
                f"a blocking decision written here FAILS OPEN"
            )
            parsed = None

    if isinstance(parsed, dict):
        # Shape 1: permissionDecision (nested under hookSpecificOutput or top-level)
        perm = None
        reason = None
        hso = parsed.get("hookSpecificOutput")
        if isinstance(hso, dict):
            perm = hso.get("permissionDecision")
            reason = hso.get("permissionDecisionReason")
        if perm is None:
            perm = parsed.get("permissionDecision")
            reason = reason or parsed.get("permissionDecisionReason")
        if perm in ("allow", "deny", "ask"):
            output: PreToolUseHookSpecificOutput = {
                "hookEventName": "PreToolUse",
                "permissionDecision": perm,
            }
            if reason:
                output["permissionDecisionReason"] = reason
            return output

        # Shape 2: root-level decision (block/approve)
        decision = parsed.get("decision")
        decision_reason = parsed.get("reason") or reason
        if decision == "block":
            output = {"hookEventName": "PreToolUse", "permissionDecision": "deny"}
            if decision_reason:
                output["permissionDecisionReason"] = decision_reason
            return output
        if decision in ("approve", "allow"):
            output = {"hookEventName": "PreToolUse", "permissionDecision": "allow"}
            if decision_reason:
                output["permissionDecisionReason"] = decision_reason
            return output

    # No parseable decision — fall back to exit-code semantics.
    if returncode == 2:
        output = {"hookEventName": "PreToolUse", "permissionDecision": "deny"}
        reason = (stderr_str or "").strip()
        if reason:
            output["permissionDecisionReason"] = reason
        return output

    # exit 0 (no output) or any other non-blocking failure → no opinion.
    return None


def create_db_pretooluse_hook_handler(
    working_dir: str,
    project_id: Optional[str] = None,
    stderr_callback: StderrCallback = None,
) -> Callable[[HookInput, Optional[str], HookContext], Awaitable[SyncHookJSONOutput]]:
    """
    Create a PreToolUse handler that executes DB-backed (project_hooks) hooks and
    can block tool calls.

    DB hooks are re-queried at fire time (via get_database_hooks_for_event, which
    also returns app-level rows with project_id IS NULL) so hooks created mid-session
    take effect. Only `bash` hooks can block; `webhook`/`message` PreToolUse hooks
    are logged as unsupported-for-blocking and skipped.

    The first bash hook that returns a decision wins; otherwise the handler returns
    a default (no-opinion) SyncHookJSONOutput and the tool proceeds to the normal
    permission flow.
    """

    async def handler(
        hook_input: HookInput,
        tool_use_id: Optional[str],
        context: HookContext,
    ) -> SyncHookJSONOutput:
        if hook_input.get("hook_event_name") != "PreToolUse":
            return SyncHookJSONOutput()

        tool_name = hook_input.get("tool_name", "") or ""

        db_hooks = await get_database_hooks_for_event(project_id, "PreToolUse")
        if not db_hooks:
            return SyncHookJSONOutput()

        # Build the CLI hook contract passed to each hook on stdin as JSON.
        # NOTE: the SDK's HookContext TypedDict only carries `signal` — it never
        # has session_id. The real session id arrives via hook_input (every
        # *HookInput TypedDict carries session_id: str). context.get(...) is kept
        # only as a defensive fallback since context is a plain dict at runtime.
        script_input = {
            "hook_event_name": "PreToolUse",
            "tool_name": tool_name,
            "tool_input": hook_input.get("tool_input", {}),
            "session_id": hook_input.get("session_id") or context.get("session_id"),
            "cwd": working_dir,
        }

        for hook in db_hooks:
            # The query filters enabled=TRUE, but double-check defensively.
            if not hook.get("enabled", True):
                continue

            # tool_matcher: exact tool name or regex. Mirror the DB dispatcher
            # (execute_hooks_for_event) which uses re.fullmatch — an exact name
            # fullmatches itself, and patterns like "Bash|Edit" work as expected.
            tool_matcher = hook.get("tool_matcher")
            if tool_matcher and tool_name:
                try:
                    if not re.fullmatch(tool_matcher, tool_name):
                        continue
                except re.error:
                    # Invalid regex → fall back to exact-string comparison.
                    if tool_matcher != tool_name:
                        continue

            hook_type = hook.get("hook_type")
            if hook_type != "bash":
                # Only bash hooks can produce a blocking decision. webhook/message
                # PreToolUse hooks cannot block a tool; skip them here.
                logger.warning(
                    f"DB PreToolUse hook {hook.get('id')} (type={hook_type}) cannot block a "
                    f"tool call; skipping for blocking evaluation"
                )
                continue

            status, returncode, stdout_str, stderr_str = await _execute_db_bash_hook(
                hook, script_input, working_dir
            )

            # Persist triggered status (best-effort; never let this block a tool).
            hook_id = hook.get("id")
            if hook_id:
                try:
                    await run_sync(database.update_hook_triggered, hook_id, status == "ok")
                except Exception as e:
                    logger.debug(f"Failed to update hook triggered status: {e}")

            if status != "ok":
                # timeout/error → no opinion for this hook; try the next one.
                continue

            decision = _map_db_pretooluse_decision(
                returncode, stdout_str, stderr_str, hook_id=hook.get("id")
            )
            if decision is not None:
                logger.debug(
                    f"DB PreToolUse hook {hook.get('id')} decided "
                    f"{decision.get('permissionDecision')} for tool={tool_name}"
                )
                # The permission decision MUST be nested under hookSpecificOutput
                # (SyncHookJSONOutput contract). Returning the bare
                # PreToolUseHookSpecificOutput at the top level is silently
                # dropped by the CLI schema — the hook fires, computes deny, and
                # the tool runs anyway (lab-confirmed fail-open).
                return SyncHookJSONOutput(hookSpecificOutput=decision)

        return SyncHookJSONOutput()

    return handler


def create_agent_arg_strip_hook_handler(
    stderr_callback: StderrCallback = None,
) -> Callable[[HookInput, Optional[str], HookContext], Awaitable[SyncHookJSONOutput]]:
    """
    Create a PreToolUse handler that strips coerced ``model``/``isolation`` args
    from Agent/Task tool calls on translated (ChatGPT/Codex-served) mains.

    Ruling S8-R1: a translated main's Responses backend coerces every tool schema
    to strict:true + all-props-required, so the model is grammar-forced to fill
    ``model`` and ``isolation`` on every Agent call. Both are enums with no neutral
    value, so the filled value carries no intent — and on a translated main every
    ``model`` alias is an unservable claude id. Stripping both is lossless and
    restores the stamped-frontmatter subagent pins.
    """

    async def handler(
        hook_input: HookInput,
        tool_use_id: Optional[str],
        context: HookContext,
    ) -> SyncHookJSONOutput:
        tool_input = (hook_input or {}).get("tool_input")
        if not isinstance(tool_input, dict):
            return SyncHookJSONOutput()

        if (hook_input or {}).get("tool_name") not in ("Agent", "Task"):
            return SyncHookJSONOutput()

        if "model" not in tool_input and "isolation" not in tool_input:
            return SyncHookJSONOutput()

        stripped = [k for k in ("model", "isolation") if k in tool_input]
        logger.debug(f"[HookService] S8-R1: stripping Agent args {stripped} on translated main")

        updated_input = {k: v for k, v in tool_input.items() if k not in ("model", "isolation")}
        return SyncHookJSONOutput(
            hookSpecificOutput={
                "hookEventName": "PreToolUse",
                "updatedInput": updated_input,
            }
        )

    return handler


def _project_has_db_pretooluse_hooks(project_id: Optional[str]) -> bool:
    """
    Return True if the project (or app-level) has any enabled DB PreToolUse hooks.

    Used at build time to avoid registering (and querying the DB per tool call)
    when no DB PreToolUse hooks exist. Best-effort: any DB error is treated as
    "none" so hook-config construction never fails.
    """
    if not project_id:
        return False
    try:
        hooks = database.get_hooks_for_project_and_event(project_id, "PreToolUse")
        return bool(hooks)
    except Exception as e:
        logger.debug(f"Could not check for DB PreToolUse hooks: {e}")
        return False


def build_hooks_config(
    working_dir: str,
    project_id: Optional[str] = None,
    plugin_paths: Optional[List[str]] = None,
    stderr_callback: StderrCallback = None,
    *,
    translated_main: bool = False,
) -> Optional[Dict[str, List[HookMatcher]]]:
    """
    Build SDK hooks configuration from all sources.

    This creates the hooks dict to pass to AgentOptions, combining:
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

    # Register a DB-backed PreToolUse handler when the project has enabled
    # PreToolUse hooks (this also covers app-level hooks via project_id IS NULL).
    # Registered as an additional matcher so it coexists with any plugin
    # PreToolUse handler above. Only bash hooks can block; see the handler.
    if _project_has_db_pretooluse_hooks(project_id):
        db_pretooluse_handler = create_db_pretooluse_hook_handler(
            working_dir, project_id, stderr_callback
        )
        hooks_config.setdefault("PreToolUse", []).append(
            HookMatcher(hooks=[db_pretooluse_handler])
        )
        logger.debug(f"[HookService] Registered DB PreToolUse handler for project {project_id}")

    # On a translated (ChatGPT/Codex-served) main the backend coerces the Agent
    # tool schema to all-props-required, force-filling `model`/`isolation`. Strip
    # them so stamped-frontmatter subagent pins survive (ruling S8-R1).
    if translated_main:
        hooks_config.setdefault("PreToolUse", []).append(
            HookMatcher(matcher="Agent|Task", hooks=[create_agent_arg_strip_hook_handler(stderr_callback)])
        )
        logger.debug("[HookService] Registered S8-R1 Agent-arg-strip PreToolUse handler (translated main)")

    # ALWAYS register the Stop hook handler for database hooks support
    stop_plugin_hooks = plugin_hooks.get("Stop", [])
    stop_handler = create_stop_hook_handler(working_dir, project_id, stop_plugin_hooks, stderr_callback)
    hooks_config["Stop"] = [HookMatcher(hooks=[stop_handler])]
    logger.debug(f"[HookService] Registered Stop handler with {len(stop_plugin_hooks)} plugin hook(s)")

    return hooks_config


def attach_hooks_to_options(
    options: Any,
    project_id: Optional[str] = None,
    stderr_callback: StderrCallback = None,
) -> None:
    """Build hooks from all sources and set them on ``options`` in place.

    The single seam every SDK-session spawn path goes through — WebSocket chat,
    one-shot ``execute_query``, SSE ``stream_query``, and the background REST
    path — so DB hooks (including blocking PreToolUse security guards) fire
    regardless of transport (issue #567). No-op when the options carry no
    working directory.
    """
    working_dir = getattr(options, "cwd", None)
    if not working_dir:
        return

    plugin_paths = []
    for plugin_entry in (options.plugins or []):
        if isinstance(plugin_entry, dict) and plugin_entry.get("path"):
            plugin_paths.append(plugin_entry["path"])

    # The S8-R1 Agent-arg-strip hook is registered only on translated mains,
    # detected via the forced env marker set by the provider overlay
    # (apply_provider_env). Presence — not truthiness — is the signal.
    translated_main = "AIH_GATEWAY_TRANSLATED_MAIN" in (getattr(options, "env", None) or {})

    hooks_config = build_hooks_config(
        working_dir=working_dir,
        project_id=project_id,
        plugin_paths=plugin_paths,
        stderr_callback=stderr_callback,
        translated_main=translated_main,
    )
    if hooks_config:
        options.hooks = hooks_config
        logger.debug(f"[HookService] Hooks configured: {list(hooks_config.keys())}")
