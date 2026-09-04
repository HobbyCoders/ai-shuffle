"""Tests for app/core/checkout_manager.py — per-runner lazy materialization of
project_v2 checkouts (v2 core, Phase 3).

Uses REAL tmp git repos (no git mocking) so the deterministic-path + real
``git worktree add`` behavior is exercised end to end, plus the pg_shim SQLite DB
for the ``checkouts`` rows (matching tests/db conventions). The workspace root is
redirected to ``tmp_path`` via the config runtime override so the deterministic
materialization lands under the test's temp dir.
"""

import re
import subprocess
from pathlib import Path

import pytest

from app.core import checkout_manager
from app.db import checkouts as checkouts_db
from app.db import projects_v2

RUNNER = "runnerA"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def test_db():
    from tests.pg_shim import new_sqlite_connection
    conn = new_sqlite_connection()
    conn.execute("PRAGMA foreign_keys = OFF")
    yield conn
    conn.close()


@pytest.fixture
def mock_db(test_db):
    from tests.pg_shim import route_db_to
    with route_db_to(test_db):
        yield test_db


@pytest.fixture
def workspace_root(tmp_path, monkeypatch):
    """Redirect the runner's workspace root (hence checkout root) to tmp_path."""
    from app.core import config
    root = tmp_path / "workspace"
    root.mkdir()
    monkeypatch.setitem(config._runtime_overrides, "workspace_dir", root)
    return root


def _git(cwd, *args):
    subprocess.run(
        ["git", *args], cwd=str(cwd), check=True,
        capture_output=True, text=True,
    )


def _make_base_repo(base_dir) -> Path:
    """A real git repo with one commit on the default branch — the base checkout."""
    repo = Path(base_dir) / "base_repo"
    repo.mkdir(parents=True)
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@t.test")
    _git(repo, "config", "user.name", "T")
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "init")
    return repo


@pytest.fixture
def project(mock_db):
    return projects_v2.upsert_project("git:repo", name="My Cool Repo")


@pytest.fixture
def base_checkout(project, tmp_path):
    """A materialized base checkout: a real repo + its checkouts row (branch NULL)."""
    repo = _make_base_repo(tmp_path)
    checkouts_db.upsert_checkout(project["id"], RUNNER, str(repo))
    return repo


# ---------------------------------------------------------------------------
# Base checkout resolution
# ---------------------------------------------------------------------------

def test_base_returns_path_when_materialized(project, base_checkout, workspace_root):
    path = checkout_manager.ensure_checkout(project, runner_id=RUNNER)
    assert path == str(base_checkout)


def test_base_not_materialized_returns_none(project, workspace_root):
    # No checkouts row at all → not on this device → None (never clones).
    assert checkout_manager.ensure_checkout(project, runner_id=RUNNER) is None


def test_base_missing_dir_returns_none_no_recreation(project, mock_db, tmp_path, workspace_root):
    # Row exists but the on-disk dir is gone → honest None, and NOTHING recreated.
    gone = tmp_path / "never_existed"
    checkouts_db.upsert_checkout(project["id"], RUNNER, str(gone))
    assert checkout_manager.ensure_checkout(project, runner_id=RUNNER) is None
    assert not gone.exists()


# ---------------------------------------------------------------------------
# Branch checkout materialization
# ---------------------------------------------------------------------------

def test_branch_lazy_create_deterministic_path(project, base_checkout, workspace_root):
    path = checkout_manager.ensure_checkout(
        project, branch="feature-x", runner_id=RUNNER, create_new_branch=True
    )
    assert path is not None
    p = Path(path)
    assert p.is_dir()
    assert (p / ".git").exists()

    # Deterministic path: {base_checkout}/.fluid/worktrees/feature-x — nested
    # inside the project's own clone, NOT an md5-hash dir.
    assert ".fluid" in p.parts and "worktrees" in p.parts
    assert p.name == "feature-x"
    # The old scheme suffixed a 4-hex md5 (e.g. "feature-x-a7b3"); assert absent.
    assert not re.search(r"-[0-9a-f]{4}$", p.name)
    # Lives under the base checkout dir.
    assert p.parents[2] == Path(base_checkout)


def test_branch_idempotent_same_path_no_duplicate_rows(project, base_checkout, workspace_root):
    first = checkout_manager.ensure_checkout(
        project, branch="feat/login", runner_id=RUNNER, create_new_branch=True
    )
    second = checkout_manager.ensure_checkout(
        project, branch="feat/login", runner_id=RUNNER, create_new_branch=True
    )
    assert first is not None and first == second
    # base + one branch checkout = exactly two rows; the re-call adds none.
    rows = checkouts_db.list_checkouts(project["id"])
    assert len(rows) == 2
    branch_rows = [r for r in rows if r.get("branch") == "feat/login"]
    assert len(branch_rows) == 1
    assert branch_rows[0]["path"] == first


