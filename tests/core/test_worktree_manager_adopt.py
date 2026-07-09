"""
Tests for the adopt-if-already-checked-out behavior in WorktreeManager.

Root cause this guards against: ``recreate_worktree`` / ``create_worktree``
compute a DETERMINISTIC on-disk path and call ``git worktree add`` for a branch
that may already be checked out in a live worktree on this device. ``git`` only
lets one worktree hold a branch, so the add fails with "already used by worktree
at X". This is especially common in a shared-Postgres cluster: another device
flips the DB record ORPHANED/DELETED because the dir is missing on *that* disk,
but the worktree is healthy locally. The fix makes recreate/create idempotent by
adopting the existing on-disk worktree instead of re-adding it.

All git / DB access is patched at the boundary used by worktree_manager so no
real git or Postgres is needed. The filesystem ``.git`` existence check is
exercised against real ``tmp_path`` dirs (matching test_worktree_manager_validity
style).
"""

from pathlib import Path

import pytest
from unittest.mock import MagicMock, patch

import app.core.worktree_manager as wm_module
from app.core.worktree_manager import WorktreeManager, WorktreeError
from app.db.base import WorktreeStatus


BRANCH = "feat/android-mobile-shell-app"


def _make_live_worktree_dir(tmp_path: Path) -> Path:
    """Create an on-disk worktree dir (folder + .git file) git would report."""
    wt_dir = tmp_path / "worktrees" / "feat-android-mobile-shell-app-bb7d"
    wt_dir.mkdir(parents=True)
    (wt_dir / ".git").write_text("gitdir: /repo/.git/worktrees/wt-bb7d\n")
    return wt_dir


@pytest.fixture
def manager(monkeypatch):
    """A WorktreeManager with a fully mocked git_service + patched DB module."""
    mgr = WorktreeManager()
    mgr.git_service = MagicMock(name="git_service")

    # database is module-level imported into worktree_manager.
    db = MagicMock(name="database")
    monkeypatch.setattr(wm_module, "database", db)

    # get_project_working_dir resolves a project dict to its main repo dir.
    monkeypatch.setattr(
        wm_module, "get_project_working_dir", lambda p: p["path"] if isinstance(p, dict) else p
    )
    return mgr, db


# ---------------------------------------------------------------------------
# recreate_worktree: adopt instead of add when branch already checked out
# ---------------------------------------------------------------------------

def test_recreate_adopts_existing_when_branch_checked_out(manager, tmp_path):
    """
    The exact cluster scenario: DB record is ORPHANED (status not active) but the
    branch is live in git on this device. recreate must ADOPT the on-disk
    worktree, NOT call add_worktree, and flip the record to ACTIVE.
    """
    mgr, db = manager
    wt_dir = _make_live_worktree_dir(tmp_path)

    db.get_worktree.return_value = {
        "id": "wt-1",
        "status": WorktreeStatus.ORPHANED,
        "branch_name": BRANCH,
        "repository_id": "repo-1",
    }
    db.get_git_repository.return_value = {"id": "repo-1", "project_id": "proj-1", "default_branch": "main"}
    db.get_project.return_value = {"id": "proj-1", "path": str(tmp_path)}
    db.update_worktree.return_value = {
        "id": "wt-1",
        "status": WorktreeStatus.ACTIVE,
        "branch_name": BRANCH,
        "worktree_path": str(wt_dir),
    }

    # Branch still exists locally; no new branch needed.
    mgr.git_service.list_branches.return_value = [{"name": BRANCH, "is_remote": False}]
    # git reports the branch is already live in a (non-main) worktree on disk.
    mgr.git_service.list_worktrees.return_value = [
        {"path": str(tmp_path), "branch": None, "is_main": True, "is_bare": False, "is_detached": False},
        {"path": str(wt_dir), "branch": BRANCH, "is_main": False, "is_bare": False, "is_detached": False},
    ]

    result = mgr.recreate_worktree("wt-1")

    # ADOPTED — add_worktree must NOT be called.
    mgr.git_service.add_worktree.assert_not_called()
    # Record flipped to ACTIVE with the adopted path. Non-rebind path →
    # repository_id passes through as None (no cross-device rebind occurred).
    db.update_worktree.assert_called_once_with(
        "wt-1",
        status=WorktreeStatus.ACTIVE,
        worktree_path=str(wt_dir),
        branch_name=BRANCH,
        repository_id=None,
    )
    assert result["status"] == WorktreeStatus.ACTIVE
    assert result["worktree_path"] == str(wt_dir)


