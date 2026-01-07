"""
Hook execution service for running bash commands and webhook calls
"""

import asyncio
import hashlib
import hmac
import json
import logging
import time
from datetime import datetime
from typing import Any, Dict, Optional

import httpx

from app.db import database

logger = logging.getLogger(__name__)


async def execute_bash_hook(
    hook: Dict[str, Any],
    event_data: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Execute a bash hook command.

    Args:
        hook: Hook configuration from database
        event_data: Event data to pass as environment variables

    Returns:
        Dict with success, duration_ms, output, and error fields
    """
    command = hook.get("command")
    timeout_ms = hook.get("timeout", 30000)
    timeout_seconds = timeout_ms / 1000

    if not command:
        return {
            "success": False,
            "duration_ms": 0,
            "output": None,
            "error": "No command specified"
        }

    start_time = time.time()

    try:
        # Prepare environment with event data
        env = {
            "HOOK_EVENT": event_data.get("event", ""),
            "HOOK_TIMESTAMP": event_data.get("timestamp", ""),
            "HOOK_DATA": json.dumps(event_data.get("data", {})),
        }

        # Add specific fields from event data
        if "data" in event_data:
            data = event_data["data"]
            for key, value in data.items():
                env_key = f"HOOK_{key.upper()}"
                env[env_key] = str(value) if value is not None else ""

        # Execute the command
        process = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**dict(__import__("os").environ), **env}  # Merge with current env
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=timeout_seconds
            )
        except asyncio.TimeoutError:
            process.kill()
            await process.communicate()
            duration_ms = int((time.time() - start_time) * 1000)
            return {
                "success": False,
                "duration_ms": duration_ms,
                "output": None,
                "error": f"Command timed out after {timeout_ms}ms"
            }

        duration_ms = int((time.time() - start_time) * 1000)

        if process.returncode == 0:
            return {
                "success": True,
                "duration_ms": duration_ms,
                "output": stdout.decode("utf-8", errors="replace")[:5000],  # Limit output
                "error": None
            }
        else:
            return {
                "success": False,
                "duration_ms": duration_ms,
                "output": stdout.decode("utf-8", errors="replace")[:5000],
                "error": stderr.decode("utf-8", errors="replace")[:2000]
            }

    except Exception as e:
        duration_ms = int((time.time() - start_time) * 1000)
        logger.exception(f"Error executing bash hook {hook.get('id')}: {e}")
        return {
            "success": False,
            "duration_ms": duration_ms,
            "output": None,
            "error": str(e)
        }


async def execute_webhook_hook(
    hook: Dict[str, Any],
    event_data: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Execute a webhook hook by POSTing to the configured URL.

    Args:
        hook: Hook configuration from database
        event_data: Event data to POST

    Returns:
        Dict with success, duration_ms, output, and error fields
    """
    url = hook.get("url")
    secret = hook.get("secret")
    timeout_ms = hook.get("timeout", 30000)
    timeout_seconds = timeout_ms / 1000

    if not url:
        return {
            "success": False,
            "duration_ms": 0,
            "output": None,
            "error": "No URL specified"
        }

    start_time = time.time()

    try:
        # Prepare payload
        payload = json.dumps(event_data)

        # Prepare headers
        headers = {
            "Content-Type": "application/json",
            "X-Hook-Event": event_data.get("event", ""),
            "X-Hook-Timestamp": event_data.get("timestamp", ""),
        }

        # Add HMAC signature if secret is configured
        if secret:
            signature = hmac.new(
                secret.encode("utf-8"),
                payload.encode("utf-8"),
                hashlib.sha256
            ).hexdigest()
            headers["X-Hook-Signature"] = f"sha256={signature}"

        # Make the request
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            response = await client.post(url, content=payload, headers=headers)

        duration_ms = int((time.time() - start_time) * 1000)

        if 200 <= response.status_code < 300:
            return {
                "success": True,
                "duration_ms": duration_ms,
                "output": f"HTTP {response.status_code}: {response.text[:1000]}",
                "error": None
            }
        else:
            return {
                "success": False,
                "duration_ms": duration_ms,
                "output": f"HTTP {response.status_code}",
                "error": response.text[:2000]
            }

    except httpx.TimeoutException:
        duration_ms = int((time.time() - start_time) * 1000)
        return {
            "success": False,
            "duration_ms": duration_ms,
            "output": None,
            "error": f"Request timed out after {timeout_ms}ms"
        }
    except Exception as e:
        duration_ms = int((time.time() - start_time) * 1000)
        logger.exception(f"Error executing webhook hook {hook.get('id')}: {e}")
        return {
            "success": False,
            "duration_ms": duration_ms,
            "output": None,
            "error": str(e)
        }


async def execute_hook(
    hook: Dict[str, Any],
    event_data: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Execute a hook based on its type.

    Args:
        hook: Hook configuration from database
        event_data: Event data to pass to the hook

    Returns:
        Dict with success, duration_ms, output, and error fields
    """
    hook_type = hook.get("hook_type")

    if hook_type == "bash":
        result = await execute_bash_hook(hook, event_data)
    elif hook_type == "webhook":
        result = await execute_webhook_hook(hook, event_data)
    else:
        result = {
            "success": False,
            "duration_ms": 0,
            "output": None,
            "error": f"Unknown hook type: {hook_type}"
        }

    # Update hook triggered status
    hook_id = hook.get("id")
    if hook_id:
        database.update_hook_triggered(hook_id, result["success"])

    return result


async def execute_hooks_for_event(
    project_id: str,
    event: str,
    event_data: Dict[str, Any],
    tool_name: Optional[str] = None
) -> None:
    """
    Execute all enabled hooks for a project that match the given event.
    This is a fire-and-forget operation - errors are logged but not raised.

    Args:
        project_id: Project ID
        event: Event type (e.g., "PreToolUse", "SessionStart")
        event_data: Event data to pass to hooks
        tool_name: Tool name for PreToolUse/PostToolUse events (for filtering)
    """
    try:
        hooks = database.get_hooks_for_project_and_event(project_id, event)

        for hook in hooks:
            # Check tool matcher for PreToolUse/PostToolUse events
            tool_matcher = hook.get("tool_matcher")
            if tool_matcher and tool_name:
                # tool_matcher is a regex pattern like "Bash|Edit"
                import re
                if not re.match(tool_matcher, tool_name):
                    continue

            # Prepare event data with timestamp
            full_event_data = {
                "event": event,
                "timestamp": datetime.utcnow().isoformat(),
                "data": event_data,
            }

            # Execute hook asynchronously (fire and forget)
            asyncio.create_task(execute_hook(hook, full_event_data))

    except Exception as e:
        logger.exception(f"Error executing hooks for event {event} in project {project_id}: {e}")


async def test_hook(hook: Dict[str, Any]) -> Dict[str, Any]:
    """
    Test a hook with sample event data.

    Args:
        hook: Hook configuration from database

    Returns:
        Dict with success, duration_ms, output, and error fields
    """
    # Create test event data
    test_event_data = {
        "event": hook.get("event", "Test"),
        "timestamp": datetime.utcnow().isoformat(),
        "data": {
            "test": True,
            "hook_id": hook.get("id"),
            "hook_name": hook.get("name"),
            "project_id": hook.get("project_id"),
            "message": "This is a test event",
        }
    }

    return await execute_hook(hook, test_event_data)
