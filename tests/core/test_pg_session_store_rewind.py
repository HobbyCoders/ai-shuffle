"""
Tests for session-store truncation on rewind (``app.core.pg_session_store``).

Bug #2 of the chat-rewind fix: in ``postgres_only`` mode the UI reads the
``session_store_entries`` table, but rewind only ever truncated the JSONL file
and the legacy ``session_messages`` table — so a rewind reported success while
the message stayed in the chat (it was still in the store the reload re-read).

``truncate_session_store_to_checkpoint`` now cuts the stored main transcript at
the exact same point as the JSONL (via the shared
``compute_rewind_keep_count``). These tests exercise that against a real
Postgres instance and are skipped when ``DATABASE_URL`` is unset — the store is
Postgres-specific (JSONB + partial indexes) by design.
"""

from __future__ import annotations

import json
import os

import pytest

DATABASE_URL = os.environ.get("DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="DATABASE_URL not set; session-store truncation requires Postgres",
)


# ─── fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def pg_pool():
    from app.db import base

    if base._pg_pool is None:
        base.init_pool(min_conn=1, max_conn=4)
    yield


@pytest.fixture(scope="module", autouse=True)
def _schema(pg_pool):
    from app.db.base import get_db

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS session_store_entries (
                id BIGSERIAL PRIMARY KEY,
                project_key TEXT NOT NULL,
                session_id TEXT NOT NULL,
                subpath TEXT,
                entry JSONB NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )


# A session id unique to this module so we never touch real data and tests
# don't collide with each other.
_SID = "rewind-test-session-0001"


@pytest.fixture(autouse=True)
def _clean_session():
    """Wipe this test session's rows before and after each test."""
    from app.db.base import get_db

    def _wipe():
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                "DELETE FROM session_store_entries WHERE session_id = %s", (_SID,)
            )

    _wipe()
    yield
    _wipe()


# ─── helpers ──────────────────────────────────────────────────────────────────

def _insert(entries, *, subpath=None, session_id=_SID, project_key="proj-x"):
    """Insert raw transcript dicts as store rows in order."""
    from app.db.base import get_db

    with get_db() as conn:
        cur = conn.cursor()
        for entry in entries:
            cur.execute(
                "INSERT INTO session_store_entries "
                "(project_key, session_id, subpath, entry) "
                "VALUES (%s, %s, %s, %s::jsonb)",
                (project_key, session_id, subpath, json.dumps(entry)),
            )


def _user(uuid, text="hi"):
    return {"uuid": uuid, "type": "user",
            "message": {"role": "user", "content": text}}


def _assistant(uuid, text="ok"):
    return {"uuid": uuid, "type": "assistant",
            "message": {"role": "assistant", "content": text}}


def _load():
    from app.core.pg_session_store import _load_entries_by_session_id_sync
    return _load_entries_by_session_id_sync(_SID, None)


# ─── _truncate_main_transcript_to_keep_count_sync ────────────────────────────

class TestTruncatePrimitive:
    def test_keep_subset_deletes_tail(self):
        from app.core.pg_session_store import (
            _truncate_main_transcript_to_keep_count_sync,
        )
        _insert([_user("u1"), _assistant("a1"), _user("u2"), _assistant("a2")])
        removed = _truncate_main_transcript_to_keep_count_sync(_SID, 2)
        assert removed == 2
        assert [e.get("uuid") for e in _load()] == ["u1", "a1"]

    def test_keep_all_deletes_nothing(self):
        from app.core.pg_session_store import (
            _truncate_main_transcript_to_keep_count_sync,
        )
        _insert([_user("u1"), _assistant("a1")])
        removed = _truncate_main_transcript_to_keep_count_sync(_SID, 5)
        assert removed == 0
        assert len(_load()) == 2

    def test_keep_zero_deletes_all(self):
        from app.core.pg_session_store import (
            _truncate_main_transcript_to_keep_count_sync,
        )
        _insert([_user("u1"), _assistant("a1")])
        removed = _truncate_main_transcript_to_keep_count_sync(_SID, 0)
        assert removed == 2
        assert _load() == []

    def test_subagent_transcripts_untouched(self):
        from app.core.pg_session_store import (
            _truncate_main_transcript_to_keep_count_sync,
        )
        _insert([_user("u1"), _assistant("a1"), _user("u2")])
        _insert([_assistant("sub1")], subpath="agent-xyz")
        _truncate_main_transcript_to_keep_count_sync(_SID, 1)
        # Main transcript truncated...
        assert [e.get("uuid") for e in _load()] == ["u1"]
        # ...but the subagent transcript (subpath IS NOT NULL) is preserved.
        from app.core.pg_session_store import _load_entries_by_session_id_sync
        sub = _load_entries_by_session_id_sync(_SID, "agent-xyz")
        assert [e.get("uuid") for e in sub] == ["sub1"]


# ─── truncate_session_store_to_checkpoint (async, via compute_rewind_keep_count)

@pytest.mark.asyncio
class TestTruncateToCheckpoint:
    async def test_rewind_second_user_removes_it_and_after(self):
        from app.core.pg_session_store import truncate_session_store_to_checkpoint
        _insert([_user("u1", "hello 1"), _assistant("a1"),
                 _user("u2", "hello 2"), _assistant("a2")])
        removed = await truncate_session_store_to_checkpoint(
            _SID, "u2", include_response=False, target_role="user"
        )
        assert removed == 2
        assert [e.get("uuid") for e in _load()] == ["u1", "a1"]

    async def test_missing_target_raises(self):
        from app.core.pg_session_store import (
            RewindTargetNotFound,
            truncate_session_store_to_checkpoint,
        )
        _insert([_user("u1"), _assistant("a1")])
        with pytest.raises(RewindTargetNotFound):
            await truncate_session_store_to_checkpoint(
                _SID, "nope", include_response=False, target_role="user"
            )

    async def test_empty_store_raises(self):
        from app.core.pg_session_store import (
            RewindTargetNotFound,
            truncate_session_store_to_checkpoint,
        )
        with pytest.raises(RewindTargetNotFound):
            await truncate_session_store_to_checkpoint(
                _SID, "u1", include_response=False, target_role="user"
            )
