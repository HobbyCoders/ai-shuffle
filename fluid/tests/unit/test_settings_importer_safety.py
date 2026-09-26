"""Settings import safety: preview never writes, apply never overwrites,
symlinks and ``..`` never carry a write outside its root, and a write that
fails part-way leaves nothing behind."""

from __future__ import annotations

import json
import os
import shutil
import stat
import sys
from pathlib import Path

import pytest

from fluid.context import settings_importer as si
from fluid.context.settings_import import apply_settings_import, preview_settings_import

POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes and symlinks")


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def symlink(link: Path, target: Path, *, directory: bool = False) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(target, target_is_directory=directory)
    except OSError:
        pytest.skip("symlinks unavailable")


def snapshot(root: Path) -> dict[str, tuple[str, bytes | str, int]]:
    """Every path under ``root`` with its type, content (or link target) and mtime."""
    out: dict[str, tuple[str, bytes | str, int]] = {}
    if not root.exists():
        return out
    for dirpath, dirs, files in os.walk(root, followlinks=False):
        for name in [*dirs, *files]:
            path = Path(dirpath) / name
            info = path.lstat()
            rel = path.relative_to(root).as_posix()
            if stat.S_ISLNK(info.st_mode):
                out[rel] = ("link", os.readlink(path), info.st_mtime_ns)
            elif stat.S_ISDIR(info.st_mode):
                out[rel] = ("dir", "", info.st_mtime_ns)
            else:
                out[rel] = ("file", path.read_bytes(), info.st_mtime_ns)
    return out


def by_kind(items, kind: str):
    return [item for item in items if item.kind == kind]


def outcome(result, item_id: str):
    return next(item for item in result.items if item.id == item_id)


def _full_claude_home(source: Path, project: Path) -> None:
    write(source / ".claude/CLAUDE.md", "global rules\n")
    write(source / ".claude/skills/review/SKILL.md", "review\n")
    write(source / ".claude/agents/helper.md", "helper\n")
    write(source / ".claude/commands/ship.md", "ship\n")
    write(source / ".claude/settings.json", json.dumps({
        "hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": "g.sh"}]}]},
    }))
    write(project / "CLAUDE.md", "project rules\n")
    write(project / ".claude/agents/reviewer.md", "reviewer\n")
    write(project / ".mcp.json", json.dumps({"mcpServers": {"s": {"command": "srv"}}}))
    write(source / ".claude.json", json.dumps({"projects": {str(project): {}}}))
    write(source / ".codex/AGENTS.md", "codex rules\n")
    write(source / ".codex/config.toml", 'model = "gpt-5"\napproval_policy = "never"\n')


# ─── preview is read-only ────────────────────────────────────────────────────


def test_preview_writes_nothing_to_either_home(tmp_path: Path) -> None:
    source, destination, project = tmp_path / "src", tmp_path / "dst", tmp_path / "proj"
    _full_claude_home(source, project)
    before_source, before_project = snapshot(source), snapshot(project)

    preview = preview_settings_import(
        sources=("claude", "codex"), source_home=source, destination_home=destination
    )

    assert any(item.status == "ready" for item in preview.items)
    assert not destination.exists()
    assert snapshot(source) == before_source
    assert snapshot(project) == before_project
    # The plan is plain data: it round-trips through JSON without the payloads.
    data = json.loads(json.dumps(preview.to_dict()))
    assert all("_content" not in item for item in data["items"])


def test_apply_selecting_nothing_writes_nothing(tmp_path: Path) -> None:
    source, destination = tmp_path / "src", tmp_path / "dst"
    write(source / ".claude/CLAUDE.md", "rules\n")
    preview = preview_settings_import(sources=("claude",), source_home=source, destination_home=destination)

    result = apply_settings_import(preview, selected_ids=())

    assert [item.status for item in result.items] == [item.status for item in preview.items]
    assert not destination.exists()


