"""Schema lifecycle: fresh install vs upgrade, idempotency, data survival,
concurrent first start and crash recovery.

How the schema is versioned (see ``app.db.database.init_database``): a single
``schema_version`` row plus a ``system_settings`` fingerprint of the migration
source. Below ``SCHEMA_VERSION`` boot runs the pre-schema renames
(profiles -> agents, omni_* -> fluid_*), the idempotent
``create_postgres_schema`` bootstrap and then the guarded
``_run_column_migrations`` pass; at the current version the pass runs only
when the stored fingerprint differs. There is no per-version step list, so
"step by step from the oldest schema" means: build the schema an older commit
created, then run today's entry point on it.

Everything here runs hermetically on file-backed SQLite through the test shim
(``tests/db/schema_lifecycle_helpers.py``), except the real-PostgreSQL
concurrent-start test (marked ``boot_smoke``; see its docstring). The
old-schema fixtures are built from ``git show <commit>:app/db/...`` at test
time; those tests skip when the checkout has no history for the commit (a
shallow clone).
"""

from __future__ import annotations

import inspect
import json
import os
import re
import subprocess
import sys
import time

import pytest

from app.db import database
from tests.db import schema_lifecycle_helpers as lifecycle

#: 2026-06-29, schema v49 (about three months before this test was written).
#: Oldest fixture: predates the agents unification (v71: ``profiles`` +
#: ``subagents``), the ``sessions.is_favorite`` -> ``is_active`` rename and
#: every network table.
V49_COMMIT = "959f51cb"
#: 2026-09-15, schema v88: carries the worker-network tables.
V88_COMMIT = "4f4aa2df"

#: Column changes the migration pass makes ONLY on PostgreSQL (``ALTER COLUMN
#: .. SET DEFAULT`` / ``TYPE`` and ``DO $$`` blocks, which SQLite cannot run
#: and the shim skips). The comparison ignores type/default for these columns;
#: each entry names the statement that must stay in the migration source.
PG_ONLY_COLUMN_CHANGES = {
    ("notifications", "surface"):
        "ALTER TABLE notifications ALTER COLUMN surface SET DEFAULT 'notification'",
    ("sessions", "v2_stream"): "ALTER TABLE sessions ALTER COLUMN v2_stream SET DEFAULT TRUE",
    ("sessions", "total_tokens_in"): "ALTER COLUMN total_tokens_in TYPE BIGINT",
    ("sessions", "total_tokens_out"): "ALTER COLUMN total_tokens_out TYPE BIGINT",
}

#: Tables older releases created that today's schema no longer defines and no
#: migration drops (the rate-limit feature was removed). Upgraded databases
#: keep them, unused; a fresh one never has them.
RETIRED_TABLES_KEPT = {
    "rate_limits": ("idx_rate_limits_api_key", "idx_rate_limits_user"),
    "request_log": ("idx_request_log_api_key", "idx_request_log_timestamp",
                    "idx_request_log_user"),
}

#: Indexes only ``_run_column_migrations`` creates (the bootstrap leaves them
#: to the pass, which always follows it on boot). Databases built with
#: ``create_postgres_schema`` alone — the shared test fixtures — lack them.
COLUMN_PASS_ONLY_INDEXES = ("idx_sessions_instance", "idx_sessions_owner_device")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fresh(tmp_path, name="fresh.db"):
    raw = lifecycle.connect(tmp_path / name)
    lifecycle.boot(raw)
    return raw


def _old(tmp_path, commit, *, with_column_pass, name="old.db"):
    raw = lifecycle.connect(tmp_path / name)
    if not lifecycle.build_old_database(raw, commit, with_column_pass=with_column_pass):
        raw.close()
        message = f"git history for {commit} is not available (shallow checkout)"
        # CI checks out full history and sets this: a skip there would hide
        # a shallow checkout or a pinned commit that was garbage-collected.
        if os.environ.get("SCHEMA_LIFECYCLE_REQUIRED") == "1":
            pytest.fail(message)
        pytest.skip(message)
    return raw


