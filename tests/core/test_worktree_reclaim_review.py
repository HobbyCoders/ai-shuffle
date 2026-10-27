"""Regression probes from review: reclamation must never stop or erase new work."""
import asyncio
import json
import os
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock

import pytest

from tests.core.test_worktree_reclaim import age_directory, repository, checkout, commit, git, reclaim, rc  # noqa: F401


def merged(repository):
    repo, _ = repository
    path = checkout(repository)
    commit(path)
    git(repo, "merge", "--ff-only", "feature")
    return repo, path


def test_stale_plan_does_not_release_session_users(repository, monkeypatch):
    from app.api import git_operations

    _, path = merged(repository)
    original = rc._plan
    calls = 0

    def plan(*args, **kwargs):
        nonlocal calls
        row = original(*args, **kwargs)
        calls += 1
        if calls == 1:
            (path / "late.txt").write_text("new work")
        return row

    monkeypatch.setattr(rc, "_plan", plan)
    item = reclaim(dry_run=False)["items"][0]
    assert item["decision"] == "keep"
    git_operations._release_checkout_sessions.assert_not_awaited()
    assert (path / "late.txt").read_text() == "new work"


def test_git_refuses_file_written_after_final_status_check(repository, monkeypatch):
    _, path = merged(repository)
    original = rc.git_service.remove_worktree

    def remove(main, target, force=False):
        (path / "late.txt").write_text("written after status check")
        return original(main, target, force)

    monkeypatch.setattr(rc.git_service, "remove_worktree", remove)
    result = reclaim(dry_run=False)
    assert result["reclaimed"] == 0
    assert (path / "late.txt").read_text() == "written after status check"


def test_fresh_automatic_grace_check_precedes_release(repository, monkeypatch):
    from app.api import git_operations

    _, path = merged(repository)
    head = git(path, "rev-parse", "HEAD")
    observations = {rc.wr._path_key(str(path)): {"head": head, "since": time.time() - 2 * rc.GRACE_SECONDS}}
    monkeypatch.setattr(rc, "_old_activity", Mock(side_effect=[True, False]))
    assert reclaim(dry_run=False, observations=observations)["reclaimed"] == 0
    git_operations._release_checkout_sessions.assert_not_awaited()
    assert path.exists()

def test_liveness_rechecked_immediately_before_removal(repository, monkeypatch):
    _, path = merged(repository)
    sessions = []
    monkeypatch.setattr(rc, "_project_sessions", lambda source: sessions)
    monkeypatch.setattr(rc, "_stamp_head", lambda *args: sessions.append({"id": "late", "branch": "feature", "running": True}))
    assert reclaim(dry_run=False)["reclaimed"] == 0
    assert path.exists()


@pytest.mark.parametrize("kind", ["monitor", "task"])
def test_idle_release_never_stops_running_background_work(monkeypatch, kind):
    from app.api import git_operations
    from app.core import browser_runtime, git_activity, session_runtime
    from app.core.session_pool import get_session_pool
    from app.mcp import builtin_tools

    monkeypatch.setattr(git_activity, "_live_session_ids", lambda: set())
    monkeypatch.setattr(git_activity.task_registry, "has_active_background_work", lambda sid: kind == "task")
    monkeypatch.setattr(git_activity.task_registry, "has_running_monitor_shell", lambda sid: kind == "monitor")
    monkeypatch.setattr(session_runtime, "get_live_runtime", lambda sid: None)
    remove = AsyncMock()
    monkeypatch.setattr(get_session_pool(), "remove", remove)
    close = AsyncMock()
    stop = AsyncMock()
    monkeypatch.setattr(browser_runtime, "close_session", close)
    monkeypatch.setattr(builtin_tools, "stop_session_background_shells", stop)
    assert asyncio.run(git_operations._release_idle_session_cwd("busy")) is False
    remove.assert_not_awaited()
    close.assert_not_awaited()
    stop.assert_not_awaited()


def test_idle_release_does_not_force_stop_shells_even_when_idle(monkeypatch):
    from app.api import git_operations
    from app.core import browser_runtime, git_activity, session_runtime
    from app.core.session_pool import get_session_pool
    from app.mcp import builtin_tools

    monkeypatch.setattr(git_activity, "active_git_sessions", lambda rows: [])
    monkeypatch.setattr(session_runtime, "get_live_runtime", lambda sid: None)
    monkeypatch.setattr(get_session_pool(), "remove", AsyncMock())
    monkeypatch.setattr(browser_runtime, "close_session", AsyncMock())
    stop = AsyncMock()
    monkeypatch.setattr(builtin_tools, "stop_session_background_shells", stop)
    assert asyncio.run(git_operations._release_idle_session_cwd("idle")) is True
    get_session_pool().remove.assert_awaited_once_with("idle", preserve_session=True)
    stop.assert_not_awaited()


