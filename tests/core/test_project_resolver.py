"""Tests for app/core/project_resolver.py — the single origin/checkout/cwd
authority (v2 core, Phase 3).

pg_shim SQLite DB (matching tests/db conventions); ``checkout_manager`` is stubbed
where lazy materialization would otherwise touch git/disk.
"""

import pytest

from app.core import project_resolver
from app.db import checkouts as checkouts_db
from app.db import projects_v2

RUNNER = "me"


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
    with route_db_to(test_db) as shim:
        yield shim


@pytest.fixture(autouse=True)
def pin_runner(monkeypatch):
    """Pin effective_instance_id so a default runner_id is deterministic."""
    from app.core.config import settings
    monkeypatch.setattr(type(settings), "effective_instance_id", property(lambda self: RUNNER))


def _seed_project(shim, pid, name, link_group_id=None):
    shim.cursor().execute(
        "INSERT INTO projects (id, name, path, link_group_id) VALUES (%s, %s, %s, %s)",
        (pid, name, "/legacy/" + pid, link_group_id),
    )


def _seed_session(shim, sid, project_id=None, project_v2_id=None, branch=None,
                  worktree_branch=None):
    shim.cursor().execute(
        "INSERT INTO sessions (id, project_id, project_v2_id, branch, worktree_branch) "
        "VALUES (%s, %s, %s, %s, %s)",
        (sid, project_id, project_v2_id, branch, worktree_branch),
    )


def _session_row(shim, sid):
    cur = shim.cursor()
    cur.execute(
        "SELECT id, project_id, project_v2_id, branch FROM sessions WHERE id = %s",
        (sid,),
    )
    return cur.fetchone()


# ---------------------------------------------------------------------------
# origin_for_legacy_project
# ---------------------------------------------------------------------------

def test_origin_git_token():
    assert project_resolver.origin_for_legacy_project(
        {"id": "p1", "link_group_id": "git:abc123"}
    ) == "git:abc123"


def test_origin_manual_token():
    assert project_resolver.origin_for_legacy_project(
        {"id": "p1", "link_group_id": "manual:grp"}
    ) == "manual:grp"


def test_origin_local_fallback_for_remote_less():
    assert project_resolver.origin_for_legacy_project(
        {"id": "p9", "link_group_id": None}
    ) == "local:p9"


# ---------------------------------------------------------------------------
# resolve_project_v2
# ---------------------------------------------------------------------------

def test_resolve_bound_project_v2(mock_db):
    pv2 = projects_v2.upsert_project("git:abc", name="Repo")
    got = project_resolver.resolve_project_v2({"id": "s1", "project_v2_id": pv2["id"]})
    assert got is not None and got["id"] == pv2["id"]
    assert got["origin"] == "git:abc"


def test_resolve_self_heal_derives_and_persists(mock_db):
    # Session has NO project_v2_id but a legacy project_id whose origin has a pv2.
    _seed_project(mock_db, "proj-a", "Repo A", "git:shared")
    pv2 = projects_v2.upsert_project("git:shared", name="Repo A")
    _seed_session(mock_db, "s2", project_id="proj-a", project_v2_id=None)

    session_row = {"id": "s2", "project_id": "proj-a", "project_v2_id": None}
    got = project_resolver.resolve_project_v2(session_row)
    assert got is not None and got["id"] == pv2["id"]

    # Self-heal persisted the binding on the sessions row (project_id untouched).
    row = _session_row(mock_db, "s2")
    assert row["project_v2_id"] == pv2["id"]
    assert row["project_id"] == "proj-a"


def test_resolve_self_heal_remote_less_local_origin(mock_db):
    _seed_project(mock_db, "loc-1", "Local", None)
    pv2 = projects_v2.upsert_project("local:loc-1", name="Local")
    got = project_resolver.resolve_project_v2(
        {"id": "s3", "project_id": "loc-1", "project_v2_id": None}
    )
    assert got is not None and got["id"] == pv2["id"]


def test_resolve_none_when_no_binding_and_no_project(mock_db):
    assert project_resolver.resolve_project_v2({"id": "s4"}) is None


