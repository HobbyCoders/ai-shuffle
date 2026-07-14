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


def _seed_session(shim, sid, project_id=None, project_v2_id=None, branch=None):
    shim.cursor().execute(
        "INSERT INTO sessions (id, project_id, project_v2_id, branch) "
        "VALUES (%s, %s, %s, %s)",
        (sid, project_id, project_v2_id, branch),
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
