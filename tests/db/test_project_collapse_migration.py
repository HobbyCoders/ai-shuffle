"""Unit tests for the V2 Phase 3 (v60) origin-collapse migration in
app/db/database.py._run_column_migrations.

The collapse folds the legacy per-install ``projects`` rows into origin-keyed
``projects_v2`` rows (one per distinct origin) with one ``checkouts`` row per
legacy project, then rebinds sessions onto their project_v2 — additively,
idempotently, and without touching any legacy column. Modeled on the v59
turns<-queued_messages migration test."""


from app.db import database
from app.db import projects_v2 as pv2
from app.db.projects_v2 import project_v2_id_for_origin as origin_id
from tests.shared_fixtures import foreign_keys_off  # noqa: F401  (autouse fixture)


def _seed_project(shim, pid, name, path, instance_id=None, link_group_id=None):
    shim.cursor().execute(
        "INSERT INTO projects (id, name, path, instance_id, link_group_id) "
        "VALUES (%s, %s, %s, %s, %s)",
        (pid, name, path, instance_id, link_group_id),
    )


def _seed_session(shim, sid, project_id, worktree_id=None):
    # NOTE: `sessions.branch` does not exist until the migration's Step A adds
    # it, so it is never referenced at seed time. `worktree_id` is a base-schema
    # column and is safe to seed pre-migration.
    shim.cursor().execute(
        "INSERT INTO sessions (id, project_id, worktree_id) VALUES (%s, %s, %s)",
        (sid, project_id, worktree_id),
    )


def _seed_git_repository(shim, repo_id, project_id):
    shim.cursor().execute(
        "INSERT INTO git_repositories (id, project_id) VALUES (%s, %s)",
        (repo_id, project_id),
    )


def _seed_worktree(shim, wt_id, repository_id, branch_name, worktree_path, status="active"):
    shim.cursor().execute(
        "INSERT INTO worktrees "
        "(id, repository_id, branch_name, worktree_path, status) "
        "VALUES (%s, %s, %s, %s, %s)",
        (wt_id, repository_id, branch_name, worktree_path, status),
    )


def _count(shim, table):
    cur = shim.cursor()
    cur.execute(f"SELECT COUNT(*) AS n FROM {table}")
    return cur.fetchone()["n"]


def test_columns_added(test_db):
    from tests.pg_shim import route_db_to
    from app.db.base import column_exists

    with route_db_to(test_db) as shim:
        database._run_column_migrations(shim.cursor())
        cur = shim.cursor()
        assert column_exists(cur, "sessions", "project_v2_id")
        assert column_exists(cur, "sessions", "branch")
        assert column_exists(cur, "checkouts", "branch")


def test_suffixed_clones_fold_to_one_project_n_checkouts(test_db):
    from tests.pg_shim import route_db_to

    with route_db_to(test_db) as shim:
        # Three per-install clones of one repo: distinct project rows sharing one
        # link_group_id (different instance_id + path).
        _seed_project(shim, "proj-a", "repo", "/a/repo", "inst1", "git:shared")
        _seed_project(shim, "proj-b", "repo", "/b/repo", "inst2", "git:shared")
        _seed_project(shim, "proj-c", "repo", "/c/repo", "inst3", "git:shared")

        database._run_column_migrations(shim.cursor())

        # 1 projects_v2 for the shared origin.
        rows = pv2.list_projects_v2()
        assert len(rows) == 1
        assert rows[0]["origin"] == "git:shared"
        assert rows[0]["id"] == origin_id("git:shared")

        # N checkouts, one per legacy row, each with its own runner_id + path.
        cur = shim.cursor()
        cur.execute(
            "SELECT id, runner_id, path, branch, status FROM checkouts "
            "WHERE project_id = %s ORDER BY runner_id",
            (origin_id("git:shared"),),
        )
        cks = cur.fetchall()
        assert len(cks) == 3
        assert [c["runner_id"] for c in cks] == ["inst1", "inst2", "inst3"]
        assert {c["id"] for c in cks} == {"ck-proj-a", "ck-proj-b", "ck-proj-c"}
        assert all(c["branch"] is None and c["status"] == "active" for c in cks)


