"""Legacy migration integration with a disposable SQLite source and fake PG sink."""
import re
import sqlite3
from typing import ClassVar

import pytest

from app.db import migrate_sqlite_to_postgres as migration


class Destination:
    columns: ClassVar[dict] = {
        "schema_version": {"version": "integer"},
        "agents": {"id": "text"},
        "projects": {"id": "text"},
        "api_users": {"id": "text", "project_id": "text"},
        "git_repositories": {"id": "text", "project_id": "text"},
        "worktrees": {"id": "text", "repository_id": "text"},
        "sessions": {"id": "text", "api_user_id": "text", "worktree_id": "text", "is_active": "boolean"},
    }

    def __init__(self):
        self.rows = {"schema_version": [{"version": 85}]}
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def cursor(self):
        return self

    def execute(self, sql, params):
        self.query = sql
        self.table = params[0]

    def fetchone(self):
        return (self.table in self.columns,)

    def fetchall(self):
        return list(self.columns[self.table].items())

    def insert(self, cursor, sql, values, **kwargs):
        table = re.search(r"INSERT INTO (\w+)", sql).group(1)
        cols = [part.strip().strip('"') for part in sql.split("(", 1)[1].split(")", 1)[0].split(",")]
        for row in values:
            record = dict(zip(cols, row))
            if table == "sessions":
                assert type(record["is_active"]) is bool
                for column, parent in (("api_user_id", "api_users"), ("worktree_id", "worktrees")):
                    assert any(parent_row["id"] == record[column] for parent_row in self.rows.get(parent, [])), "Foreign key parent missing"
            self.rows.setdefault(table, []).append(record)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1
        self.rows = {"schema_version": [{"version": 85}]}

    def close(self):
        self.closed = True


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "legacy.sqlite"
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE schema_version(version INTEGER); INSERT INTO schema_version VALUES (10);
            CREATE TABLE projects(id TEXT); INSERT INTO projects VALUES ('project');
            CREATE TABLE profiles(id TEXT); INSERT INTO profiles VALUES ('agent');
            CREATE TABLE api_users(id TEXT, project_id TEXT); INSERT INTO api_users VALUES ('user', 'project');
            CREATE TABLE git_repositories(id TEXT, project_id TEXT); INSERT INTO git_repositories VALUES ('repo', 'project');
            CREATE TABLE worktrees(id TEXT, repository_id TEXT); INSERT INTO worktrees VALUES ('tree', 'repo');
            CREATE TABLE sessions(id TEXT, api_user_id TEXT, worktree_id TEXT, is_favorite INTEGER);
            INSERT INTO sessions VALUES ('session', 'user', 'tree', 1);
        """)
    return path


@pytest.fixture
def destination(monkeypatch):
    dest = Destination()
    monkeypatch.setattr(migration.psycopg2, "connect", lambda **kwargs: dest)
    monkeypatch.setattr(migration.psycopg2.extras, "execute_values", dest.insert)
    monkeypatch.setattr(migration, "_reset_sequences", lambda conn: None)
    monkeypatch.setattr(migration, "_build_tsvectors", lambda conn: None)
    return dest


def test_migration_orders_foreign_keys_converts_bools_and_keeps_version(source, destination):
    result = migration.migrate(str(source), "postgresql://REDACTED@localhost/fixture")
    assert result["sessions"] == 1
    assert destination.rows["sessions"][0]["is_active"] is True
    assert destination.rows["schema_version"] == [{"version": 85}]
    assert destination.commits == 1
    assert destination.rollbacks == 0
    assert destination.closed


def test_failed_table_aborts_entire_import(source, destination, monkeypatch):
    original = destination.insert

    def fail_sessions(cursor, sql, values, **kwargs):
        if "INSERT INTO sessions " in sql:
            raise ValueError("Fixture rejected")
        return original(cursor, sql, values, **kwargs)

    monkeypatch.setattr(migration.psycopg2.extras, "execute_values", fail_sessions)
    with pytest.raises(RuntimeError, match="Migration failed for table 'sessions'"):
        migration.migrate(str(source), "postgresql://REDACTED@localhost/fixture")
    assert destination.commits == 0
    assert destination.rollbacks == 1
    assert "sessions" not in destination.rows
    assert destination.closed


def test_unknown_legacy_columns_fail_instead_of_disappearing(source, destination):
    with sqlite3.connect(source) as conn:
        conn.execute("ALTER TABLE sessions ADD COLUMN unknown_setting TEXT")
    with pytest.raises(RuntimeError, match="Unsupported legacy columns"):
        migration.migrate(str(source), "postgresql://REDACTED@localhost/fixture")
    assert destination.commits == 0
    assert destination.rollbacks == 1


def test_dry_run_never_connects_to_postgres(source, monkeypatch):
    def forbidden(**kwargs):
        pytest.fail("Dry run must not open a PostgreSQL connection")
    monkeypatch.setattr(migration.psycopg2, "connect", forbidden)
    assert migration.migrate(str(source), "", dry_run=True)["sessions"] == 1
