"""Measure the owner no-project ACP append at its producer, without a provider call."""
from pathlib import Path

import pytest

from app.core import auth, commit_attribution, config, preview_access, query_options, session_orchestration
from app.core.provider import harness_adapter as ha
from app.core.session_modes import owner_chat_dir
from app.db import database
from tests.core.test_antigravity_provider import _create, client, fake_agy  # noqa: F401
from tests.core.test_session_orchestration import chat


@pytest.mark.asyncio
async def test_owner_no_project_append(client, monkeypatch, tmp_path):  # noqa: F811
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setitem(config._runtime_overrides, "workspace_dir", tmp_path / "workspace")
    monkeypatch.setattr(session_orchestration, "_principal", lambda sid: None)
    monkeypatch.setattr(preview_access, "listener_port", lambda: None)
    monkeypatch.setattr(auth, "is_github_authenticated_cached", lambda: True)
    row = _create(client, "gemini-api-key", api_key="test-gemini-placeholder")
    model = "gemini-4-hydrogen-high"
    database.update_model_provider(row["id"], models_json=[{"id": model, "name": model}])
    role = "You are the Overseer. Coordinate engineering work and report results."
    database.create_profile("agent", "Overseer", None, {
        "model": model, "system_prompt": {"type": "custom", "content": role},
    })
    session, _ = chat("owner-acp", mode="chat")
    database.set_session_run_binding(
        session["id"], model=f"prov:{row['id']}:{model}", permission_mode="default",
    )
    sdk, _ = query_options.build_options_from_profile(
        database.get_profile("agent"), session_id=session["id"],
        overrides={"system_prompt_append": "Explicit session instruction."},
    )
    assert Path(sdk.cwd) == owner_chat_dir()
    adapter = ha.HarnessAdapter()
    try:
        await adapter.connect_with_sdk_options(sdk)
        assert adapter._provider_kind == "antigravity"
        appendix = adapter._engine.system_append
        assert appendix.count(role) == 1
        assert "## Chat display" in appendix
        assert "Explicit session instruction." in appendix
        assert "# No project" in appendix
        assert adapter._engine.model == model
        assert adapter._options.permission_mode == "default"
        assert "appended to the end of a tool result" not in appendix
        assert "Use Claude Code's dedicated tools" not in appendix
        shared = ha._antigravity_guidance_blocks(
            adapter._options, adapter._registry, commit_attribution=commit_attribution.read(),
            host_servers=adapter._host_mcp_servers,
        )
        guidance = "\n\n".join(shared)
        # Only the generated layer is budgeted: profiles, discovered instruction
        # files and skills are not limited by this test. Before: ~11.6 KB.
        # Allow for either browser route and both attribution settings.
        print(f"ACP append: {len(appendix)} chars / {len(appendix.encode())} bytes; "
              f"app guidance: {len(guidance.encode())} bytes")
        assert len(guidance.encode()) <= 8_500
        assert guidance in appendix
        assert guidance.count("# Repository work") == 1
        assert "git worktree list" not in guidance
    finally:
        if adapter._engine is not None:
            await adapter._engine.aclose()
        if adapter._registry is not None:
            await adapter._registry.aclose()