def test_remote_less_gets_local_origin(test_db):
    from tests.pg_shim import route_db_to

    with route_db_to(test_db) as shim:
        _seed_project(shim, "loc-1", "local proj", "/x/loc", "inst1", None)
        database._run_column_migrations(shim.cursor())

        got = pv2.get_project_v2_by_origin("local:loc-1")
        assert got is not None
        assert got["name"] == "local proj"
        # Its checkout carries the legacy instance_id as runner_id.
        cur = shim.cursor()
        cur.execute(
            "SELECT runner_id, path FROM checkouts WHERE id = %s", ("ck-loc-1",)
        )
        ck = cur.fetchone()
        assert ck["runner_id"] == "inst1" and ck["path"] == "/x/loc"


def test_null_instance_id_becomes_empty_runner(test_db):
    from tests.pg_shim import route_db_to

    with route_db_to(test_db) as shim:
        _seed_project(shim, "loc-2", "p", "/y/loc", None, None)
        database._run_column_migrations(shim.cursor())
        cur = shim.cursor()
        cur.execute("SELECT runner_id FROM checkouts WHERE id = %s", ("ck-loc-2",))
        assert cur.fetchone()["runner_id"] == ""


def test_manual_link_group_folds_to_one(test_db):
    from tests.pg_shim import route_db_to

    with route_db_to(test_db) as shim:
        # Two clones with different remotes, manually linked -> one origin.
        _seed_project(shim, "fork-a", "fork a", "/a/fork", "inst1", "manual:grp")
        _seed_project(shim, "fork-b", "fork b", "/b/fork", "inst2", "manual:grp")
        database._run_column_migrations(shim.cursor())

        rows = pv2.list_projects_v2()
        assert len(rows) == 1
        assert rows[0]["origin"] == "manual:grp"
        assert _count(shim, "checkouts") == 2


def test_session_rebind_keeps_project_id(test_db):
    from tests.pg_shim import route_db_to

    with route_db_to(test_db) as shim:
        _seed_project(shim, "proj-a", "repo", "/a/repo", "inst1", "git:shared")
        _seed_session(shim, "sess-1", "proj-a")
        _seed_session(shim, "sess-2", "proj-a")

        database._run_column_migrations(shim.cursor())

        cur = shim.cursor()
        cur.execute(
            "SELECT project_id, project_v2_id FROM sessions ORDER BY id"
        )
        sess = cur.fetchall()
        # project_v2_id set to the origin's pv2; legacy project_id untouched.
        assert all(s["project_id"] == "proj-a" for s in sess)
        assert all(s["project_v2_id"] == origin_id("git:shared") for s in sess)


def test_rerun_is_zero_write_and_identical(test_db):
    from tests.pg_shim import route_db_to

    with route_db_to(test_db) as shim:
        _seed_project(shim, "proj-a", "repo", "/a/repo", "inst1", "git:shared")
        _seed_project(shim, "proj-b", "repo", "/b/repo", "inst2", "git:shared")
        _seed_project(shim, "loc-1", "local", "/x/loc", None, None)
        _seed_session(shim, "sess-1", "proj-a")

        database._run_column_migrations(shim.cursor())

        def _snapshot():
            cur = shim.cursor()
            cur.execute("SELECT * FROM projects_v2 ORDER BY id")
            pv2_rows = cur.fetchall()
            cur.execute("SELECT * FROM checkouts ORDER BY id")
            ck_rows = cur.fetchall()
            cur.execute("SELECT id, project_v2_id FROM sessions ORDER BY id")
            sess_rows = cur.fetchall()
            return pv2_rows, ck_rows, sess_rows

        before = _snapshot()
        pv2_n, ck_n, sess_n = _count(shim, "projects_v2"), _count(shim, "checkouts"), _count(shim, "sessions")

        # Second boot: identical rows, no new inserts, no duplication.
        database._run_column_migrations(shim.cursor())
        after = _snapshot()

        # Two distinct origins (git:shared shared by proj-a/proj-b; local:loc-1).
        assert _count(shim, "projects_v2") == pv2_n == 2
        assert _count(shim, "checkouts") == ck_n == 3
        assert _count(shim, "sessions") == sess_n == 1
        assert before == after


