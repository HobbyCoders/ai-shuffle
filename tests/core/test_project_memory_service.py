"""Unit tests for ``app.core.project_memory_service``.

The service is a thin filesystem wrapper, so every test uses a real temp
project directory and asserts on the files that actually land on disk.

Coverage:
    save    — creates AGENTS.md / .agents/AGENTS.md (parent dir auto-created),
              writes the primary path even on a legacy-only project and
              leaves the legacy file untouched
    load    — prefers AGENTS.md over CLAUDE.md, falls back to the legacy file
              (content AND reported path) when no primary file exists
    delete  — removes the primary file when present, otherwise the legacy one
"""

from pathlib import Path

import pytest

from app.core.project_memory_service import ProjectMemoryService


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """An empty project directory."""
    root = tmp_path / "proj"
    root.mkdir()
    return root


@pytest.fixture
def service(project: Path) -> ProjectMemoryService:
    return ProjectMemoryService(project)


# =============================================================================
# save - always writes the primary path
# =============================================================================

def test_save_project_creates_agents_md(project, service):
    result = service.save("project", "# hello")

    assert (project / "AGENTS.md").read_text(encoding="utf-8") == "# hello"
    assert result.exists is True
    assert result.file_path == "AGENTS.md"
    assert result.content == "# hello"
    assert not (project / "CLAUDE.md").exists()


def test_save_local_creates_agents_dir_and_file(project, service):
    result = service.save("local", "local notes")

    target = project / ".agents" / "AGENTS.md"
    assert target.read_text(encoding="utf-8") == "local notes"
    assert result.file_path == ".agents/AGENTS.md"
    assert not (project / ".claude").exists()


def test_save_on_legacy_only_project_writes_primary_and_keeps_legacy(project, service):
    (project / "CLAUDE.md").write_text("legacy body", encoding="utf-8")

    result = service.save("project", "new body")

    assert (project / "AGENTS.md").read_text(encoding="utf-8") == "new body"
    assert (project / "CLAUDE.md").read_text(encoding="utf-8") == "legacy body"
    assert result.file_path == "AGENTS.md"


def test_save_local_on_legacy_only_project_keeps_legacy(project, service):
    (project / ".claude").mkdir()
    (project / ".claude" / "CLAUDE.md").write_text("legacy local", encoding="utf-8")

    service.save("local", "new local")

    assert (project / ".agents" / "AGENTS.md").read_text(encoding="utf-8") == "new local"
    assert (project / ".claude" / "CLAUDE.md").read_text(encoding="utf-8") == "legacy local"


# =============================================================================
# load - primary preferred, legacy fallback
# =============================================================================

def test_get_prefers_primary_when_both_exist(project, service):
    (project / "AGENTS.md").write_text("primary", encoding="utf-8")
    (project / "CLAUDE.md").write_text("legacy", encoding="utf-8")

    result = service.get("project")

    assert result.exists is True
    assert result.content == "primary"
    assert result.file_path == "AGENTS.md"


def test_get_falls_back_to_legacy_project_file(project, service):
    (project / "CLAUDE.md").write_text("legacy body", encoding="utf-8")

    result = service.get("project")

    assert result.exists is True
    assert result.content == "legacy body"
    assert result.file_path == "CLAUDE.md"


def test_get_falls_back_to_legacy_local_file(project, service):
    (project / ".claude").mkdir()
    (project / ".claude" / "CLAUDE.md").write_text("legacy local", encoding="utf-8")

    result = service.get("local")

    assert result.exists is True
    assert result.content == "legacy local"
    assert result.file_path == ".claude/CLAUDE.md"


def test_get_reports_primary_path_when_nothing_exists(service):
    assert service.get("project").exists is False
    assert service.get("project").file_path == "AGENTS.md"
    assert service.get("local").file_path == ".agents/AGENTS.md"


def test_get_all_returns_both_locations(project, service):
    (project / "CLAUDE.md").write_text("legacy", encoding="utf-8")
    service.save("local", "local body")

    by_location = {f.location: f for f in service.get_all()}

    assert set(by_location) == {"project", "local"}
    assert by_location["project"].file_path == "CLAUDE.md"
    assert by_location["local"].file_path == ".agents/AGENTS.md"


def test_get_rejects_unknown_location(service):
    with pytest.raises(ValueError):
        service.get("nope")


# =============================================================================
# delete - resolved file only
# =============================================================================

def test_delete_removes_primary_and_spares_legacy(project, service):
    (project / "AGENTS.md").write_text("primary", encoding="utf-8")
    (project / "CLAUDE.md").write_text("legacy", encoding="utf-8")

    assert service.delete("project") is True

    assert not (project / "AGENTS.md").exists()
    assert (project / "CLAUDE.md").exists()


def test_delete_removes_legacy_when_no_primary(project, service):
    (project / "CLAUDE.md").write_text("legacy", encoding="utf-8")

    assert service.delete("project") is True

    assert not (project / "CLAUDE.md").exists()


def test_delete_removes_legacy_local_when_no_primary(project, service):
    (project / ".claude").mkdir()
    (project / ".claude" / "CLAUDE.md").write_text("legacy local", encoding="utf-8")

    assert service.delete("local") is True

    assert not (project / ".claude" / "CLAUDE.md").exists()


def test_delete_raises_when_nothing_exists(service):
    with pytest.raises(ValueError):
        service.delete("project")
