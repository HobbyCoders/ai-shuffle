"""
Regression tests for the "interrupt kills all background tasks" bug.

Root cause: requires_reconnect() treated an on-disk OAuth credential rotation
(mtime change of ~/.claude/.credentials.json) as grounds for a subprocess
respawn. pool.remove() → graceful_disconnect() SIGTERMs the CLI, which kills
every background task/sub-agent parented to it — so the first query after a
token rotation (typically the user's send-now interrupt during long background
work) wiped all running tasks.

Fix: rotation is no longer a reconnect reason. credentials_rotated() detects it
and refresh_subprocess_credentials() syncs the fresh token into a
resume-materialized subprocess's temp CLAUDE_CONFIG_DIR in place (never
downgrading a token the subprocess refreshed itself), then restamps
state.credentials_mtime. The subprocess — and its background tasks — survive.
"""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.core.session_pool as sp
from app.core.session_pool import (
    SessionState,
    credentials_rotated,
    refresh_subprocess_credentials,
    requires_reconnect,
)

from claude_agent_sdk import ClaudeAgentOptions


# ─── Helpers ──────────────────────────────────────────────────────────────


def _make_state(options=None, credentials_mtime=None, sdk_client=None) -> SessionState:
    adapter = MagicMock()
    adapter.is_alive.return_value = True
    adapter.is_connected.return_value = True
    adapter.sdk_client = sdk_client
    adapter.disconnect = AsyncMock()
    return SessionState(
        adapter=adapter,
        is_connected=True,
        connect_options=options,
        credentials_mtime=credentials_mtime,
    )


def _write_creds(path, expires_at_ms: int) -> None:
    path.write_text(json.dumps({"claudeAiOauth": {
        "accessToken": f"tok-{expires_at_ms}",
        "refreshToken": f"ref-{expires_at_ms}",
        "expiresAt": expires_at_ms,
    }}))


def _materialized_client(config_dir):
    """SDK-client stand-in exposing ._materialized.config_dir like a resumed client."""
    client = MagicMock()
    client._materialized = MagicMock()
    client._materialized.config_dir = config_dir
    return client


# ─── requires_reconnect: rotation is no longer a respawn reason ─────────────


def test_credentials_rotation_does_not_require_reconnect(monkeypatch):
    """THE regression: identical options + rotated creds must reuse, not respawn."""
    opts = ClaudeAgentOptions(system_prompt="p", cwd="/w")
    state = _make_state(options=opts, credentials_mtime=100.0)
    monkeypatch.setattr(sp, "claude_credentials_mtime", lambda: 200.0)

    assert requires_reconnect(state, ClaudeAgentOptions(system_prompt="p", cwd="/w")) is False
    assert credentials_rotated(state) is True


def test_hard_option_change_still_requires_reconnect(monkeypatch):
    opts = ClaudeAgentOptions(system_prompt="p", cwd="/w")
    state = _make_state(options=opts, credentials_mtime=100.0)
    monkeypatch.setattr(sp, "claude_credentials_mtime", lambda: 100.0)

    assert requires_reconnect(state, ClaudeAgentOptions(system_prompt="CHANGED", cwd="/w")) is True


def test_credentials_not_rotated_when_mtime_matches(monkeypatch):
    state = _make_state(credentials_mtime=100.0)
    monkeypatch.setattr(sp, "claude_credentials_mtime", lambda: 100.0)
    assert credentials_rotated(state) is False


def test_permission_mode_only_change_does_not_require_reconnect():
    """A mid-session permission_mode switch must reuse the pooled client — it is
    applied in place via client.set_permission_mode() (issue #608), so it is NOT
    a respawn reason. This is the reuse precondition the in-place apply relies on.
    """
    opts = ClaudeAgentOptions(system_prompt="p", cwd="/w", permission_mode="default")
    state = _make_state(options=opts)
    new_opts = ClaudeAgentOptions(system_prompt="p", cwd="/w", permission_mode="plan")
    assert requires_reconnect(state, new_opts) is False


# ─── refresh_subprocess_credentials ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_refresh_syncs_newer_creds_into_materialized_dir(tmp_path, monkeypatch):
    host_dir = tmp_path / "host"
    mat_dir = tmp_path / "materialized"
    host_dir.mkdir()
    mat_dir.mkdir()
    _write_creds(host_dir / ".credentials.json", expires_at_ms=2_000)
    _write_creds(mat_dir / ".credentials.json", expires_at_ms=1_000)

    monkeypatch.setattr(sp, "claude_credentials_mtime", lambda: 555.0)
    from app.core.oauth import direct_oauth
    monkeypatch.setattr(direct_oauth, "config_dir", host_dir)

    state = _make_state(credentials_mtime=100.0, sdk_client=_materialized_client(mat_dir))
    await refresh_subprocess_credentials(state, "s1")

    synced = json.loads((mat_dir / ".credentials.json").read_text())
    assert synced["claudeAiOauth"]["expiresAt"] == 2_000
    assert state.credentials_mtime == 555.0
    assert not (mat_dir / ".credentials.json.tmp").exists()


@pytest.mark.asyncio
async def test_refresh_never_downgrades_subprocess_refreshed_creds(tmp_path, monkeypatch):
    """If the subprocess refreshed the token itself, its newer copy must win."""
    host_dir = tmp_path / "host"
    mat_dir = tmp_path / "materialized"
    host_dir.mkdir()
    mat_dir.mkdir()
    _write_creds(host_dir / ".credentials.json", expires_at_ms=1_000)
    _write_creds(mat_dir / ".credentials.json", expires_at_ms=9_000)

    monkeypatch.setattr(sp, "claude_credentials_mtime", lambda: 555.0)
    from app.core.oauth import direct_oauth
    monkeypatch.setattr(direct_oauth, "config_dir", host_dir)

    state = _make_state(credentials_mtime=100.0, sdk_client=_materialized_client(mat_dir))
    await refresh_subprocess_credentials(state, "s1")

    kept = json.loads((mat_dir / ".credentials.json").read_text())
    assert kept["claudeAiOauth"]["expiresAt"] == 9_000  # untouched
    assert state.credentials_mtime == 555.0  # rotation still marked handled


@pytest.mark.asyncio
async def test_refresh_on_fresh_spawn_client_only_restamps(monkeypatch):
    """Non-materialized client (shares the host config dir): nothing to copy."""
    monkeypatch.setattr(sp, "claude_credentials_mtime", lambda: 777.0)
    state = _make_state(credentials_mtime=100.0, sdk_client=None)
    await refresh_subprocess_credentials(state, "s1")
    assert state.credentials_mtime == 777.0


@pytest.mark.asyncio
async def test_refresh_restamps_even_when_copy_fails(tmp_path, monkeypatch):
    """A sync failure must not leave rotation re-detected on every query."""
    monkeypatch.setattr(sp, "claude_credentials_mtime", lambda: 888.0)

    def _boom(state):
        raise RuntimeError("adapter exploded")

    monkeypatch.setattr(sp, "_materialized_config_dir", _boom)
    state = _make_state(credentials_mtime=100.0)
    await refresh_subprocess_credentials(state, "s1")  # must not raise
    assert state.credentials_mtime == 888.0
