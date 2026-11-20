"""Measure the owner no-project ACP append at its producer, without a provider call."""
import subprocess
from pathlib import Path

import pytest

from app.core import auth, commit_attribution, config, preview_access, query_options, session_orchestration
from app.core.provider import harness_adapter as ha
from app.core.session_modes import owner_chat_dir
from app.db import database
from tests.core.test_antigravity_provider import _create, client, fake_agy  # noqa: F401
from tests.core.test_session_orchestration import chat


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, timeout=10)


@pytest.mark.asyncio
@pytest.mark.parametrize(("placement", "allowlist"), [
    pytest.param("standalone", None, id="standalone"),
    pytest.param("nested", None, id="nested"),
    pytest.param("windows-no-gh", None, id="windows-no-gh"),
    pytest.param("windows-nested", None, id="windows-nested"),
    pytest.param("linked", None, id="linked"),
    pytest.param("windows-no-gh", ["Monitor"], id="windows-monitor-only"),
    pytest.param("standalone", ["Monitor"], id="linux-monitor-only"),
    pytest.param("windows-no-gh", ["Read"], id="windows-read-only"),
])
async def test_repository_scope_at_the_acp_producer(
    client, monkeypatch, tmp_path, placement, allowlist,  # noqa: F811
):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    workspace = tmp_path / "workspace"
    if placement in {"nested", "windows-nested", "linked"}:
        repo = tmp_path / "repo"
        repo.mkdir()
        _git(repo, "init", "-b", "main")
        if placement == "linked":
            _git(repo, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                 "-c", "commit.gpgsign=false", "commit", "--allow-empty", "-m", "Initial commit")
            checkout = tmp_path / "linked"
            _git(repo, "worktree", "add", "-b", "feature", str(checkout))
            assert (checkout / ".git").is_file()
            database.create_project("repo", "Fixture project", None, str(repo))
        else:
            workspace = repo / "workspace"
    monkeypatch.setitem(config._runtime_overrides, "workspace_dir", workspace)
    windows = placement.startswith("windows-")
    monkeypatch.setattr(ha, "_windows_worktree_junctions", lambda: windows)
    monkeypatch.setattr(session_orchestration, "_principal", lambda sid: None)
    monkeypatch.setattr(preview_access, "listener_port", lambda: None)
    github_connected = placement != "windows-no-gh"
    monkeypatch.setattr(auth, "is_github_authenticated_cached", lambda: github_connected)
    row = _create(client, "gemini-api-key", api_key="test-gemini-placeholder")
    model = "gemini-4-hydrogen-high"
    database.update_model_provider(row["id"], models_json=[{"id": model, "name": model}])
    role = "You are the Overseer. Coordinate engineering work and report results."
    agent_config = {"model": model, "system_prompt": {"type": "custom", "content": role}}
    if allowlist is not None:
        agent_config.update(tool_selection_mode="allow", allowed_tools=allowlist)
    database.create_profile("agent", "Overseer", None, agent_config)
    if placement == "linked":
        session, _ = chat("owner-acp", mode="code", project_id="repo")
        overrides = {"cwd": str(checkout)}
        expected_cwd = checkout
    else:
        session, _ = chat("owner-acp", mode="chat")
        overrides = {}
        expected_cwd = owner_chat_dir()
    database.set_session_run_binding(
        session["id"], model=f"prov:{row['id']}:{model}", permission_mode="default",
    )
    sdk, _ = query_options.build_options_from_profile(
        database.get_profile("agent"), session_id=session["id"],
        overrides={**overrides, "system_prompt_append": "Explicit session instruction."},
    )
    assert Path(sdk.cwd) == expected_cwd
    if placement in {"nested", "windows-nested", "linked"}:
        # Git can discover the ancestor for a no-project folder as well as
        # the repository for a linked checkout. That alone must not scope the prompt.
        _git(expected_cwd, "rev-parse", "--git-dir")
    adapter = ha.HarnessAdapter()
    try:
        await adapter.connect_with_sdk_options(sdk)
        assert adapter._provider_kind == "antigravity"
        appendix = adapter._engine.system_append
        assert appendix.count(role) == 1
        assert "## Chat display" in appendix
        assert "Explicit session instruction." in appendix
        assert ("# No project" in appendix) is (placement != "linked")
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
        assert guidance in appendix
        if placement == "linked":
            assert guidance.count(ha.GITHUB_BLOCK) == 1
            assert "# Repository work" not in guidance
        else:
            if allowlist is not None:
                permits = ha._delegated_tool_predicate(adapter._options)
                assert permits is not None and not permits("Bash") and not permits("PowerShell")
                monitor = ha._bundled_registry_name("Monitor")
                served = adapter._engine.bridged_tools() + adapter._engine.deferred_tools()
                assert (monitor in served) is ("Monitor" in allowlist)
            assert guidance.count("# Repository work") == int(github_connected and allowlist is None)
            assert "git worktree list" not in guidance
            can_shell = allowlist is None or "Monitor" in allowlist
            assert guidance.count(ha.WORKTREE_REMOVAL_RULE) == int(windows and can_shell)
            assert len(guidance.encode()) <= 8_500
    finally:
        if adapter._engine is not None:
            await adapter._engine.aclose()
        if adapter._registry is not None:
            await adapter._registry.aclose()
