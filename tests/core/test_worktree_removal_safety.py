"""Recursive worktree removal must never reach linked or mounted targets."""

import os
import stat
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

import app.core.checkout_manager as cm
from app.core.git_service import git_service
from tests.git_repo_helpers import git, hermetic_git_env, init_repo


@pytest.fixture
def worktree(tmp_path, monkeypatch):
    hermetic_git_env(monkeypatch, tmp_path)
    main = init_repo(tmp_path / "main")
    wt = tmp_path / "wt"
    git(main, "worktree", "add", "-q", "-b", "feature", str(wt))
    return main, wt


def test_preflight_uses_real_device_metadata_not_direntry_cache(tmp_path, monkeypatch):
    wt = tmp_path / "wt"
    (wt / "ordinary").mkdir(parents=True)
    marker = wt / "ordinary" / "keep.txt"
    marker.write_text("unchanged", encoding="utf-8")
    scandir = os.scandir

    @contextmanager
    def windows_scandir(path):
        with scandir(path) as entries:
            # On Windows the cached DirEntry metadata has st_dev=0, unlike
            # lstat. Emulate that on every host to catch false mount refusals.
            yield [SimpleNamespace(
                path=entry.path, name=entry.name,
                stat=lambda **kwargs: SimpleNamespace(st_dev=0, st_mode=stat.S_IFDIR),
            ) for entry in entries]

    monkeypatch.setattr(os, "scandir", windows_scandir)
    assert cm.sever_runtime_links(str(wt)) == {"severed": [], "errors": []}
    assert marker.read_text(encoding="utf-8") == "unchanged"


