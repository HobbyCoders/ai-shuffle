"""Unit tests for app/db/checkouts.py — per-runner working-tree checkouts of a
project_v2 (v2 core, Phase 3)."""

import pytest

from app.db import checkouts
from app.db import projects_v2


@pytest.fixture
def project(mock_db):
    return projects_v2.upsert_project("git:repo", name="Repo")


def test_upsert_inserts_then_reads_back(project):
    c = checkouts.upsert_checkout(project["id"], "runnerA", "/work/repo")
    assert c["project_id"] == project["id"]
    assert c["runner_id"] == "runnerA"
    assert c["path"] == "/work/repo"
    assert c["branch"] is None
    assert c["status"] == "active"

    got = checkouts.get_checkout(project["id"], "runnerA")
    assert got is not None and got["id"] == c["id"]


def test_unique_project_runner_path_dedupes(project):
    a = checkouts.upsert_checkout(project["id"], "runnerA", "/work/repo")
    b = checkouts.upsert_checkout(
        project["id"], "runnerA", "/work/repo", head_sha="deadbeef"
    )
    # Same (project, runner, path) -> one row, updated in place.
    assert a["id"] == b["id"]
    assert b["head_sha"] == "deadbeef"
    assert len(checkouts.list_checkouts(project["id"])) == 1


def test_partial_upsert_does_not_clobber(project):
    checkouts.upsert_checkout(
        project["id"], "runnerA", "/work/repo", head_sha="sha1", dirty=True
    )
    # A later upsert that omits head_sha/dirty must not null them out.
    c = checkouts.upsert_checkout(project["id"], "runnerA", "/work/repo")
    assert c["head_sha"] == "sha1"
    assert c["dirty"] is True


def test_path_recovery_and_archive_are_runner_and_project_scoped(project, monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, 'instance_id', 'local')
    other = projects_v2.upsert_project('git:other', name='Other')
    foreign = checkouts.upsert_checkout(project['id'], 'foreign', '/work/shared', head_sha='foreign')
    local = checkouts.upsert_checkout(project['id'], 'local', '/work/shared', head_sha='local')
    assert checkouts.get_checkout_by_path('/work/shared')['id'] == local['id']
    unrelated = checkouts.upsert_checkout(other['id'], 'local', '/work/shared', head_sha='other')
    assert checkouts.get_checkout_by_path('/work/shared') is None
    assert checkouts.set_head_sha_for_path('/work/shared', 'ambiguous') is None
    assert checkouts.set_head_sha_for_path('/work/shared', 'updated', project_id=project['id'])['id'] == local['id']
    assert checkouts.archive_checkout_by_path(project['id'], '/work/shared')['id'] == local['id']
    assert checkouts.get_checkout(project['id'], 'foreign')['head_sha'] == foreign['head_sha']
    assert checkouts.get_checkout(project['id'], 'foreign')['status'] == 'active'
    assert checkouts.get_checkout(other['id'], 'local')['head_sha'] == unrelated['head_sha']


def test_per_runner_divergence(project):
    checkouts.upsert_checkout(project["id"], "runnerA", "/a/repo")
    checkouts.upsert_checkout(project["id"], "runnerB", "/b/repo")
    a = checkouts.get_checkout(project["id"], "runnerA")
    b = checkouts.get_checkout(project["id"], "runnerB")
    assert a["path"] == "/a/repo"
    assert b["path"] == "/b/repo"
    assert len(checkouts.list_checkouts(project["id"])) == 2


def test_base_vs_branch_get(project):
    base = checkouts.upsert_checkout(project["id"], "runnerA", "/work/base")
    feat = checkouts.upsert_checkout(
        project["id"], "runnerA", "/work/feat", branch="feature-x"
    )
    # branch=None resolves the base checkout (branch NULL), not the branch one.
    got_base = checkouts.get_checkout(project["id"], "runnerA")
    assert got_base["id"] == base["id"]
    assert got_base["branch"] is None
    # explicit branch resolves the logical worktree.
    got_feat = checkouts.get_checkout(project["id"], "runnerA", branch="feature-x")
    assert got_feat["id"] == feat["id"]
    assert got_feat["branch"] == "feature-x"


def test_empty_string_branch_is_base(project):
    checkouts.upsert_checkout(project["id"], "runnerA", "/work/base", branch="")
    got = checkouts.get_checkout(project["id"], "runnerA")
    assert got is not None and got["path"] == "/work/base"