def test_branch_requires_base_checkout(project, workspace_root):
    # No base checkout materialized → a branch cannot be materialized either.
    assert checkout_manager.ensure_checkout(project, branch="feature-x", runner_id=RUNNER) is None


def test_branch_sanitizes_slashes_in_dir_name(project, base_checkout, workspace_root):
    path = checkout_manager.ensure_checkout(
        project, branch="feat/deep/name", runner_id=RUNNER, create_new_branch=True
    )
    assert path is not None
    # Slashes are sanitized to dashes in the final path component.
    assert Path(path).name == "feat-deep-name"


def test_existing_dir_not_a_worktree_returns_none(project, base_checkout, workspace_root):
    # Pre-create the deterministic dest as a plain dir (no .git) → refuse, don't destroy.
    dest = checkout_manager._branch_checkout_path(str(base_checkout), "feature-y")
    dest.mkdir(parents=True)
    (dest / "sentinel.txt").write_text("keep me\n", encoding="utf-8")

    assert checkout_manager.ensure_checkout(project, branch="feature-y", runner_id=RUNNER) is None
    # The sentinel survives — nothing was torn down.
    assert (dest / "sentinel.txt").exists()


def test_per_runner_divergence_same_branch(project, base_checkout, tmp_path, workspace_root, monkeypatch):
    """A second install resolves the SAME branch to its OWN path — divergence comes
    from each install's own configured workspace root (home dirs differ per machine),
    with an independent per-runner base checkout + checkouts row."""
    path_a = checkout_manager.ensure_checkout(
        project, branch="shared", runner_id=RUNNER, create_new_branch=True
    )
    assert path_a is not None

    # Install B: a different workspace root + its own base checkout clone.
    from app.core import config
    root_b = tmp_path / "workspace_b"
    root_b.mkdir()
    monkeypatch.setitem(config._runtime_overrides, "workspace_dir", root_b)
    repo_b = _make_base_repo(tmp_path / "b")
    checkouts_db.upsert_checkout(project["id"], "runnerB", str(repo_b))

    path_b = checkout_manager.ensure_checkout(
        project, branch="shared", runner_id="runnerB", create_new_branch=True
    )
    assert path_b is not None
    assert path_b != path_a
    # B's checkout nests under ITS OWN base clone (repo_b), not A's.
    assert str(repo_b) in path_b


# ---------------------------------------------------------------------------
# Branch-plan semantics (R5.2): existing-local vs origin-recovery vs
# not-materializable vs intentional new branch.
# ---------------------------------------------------------------------------

def _current_branch(repo) -> str:
    r = subprocess.run(
        ["git", "symbolic-ref", "--short", "HEAD"], cwd=str(repo),
        check=True, capture_output=True, text=True,
    )
    return r.stdout.strip()


def _make_repo_with_origin(tmp_root):
    """A ``base_repo`` clone carrying an ``origin`` remote (a bare repo), plus a
    ``seed`` clone used to push branches the base_repo has NOT fetched yet."""
    remote = Path(tmp_root) / "remote.git"
    _git(tmp_root, "init", "--bare", str(remote))

    seed = Path(tmp_root) / "seed"
    _git(tmp_root, "clone", str(remote), str(seed))
    _git(seed, "config", "user.email", "t@t.test")
    _git(seed, "config", "user.name", "T")
    (seed / "README.md").write_text("hello\n", encoding="utf-8")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-m", "init")
    _git(seed, "push", "origin", "HEAD")

    base = Path(tmp_root) / "base_repo"
    _git(tmp_root, "clone", str(remote), str(base))
    _git(base, "config", "user.email", "t@t.test")
    _git(base, "config", "user.name", "T")
    return remote, seed, base


def test_existing_local_branch_checked_out_without_fetch(
    project, base_checkout, workspace_root, monkeypatch
):
    # A branch already present locally is checked out as-is — no origin recovery,
    # no fetch (create_new_branch left False).
    _git(base_checkout, "branch", "existing-local")

    from app.core.git_service import git_service
    fetches = []
    orig_fetch = git_service.fetch
    monkeypatch.setattr(
        git_service, "fetch",
        lambda wd, remote="origin": (fetches.append(wd), orig_fetch(wd, remote))[1],
    )

    path = checkout_manager.ensure_checkout(
        project, branch="existing-local", runner_id=RUNNER
    )
    assert path is not None and (Path(path) / ".git").exists()
    assert fetches == []