def test_legacy_projects_rows_untouched(test_db):
    from tests.pg_shim import route_db_to

    with route_db_to(test_db) as shim:
        _seed_project(shim, "proj-a", "repo", "/a/repo", "inst1", "git:shared")
        _seed_project(shim, "loc-1", "local", "/x/loc", None, None)

        cur = shim.cursor()
        cur.execute("SELECT * FROM projects ORDER BY id")
        before = cur.fetchall()

        database._run_column_migrations(shim.cursor())

        cur = shim.cursor()
        cur.execute("SELECT * FROM projects ORDER BY id")
        after = cur.fetchall()
        assert before == after


def test_empty_projects_is_noop(test_db):
    from tests.pg_shim import route_db_to

    with route_db_to(test_db) as shim:
        database._run_column_migrations(shim.cursor())
        assert _count(shim, "projects_v2") == 0
        assert _count(shim, "checkouts") == 0


# ---------------------------------------------------------------------------
# Steps E–F: branch checkouts seeded from legacy `worktrees`, sessions.branch
# backfill.
# ---------------------------------------------------------------------------


def test_worktree_seeds_branch_checkout(test_db):
    from tests.pg_shim import route_db_to

    with route_db_to(test_db) as shim:
        _seed_project(shim, "proj-a", "repo", "/a/repo", "inst1", "git:shared")
        _seed_git_repository(shim, "repo-a", "proj-a")
        _seed_worktree(shim, "wt-1", "repo-a", "feature/x", "/a/repo/.wt/x")

        database._run_column_migrations(shim.cursor())

        cur = shim.cursor()
        cur.execute(
            "SELECT id, project_id, runner_id, path, branch, status "
            "FROM checkouts WHERE id = %s",
            ("ck-wt-wt-1",),
        )
        ck = cur.fetchone()
        assert ck is not None
        # project = owning legacy project's origin; runner = legacy instance_id;
        # path/branch from the worktree row.
        assert ck["project_id"] == origin_id("git:shared")
        assert ck["runner_id"] == "inst1"
        assert ck["path"] == "/a/repo/.wt/x"
        assert ck["branch"] == "feature/x"
        assert ck["status"] == "active"


def test_session_branch_backfilled_from_worktree(test_db):
    from tests.pg_shim import route_db_to

    with route_db_to(test_db) as shim:
        _seed_project(shim, "proj-a", "repo", "/a/repo", "inst1", "git:shared")
        _seed_git_repository(shim, "repo-a", "proj-a")
        _seed_worktree(shim, "wt-1", "repo-a", "feature/x", "/a/repo/.wt/x")
        _seed_session(shim, "sess-1", "proj-a", worktree_id="wt-1")
        # A session with no worktree stays branch-NULL.
        _seed_session(shim, "sess-2", "proj-a")

        database._run_column_migrations(shim.cursor())

        cur = shim.cursor()
        cur.execute("SELECT branch FROM sessions WHERE id = %s", ("sess-1",))
        assert cur.fetchone()["branch"] == "feature/x"
        cur.execute("SELECT branch FROM sessions WHERE id = %s", ("sess-2",))
        assert cur.fetchone()["branch"] is None


