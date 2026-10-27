"""Reclaim uses actual Git worktrees, never the live repository."""
import asyncio
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.core import worktree_reclaim as rc
from tests.git_repo_helpers import hermetic_git_env
from tests.shared_fixtures import foreign_keys_off  # noqa: F401


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def reclaim(**kwargs):
    return asyncio.run(rc.reclaim("p", **kwargs))


@pytest.fixture
def repository(tmp_path, monkeypatch):
    hermetic_git_env(monkeypatch, tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "commit", "-qm", "initial", "--allow-empty")
    source = {"project_id": "p", "project": "test", "path": str(repo),
              "aliases": ["p"], "registered": []}
    monkeypatch.setattr(rc, "_local_sources", lambda: [source])
    monkeypatch.setattr(rc, "_project_sessions", lambda source: [])
    monkeypatch.setattr(rc, "_archive", Mock())
    monkeypatch.setattr(rc, "_stamp_head", Mock())
    monkeypatch.setattr(rc, "_invalidate", Mock())
    from app.api import git_operations
    from app.core.github_service import github_service
    monkeypatch.setattr(git_operations, "_release_checkout_sessions", AsyncMock(return_value=True))
    # No network: GitHub knows no commit of these disposable repositories.
    monkeypatch.setattr(github_service, "list_commit_pulls", lambda *a, **k: [])
    return repo, source


def age_directory(path):
    old = time.time() - 2 * rc.GRACE_SECONDS
    os.utime(path, (old, old))


def checkout(repository, name="feature", detached=False):
    repo, source = repository
    path = repo / ".fluid" / "worktrees" / name
    args = ["--detach"] if detached else ["-b", name]
    git(repo, "worktree", "add", "-q", *args, str(path))
    # Default removal fixtures represent old, unused checkouts. Fresh-dispatch
    # regression tests create their real registry/worktree independently.
    age_directory(path)
    source["registered"].append({"path": str(path), "branch": None if detached else name,
                                 "registry_ids": ["ck-" + name]})
    return path


def commit(path, name="change", text="integrated"):
    (path / name).write_text(text)
    git(path, "add", name)
    git(path, "commit", "-qm", name)


def test_merged_dry_run_and_apply(repository):
    repo, _ = repository
    path = checkout(repository)
    commit(path)
    git(repo, "merge", "--ff-only", "feature")
    preview = reclaim()
    assert preview["items"][0]["decision"] == "remove"
    assert path.exists()
    result = reclaim(dry_run=False)
    assert result["reclaimed"] == 1
    assert not path.exists()
    assert not rc.wr._git(str(repo), "show-ref", "--verify", "refs/heads/feature")
    rc._archive.assert_called_once()


def test_squash_merge_is_contained(repository):
    repo, _ = repository
    path = checkout(repository)
    commit(path)
    git(repo, "merge", "--squash", "feature")
    git(repo, "commit", "-qm", "squashed")
    # No ancestry and different commit ids: exercise merge-tree containment.
    assert git(repo, "rev-list", "--count", "main..feature") == "1"
    assert reclaim(dry_run=False)["reclaimed"] == 1
    assert not path.exists()


@pytest.mark.parametrize("kind", ["unmerged", "dirty", "unpushed", "closed-pr", "detached-unmerged"])
def test_valuable_work_is_retained(repository, monkeypatch, kind):
    repo, _ = repository
    path = checkout(repository, detached=kind == "detached-unmerged")
    if kind == "dirty":
        (path / "untracked").write_text("valuable")
    else:
        if kind == "unpushed":
            git(repo, "update-ref", "refs/remotes/origin/feature", "HEAD")
            git(repo, "config", "branch.feature.remote", "origin")
            git(repo, "config", "branch.feature.merge", "refs/heads/feature")
        commit(path)
    if kind == "closed-pr":
        old = datetime.fromtimestamp(time.time() - 2 * rc.GRACE_SECONDS, timezone.utc).isoformat()
        monkeypatch.setattr(rc, "_project_sessions", lambda source: [{"id": "s", "branch": "feature", "pr_state": "closed",
                                                                    "updated_at": old, "finished_at": old}])
    result = reclaim(dry_run=False)
    assert result["reclaimed"] == 0 and result["items"][0]["decision"] == "keep"
    assert path.exists()
    if kind != "dirty":
        assert "unmerged or unpushed" in result["items"][0]["reason"]