def test_resolve_lazy_seeds_pv2_for_post_migration_legacy(mock_db):
    # A legacy project created AFTER the v60 one-time seed (so its origin was
    # never folded). F4: resolve self-seeds the SAME rows the migration would
    # have — the pv2 (deterministic id) AND the legacy row's base checkout —
    # then resolves, without touching any legacy column.
    _seed_project(mock_db, "proj-z", "Z", "git:unseeded")

    got = project_resolver.resolve_project_v2(
        {"id": "s5", "project_id": "proj-z", "project_v2_id": None}
    )

    assert got is not None
    assert got["id"] == projects_v2.project_v2_id_for_origin("git:unseeded")
    assert got["origin"] == "git:unseeded"
    assert got["name"] == "Z"

    # Base checkout row seeded with the migration's deterministic id + legacy
    # path; runner_id is '' because the legacy row had no instance_id; branch NULL.
    cur = mock_db.cursor()
    cur.execute("SELECT * FROM checkouts WHERE id = %s", ("ck-proj-z",))
    ck = cur.fetchone()
    assert ck is not None
    assert ck["project_id"] == got["id"]
    assert ck["runner_id"] == ""
    assert ck["path"] == "/legacy/proj-z"
    assert ck["branch"] is None

    # Legacy projects row is untouched.
    cur.execute("SELECT id, link_group_id FROM projects WHERE id = %s", ("proj-z",))
    legacy = cur.fetchone()
    assert legacy["link_group_id"] == "git:unseeded"


def test_resolve_lazy_seed_second_resolve_is_zero_write(mock_db):
    # F4: the self-seed is idempotent — a second resolve inserts nothing new.
    _seed_project(mock_db, "proj-z", "Z", "git:unseeded")
    project_resolver.resolve_project_v2(
        {"id": "s5", "project_id": "proj-z", "project_v2_id": None}
    )

    def _counts():
        cur = mock_db.cursor()
        cur.execute("SELECT COUNT(*) AS n FROM projects_v2")
        pv2_n = cur.fetchone()["n"]
        cur.execute("SELECT COUNT(*) AS n FROM checkouts")
        ck_n = cur.fetchone()["n"]
        return pv2_n, ck_n

    before = _counts()
    got = project_resolver.resolve_project_v2(
        {"id": "s6", "project_id": "proj-z", "project_v2_id": None}
    )
    after = _counts()

    assert got is not None
    assert before == after == (1, 1)


# ---------------------------------------------------------------------------
# resolve_local_checkout
# ---------------------------------------------------------------------------

def test_resolve_local_checkout_base(mock_db):
    pv2 = projects_v2.upsert_project("git:c", name="C")
    checkouts_db.upsert_checkout(pv2["id"], RUNNER, "/work/base")
    row = project_resolver.resolve_local_checkout(
        {"id": "s6", "project_v2_id": pv2["id"]}
    )
    assert row is not None and row["path"] == "/work/base"
    assert row["branch"] is None


def test_resolve_local_checkout_branch(mock_db):
    pv2 = projects_v2.upsert_project("git:d", name="D")
    checkouts_db.upsert_checkout(pv2["id"], RUNNER, "/work/base")
    checkouts_db.upsert_checkout(pv2["id"], RUNNER, "/work/feat", branch="feature-x")
    row = project_resolver.resolve_local_checkout(
        {"id": "s7", "project_v2_id": pv2["id"], "branch": "feature-x"}
    )
    assert row is not None and row["path"] == "/work/feat"
    assert row["branch"] == "feature-x"


def test_resolve_local_checkout_wrong_runner_none(mock_db):
    pv2 = projects_v2.upsert_project("git:e", name="E")
    checkouts_db.upsert_checkout(pv2["id"], "someone-else", "/work/base")
    row = project_resolver.resolve_local_checkout(
        {"id": "s8", "project_v2_id": pv2["id"]}
    )
    assert row is None  # not materialized on THIS runner


def test_resolve_local_checkout_no_project_none(mock_db):
    assert project_resolver.resolve_local_checkout({"id": "s9"}) is None


# ---------------------------------------------------------------------------
# _effective_branch — lazy Step-F sessions.branch backfill
# ---------------------------------------------------------------------------

def test_effective_branch_prefers_set_branch(mock_db):
    # A non-NULL branch wins outright; worktree_branch is ignored, no DB touched.
    row = {"id": "sb1", "branch": "feature-a", "worktree_branch": "other-b"}
    assert project_resolver._effective_branch(row) == "feature-a"


def test_effective_branch_none_when_both_absent(mock_db):
    assert project_resolver._effective_branch({"id": "sb2"}) is None
    assert project_resolver._effective_branch(
        {"id": "sb2", "branch": "", "worktree_branch": ""}
    ) is None


