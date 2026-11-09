"""Short settled-task grace keeps all work in the existing Git archive route."""
import os
import time

import pytest

from tests.core import test_worktree_reclaim as fixtures
from tests.core.test_worktree_cleanup_pileup import abandoned
from tests.core.test_worktree_reclaim import git, rc, reclaim

repository = fixtures.repository


def test_settled_task_archives_while_unrelated_parent_remains_busy(repository, monkeypatch):
    repo, path, _ = abandoned(repository, monkeypatch)
    monkeypatch.setattr(rc, "_session_activity", lambda sid: {"running": True})
    assert reclaim(dry_run=False)["reclaimed"] == 1
    assert not path.exists()
    assert git(repo, "show", "refs/fluid-archive/chore/task-w2:notes.txt") == "untracked findings"


@pytest.mark.parametrize("days,removed", [(2, False), (8, True)])
def test_missing_worker_row_requires_old_checkout_and_archives_work(repository, monkeypatch, days, removed):
    repo, path, _ = abandoned(repository, monkeypatch)
    monkeypatch.setattr(rc, "_project_sessions", lambda source: [])
    stamp = time.time() - days * 86400
    os.utime(path, (stamp, stamp))
    result = reclaim(dry_run=False)
    assert bool(result["reclaimed"]) is removed
    if removed:
        assert git(repo, "show", "refs/fluid-archive/chore/task-w2:tracked.txt") == "edited after the commit"
    else:
        assert (path / "notes.txt").is_file()


def test_settled_failed_dispatch_without_finished_turn_can_archive(repository, monkeypatch):
    _, path, _ = abandoned(repository, monkeypatch, is_active=False)
    original = rc._project_sessions
    monkeypatch.setattr(rc, "_project_sessions", lambda source: [{**s, "finished_at": None}
                                                               for s in original(source)])
    assert reclaim(dry_run=False)["reclaimed"] == 1
    assert not path.exists()


@pytest.mark.parametrize("directory,manifest", [("target", "Cargo.toml"), ("Intermediate", "Game.uproject")])
def test_known_generated_output_does_not_pin_settled_task(repository, monkeypatch, directory, manifest):
    repo, path, _ = abandoned(repository, monkeypatch)
    (path / manifest).write_text("project manifest")
    output = path / directory
    output.mkdir()
    (output / "generated").write_text("rebuildable")
    (path / ".gitignore").write_text(directory + "/\n")
    assert reclaim(dry_run=False)["reclaimed"] == 1
    assert git(repo, "show", f"refs/fluid-archive/chore/task-w2:{manifest}") == "project manifest"


def test_unknown_ignored_target_is_still_protected(repository, monkeypatch):
    _, path, _ = abandoned(repository, monkeypatch)
    output = path / "target"
    output.mkdir()
    (output / "valuable").write_text("not proven generated")
    (path / ".gitignore").write_text("target/\n")
    assert reclaim(dry_run=False)["reclaimed"] == 0
    assert (output / "valuable").read_text() == "not proven generated"


def test_sweep_capacity_exceeds_daily_creation_and_grace_is_short():
    assert rc.MAX_ITEMS * 86400 / rc.INTERVAL_SECONDS > 30
    assert rc.GRACE_SECONDS <= 15 * 60