def test_branch_recovered_from_origin_fetches(
    project, tmp_path, workspace_root, monkeypatch
):
    # A branch that lives ONLY on origin is recovered: the plan fetches and creates
    # the checkout from the pushed tip (never a fabricated empty branch).
    remote, seed, base = _make_repo_with_origin(tmp_path)
    checkouts_db.upsert_checkout(project["id"], RUNNER, str(base))

    _git(seed, "checkout", "-b", "pushed-feature")
    (seed / "feat.txt").write_text("x\n", encoding="utf-8")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-m", "feat")
    _git(seed, "push", "origin", "pushed-feature")

    from app.core import checkout_manager as cm
    from app.core.git_service import git_service
    fetches = []
    orig_fetch = git_service.fetch
    monkeypatch.setattr(
        git_service, "fetch",
        lambda wd, remote="origin": (fetches.append(wd), orig_fetch(wd, remote))[1],
    )

    path = cm.ensure_checkout(project, branch="pushed-feature", runner_id=RUNNER)
    assert path is not None
    assert Path(path).is_dir() and (Path(path) / ".git").exists()
    # Materialized from the PUSHED tip — the origin commit's file is present.
    assert (Path(path) / "feat.txt").exists()
    # A fetch was performed during recovery.
    assert fetches


def test_branch_missing_everywhere_returns_none(project, tmp_path, workspace_root):
    # Not local, not on origin, not an intentional new branch → honest None
    # (the pre-fix bug silently fabricated an empty branch off the base HEAD).
    remote, seed, base = _make_repo_with_origin(tmp_path)
    checkouts_db.upsert_checkout(project["id"], RUNNER, str(base))
    assert checkout_manager.ensure_checkout(
        project, branch="ghost-branch", runner_id=RUNNER
    ) is None


def test_deleted_branch_restored_from_recorded_head_sha(
    project, base_checkout, workspace_root
):
    """After cleanup deletes the local branch, ensure_checkout restores it from
    the checkout row's head_sha and rematerializes the worktree."""
    default = _current_branch(base_checkout)
    _git(base_checkout, "checkout", "-b", "feat/resume")
    (base_checkout / "resume.txt").write_text("from-tip\n", encoding="utf-8")
    _git(base_checkout, "add", "-A")
    _git(base_checkout, "commit", "-m", "resume me")
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(base_checkout),
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    _git(base_checkout, "checkout", default)
    dest_hint = checkout_manager._branch_checkout_path(str(base_checkout), "feat/resume")
    checkouts_db.upsert_checkout(
        project["id"], RUNNER, str(dest_hint),
        branch="feat/resume", head_sha=sha, status="archived",
    )
    _git(base_checkout, "branch", "-D", "feat/resume")

    path = checkout_manager.ensure_checkout(
        project, branch="feat/resume", runner_id=RUNNER
    )
    assert path is not None
    assert Path(path).is_dir() and (Path(path) / ".git").exists()
    assert (Path(path) / "resume.txt").read_text(encoding="utf-8") == "from-tip\n"


def test_deleted_branch_restored_from_pull_ref(project, tmp_path, workspace_root):
    """GitHub keeps pull/N/head after deleting the branch; fetch that refspec
    restores the local branch so the worktree can rematerialize."""
    remote, seed, base = _make_repo_with_origin(tmp_path)
    checkouts_db.upsert_checkout(project["id"], RUNNER, str(base))

    _git(seed, "checkout", "-b", "feat/pr-head")
    (seed / "pr.txt").write_text("pr-tip\n", encoding="utf-8")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-m", "pr head")
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(seed),
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    _git(seed, "push", "origin", "feat/pr-head")
    # Simulate GitHub's surviving PR ref after branch delete.
    _git(remote, "update-ref", "refs/pull/42/head", sha)
    _git(seed, "push", "origin", ":feat/pr-head")

    from app.db.sessions import latest_pr_number_for_branch
    from unittest.mock import patch

    with patch(
        "app.db.sessions.latest_pr_number_for_branch", return_value=42
    ) as lookup:
        path = checkout_manager.ensure_checkout(
            project, branch="feat/pr-head", runner_id=RUNNER
        )

    assert path is not None
    assert (Path(path) / "pr.txt").read_text(encoding="utf-8") == "pr-tip\n"
    lookup.assert_called()


def test_create_new_branch_honors_base_and_skips_recovery(
    project, base_checkout, workspace_root, monkeypatch
):
    # create_new_branch=True mints the branch from base_branch WITHOUT consulting
    # the origin-recovery plan at all.
    from app.core import checkout_manager as cm

    called = {"hit": False}

    def _boom(git, main_dir, branch_name):  # pragma: no cover - must never run
        called["hit"] = True
        raise AssertionError("origin recovery must not run for create_new_branch")

    monkeypatch.setattr(cm, "resolve_rebound_branch_plan", _boom)

    default = _current_branch(base_checkout)
    path = cm.ensure_checkout(
        project,
        branch="brand-new",
        runner_id=RUNNER,
        base_branch=default,
        create_new_branch=True,
    )
    assert path is not None
    assert Path(path).is_dir() and (Path(path) / ".git").exists()
    assert called["hit"] is False


