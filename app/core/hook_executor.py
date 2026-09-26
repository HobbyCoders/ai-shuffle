"""
Hook execution service for running bash commands and webhook calls
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import shutil
import signal
import sys
import time
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Optional

import httpx

from app.core.scrub import scrub_url_credentials
from app.db import database
from app.db.async_database import run_sync

logger = logging.getLogger(__name__)

# Track active hook tasks to prevent silent loss on shutdown
_active_hook_tasks: set = set()

_IS_WINDOWS = sys.platform == "win32"

# bash.exe launchers that are NOT Git Bash: System32\bash.exe and the
# WindowsApps stub both launch WSL, which may not be installed and does not
# share filesystem/PATH semantics with the hook's working directory.
_NON_GIT_BASH_MARKERS = ("system32", "windowsapps")


@lru_cache(maxsize=1)
def _resolve_windows_bash() -> Optional[str]:
    """
    Locate a Git Bash executable for running hook commands on Windows.

    Hook commands are written in bash syntax; running them through cmd.exe
    mangles quoting so a blocking guard's JSON output never parses and the
    guard silently fails open (issue #572). Discovery order: explicit env
    override, bash on PATH (skipping WSL launchers), the bash bundled next
    to git.exe, then common Git for Windows install locations.
    """
    for env_var in ("AI_SHUFFLE_HOOK_BASH", "CLAUDE_CODE_GIT_BASH_PATH"):
        override = (os.environ.get(env_var) or "").strip()
        if override and Path(override).is_file():
            return override

    found = shutil.which("bash")
    if found and not any(marker in found.lower() for marker in _NON_GIT_BASH_MARKERS):
        return found

    # Derive from git.exe: <install>\cmd\git.exe → <install>\bin\bash.exe
    git_exe = shutil.which("git")
    if git_exe:
        candidate = Path(git_exe).parent.parent / "bin" / "bash.exe"
        if candidate.is_file():
            return str(candidate)

    # Build with Path components, not a backslashed string — backslashes are
    # not separators on POSIX, where these tests also run.
    for base, rel in (
        (os.environ.get("ProgramFiles"), ("Git", "bin", "bash.exe")),
        (os.environ.get("ProgramFiles(x86)"), ("Git", "bin", "bash.exe")),
        (os.environ.get("LOCALAPPDATA"), ("Programs", "Git", "bin", "bash.exe")),
    ):
        if base:
            candidate = Path(base).joinpath(*rel)
            if candidate.is_file():
                return str(candidate)

    return None


# Linux caps one environment string at MAX_ARG_STRLEN (128 KiB); a larger
# value (or one with a NUL byte) makes execve fail, and the hook never runs.
# Tool input and output reach HOOK_* values, so the model could otherwise
# silence a hook by padding a command or emitting a NUL. The full payload
# still goes on stdin wherever the hook contract uses it (PreToolUse);
# HOOK_DATA stays valid JSON when capped (hook_data_env_value).
HOOK_ENV_VALUE_MAX_BYTES = 64 * 1024
HOOK_ENV_TRUNCATED_MARKER = "...[truncated]"


def hook_env_value(value: Any) -> str:
    """``value`` as a HOOK_* env value that execve always accepts.

    NUL bytes are stripped and the UTF-8 form is capped at
    ``HOOK_ENV_VALUE_MAX_BYTES``, with ``HOOK_ENV_TRUNCATED_MARKER`` appended
    when it was cut. Unencodable characters (lone surrogates) become ``?``.
    """
    text = "" if value is None else str(value)
    data = text.replace("\x00", "").encode("utf-8", errors="replace")
    if len(data) <= HOOK_ENV_VALUE_MAX_BYTES:
        return data.decode("utf-8")
    cut = data[:HOOK_ENV_VALUE_MAX_BYTES].decode("utf-8", errors="ignore")
    return cut + HOOK_ENV_TRUNCATED_MARKER


def _shorten_strings(value: Any, limit: int) -> Any:
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + HOOK_ENV_TRUNCATED_MARKER
    if isinstance(value, dict):
        return {k: _shorten_strings(v, limit) for k, v in value.items()}
    if isinstance(value, list):
        return [_shorten_strings(v, limit) for v in value]
    return value


def _longest_string(value: Any) -> int:
    if isinstance(value, str):
        return len(value)
    if isinstance(value, dict):
        return max((_longest_string(v) for v in value.values()), default=0)
    if isinstance(value, list):
        return max((_longest_string(v) for v in value), default=0)
    return 0


def hook_data_env_value(data: Any) -> str:
    """``data`` as the ``HOOK_DATA`` env value: JSON that always parses.

    Within ``HOOK_ENV_VALUE_MAX_BYTES`` it is the plain serialization. Over the
    cap, the payload's longest strings (a padded ``tool_input.command``, a
    large Write body) are shortened just enough to fit, each ending in
    ``HOOK_ENV_TRUNCATED_MARKER``, and ``"truncated": true`` is added at the
    top level. Cutting the serialized text instead left invalid JSON, so a
    ``jq`` guard failed open on a padded call. The output is ASCII (json
    escapes everything else, NUL included), so characters equal bytes.
    """
    text = json.dumps(data, default=str)
    if len(text) <= HOOK_ENV_VALUE_MAX_BYTES:
        return text
    base = json.loads(text)

    def render(limit: int) -> str:
        short = _shorten_strings(base, limit)
        flagged = {**short, "truncated": True} if isinstance(short, dict) else {
            "value": short, "truncated": True,
        }
        return json.dumps(flagged)

    # Largest per-string limit that fits (binary search on the real size).
    lo, hi, best = 0, _longest_string(base), None
    while lo <= hi:
        mid = (lo + hi) // 2
        candidate = render(mid)
        if len(candidate) <= HOOK_ENV_VALUE_MAX_BYTES:
            best, lo = candidate, mid + 1
        else:
            hi = mid - 1
    if best is not None:
        return best
    # Too many keys or items to fit even with empty strings.
    minimal = {"truncated": True}
    if isinstance(base, dict):
        for key in ("hook_event_name", "tool_name"):
            if isinstance(base.get(key), str):
                minimal[key] = base[key][:256]
    return json.dumps(minimal)


def hook_base_env() -> Dict[str, str]:
    """The environment a hook subprocess inherits: the app's own, without
    host secrets (DATABASE_URL, OWNER_PASSWORD, SECRET_KEY, *_TOKEN, ...),
    scrubbed exactly as the delegated CLI child's environment is."""
    from fluid.tools.child_env import scrub_child_env

    return scrub_child_env(os.environ)