def test_effective_branch_backfills_from_worktree_branch(mock_db):
    # Worktree session minted by an un-upgraded install AFTER the v60 collapse:
    # branch is NULL but worktree_branch carries the logical branch. Mirror Step F
    # — one-shot, NULL-guarded backfill of sessions.branch — and use it here.
    _seed_session(mock_db, "sb3", project_v2_id="pv2-x", branch=None,
                  worktree_branch="feat/minted")
    row = {"id": "sb3", "branch": None, "worktree_branch": "feat/minted"}

    assert project_resolver._effective_branch(row) == "feat/minted"
    # persisted onto the sessions row (one-shot Step-F mirror) ...
    assert _session_row(mock_db, "sb3")["branch"] == "feat/minted"
    # ... and mutated in-place for THIS resolution.
    assert row["branch"] == "feat/minted"


def test_effective_branch_never_overwrites_set_db_branch(mock_db):
    # NULL-guarded: an already-set branch is never clobbered by worktree_branch.
    _seed_session(mock_db, "sb4", project_v2_id="pv2-x", branch="kept",
                  worktree_branch="ignored")
    row = {"id": "sb4", "branch": "kept", "worktree_branch": "ignored"}
    assert project_resolver._effective_branch(row) == "kept"
    assert _session_row(mock_db, "sb4")["branch"] == "kept"


def test_resolve_local_checkout_honors_backfilled_worktree_branch(mock_db):
    # End-to-end: a branch-NULL + worktree_branch session resolves to the BRANCH
    # checkout (not base) because _effective_branch backfills the branch first —
    # without it such a session would regress to the base checkout.
    pv2 = projects_v2.upsert_project("git:wb", name="WB")
    checkouts_db.upsert_checkout(pv2["id"], RUNNER, "/work/base")
    checkouts_db.upsert_checkout(pv2["id"], RUNNER, "/work/feat", branch="feat/minted")
    _seed_session(mock_db, "sb5", project_v2_id=pv2["id"], branch=None,
                  worktree_branch="feat/minted")

    row = {"id": "sb5", "project_v2_id": pv2["id"], "branch": None,
           "worktree_branch": "feat/minted"}
    got = project_resolver.resolve_local_checkout(row)
    assert got is not None and got["path"] == "/work/feat"
    assert got["branch"] == "feat/minted"
    # the backfill also persisted to the sessions row.
    assert _session_row(mock_db, "sb5")["branch"] == "feat/minted"


# ---------------------------------------------------------------------------
# resolve_cwd (checkout_manager stubbed)
# ---------------------------------------------------------------------------

def test_resolve_cwd_branch_path(mock_db, monkeypatch):
    pv2 = projects_v2.upsert_project("git:f", name="F")
    calls = []
    monkeypatch.setattr(
        project_resolver.checkout_manager,
        "ensure_checkout",
        lambda project, branch=None, runner_id=None: calls.append((project["id"], branch)) or "/cwd/branch",
    )
    out = project_resolver.resolve_cwd(
        {"id": "s10", "project_v2_id": pv2["id"], "branch": "feat/x"}
    )
    assert out == "/cwd/branch"
    assert calls == [(pv2["id"], "feat/x")]


def test_resolve_cwd_base_path(mock_db, monkeypatch):
    pv2 = projects_v2.upsert_project("git:g", name="G")
    calls = []
    monkeypatch.setattr(
        project_resolver.checkout_manager,
        "ensure_checkout",
        lambda project, branch=None, runner_id=None: calls.append((project["id"], branch)) or "/cwd/base",
    )
    out = project_resolver.resolve_cwd({"id": "s11", "project_v2_id": pv2["id"]})
    assert out == "/cwd/base"
    assert calls == [(pv2["id"], None)]


def test_resolve_cwd_none_when_no_project(mock_db, monkeypatch):
    called = []
    monkeypatch.setattr(
        project_resolver.checkout_manager,
        "ensure_checkout",
        lambda *a, **k: called.append(1) or "/should/not/happen",
    )
    assert project_resolver.resolve_cwd({"id": "s12"}) is None
    assert called == []  # never materialized without a project


def test_resolve_cwd_empty_branch_treated_as_base(mock_db, monkeypatch):
    pv2 = projects_v2.upsert_project("git:h", name="H")
    calls = []
    monkeypatch.setattr(
        project_resolver.checkout_manager,
        "ensure_checkout",
        lambda project, branch=None, runner_id=None: calls.append(branch) or "/cwd/base",
    )
    project_resolver.resolve_cwd({"id": "s13", "project_v2_id": pv2["id"], "branch": ""})
    assert calls == [None]  # empty-string branch → base checkout