# ---------------------------------------------------------------------------
# Lazy Step-E mirror: adopt an EXISTING post-collapse legacy worktree dir
# (un-upgraded install / not-yet-cut-over WS worktree_id intake) as the branch
# checkout, instead of failing on "branch already checked out".
# ---------------------------------------------------------------------------

def _seed_legacy_project(pid, name, path, instance_id=None, link_group_id=None):
    from app.db.base import get_db
    with get_db() as conn:
        conn.cursor().execute(
            "INSERT INTO projects (id, name, path, instance_id, link_group_id) "
            "VALUES (%s, %s, %s, %s, %s)",
            (pid, name, path, instance_id, link_group_id),
        )


def _seed_git_repository(repo_id, project_id):
    from app.db.base import get_db
    with get_db() as conn:
        conn.cursor().execute(
            "INSERT INTO git_repositories (id, project_id) VALUES (%s, %s)",
            (repo_id, project_id),
        )


def _seed_worktree(wt_id, repository_id, branch_name, worktree_path, status="active"):
    from app.db.base import get_db
    with get_db() as conn:
        conn.cursor().execute(
            "INSERT INTO worktrees "
            "(id, repository_id, branch_name, worktree_path, status) "
            "VALUES (%s, %s, %s, %s, %s)",
            (wt_id, repository_id, branch_name, worktree_path, status),
        )