async def spawn_hook_shell(
    command: str,
    *,
    stdin: Optional[int] = None,
    cwd: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
) -> asyncio.subprocess.Process:
    """
    Spawn a bash-type hook command and return the process (stdout/stderr piped).

    On POSIX, create_subprocess_shell already runs a bourne shell. On Windows
    it runs cmd.exe, which breaks bash quoting — so prefer Git Bash there and
    fall back to cmd.exe (loudly) only when no bash exists (issue #572).
    """
    if _IS_WINDOWS:
        bash = _resolve_windows_bash()
        if bash:
            return await asyncio.create_subprocess_exec(
                bash,
                "-c",
                command,
                stdin=stdin,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env,
            )
        logger.warning(
            "No bash executable found for hook command; falling back to cmd.exe — "
            "bash-syntax hooks (including blocking guards) may silently misbehave. "
            "Install Git for Windows or set AI_SHUFFLE_HOOK_BASH."
        )
    return await asyncio.create_subprocess_shell(
        command,
        stdin=stdin,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=env,
        # Own process group, so a timeout can kill whatever the hook spawned
        # (see kill_hook_process). Not on the cmd.exe fallback.
        **({} if _IS_WINDOWS else {"start_new_session": True}),
    )


async def kill_hook_process(
    process: asyncio.subprocess.Process, *, drain_timeout: float = 2.0
) -> None:
    """
    Kill a timed-out hook and everything it spawned, then reap it.

    ``/bin/sh -c`` forks children instead of exec'ing them, so killing only the
    shell leaves e.g. a ``sleep`` holding stdout/stderr open, and reaping the
    shell would then block for that child's whole lifetime. On POSIX the hook
    runs in its own session (``spawn_hook_shell``), so kill the whole group.
    The drain is still bounded: a descendant that left the group (``setsid``)
    can keep the pipes open and must not stall the tool call.
    """
    if not _IS_WINDOWS:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    try:
        process.kill()
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(process.communicate(), timeout=drain_timeout)
    except asyncio.TimeoutError:
        logger.warning(
            "Hook process %s killed but its output pipes stayed open; not waiting further",
            process.pid,
        )


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
        env = {}

        # Add specific fields from event data
        if "data" in event_data:
            data = event_data["data"]
            for key, value in data.items():
                env_key = f"HOOK_{key.upper()}"
                env[env_key] = hook_env_value(value)

        # Set last so a data key such as "event" cannot overwrite them.
        env.update({
            "HOOK_EVENT": hook_env_value(event_data.get("event", "")),
            "HOOK_TIMESTAMP": hook_env_value(event_data.get("timestamp", "")),
            "HOOK_DATA": hook_data_env_value(event_data.get("data", {})),
        })

        # Execute the command
        process = await spawn_hook_shell(
            command,
            env={**hook_base_env(), **env},
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=timeout_seconds
            )
        except asyncio.TimeoutError:
            await kill_hook_process(process)
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


