"""Unit tests for the explicit agent-memory import
(``app.core.memory_import_service``).

Sync is native-only, so a project's claude-CLI memory no longer reaches the
fluid dir by itself; this import is the only path across, and it must be safe
to press twice. The provider roots are real temp dirs (patched the same way
tests/core/test_memory_sync.py patches ``claude_projects_dir`` /
``fluid_projects_dir``) so the copy is exercised on disk.

Coverage:
    preview  — ready vs conflict split and counts; empty for a missing dir
    apply    — copies ready files, skips existing (content untouched), refuses
               a path escape, honours an explicit selection, source untouched
    codex    — resolves a path, previews empty, applies as a no-op
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from app.core import memory_import_service as svc
from app.core import memory_sync


PROJECT_PATH = "/workspace/ai-shuffle"
PROJECT = {"id": "p1", "link_group_id": "git:deadbeef", "path": PROJECT_PATH}
SLUG = memory_sync.get_project_dir_name(PROJECT_PATH)


@pytest.fixture
def roots(tmp_path):
    """Patched claude/codex/fluid provider roots + the two memory dirs."""
    claude_root = tmp_path / "claude-projects"
    fluid_root = tmp_path / "fluid-projects"
    codex_home = tmp_path / "home"

    source = claude_root / SLUG / "memory"
    destination = fluid_root / SLUG / "memory"

    with patch.object(memory_sync.settings, "claude_projects_dir", claude_root), \
         patch.object(memory_sync.settings, "fluid_projects_dir", str(fluid_root)), \
         patch.object(svc.Path, "home", staticmethod(lambda: codex_home)):
        yield {
            "source": source,
            "destination": destination,
            "codex_home": codex_home,
        }


def write(path: Path, name: str, content: str) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    target = path / name
    target.write_text(content, encoding="utf-8")
    return target


# =============================================================================
# preview
# =============================================================================

def test_preview_splits_ready_from_conflict(roots):
    write(roots["source"], "MEMORY.md", "- [Deploy](deploy.md)")
    write(roots["source"], "deploy.md", "build, copy, restart")
    write(roots["destination"], "MEMORY.md", "already here")

    result = svc.preview_memory_import(PROJECT, "claude")

    assert result["provider"] == "claude"
    assert result["source_dir"] == str(roots["source"])
    assert result["destination_dir"] == str(roots["destination"])
    assert {i["relative_path"]: i["status"] for i in result["items"]} == {
        "MEMORY.md": "conflict",   # destination file exists
        "deploy.md": "ready",
    }
    assert result["ready_count"] == 1
    assert result["conflict_count"] == 1
    # Sizes come from the SOURCE file, not the destination's stand-in.
    sizes = {i["relative_path"]: i["size"] for i in result["items"]}
    assert sizes["deploy.md"] == len("build, copy, restart")


def test_preview_of_a_missing_source_dir_is_empty_not_an_error(roots):
    result = svc.preview_memory_import(PROJECT, "claude")

    assert result["items"] == []
    assert result["ready_count"] == 0
    assert result["conflict_count"] == 0


def test_preview_rejects_an_unknown_provider(roots):
    with pytest.raises(ValueError):
        svc.preview_memory_import(PROJECT, "gemini")


# =============================================================================
# apply
# =============================================================================

def test_apply_copies_ready_files_and_skips_existing(roots):
    write(roots["source"], "MEMORY.md", "source index")
    write(roots["source"], "deploy.md", "build, copy, restart")
    write(roots["destination"], "MEMORY.md", "destination index")

    result = svc.apply_memory_import(PROJECT, "claude", None)

    assert result["imported"] == ["deploy.md"]
    assert result["skipped"] == [{"relative_path": "MEMORY.md", "reason": "exists"}]
    assert (roots["destination"] / "deploy.md").read_text(encoding="utf-8") == \
        "build, copy, restart"
    # The conflicting destination file keeps its own content — never overwritten.
    assert (roots["destination"] / "MEMORY.md").read_text(encoding="utf-8") == \
        "destination index"


def test_apply_creates_the_destination_dir(roots):
    write(roots["source"], "MEMORY.md", "source index")
    assert not roots["destination"].exists()

    result = svc.apply_memory_import(PROJECT, "claude", None)

    assert result["imported"] == ["MEMORY.md"]
    assert (roots["destination"] / "MEMORY.md").read_text(encoding="utf-8") == \
        "source index"


def test_apply_honours_an_explicit_selection(roots):
    write(roots["source"], "MEMORY.md", "source index")
    write(roots["source"], "deploy.md", "build, copy, restart")

    result = svc.apply_memory_import(PROJECT, "claude", ["deploy.md"])

    assert result["imported"] == ["deploy.md"]
    assert not (roots["destination"] / "MEMORY.md").exists()


def test_apply_refuses_a_path_escape(roots, tmp_path):
    write(roots["source"], "MEMORY.md", "source index")
    outside = roots["destination"].parent.parent / "evil.md"

    result = svc.apply_memory_import(PROJECT, "claude", ["../../evil.md"])

    assert result["imported"] == []
    assert result["skipped"] == [
        {"relative_path": "../../evil.md", "reason": "invalid_path"}
    ]
    assert not outside.exists()


def test_apply_skips_a_path_not_present_at_the_source(roots):
    write(roots["source"], "MEMORY.md", "source index")

    result = svc.apply_memory_import(PROJECT, "claude", ["ghost.md"])

    assert result["imported"] == []
    assert result["skipped"] == [{"relative_path": "ghost.md", "reason": "not_found"}]


def test_apply_never_modifies_the_source(roots):
    write(roots["source"], "MEMORY.md", "source index")
    write(roots["source"], "deploy.md", "build, copy, restart")

    svc.apply_memory_import(PROJECT, "claude", None)
    svc.apply_memory_import(PROJECT, "claude", None)  # idempotent second press

    assert sorted(p.name for p in roots["source"].iterdir()) == \
        ["MEMORY.md", "deploy.md"]
    assert (roots["source"] / "MEMORY.md").read_text(encoding="utf-8") == "source index"
    assert (roots["source"] / "deploy.md").read_text(encoding="utf-8") == \
        "build, copy, restart"


def test_apply_writes_no_db_rows(roots):
    """Filesystem only — the next native session's push is what shares them."""
    write(roots["source"], "MEMORY.md", "source index")
    calls = []

    with patch.object(memory_sync.memdb, "upsert_memory_file",
                      side_effect=lambda *a, **k: calls.append(a)):
        svc.apply_memory_import(PROJECT, "claude", None)

    assert calls == []