def _add_legacy_worktree(base_repo, dest, branch):
    """A REAL ``git worktree add`` at an md5-style legacy path — the physical dir
    the old worktree_manager materialized, distinct from the deterministic path."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    _git(base_repo, "worktree", "add", str(dest), "-b", branch)
    return dest


def test_adopts_legacy_worktree_dir_as_branch_checkout(
    project, base_checkout, mock_db, tmp_path, workspace_root
):
    # A branch checked out in a post-collapse legacy worktree dir, WITH its
    # legacy projects/git_repositories/worktrees rows present but NO checkouts
    # row → ensure_checkout adopts the EXISTING legacy dir (Step-E mirror),
    # upserts the checkout row at that path, and does NOT create the
    # deterministic dir (which git would refuse: "branch already checked out").
    branch = "legacy-feat"
    legacy_dir = _add_legacy_worktree(
        base_checkout, tmp_path / "legacy_wt" / f"{branch}-a1b2", branch
    )

    # Legacy rows: origin = link_group_id must equal the project_v2 origin
    # ("git:repo"), joined worktrees -> git_repositories -> projects.
    _seed_legacy_project("lp1", "My Cool Repo", str(base_checkout),
                         link_group_id="git:repo")
    _seed_git_repository("gr1", "lp1")
    _seed_worktree("wt1", "gr1", branch, str(legacy_dir))

    path = checkout_manager.ensure_checkout(project, branch=branch, runner_id=RUNNER)

    assert path == str(legacy_dir)
    # The checkout row was upserted at the LEGACY path.
    rows = checkouts_db.list_checkouts(project["id"])
    branch_rows = [r for r in rows if r.get("branch") == branch]
    assert len(branch_rows) == 1
    assert branch_rows[0]["path"] == str(legacy_dir)
    # The deterministic dir was NEVER created.
    assert not checkout_manager._branch_checkout_path(str(base_checkout), branch).exists()


def test_adopted_legacy_worktree_second_call_uses_row_fast_path(
    project, base_checkout, mock_db, tmp_path, workspace_root
):
    branch = "legacy-feat2"
    legacy_dir = _add_legacy_worktree(
        base_checkout, tmp_path / "legacy_wt" / f"{branch}-c3d4", branch
    )
    _seed_legacy_project("lp1", "My Cool Repo", str(base_checkout),
                         link_group_id="git:repo")
    _seed_git_repository("gr1", "lp1")
    _seed_worktree("wt1", "gr1", branch, str(legacy_dir))

    first = checkout_manager.ensure_checkout(project, branch=branch, runner_id=RUNNER)
    second = checkout_manager.ensure_checkout(project, branch=branch, runner_id=RUNNER)
    assert first == str(legacy_dir)
    assert second == first
    # Still exactly one branch checkout row (fast path added none).
    rows = checkouts_db.list_checkouts(project["id"])
    branch_rows = [r for r in rows if r.get("branch") == branch]
    assert len(branch_rows) == 1


def test_no_legacy_row_branch_checked_out_elsewhere_returns_none(
    project, base_checkout, mock_db, tmp_path, workspace_root
):
    # The bug's shape WITHOUT the legacy worktrees row: the branch is checked out
    # in an untracked dir, so git refuses `git worktree add` at the deterministic
    # path and there is no row to adopt → honest None (unchanged behavior).
    branch = "orphan-feat"
    _add_legacy_worktree(base_checkout, tmp_path / "untracked_wt" / branch, branch)
    # No legacy projects/git_repositories/worktrees rows seeded.
    assert checkout_manager.ensure_checkout(
        project, branch=branch, runner_id=RUNNER
    ) is None


# ---------------------------------------------------------------------------
# _resolve_clone_url: recover a cloneable remote from legacy projects ->
# git_repositories.remote_url, validated by auto_link_group_id == origin.
# Inert PR-1 plumbing (#607) — no callers yet; wired in PR-2.
# ---------------------------------------------------------------------------

def _seed_git_repository_with_remote(repo_id, project_id, remote_url):
    from app.db.base import get_db
    with get_db() as conn:
        conn.cursor().execute(
            "INSERT INTO git_repositories (id, project_id, remote_url) "
            "VALUES (%s, %s, %s)",
            (repo_id, project_id, remote_url),
        )


def test_resolve_clone_url_matching_legacy_repo_returns_remote(mock_db):
    from app.core.project_links import auto_link_group_id

    remote_url = "https://github.com/o/r.git"
    origin = auto_link_group_id(remote_url)  # the git:… token the collapse assigns
    _seed_legacy_project("lp1", "R", "/some/legacy/path", link_group_id=origin)
    _seed_git_repository_with_remote("gr1", "lp1", remote_url)

    assert checkout_manager._resolve_clone_url(origin) == remote_url


def test_resolve_clone_url_no_legacy_row_returns_none(mock_db):
    from app.core.project_links import auto_link_group_id

    # A well-formed git:… origin with no matching legacy git_repositories row.
    origin = auto_link_group_id("https://github.com/o/absent.git")
    assert checkout_manager._resolve_clone_url(origin) is None


def test_resolve_clone_url_non_matching_remote_returns_none(mock_db):
    from app.core.project_links import auto_link_group_id

    # A legacy repo exists, but its remote hashes to a DIFFERENT origin than asked.
    other_remote = "https://github.com/o/other.git"
    _seed_legacy_project("lp1", "Other", "/some/legacy/path",
                         link_group_id=auto_link_group_id(other_remote))
    _seed_git_repository_with_remote("gr1", "lp1", other_remote)

    asked_origin = auto_link_group_id("https://github.com/o/wanted.git")
    assert asked_origin != auto_link_group_id(other_remote)
    assert checkout_manager._resolve_clone_url(asked_origin) is None


@pytest.mark.parametrize("origin", ["local:lp1", "manual:group-7", "", None])
def test_resolve_clone_url_excluded_origins_return_none_without_db(origin, monkeypatch):
    # local:/manual:/empty origins hard-fail up front with NO DB lookup — prove it
    # by making any get_db access explode, then asserting we still get None.
    def _boom(*_a, **_k):  # pragma: no cover - must never be reached
        raise AssertionError("no DB lookup expected for excluded origin")

    monkeypatch.setattr(checkout_manager, "get_db", _boom)
    assert checkout_manager._resolve_clone_url(origin) is None


# ---------------------------------------------------------------------------
# _seed_base_checkout: lazy self-seed of a MISSING base checkout (#607 PR-2),
# gated behind AI_SHUFFLE_SELF_SEED. git operations are MOCKED — no real clone.
# ---------------------------------------------------------------------------

@pytest.fixture
def seedable(mock_db):
    """A project_v2 whose origin resolves (via legacy data) to a cloneable URL,
    so ``_seed_base_checkout`` has a real URL to hand to the (mocked) clone."""
    from app.core.project_links import auto_link_group_id

    remote_url = "https://github.com/o/seedme.git"
    origin = auto_link_group_id(remote_url)
    proj = projects_v2.upsert_project(origin, name="Seed Me")
    _seed_legacy_project("lp-seed", "Seed Me", "/legacy/seedme", link_group_id=origin)
    _seed_git_repository_with_remote("gr-seed", "lp-seed", remote_url)
    return proj, remote_url


def _mock_clone(monkeypatch):
    """Patch ``git_service.clone_repo`` to a recording fake that fabricates a minimal
    ``.git`` in the (already-created) temp target so the atomic rename succeeds."""
    from app.core.git_service import git_service

    calls = []

    def _fake(working_dir, clone_url, timeout=120):
        calls.append({"working_dir": working_dir, "clone_url": clone_url, "timeout": timeout})
        (Path(working_dir) / ".git").mkdir()
        (Path(working_dir) / "README.md").write_text("seeded\n", encoding="utf-8")
        return True, None

    monkeypatch.setattr(git_service, "clone_repo", _fake)
    return calls


def test_seed_happy_path_clones_renames_and_upserts(seedable, workspace_root, monkeypatch):
    proj, remote_url = seedable
    calls = _mock_clone(monkeypatch)

    path = checkout_manager.ensure_checkout(proj, runner_id=RUNNER)

    # Cloned exactly once, with the RESOLVED remote URL.
    assert len(calls) == 1
    assert calls[0]["clone_url"] == remote_url

    # Materialized at the reserved {slug}/__base__ path via atomic rename.
    assert path is not None
    p = Path(path)
    assert p.name == checkout_manager._BASE_DIR_NAME
    assert p.parent.name == checkout_manager._project_slug(proj)
    assert (p / ".git").exists()

    # The clone landed at the final base path, not a leftover temp sibling.
    slug_dir = checkout_manager._checkout_root() / checkout_manager._project_slug(proj)
    leftovers = [d for d in slug_dir.iterdir() if d.name != checkout_manager._BASE_DIR_NAME]
    assert leftovers == []

    # A base (branch-NULL) checkout row was recorded at that path.
    rows = checkouts_db.list_checkouts(proj["id"])
    base_rows = [r for r in rows if not r.get("branch")]
    assert len(base_rows) == 1
    assert base_rows[0]["path"] == path

    # Idempotent: a second call reuses the seeded base (no second clone).
    again = checkout_manager.ensure_checkout(proj, runner_id=RUNNER)
    assert again == path
    assert len(calls) == 1


def test_seed_flag_off_no_clone(seedable, workspace_root, monkeypatch):
    proj, _ = seedable
    monkeypatch.setattr(checkout_manager.settings, "self_seed_checkouts", False)
    calls = _mock_clone(monkeypatch)

    assert checkout_manager.ensure_checkout(proj, runner_id=RUNNER) is None
    assert calls == []
    assert checkouts_db.list_checkouts(proj["id"]) == []


def test_seed_resolver_none_no_clone_and_warns(project, workspace_root, monkeypatch, caplog):
    # The default ``project`` fixture's origin ("git:repo") has NO matching legacy
    # git_repositories row → _resolve_clone_url returns None → no clone, a warning,
    # and honest None (fall-through to existing missing-checkout behavior).
    calls = _mock_clone(monkeypatch)

    with caplog.at_level("WARNING"):
        assert checkout_manager.ensure_checkout(project, runner_id=RUNNER) is None

    assert calls == []
    assert checkouts_db.list_checkouts(project["id"]) == []
    assert any(
        "no cloneable remote URL resolved" in r.getMessage() for r in caplog.records
    )


def test_seed_local_origin_no_clone(mock_db, workspace_root, monkeypatch):
    # A ``local:`` origin is hard-excluded by _resolve_clone_url (ruling 5) → None,
    # so the seed never clones even though the flag is ON.
    proj = projects_v2.upsert_project("local:lp-x", name="Local Only")
    calls = _mock_clone(monkeypatch)

    assert checkout_manager.ensure_checkout(proj, runner_id=RUNNER) is None
    assert calls == []
    assert checkouts_db.list_checkouts(proj["id"]) == []


def _seed_project_with_settings(settings):
    """A seedable project_v2 (resolvable clone URL via legacy data) carrying the
    given ``settings`` dict, so the per-project opt-out can be exercised."""
    from app.core.project_links import auto_link_group_id

    remote_url = "https://github.com/o/optout.git"
    origin = auto_link_group_id(remote_url)
    proj = projects_v2.upsert_project(origin, name="Opt Out", settings=settings)
    _seed_legacy_project("lp-opt", "Opt Out", "/legacy/opt", link_group_id=origin)
    _seed_git_repository_with_remote("gr-opt", "lp-opt", remote_url)
    return proj


def test_seed_per_project_opt_out_no_clone(mock_db, workspace_root, monkeypatch, caplog):
    # projects_v2.settings.self_seed=false disables self-seed for THIS project even
    # though the global flag is ON and the clone URL resolves (#607 ruling 2).
    proj = _seed_project_with_settings({"self_seed": False})
    calls = _mock_clone(monkeypatch)

    with caplog.at_level("INFO"):
        assert checkout_manager.ensure_checkout(proj, runner_id=RUNNER) is None

    assert calls == []
    assert checkouts_db.list_checkouts(proj["id"]) == []
    assert any("self-seed disabled for project" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("settings", [{}, {"self_seed": True}, {"other": 1}])
def test_seed_per_project_default_on_still_clones(settings, mock_db, workspace_root, monkeypatch):
    # A missing / truthy self_seed keeps the default-ON behavior — only an explicit
    # False opts out.
    proj = _seed_project_with_settings(settings)
    calls = _mock_clone(monkeypatch)

    path = checkout_manager.ensure_checkout(proj, runner_id=RUNNER)
    assert path is not None
    assert len(calls) == 1
    assert Path(path).name == checkout_manager._BASE_DIR_NAME


def test_seed_clone_timeout_cleans_temp_and_no_upsert(seedable, workspace_root, monkeypatch):
    # A private repo without creds (or a too-large repo) makes the clone hit the
    # 120s bound: clone_repo returns (False, "Clone timed out …") — a bounded-time
    # failure, never a hang. The seed cleans up and records nothing (#607).
    proj, _ = seedable
    from app.core.git_service import git_service

    calls = []

    def _timeout(working_dir, clone_url, timeout=120):
        calls.append(timeout)
        return False, f"Clone timed out after {timeout}s — the repository may be too large"

    monkeypatch.setattr(git_service, "clone_repo", _timeout)

    assert checkout_manager.ensure_checkout(proj, runner_id=RUNNER) is None
    assert calls == [120]  # attempted once, with the bounded timeout

    slug_dir = checkout_manager._checkout_root() / checkout_manager._project_slug(proj)
    if slug_dir.exists():
        assert list(slug_dir.iterdir()) == []
    assert not (slug_dir / checkout_manager._BASE_DIR_NAME).exists()
    assert checkouts_db.list_checkouts(proj["id"]) == []


def test_seed_clone_failure_cleans_temp_and_no_upsert(seedable, workspace_root, monkeypatch):
    proj, _ = seedable
    from app.core.git_service import git_service

    calls = []

    def _fail(working_dir, clone_url, timeout=120):
        calls.append(working_dir)
        # Leave the temp dir on disk as clone_repo would on a partial/failed clone.
        return False, "fatal: could not read Username (terminal prompts disabled)"

    monkeypatch.setattr(git_service, "clone_repo", _fail)

    assert checkout_manager.ensure_checkout(proj, runner_id=RUNNER) is None

    # Clone was attempted, but the temp dir was cleaned up — no partial checkout,
    # and the base path never came into existence.
    assert len(calls) == 1
    slug_dir = checkout_manager._checkout_root() / checkout_manager._project_slug(proj)
    if slug_dir.exists():
        assert list(slug_dir.iterdir()) == []
    base_path = slug_dir / checkout_manager._BASE_DIR_NAME
    assert not base_path.exists()

    # Nothing recorded.
    assert checkouts_db.list_checkouts(proj["id"]) == []


def test_seed_rename_failure_cleans_temp_and_no_upsert(seedable, workspace_root, monkeypatch):
    proj, _ = seedable
    calls = _mock_clone(monkeypatch)

    # Force the atomic rename to fail after a "successful" clone.
    def _boom_rename(src, dst):
        raise OSError("simulated rename failure")

    monkeypatch.setattr(checkout_manager.os, "rename", _boom_rename)

    assert checkout_manager.ensure_checkout(proj, runner_id=RUNNER) is None
    assert len(calls) == 1

    slug_dir = checkout_manager._checkout_root() / checkout_manager._project_slug(proj)
    if slug_dir.exists():
        assert list(slug_dir.iterdir()) == []
    assert not (slug_dir / checkout_manager._BASE_DIR_NAME).exists()
    assert checkouts_db.list_checkouts(proj["id"]) == []


def test_seed_adopts_existing_valid_base_dir_without_clone(seedable, workspace_root, monkeypatch):
    # A valid checkout already sits at the reserved __base__ path but carries no
    # checkouts row → adopt it (upsert), never clone over it.
    proj, _ = seedable
    calls = _mock_clone(monkeypatch)

    slug_dir = checkout_manager._checkout_root() / checkout_manager._project_slug(proj)
    base_path = slug_dir / checkout_manager._BASE_DIR_NAME
    existing = _make_base_repo(slug_dir)  # a real repo dir
    existing.rename(base_path)

    path = checkout_manager.ensure_checkout(proj, runner_id=RUNNER)
    assert path == str(base_path)
    assert calls == []  # adopted, not cloned
    rows = checkouts_db.list_checkouts(proj["id"])
    assert [r["path"] for r in rows if not r.get("branch")] == [str(base_path)]


def test_seed_refuses_non_worktree_at_base_path(seedable, workspace_root, monkeypatch):
    # A plain (non-git) dir occupies __base__ → refuse to seed over it (never
    # destructive), and record nothing.
    proj, _ = seedable
    calls = _mock_clone(monkeypatch)

    slug_dir = checkout_manager._checkout_root() / checkout_manager._project_slug(proj)
    base_path = slug_dir / checkout_manager._BASE_DIR_NAME
    base_path.mkdir(parents=True)
    (base_path / "sentinel.txt").write_text("keep me\n", encoding="utf-8")

    assert checkout_manager.ensure_checkout(proj, runner_id=RUNNER) is None
    assert calls == []
    assert (base_path / "sentinel.txt").exists()  # untouched
    assert checkouts_db.list_checkouts(proj["id"]) == []


def test_branch_checkout_path_collision_guard_for_reserved_name(project, base_checkout, workspace_root):
    # A logical branch literally named "__base__" must NOT resolve onto the base dir.
    p = checkout_manager._branch_checkout_path(str(base_checkout), checkout_manager._BASE_DIR_NAME)
    assert p.name != checkout_manager._BASE_DIR_NAME
    assert p.name == f"{checkout_manager._BASE_DIR_NAME}-branch"


# ---------------------------------------------------------------------------
# _resolve_checkout_path: workspace-relative legacy paths (#654) are resolved
# against the runner's workspace_dir at READ time; absolute paths pass through.
# ---------------------------------------------------------------------------

def _insert_relative_checkout_row(project_id, runner_id, rel_path, branch=None):
    """Insert a checkouts row with a RELATIVE path directly (bypassing the
    upsert_checkout guard) to reproduce a #654 legacy row."""
    from app.db.base import get_db
    cid = checkouts_db.checkout_id_for(project_id, runner_id, rel_path)
    with get_db() as conn:
        conn.cursor().execute(
            "INSERT INTO checkouts (id, project_id, runner_id, path, branch, status) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (cid, project_id, runner_id, rel_path, branch, "active"),
        )