def test_non_null_branch_never_clobbered(test_db):
    from tests.pg_shim import route_db_to

    with route_db_to(test_db) as shim:
        _seed_project(shim, "proj-a", "repo", "/a/repo", "inst1", "git:shared")
        _seed_git_repository(shim, "repo-a", "proj-a")
        _seed_worktree(shim, "wt-1", "repo-a", "feature/x", "/a/repo/.wt/x")
        _seed_session(shim, "sess-1", "proj-a", worktree_id="wt-1")

        # First boot backfills branch. Then a hand-set non-NULL branch must
        # survive a second boot (Step F is WHERE branch IS NULL).
        database._run_column_migrations(shim.cursor())
        shim.cursor().execute(
            "UPDATE sessions SET branch = %s WHERE id = %s",
            ("hand-picked", "sess-1"),
        )
        database._run_column_migrations(shim.cursor())

        cur = shim.cursor()
        cur.execute("SELECT branch FROM sessions WHERE id = %s", ("sess-1",))
        assert cur.fetchone()["branch"] == "hand-picked"


def test_worktree_seed_is_idempotent(test_db):
    from tests.pg_shim import route_db_to

    with route_db_to(test_db) as shim:
        _seed_project(shim, "proj-a", "repo", "/a/repo", "inst1", "git:shared")
        _seed_git_repository(shim, "repo-a", "proj-a")
        _seed_worktree(shim, "wt-1", "repo-a", "feature/x", "/a/repo/.wt/x")

        database._run_column_migrations(shim.cursor())
        cur = shim.cursor()
        cur.execute("SELECT * FROM checkouts WHERE id = %s", ("ck-wt-wt-1",))
        before = cur.fetchone()

        database._run_column_migrations(shim.cursor())
        cur = shim.cursor()
        cur.execute("SELECT COUNT(*) AS n FROM checkouts WHERE id = %s", ("ck-wt-wt-1",))
        assert cur.fetchone()["n"] == 1
        cur.execute("SELECT * FROM checkouts WHERE id = %s", ("ck-wt-wt-1",))
        assert cur.fetchone() == before


def test_deleted_worktree_still_seeds(test_db):
    from tests.pg_shim import route_db_to

    with route_db_to(test_db) as shim:
        # STATUS-BLIND: a swept 'deleted' worktree still seeds a checkout — the
        # per-device sync sweep flips status spuriously; the dir is stat'd at use
        # time, which is the real check.
        _seed_project(shim, "proj-a", "repo", "/a/repo", "inst1", "git:shared")
        _seed_git_repository(shim, "repo-a", "proj-a")
        _seed_worktree(
            shim, "wt-1", "repo-a", "feature/x", "/a/repo/.wt/x", status="deleted"
        )

        database._run_column_migrations(shim.cursor())

        cur = shim.cursor()
        cur.execute("SELECT branch, path FROM checkouts WHERE id = %s", ("ck-wt-wt-1",))
        ck = cur.fetchone()
        assert ck is not None
        assert ck["branch"] == "feature/x" and ck["path"] == "/a/repo/.wt/x"


# ---------------------------------------------------------------------------
# Bare `ON CONFLICT DO NOTHING` (Steps C & E) — duplicate-path legacy rows must
# collide on the UNIQUE (project_id, runner_id, path) arbiter and be SKIPPED,
# not raise UniqueViolation and abort the whole boot. `(id)`-scoped conflict
# handling would have let the second insert reach the unique constraint and
# crash `_run_column_migrations` on EVERY boot.
# ---------------------------------------------------------------------------


def test_duplicate_worktree_paths_do_not_abort_migration(test_db):
    from tests.pg_shim import route_db_to

    with route_db_to(test_db) as shim:
        # Two worktree rows under one project sharing the SAME path + branch
        # (distinct wt ids → distinct `ck-wt-<id>` ids, but they collide on the
        # (project_id, runner_id, path) unique). Bare ON CONFLICT skips the
        # second; `(id)`-scoped would have raised and aborted the boot.
        _seed_project(shim, "proj-a", "repo", "/a/repo", "inst1", "git:shared")
        _seed_git_repository(shim, "repo-a", "proj-a")
        _seed_worktree(shim, "wt-1", "repo-a", "feature/x", "/a/repo/.wt/x")
        _seed_worktree(shim, "wt-2", "repo-a", "feature/x", "/a/repo/.wt/x")

        # Must complete without raising.
        database._run_column_migrations(shim.cursor())

        cur = shim.cursor()
        cur.execute(
            "SELECT COUNT(*) AS n FROM checkouts "
            "WHERE project_id = %s AND runner_id = %s AND path = %s",
            (origin_id("git:shared"), "inst1", "/a/repo/.wt/x"),
        )
        # Exactly one checkout row survives the collision.
        assert cur.fetchone()["n"] == 1