def test_github_round_trip_is_outside_repository_lock(repository, monkeypatch):
    from app.core.github_service import github_service

    repo, _ = repository
    merged(repository)
    git(repo, "remote", "add", "origin", "https://github.com/example/disposable.git")
    held = False

    @contextmanager
    def lock(repo):
        nonlocal held
        held = True
        try:
            yield
        finally:
            held = False

    def pulls(*args, **kwargs):
        assert not held, "network call while provisioning lock is held"
        return []

    monkeypatch.setattr(rc, "repo_worktree_lock", lock)
    monkeypatch.setattr(github_service, "list_pulls", pulls)
    assert reclaim(dry_run=False)["reclaimed"] == 1


@pytest.mark.parametrize("name", [".env", ".env.local", "local-output.txt", "dist/output.js",
                                  "outputs/result.txt", "unknown/data", ".pytest_cache_backup/data",
                                  "cache.pyc/data", "dist/.pytest_cache/data", "dist/__pycache__/x.pyc"])
def test_non_dependency_ignored_files_are_kept(repository, name):
    repo, _ = repository
    _, path = merged(repository)
    excludes = repo / ".git" / "info" / "exclude"
    excludes.write_text(excludes.read_text() + "\n" + name.split("/")[0] + "\n")
    valuable = path / name
    valuable.parent.mkdir(parents=True, exist_ok=True)
    valuable.write_text("local fixture output")
    preview = reclaim()["items"][0]
    assert preview["decision"] == "keep" and "ignored files" in preview["reason"]
    assert reclaim(dry_run=False)["reclaimed"] == 0
    assert valuable.read_text() == "local fixture output"


@pytest.mark.parametrize("name", [".pytest_cache/v/cache/nodeids", "__pycache__/module.pyc",
                                  "nested/__pycache__/module.pyc", "module.pyc", "nested/module.pyc",
                                  ".svelte-kit/generated/client.js", "frontend/.svelte-kit/output/client.js"])
def test_known_ignored_caches_do_not_block_reclaim(repository, name):
    repo, path = merged(repository)
    excludes = repo / ".git" / "info" / "exclude"
    excludes.write_text(excludes.read_text() + "\n.pytest_cache/\n__pycache__/\n*.pyc\n.svelte-kit/\n")
    cache = path / name
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text("disposable cache")
    assert reclaim()["items"][0]["decision"] == "remove"
    assert reclaim(dry_run=False)["reclaimed"] == 1
    assert not path.exists()


@pytest.mark.parametrize("kind", ["symlink", "junction"])
def test_symlinked_cache_is_kept_without_touching_target(repository, tmp_path, kind):
    if kind == "junction" and os.name != "nt":
        pytest.skip("directory junctions exist only on Windows")
    repo, path = merged(repository)
    target = tmp_path / "cache-target"
    target.mkdir()
    valuable = target / "valuable"
    valuable.write_text("keep")
    link = path / ".pytest_cache"
    if kind == "junction":
        # The directory link Windows checkouts carry (node_modules); it needs
        # no symlink privilege, so it runs on every Windows host.
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError:
            # WinError 1314: no symlink privilege (no Developer Mode or admin).
            pytest.skip("this Windows account cannot create symlinks; the junction case covers it")
    excludes = repo / ".git" / "info" / "exclude"
    excludes.write_text(excludes.read_text() + "\n.pytest_cache\n")
    assert reclaim()["items"][0]["decision"] == "keep"
    assert reclaim(dry_run=False)["reclaimed"] == 0
    assert link.is_junction() if kind == "junction" else link.is_symlink()
    assert valuable.read_text() == "keep"


def test_unignored_parent_with_unknown_ignored_content_is_kept(repository):
    repo, path = merged(repository)
    valuable = path / "untracked-folder" / "local-output.txt"
    valuable.parent.mkdir()
    valuable.write_text("keep")
    excludes = repo / ".git" / "info" / "exclude"
    excludes.write_text(excludes.read_text() + "\nlocal-output.txt\n")
    assert not git(path, "status", "--porcelain")
    assert reclaim()["items"][0]["decision"] == "keep"
    assert reclaim(dry_run=False)["reclaimed"] == 0
    assert valuable.read_text() == "keep"


