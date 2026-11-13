"""The delegated Claude Code CLI's context limits.

The Bash and MCP output caps and the auto-memory switch go into the env of
the CLI process only, never into SessionOptions.env (which also feeds tools
and MCP servers), and a value the profile already set wins.
"""

from unittest.mock import patch

import pytest

from app.core import claude_auth_mode
from app.core.environment import DELEGATED_CLI_ENV_DEFAULTS, apply_delegated_cli_limits

EXPECTED = {
    "BASH_MAX_OUTPUT_LENGTH": "12000",
    "MAX_MCP_OUTPUT_TOKENS": "8000",
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
}


def test_defaults_are_added_to_an_empty_env():
    env = {}
    assert apply_delegated_cli_limits(env) is env
    assert env == EXPECTED == DELEGATED_CLI_ENV_DEFAULTS


def test_a_profile_value_wins():
    env = apply_delegated_cli_limits({"BASH_MAX_OUTPUT_LENGTH": "40000", "FOO": "bar"})
    assert env["BASH_MAX_OUTPUT_LENGTH"] == "40000"
    assert env["MAX_MCP_OUTPUT_TOKENS"] == "8000"
    assert env["FOO"] == "bar"


def test_a_profile_change_respawns_a_provider_session():
    from app.core.session_pool import provider_env_requires_reconnect

    old = {"ANTHROPIC_BASE_URL": "https://gw", "MAX_MCP_OUTPUT_TOKENS": "8000"}
    new = {"ANTHROPIC_BASE_URL": "https://gw", "MAX_MCP_OUTPUT_TOKENS": "20000"}
    assert provider_env_requires_reconnect(old, new)


@pytest.mark.asyncio
async def test_the_delegated_spawn_env_carries_the_limits(mock_db, monkeypatch, tmp_path):
    from fluid.engine.delegated import DelegatedLoop
    from fluid.store.base import MemoryStore
    from fluid.tools.registry import ToolRegistry

    from app.core.provider import harness_adapter as ha
    from app.core.provider.types import SessionOptions

    monkeypatch.setattr(ha, "_resolve_claude_cmd", lambda: ["claude"])
    shared = {"FOO": "bar"}
    options = SessionOptions(
        model="opus", cwd=str(tmp_path), env=shared,
        extra={"fluid_store": MemoryStore(), "fluid_registry": ToolRegistry()},
    )
    adapter = ha.HarnessAdapter()
    with patch.object(claude_auth_mode, "current_mode", return_value="subscription"):
        await adapter.connect(options)
    try:
        assert isinstance(adapter._engine, DelegatedLoop)
        assert {k: adapter._engine.env[k] for k in EXPECTED} == EXPECTED
        assert adapter._engine.env["FOO"] == "bar"
        # Tools and MCP servers keep the CLI's defaults out of their env.
        assert not set(options.env) & set(EXPECTED)
    finally:
        await adapter.disconnect()