def test_duplicate_legacy_project_paths_do_not_abort_migration(test_db):
    from tests.pg_shim import route_db_to

    with route_db_to(test_db) as shim:
        # Two legacy `projects` rows sharing origin + instance_id + path (distinct
        # pids → distinct `ck-<id>` ids, colliding on the (project_id, runner_id,
        # path) unique). Duplicate-path legacy rows exist in the wild. Bare ON
        # CONFLICT skips the second base checkout instead of aborting the boot.
        _seed_project(shim, "proj-a", "repo", "/shared/clone", "inst1", "git:shared")
        _seed_project(shim, "proj-b", "repo", "/shared/clone", "inst1", "git:shared")

        # Must complete without raising.
        database._run_column_migrations(shim.cursor())

        cur = shim.cursor()
        cur.execute(
            "SELECT COUNT(*) AS n FROM checkouts "
            "WHERE project_id = %s AND runner_id = %s AND path = %s AND branch IS NULL",
            (origin_id("git:shared"), "inst1", "/shared/clone"),
        )
        # Exactly one base checkout row survives the collision.
        assert cur.fetchone()["n"] == 1


# ---------------------------------------------------------------------------
# Step G: duplicate projects_v2 consolidation (local: twin -> git: survivor)
# and orphan purge (local: row with no legacy projects row left anywhere).
# ---------------------------------------------------------------------------


def _seed_pv2_row(shim, origin, name):
    shim.cursor().execute(
        "INSERT INTO projects_v2 (id, origin, name, settings) VALUES (%s, %s, %s, '{}')",
        (origin_id(origin), origin, name),
    )


def test_local_twin_merges_into_git_sibling(test_db):
    from tests.pg_shim import route_db_to

    with route_db_to(test_db) as shim:
        # The wild shape: a project seeded as local:<id> pre-git-poll re-seeded
        # under git:<hash> after the remote was read. Same name, two rows.
        _seed_pv2_row(shim, "local:t3-code", "t3-code")
        _seed_pv2_row(shim, "git:t3hash", "t3-code")
        # Sessions + a branch checkout stranded on the twin.
        shim.cursor().execute(
            "INSERT INTO sessions (id, project_id, project_v2_id) VALUES (%s, %s, %s)",
            ("sess-twin", "t3-code", origin_id("local:t3-code")),
        )
        shim.cursor().execute(
            "INSERT INTO checkouts (id, project_id, runner_id, path, status) "
            "VALUES ('ck-wt-x', %s, 'r1', '/x/wt', 'active')",
            (origin_id("local:t3-code"),),
        )

        database._run_column_migrations(shim.cursor())

        rows = pv2.list_projects_v2()
        assert len(rows) == 1 and rows[0]["origin"] == "git:t3hash"

        cur = shim.cursor()
        cur.execute("SELECT project_v2_id FROM sessions WHERE id = 'sess-twin'")
        assert cur.fetchone()["project_v2_id"] == origin_id("git:t3hash")
        cur.execute("SELECT project_id FROM checkouts WHERE id = 'ck-wt-x'")
        assert cur.fetchone()["project_id"] == origin_id("git:t3hash")