def test_missing_history_fails_instead_of_skipping_when_required(tmp_path, monkeypatch):
    """R10-29: CI runs ``-q`` without ``-rs``, so a skip there went unnoticed."""
    monkeypatch.setattr(lifecycle, "build_old_database", lambda raw, commit, **kw: False)
    outcomes = []
    for required in ("1", None):
        if required:
            monkeypatch.setenv("SCHEMA_LIFECYCLE_REQUIRED", required)
        else:
            monkeypatch.delenv("SCHEMA_LIFECYCLE_REQUIRED", raising=False)
        try:
            _old(tmp_path, V49_COMMIT, with_column_pass=False, name=f"old-{required}.db")
        except BaseException as exc:  # noqa: BLE001 - pytest's outcomes are BaseException
            outcomes.append(type(exc))
    assert outcomes == [pytest.fail.Exception, pytest.skip.Exception]


def _comparable(snapshot):
    """Drop the documented PG-only and retired-table differences."""
    out = {}
    retired_indexes = {i for names in RETIRED_TABLES_KEPT.values() for i in names}
    for key, value in snapshot.items():
        kind, name = key
        if kind == "table" and name in RETIRED_TABLES_KEPT:
            continue
        if kind == "index" and name in retired_indexes:
            continue
        if kind == "table":
            columns = dict(value["columns"])
            for (table, column) in PG_ONLY_COLUMN_CHANGES:
                if table == name and column in columns:
                    _type, notnull, _default, pk = columns[column]
                    columns[column] = (None, notnull, None, pk)
            value = {"columns": columns, "fks": value["fks"]}
        out[key] = value
    return out


def _schema_diff(expected, actual):
    lines = []
    for key in sorted(set(expected) | set(actual)):
        if expected.get(key) == actual.get(key):
            continue
        if key[0] == "table" and key in expected and key in actual:
            e, a = expected[key]["columns"], actual[key]["columns"]
            for column in sorted(set(e) | set(a)):
                if e.get(column) != a.get(column):
                    lines.append(f"{key[1]}.{column}: fresh={e.get(column)} upgraded={a.get(column)}")
            if expected[key]["fks"] != actual[key]["fks"]:
                lines.append(f"{key[1]} fks: fresh={expected[key]['fks']} upgraded={actual[key]['fks']}")
        else:
            lines.append(f"{key}: fresh={expected.get(key)} upgraded={actual.get(key)}")
    return "\n".join(lines)


def _assert_same_schema(fresh_raw, other_raw):
    expected = _comparable(lifecycle.schema(fresh_raw))
    actual = _comparable(lifecycle.schema(other_raw))
    assert expected == actual, "schema drift between fresh and migrated:\n" + _schema_diff(expected, actual)


def _rows(raw, table):
    return [dict(r) for r in raw.execute(f'SELECT * FROM "{table}"')]


def _fingerprint(raw):
    row = raw.execute("SELECT value FROM system_settings WHERE key = ?",
                      (database.MIGRATIONS_FINGERPRINT_KEY,)).fetchone()
    return row[0] if row else None


def _version(raw):
    return [r[0] for r in raw.execute("SELECT version FROM schema_version")]