# ---------------------------------------------------------------------------
# resolve_local_checkout — runner_id='' legacy fallback (M-2)
# ---------------------------------------------------------------------------

def test_resolve_local_checkout_legacy_empty_runner_fallback(mock_db):
    # No checkout on THIS runner, but a legacy runner_id='' row exists (a
    # pre-instance_id project seeded under the '' sentinel). It is the fallback.
    pv2 = projects_v2.upsert_project("git:lf", name="LF")
    checkouts_db.upsert_checkout(pv2["id"], "", "/work/legacy-base")
    row = project_resolver.resolve_local_checkout(
        {"id": "s14", "project_v2_id": pv2["id"]}
    )
    assert row is not None and row["path"] == "/work/legacy-base"
    assert row["runner_id"] == ""


def test_resolve_local_checkout_real_runner_shadows_empty(mock_db):
    # Both a real-runner row and a legacy '' row exist → the real-runner row WINS.
    pv2 = projects_v2.upsert_project("git:sh", name="SH")
    checkouts_db.upsert_checkout(pv2["id"], "", "/work/legacy-base")
    checkouts_db.upsert_checkout(pv2["id"], RUNNER, "/work/mine")
    row = project_resolver.resolve_local_checkout(
        {"id": "s15", "project_v2_id": pv2["id"]}
    )
    assert row is not None and row["path"] == "/work/mine"
    assert row["runner_id"] == RUNNER


# ---------------------------------------------------------------------------
# resolve_existing_cwd — pure read, NEVER materializes (F3)
# ---------------------------------------------------------------------------

def test_resolve_existing_cwd_returns_path_when_row_and_dir_exist(mock_db, monkeypatch, tmp_path):
    pv2 = projects_v2.upsert_project("git:ex", name="EX")
    checkouts_db.upsert_checkout(pv2["id"], RUNNER, str(tmp_path))
    # ensure_checkout must NEVER be called on the read path.
    monkeypatch.setattr(
        project_resolver.checkout_manager,
        "ensure_checkout",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("materialized on a read")),
    )
    out = project_resolver.resolve_existing_cwd(
        {"id": "s16", "project_v2_id": pv2["id"]}
    )
    assert out == str(tmp_path)


def test_resolve_existing_cwd_none_when_dir_absent(mock_db):
    # Row exists but its path is not on disk → None (honest not-materialized),
    # never materializes.
    pv2 = projects_v2.upsert_project("git:ex2", name="EX2")
    checkouts_db.upsert_checkout(pv2["id"], RUNNER, "/does/not/exist/anywhere")
    out = project_resolver.resolve_existing_cwd(
        {"id": "s17", "project_v2_id": pv2["id"]}
    )
    assert out is None


def test_resolve_existing_cwd_none_when_no_checkout_row(mock_db):
    pv2 = projects_v2.upsert_project("git:ex3", name="EX3")
    out = project_resolver.resolve_existing_cwd(
        {"id": "s18", "project_v2_id": pv2["id"]}
    )
    assert out is None


# ---------------------------------------------------------------------------
# has_local_base_checkout — the adopt preflight gate (R5.1). Gates on BASE
# availability (dir present on disk), NOT the branch checkout — a real dir is
# required, so these use tmp_path.
# ---------------------------------------------------------------------------

def test_has_local_base_checkout_true_when_base_present(mock_db, tmp_path):
    base = tmp_path / "base"
    base.mkdir()
    pv2 = projects_v2.upsert_project("git:hb1", name="HB1")
    checkouts_db.upsert_checkout(pv2["id"], RUNNER, str(base))
    assert project_resolver.has_local_base_checkout(
        {"id": "hb1", "project_v2_id": pv2["id"]}
    ) is True


def test_has_local_base_checkout_true_for_branch_session_on_base_only(mock_db, tmp_path):
    # THE FIX: a BRANCH session is adoptable on BASE availability — the branch
    # checkout need not exist yet (it materializes lazily at spawn). Gating on the
    # branch row (as resolve_local_checkout does) would spuriously return False.
    base = tmp_path / "base"
    base.mkdir()
    pv2 = projects_v2.upsert_project("git:hb2", name="HB2")
    checkouts_db.upsert_checkout(pv2["id"], RUNNER, str(base))  # base only, no branch row
    row = {"id": "hb2", "project_v2_id": pv2["id"], "branch": "feature-x"}
    assert project_resolver.has_local_base_checkout(row) is True
    # Sanity: the branch checkout genuinely is NOT materialized on this runner.
    assert project_resolver.resolve_local_checkout(row) is None