@pytest.mark.parametrize("session", [
    {"id": "s", "branch": "feature", "is_active": True},
    {"id": "s", "worktree_branch": "feature", "running": True},
    {"id": "s", "branch": "feature", "pr_state": "open"},
    {"id": "s", "worktree_id": "ck-feature", "is_active": True},
])
def test_active_sidebar_running_and_open_pr_protected(repository, monkeypatch, session):
    path = checkout(repository)
    monkeypatch.setattr(rc, "_project_sessions", lambda source: [session])
    assert reclaim(dry_run=False)["reclaimed"] == 0
    assert path.exists()


@pytest.mark.parametrize("state,exact,expected", [("OPEN", True, 0), ("CLOSED", True, 0),
                                                ("MERGED", False, 0), ("MERGED", True, 1)])
def test_pr_head_must_match_and_open_always_wins(repository, monkeypatch, state, exact, expected):
    from app.core.github_service import github_service

    repo, _ = repository
    path = checkout(repository)
    commit(path)
    git(repo, "remote", "add", "origin", "https://github.com/example/disposable.git")
    pr = SimpleNamespace(state=state, head_sha=git(path, "rev-parse", "HEAD") if exact else "0" * 40)
    if state == "OPEN":
        git(repo, "merge", "--ff-only", "feature")  # integrated but still protected by the open PR
    monkeypatch.setattr(github_service, "list_pulls", lambda *a, **k: [pr])
    assert reclaim(dry_run=False)["reclaimed"] == expected
    assert path.exists() is (not expected)


def test_pr_lookup_failure_retains_work(repository, monkeypatch):
    from app.core.github_service import github_service

    repo, _ = repository
    path = checkout(repository)
    git(repo, "remote", "add", "origin", "https://github.com/example/disposable.git")
    monkeypatch.setattr(github_service, "list_pulls", Mock(side_effect=RuntimeError("offline")))
    assert reclaim(dry_run=False)["reclaimed"] == 0
    assert path.exists()


@pytest.mark.parametrize("merged,gone", [(True, False), (False, True), (False, False)])
def test_missing_directory_reconciles_only_safe_history(repository, merged, gone):
    import shutil

    repo, _ = repository
    path = checkout(repository)
    if not merged:
        commit(path)
    shutil.rmtree(path)  # disposable fixture only; never the application removal path
    if gone:
        git(repo, "worktree", "prune")
        git(repo, "branch", "-D", "feature")
    result = reclaim(dry_run=False)
    assert result["reclaimed"] == int(merged or gone)
    if merged or gone:
        assert result["items"][0]["decision"] == "forget"
        assert len(rc.wr.parse_worktree_list(git(repo, "worktree", "list", "--porcelain"))) == 1
        rc._archive.assert_called_once()
    else:
        assert git(repo, "rev-parse", "feature")


def test_detached_contained_checkout(repository):
    path = checkout(repository, detached=True)
    assert reclaim(dry_run=False)["reclaimed"] == 1
    assert not path.exists()


def test_link_targets_untouched_and_severed_before_git(repository, tmp_path, monkeypatch):
    repo, _ = repository
    path = checkout(repository)
    target = tmp_path / "runtime"
    target.mkdir()
    (target / "valuable").write_text("keep")
    link = path / "node_modules"
    if os.name == "nt":
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True)
    else:
        link.symlink_to(target, target_is_directory=True)
    excludes = repo / ".git" / "info" / "exclude"
    excludes.write_text(excludes.read_text() + "\nnode_modules\n")
    original = rc.git_service._run_git
    observed = []

    def tracked(cwd, args, **kwargs):
        if args[:2] == ["worktree", "remove"]:
            observed.append(not link.exists() and not link.is_symlink())
        return original(cwd, args, **kwargs)

    monkeypatch.setattr(rc.git_service, "_run_git", tracked)
    age_directory(path)  # creating the runtime link changed the directory mtime
    assert reclaim(dry_run=False)["reclaimed"] == 1
    assert observed == [True]
    assert (target / "valuable").read_text() == "keep"


def test_sever_failure_blocks_git_removal(repository, monkeypatch):
    from app.core import checkout_manager

    path = checkout(repository)
    monkeypatch.setattr(checkout_manager, "sever_runtime_links", lambda path: {"severed": [], "errors": ["blocked"]})
    assert reclaim(dry_run=False)["reclaimed"] == 0
    assert path.exists()


