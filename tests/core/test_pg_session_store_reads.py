"""Regression tests for the PR C.1 project_key-tolerant read path.

These run WITHOUT Postgres. ``get_db()`` is replaced by a fake connection
returning a fake cursor that RECORDS the executed SQL + params and returns
canned rows. The point is to prove the migration's load-bearing decision: the
app/detail/resume reads match on ``session_id`` (+``subpath``) ALONE and never
filter by ``project_key`` — so a row whose stored ``project_key`` is WRONG
(diverged from the cwd-derived key) is still returned.

This is the guard against the divergence bug that motivated PR C.1.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import List

import pytest

import app.core.pg_session_store as pss
from tests.fakes.db import RecordingConn, RecordingCursor


@contextmanager
def _fake_get_db(cursor: RecordingCursor):
    yield RecordingConn(cursor)


def _patch_db(monkeypatch, rows: List[dict]) -> RecordingCursor:
    cur = RecordingCursor(rows)
    monkeypatch.setattr(pss, "get_db", lambda: _fake_get_db(cur))
    return cur


# ---------------------------------------------------------------------------
# _load_entries_by_session_id_sync — no project_key predicate
# ---------------------------------------------------------------------------


def test_load_entries_main_has_no_project_key_predicate(monkeypatch):
    """Main-transcript load matches on session_id + subpath IS NULL only."""
    rows = [{"entry": {"uuid": "u0"}}, {"entry": {"uuid": "u1"}}]
    cur = _patch_db(monkeypatch, rows)

    result = pss._load_entries_by_session_id_sync("sess-123", None)

    assert result == [{"uuid": "u0"}, {"uuid": "u1"}]
    sql, params = cur.executed[-1]
    assert "project_key" not in sql
    assert "subpath IS NULL" in sql
    assert "ORDER BY id ASC" in sql
    # Only the session_id is bound — no project_key param.
    assert params == ("sess-123",)


def test_load_entries_subpath_has_no_project_key_predicate(monkeypatch):
    """Subagent load matches on session_id + subpath= only."""
    rows = [{"entry": {"uuid": "a0"}}]
    cur = _patch_db(monkeypatch, rows)

    result = pss._load_entries_by_session_id_sync("sess-123", "subagents/agent-x")

    assert result == [{"uuid": "a0"}]
    sql, params = cur.executed[-1]
    assert "project_key" not in sql
    assert "subpath = %s" in sql
    assert params == ("sess-123", "subagents/agent-x")


def test_load_entries_returns_rows_despite_wrong_stored_project_key(monkeypatch):
    """The CORE regression guard: rows are returned by session_id even though
    the stored project_key would NOT match any cwd-derived key.

    The fake cursor ignores the WHERE clause and returns canned rows, but the
    assertion that matters is that the issued SQL carries NO project_key filter,
    so in real Postgres these rows (stored under a divergent project_key) would
    still be selected.
    """
    rows = [{"entry": {"uuid": "x0"}}, {"entry": {"uuid": "x1"}}]
    cur = _patch_db(monkeypatch, rows)

    result = pss._load_entries_by_session_id_sync("sess-divergent", None)

    assert [e["uuid"] for e in result] == ["x0", "x1"]
    sql, _params = cur.executed[-1]
    assert "project_key" not in sql


# ---------------------------------------------------------------------------
# _load_sync (SDK protocol method) — ignores the key's project_key
# ---------------------------------------------------------------------------


def test_load_sync_ignores_key_project_key(monkeypatch):
    """_load_sync gets a full SDK key (with a possibly-wrong project_key) but
    must issue a session_id-only query."""
    rows = [{"entry": {"uuid": "m0"}}]
    cur = _patch_db(monkeypatch, rows)

    key = {"project_key": "WRONG--encoded--path", "session_id": "sess-9"}
    result = pss._load_sync(key)

    assert result == [{"uuid": "m0"}]
    sql, params = cur.executed[-1]
    assert "project_key" not in sql
    assert "WRONG--encoded--path" not in (params or ())
    assert params == ("sess-9",)


def test_load_sync_returns_none_when_empty(monkeypatch):
    """No rows -> None (SDK contract for a missing transcript)."""
    cur = _patch_db(monkeypatch, [])
    result = pss._load_sync({"project_key": "anything", "session_id": "missing"})
    assert result is None
    assert "project_key" not in cur.executed[-1][0]


# ---------------------------------------------------------------------------
# _list_subkeys_sync / _list_subpaths_by_session_id_sync — no project_key
# ---------------------------------------------------------------------------


def test_list_subkeys_has_no_project_key_predicate(monkeypatch):
    rows = [{"subpath": "subagents/agent-a"}, {"subpath": "subagents/agent-b"}]
    cur = _patch_db(monkeypatch, rows)

    key = {"project_key": "WRONG", "session_id": "sess-7"}
    result = pss._list_subkeys_sync(key)

    assert sorted(result) == ["subagents/agent-a", "subagents/agent-b"]
    sql, params = cur.executed[-1]
    assert "project_key" not in sql
    assert "subpath IS NOT NULL" in sql
    assert params == ("sess-7",)


def test_list_subpaths_by_session_id_no_project_key(monkeypatch):
    rows = [{"subpath": "subagents/agent-z"}]
    cur = _patch_db(monkeypatch, rows)

    result = pss._list_subpaths_by_session_id_sync("sess-7")

    assert result == ["subagents/agent-z"]
    sql, params = cur.executed[-1]
    assert "project_key" not in sql
    assert params == ("sess-7",)


# ---------------------------------------------------------------------------
# async wrappers delegate to the sync helpers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_load_entries_by_session_id(monkeypatch):
    cur = _patch_db(monkeypatch, [{"entry": {"uuid": "p0"}}])
    store = pss.PgSessionStore()
    result = await store.load_entries_by_session_id("sess-async", None)
    assert result == [{"uuid": "p0"}]
    assert "project_key" not in cur.executed[-1][0]


@pytest.mark.asyncio
async def test_async_list_subpaths_by_session_id(monkeypatch):
    cur = _patch_db(monkeypatch, [{"subpath": "subagents/agent-q"}])
    store = pss.PgSessionStore()
    result = await store.list_subpaths_by_session_id("sess-async")
    assert result == ["subagents/agent-q"]
    assert "project_key" not in cur.executed[-1][0]
