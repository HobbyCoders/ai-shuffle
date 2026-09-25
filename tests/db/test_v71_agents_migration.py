"""v71 migration (ruling A4): profiles -> agents rename + subagents fold."""

import json
import sqlite3

import pytest


@pytest.fixture()
def old_shape_cursor():
    """An in-memory DB shaped like a v70 install (profiles + subagents)."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute(
        """CREATE TABLE profiles (
            id TEXT PRIMARY KEY, name TEXT, description TEXT,
            is_builtin BOOLEAN DEFAULT 0, config TEXT,
            mcp_tools TEXT, created_at TEXT, updated_at TEXT)"""
    )
    cur.execute(
        """CREATE TABLE subagents (
            id TEXT PRIMARY KEY, name TEXT, description TEXT, prompt TEXT,
            tools TEXT, model TEXT, skills TEXT, memory TEXT, mcp_servers TEXT,
            is_builtin BOOLEAN DEFAULT 0,
            default_name TEXT, default_description TEXT, default_prompt TEXT,
            default_tools TEXT, default_model TEXT,
            created_at TEXT, updated_at TEXT)"""
    )
    cur.execute(
        "INSERT INTO profiles (id, name, config) VALUES ('p1', 'Main', '{}')"
    )
    cur.execute(
        """INSERT INTO subagents (id, name, description, prompt, tools, model, memory)
           VALUES ('helper', 'Helper', 'helps', 'You help.',
                   '["Read","Bash"]', 'prov:codex:gpt-5', 'knows things')"""
    )
    cur.execute(
        """INSERT INTO subagents (id, name, description, prompt, model)
           VALUES ('p1', 'Collides', 'same id as a profile', 'X.', 'sonnet')"""
    )
    yield _ShimCursor(cur)
    conn.close()


class _ShimCursor:
    """%s-style paramstyle shim so the pg-flavored migration runs on sqlite."""

    def __init__(self, cur):
        self._cur = cur

    def execute(self, sql, params=None):
        self._cur.execute(sql.replace("%s", "?"), params or ())
        return self

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()


@pytest.fixture(autouse=True)
def _sqlite_table_exists(monkeypatch):
    """The migration probes tables via pg's information_schema; give it a
    sqlite_master-backed equivalent for this in-memory fixture."""
    import app.db.database as db

    def _exists(cursor, name):
        row = cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name = %s",
            (name,),
        ).fetchone()
        return row is not None

    monkeypatch.setattr(db, "table_exists", _exists)


def test_rename_and_fold(old_shape_cursor):
    from app.db.database import _migrate_profiles_to_agents

    _migrate_profiles_to_agents(old_shape_cursor)

    # profiles renamed; subagents dropped
    tables = {
        r["name"]
        for r in old_shape_cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "agents" in tables
    assert "profiles" not in tables
    assert "subagents" not in tables

    rows = {
        r["id"]: dict(r)
        for r in old_shape_cursor.execute("SELECT * FROM agents").fetchall()
    }
    # original profile survived the rename
    assert rows["p1"]["name"] == "Main"

    # folded subagent: prompt -> custom system prompt, tools -> allowed_tools,
    # memory kept, prov: composite dropped
    helper = json.loads(rows["helper"]["config"])
    assert helper["system_prompt"] == {"type": "custom", "content": "You help."}
    assert helper["allowed_tools"] == ["Read", "Bash"]
    assert helper["memory"] == "knows things"
    assert "model" not in helper  # prov: composite dropped (inert D4)

    # id collision resolved with the -agent suffix
    assert "p1-agent" in rows
    assert json.loads(rows["p1-agent"]["config"])["model"] == "sonnet"


def test_migration_is_idempotent(old_shape_cursor):
    from app.db.database import _migrate_profiles_to_agents

    _migrate_profiles_to_agents(old_shape_cursor)
    _migrate_profiles_to_agents(old_shape_cursor)  # second run: no-op

    count = old_shape_cursor.execute("SELECT COUNT(*) AS c FROM agents").fetchone()["c"]
    assert count == 3