def test_resolve_checkout_path_absolute_unchanged():
    # Absolute paths are returned verbatim — POSIX and Windows styles alike, on
    # any host OS (the primary is Windows, the worker Linux, sharing one DB).
    assert checkout_manager._resolve_checkout_path("/workspace/x") == "/workspace/x"
    assert checkout_manager._resolve_checkout_path(r"C:\x") == r"C:\x"


def test_resolve_checkout_path_relative_joins_workspace(workspace_root):
    # A workspace-relative path is joined under effective_workspace_dir (#654).
    resolved = checkout_manager._resolve_checkout_path("ai-shuffle")
    assert resolved == str(workspace_root / "ai-shuffle")


def test_base_resolves_workspace_relative_legacy_path(project, mock_db, workspace_root):
    # A #654 legacy base row whose path is workspace-RELATIVE resolves under the
    # runner's workspace_dir and materializes normally.
    (workspace_root / "relrepo").mkdir()
    _insert_relative_checkout_row(project["id"], RUNNER, "relrepo")

    path = checkout_manager.ensure_checkout(project, runner_id=RUNNER)
    assert path == str(workspace_root / "relrepo")
    # Read-only normalization — the DB row is NOT rewritten to the absolute path.
    rows = checkouts_db.list_checkouts(project["id"])
    assert [r["path"] for r in rows] == ["relrepo"]