def _stable_dump(raw, *, exact_revisions=True):
    """Every row, minus timestamps (column names ending in ``_at``), which
    legitimately differ between two boots. ``revision`` values are random
    uuids: across two databases compare only whether each row has one."""
    out = {}
    tables = [r[0] for r in raw.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
    for table in tables:
        rows = []
        for row in _rows(raw, table):
            if not exact_revisions and "revision" in row:
                row["revision"] = row["revision"] is not None
            rows.append(repr(sorted((k, v) for k, v in row.items() if not k.endswith("_at"))))
        out[table] = sorted(rows)
    return out


# ---------------------------------------------------------------------------
# Seeds for the old fixtures (written with the old column sets)
# ---------------------------------------------------------------------------

def _insert(raw, table, row):
    columns = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    raw.execute(f"INSERT INTO {table} ({columns}) VALUES ({marks})", tuple(row.values()))


V49_SEED = {
    "profiles": [{"id": "p-old", "name": "Old profile", "description": "kept",
                  "config": json.dumps({"model": "claude-old", "max_turns": 7})}],
    "subagents": [{"id": "helper", "name": "Helper", "description": "folds into agents",
                   "prompt": "Help out.", "tools": json.dumps(["Read"])}],
    "projects": [{"id": "proj-old", "name": "Old project", "path": "/srv/old",
                  "instance_id": "inst-a"}],
    "api_users": [{"id": "user-old", "name": "Old user", "username": "olduser",
                   "api_key_hash": "hash-1", "project_id": "proj-old", "profile_id": "p-old"}],
    "sessions": [{"id": "sess-old", "project_id": "proj-old", "profile_id": "p-old",
                  "api_user_id": "user-old", "title": "Old session", "status": "idle",
                  "is_favorite": 1, "total_cost_usd": 1.5, "total_tokens_in": 1234,
                  "total_tokens_out": 567, "turn_count": 3, "instance_id": "inst-a"}],
    "session_messages": [
        {"id": 1, "session_id": "sess-old", "role": "user", "content": "hello"},
        {"id": 2, "session_id": "sess-old", "role": "assistant", "content": "hi there",
         "tool_name": "Read", "metadata": json.dumps({"k": 1})},
    ],
    "system_settings": [{"key": "theme", "value": "dark"},
                        {"key": "custom_flag", "value": "42"}],
    "automations": [{"id": "auto-old", "name": "Nightly", "trigger_type": "schedule",
                     "trigger_config": json.dumps({"cron": "0 3 * * *"}), "prompt": "Report",
                     "profile_id": "p-old", "project_id": "proj-old", "run_count": 9}],
    "connectors": [{"id": "conn-old", "type": "webhook", "name": "Hook",
                    "api_user_id": "user-old", "config": json.dumps({"secret": "s"}),
                    "is_active": 1}],
}

V88_SEED = {
    "agents": [{"id": "a-old", "name": "Old agent",
                "config": json.dumps({"model": "claude-old", "tool_selection_mode": "all"})}],
    "projects": [{"id": "proj-old", "name": "Old project", "path": "/srv/old",
                  "instance_id": "inst-a"}],
    "api_users": [{"id": "user-old", "name": "Old user", "username": "olduser",
                   "api_key_hash": "hash-1", "project_id": "proj-old", "profile_id": "a-old"}],
    "sessions": [{"id": "sess-old", "project_id": "proj-old", "profile_id": "a-old",
                  "api_user_id": "user-old", "title": "Old session", "status": "idle",
                  "is_active": 1, "total_cost_usd": 1.5, "model": "claude-old",
                  "mode": "chat", "instance_id": "inst-a"}],
    "session_messages": [
        {"id": 1, "session_id": "sess-old", "role": "user", "content": "hello"},
        {"id": 2, "session_id": "sess-old", "role": "assistant", "content": "hi there"},
    ],
    "system_settings": [{"key": "theme", "value": "dark"}],
    "automations": [{"id": "auto-old", "name": "Nightly", "trigger_type": "schedule",
                     "prompt": "Report", "profile_id": "a-old", "project_id": "proj-old"}],
    "connectors": [{"id": "conn-old", "type": "webhook", "name": "Hook",
                    "api_user_id": "user-old", "config": json.dumps({})}],
    "network_members": [{"device_id": "dev-1", "name": "Laptop", "kind": "worker",
                         "signing_public": "sig", "encryption_public": "enc",
                         "generation": 2, "last_seq_accepted": 99}],
}

#: Old table/column -> today's name.
TABLE_RENAMES = {"profiles": "agents"}
COLUMN_RENAMES = {("sessions", "is_favorite"): "is_active"}
#: (table, column) whose JSON value a migration may extend but never lose keys from.
JSON_EXTENDED = {("agents", "config"), ("profiles", "config")}


def _seed(raw, seed):
    for table, rows in seed.items():
        for row in rows:
            _insert(raw, table, row)
    raw.commit()


def _count(raw, table):
    """Rows in ``table``, leaving out the markers the migration pass itself
    records in ``system_settings``."""
    if table == "system_settings":
        return raw.execute(
            "SELECT COUNT(*) FROM system_settings WHERE key NOT LIKE 'migration:%' AND key != ?",
            (database.MIGRATIONS_FINGERPRINT_KEY,)).fetchone()[0]
    return raw.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _pk_columns(raw, table):
    return [c[1] for c in sorted(raw.execute(f"PRAGMA table_info('{table}')"), key=lambda c: c[5]) if c[5]]


def _assert_rows_survived(before, after_raw):
    """Every old row is still there with every old non-NULL value (after
    renames). NULLs may be backfilled (e.g. ``sessions.project_v2_id``)."""
    for table, rows in before.items():
        new_table = TABLE_RENAMES.get(table, table)
        pk = _pk_columns(after_raw, new_table)
        assert pk, new_table
        for old in rows:
            where = " AND ".join(f"{c} = ?" for c in pk)
            new = after_raw.execute(f"SELECT * FROM {new_table} WHERE {where}",
                                    tuple(old[c] for c in pk)).fetchone()
            assert new is not None, f"{table} row {[old[c] for c in pk]} lost"
            new = dict(new)
            for column, value in old.items():
                new_column = COLUMN_RENAMES.get((table, column), column)
                assert new_column in new, f"{table}.{column} lost"
                if value is None:
                    continue  # a later migration may backfill a NULL
                if (table, column) in JSON_EXTENDED:
                    old_obj, new_obj = json.loads(value), json.loads(new[new_column])
                    assert old_obj.items() <= new_obj.items(), (table, column, old_obj, new_obj)
                    continue
                assert new[new_column] == value, (table, column, value, new[new_column])


# ---------------------------------------------------------------------------
# (1) fresh == migrated
# ---------------------------------------------------------------------------

def test_pg_only_column_changes_are_still_in_the_migration_pass():
    """The comparison below waives type/default for these columns only because
    the PostgreSQL migration really does make them match; keep that true."""
    source = re.sub(r"\s+", " ", inspect.getsource(database._run_column_migrations))
    for (table, column), statement in PG_ONLY_COLUMN_CHANGES.items():
        assert statement in source, f"{table}.{column}: {statement!r} no longer in the pass"


def test_retired_tables_are_not_part_of_the_current_schema(tmp_path):
    fresh = _fresh(tmp_path)
    names = {r[0] for r in fresh.execute("SELECT name FROM sqlite_master")}
    for table, indexes in RETIRED_TABLES_KEPT.items():
        assert table not in names and not set(indexes) & names


@pytest.mark.parametrize("commit, with_column_pass", [
    (V49_COMMIT, True),
    (V88_COMMIT, False),
], ids=["from-v49", "from-v88"])
def test_upgraded_schema_matches_fresh_install(tmp_path, commit, with_column_pass):
    fresh = _fresh(tmp_path)
    old = _old(tmp_path, commit, with_column_pass=with_column_pass)
    assert _version(old)[0] < database.SCHEMA_VERSION

    lifecycle.boot(old)

    _assert_same_schema(fresh, old)
    assert _version(old) == [database.SCHEMA_VERSION]
    assert _fingerprint(old) == database.migrations_fingerprint()


def test_bootstrap_only_database_matches_full_boot(tmp_path):
    """The test fixtures (and ``tests/db/postgres_harness``) build databases
    with ``create_postgres_schema`` alone; it must describe the same schema a
    real boot produces, or tests run against a schema production never has."""
    from tests.pg_shim import ShimCursor
    from app.db.schema_postgres import create_postgres_schema

    fresh = _fresh(tmp_path)
    bare = lifecycle.connect(tmp_path / "bare.db")
    create_postgres_schema(ShimCursor(bare.cursor()))
    bare.commit()
    for name in COLUMN_PASS_ONLY_INDEXES:
        assert bare.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (name,)).fetchone() is None
        fresh.execute(f"DROP INDEX {name}")
    _assert_same_schema(fresh, bare)