def test_stamp_failure_aborts_before_removal(repository, monkeypatch):
    _, path = merged(repository)
    monkeypatch.setattr(rc, "_stamp_head", Mock(side_effect=RuntimeError("registry unavailable")))
    assert reclaim(dry_run=False)["reclaimed"] == 0
    assert path.exists()
    assert git(path, "rev-parse", "feature")


@pytest.mark.parametrize("state", [[], {"project_index": "bad"}, {"offsets": []},
                                    {"observations": {"bad": {}}}, {"observations": {"bad": {"since": float("nan")}}}])
def test_malformed_state_resets_conservative_grace(repository, tmp_path, monkeypatch, caplog, state):
    _, path = merged(repository)
    monkeypatch.setattr(rc.settings, "data_dir", tmp_path / "state")
    state_path = rc.Path(rc.settings.effective_data_dir) / "checkout-reclaim.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(json.dumps(state))
    asyncio.run(rc.automatic_pass())
    assert "resetting conservative grace" in caplog.text
    assert isinstance(json.loads(state_path.read_text()), dict) and path.exists()


def test_preview_never_starts_automatic_reclamation(repository, monkeypatch):
    monkeypatch.setenv("AI_SHUFFLE_PREVIEW", "1")
    sources = Mock(side_effect=AssertionError("preview must not inspect local registry"))
    monkeypatch.setattr(rc, "_local_sources", sources)
    asyncio.run(rc.automatic_pass())
    asyncio.run(rc.periodic_reclaim())
    sources.assert_not_called()


@pytest.mark.parametrize("young", ["both", "registry", "directory"])
def test_fresh_unbound_checkout_is_kept(mock_db, tmp_path, monkeypatch, young):
    import os

    from app.db import checkouts, projects_v2
    from app.db.base import get_db
    from tests.git_repo_helpers import hermetic_git_env, init_repo

    hermetic_git_env(monkeypatch, tmp_path)
    repo = init_repo(tmp_path / "unbound")
    project = projects_v2.upsert_project("local:unbound-reclaim", name="Unbound")
    checkouts.upsert_checkout(project["id"], rc.settings.effective_instance_id, str(repo))
    path = repo / ".fluid" / "worktrees" / "task"
    git(repo, "worktree", "add", "-q", "-b", "task", str(path))
    row = checkouts.upsert_checkout(project["id"], rc.settings.effective_instance_id, str(path), branch="task")
    old_time = time.time() - 2 * rc.GRACE_SECONDS
    old = datetime.fromtimestamp(old_time, timezone.utc).isoformat()
    if young == "registry":
        os.utime(path, (old_time, old_time))
    if young == "directory":
        with get_db() as conn:
            conn.cursor().execute("UPDATE checkouts SET created_at = %s WHERE id = %s", (old, row["id"]))
    preview = asyncio.run(rc.reclaim(project["id"]))
    assert preview["items"][0]["decision"] == "keep"
    assert "freshness guard" in preview["items"][0]["reason"]
    assert asyncio.run(rc.reclaim(project["id"], dry_run=False))["reclaimed"] == 0
    assert path.exists() and git(path, "rev-parse", "task")
    assert checkouts.get_checkout_by_path(str(path), project_id=project["id"])["status"] == "active"
    # Once both independent clocks are old, an unused checkout is reclaimable.
    with get_db() as conn:
        conn.cursor().execute("UPDATE checkouts SET created_at = %s WHERE id = %s", (old, row["id"]))
    os.utime(path, (old_time, old_time))
    assert asyncio.run(rc.reclaim(project["id"], dry_run=False))["reclaimed"] == 1
    assert not path.exists()