def test_recreate_skips_adopt_when_dir_missing_on_disk(manager, tmp_path):
    """
    If git lists the branch but the directory is gone on disk (or lacks .git),
    it is NOT adoptable — recreate falls through to add_worktree.
    """
    mgr, db = manager
    missing_dir = tmp_path / "worktrees" / "gone-bb7d"  # never created

    db.get_worktree.return_value = {
        "id": "wt-1",
        "status": WorktreeStatus.DELETED,
        "branch_name": BRANCH,
        "repository_id": "repo-1",
    }
    db.get_git_repository.return_value = {"id": "repo-1", "project_id": "proj-1", "default_branch": "main"}
    db.get_project.return_value = {"id": "proj-1", "path": str(tmp_path)}
    db.update_worktree.return_value = {"id": "wt-1", "status": WorktreeStatus.ACTIVE}

    mgr.git_service.list_branches.return_value = [{"name": BRANCH, "is_remote": False}]
    mgr.git_service.list_worktrees.return_value = [
        {"path": str(missing_dir), "branch": BRANCH, "is_main": False, "is_bare": False, "is_detached": False},
    ]
    mgr.git_service.add_worktree.return_value = (True, None)

    # The deterministic path that add_worktree will target must exist after add;
    # patch _get_worktree_path to a real dir so the post-add is_dir() check passes.
    target = tmp_path / "worktrees" / "feat-android-mobile-shell-app-bb7d"
    target.mkdir(parents=True)
    monkeypatch_path(mgr, target)

    mgr.recreate_worktree("wt-1")

    # Not adoptable → add_worktree IS called.
    mgr.git_service.add_worktree.assert_called_once()


def monkeypatch_path(mgr, target: Path):
    """Helper: force _get_worktree_path to return ``target``."""
    mgr._get_worktree_path = lambda project_path, branch: target


# ---------------------------------------------------------------------------
# recreate_worktree: add_worktree fallback adopts on "already used by worktree"
# ---------------------------------------------------------------------------

def test_recreate_add_failure_already_used_falls_back_to_adopt(manager, tmp_path):
    """
    Belt-and-suspenders: if the first list_worktrees scan misses (returns no
    match) but add_worktree fails with "already used by worktree at X", recreate
    re-scans and adopts rather than raising.
    """
    mgr, db = manager
    wt_dir = _make_live_worktree_dir(tmp_path)

    db.get_worktree.return_value = {
        "id": "wt-1",
        "status": WorktreeStatus.ORPHANED,
        "branch_name": BRANCH,
        "repository_id": "repo-1",
    }
    db.get_git_repository.return_value = {"id": "repo-1", "project_id": "proj-1", "default_branch": "main"}
    db.get_project.return_value = {"id": "proj-1", "path": str(tmp_path)}
    db.update_worktree.return_value = {
        "id": "wt-1",
        "status": WorktreeStatus.ACTIVE,
        "worktree_path": str(wt_dir),
    }

    mgr.git_service.list_branches.return_value = [{"name": BRANCH, "is_remote": False}]
    # First scan: empty (race). Second scan (after add fails): finds the worktree.
    mgr.git_service.list_worktrees.side_effect = [
        [],  # pre-add scan misses
        [  # post-failure re-scan finds it
            {"path": str(wt_dir), "branch": BRANCH, "is_main": False, "is_bare": False, "is_detached": False},
        ],
    ]
    monkeypatch_path(mgr, tmp_path / "worktrees" / "feat-android-mobile-shell-app-bb7d")
    mgr.git_service.add_worktree.return_value = (
        False,
        f"fatal: '{BRANCH}' is already used by worktree at '{wt_dir}'",
    )

    result = mgr.recreate_worktree("wt-1")

    mgr.git_service.add_worktree.assert_called_once()
    db.update_worktree.assert_called_once_with(
        "wt-1",
        status=WorktreeStatus.ACTIVE,
        worktree_path=str(wt_dir),
        branch_name=BRANCH,
        repository_id=None,
    )
    assert result["status"] == WorktreeStatus.ACTIVE