def test_has_local_base_checkout_false_when_no_base_row(mock_db):
    pv2 = projects_v2.upsert_project("git:hb3", name="HB3")
    assert project_resolver.has_local_base_checkout(
        {"id": "hb3", "project_v2_id": pv2["id"]}
    ) is False


def test_has_local_base_checkout_false_when_dir_missing(mock_db, tmp_path):
    gone = tmp_path / "never"  # row present, dir absent
    pv2 = projects_v2.upsert_project("git:hb4", name="HB4")
    checkouts_db.upsert_checkout(pv2["id"], RUNNER, str(gone))
    assert project_resolver.has_local_base_checkout(
        {"id": "hb4", "project_v2_id": pv2["id"]}
    ) is False


def test_has_local_base_checkout_false_when_no_project(mock_db):
    assert project_resolver.has_local_base_checkout({"id": "hb5"}) is False


def test_has_local_base_checkout_wrong_runner_false(mock_db, tmp_path):
    base = tmp_path / "base"
    base.mkdir()
    pv2 = projects_v2.upsert_project("git:hb6", name="HB6")
    checkouts_db.upsert_checkout(pv2["id"], "other-runner", str(base))
    # Default runner is RUNNER (pinned) — the base lives only on another runner.
    assert project_resolver.has_local_base_checkout(
        {"id": "hb6", "project_v2_id": pv2["id"]}
    ) is False

@pytest.mark.parametrize("branch", [None, "feature-x"])
@pytest.mark.parametrize("runner", [RUNNER, ""])
def test_moved_checkout_resolves_existing_candidate(mock_db, monkeypatch, tmp_path, branch, runner):
    from app.api import git_helpers

    pv2 = projects_v2.upsert_project("git:moved", name="Moved")
    old_path = tmp_path / "old"
    live_path = tmp_path / "live"
    old_path.mkdir()
    live_path.mkdir()
    # Use the production writer: registering the new path retains the old row.
    old = checkouts_db.upsert_checkout(pv2["id"], runner, str(old_path), branch=branch)
    live = checkouts_db.upsert_checkout(pv2["id"], runner, str(live_path), branch=branch)
    mock_db.cursor().execute(
        "UPDATE checkouts SET created_at = %s WHERE id = %s",
        ("2000-01-01", old["id"]),
    )
    old_path.rmdir()
    session = {"id": "moved-session", "project_v2_id": pv2["id"], "branch": branch}
    monkeypatch.setattr(git_helpers.database, "get_session", lambda sid: session)
    monkeypatch.setattr(
        project_resolver.checkout_manager, "ensure_checkout",
        lambda *a, **kw: pytest.fail("Read must not materialize a checkout"),
    )
    assert project_resolver.resolve_local_checkout(session)["id"] == live["id"]
    assert project_resolver.resolve_existing_cwd(session) == str(live_path)
    if branch is None:
        assert project_resolver.has_local_base_checkout(session)
    git_helpers.invalidate_working_dir_cache()
    try:
        assert git_helpers.get_working_dir("legacy-project", session_id=session["id"]) == str(live_path)
    finally:
        git_helpers.invalidate_working_dir_cache()
    assert len(checkouts_db.list_checkouts(pv2["id"])) == 2


def test_missing_branch_does_not_use_live_base_or_other_runner(mock_db, tmp_path):
    pv2 = projects_v2.upsert_project("git:isolated", name="Isolated")
    checkouts_db.upsert_checkout(pv2["id"], RUNNER, str(tmp_path))
    checkouts_db.upsert_checkout(pv2["id"], "other", str(tmp_path), branch="feature-x")
    session = {"project_v2_id": pv2["id"], "branch": "feature-x"}
    assert project_resolver.resolve_existing_cwd(session) is None


def test_missing_exact_runner_does_not_use_live_legacy_candidate(mock_db, tmp_path):
    pv2 = projects_v2.upsert_project("git:precedence", name="Precedence")
    missing = checkouts_db.upsert_checkout(pv2["id"], RUNNER, str(tmp_path / "missing"))
    checkouts_db.upsert_checkout(pv2["id"], "", str(tmp_path))
    session = {"project_v2_id": pv2["id"]}
    assert project_resolver.resolve_local_checkout(session)["id"] == missing["id"]
    assert project_resolver.resolve_existing_cwd(session) is None
    assert not project_resolver.has_local_base_checkout(session)
