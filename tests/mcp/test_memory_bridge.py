"""The delegated leg's app-memory bridge (docs/MEMORY_ARCHITECTURE.md).

The claude CLI subprocess sees the app's tools only through MCP, so the
builtins server re-exposes the memory surface fluid's builtins give native
legs: Remember and Recall over the continual SkillStore, SessionSearch and
SessionView over the Postgres store's recall hooks. These tests pin the
gating (which tools exist under which settings) and drive the durable-memory
tools end to end against a temp skills root; the session pair is registration-
gated on a database, so only its wiring is asserted here.
"""

from unittest.mock import patch

import pytest


def _bridge_tools(session_id="session-1", cwd=None):
    import app.mcp.inproc as sdk

    from app.mcp import builtin_tools

    captured = {}
    real_create = sdk.create_tool_server

    def _spy(name, version, tools):
        captured["tools"] = tools
        return real_create(name=name, version=version, tools=tools)

    with patch.object(sdk, "create_tool_server", side_effect=_spy):
        builtin_tools.create_builtin_tools_server(session_id=session_id, cwd=cwd)

    return {tool.name: tool for tool in captured["tools"]}


# ─── registration gating ─────────────────────────────────────────────────────


def test_journal_pair_registered_by_default(monkeypatch):
    """Continual + code mode default ON; no database in unit tests, so the
    session pair stays off while Remember/Recall ride every server."""
    from app.core.config import settings

    monkeypatch.setattr(type(settings), "managed_database_available", lambda self: False)
    tools = _bridge_tools()
    assert "Remember" in tools
    assert "Recall" in tools
    assert "SessionSearch" not in tools
    assert "SessionView" not in tools


def test_session_pair_needs_a_database(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(type(settings), "managed_database_available", lambda self: True)
    tools = _bridge_tools()
    assert "SessionSearch" in tools
    assert "SessionView" in tools
    # The CLI leg has no HistorySearch, so the description must not route to
    # it (ruling T9 for descriptions) — fluid's describe() gets the bridged
    # surface, not the native one.
    assert "HistorySearch" not in tools["SessionSearch"].description


def test_continual_off_keeps_explicit_memory(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "fluid_continual", False)
    tools = _bridge_tools()
    assert "Remember" in tools
    # The store still holds whatever was learned before the switch-off.
    assert "Recall" in tools


def test_inference_and_code_mode_off_keep_explicit_memory(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "fluid_continual", False)
    monkeypatch.setattr(settings, "fluid_code_mode", False)
    tools = _bridge_tools()
    assert "Recall" in tools
    assert "Remember" in tools


def test_native_shadow_set_covers_the_memory_four():
    """On native legs fluid's bare-named builtins serve the same stores; the
    bridged copies must be dropped there or the model sees two of each."""
    from app.core.provider.fluid_tool_bridge import NATIVE_SHADOWED_BUILTINS

    assert {"Remember", "Recall", "Forget", "SessionSearch", "SessionView"} <= set(
        NATIVE_SHADOWED_BUILTINS
    )


# ─── the journal-backed pair, end to end ─────────────────────────────────────


@pytest.mark.asyncio
async def test_remember_commits_the_fact_before_returning(monkeypatch, tmp_path):
    from app.core.config import settings

    monkeypatch.setattr(settings, "fluid_skills_dir", str(tmp_path / "skills"))
    tools = _bridge_tools()

    result = await tools["Remember"].handler(
        {"text": "the staging deploy runs from the blue box", "scope": "project"}
    )

    assert not result.get("isError")
    assert "Saved" in result["content"][0]["text"]
    from fluid.codemode.skills import SkillStore
    (entry,) = SkillStore(tmp_path / "skills").memories()
    # An agent-initiated save is durable but evictable; only a
    # user_requested save is permanent.
    assert entry.durable and not entry.protected
    assert entry.scope != "global"
    assert "blue box" in entry.text
    assert not (tmp_path / "skills" / "journal.jsonl").exists()

    await tools["Remember"].handler(
        {"text": "the user wants deploys on Fridays", "user_requested": True}
    )
    pinned = [e for e in SkillStore(tmp_path / "skills").memories() if "Fridays" in e.text]
    assert pinned and pinned[0].protected


@pytest.mark.asyncio
async def test_remember_refuses_secrets(monkeypatch, tmp_path):
    """fluid's screening rides the bridge unchanged: a credential is refused
    before it can reach the journal."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "fluid_skills_dir", str(tmp_path / "skills"))
    tools = _bridge_tools()

    result = await tools["Remember"].handler(
        {"text": "the api key is sk-ant-abc123def456ghi789"}
    )

    assert result.get("isError")
    assert not (tmp_path / "skills" / "journal.jsonl").exists()


@pytest.mark.asyncio
async def test_recall_searches_the_shared_store(monkeypatch, tmp_path):
    """Recall runs fluid's handler over the SAME store the native leg wires —
    an empty store answers with fluid's honest no-match text, proving the
    whole path (SDK envelope → fluid ctx → SkillStore) is live."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "fluid_skills_dir", str(tmp_path / "skills"))
    tools = _bridge_tools()

    result = await tools["Recall"].handler({"query": "staging deploy"})

    assert not result.get("isError")
    assert "No stored memory or skill matches" in result["content"][0]["text"]