def test_unknown_source_is_refused() -> None:
    with pytest.raises(ValueError, match="unsupported settings source"):
        preview_settings_import(sources=("claude", "cursor"))


# ─── never overwrite: conflict reporting ─────────────────────────────────────


def test_existing_agent_is_a_conflict_and_is_never_overwritten(tmp_path: Path) -> None:
    source, destination = tmp_path / "src", tmp_path / "dst"
    write(source / ".claude/agents/helper.md", "incoming helper\n")
    write(source / ".claude/agents/same.md", "same\n")
    native = write(destination / ".fluid/agents/helper.md", "native helper\n")
    write(destination / ".fluid/agents/same.md", "same\n")

    preview = preview_settings_import(sources=("claude",), source_home=source, destination_home=destination)
    agents = {Path(item.destination).name: item for item in by_kind(preview.items, "global_agent")}

    assert agents["helper.md"].status == "conflict"
    assert "it wins" in agents["helper.md"].message
    assert agents["same.md"].status == "duplicate"

    # Applying every id, conflicts included, still writes nothing over them.
    apply_settings_import(preview, selected_ids=[item.id for item in preview.items])
    assert native.read_text(encoding="utf-8") == "native helper\n"
    assert not list((destination / ".fluid").glob("provenance/*.json"))


def test_existing_skill_directory_is_a_conflict(tmp_path: Path) -> None:
    source, destination = tmp_path / "src", tmp_path / "dst"
    write(source / ".claude/skills/review/SKILL.md", "incoming\n")
    kept = write(destination / ".fluid/skills/review/SKILL.md", "native\n")

    preview = preview_settings_import(sources=("claude",), source_home=source, destination_home=destination)
    item = by_kind(preview.items, "global_skill")[0]
    assert item.status == "conflict"

    apply_settings_import(preview)
    assert kept.read_text(encoding="utf-8") == "native\n"
    assert sorted(p.name for p in kept.parent.iterdir()) == ["SKILL.md"]


def test_skill_directory_appearing_after_preview_is_not_merged_into(tmp_path: Path) -> None:
    source, destination = tmp_path / "src", tmp_path / "dst"
    write(source / ".claude/skills/review/SKILL.md", "incoming\n")
    write(source / ".claude/skills/review/extra.md", "extra\n")
    preview = preview_settings_import(sources=("claude",), source_home=source, destination_home=destination)
    item = by_kind(preview.items, "global_skill")[0]
    assert item.status == "ready"

    raced = write(destination / ".fluid/skills/review/SKILL.md", "native raced\n")
    result = apply_settings_import(preview)

    assert outcome(result, item.id).status == "conflict"
    assert raced.read_text(encoding="utf-8") == "native raced\n"
    assert not (raced.parent / "extra.md").exists()


# ─── project .claude/agents → .fluid/agents (the app's migration contract) ──


def _project(source: Path, project: Path) -> None:
    (project / ".git").mkdir(parents=True, exist_ok=True)
    write(source / ".claude.json", json.dumps({"projects": {str(project): {}}}))


@POSIX_ONLY
def test_project_agents_copy_like_the_app_migration(tmp_path: Path) -> None:
    """Copied, never moved; an existing ``.fluid/agents`` file wins; a symlinked
    agent is left alone — the same rules ``_migrate_legacy_agents`` follows."""
    source, destination, project = tmp_path / "src", tmp_path / "dst", tmp_path / "proj"
    _project(source, project)
    legacy = project / ".claude/agents"
    write(legacy / "fresh.md", "fresh\n")
    write(legacy / "taken.md", "incoming\n")
    outside = write(tmp_path / "elsewhere/secret.md", "secret\n")
    symlink(legacy / "linked.md", outside)
    existing = write(project / ".fluid/agents/taken.md", "native\n")
    before = snapshot(legacy)

    preview = preview_settings_import(sources=("claude",), source_home=source, destination_home=destination)
    agents = {Path(item.source_paths[0]).name: item for item in by_kind(preview.items, "project_agent")}
    assert agents["fresh.md"].status == "ready"
    assert agents["fresh.md"].destination == str(project / ".fluid/agents/fresh.md")
    assert agents["taken.md"].status == "conflict"
    assert agents["linked.md"].status == "skipped"

    result = apply_settings_import(preview)

    assert outcome(result, agents["fresh.md"].id).status == "imported"
    assert (project / ".fluid/agents/fresh.md").read_text(encoding="utf-8") == "fresh\n"
    assert existing.read_text(encoding="utf-8") == "native\n"
    assert not (project / ".fluid/agents/linked.md").exists()
    assert snapshot(legacy) == before  # sources kept, byte for byte