def test_recreate_add_failure_other_error_still_raises(manager, tmp_path):
    """A non-"already used" add failure with no adoptable worktree still raises."""
    mgr, db = manager

    db.get_worktree.return_value = {
        "id": "wt-1",
        "status": WorktreeStatus.DELETED,
        "branch_name": BRANCH,
        "repository_id": "repo-1",
    }
    db.get_git_repository.return_value = {"id": "repo-1", "project_id": "proj-1", "default_branch": "main"}
    db.get_project.return_value = {"id": "proj-1", "path": str(tmp_path)}

    mgr.git_service.list_branches.return_value = [{"name": BRANCH, "is_remote": False}]
    mgr.git_service.list_worktrees.return_value = []  # nothing adoptable, ever
    monkeypatch_path(mgr, tmp_path / "worktrees" / "feat-android-mobile-shell-app-bb7d")
    mgr.git_service.add_worktree.return_value = (False, "fatal: some other git error")

    with pytest.raises(WorktreeError) as ei:
        mgr.recreate_worktree("wt-1")

    assert "Git failed to recreate worktree" in str(ei.value)
    db.update_worktree.assert_not_called()


def test_recreate_active_valid_fast_path_unchanged(manager, tmp_path):
    """The ACTIVE-and-valid fast path returns the record without touching git."""
    mgr, db = manager
    wt_dir = _make_live_worktree_dir(tmp_path)

    wt = {
        "id": "wt-1",
        "status": WorktreeStatus.ACTIVE,
        "branch_name": BRANCH,
        "worktree_path": str(wt_dir),
        "repository_id": "repo-1",
    }
    db.get_worktree.return_value = wt
    # is_worktree_valid resolves the path; patch the resolver to the real dir.
    import app.core.worktree_manager as _wm
    _orig = _wm.get_worktree_working_dir_as_path
    _wm.get_worktree_working_dir_as_path = lambda w: Path(w["worktree_path"])
    try:
        result = mgr.recreate_worktree("wt-1")
    finally:
        _wm.get_worktree_working_dir_as_path = _orig

    assert result is wt
    mgr.git_service.list_worktrees.assert_not_called()
    mgr.git_service.add_worktree.assert_not_called()


# ---------------------------------------------------------------------------
# create_worktree: adopt instead of add when branch already checked out
# ---------------------------------------------------------------------------

def test_create_adopts_existing_when_branch_checked_out(manager, tmp_path):
    """
    create_worktree for an existing branch that is already checked out on disk
    must adopt that path (NOT call add_worktree) and write a record pointing at
    the adopted path.
    """
    mgr, db = manager
    wt_dir = _make_live_worktree_dir(tmp_path)

    db.get_project.return_value = {"id": "proj-1", "path": str(tmp_path)}
    db.get_active_worktrees_for_repository.return_value = []
    db.create_worktree.side_effect = lambda **kw: dict(kw)

    # _ensure_repository_record → repo dict.
    mgr._ensure_repository_record = lambda pid, ppath: {"id": "repo-1"}
    # Existing branch present locally (create_new_branch=False).
    mgr.git_service.list_branches.return_value = [{"name": BRANCH, "is_remote": False}]
    mgr.git_service.list_worktrees.return_value = [
        {"path": str(wt_dir), "branch": BRANCH, "is_main": False, "is_bare": False, "is_detached": False},
    ]

    result = mgr.create_worktree("proj-1", BRANCH, create_new_branch=False)

    mgr.git_service.add_worktree.assert_not_called()
    # Record created with the adopted path.
    _, kwargs = db.create_worktree.call_args
    assert kwargs["worktree_path"] == str(wt_dir)
    assert kwargs["branch_name"] == BRANCH
    assert result["worktree_path"] == str(wt_dir)


