"""
Hook execution service for running bash commands, webhook calls, and message injection
"""

import asyncio
import hashlib
import hmac
import json
import logging
import re
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


async def execute_message_hook(
    hook: Dict[str, Any],
    event_data: Dict[str, Any],
    session_id: Optional[str] = None
) -> Dict[str, Any]:
    """
    Execute a message hook by injecting a user message into the active chat session.

    The message_template is rendered with event data variables and sent as a new
    user message in the specified session.

    Args:
        hook: Hook configuration from database (must include message_template)
        event_data: Event data for template variable substitution
        session_id: Active session to inject the message into

    Returns:
        Dict with success, duration_ms, output, and error fields
    """
    template = hook.get("message_template")
    if not template:
        return {
            "success": False,
            "duration_ms": 0,
            "output": None,
            "error": "No message template specified"
        }

    if not session_id:
        return {
            "success": False,
            "duration_ms": 0,
            "output": None,
            "error": "No active session to send message to"
        }

    start_time = time.time()

    try:
        # Build substitution variables from event_data
        data = event_data.get("data", {})
        variables = {
            "event": event_data.get("event", ""),
            "timestamp": event_data.get("timestamp", ""),
            "session_id": session_id,
            "tool_name": data.get("tool_name", ""),
            "hook_name": hook.get("name", ""),
            **{k: str(v) for k, v in data.items() if v is not None}
        }

        # Render template — replace {variable} placeholders
        rendered = template
        for key, value in variables.items():
            rendered = rendered.replace(f"{{{key}}}", str(value))

        # Inject as a user message into the session
        message = database.add_session_message(
            session_id=session_id,
            role="user",
            content=rendered,
            metadata={"source": "hook", "hook_id": hook.get("id"), "hook_name": hook.get("name")}
        )

        # Broadcast to connected clients
        try:
            from app.core.sync_engine import sync_engine
            await sync_engine.broadcast_message_added(
                session_id=session_id,
                message={
                    "role": "user",
                    "content": rendered,
                    "metadata": {"source": "hook", "hook_id": hook.get("id")}
                },
                source_device_id=None  # Broadcast to all devices
            )
        except Exception as broadcast_err:
            logger.warning(f"Failed to broadcast hook message: {broadcast_err}")

        duration_ms = int((time.time() - start_time) * 1000)
        return {
            "success": True,
            "duration_ms": duration_ms,
            "output": f"Message sent: {rendered[:200]}",
            "error": None
        }

    except Exception as e:
        duration_ms = int((time.time() - start_time) * 1000)
        logger.exception(f"Error executing message hook {hook.get('id')}: {e}")
        return {
            "success": False,
            "duration_ms": duration_ms,
            "output": None,
            "error": str(e)
        }


async def execute_hook(
    hook: Dict[str, Any],
    event_data: Dict[str, Any],
    session_id: Optional[str] = None
) -> Dict[str, Any]:
    """
    Execute a hook based on its type.

    Args:
        hook: Hook configuration from database
        event_data: Event data to pass to the hook
        session_id: Active session ID (required for message hooks)

    Returns:
        Dict with success, duration_ms, output, and error fields
    """
    hook_type = hook.get("hook_type")

    if hook_type == "bash":
        result = await execute_bash_hook(hook, event_data)
    elif hook_type == "webhook":
        result = await execute_webhook_hook(hook, event_data)
    elif hook_type == "message":
        result = await execute_message_hook(hook, event_data, session_id=session_id)
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
    project_id: Optional[str],
    event: str,
    event_data: Dict[str, Any],
    tool_name: Optional[str] = None,
    session_id: Optional[str] = None
) -> None:
    """
    Execute all enabled hooks for a project (+ app hooks) that match the given event.
    This is a fire-and-forget operation - errors are logged but not raised.

    Args:
        project_id: Project ID (None for app-only hooks)
        event: Event type (e.g., "PreToolUse", "SessionStart")
        event_data: Event data to pass to hooks
        tool_name: Tool name for PreToolUse/PostToolUse events (for filtering)
        session_id: Active session ID (required for message hooks)
    """
    try:
        if project_id:
            # Returns both project-scoped and app-level hooks
            hooks_list = database.get_hooks_for_project_and_event(project_id, event)
        else:
            # App-level hooks only
            hooks_list = database.get_app_hooks_for_event(event)

        for hook in hooks_list:
            # Check tool matcher for PreToolUse/PostToolUse events
            tool_matcher = hook.get("tool_matcher")
            if tool_matcher and tool_name:
                # tool_matcher is a regex pattern like "Bash|Edit"
                if not re.match(tool_matcher, tool_name):
                    continue

            # Prepare event data with timestamp
            full_event_data = {
                "event": event,
                "timestamp": datetime.utcnow().isoformat(),
                "data": event_data,
            }

            # Execute hook asynchronously with result logging
            async def _execute_and_log(h: Dict[str, Any], data: Dict[str, Any], sid: Optional[str]) -> None:
                result = await execute_hook(h, data, session_id=sid)
                if not result.get("success"):
                    logger.warning(
                        f"Hook {h.get('id')} ({h.get('name', 'unnamed')}) failed for event {event}: "
                        f"{result.get('error', 'unknown error')}"
                    )

            task = asyncio.create_task(_execute_and_log(hook, full_event_data, session_id))
            task.add_done_callback(
                lambda t: logger.error(f"Hook task crashed: {t.exception()}")
                if not t.cancelled() and t.exception() else None
            )

    except Exception as e:
        logger.exception(f"Error executing hooks for event {event} in project {project_id}: {e}")


async def test_hook(hook: Dict[str, Any]) -> Dict[str, Any]:
    """
    Test a hook with sample event data.

    For message hooks, renders the template and returns the preview without
    actually injecting a message into any session.

    Args:
        hook: Hook configuration from database

    Returns:
        Dict with success, duration_ms, output, and error fields
    """
    hook_type = hook.get("hook_type")

    # Create test event data
    test_event_data = {
        "event": hook.get("event", "Test"),
        "timestamp": datetime.utcnow().isoformat(),
        "data": {
            "test": True,
            "hook_id": hook.get("id"),
            "hook_name": hook.get("name"),
            "project_id": hook.get("project_id"),
            "tool_name": "ExampleTool",
            "message": "This is a test event",
        }
    }

    # For message hooks, preview the rendered template without sending
    if hook_type == "message":
        template = hook.get("message_template", "")
        if not template:
            return {
                "success": False,
                "duration_ms": 0,
                "output": None,
                "error": "No message template specified"
            }

        variables = {
            "event": test_event_data["event"],
            "timestamp": test_event_data["timestamp"],
            "session_id": "test-session-id",
            "tool_name": "ExampleTool",
            "hook_name": hook.get("name", ""),
        }
        rendered = template
        for key, value in variables.items():
            rendered = rendered.replace(f"{{{key}}}", str(value))

        return {
            "success": True,
            "duration_ms": 0,
            "output": f"[Preview] Message that would be sent:\n{rendered}",
            "error": None
        }

    return await execute_hook(hook, test_event_data)
