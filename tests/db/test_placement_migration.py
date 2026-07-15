"""Unit tests for the V2 Phase 4 (v61) placement migration in
app/db/database.py._run_column_migrations.

Step A adds the additive columns (sessions.placement, sessions.working_set_ref,
artifacts.runner_id). Step B backfills `sessions.placement` under the A9 rule
(07-14): EVERY session with a non-empty `instance_id` — self-owned included — is
pinned to its owner (runner:<id>); this depends only on row data, so it is
deterministic/boot-order-independent, and it preserves stickiness (OD-6) since
the owner's disk already holds the CLI state. There is NO "anywhere" arm —
"anywhere" is valid only for NEW sessions before their first claim.

These tests exercise the FULL `_run_column_migrations` pipeline, so the
pre-existing v40 instance_id backfill (~database.py:372-385) runs BEFORE Step B:
a session seeded with instance_id=None is first adopted by the migrating
install (v40's long-established local-adoption semantic) and then pinned to it
by Step B. The only rows that keep NULL placement are those with an
empty-string instance_id (the legacy sentinel — skipped by both v40's IS NULL
predicate and Step B's <> '' predicate). The backfill is additive, idempotent,
and Step B itself never writes a legacy column. Modeled on the v60
origin-collapse migration test."""

import json

import pytest

from app.db import database


RUNNER = "me-install"


@pytest.fixture
def test_db():
    from tests.pg_shim import new_sqlite_connection
    conn = new_sqlite_connection()
    # The backfill touches only sessions; the FK to projects is irrelevant here.
    conn.execute("PRAGMA foreign_keys = OFF")
    yield conn
    conn.close()


@pytest.fixture(autouse=True)
def pin_runner(monkeypatch):
    """Pin effective_instance_id so "this install" is deterministic in Step B."""
    from app.core.config import settings
    monkeypatch.setattr(
        type(settings), "effective_instance_id", property(lambda self: RUNNER)
    )


def _seed_session(
    shim, sid, instance_id=None, branch=None, worktree_branch=None, placement=None
):
    # `placement` exists in the base schema (v61); pass it explicitly so a
    # pre-set placement can be tested for the idempotency guard.
    shim.cursor().execute(
        "INSERT INTO sessions (id, instance_id, branch, worktree_branch, placement) "
        "VALUES (%s, %s, %s, %s, %s)",
        (sid, instance_id, branch, worktree_branch, placement),
    )


def _placement_of(shim, sid):
    cur = shim.cursor()
    cur.execute("SELECT placement FROM sessions WHERE id = %s", (sid,))
    raw = cur.fetchone()["placement"]
    if raw is None:
        return None
    return json.loads(raw) if isinstance(raw, str) else raw


def _count(shim, table):
    cur = shim.cursor()
    cur.execute(f"SELECT COUNT(*) AS n FROM {table}")
    return cur.fetchone()["n"]


# ---------------------------------------------------------------------------
# Step A — additive columns.
# ---------------------------------------------------------------------------

def test_columns_added(test_db):
    from tests.pg_shim import route_db_to
    from app.db.base import column_exists

    with route_db_to(test_db) as shim:
        database._run_column_migrations(shim.cursor())
        cur = shim.cursor()
        assert column_exists(cur, "sessions", "placement")
        assert column_exists(cur, "sessions", "working_set_ref")
        assert column_exists(cur, "artifacts", "runner_id")


# ---------------------------------------------------------------------------
# Step B — placement backfill.
# ---------------------------------------------------------------------------

def test_remote_owned_session_is_runner_pinned(test_db):
    from tests.pg_shim import route_db_to

    with route_db_to(test_db) as shim:
        _seed_session(shim, "sess-remote", instance_id="other-box")
        database._run_column_migrations(shim.cursor())
        assert _placement_of(shim, "sess-remote") == {"where": "runner:other-box"}


def test_own_install_session_is_runner_pinned(test_db):
    from tests.pg_shim import route_db_to

    with route_db_to(test_db) as shim:
        # A9: self-owned sessions ARE pinned — the rule keys on row data only
        # (non-empty instance_id), so this install's own sessions pin to itself.
        # This is what makes the backfill deterministic/boot-order-independent.
        _seed_session(shim, "sess-mine", instance_id=RUNNER)
        database._run_column_migrations(shim.cursor())
        assert _placement_of(shim, "sess-mine") == {"where": f"runner:{RUNNER}"}


def test_null_instance_id_adopted_by_v40_then_pinned(test_db):
    from tests.pg_shim import route_db_to

    with route_db_to(test_db) as shim:
        # Pipeline reality: instance_id=None is first adopted locally by the
        # v40 backfill (runs before Step B in the same function), so Step B
        # then pins the row to the migrating install. On any real >=v40 DB no
        # NULL instance_id rows exist to begin with.
        _seed_session(shim, "sess-local")
        database._run_column_migrations(shim.cursor())
        assert _placement_of(shim, "sess-local") == {"where": f"runner:{RUNNER}"}