# ---------------------------------------------------------------------------
# (2) idempotency
# ---------------------------------------------------------------------------

def test_second_boot_changes_nothing(tmp_path, monkeypatch):
    raw = _fresh(tmp_path)
    schema_before, rows_before = lifecycle.schema(raw), _stable_dump(raw)

    # Gated re-run (the normal restart) ...
    lifecycle.boot(raw)
    # ... the column pass forced ...
    monkeypatch.setenv("FORCE_DB_MIGRATIONS", "1")
    lifecycle.boot(raw)
    # ... and the whole bootstrap path again (a stale version row).
    raw.execute("DELETE FROM schema_version")
    raw.commit()
    lifecycle.boot(raw)

    assert lifecycle.schema(raw) == schema_before
    assert _stable_dump(raw) == rows_before


def test_second_boot_after_upgrade_changes_nothing(tmp_path, monkeypatch):
    old = _old(tmp_path, V49_COMMIT, with_column_pass=True)
    _seed(old, V49_SEED)
    lifecycle.boot(old)
    schema_before, rows_before = lifecycle.schema(old), _stable_dump(old)

    monkeypatch.setenv("FORCE_DB_MIGRATIONS", "1")
    lifecycle.boot(old)
    old.execute("DELETE FROM schema_version")
    old.commit()
    lifecycle.boot(old)

    assert lifecycle.schema(old) == schema_before
    assert _stable_dump(old) == rows_before