def test_create_new_branch_always_adds(manager, tmp_path):
    """create_new_branch=True can't already be checked out → always add_worktree."""
    mgr, db = manager

    db.get_project.return_value = {"id": "proj-1", "path": str(tmp_path)}
    db.get_active_worktrees_for_repository.return_value = []
    db.create_worktree.side_effect = lambda **kw: dict(kw)
    mgr._ensure_repository_record = lambda pid, ppath: {"id": "repo-1"}
    # New branch must NOT already exist.
    mgr.git_service.list_branches.return_value = [{"name": "main", "is_remote": False}]
    mgr.git_service.add_worktree.return_value = (True, None)
    # Deterministic path the record will store.
    target = tmp_path / "worktrees" / "new-branch-bb7d"
    monkeypatch_path(mgr, target)

    mgr.create_worktree("proj-1", "new-branch", create_new_branch=True, base_branch="main")

    mgr.git_service.add_worktree.assert_called_once()
    # The adopt scan should not even be consulted for a brand-new branch.
    mgr.git_service.list_worktrees.assert_not_called()


def test_create_add_failure_already_used_falls_back_to_adopt(manager, tmp_path):
    """create_worktree add failing with 'already used' re-scans and adopts."""
    mgr, db = manager
    wt_dir = _make_live_worktree_dir(tmp_path)

    db.get_project.return_value = {"id": "proj-1", "path": str(tmp_path)}
    db.get_active_worktrees_for_repository.return_value = []
    db.create_worktree.side_effect = lambda **kw: dict(kw)
    mgr._ensure_repository_record = lambda pid, ppath: {"id": "repo-1"}
    mgr.git_service.list_branches.return_value = [{"name": BRANCH, "is_remote": False}]
    # First scan misses, post-failure scan finds it.
    mgr.git_service.list_worktrees.side_effect = [
        [],
        [{"path": str(wt_dir), "branch": BRANCH, "is_main": False, "is_bare": False, "is_detached": False}],
    ]
    monkeypatch_path(mgr, tmp_path / "worktrees" / "feat-android-mobile-shell-app-bb7d")
    mgr.git_service.add_worktree.return_value = (
        False,
        f"fatal: '{BRANCH}' is already used by worktree at '{wt_dir}'",
    )

    result = mgr.create_worktree("proj-1", BRANCH, create_new_branch=False)

    mgr.git_service.add_worktree.assert_called_once()
    _, kwargs = db.create_worktree.call_args
    assert kwargs["worktree_path"] == str(wt_dir)
    assert result["worktree_path"] == str(wt_dir)


# ---------------------------------------------------------------------------
# recreate_worktree: cross-device rebind onto a local clone of the same repo
# ---------------------------------------------------------------------------

def _setup_rebind(mgr, db, tmp_path, *, local_project):
    """Stored project's checkout is missing; rebind resolves to ``local_project``.

    Wires the DB boundary and the ``resolve_runnable_local_project`` return so
    recreate_worktree takes the rebind path. Returns nothing — callers set up the
    branch/worktree git mocks for their specific scenario.
    """
    db.get_worktree.return_value = {
        "id": "wt-1",
        "status": WorktreeStatus.ORPHANED,
        "branch_name": BRANCH,
        "repository_id": "repo-foreign",
    }
    db.get_git_repository.return_value = {
        "id": "repo-foreign",
        "project_id": "proj-foreign",
        "default_branch": "main",
    }
    # Stored project lives on another device: path is missing here.
    db.get_project.return_value = {
        "id": "proj-foreign",
        "path": "/foreign/missing",
        "link_group_id": "git:abc",
    }
    db.get_sessions_for_worktree.return_value = [{"id": "ses-1"}]
    db.update_worktree.return_value = {
        "id": "wt-1",
        "status": WorktreeStatus.ACTIVE,
    }
    # This install's clone of the same repo.
    mgr._ensure_repository_record = lambda pid, ppath: {"id": "repo-local"}