@POSIX_ONLY
def test_symlinked_project_fluid_dir_is_never_written_through(tmp_path: Path) -> None:
    """A cloned repo whose ``.fluid`` points at the user's global folder must
    not get agents planted there (``_agents_dir_confined`` in the app)."""
    source, destination, project = tmp_path / "src", tmp_path / "dst", tmp_path / "proj"
    _project(source, project)
    write(project / ".claude/agents/planted.md", "planted\n")
    victim = tmp_path / "victim-global-fluid"
    victim.mkdir()
    symlink(project / ".fluid", victim, directory=True)

    preview = preview_settings_import(sources=("claude",), source_home=source, destination_home=destination)
    agent = by_kind(preview.items, "project_agent")[0]
    assert agent.status == "skipped" and "symlink" in agent.message

    apply_settings_import(preview, selected_ids=[agent.id])
    assert list(victim.iterdir()) == []


@POSIX_ONLY
def test_destination_symlinked_after_preview_is_rejected_at_apply(tmp_path: Path) -> None:
    source, destination = tmp_path / "src", tmp_path / "dst"
    write(source / ".claude/agents/helper.md", "helper\n")
    preview = preview_settings_import(sources=("claude",), source_home=source, destination_home=destination)
    item = by_kind(preview.items, "global_agent")[0]
    assert item.status == "ready"

    victim = tmp_path / "victim"
    victim.mkdir()
    destination.mkdir()
    symlink(destination / ".fluid", victim, directory=True)
    result = apply_settings_import(preview)

    assert outcome(result, item.id).status == "skipped"
    assert list(victim.iterdir()) == []


@POSIX_ONLY
def test_symlinks_inside_sources_reject_the_item(tmp_path: Path) -> None:
    source, destination = tmp_path / "src", tmp_path / "dst"
    outside = write(tmp_path / "outside/creds", "token\n")
    write(source / ".claude/skills/leaky/SKILL.md", "leaky\n")
    symlink(source / ".claude/skills/leaky/creds", outside)
    symlink(source / ".claude/agents", tmp_path / "outside", directory=True)
    plugin = source / ".claude/plugins/cache/mp/plug/1.0.0"
    write(plugin / "plugin.json", "{}")
    symlink(plugin / "creds", outside)
    write(source / ".claude/settings.json", json.dumps({"enabledPlugins": {"plug@mp": True}}))

    preview = preview_settings_import(sources=("claude",), source_home=source, destination_home=destination)

    skill = by_kind(preview.items, "global_skill")[0]
    assert skill.status == "skipped" and skill.source_digest is None
    agents_root = by_kind(preview.items, "global_agent")[0]
    assert agents_root.status == "skipped"
    plugin_item = by_kind(preview.items, "plugin")[0]
    assert plugin_item.status == "diagnostic" and "symlink" in plugin_item.message
    assert not by_kind(preview.items, "plugin_enablement")

    apply_settings_import(preview, selected_ids=[item.id for item in preview.items])
    assert not destination.exists()


