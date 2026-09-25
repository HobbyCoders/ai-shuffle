"""Legacy ``.claude/agents`` project agents move into ``.fluid/agents`` on listing.

UI-created project agents used to be written to ``<project>/.claude/agents``,
which Fluid never reads. Listing a project's agents moves each such file into
``.fluid/agents`` unless a same-named file is already there.
"""

import logging
from pathlib import Path

from app.core.project_agent_service import ProjectAgentService

_LEGACY = "---\ndescription: Legacy reviewer\nmodel: sonnet\n---\n\nOld prompt."
_NATIVE = "---\ndescription: Native reviewer\n---\n\nNew prompt."


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_legacy_agent_is_moved_and_listed(tmp_path, caplog):
    legacy = tmp_path / ".claude" / "agents" / "reviewer.md"
    _write(legacy, _LEGACY)

    with caplog.at_level(logging.INFO, logger="app.core.project_agent_service"):
        agents = ProjectAgentService(tmp_path).get_agents()

    assert [a.name for a in agents] == ["reviewer"]
    assert agents[0].description == "Legacy reviewer"
    assert agents[0].file_path == str(Path(".fluid") / "agents" / "reviewer.md")
    assert not legacy.exists()
    assert (tmp_path / ".fluid" / "agents" / "reviewer.md").read_text(encoding="utf-8") == _LEGACY
    assert any("Migrated legacy project agent" in r.getMessage() for r in caplog.records)

    # A second listing has nothing left to move.
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="app.core.project_agent_service"):
        assert [a.name for a in ProjectAgentService(tmp_path).get_agents()] == ["reviewer"]
    assert not any("Migrated" in r.getMessage() for r in caplog.records)


def test_existing_fluid_agent_is_not_overwritten(tmp_path):
    legacy = tmp_path / ".claude" / "agents" / "reviewer.md"
    native = tmp_path / ".fluid" / "agents" / "reviewer.md"
    _write(legacy, _LEGACY)
    _write(native, _NATIVE)

    agents = ProjectAgentService(tmp_path).get_agents()

    assert [(a.name, a.description) for a in agents] == [("reviewer", "Native reviewer")]
    assert native.read_text(encoding="utf-8") == _NATIVE
    assert legacy.read_text(encoding="utf-8") == _LEGACY


def test_missing_legacy_dir_is_a_no_op(tmp_path):
    assert ProjectAgentService(tmp_path).get_agents() == []
    assert not (tmp_path / ".fluid").exists()
    assert not (tmp_path / ".claude").exists()
