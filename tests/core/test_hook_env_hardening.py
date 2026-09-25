"""Hooks must not fail open, and must not inherit host secrets (S-3, S-4, S-5).

* A PreToolUse guard used to get the whole tool input as one ``HOOK_DATA`` env
  string. Above 128 KiB (Linux MAX_ARG_STRLEN) execve failed with E2BIG, the
  hook reported "error", and the call was allowed: padding a Bash command
  switched a guard off.
* ``execute_bash_hook`` put every event field in an env value, so a large or
  NUL-bearing tool output stopped PostToolUse hooks from running at all.
* Hook shells inherited the app's unscrubbed environment.
"""

from __future__ import annotations

import json
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core import hook_executor, hook_service

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell hooks")

SECRETS = {
    "DATABASE_URL": "postgres://app:pw@db/app",
    "OWNER_PASSWORD": "owner-pw",
    "SECRET_KEY": "sk",
}


def _guard_handler(monkeypatch, tmp_path, command):
    hook = {"id": "guard", "hook_type": "bash", "enabled": True, "command": command, "timeout": 10000}
    monkeypatch.setattr(hook_service, "get_database_hooks_for_event", AsyncMock(return_value=[hook]))
    monkeypatch.setattr(hook_service, "database", MagicMock())
    return hook_service.create_db_pretooluse_hook_handler(str(tmp_path))


def _decision(output):
    return (output.get("hookSpecificOutput") or {}).get("permissionDecision")


@pytest.mark.asyncio
async def test_oversized_bash_command_still_reaches_the_guard_and_is_blocked(monkeypatch, tmp_path):
    handler = _guard_handler(
        monkeypatch, tmp_path,
        "if grep -q 'rm -rf'; then echo blocked-by-guard >&2; exit 2; fi; exit 0",
    )
    padded = {"command": "rm -rf / #" + "a" * 200_000}
    output = await handler(
        {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": padded, "session_id": "s"},
        None, {},
    )
    assert _decision(output) == "deny"
    assert output["hookSpecificOutput"]["permissionDecisionReason"] == "blocked-by-guard"


@pytest.mark.asyncio
async def test_a_guard_that_cannot_spawn_blocks_the_call(monkeypatch, tmp_path):
    handler = _guard_handler(monkeypatch, tmp_path, "exit 0")
    monkeypatch.setattr(
        hook_service, "spawn_hook_shell", AsyncMock(side_effect=OSError(7, "Argument list too long"))
    )
    output = await handler(
        {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "ls"}},
        None, {},
    )
    assert _decision(output) == "deny"
    assert "could not run" in output["hookSpecificOutput"]["permissionDecisionReason"]


@pytest.mark.asyncio
async def test_nul_and_oversized_tool_output_still_fire_the_background_hook(tmp_path):
    out = tmp_path / "out"
    hook = {"id": "h", "command": f'printf %s "$HOOK_TOOL_RESPONSE" > {out}', "timeout": 10000}
    result = await hook_executor.execute_bash_hook(
        hook, {"event": "PostToolUse", "timestamp": "t", "data": {"tool_response": "a\x00b"}},
    )
    assert result["success"], result["error"]
    assert out.read_text() == "ab"

    result = await hook_executor.execute_bash_hook(
        hook, {"event": "PostToolUse", "timestamp": "t", "data": {"tool_response": "x" * 200_000}},
    )
    assert result["success"], result["error"]
    text = out.read_text()
    assert text.endswith(hook_executor.HOOK_ENV_TRUNCATED_MARKER)
    assert len(text.encode()) <= hook_executor.HOOK_ENV_VALUE_MAX_BYTES + len(
        hook_executor.HOOK_ENV_TRUNCATED_MARKER
    )


def test_hook_env_value_caps_by_utf8_bytes_and_drops_unencodable():
    value = hook_executor.hook_env_value("€" * 100_000 + "\ud800")
    assert len(value.encode("utf-8")) <= hook_executor.HOOK_ENV_VALUE_MAX_BYTES + len(
        hook_executor.HOOK_ENV_TRUNCATED_MARKER
    )
    assert hook_executor.hook_env_value(None) == ""
    assert hook_executor.hook_env_value("plain") == "plain"


@pytest.mark.asyncio
async def test_bash_and_guard_hooks_do_not_inherit_host_secrets(monkeypatch, tmp_path):
    for name, value in SECRETS.items():
        monkeypatch.setenv(name, value)
    dump = tmp_path / "env"

    result = await hook_executor.execute_bash_hook(
        {"id": "h", "command": f"env > {dump}", "timeout": 10000},
        {"event": "PostToolUse", "timestamp": "t", "data": {"tool_name": "Read"}},
    )
    assert result["success"], result["error"]
    names = {line.split("=", 1)[0] for line in dump.read_text().splitlines()}
    assert "HOOK_TOOL_NAME" in names and "PATH" in names
    assert not names & set(SECRETS)

    status, *_ = await hook_service._execute_db_bash_hook(
        {"id": "g", "command": f"env > {dump}", "timeout": 10000},
        {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {}},
        str(tmp_path),
    )
    assert status == "ok"
    names = {line.split("=", 1)[0] for line in dump.read_text().splitlines()}
    assert "HOOK_TOOL_NAME" in names
    assert not names & set(SECRETS)

    await hook_service.execute_plugin_hook(
        {"command": f"env > {dump}", "timeout": 10, "plugin_path": str(tmp_path)},
        {"hook_event_name": "PostToolUse"}, str(tmp_path),
    )
    names = {line.split("=", 1)[0] for line in dump.read_text().splitlines()}
    assert "CLAUDE_PLUGIN_ROOT" in names
    assert not names & set(SECRETS)


@pytest.mark.asyncio
async def test_webhook_payload_redacts_url_credentials(monkeypatch):
    client = MagicMock()
    client.post = AsyncMock(return_value=MagicMock(status_code=200, text="ok"))
    monkeypatch.setattr("app.core.http_client.get_http_client", lambda: client)
    await hook_executor.execute_webhook_hook(
        {"id": "w", "url": "https://hooks.example/x", "timeout": 5000},
        {"event": "PostToolUse", "timestamp": "t",
         "data": {"tool_response": "DATABASE_URL=postgres://app:s3cret@db:5432/app"}},
    )
    body = json.loads(client.post.call_args.kwargs["content"])
    assert "s3cret" not in body["data"]["tool_response"]
    assert "postgres://app:***@db:5432/app" in body["data"]["tool_response"]