@POSIX_ONLY
def test_source_swapped_for_a_symlink_after_preview_is_skipped(tmp_path: Path) -> None:
    source, destination = tmp_path / "src", tmp_path / "dst"
    agent = write(source / ".claude/agents/helper.md", "helper\n")
    preview = preview_settings_import(sources=("claude",), source_home=source, destination_home=destination)
    item = by_kind(preview.items, "global_agent")[0]

    twin = write(tmp_path / "twin.md", "helper\n")  # identical bytes
    agent.unlink()
    symlink(agent, twin)
    result = apply_settings_import(preview)

    assert outcome(result, item.id).status == "skipped"
    assert not (destination / ".fluid/agents/helper.md").exists()


# ─── path confinement ────────────────────────────────────────────────────────


def _ready_destinations_confined(preview, *roots: Path) -> None:
    resolved = [root.resolve() for root in roots]
    for item in preview.items:
        if item.status != "ready" or item.destination is None:
            continue
        target = Path(os.path.normpath(item.destination))
        assert any(target.is_relative_to(root) for root in resolved), item


@pytest.mark.parametrize("ref", [
    "../../../../loot@mp",          # name climbs out of the marketplace dir
    "loot@../../..",                # marketplace climbs out: marketplaces/../../../loot is src/loot
    "sub/dir@mp",
    "..@mp",
    "D:loot@mp",                    # Windows drive-relative name
    "loot@D:",                      # Windows drive-relative marketplace
])
def test_plugin_refs_cannot_escape_the_plugin_roots(tmp_path: Path, ref: str) -> None:
    """Regression: an ``enabledPlugins`` key is joined onto both the source
    plugins root and ``~/.fluid/plugins``. ``../`` in it copied a directory
    from outside ``~/.claude`` to outside ``~/.fluid``."""
    source, destination = tmp_path / "src", tmp_path / "dst"
    (source / ".claude/plugins/marketplaces/mp").mkdir(parents=True)
    (source / ".claude/plugins/cache/mp").mkdir(parents=True)
    write(source / "loot/data.txt", "private\n")
    write(source / "loot/plugin.json", "{}")
    write(source / ".claude/settings.json", json.dumps({"enabledPlugins": {ref: True}}))

    preview = preview_settings_import(sources=("claude",), source_home=source, destination_home=destination)

    assert not [item for item in by_kind(preview.items, "plugin") if item.status == "ready"]
    assert not by_kind(preview.items, "plugin_enablement")
    rejected = [item for item in by_kind(preview.items, "plugin") if item.status == "diagnostic"]
    assert rejected
    assert all("not a plain name@marketplace" in item.message for item in rejected)
    apply_settings_import(preview)
    assert not (destination / "loot").exists()
    assert not list(tmp_path.glob("**/dst/**/data.txt"))


@pytest.mark.parametrize("value", ["D:x", "D:", "x:y"])
def test_plain_component_refuses_a_drive_prefix(value: str) -> None:
    from pathlib import PureWindowsPath

    # Why: on Windows the drive-relative segment replaces the root it joins.
    assert PureWindowsPath("C:/plugins") / "D:x" == PureWindowsPath("D:x")
    assert not si._plain_component(value)
    assert si._plain_component("good-plugin.v2")


def test_plain_plugin_ref_still_imports(tmp_path: Path) -> None:
    source, destination = tmp_path / "src", tmp_path / "dst"
    write(source / ".claude/plugins/marketplaces/mp/good-plugin.v2/plugin.json", "{}")
    write(source / ".claude/settings.json", json.dumps({"enabledPlugins": {"good-plugin.v2@mp": True}}))

    preview = preview_settings_import(sources=("claude",), source_home=source, destination_home=destination)

    plugin = next(item for item in by_kind(preview.items, "plugin") if item.status == "ready")
    assert plugin.destination == str(destination / ".fluid/plugins/marketplaces/mp/good-plugin.v2")


def test_codex_agent_reference_outside_codex_is_rejected(tmp_path: Path) -> None:
    source, destination = tmp_path / "src", tmp_path / "dst"
    write(source / "stolen.toml", 'developer_instructions = "x"\n')
    write(source / ".codex/config.toml", '[agents]\nfile = "../stolen.toml"\n')

    preview = preview_settings_import(sources=("codex",), source_home=source, destination_home=destination)

    diag = next(item for item in preview.items if item.kind == "agent")
    assert diag.status == "diagnostic" and "escapes .codex" in diag.message
    assert not [item for item in by_kind(preview.items, "global_agent") if item.status == "ready"]