def test_base_default_locked_and_unregistered_protected(repository):
    repo, source = repository
    path = checkout(repository)
    git(repo, "worktree", "lock", str(path))
    source["registered"].append({"path": str(repo), "branch": "main", "registry_ids": ["base"]})
    external = repo / ".fluid" / "worktrees" / "external"
    git(repo, "worktree", "add", "-q", "-b", "external", str(external))
    result = reclaim(dry_run=False)
    assert result["total"] == 2 and result["reclaimed"] == 0
    assert path.exists() and repo.exists() and external.exists()


def test_apply_rechecks_changed_head_and_activity(repository, monkeypatch):
    path = checkout(repository)
    original = rc._apply

    def moved(source, registered, row, **kwargs):
        commit(path, "late", "new unmerged work")
        return original(source, registered, row, **kwargs)

    monkeypatch.setattr(rc, "_apply", moved)
    assert reclaim(dry_run=False)["reclaimed"] == 0
    assert (path / "late").exists()


def test_bounded_pagination(repository):
    checkout(repository, "one")
    checkout(repository, "two")
    first = reclaim(limit=1)
    assert len(first["items"]) == 1 and first["next_offset"] == 1
    second = reclaim(limit=1, offset=first["next_offset"])
    assert second["next_offset"] is None
    assert first["items"][0]["path"] != second["items"][0]["path"]


def test_automatic_grace_and_activity(repository, monkeypatch):
    path = checkout(repository)
    observations = {}
    now = time.time()
    monkeypatch.setattr(rc.time, "time", lambda: now)
    result = reclaim(dry_run=False, observations=observations)
    assert result["reclaimed"] == 0 and "grace" in result["items"][0]["reason"]
    now += rc.GRACE_SECONDS + 1
    monkeypatch.setattr(rc, "_project_sessions", lambda source: [{
        "id": "s", "branch": "feature", "updated_at": datetime.fromtimestamp(now, timezone.utc).isoformat()}])
    assert reclaim(dry_run=False, observations=observations)["reclaimed"] == 0
    now += rc.GRACE_SECONDS + 1
    monkeypatch.setattr(rc, "_project_sessions", lambda source: [])
    assert reclaim(dry_run=False, observations=observations)["reclaimed"] == 0  # eligibility restarts after activity
    now += rc.GRACE_SECONDS + 1
    assert reclaim(dry_run=False, observations=observations)["reclaimed"] == 1
    assert not path.exists()


def test_automatic_setting_and_persisted_grace(repository, tmp_path, monkeypatch):
    path = checkout(repository)
    monkeypatch.setattr(rc.settings, "data_dir", tmp_path / "state")
    monkeypatch.setattr(rc.settings, "worktree_auto_reclaim_enabled", False)
    asyncio.run(rc.automatic_pass())
    state = Path(rc.settings.effective_data_dir) / "checkout-reclaim.json"
    assert not state.exists() and path.exists()
    monkeypatch.setattr(rc.settings, "worktree_auto_reclaim_enabled", True)
    asyncio.run(rc.automatic_pass())
    assert state.exists() and path.exists()
    now = time.time() + rc.GRACE_SECONDS + 1
    monkeypatch.setattr(rc.time, "time", lambda: now)
    asyncio.run(rc.automatic_pass())
    assert not path.exists()


