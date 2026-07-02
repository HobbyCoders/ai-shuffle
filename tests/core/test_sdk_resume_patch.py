"""
Regression tests: store-backed resumes must reuse the real Claude config dir.

The stock SDK materializes every resume into a fresh temp CLAUDE_CONFIG_DIR
(``claude-resume-*``). The CLI's Claude-in-Chrome setup embeds
``$CLAUDE_CONFIG_DIR/chrome/chrome-native-host.bat`` in the global
native-messaging manifest, so a fresh dir per spawn rewrites the manifest on
every resumed spawn — and any rewrite makes the CLI open the extension
reconnect page in the browser ("auth window on every message"). The manifest
was also left pointing into temp dirs that get deleted on disconnect.

Fix under test (app/core/sdk_resume_patch.py): with no custom
CLAUDE_CONFIG_DIR, materialize the transcript into the REAL config dir's
projects tree and spawn with NO config-dir override, so resumed spawns are
indistinguishable from fresh spawns to the CLI's manifest check. Custom
config dirs keep stock behavior.
"""

import json
import uuid
from pathlib import Path

import pytest

import claude_agent_sdk._internal.session_resume as sr
from claude_agent_sdk.types import ClaudeAgentOptions

import app.core.sdk_resume_patch as patch_mod
from app.core.sdk_resume_patch import install_stable_resume_patch


SESSION_ID = str(uuid.uuid4())
ENTRIES = [
    {"type": "user", "uuid": str(uuid.uuid4()), "message": {"content": "hi"}},
    {"type": "assistant", "uuid": str(uuid.uuid4()), "message": {"content": "yo"}},
]


class _FakeStore:
    """Minimal SessionStore: load() only (no list_subkeys → subkeys skipped)."""

    def __init__(self, entries=None):
        self._entries = ENTRIES if entries is None else entries

    async def load(self, key):
        return self._entries

    async def append(self, key, entries):  # pragma: no cover - protocol filler
        return None

    async def delete(self, key):  # pragma: no cover - protocol filler
        return None

    async def list_sessions(self, project_key):  # pragma: no cover
        return []


@pytest.fixture()
def fake_home(tmp_path, monkeypatch):
    """Redirect the patch's idea of ~/.claude and clear env overrides."""
    home = tmp_path / "claude-home"
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(patch_mod, "_default_config_dir", lambda: home)
    install_stable_resume_patch()
    return home


@pytest.mark.asyncio
async def test_resume_materializes_into_real_config_dir(fake_home, tmp_path):
    cwd = tmp_path / "proj"
    cwd.mkdir()
    options = ClaudeAgentOptions(
        resume=SESSION_ID, session_store=_FakeStore(), cwd=str(cwd), env={}
    )

    result = await sr.materialize_resume_session(options)

    assert result is not None
    assert Path(result.config_dir) == fake_home
    assert result.resume_session_id == SESSION_ID

    project_key = sr.project_key_for_directory(str(cwd))
    transcript = fake_home / "projects" / project_key / f"{SESSION_ID}.jsonl"
    assert transcript.exists()
    lines = transcript.read_text(encoding="utf-8").strip().splitlines()
    assert [json.loads(line) for line in lines] == ENTRIES

    # No auth files are copied — the subprocess reads the real ones.
    assert not (fake_home / ".credentials.json").exists()
    assert not (fake_home / ".claude.json").exists()

    # cleanup() must never delete the real config dir.
    await result.cleanup()
    assert transcript.exists()


@pytest.mark.asyncio
async def test_apply_options_omits_config_dir_override(fake_home, tmp_path):
    """The subprocess must spawn WITHOUT CLAUDE_CONFIG_DIR so it resolves
    ~/.claude.json, credentials, and the chrome manifest like a fresh spawn."""
    cwd = tmp_path / "proj"
    cwd.mkdir()
    options = ClaudeAgentOptions(
        resume=SESSION_ID,
        session_store=_FakeStore(),
        cwd=str(cwd),
        env={},
        continue_conversation=False,
    )
    materialized = await sr.materialize_resume_session(options)

    applied = sr.apply_materialized_options(options, materialized)

    assert "CLAUDE_CONFIG_DIR" not in applied.env
    assert applied.resume == SESSION_ID
    assert applied.continue_conversation is False


@pytest.mark.asyncio
async def test_custom_config_dir_keeps_stock_temp_dir_behavior(
    fake_home, tmp_path
):
    """Operators that set CLAUDE_CONFIG_DIR keep the SDK's isolated temp dir."""
    custom = tmp_path / "custom-config"
    custom.mkdir()
    cwd = tmp_path / "proj"
    cwd.mkdir()
    options = ClaudeAgentOptions(
        resume=SESSION_ID,
        session_store=_FakeStore(),
        cwd=str(cwd),
        env={"CLAUDE_CONFIG_DIR": str(custom)},
    )

    result = await sr.materialize_resume_session(options)
    try:
        assert result is not None
        temp_dir = Path(result.config_dir)
        assert temp_dir != fake_home
        assert temp_dir.name.startswith("claude-resume-")
        # Stock apply sets the env override for temp-dir materializations.
        applied = sr.apply_materialized_options(options, result)
        assert applied.env.get("CLAUDE_CONFIG_DIR") == str(temp_dir)
    finally:
        if result is not None:
            await result.cleanup()
    assert not Path(result.config_dir).exists()


@pytest.mark.asyncio
async def test_no_store_or_no_resume_returns_none(fake_home):
    assert (
        await sr.materialize_resume_session(ClaudeAgentOptions(resume=SESSION_ID))
        is None
    )
    assert (
        await sr.materialize_resume_session(
            ClaudeAgentOptions(session_store=_FakeStore())
        )
        is None
    )


@pytest.mark.asyncio
async def test_invalid_session_id_returns_none(fake_home, tmp_path):
    options = ClaudeAgentOptions(
        resume="../../evil", session_store=_FakeStore(), cwd=str(tmp_path), env={}
    )
    assert await sr.materialize_resume_session(options) is None


def test_install_is_idempotent_and_rebinds_internal_client(fake_home):
    import claude_agent_sdk._internal.client as internal_client

    patched = sr.materialize_resume_session
    install_stable_resume_patch()  # second call must not re-wrap
    assert sr.materialize_resume_session is patched
    assert internal_client.materialize_resume_session is patched
    assert internal_client.apply_materialized_options is sr.apply_materialized_options