# ---------------------------------------------------------------------------
# (3) row data survives an upgrade from N releases back
# ---------------------------------------------------------------------------

def test_upgrade_from_v49_preserves_rows(tmp_path):
    old = _old(tmp_path, V49_COMMIT, with_column_pass=True)
    _seed(old, V49_SEED)
    before = {t: _rows(old, t) for t in V49_SEED}
    counts = {t: len(r) for t, r in before.items()}

    lifecycle.boot(old)

    _assert_rows_survived({t: r for t, r in before.items() if t != "subagents"}, old)
    counts["profiles"] += counts.pop("subagents")  # folded into agents
    for table, count in counts.items():
        new_table = TABLE_RENAMES.get(table, table)
        assert _count(old, new_table) == count, table
    # v71: subagents are folded into agents, keeping their prompt.
    names = {r[0] for r in old.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "subagents" not in names and "profiles" not in names
    helper = dict(old.execute("SELECT * FROM agents WHERE id = 'helper'").fetchone())
    config = json.loads(helper["config"])
    assert config["system_prompt"] == {"type": "custom", "content": "Help out."}
    # FKs follow the rename: the old session still resolves its agent.
    assert old.execute(
        "SELECT a.name FROM sessions s JOIN agents a ON a.id = s.profile_id "
        "WHERE s.id = 'sess-old'"
    ).fetchone()[0] == "Old profile"
    assert old.execute("PRAGMA foreign_key_check").fetchall() == []


def test_upgrade_from_v88_preserves_rows(tmp_path):
    old = _old(tmp_path, V88_COMMIT, with_column_pass=False)
    _seed(old, V88_SEED)
    before = {t: _rows(old, t) for t in V88_SEED}

    lifecycle.boot(old)

    _assert_rows_survived(before, old)
    for table, rows in before.items():
        assert _count(old, table) == len(rows), table
    assert old.execute("PRAGMA foreign_key_check").fetchall() == []


# ---------------------------------------------------------------------------
# (4) concurrent first start
# ---------------------------------------------------------------------------

def test_init_takes_the_schema_lock_before_anything_else(tmp_path, monkeypatch):
    raw = lifecycle.connect(tmp_path / "db.sqlite")
    seen = []
    with lifecycle.route(raw) as shim:
        cursor_class = type(shim.cursor())
        original = cursor_class.execute

        def recording(self, sql, params=None):
            seen.append((sql, params))
            return original(self, sql, params)

        monkeypatch.setattr(cursor_class, "execute", recording)
        database.init_database()
    assert "pg_advisory_xact_lock" in seen[0][0]
    assert seen[0][1] == (database.SCHEMA_INIT_LOCK_KEY,)
    assert -(2 ** 63) <= database.SCHEMA_INIT_LOCK_KEY < 2 ** 63


def test_two_processes_initialising_one_file(tmp_path):
    """Two processes boot the same empty database at once; both succeed and
    the result equals a single boot (no row written twice).

    SQLite's file lock already serializes writers, so this cannot reproduce
    the PostgreSQL race the schema lock closes (both boots see a column
    missing, both ALTER, the loser fails once the winner commits); the
    ordering test above pins the lock itself, and
    ``test_workers_booting_together_on_one_postgres`` exercises it."""
    db_file = tmp_path / "shared.db"
    flag = tmp_path / "go"
    child = os.path.join(os.path.dirname(__file__), "schema_init_child.py")
    procs = [
        subprocess.Popen([sys.executable, child, str(db_file), str(flag)],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for _ in range(2)
    ]
    try:
        flag.write_text("go")
        results = [p.communicate(timeout=100) for p in procs]
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()
    for p, (_out, err) in zip(procs, results):
        assert p.returncode == 0, err[-4000:]

    shared = lifecycle.connect(db_file)
    single = _fresh(tmp_path, "single.db")
    assert lifecycle.schema(shared) == lifecycle.schema(single)
    assert _version(shared) == [database.SCHEMA_VERSION]
    # No seed or marker row was written twice.
    assert _stable_dump(shared, exact_revisions=False) == _stable_dump(single, exact_revisions=False)


#: Workers per round and rounds (each on an emptied database) for the
#: PostgreSQL variant. With the advisory locks removed, 3 workers on a fresh
#: database fail most rounds on duplicate DDL (``pg_type_typname_nsp_index``).
PG_WORKERS = 4
PG_ROUNDS = 2


@pytest.mark.boot_smoke
@pytest.mark.timeout(300)
def test_workers_booting_together_on_one_postgres(tmp_path):
    """Several worker processes run the whole database start-up
    (``init_core``: schema, migrations, mobile tables, built-in agents and
    templates) against one EMPTY real PostgreSQL 16 database at the same
    moment. Every one must start, and the seeds must exist exactly once.

    This is the race the SQLite test above cannot show: PostgreSQL runs the
    workers' DDL and check-then-insert seeds truly concurrently, so without
    the advisory locks the losers fail on duplicate types, relations or keys.

    Needs PostgreSQL 16 server binaries (as ``tests/smoke``: ``SMOKE_PG_BIN``,
    PATH or ``/usr/lib/postgresql/16/bin``); skips without them unless
    ``BOOT_SMOKE_REQUIRED=1``. Runs serially only (CI's boot smoke step): it
    owns a real cluster and several processes, so it skips under xdist.

    The boot lock and the idempotent seeds each cover this race on their own,
    so this test stays green with either removed; the two tests after it pin
    each half."""
    bindir, data, env = _postgres_env(tmp_path)

    with lifecycle.private_postgres(bindir, data) as descriptor:
        for round_no in range(PG_ROUNDS):
            _empty_postgres(descriptor)
            _run_released_together(tmp_path, env, "--postgres", f"go-{round_no}", round_no)

            conn = lifecycle.pg_connect(descriptor)
            try:
                cur = conn.cursor()
                cur.execute("SELECT version FROM schema_version")
                assert [r["version"] for r in cur.fetchall()] == [database.SCHEMA_VERSION]
                _assert_seeded_once(cur)
                cur.execute("SELECT to_regclass('mobile_tool_commands') IS NOT NULL AS a, "
                            "to_regclass('mobile_tool_identity') IS NOT NULL AS b")
                assert dict(cur.fetchone()) == {"a": True, "b": True}
            finally:
                conn.close()


def _postgres_env(tmp_path):
    """(bindir, DATA_DIR, child env) for a real-PostgreSQL start-up test, or a
    skip (a failure under ``BOOT_SMOKE_REQUIRED=1``) without PG 16 binaries."""
    if os.environ.get("PYTEST_XDIST_WORKER"):
        pytest.skip("real-PostgreSQL start-up race: runs serially in the boot smoke step")
    bindir = lifecycle.pg16_bindir()
    if bindir is None:
        message = ("PostgreSQL 16 server binaries not found (PATH, SMOKE_PG_BIN or "
                   "/usr/lib/postgresql/16/bin)")
        if os.environ.get("BOOT_SMOKE_REQUIRED") == "1":
            pytest.fail(message)
        pytest.skip(message)

    data, workspace, home = tmp_path / "data", tmp_path / "workspace", tmp_path / "home"
    for path in (workspace, home):
        path.mkdir()
    env_file = tmp_path / "empty.env"
    env_file.write_text("")
    # An allow-list, as the boot smoke test: the test process carries
    # TESTING=1 and a DATA_DIR from tests/conftest.py.
    inherited = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "LD_LIBRARY_PATH", "PYTHONPATH")
    env = {k: v for k, v in os.environ.items() if k in inherited}
    env.update({
        "HOME": str(home), "DATA_DIR": str(data), "WORKSPACE_DIR": str(workspace),
        "AI_SHUFFLE_ENV_FILE": str(env_file), "TESTING": "0", "PYTHONDONTWRITEBYTECODE": "1",
    })
    return bindir, data, env


_CHILD = os.path.join(os.path.dirname(__file__), "schema_init_child.py")


def _empty_postgres(descriptor):
    admin = lifecycle.pg_connect(descriptor)
    try:
        admin.cursor().execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
    finally:
        admin.close()


def _run_released_together(tmp_path, env, mode, flag_name, round_no, workers=PG_WORKERS):
    """Start ``workers`` children in ``mode``, release them at once when all
    have imported the app, and assert every one exited 0."""
    flag = tmp_path / flag_name
    procs = [
        subprocess.Popen([sys.executable, _CHILD, mode, str(flag)], env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for _ in range(workers)
    ]
    try:
        deadline = time.monotonic() + 120
        while len(list(tmp_path.glob(f"{flag_name}.ready.*"))) < workers:
            if any(p.poll() is not None for p in procs) or time.monotonic() > deadline:
                break
            time.sleep(0.05)
        flag.write_text("go")
        results = [p.communicate(timeout=180) for p in procs]
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()
    failures = [f"worker {i} exit {p.returncode}:\n{err[-3000:]}"
                for i, (p, (_out, err)) in enumerate(zip(procs, results))
                if p.returncode != 0]
    assert not failures, f"round {round_no}: " + "\n\n".join(failures)


def _assert_seeded_once(cur):
    from app.core.builtin_subagents import BUILTIN_SUBAGENTS
    from app.core.profiles import DEFAULT_TEMPLATES

    cur.execute("SELECT id FROM agents WHERE is_builtin ORDER BY id")
    assert [r["id"] for r in cur.fetchall()] == sorted(d["id"] for d in BUILTIN_SUBAGENTS)
    cur.execute("SELECT id FROM templates ORDER BY id")
    assert [r["id"] for r in cur.fetchall()] == sorted(t["id"] for t in DEFAULT_TEMPLATES)


@pytest.mark.boot_smoke
@pytest.mark.timeout(300)
def test_the_boot_lock_is_held_through_seeding_on_postgres(tmp_path):
    """R11-11 (lock half): a worker stopped inside start-up, where it seeds
    the built-in agents, still holds the boot lock, seen from a second
    connection. The concurrent-start test above passed with the lock replaced
    by a no-op, because the idempotent seeds covered for it; the migrations,
    the mobile-tool DDL and the notes and attachment passes have only the
    lock."""
    bindir, data, env = _postgres_env(tmp_path)
    flag = tmp_path / "hold"
    seeding, release = tmp_path / "hold.seeding", tmp_path / "hold.release"

    with lifecycle.private_postgres(bindir, data) as descriptor:
        proc = subprocess.Popen([sys.executable, _CHILD, "--postgres-hold", str(flag)], env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 120
            while not seeding.exists() and proc.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            reached = seeding.exists()
            held_while_seeding = None
            if reached:
                conn = lifecycle.pg_connect(descriptor)
                try:
                    cur = conn.cursor()
                    cur.execute("SELECT pg_try_advisory_xact_lock(%s) AS got",
                                (database.BOOT_INIT_LOCK_KEY,))
                    held_while_seeding = not cur.fetchone()["got"]
                finally:
                    conn.close()
            release.write_text("go")
            _out, err = proc.communicate(timeout=180)
        finally:
            if proc.poll() is None:
                proc.kill()
        assert reached, f"the worker never reached seeding (exit {proc.returncode}):\n{err[-3000:]}"
        assert proc.returncode == 0, err[-3000:]
        assert held_while_seeding is True

        conn = lifecycle.pg_connect(descriptor)
        try:
            cur = conn.cursor()
            # Released with the worker's start-up transaction.
            cur.execute("SELECT pg_try_advisory_xact_lock(%s) AS got", (database.BOOT_INIT_LOCK_KEY,))
            assert cur.fetchone()["got"] is True
            _assert_seeded_once(cur)
        finally:
            conn.close()


@pytest.mark.boot_smoke
@pytest.mark.timeout(300)
def test_seeders_racing_without_the_boot_lock_on_postgres(tmp_path):
    """R11-11 (seed half): the start-up seeders run by several workers at
    once with NO boot lock still create each built-in agent and template
    exactly once, and none of them fails. The concurrent-start test above
    passed with the seeds' ``ON CONFLICT DO NOTHING`` removed, because the
    boot lock serialized them."""
    bindir, data, env = _postgres_env(tmp_path)

    with lifecycle.private_postgres(bindir, data) as descriptor:
        _empty_postgres(descriptor)
        # One ordinary start creates the schema (and seeds, removed below).
        _run_released_together(tmp_path, env, "--postgres", "schema", "schema", workers=1)
        for round_no in range(PG_ROUNDS + 1):
            conn = lifecycle.pg_connect(descriptor)
            try:
                cur = conn.cursor()
                cur.execute("DELETE FROM agents WHERE is_builtin")
                cur.execute("DELETE FROM templates")
            finally:
                conn.close()

            _run_released_together(tmp_path, env, "--postgres-seed", f"seed-{round_no}", round_no)

            conn = lifecycle.pg_connect(descriptor)
            try:
                _assert_seeded_once(conn.cursor())
            finally:
                conn.close()


# ---------------------------------------------------------------------------
# (5) partially applied migrations
# ---------------------------------------------------------------------------

def _strip_to_v78(raw):
    """A database that is missing later additive columns, with one of v79's
    three run-binding columns already present (a half-applied step)."""
    raw.execute("DROP INDEX IF EXISTS idx_usage_log_session")
    raw.execute("ALTER TABLE sessions DROP COLUMN is_worker")
    raw.execute("ALTER TABLE session_store_summaries DROP COLUMN folded_through_id")
    raw.execute("ALTER TABLE sessions DROP COLUMN effort")
    raw.execute("ALTER TABLE sessions DROP COLUMN permission_mode")
    raw.execute("DELETE FROM schema_version")
    raw.execute("INSERT INTO schema_version (version) VALUES (78)")
    raw.execute("DELETE FROM system_settings WHERE key = ?", (database.MIGRATIONS_FINGERPRINT_KEY,))
    raw.commit()


def _seed_current(raw):
    raw.execute("INSERT INTO agents (id, name, config) VALUES ('a1', 'A', '{}')")
    raw.execute("INSERT INTO sessions (id, profile_id, title, model) "
                "VALUES ('s1', 'a1', 'kept', 'claude-x')")
    raw.execute("INSERT INTO session_messages (session_id, role, content) "
                "VALUES ('s1', 'user', 'hello')")
    raw.commit()


def test_half_applied_step_is_completed_on_next_start(tmp_path):
    fresh = _fresh(tmp_path)
    raw = _fresh(tmp_path, "partial.db")
    _seed_current(raw)
    _strip_to_v78(raw)

    lifecycle.boot(raw)

    _assert_same_schema(fresh, raw)
    assert _version(raw) == [database.SCHEMA_VERSION]
    assert raw.execute("SELECT model FROM sessions WHERE id = 's1'").fetchone()[0] == "claude-x"


@pytest.mark.parametrize("commit_before_crash", [False, True],
                         ids=["rolled-back", "alter-committed"])
def test_crash_after_one_alter_recovers_on_next_start(tmp_path, monkeypatch, commit_before_crash):
    """Kill the migration right after its first ADD COLUMN. Rolled back (what
    PostgreSQL does: one transaction), nothing is stamped. Committed (the worst
    case: a driver or operator that persisted the half), the version row may be
    current but the fingerprint is not, so the next start re-runs the pass."""
    fresh = _fresh(tmp_path)
    raw = _fresh(tmp_path, "crash.db")
    _seed_current(raw)
    _strip_to_v78(raw)
    rows_before = _rows(raw, "session_messages")

    class Crash(RuntimeError):
        pass

    with lifecycle.route(raw) as shim:
        cursor_class = type(shim.cursor())
        original = cursor_class.execute

        def crashing(self, sql, params=None):
            result = original(self, sql, params)
            if re.search(r"ALTER\s+TABLE\s+\w+\s+ADD\s+COLUMN", sql, re.I):
                if commit_before_crash:
                    raw.commit()
                raise Crash(sql)
            return result

        monkeypatch.setattr(cursor_class, "execute", crashing)
        with pytest.raises(Crash):
            database.init_database()

    assert _fingerprint(raw) is None
    if not commit_before_crash:
        assert _version(raw) == [78]
    assert _rows(raw, "session_messages") == rows_before

    lifecycle.boot(raw)

    _assert_same_schema(fresh, raw)
    assert _version(raw) == [database.SCHEMA_VERSION]
    assert _fingerprint(raw) == database.migrations_fingerprint()
    assert _rows(raw, "session_messages") == rows_before
    assert raw.execute("SELECT title FROM sessions WHERE id = 's1'").fetchone()[0] == "kept"