def test_registry_producing_path_and_foreign_worker_exclusion(mock_db, tmp_path, monkeypatch):
    from app.core.config import settings
    from app.db import checkouts, database, projects_v2
    from tests.git_repo_helpers import init_repo

    hermetic_git_env(monkeypatch, tmp_path)
    repo = init_repo(tmp_path / "real")
    project = projects_v2.upsert_project("local:reclaim-test", name="Reclaim test")
    checkouts.upsert_checkout(project["id"], settings.effective_instance_id, str(repo))
    path = repo / ".fluid" / "worktrees" / "task"
    git(repo, "worktree", "add", "-q", "-b", "task", str(path))
    tip = git(path, "rev-parse", "HEAD")
    row = checkouts.upsert_checkout(project["id"], settings.effective_instance_id, str(path), branch="task")
    foreign = checkouts.upsert_checkout(project["id"], "foreign-worker", str(tmp_path / "foreign"), branch="other")
    session = database.create_session("reclaim-session", None, project_v2_id=project["id"],
                                      worktree_branch="task", is_active=True, derive_provider_from_profile=False)
    result = asyncio.run(rc.reclaim(project["id"], dry_run=False))
    assert result["reclaimed"] == 0 and path.exists()  # sidebar-active producer state
    database.set_session_active(session["id"], False)
    from app.db import turns
    from app.db.base import get_db

    turns.record_adhoc_turn(session["id"], "test", outcome="completed")
    old = datetime.fromtimestamp(time.time() - 2 * rc.GRACE_SECONDS, timezone.utc).isoformat()
    with get_db() as conn:
        conn.cursor().execute("UPDATE sessions SET updated_at = %s WHERE id = %s", (old, session["id"]))
        conn.cursor().execute("UPDATE turns SET ended_at = %s WHERE session_id = %s", (old, session["id"]))
        conn.cursor().execute("UPDATE checkouts SET created_at = %s WHERE id = %s", (old, row["id"]))
    age_directory(path)
    result = asyncio.run(rc.reclaim(project["id"], dry_run=False))
    assert result["reclaimed"] == 1 and not path.exists()
    updated = checkouts.get_checkout_by_path(str(path), project_id=project["id"])
    assert updated["status"] == "archived" and updated["head_sha"] == tip
    assert checkouts.get_checkout_by_path(foreign["path"], project_id=project["id"], runner_id="foreign-worker")["status"] == "active"
    assert row["id"] == updated["id"]

@pytest.mark.parametrize("kind", ["default", "mismatch", "link-root", "ambiguous"])
def test_special_placements_are_never_removed(repository, tmp_path, kind):
    repo, source = repository
    path = checkout(repository)
    if kind == "default":
        git(repo, "checkout", "--detach")
        git(path, "checkout", "main")
        source["registered"][0]["branch"] = "main"
    elif kind == "mismatch":
        git(path, "checkout", "-b", "switched")
    elif kind == "link-root":
        moved = tmp_path / "moved"
        git(repo, "worktree", "move", str(path), str(moved))
        if os.name == "nt":
            subprocess.run(["cmd", "/c", "mklink", "/J", str(path), str(moved)], check=True, capture_output=True)
        else:
            path.symlink_to(moved, target_is_directory=True)
    else:
        source["registered"].append({**source["registered"][0], "branch": "different"})
    assert reclaim(dry_run=False)["reclaimed"] == 0
    assert path.exists()


def test_grace_resets_when_new_work_appears(repository, monkeypatch):
    path = checkout(repository)
    observations = {}
    now = time.time()
    monkeypatch.setattr(rc.time, "time", lambda: now)
    assert reclaim(dry_run=False, observations=observations)["reclaimed"] == 0
    now += rc.GRACE_SECONDS + 1
    commit(path, "new-work", "not integrated")
    assert reclaim(dry_run=False, observations=observations)["reclaimed"] == 0
    assert not observations
    git(repository[0], "merge", "--ff-only", "feature")
    assert reclaim(dry_run=False, observations=observations)["reclaimed"] == 0
    assert path.exists()


def test_automatic_schedule_is_delayed_and_low_frequency(monkeypatch):
    delays = []
    passes = []

    async def sleep(seconds):
        delays.append(seconds)
        if len(delays) == 2:
            raise asyncio.CancelledError

    async def one_pass():
        passes.append(True)

    monkeypatch.setattr(rc.asyncio, "sleep", sleep)
    monkeypatch.setattr(rc, "automatic_pass", one_pass)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(rc.periodic_reclaim())
    assert passes == [True] and delays == [60, 2 * 3600]


def test_setting_defaults_on_and_supports_opt_out(monkeypatch):
    from app.core.config import Settings

    assert Settings().worktree_auto_reclaim_enabled is True
    monkeypatch.setenv("WORKTREE_AUTO_RECLAIM_ENABLED", "false")
    assert Settings().worktree_auto_reclaim_enabled is False