#: How much of a webhook's 2xx response body is kept for decision parsing.
WEBHOOK_BODY_MAX_CHARS = 64 * 1024


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
        # Prepare payload. Tool input/output rides in it, so URL-embedded
        # credentials (a pasted DATABASE_URL) are redacted before it leaves.
        payload = json.dumps(scrub_url_credentials(event_data))

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
        from app.core.http_client import get_http_client
        client = get_http_client()
        response = await client.post(url, content=payload, headers=headers, timeout=timeout_seconds)

        duration_ms = int((time.time() - start_time) * 1000)

        if 200 <= response.status_code < 300:
            return {
                "success": True,
                "duration_ms": duration_ms,
                "output": f"HTTP {response.status_code}: {response.text[:1000]}",
                "error": None,
                # The raw response body, for callers that read a decision
                # from it (a Stop webhook's {"decision": "block"}).
                "body": response.text[:WEBHOOK_BODY_MAX_CHARS],
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
    event_data: Dict[str, Any],
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
        await run_sync(database.update_hook_triggered, hook_id, result["success"])

    return result


_warned_tool_matchers: set = set()


def tool_matcher_matches(pattern: str, tool_name: str, *, plugin: bool = False) -> bool:
    """Whether ``tool_name`` fully matches a hook's ``tool_matcher`` regex.

    ``plugin`` keeps the rule for a plugin ``hooks.json`` matcher instead: a
    case-insensitive match at the start of the name.

    An invalid pattern is compared as plain text and logged once per pattern,
    so one bad hook cannot stop the event's other hooks from running.
    """
    try:
        if plugin:
            return re.match(pattern, tool_name, re.IGNORECASE) is not None
        return re.fullmatch(pattern, tool_name) is not None
    except re.error as e:
        if pattern not in _warned_tool_matchers:
            _warned_tool_matchers.add(pattern)
            logger.warning(
                "Hook tool_matcher %r is not a valid regex (%s); comparing it as plain text",
                pattern, e,
            )
        if plugin:
            return pattern.casefold() == tool_name.casefold()
        return pattern == tool_name


async def execute_hooks_for_event(
    project_id: Optional[str],
    event: str,
    event_data: Dict[str, Any],
    tool_name: Optional[str] = None,
) -> None:
    """
    Execute all enabled hooks for a project (+ app hooks) that match the given event.
    This is a fire-and-forget operation - errors are logged but not raised.

    Args:
        project_id: Project ID (None for app-only hooks)
        event: Event type (e.g., "PreToolUse", "SessionStart")
        event_data: Event data to pass to hooks
        tool_name: Tool name for PreToolUse/PostToolUse events (for filtering)
    """
    try:
        # Exactly one of these reads runs per call, so this costs a single
        # thread hop; everything after it (task creation) needs the loop.
        if project_id:
            # Returns both project-scoped and app-level hooks
            hooks_list = await run_sync(
                database.get_hooks_for_project_and_event, project_id, event
            )
        else:
            # App-level hooks only
            hooks_list = await run_sync(database.get_app_hooks_for_event, event)

        for hook in hooks_list:
            # Check tool matcher for PreToolUse/PostToolUse events
            tool_matcher = hook.get("tool_matcher")
            if tool_matcher and tool_name and not tool_matcher_matches(tool_matcher, tool_name):
                continue

            # Prepare event data with timestamp
            full_event_data = {
                "event": event,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "data": event_data,
            }

            # Execute hook asynchronously with result logging
            async def _execute_and_log(h: Dict[str, Any], data: Dict[str, Any]) -> None:
                result = await execute_hook(h, data)
                if not result.get("success"):
                    logger.warning(
                        f"Hook {h.get('id')} ({h.get('name', 'unnamed')}) failed for event {event}: "
                        f"{result.get('error', 'unknown error')}"
                    )

            def _on_hook_done(t):
                _active_hook_tasks.discard(t)
                if not t.cancelled() and t.exception():
                    logger.error(f"Hook task crashed: {t.exception()}")

            task = asyncio.create_task(_execute_and_log(hook, full_event_data))
            _active_hook_tasks.add(task)
            task.add_done_callback(_on_hook_done)

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
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "data": {
            "test": True,
            "hook_id": hook.get("id"),
            "hook_name": hook.get("name"),
            "project_id": hook.get("project_id"),
            "tool_name": "ExampleTool",
            "message": "This is a test event",
        }
    }

    return await execute_hook(hook, test_event_data)