def test_linked_root_is_neither_recoverable_nor_detached(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    remnant = tmp_path / "remnant"
    cm._link_directory(target, remnant)

    assert cm._is_empty_cleanup_remnant(str(remnant), {"path": str(remnant)}) is False
    summary = cm.sever_runtime_links(str(remnant))
    assert summary["severed"] == []
    assert "worktree root is a link" in summary["errors"][0]
    assert cm._is_directory_link(remnant)
    assert target.is_dir()


def test_legacy_junction_tag_is_still_detected(tmp_path, monkeypatch):
    candidate = tmp_path / "junction"
    candidate.mkdir()
    monkeypatch.setattr(Path, "is_symlink", lambda self: False)
    # This constant is Windows-only; supply a sentinel for the Linux simulation.
    monkeypatch.setattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", 1, raising=False)
    monkeypatch.setattr(
        os, "lstat",
        lambda path: SimpleNamespace(st_reparse_tag=stat.IO_REPARSE_TAG_MOUNT_POINT),
    )
    assert cm._is_directory_link(candidate)


@pytest.mark.parametrize("relative_parent", [".", "arbitrary/deep"])
def test_removal_detaches_unknown_nested_directory_link(
    worktree, tmp_path, monkeypatch, relative_parent
):
    main, wt = worktree
    target = tmp_path / "external"
    target.mkdir()
    marker = target / "keep.txt"
    marker.write_text("keep every byte", encoding="utf-8")
    nested_target = tmp_path / "dependencies"
    nested_target.mkdir()
    # The old shallow scan could follow an unknown parent link and detach
    # node_modules INSIDE its target. Neither that link nor its files are ours.
    cm._link_directory(nested_target, target / "node_modules")
    parent = wt / relative_parent
    parent.mkdir(parents=True, exist_ok=True)
    link = parent / "borrowed"
    cm._link_directory(target, link)
    run_git = git_service._run_git
    removals = []

    def check_preflight(cwd, args, **kwargs):
        if args[:2] == ["worktree", "remove"]:
            assert not os.path.lexists(link), "detach before Git can recurse"
            removals.append(args)
        return run_git(cwd, args, **kwargs)

    monkeypatch.setattr(git_service, "_run_git", check_preflight)
    assert git_service.remove_worktree(str(main), str(wt), force=True)
    assert len(removals) == 1

    assert not wt.exists()
    assert marker.read_text(encoding="utf-8") == "keep every byte"
    assert cm._is_directory_link(target / "node_modules")
    assert nested_target.is_dir()


@pytest.mark.parametrize("detection", ["isjunction", "reparse_attribute"])
def test_windows_junction_detection_and_rmdir_fallback(tmp_path, monkeypatch, detection):
    wt = tmp_path / "wt"
    junction = wt / "nested" / "unknown-junction"
    junction.mkdir(parents=True)
    is_link = cm._is_directory_link
    if detection == "isjunction":
        monkeypatch.setattr(os.path, "isjunction", lambda p: Path(p) == junction, raising=False)
    else:
        def reparse_info(path, info=None):
            if Path(path) == junction:
                info = SimpleNamespace(
                    st_mode=stat.S_IFDIR,
                    st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT,
                )
            return is_link(path, info)
        monkeypatch.setattr(cm, "_is_directory_link", reparse_info)
        monkeypatch.setattr(os.path, "isjunction", lambda p: False, raising=False)

    unlink = os.unlink
    rmdir = os.rmdir
    removed = []

    def windows_unlink(path, *args, **kwargs):
        if Path(path) == junction:
            raise PermissionError("directory junction requires rmdir")
        return unlink(path, *args, **kwargs)

    def record_rmdir(path, *args, **kwargs):
        removed.append(Path(path))
        return rmdir(path, *args, **kwargs)

    scandir = os.scandir

    def no_traversal(path):
        assert Path(path) != junction, "must prune junctions before scanning"
        return scandir(path)

    monkeypatch.setattr(os, "unlink", windows_unlink)
    monkeypatch.setattr(os, "rmdir", record_rmdir)
    monkeypatch.setattr(os, "scandir", no_traversal)
    summary = cm.sever_runtime_links(str(wt))

    assert summary == {"severed": [str(junction.relative_to(wt))], "errors": []}
    assert removed == [junction]
    assert not junction.exists()


@pytest.mark.parametrize("detection", ["ismount", "mount_table", "device"])
def test_mount_refuses_before_git_removal(worktree, monkeypatch, caplog, detection):
    main, wt = worktree
    mounted = wt / "nested" / "mounted"
    mounted.mkdir(parents=True)
    marker = mounted / "keep.txt"
    marker.write_text("mounted data", encoding="utf-8")
    if detection == "ismount":
        ismount = os.path.ismount
        monkeypatch.setattr(os.path, "ismount", lambda p: Path(p) == mounted or ismount(p))
    elif detection == "mount_table":
        # Same-device bind mounts are invisible to both ismount and st_dev.
        monkeypatch.setattr(cm, "_worktree_mount_points", lambda: {mounted})
        monkeypatch.setattr(os.path, "ismount", lambda p: False)
    else:
        lstat = Path.lstat

        def foreign_device(path, *args, **kwargs):
            info = lstat(path, *args, **kwargs)
            if path == wt:
                return SimpleNamespace(st_mode=info.st_mode, st_dev=info.st_dev + 1)
            return info

        monkeypatch.setattr(Path, "lstat", foreign_device)

    def no_git(*args, **kwargs):
        pytest.fail("Git must never receive a worktree containing a mount")

    monkeypatch.setattr(git_service, "_run_git", no_git)
    assert git_service.remove_worktree(str(main), str(wt), force=True) is False
    assert "refusing removal: mount point" in caplog.text
    assert marker.read_text(encoding="utf-8") == "mounted data"
    assert (wt / ".git").is_file()


def test_inspection_failure_refuses_removal(worktree, monkeypatch, caplog):
    main, wt = worktree
    nested = wt / "unreadable"
    nested.mkdir()
    scandir = os.scandir

    def unreadable(path):
        if Path(path) == nested:
            raise PermissionError("cannot inspect directory")
        return scandir(path)

    monkeypatch.setattr(os, "scandir", unreadable)
    assert git_service.remove_worktree(str(main), str(wt), force=True) is False
    assert "cannot inspect directory" in caplog.text
    assert wt.exists()


def test_git_internals_are_not_walked_but_git_entry_is_checked(tmp_path, monkeypatch):
    wt = tmp_path / "wt"
    metadata = wt / ".git"
    metadata.mkdir(parents=True)
    scandir = os.scandir

    def skip_git(path):
        assert Path(path) != metadata
        return scandir(path)

    monkeypatch.setattr(os, "scandir", skip_git)
    assert cm.sever_runtime_links(str(wt)) == {"severed": [], "errors": []}
    monkeypatch.setattr(os.path, "ismount", lambda p: Path(p) == metadata)
    assert "mount point" in cm.sever_runtime_links(str(wt))["errors"][0]


def test_linux_mount_table_unescapes_paths(monkeypatch):
    monkeypatch.setattr(os.sys, "platform", "linux")
    read_text = Path.read_text

    def mountinfo(path, *args, **kwargs):
        if path == Path("/proc/self/mountinfo"):
            return r"1 0 0:1 / /tmp/work\040tree/bind rw - tmpfs tmpfs rw" + "\n"
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", mountinfo)
    assert cm._worktree_mount_points() == {Path("/tmp/work tree/bind")}