def test_every_ready_destination_stays_under_fluid_or_the_project(tmp_path: Path) -> None:
    source, destination, project = tmp_path / "src", tmp_path / "dst", tmp_path / "proj"
    _full_claude_home(source, project)
    write(source / ".claude/agents/deep/nested/a.md", "a\n")

    preview = preview_settings_import(
        sources=("claude", "codex"), source_home=source, destination_home=destination
    )

    _ready_destinations_confined(preview, destination / ".fluid", project)


# ─── _write_exclusive: no overwrite, owner-only, nothing half-written ────────


class _ExplodingHandle:
    """A file object whose write lands a few bytes and then fails (disk full)."""

    def __init__(self, fd: int) -> None:
        self._fd = fd

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        os.close(self._fd)

    def write(self, data: bytes) -> None:
        os.write(self._fd, data[:3])
        raise OSError(28, "No space left on device")


def _explode_on_write(monkeypatch) -> None:
    monkeypatch.setattr(si.os, "fdopen", lambda fd, *a, **k: _ExplodingHandle(fd))


@pytest.mark.parametrize("walk", [True, False], ids=["dir-fd-walk", "portable"])
def test_write_exclusive_leaves_no_partial_file_on_failure(tmp_path: Path, monkeypatch, walk) -> None:
    if walk and not si._SUPPORTS_DIR_FD_WALK:
        pytest.skip("platform has no dir_fd walk")
    monkeypatch.setattr(si, "_SUPPORTS_DIR_FD_WALK", walk)
    target = tmp_path / "a" / "b" / "file.json"
    _explode_on_write(monkeypatch)

    with pytest.raises(OSError, match="No space"):
        si._write_exclusive(target, b"0123456789")

    assert not target.exists()


@pytest.mark.parametrize("walk", [True, False], ids=["dir-fd-walk", "portable"])
def test_write_exclusive_refuses_an_existing_file(tmp_path: Path, monkeypatch, walk) -> None:
    if walk and not si._SUPPORTS_DIR_FD_WALK:
        pytest.skip("platform has no dir_fd walk")
    monkeypatch.setattr(si, "_SUPPORTS_DIR_FD_WALK", walk)
    target = write(tmp_path / "keep.txt", "native")

    with pytest.raises(FileExistsError):
        si._write_exclusive(target, b"imported")

    assert target.read_text(encoding="utf-8") == "native"


@POSIX_ONLY
def test_write_exclusive_creates_owner_only_files_and_dirs(tmp_path: Path) -> None:
    if not si._SUPPORTS_DIR_FD_WALK:
        pytest.skip("platform has no dir_fd walk")
    target = tmp_path / "new" / "deeper" / "file.json"
    old_umask = os.umask(0)
    try:
        si._write_exclusive(target, b"{}")
    finally:
        os.umask(old_umask)
    assert target.read_bytes() == b"{}"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "new").stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp_path / "new" / "deeper").stat().st_mode) == 0o700


@POSIX_ONLY
def test_write_exclusive_refuses_a_symlinked_ancestor_itself(tmp_path: Path) -> None:
    """The last line of defence when a directory is swapped for a symlink
    between the caller's ``_path_has_symlink`` check and the write."""
    if not si._SUPPORTS_DIR_FD_WALK:
        pytest.skip("platform has no dir_fd walk")
    victim = tmp_path / "victim"
    victim.mkdir()
    symlink(tmp_path / "home" / ".fluid", victim, directory=True)

    with pytest.raises(OSError):
        si._write_exclusive(tmp_path / "home" / ".fluid" / "agents" / "x.md", b"planted")

    assert list(victim.iterdir()) == []