def test_branch_session_without_instance_id_adopted_then_pinned(test_db):
    from tests.pg_shim import route_db_to

    with route_db_to(test_db) as shim:
        # A9: no "anywhere" arm. An ownerless branch session is adopted by the
        # v40 backfill and pinned locally by Step B — never tagged "anywhere"
        # ("anywhere" is only for NEW sessions pre-first-claim).
        _seed_session(shim, "sess-branch", branch="feature/x")
        database._run_column_migrations(shim.cursor())
        assert _placement_of(shim, "sess-branch") == {"where": f"runner:{RUNNER}"}


def test_worktree_branch_session_without_instance_id_adopted_then_pinned(test_db):
    from tests.pg_shim import route_db_to

    with route_db_to(test_db) as shim:
        _seed_session(shim, "sess-wt", worktree_branch="feature/y")
        database._run_column_migrations(shim.cursor())
        assert _placement_of(shim, "sess-wt") == {"where": f"runner:{RUNNER}"}


def test_branch_session_with_instance_id_pins_to_owner(test_db):
    from tests.pg_shim import route_db_to

    with route_db_to(test_db) as shim:
        # A9: a branch session that has an owner pins to that owner (stickiness),
        # NOT to "anywhere".
        _seed_session(shim, "sess-both", instance_id="other-box", branch="feature/z")
        database._run_column_migrations(shim.cursor())
        assert _placement_of(shim, "sess-both") == {"where": "runner:other-box"}


def test_empty_instance_id_is_not_pinned(test_db):
    from tests.pg_shim import route_db_to

    with route_db_to(test_db) as shim:
        # Empty-string instance_id is the legacy sentinel, not a real owner.
        # It is the one true stays-NULL case: skipped by v40's IS NULL adoption
        # AND excluded by Step B's <> '' predicate -> placement stays NULL
        # (the local-default).
        _seed_session(shim, "sess-empty", instance_id="")
        database._run_column_migrations(shim.cursor())
        assert _placement_of(shim, "sess-empty") is None


def test_preexisting_placement_never_clobbered(test_db):
    from tests.pg_shim import route_db_to

    with route_db_to(test_db) as shim:
        # A hand-set placement must survive (Step B is WHERE placement IS NULL),
        # even though the row would otherwise pin to its remote owner.
        preset = json.dumps({"where": "runner:hand-picked"})
        _seed_session(shim, "sess-set", instance_id="other-box", placement=preset)
        database._run_column_migrations(shim.cursor())
        assert _placement_of(shim, "sess-set") == {"where": "runner:hand-picked"}


# ---------------------------------------------------------------------------
# Idempotency + legacy safety.
# ---------------------------------------------------------------------------

def test_rerun_is_zero_write_and_identical(test_db):
    from tests.pg_shim import route_db_to

    with route_db_to(test_db) as shim:
        _seed_session(shim, "sess-remote", instance_id="other-box")
        _seed_session(shim, "sess-branch", branch="feature/x")  # v40-adopted -> local pin
        _seed_session(shim, "sess-local")  # v40-adopted -> local pin
        _seed_session(shim, "sess-empty", instance_id="")  # stays NULL

        database._run_column_migrations(shim.cursor())

        def _snapshot():
            cur = shim.cursor()
            cur.execute(
                "SELECT id, instance_id, branch, worktree_branch, placement "
                "FROM sessions ORDER BY id"
            )
            return cur.fetchall()

        before = _snapshot()
        database._run_column_migrations(shim.cursor())
        after = _snapshot()

        assert before == after
        # Values still exactly as expected after the second boot.
        assert _placement_of(shim, "sess-remote") == {"where": "runner:other-box"}
        assert _placement_of(shim, "sess-branch") == {"where": f"runner:{RUNNER}"}
        assert _placement_of(shim, "sess-local") == {"where": f"runner:{RUNNER}"}
        assert _placement_of(shim, "sess-empty") is None


def test_legacy_columns_untouched(test_db):
    from tests.pg_shim import route_db_to

    with route_db_to(test_db) as shim:
        _seed_session(shim, "sess-remote", instance_id="other-box", worktree_branch="wt/a")
        _seed_session(shim, "sess-local", instance_id=RUNNER)

        cur = shim.cursor()
        cur.execute(
            "SELECT id, instance_id, worktree_branch FROM sessions ORDER BY id"
        )
        before = cur.fetchall()

        database._run_column_migrations(shim.cursor())

        cur = shim.cursor()
        cur.execute(
            "SELECT id, instance_id, worktree_branch FROM sessions ORDER BY id"
        )
        after = cur.fetchall()
        # instance_id / worktree_branch never rewritten by the placement backfill.
        assert before == after


def test_empty_sessions_is_noop(test_db):
    from tests.pg_shim import route_db_to

    with route_db_to(test_db) as shim:
        database._run_column_migrations(shim.cursor())
        assert _count(shim, "sessions") == 0