def test_apply_rejects_an_unknown_provider(roots):
    with pytest.raises(ValueError):
        svc.apply_memory_import(PROJECT, "gemini", None)


# =============================================================================
# codex — the placeholder provider (no such dir exists today)
# =============================================================================

def test_codex_dir_mirrors_the_same_slug_shape(roots):
    resolved = svc._codex_memory_dir(PROJECT_PATH)

    assert resolved == roots["codex_home"] / ".codex" / "projects" / SLUG / "memory"
    # A worktree cwd folds to the main checkout, exactly like memory_sync does.
    assert svc._codex_memory_dir(
        f"{PROJECT_PATH}/.fluid/worktrees/fix-thing-bb7d"
    ) == resolved


def test_codex_preview_and_apply_are_empty_no_ops(roots):
    # A claude dir with content must not leak into the codex provider's result.
    write(roots["source"], "MEMORY.md", "claude only")

    preview = svc.preview_memory_import(PROJECT, "codex")
    applied = svc.apply_memory_import(PROJECT, "codex", None)

    assert preview["items"] == []
    assert preview["ready_count"] == preview["conflict_count"] == 0
    assert ".codex" in preview["source_dir"]
    assert applied["imported"] == [] and applied["skipped"] == []
    assert not roots["destination"].exists()