def test_recreate_rebinds_to_link_group_sibling(manager, tmp_path):
    """(a) Stored path missing + local sibling → worktree created under the
    sibling, repository_id re-pointed, and ONLY the resumed session moved via
    set_session_project."""
    mgr, db = manager
    local_dir = tmp_path / "local-clone"
    local_dir.mkdir()
    local_project = {"id": "proj-local", "path": str(local_dir), "link_group_id": "git:abc"}
    _setup_rebind(mgr, db, tmp_path, local_project=local_project)

    # Branch exists locally in the clone → reuse (create_new=False).
    mgr.git_service.list_branches.return_value = [{"name": BRANCH, "is_remote": False}]
    mgr.git_service.list_worktrees.return_value = []  # nothing to adopt
    mgr.git_service.add_worktree.return_value = (True, None)
    target = tmp_path / "local-clone" / "worktrees" / "wt-bb7d"
    target.mkdir(parents=True)
    monkeypatch_path(mgr, target)

    with patch(
        "app.core.project_links.resolve_runnable_local_project",
        return_value=({"id": "proj-foreign"}, local_project),
    ):
        mgr.recreate_worktree("wt-1", rebind_session_id="ses-1")

    # add_worktree ran against the LOCAL clone with the existing branch.
    _, add_kwargs = mgr.git_service.add_worktree.call_args
    assert add_kwargs["main_dir"] == str(local_dir)
    assert add_kwargs["new_branch"] is False
    # repository_id re-pointed to the local repo on the final write.
    _, upd_kwargs = db.update_worktree.call_args
    assert upd_kwargs["repository_id"] == "repo-local"
    # ONLY the resumed session was moved onto the local project row.
    db.set_session_project.assert_called_once_with("ses-1", "proj-local")


def test_recreate_rebind_scopes_repoint_to_resumed_session(manager, tmp_path):
    """follow-up #2: a worktree can back several sessions (forks). Resuming ONE of
    them cross-device must move ONLY that session onto the local project row — the
    #493 code re-pointed EVERY session on the worktree via
    get_sessions_for_worktree, corrupting sibling forks' binding on their home
    device. Here two sessions share the worktree; only 'ses-1' is resumed."""
    mgr, db = manager
    local_dir = tmp_path / "local-clone"
    local_dir.mkdir()
    local_project = {"id": "proj-local", "path": str(local_dir), "link_group_id": "git:abc"}
    _setup_rebind(mgr, db, tmp_path, local_project=local_project)
    # The worktree backs two sessions — a fork edge. If the old broad repoint were
    # still in place this would move both.
    db.get_sessions_for_worktree.return_value = [{"id": "ses-1"}, {"id": "ses-2-fork"}]

    mgr.git_service.list_branches.return_value = [{"name": BRANCH, "is_remote": False}]
    mgr.git_service.list_worktrees.return_value = []
    mgr.git_service.add_worktree.return_value = (True, None)
    target = tmp_path / "local-clone" / "worktrees" / "wt-bb7d"
    target.mkdir(parents=True)
    monkeypatch_path(mgr, target)

    with patch(
        "app.core.project_links.resolve_runnable_local_project",
        return_value=({"id": "proj-foreign"}, local_project),
    ):
        mgr.recreate_worktree("wt-1", rebind_session_id="ses-1")

    # Exactly one repoint — the resumed session — never the sibling fork.
    db.set_session_project.assert_called_once_with("ses-1", "proj-local")
    repointed = {c.args[0] for c in db.set_session_project.call_args_list}
    assert "ses-2-fork" not in repointed