def test_status_transition(project):
    c = checkouts.upsert_checkout(project["id"], "runnerA", "/work/repo")
    updated = checkouts.set_checkout_status(c["id"], "archived")
    assert updated is not None and updated["status"] == "archived"
    assert checkouts.set_checkout_status("no-such-id", "active") is None


def test_archive_checkout_by_path(project):
    c = checkouts.upsert_checkout(
        project["id"], "runnerA", "/work/feat", branch="feat-x"
    )
    updated = checkouts.archive_checkout_by_path(project["id"], "/work/feat", runner_id="runnerA")
    assert updated is not None and updated["id"] == c["id"]
    assert updated["status"] == "archived"
    again = checkouts.archive_checkout_by_path(project["id"], "/work/feat", runner_id="runnerA")
    assert again is not None and again["id"] == c["id"]
    assert checkouts.archive_checkout_by_path(project["id"], "/no/such") is None
    assert checkouts.archive_checkout_by_path("", "/work/feat") is None


def test_get_missing_returns_none(project):
    assert checkouts.get_checkout(project["id"], "ghost") is None


# ---------------------------------------------------------------------------
# Relative-path write guard (#654) — checkouts.path must be absolute.
# ---------------------------------------------------------------------------

def test_upsert_rejects_relative_path(project):
    # The v60 collapse once seeded workspace-relative paths verbatim; the V2
    # resolver treats checkouts.path as absolute, so reject relative writes.
    with pytest.raises(ValueError):
        checkouts.upsert_checkout(project["id"], "runnerA", "relative/repo")
    # Nothing was written.
    assert checkouts.list_checkouts(project["id"]) == []


# ---------------------------------------------------------------------------
# get_checkout_with_legacy_fallback — runner_id='' fallback (M-2)
# ---------------------------------------------------------------------------

def test_legacy_fallback_finds_empty_runner_when_no_exact(project):
    # No exact-runner row; a legacy runner_id='' row exists → it is the fallback.
    checkouts.upsert_checkout(project["id"], "", "/work/legacy")
    got = checkouts.get_checkout_with_legacy_fallback(project["id"], "runnerA")
    assert got is not None and got["runner_id"] == "" and got["path"] == "/work/legacy"


def test_legacy_fallback_exact_shadows_empty(project):
    # Both an exact-runner row and a legacy '' row exist → exact row WINS.
    checkouts.upsert_checkout(project["id"], "", "/work/legacy")
    checkouts.upsert_checkout(project["id"], "runnerA", "/work/mine")
    got = checkouts.get_checkout_with_legacy_fallback(project["id"], "runnerA")
    assert got is not None and got["runner_id"] == "runnerA" and got["path"] == "/work/mine"


def test_legacy_fallback_none_when_neither(project):
    assert checkouts.get_checkout_with_legacy_fallback(project["id"], "runnerA") is None


def test_legacy_fallback_branch_scoped(project):
    # Fallback also applies to a branch lookup: the '' row's branch checkout.
    checkouts.upsert_checkout(project["id"], "", "/work/legacy-feat", branch="feature-x")
    got = checkouts.get_checkout_with_legacy_fallback(
        project["id"], "runnerA", branch="feature-x"
    )
    assert got is not None and got["runner_id"] == "" and got["branch"] == "feature-x"


def test_legacy_fallback_empty_runner_query_no_double_lookup(project):
    # Querying with runner_id='' directly: no exact row → must NOT re-query '' as
    # its own fallback (returns None cleanly).
    assert checkouts.get_checkout_with_legacy_fallback(project["id"], "") is None


# ---------------------------------------------------------------------------
# has_active_base_checkout — THE shared eligibility predicate (W3-2). The
# with-worktree remote preflight and the cluster git-state API both ask this
# one function, so the composer badge can never disagree with the 409.
# ---------------------------------------------------------------------------

def test_active_base_checkout_is_eligible(project):
    checkouts.upsert_checkout(project["id"], "runnerA", "/work/repo")
    assert checkouts.has_active_base_checkout(project["id"], "runnerA") is True


def test_no_checkout_at_all_is_not_eligible(project):
    assert checkouts.has_active_base_checkout(project["id"], "runnerA") is False