# ---------------------------------------------------------------------------
# Adopting worktrees the app did not create
# ---------------------------------------------------------------------------

def _hand_made_worktree(base_repo, branch, path):
    """What an agent's own `git worktree add` in Bash leaves behind: a real
    worktree on disk that no app table knows about."""
    _git(base_repo, "worktree", "add", "-b", branch, str(path))
    return path


def test_a_worktree_the_app_did_not_create_gets_a_row(
    project, base_checkout, workspace_root, tmp_path
):
    """The field symptom: a session's branch was set while its worktree id and
    path were null, because nothing had ever registered the worktree."""
    hand_made = _hand_made_worktree(
        base_checkout, "agent/side-quest", tmp_path / "handmade"
    )
    assert not any(
        r["path"] == str(hand_made) for r in checkouts_db.list_checkouts(project["id"])
    )

    checkout_manager.ensure_checkout(project, runner_id=RUNNER)

    rows = {r["path"]: r for r in checkouts_db.list_checkouts(project["id"])}
    assert str(hand_made) in rows
    assert rows[str(hand_made)]["branch"] == "agent/side-quest"
    assert rows[str(hand_made)]["status"] == "active"


def test_adoption_is_idempotent(project, base_checkout, workspace_root, tmp_path):
    _hand_made_worktree(base_checkout, "agent/again", tmp_path / "handmade")

    checkout_manager.ensure_checkout(project, runner_id=RUNNER)
    before = len(checkouts_db.list_checkouts(project["id"]))
    adopted = checkout_manager.register_unregistered_worktrees(
        project["id"], RUNNER, str(base_checkout)
    )

    assert adopted == []
    assert len(checkouts_db.list_checkouts(project["id"])) == before


def test_a_detached_worktree_is_not_adopted(
    project, base_checkout, workspace_root, tmp_path
):
    """Resolution is branch-keyed, so a detached worktree has nothing to key on."""
    detached = tmp_path / "detached"
    _git(base_checkout, "worktree", "add", "--detach", str(detached))

    checkout_manager.ensure_checkout(project, runner_id=RUNNER)

    paths = [r["path"] for r in checkouts_db.list_checkouts(project["id"])]
    assert str(detached) not in paths


def test_adoption_failure_never_fails_the_checkout(
    project, base_checkout, workspace_root, monkeypatch
):
    from app.core.git_service import git_service

    monkeypatch.setattr(
        git_service, "list_worktrees",
        lambda d: (_ for _ in ()).throw(RuntimeError("git exploded")),
    )

    assert checkout_manager.ensure_checkout(project, runner_id=RUNNER) == str(
        base_checkout
    )
