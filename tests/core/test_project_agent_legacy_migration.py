"""Legacy ``.claude/agents`` project agents are copied into ``.fluid/agents``.

UI-created project agents used to be written to ``<project>/.claude/agents``,
which Fluid never reads. That folder is also Claude Code's own (often
git-tracked) project-agents folder, so listing a project's agents for its base
path COPIES each file into ``.fluid/agents`` unless a same-named file is already
there, and never deletes the source. Session worktrees are never migrated.
"""

import asyncio
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.core.project_agent_service import ProjectAgentService

_LEGACY = "---\ndescription: Legacy reviewer\nmodel: sonnet\n---\n\nOld prompt."
_NATIVE = "---\ndescription: Native reviewer\n---\n\nNew prompt."


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_legacy_agent_is_copied_and_source_kept(tmp_path, caplog):
    legacy = tmp_path / ".claude" / "agents" / "reviewer.md"
    _write(legacy, _LEGACY)

    with caplog.at_level(logging.INFO, logger="app.core.project_agent_service"):
        agents = ProjectAgentService(tmp_path).get_agents(migrate_legacy=True)

    assert [a.name for a in agents] == ["reviewer"]
    assert agents[0].description == "Legacy reviewer"
    assert agents[0].file_path == str(Path(".fluid") / "agents" / "reviewer.md")
    # Claude Code's own folder is left exactly as it was.
    assert legacy.read_text(encoding="utf-8") == _LEGACY
    assert (tmp_path / ".fluid" / "agents" / "reviewer.md").read_text(encoding="utf-8") == _LEGACY
    assert any("Copied legacy project agent" in r.getMessage() for r in caplog.records)

    # A second listing copies nothing.
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="app.core.project_agent_service"):
        assert [a.name for a in ProjectAgentService(tmp_path).get_agents(migrate_legacy=True)] == ["reviewer"]
    assert not any("Copied" in r.getMessage() for r in caplog.records)
    assert legacy.exists()


def test_deleted_copy_is_not_resurrected(tmp_path):
    _write(tmp_path / ".claude" / "agents" / "reviewer.md", _LEGACY)
    service = ProjectAgentService(tmp_path)
    assert [a.name for a in service.get_agents(migrate_legacy=True)] == ["reviewer"]

    service.delete_agent("reviewer")

    assert service.get_agents(migrate_legacy=True) == []
    assert (tmp_path / ".claude" / "agents" / "reviewer.md").exists()


def test_existing_fluid_agent_is_not_overwritten(tmp_path):
    legacy = tmp_path / ".claude" / "agents" / "reviewer.md"
    native = tmp_path / ".fluid" / "agents" / "reviewer.md"
    _write(legacy, _LEGACY)
    _write(native, _NATIVE)

    agents = ProjectAgentService(tmp_path).get_agents(migrate_legacy=True)

    assert [(a.name, a.description) for a in agents] == [("reviewer", "Native reviewer")]
    assert native.read_text(encoding="utf-8") == _NATIVE
    assert legacy.read_text(encoding="utf-8") == _LEGACY


def test_listing_without_migration_touches_nothing(tmp_path):
    legacy = tmp_path / ".claude" / "agents" / "reviewer.md"
    _write(legacy, _LEGACY)

    assert ProjectAgentService(tmp_path).get_agents() == []
    assert legacy.read_text(encoding="utf-8") == _LEGACY
    assert not (tmp_path / ".fluid").exists()


def test_missing_legacy_dir_is_a_no_op(tmp_path):
    assert ProjectAgentService(tmp_path).get_agents(migrate_legacy=True) == []
    assert not (tmp_path / ".fluid").exists()
    assert not (tmp_path / ".claude").exists()


def _subagents_all(project_dir: Path, worktree_dir: Path, worktree_id):
    """Run the composer autocomplete route against real directories."""
    from app.api import agents as agents_api
    from app.core.project_agent_service import clear_agent_service_cache

    clear_agent_service_cache()
    request = MagicMock()
    request.state.api_user = None
    db = MagicMock()
    db.get_profile.return_value = {"id": "p", "name": "P", "config": {}}
    db.get_project.return_value = {"id": "proj", "name": "Proj", "path": str(project_dir)}
    db.get_worktree.return_value = {
        "id": "wt", "worktree_path": str(worktree_dir), "repository_id": "repo",
    }
    db.get_git_repository_by_project.return_value = {"id": "repo"}
    plugin_service = MagicMock()
    plugin_service.get_file_based_agents.return_value = []
    user_service = MagicMock()
    user_service.get_agents.return_value = []
    with patch.object(agents_api, "database", db), \
         patch("app.api.git_worktrees.database", db), \
         patch.object(agents_api, "get_api_user_from_request", return_value=None), \
         patch.object(agents_api, "get_project_working_dir_as_path", return_value=project_dir), \
         patch.object(agents_api, "get_worktree_working_dir_as_path", return_value=worktree_dir), \
         patch.object(agents_api, "get_plugin_service", return_value=plugin_service), \
         patch.object(agents_api, "get_user_agent_service", return_value=user_service):
        try:
            return asyncio.run(agents_api.get_all_available_subagents(
                request, "p", project_id="proj", worktree_id=worktree_id,
                search="", limit=30, token="t",
            ))
        finally:
            clear_agent_service_cache()


def test_composer_autocomplete_never_migrates_in_a_worktree(tmp_path):
    project_dir = tmp_path / "project"
    worktree_dir = tmp_path / "worktree"
    tracked = worktree_dir / ".claude" / "agents" / "reviewer.md"
    _write(tracked, _LEGACY)
    project_dir.mkdir()

    _subagents_all(project_dir, worktree_dir, worktree_id="wt")

    assert tracked.read_text(encoding="utf-8") == _LEGACY
    assert not (worktree_dir / ".fluid").exists()


def test_composer_autocomplete_copies_for_the_base_path(tmp_path):
    project_dir = tmp_path / "project"
    legacy = project_dir / ".claude" / "agents" / "reviewer.md"
    _write(legacy, _LEGACY)

    result = _subagents_all(project_dir, tmp_path / "unused", worktree_id=None)

    assert [item.id for item in result] == ["project:reviewer"]
    assert legacy.read_text(encoding="utf-8") == _LEGACY
    assert (project_dir / ".fluid" / "agents" / "reviewer.md").exists()


def _symlinked_fluid(tmp_path, link_target):
    project = tmp_path / "project"
    _write(project / ".claude" / "agents" / "evil.md", _LEGACY)
    (project / ".fluid").symlink_to(link_target, target_is_directory=True)
    return project


def test_symlinked_fluid_dir_is_never_written_through(tmp_path):
    # A cloned repo ships ``.fluid -> <user-global agents home>``.
    outside = tmp_path / "outside"
    outside.mkdir()
    project = _symlinked_fluid(tmp_path, outside)

    ProjectAgentService(project).get_agents(migrate_legacy=True)

    assert list(outside.rglob("*")) == []


def test_symlinked_agents_dir_inside_the_project_is_refused(tmp_path):
    project = tmp_path / "project"
    _write(project / ".claude" / "agents" / "evil.md", _LEGACY)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (project / ".fluid").mkdir()
    (project / ".fluid" / "agents").symlink_to(elsewhere, target_is_directory=True)

    ProjectAgentService(project).get_agents(migrate_legacy=True)

    assert list(elsewhere.iterdir()) == []