def test_apply_rolls_back_a_file_whose_write_failed(tmp_path: Path, monkeypatch) -> None:
    source, destination = tmp_path / "src", tmp_path / "dst"
    write(source / ".claude/agents/helper.md", "helper body\n")
    preview = preview_settings_import(sources=("claude",), source_home=source, destination_home=destination)
    item = by_kind(preview.items, "global_agent")[0]
    _explode_on_write(monkeypatch)

    result = apply_settings_import(preview)

    done = outcome(result, item.id)
    assert done.status == "skipped" and "copy failed without overwrite" in done.message
    assert not (destination / ".fluid/agents/helper.md").exists()


@pytest.mark.parametrize("kind", ["global_agent", "global_skill", "global_mcp"])
def test_apply_rolls_back_when_the_provenance_record_fails(
    tmp_path: Path, monkeypatch, kind: str
) -> None:
    source, destination = tmp_path / "src", tmp_path / "dst"
    write(source / ".claude/agents/helper.md", "helper\n")
    write(source / ".claude/skills/review/SKILL.md", "review\n")
    write(source / ".claude.json", json.dumps({"mcpServers": {"s": {"command": "srv"}}}))
    preview = preview_settings_import(sources=("claude",), source_home=source, destination_home=destination)
    item = next(it for it in by_kind(preview.items, kind) if it.status == "ready")

    def refuse(_home, _item):
        raise OSError("provenance disk full")

    monkeypatch.setattr(si, "_provenance", refuse)
    result = apply_settings_import(preview, selected_ids=[item.id])

    done = outcome(result, item.id)
    assert done.status == "skipped" and "provenance disk full" in done.message
    assert not Path(item.destination).exists()


def test_apply_removes_a_partially_copied_directory(tmp_path: Path, monkeypatch) -> None:
    source, destination = tmp_path / "src", tmp_path / "dst"
    write(source / ".claude/skills/review/SKILL.md", "review\n")
    write(source / ".claude/skills/review/big.bin", "x" * 100)
    preview = preview_settings_import(sources=("claude",), source_home=source, destination_home=destination)
    item = by_kind(preview.items, "global_skill")[0]

    def half_copy(src, dst, **_kwargs):
        Path(dst).mkdir()
        shutil.copy2(Path(src) / "SKILL.md", Path(dst) / "SKILL.md")
        raise shutil.Error([(str(src), str(dst), "disk vanished")])

    monkeypatch.setattr(si.shutil, "copytree", half_copy)
    result = apply_settings_import(preview)

    assert outcome(result, item.id).status == "skipped"
    assert not Path(item.destination).exists()


@POSIX_ONLY
def test_apply_removes_a_copied_tree_that_gained_a_symlink(tmp_path: Path, monkeypatch) -> None:
    source, destination = tmp_path / "src", tmp_path / "dst"
    write(source / ".claude/skills/review/SKILL.md", "review\n")
    preview = preview_settings_import(sources=("claude",), source_home=source, destination_home=destination)
    item = by_kind(preview.items, "global_skill")[0]
    real_copytree = shutil.copytree

    def copy_then_plant(src, dst, **kwargs):
        real_copytree(src, dst, **kwargs)
        os.symlink("/etc/passwd", Path(dst) / "planted")

    monkeypatch.setattr(si.shutil, "copytree", copy_then_plant)
    result = apply_settings_import(preview)

    done = outcome(result, item.id)
    assert done.status == "skipped" and "symlink" in done.message
    assert not Path(item.destination).exists()


# ─── hooks.json merge: the one replace, and its failure paths ───────────────


def _hooks_merge_preview(tmp_path: Path):
    source, destination = tmp_path / "src", tmp_path / "dst"
    write(source / ".claude/settings.json", json.dumps({"hooks": {
        "Stop": [{"hooks": [{"type": "command", "command": "notify.sh"}]}],
    }}))
    existing = write(destination / ".fluid/hooks.json", json.dumps({"hooks": {"PreToolUse": []}}))
    preview = preview_settings_import(sources=("claude",), source_home=source, destination_home=destination)
    item = next(it for it in preview.items if it.kind == "hooks")
    assert item.status == "ready" and item.provenance["merge_into_existing"] is True
    return preview, item, existing