def test_branch_only_checkout_is_not_a_base_checkout(project):
    # ``capable_runners`` would accept this row; the base gate must not —
    # Phase 3 never clones, so a new branch has nothing to fork from here.
    checkouts.upsert_checkout(
        project["id"], "runnerA", "/work/feat", branch="feature-x"
    )
    assert checkouts.has_active_base_checkout(project["id"], "runnerA") is False


def test_archived_base_checkout_is_not_eligible(project):
    c = checkouts.upsert_checkout(project["id"], "runnerA", "/work/repo")
    checkouts.set_checkout_status(c["id"], "archived")
    assert checkouts.has_active_base_checkout(project["id"], "runnerA") is False


def test_legacy_empty_runner_base_row_makes_any_runner_eligible(project):
    # Matches get_checkout_with_legacy_fallback semantics: the pre-instance_id
    # '' row is the fallback for every runner.
    checkouts.upsert_checkout(project["id"], "", "/work/legacy")
    assert checkouts.has_active_base_checkout(project["id"], "runnerA") is True
    assert checkouts.has_active_base_checkout(project["id"], "runnerB") is True


def test_archived_old_base_row_does_not_mask_newer_active_one(project):
    # A moved project path leaves TWO base rows on the same runner (uniqueness
    # is per (project, runner, path)): the old one archived, the new one active.
    # get_checkout returns the OLDEST row, so the predicate must scan ALL base
    # rows rather than judge the first one it finds.
    old = checkouts.upsert_checkout(project["id"], "runnerA", "/old/repo")
    checkouts.set_checkout_status(old["id"], "archived")
    checkouts.upsert_checkout(project["id"], "runnerA", "/new/repo")
    assert checkouts.has_active_base_checkout(project["id"], "runnerA") is True


def test_archived_own_base_row_blocks_legacy_fallback(project):
    """Only-archived own base rows do NOT fall back to the legacy '' row.

    Matches get_checkout_with_legacy_fallback's actual semantics: it falls back
    ONLY when the exact-runner lookup returns no row at all — an exact-runner
    row WINS regardless of its status. So a runner whose only base row is
    archived stays ineligible even though a legacy '' active base row exists.
    """
    own = checkouts.upsert_checkout(project["id"], "runnerA", "/work/mine")
    checkouts.set_checkout_status(own["id"], "archived")
    checkouts.upsert_checkout(project["id"], "", "/work/legacy")
    assert checkouts.has_active_base_checkout(project["id"], "runnerA") is False
    # A runner with no base rows of its own still gets the legacy fallback.
    assert checkouts.has_active_base_checkout(project["id"], "runnerB") is True


def test_a_base_checkout_on_another_runner_does_not_count(project):
    checkouts.upsert_checkout(project["id"], "runnerA", "/work/repo")
    assert checkouts.has_active_base_checkout(project["id"], "runnerB") is False


# ---------------------------------------------------------------------------
# list_active_base_checkouts — one SELECT for the cluster catalog (PR 8).
# ---------------------------------------------------------------------------

def test_list_active_base_checkouts_one_select_across_projects(mock_db):
    a = projects_v2.upsert_project("git:a", name="A")
    b = projects_v2.upsert_project("git:b", name="B")
    checkouts.upsert_checkout(a["id"], "runnerA", "/a/repo")
    checkouts.upsert_checkout(b["id"], "runnerB", "/b/repo")
    checkouts.upsert_checkout(a["id"], "runnerA", "/a/feat", branch="feature-x")
    rows = checkouts.list_active_base_checkouts()
    paths = sorted(r["path"] for r in rows)
    assert paths == ["/a/repo", "/b/repo"]
    assert all(not r.get("branch") for r in rows)


def test_list_active_base_checkouts_omits_archived_and_branch_only(project):
    checkouts.upsert_checkout(project["id"], "runnerA", "/work/repo")
    archived = checkouts.upsert_checkout(project["id"], "runnerB", "/work/old")
    checkouts.set_checkout_status(archived["id"], "archived")
    checkouts.upsert_checkout(
        project["id"], "runnerA", "/work/feat", branch="feature-x"
    )
    rows = checkouts.list_active_base_checkouts()
    assert [r["path"] for r in rows] == ["/work/repo"]


def test_list_active_base_checkouts_empty_string_branch_is_base(project):
    checkouts.upsert_checkout(project["id"], "runnerA", "/work/base", branch="")
    rows = checkouts.list_active_base_checkouts()
    assert len(rows) == 1
    assert rows[0]["path"] == "/work/base"