def test_twin_checkout_colliding_with_survivor_is_dropped(test_db):
    from tests.pg_shim import route_db_to

    with route_db_to(test_db) as shim:
        # Both twins seeded a base checkout at the same runner+path — moving the
        # twin's would violate the survivor's UNIQUE (project_id, runner_id,
        # path), so the colliding row must be deleted instead of moved.
        _seed_pv2_row(shim, "local:t3-code", "t3-code")
        _seed_pv2_row(shim, "git:t3hash", "t3-code")
        shim.cursor().execute(
            "INSERT INTO checkouts (id, project_id, runner_id, path, status) "
            "VALUES ('ck-surv', %s, 'r1', '/x/repo', 'active')",
            (origin_id("git:t3hash"),),
        )
        shim.cursor().execute(
            "INSERT INTO checkouts (id, project_id, runner_id, path, status) "
            "VALUES ('ck-twin', %s, 'r1', '/x/repo', 'active')",
            (origin_id("local:t3-code"),),
        )

        database._run_column_migrations(shim.cursor())

        cur = shim.cursor()
        cur.execute("SELECT COUNT(*) AS n FROM checkouts")
        assert cur.fetchone()["n"] == 1
        cur.execute("SELECT project_id FROM checkouts WHERE id = 'ck-surv'")
        assert cur.fetchone()["project_id"] == origin_id("git:t3hash")


def test_distinct_name_local_rows_are_not_merged(test_db):
    from tests.pg_shim import route_db_to

    with route_db_to(test_db) as shim:
        # discord has no git sibling — it must survive untouched even though
        # other projects on this install have git origins.
        _seed_project(shim, "discord", "discord", "/d/discord", "inst1", None)
        _seed_project(shim, "repo-a", "repo", "/a/repo", "inst1", "git:shared")
        database._run_column_migrations(shim.cursor())

        origins = {r["origin"] for r in pv2.list_projects_v2()}
        assert "local:discord" in origins and "git:shared" in origins


def test_orphaned_local_row_with_no_references_is_purged(test_db):
    from tests.pg_shim import route_db_to

    with route_db_to(test_db) as shim:
        # read2me: legacy projects row deleted everywhere, zero sessions AND
        # zero checkouts — nothing references the registry row; purge it.
        _seed_pv2_row(shim, "local:read2me", "Read2Me")

        database._run_column_migrations(shim.cursor())

        assert pv2.list_projects_v2() == []


def test_orphaned_local_row_with_sessions_is_kept(test_db):
    from tests.pg_shim import route_db_to

    with route_db_to(test_db) as shim:
        # History still hangs off the row — keep it so removed-project
        # handling can surface the sessions; never strand them.
        _seed_pv2_row(shim, "local:read2me", "Read2Me")
        shim.cursor().execute(
            "INSERT INTO sessions (id, project_id, project_v2_id) VALUES (%s, %s, %s)",
            ("sess-r2m", "read2me", origin_id("local:read2me")),
        )

        database._run_column_migrations(shim.cursor())

        rows = pv2.list_projects_v2()
        assert len(rows) == 1 and rows[0]["origin"] == "local:read2me"


def test_step_g_rerun_is_zero_write(test_db):
    from tests.pg_shim import route_db_to

    with route_db_to(test_db) as shim:
        _seed_pv2_row(shim, "local:t3-code", "t3-code")
        _seed_pv2_row(shim, "git:t3hash", "t3-code")
        _seed_pv2_row(shim, "local:read2me", "Read2Me")
        shim.cursor().execute(
            "INSERT INTO sessions (id, project_id, project_v2_id) VALUES (%s, %s, %s)",
            ("sess-twin", "t3-code", origin_id("local:t3-code")),
        )

        database._run_column_migrations(shim.cursor())
        cur = shim.cursor()
        cur.execute("SELECT * FROM projects_v2 ORDER BY id")
        after_first = cur.fetchall()
        cur.execute("SELECT * FROM checkouts ORDER BY id")
        cks_first = cur.fetchall()

        database._run_column_migrations(shim.cursor())
        cur = shim.cursor()
        cur.execute("SELECT * FROM projects_v2 ORDER BY id")
        assert cur.fetchall() == after_first
        cur.execute("SELECT * FROM checkouts ORDER BY id")
        assert cur.fetchall() == cks_first