def test_hooks_merge_blocked_by_a_stale_temp_file_leaves_both_alone(tmp_path: Path) -> None:
    preview, item, existing = _hooks_merge_preview(tmp_path)
    before = existing.read_bytes()
    stale = write(existing.with_name("hooks.json.fluid-import"), "someone else's")

    result = apply_settings_import(preview)

    done = outcome(result, item.id)
    assert done.status == "skipped" and "stale temporary file" in done.message
    assert existing.read_bytes() == before
    assert stale.read_text(encoding="utf-8") == "someone else's"


def test_hooks_merge_replace_failure_cleans_its_temp_file(tmp_path: Path, monkeypatch) -> None:
    preview, item, existing = _hooks_merge_preview(tmp_path)
    before = existing.read_bytes()

    def fail_replace(src, dst):
        raise PermissionError(13, "read-only filesystem")

    monkeypatch.setattr(si.os, "replace", fail_replace)
    result = apply_settings_import(preview)

    done = outcome(result, item.id)
    assert done.status == "skipped" and "merge failed" in done.message
    assert existing.read_bytes() == before
    assert not existing.with_name("hooks.json.fluid-import").exists()


@POSIX_ONLY
def test_hooks_merge_refuses_a_destination_swapped_for_a_symlink(tmp_path: Path) -> None:
    preview, item, existing = _hooks_merge_preview(tmp_path)
    elsewhere = write(tmp_path / "elsewhere.json", existing.read_text(encoding="utf-8"))
    existing.unlink()
    symlink(existing, elsewhere)
    before = elsewhere.read_bytes()

    result = apply_settings_import(preview)

    assert outcome(result, item.id).status == "skipped"  # symlink ancestor check
    assert elsewhere.read_bytes() == before


def test_hooks_merge_records_provenance_failure_without_undoing_the_merge(
    tmp_path: Path, monkeypatch
) -> None:
    preview, item, existing = _hooks_merge_preview(tmp_path)

    def refuse(_home, _item):
        raise OSError("provenance disk full")

    monkeypatch.setattr(si, "_provenance", refuse)
    result = apply_settings_import(preview)

    done = outcome(result, item.id)
    assert done.status == "imported" and "provenance record failed" in done.message
    assert "Stop" in json.loads(existing.read_text(encoding="utf-8"))["hooks"]


def test_unparseable_existing_hooks_json_is_a_conflict(tmp_path: Path) -> None:
    source, destination = tmp_path / "src", tmp_path / "dst"
    write(source / ".claude/settings.json", json.dumps({"hooks": {"Stop": []}}))
    existing = write(destination / ".fluid/hooks.json", "{not json")

    preview = preview_settings_import(sources=("claude",), source_home=source, destination_home=destination)
    item = next(it for it in preview.items if it.kind == "hooks")
    assert item.status == "conflict" and "cannot be merged" in item.message

    apply_settings_import(preview, selected_ids=[item.id])
    assert existing.read_text(encoding="utf-8") == "{not json"


def test_provenance_sidecar_never_overwrites_an_earlier_record(tmp_path: Path) -> None:
    source, destination = tmp_path / "src", tmp_path / "dst"
    write(source / ".claude/agents/helper.md", "helper\n")
    preview = preview_settings_import(sources=("claude",), source_home=source, destination_home=destination)
    item = by_kind(preview.items, "global_agent")[0]
    first = write(destination / ".fluid/provenance" / f"{item.id}.json", "earlier record")

    apply_settings_import(preview)

    assert first.read_text(encoding="utf-8") == "earlier record"
    second = json.loads((first.parent / f"{item.id}-1.json").read_text(encoding="utf-8"))
    assert second["item_id"] == item.id and second["destination"] == item.destination