def test_recreate_rebind_no_session_repoints_nothing(manager, tmp_path):
    """follow-up #2: the manual reopen endpoint calls recreate_worktree without a
    session id. With no single session to scope to, we re-point NOTHING rather
    than clobber every session on the worktree."""
    mgr, db = manager
    local_dir = tmp_path / "local-clone"
    local_dir.mkdir()
    local_project = {"id": "proj-local", "path": str(local_dir), "link_group_id": "git:abc"}
    _setup_rebind(mgr, db, tmp_path, local_project=local_project)
    db.get_sessions_for_worktree.return_value = [{"id": "ses-1"}, {"id": "ses-2"}]

    mgr.git_service.list_branches.return_value = [{"name": BRANCH, "is_remote": False}]
    mgr.git_service.list_worktrees.return_value = []
    mgr.git_service.add_worktree.return_value = (True, None)
    target = tmp_path / "local-clone" / "worktrees" / "wt-bb7d"
    target.mkdir(parents=True)
    monkeypatch_path(mgr, target)

    with patch(
        "app.core.project_links.resolve_runnable_local_project",
        return_value=({"id": "proj-foreign"}, local_project),
    ):
        mgr.recreate_worktree("wt-1")  # no rebind_session_id

    db.set_session_project.assert_not_called()


def test_recreate_rebind_recovers_branch_from_origin(manager, tmp_path):
    """(b) Branch missing locally but origin/<branch> exists → add_worktree with
    new_branch=True, base_branch=<branch> (lands on the pushed tip)."""
    mgr, db = manager
    local_dir = tmp_path / "local-clone"
    local_dir.mkdir()
    local_project = {"id": "proj-local", "path": str(local_dir), "link_group_id": "git:abc"}
    _setup_rebind(mgr, db, tmp_path, local_project=local_project)

    def _branches(_dir, include_remote=False):
        if include_remote:
            return [
                {"name": "main", "is_remote": False},
                {"name": f"origin/{BRANCH}", "is_remote": True},
            ]
        return [{"name": "main", "is_remote": False}]

    mgr.git_service.list_branches.side_effect = _branches
    mgr.git_service.list_worktrees.return_value = []
    mgr.git_service.add_worktree.return_value = (True, None)
    target = tmp_path / "local-clone" / "worktrees" / "wt-bb7d"
    target.mkdir(parents=True)
    monkeypatch_path(mgr, target)

    with patch(
        "app.core.project_links.resolve_runnable_local_project",
        return_value=({"id": "proj-foreign"}, local_project),
    ):
        mgr.recreate_worktree("wt-1")

    mgr.git_service.fetch.assert_called_once()
    _, add_kwargs = mgr.git_service.add_worktree.call_args
    assert add_kwargs["new_branch"] is True
    assert add_kwargs["base_branch"] == BRANCH


def test_recreate_rebind_branch_missing_everywhere_raises(manager, tmp_path):
    """(c) Branch missing locally AND not on origin → WorktreeError with a
    push-instruction, and NO add_worktree with a default base."""
    mgr, db = manager
    local_dir = tmp_path / "local-clone"
    local_dir.mkdir()
    local_project = {"id": "proj-local", "path": str(local_dir), "link_group_id": "git:abc"}
    _setup_rebind(mgr, db, tmp_path, local_project=local_project)

    def _branches(_dir, include_remote=False):
        if include_remote:
            return [
                {"name": "main", "is_remote": False},
                {"name": "origin/main", "is_remote": True},
            ]
        return [{"name": "main", "is_remote": False}]

    mgr.git_service.list_branches.side_effect = _branches

    with patch(
        "app.core.project_links.resolve_runnable_local_project",
        return_value=({"id": "proj-foreign"}, local_project),
    ):
        with pytest.raises(WorktreeError) as ei:
            mgr.recreate_worktree("wt-1")

    assert "git push -u origin" in str(ei.value)
    mgr.git_service.add_worktree.assert_not_called()