def test_missing_legacy_and_canonical_aliases_reconciled(mock_db, tmp_path, monkeypatch):
    from app.core.worktree_manager import worktree_manager
    from app.db import checkouts, database, projects_v2
    from tests.git_repo_helpers import init_repo

    hermetic_git_env(monkeypatch, tmp_path)
    repo = init_repo(tmp_path / "legacy")
    database.create_project("legacy-project", "Legacy", None, str(repo))
    record = worktree_manager._ensure_repository_record("legacy-project", str(repo))
    project = projects_v2.upsert_project("local:legacy-reclaim", name="Canonical alias")
    checkouts.upsert_checkout(project["id"], rc.settings.effective_instance_id, str(repo))
    path = repo / ".fluid" / "worktrees" / "gone"
    git(repo, "worktree", "add", "-q", "-b", "gone", str(path))
    legacy = database.create_worktree("legacy-wt", record["id"], "gone", str(path))
    checkouts.upsert_checkout(project["id"], rc.settings.effective_instance_id, str(path), branch="gone")
    # Make the directory genuinely absent without deleting any outside fixture.
    moved = tmp_path / "only-fixture-files"
    path.rename(moved)
    result = asyncio.run(rc.reclaim(project["id"], dry_run=False))
    assert result["reclaimed"] == 1 and result["items"][0]["decision"] == "forget"
    assert database.get_worktree(legacy["id"])["status"] == "completed"
    assert checkouts.get_checkout_by_path(str(path), project_id=project["id"])["status"] == "archived"
    assert moved.exists()  # metadata cleanup never follows another directory
    assert asyncio.run(rc.reclaim(project["id"]))["total"] == 0


def test_real_run_lease_protects_inactive_session(mock_db, tmp_path, monkeypatch):
    from app.db import checkouts, database, projects_v2, runs
    from tests.git_repo_helpers import init_repo

    hermetic_git_env(monkeypatch, tmp_path)
    repo = init_repo(tmp_path / "lease")
    project = projects_v2.upsert_project("local:lease-reclaim", name="Lease")
    checkouts.upsert_checkout(project["id"], rc.settings.effective_instance_id, str(repo))
    path = repo / ".fluid" / "worktrees" / "running"
    git(repo, "worktree", "add", "-q", "-b", "running", str(path))
    checkouts.upsert_checkout(project["id"], rc.settings.effective_instance_id, str(path), branch="running")
    database.create_session("running-session", None, project_v2_id=project["id"],
                            worktree_branch="running", is_active=False, derive_provider_from_profile=False)
    assert runs.claim_run("running-session", rc.settings.effective_instance_id, 300)
    result = asyncio.run(rc.reclaim(project["id"], dry_run=False))
    assert result["reclaimed"] == 0 and "active or running" in result["items"][0]["reason"]
    assert path.exists()


def test_remote_read_failure_is_not_treated_as_no_pr(repository, monkeypatch):
    path = checkout(repository)
    original = rc.wr._git

    def unreadable(repo, *args, **kwargs):
        return None if args == ("remote",) else original(repo, *args, **kwargs)

    monkeypatch.setattr(rc.wr, "_git", unreadable)
    with pytest.raises(ValueError, match="remote configuration unavailable"):
        reclaim(dry_run=False)
    assert path.exists()


def test_missing_directory_ref_read_error_does_not_forget_registry(repository, monkeypatch, tmp_path):
    path = checkout(repository)
    path.rename(tmp_path / "moved-fixture")
    original = rc.git_service._run_git

    def unreadable(repo, args, **kwargs):
        if args[:3] == ["show-ref", "--verify", "--quiet"]:
            return SimpleNamespace(returncode=128)
        return original(repo, args, **kwargs)

    monkeypatch.setattr(rc.git_service, "_run_git", unreadable)
    result = reclaim(dry_run=False)
    assert result["reclaimed"] == 0 and result["items"][0]["error"] is True
    rc._archive.assert_not_called()
    assert git(repository[0], "rev-parse", "feature")


def test_ref_moved_during_removal_is_not_deleted(repository, monkeypatch):
    repo, _ = repository
    checkout(repository)
    original = rc.git_service.remove_worktree
    late_head = git(repo, "commit-tree", "HEAD^{tree}", "-p", "HEAD", "-m", "late work")

    def changed(main, path, force):
        result = original(main, path, force)
        git(repo, "update-ref", "refs/heads/feature", late_head)
        return result

    monkeypatch.setattr(rc.git_service, "remove_worktree", changed)
    result = reclaim(dry_run=False)
    assert result["reclaimed"] == 0 and result["items"][0]["error"] is True
    assert git(repo, "rev-parse", "feature") == late_head
    rc._archive.assert_not_called()
