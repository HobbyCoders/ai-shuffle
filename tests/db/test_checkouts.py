"""Unit tests for app/db/checkouts.py — per-runner working-tree checkouts of a
project_v2 (v2 core, Phase 3)."""

import pytest

from app.db import checkouts
from app.db import projects_v2


@pytest.fixture
def test_db():
    from tests.pg_shim import new_sqlite_connection
    conn = new_sqlite_connection()
    yield conn
    conn.close()


@pytest.fixture
def mock_db(test_db):
    from tests.pg_shim import route_db_to
    with route_db_to(test_db):
        yield test_db


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