def test_recreate_rebind_no_local_clone_raises_early(manager, tmp_path):
    """(d) Stored path missing + no local clone → early WorktreeError, git never
    touched (no branch listing, no add_worktree)."""
    mgr, db = manager
    _setup_rebind(mgr, db, tmp_path, local_project=None)

    with patch(
        "app.core.project_links.resolve_runnable_local_project",
        return_value=({"id": "proj-foreign", "path": "/foreign/missing"}, None),
    ):
        with pytest.raises(WorktreeError) as ei:
            mgr.recreate_worktree("wt-1")

    assert "no local clone" in str(ei.value)
    mgr.git_service.add_worktree.assert_not_called()
    mgr.git_service.list_branches.assert_not_called()


def test_create_add_failure_other_error_raises(manager, tmp_path):
    """A non-'already used' add failure (no adoptable wt) still raises in create."""
    mgr, db = manager

    db.get_project.return_value = {"id": "proj-1", "path": str(tmp_path)}
    db.get_active_worktrees_for_repository.return_value = []
    mgr._ensure_repository_record = lambda pid, ppath: {"id": "repo-1"}
    mgr.git_service.list_branches.return_value = [{"name": BRANCH, "is_remote": False}]
    mgr.git_service.list_worktrees.return_value = []
    monkeypatch_path(mgr, tmp_path / "worktrees" / "feat-android-mobile-shell-app-bb7d")
    mgr.git_service.add_worktree.return_value = (False, "fatal: disk full")

    with pytest.raises(WorktreeError) as ei:
        mgr.create_worktree("proj-1", BRANCH, create_new_branch=False)

    assert "Git failed to create worktree" in str(ei.value)
    db.create_worktree.assert_not_called()


# ---------------------------------------------------------------------------
# follow-up #1: the cross-device rebind fetch is primed at adopt time and cached,
# so ``resolve_rebound_branch_plan`` does not block the SEND hot path on a repeat
# ``git fetch``. (The blocking fetch was the reviewer's first follow-up.)
# ---------------------------------------------------------------------------

def test_resolve_rebound_branch_plan_reuses_primed_fetch(tmp_path):
    """A fetch primed at adopt time (note_repo_fetched) is reused: the plan does
    NOT fetch again when it must consult origin for a non-local branch."""
    from app.core.worktree_manager import (
        resolve_rebound_branch_plan,
        note_repo_fetched,
        reset_fetch_cache,
    )

    reset_fetch_cache()
    main_dir = str(tmp_path / "clone")
    git = MagicMock(name="git_service")

    def _branches(_dir, include_remote=False):
        if include_remote:
            return [
                {"name": "main", "is_remote": False},
                {"name": "origin/feat/x", "is_remote": True},
            ]
        # branch NOT local → would otherwise fetch
        return [{"name": "main", "is_remote": False}]

    git.list_branches.side_effect = _branches

    # Adopt-time primed the fetch for this dir.
    note_repo_fetched(main_dir)

    create_new, base = resolve_rebound_branch_plan(git, main_dir, "feat/x")

    assert (create_new, base) == (True, "feat/x")
    git.fetch.assert_not_called()  # hot path did not re-fetch


def test_resolve_rebound_branch_plan_fetches_when_not_primed(tmp_path):
    """Control: with no primed fetch, the plan DOES fetch once to freshen refs
    before deciding the branch is recoverable from origin."""
    from app.core.worktree_manager import resolve_rebound_branch_plan, reset_fetch_cache

    reset_fetch_cache()
    main_dir = str(tmp_path / "clone-2")
    git = MagicMock(name="git_service")

    def _branches(_dir, include_remote=False):
        if include_remote:
            return [
                {"name": "main", "is_remote": False},
                {"name": "origin/feat/y", "is_remote": True},
            ]
        return [{"name": "main", "is_remote": False}]

    git.list_branches.side_effect = _branches

    create_new, base = resolve_rebound_branch_plan(git, main_dir, "feat/y")

    assert (create_new, base) == (True, "feat/y")
    git.fetch.assert_called_once_with(main_dir)