def test_first_dispatch_is_protected_until_finished_and_quiet(mock_db, tmp_path, monkeypatch):
    from app.db import checkouts, database, projects_v2, turns
    from app.db.base import get_db
    from tests.git_repo_helpers import hermetic_git_env, init_repo

    hermetic_git_env(monkeypatch, tmp_path)
    repo = init_repo(tmp_path / "dispatch")
    project = projects_v2.upsert_project("local:dispatch-reclaim", name="Dispatch")
    checkouts.upsert_checkout(project["id"], rc.settings.effective_instance_id, str(repo))
    path = repo / ".fluid" / "worktrees" / "task"
    git(repo, "worktree", "add", "-q", "-b", "task", str(path))
    checkouts.upsert_checkout(project["id"], rc.settings.effective_instance_id, str(path), branch="task")
    database.create_session("new-worker", None, project_v2_id=project["id"], worktree_branch="task",
                            is_active=False, is_worker=True, derive_provider_from_profile=False)
    first = asyncio.run(rc.reclaim(project["id"], dry_run=False))
    assert first["reclaimed"] == 0 and "no finished turn" in first["items"][0]["reason"]
    assert path.exists()
    turns.record_adhoc_turn("new-worker", "test", outcome="completed")
    second = asyncio.run(rc.reclaim(project["id"], dry_run=False))
    assert second["reclaimed"] == 0 and "freshness guard" in second["items"][0]["reason"]
    old = datetime.fromtimestamp(time.time() - 2 * rc.GRACE_SECONDS, timezone.utc).isoformat()
    with get_db() as conn:
        conn.cursor().execute("UPDATE sessions SET updated_at = %s WHERE id = %s", (old, "new-worker"))
        conn.cursor().execute("UPDATE turns SET ended_at = %s WHERE session_id = %s", (old, "new-worker"))
        conn.cursor().execute("UPDATE checkouts SET created_at = %s WHERE path = %s", (old, str(path)))
    age_directory(path)
    third = asyncio.run(rc.reclaim(project["id"], dry_run=False))
    assert third["reclaimed"] == 1 and third["items"][0]["reason"] == "no commits of its own"
    assert not path.exists()


def test_restoration_sha_survives_archive_failure(mock_db, tmp_path, monkeypatch):
    from app.db import checkouts, projects_v2
    from tests.git_repo_helpers import hermetic_git_env, init_repo

    hermetic_git_env(monkeypatch, tmp_path)
    repo = init_repo(tmp_path / "stamp")
    project = projects_v2.upsert_project("local:stamp-reclaim", name="Stamp")
    checkouts.upsert_checkout(project["id"], rc.settings.effective_instance_id, str(repo))
    path = repo / ".fluid" / "worktrees" / "feature"
    git(repo, "worktree", "add", "-q", "-b", "feature", str(path))
    commit(path)
    git(repo, "merge", "--ff-only", "feature")
    tip = git(path, "rev-parse", "HEAD")
    checkouts.upsert_checkout(project["id"], rc.settings.effective_instance_id, str(path), branch="feature")
    delete = rc._delete_branch
    observed = []

    def check_before_delete(*args, **kwargs):
        observed.append(checkouts.get_checkout_by_path(str(path), project_id=project["id"])["head_sha"])
        return delete(*args, **kwargs)

    monkeypatch.setattr(rc, "_delete_branch", check_before_delete)
    monkeypatch.setattr(rc, "_archive", Mock(side_effect=RuntimeError("archive failed")))
    result = asyncio.run(rc.reclaim(project["id"], dry_run=False))
    assert result["reclaimed"] == 0 and result["items"][0]["error"] is True
    assert observed == [tip] and not path.exists()
    row = checkouts.get_checkout_by_path(str(path), project_id=project["id"])
    assert row["head_sha"] == tip and row["status"] == "active"
    git(repo, "branch", "feature", row["head_sha"])
    assert git(repo, "rev-parse", "feature") == tip


def test_new_session_after_release_keeps_checkout(repository, monkeypatch):
    from app.api import git_operations

    _, path = merged(repository)
    sessions = []
    monkeypatch.setattr(rc, "_project_sessions", lambda source: sessions)

    async def released(*args, **kwargs):
        assert kwargs["idle_only"] is True
        sessions.append({"id": "new-run", "branch": "feature", "running": True})
        return True

    monkeypatch.setattr(git_operations, "_release_checkout_sessions", released)
    assert reclaim(dry_run=False)["reclaimed"] == 0
    assert path.exists()


def test_merged_work_and_unstarted_branch_have_distinct_reasons(repository):
    _, path = merged(repository)
    assert reclaim()["items"][0]["reason"] == "integration proven; safe to reclaim"
    checkout(repository, "empty")
    rows = {row["branch"]: row for row in reclaim()["items"]}
    assert rows["empty"]["reason"] == "no commits of its own"
    assert path.exists()


def test_activity_started_during_pr_lookup_prevents_release(repository, monkeypatch):
    from app.api import git_operations
    from app.core.github_service import github_service

    repo, path = merged(repository)
    git(repo, "remote", "add", "origin", "https://github.com/example/disposable.git")
    sessions = []
    monkeypatch.setattr(rc, "_project_sessions", lambda source: sessions)
    calls = 0

    def pulls(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:  # pass-start lookup succeeded; fresh lookup overlaps a new run
            sessions.append({"id": "new-run", "branch": "feature", "running": True})
        return []

    monkeypatch.setattr(github_service, "list_pulls", pulls)
    assert reclaim(dry_run=False)["reclaimed"] == 0
    git_operations._release_checkout_sessions.assert_not_awaited()
    assert path.exists()