def test_fetch_repo_cached_force_always_fetches_and_primes(tmp_path):
    """Adopt uses force=True: it always fetches (freshness for the advisory) and
    records the timestamp so a following send-path call skips."""
    from app.core.worktree_manager import (
        fetch_repo_cached,
        reset_fetch_cache,
    )

    reset_fetch_cache()
    main_dir = str(tmp_path / "clone-3")
    git = MagicMock(name="git_service")

    assert fetch_repo_cached(git, main_dir, force=True) is True
    # A subsequent non-forced call within the TTL is a cache hit (no fetch).
    assert fetch_repo_cached(git, main_dir) is False
    git.fetch.assert_called_once_with(main_dir)


# ---------------------------------------------------------------------------
# follow-up #1 (stale-cache spurious failure): when the plan decides on CACHED
# refs (a fetch was primed at adopt time) and the branch is absent, it forces one
# fresh fetch and re-checks before raising. This closes the verified failure —
# adopt primes the cache + warns "push it from the other device"; the user pushes
# and re-sends within the 60s TTL; without the retry the plan raises in the exact
# remediation flow it recommended.
# ---------------------------------------------------------------------------

def test_resolve_rebound_branch_plan_force_fetches_on_stale_cache_miss(tmp_path):
    """RED before the fix: with the cache primed and origin/<branch> not yet in the
    (stale) refs, the plan raised immediately. It must instead force one fetch and
    recover the freshly pushed branch."""
    from app.core.worktree_manager import (
        resolve_rebound_branch_plan,
        note_repo_fetched,
        reset_fetch_cache,
    )

    reset_fetch_cache()
    main_dir = str(tmp_path / "clone-stale")
    git = MagicMock(name="git_service")

    state = {"fetched": False}

    def _fetch(_dir):
        state["fetched"] = True

    git.fetch.side_effect = _fetch

    def _branches(_dir, include_remote=False):
        if include_remote:
            names = [{"name": "main", "is_remote": False}]
            # The pushed branch only becomes visible AFTER a real fetch — modelling
            # the stale-cache window where the ref hasn't been fetched in yet.
            if state["fetched"]:
                names.append({"name": "origin/feat/z", "is_remote": True})
            return names
        return [{"name": "main", "is_remote": False}]

    git.list_branches.side_effect = _branches

    # Adopt-time primed the cache → the first (non-forced) fetch is skipped, so the
    # decision runs on stale refs and misses the branch.
    note_repo_fetched(main_dir)

    create_new, base = resolve_rebound_branch_plan(git, main_dir, "feat/z")

    assert (create_new, base) == (True, "feat/z")
    # Exactly one fetch: the forced retry after the cached-refs miss.
    git.fetch.assert_called_once_with(main_dir)


def test_resolve_rebound_branch_plan_force_fetch_still_absent_raises(tmp_path):
    """The forced retry doesn't paper over a genuinely missing branch: when it is
    absent locally AND on origin even after a fresh fetch, the loud WorktreeError
    remains."""
    from app.core.worktree_manager import (
        resolve_rebound_branch_plan,
        note_repo_fetched,
        reset_fetch_cache,
    )

    reset_fetch_cache()
    main_dir = str(tmp_path / "clone-stale-2")
    git = MagicMock(name="git_service")

    def _branches(_dir, include_remote=False):
        # Neither local nor origin carries the branch, before OR after a fetch.
        if include_remote:
            return [{"name": "main", "is_remote": False}]
        return [{"name": "main", "is_remote": False}]

    git.list_branches.side_effect = _branches

    note_repo_fetched(main_dir)  # primes cache → first check runs on cached refs

    with pytest.raises(WorktreeError):
        resolve_rebound_branch_plan(git, main_dir, "feat/w")

    # The forced retry was still attempted before giving up.
    git.fetch.assert_called_once_with(main_dir)
