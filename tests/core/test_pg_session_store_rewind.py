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
import uuid

import pytest

# Skipped by default: this module opens a REAL Postgres pool and truncates
# session-store tables, so it must never run against the app's configured
# DATABASE_URL (commonly shared/production). Opt in with ALLOW_REAL_PG=1 against
# a disposable test database. See tests/conftest.py for the import-time guard.
pytestmark = pytest.mark.skipif(
    os.environ.get("ALLOW_REAL_PG") != "1",
    reason="ALLOW_REAL_PG!=1; session-store truncation mutates a real Postgres "
    "and is skipped by default for safety",
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
    from app.db.schema_postgres import create_postgres_schema

    with get_db() as conn:
        cur = conn.cursor()
        create_postgres_schema(cur)
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


class TestRewindResetCommitsWithTheCut:
    """A failed chat's rewind and its reset land together: no in-memory repair exists."""

    def _failed_chat(self):
        from app.db import database, turns

        chat = f"chat-{uuid.uuid4().hex}"
        database.create_profile("rewind-reset", "Rewind reset", None, {}, if_absent=True)
        database.create_session(session_id=chat, profile_id="rewind-reset")
        turn = turns.enqueue_turn(chat, "user", prompt="go")
        turns.start_turn(turn["id"])
        turns.settle_turn(turn["id"], "aborted", "error", error="boom")
        return chat, turn["id"]

    def test_a_refused_reset_takes_the_cut_back_with_it_and_a_retry_finishes_both(self, monkeypatch):
        from app.core.pg_session_store import _truncate_main_transcript_to_keep_count_sync
        from app.db import database, turns

        _insert([_user("u1"), _assistant("a1"), _user("u2"), _assistant("a2")])
        chat, turn_id = self._failed_chat()
        try:
            def refused(*args, **kwargs):
                raise RuntimeError("database away")

            with monkeypatch.context() as outage:
                outage.setattr(turns, "set_outcome_baseline", refused)
                with pytest.raises(RuntimeError, match="database away"):
                    _truncate_main_transcript_to_keep_count_sync(_SID, 2, reset_session_id=chat)

            assert len(_load()) == 4
            assert turns.sessions_with_failed_latest_turn([chat]) == {chat}

            assert _truncate_main_transcript_to_keep_count_sync(_SID, 2, reset_session_id=chat) == 2
            assert [e.get("uuid") for e in _load()] == ["u1", "a1"]
            assert turns.sessions_with_failed_latest_turn([chat]) == set()
            turn = turns.get_turn(turn_id)
            assert (turn["state"], turn["error"]) == ("aborted", "boom")
        finally:
            database.delete_session(chat)


    @pytest.mark.asyncio
    @pytest.mark.parametrize("refusal", ["reset", "target"])
    async def test_refused_store_leaves_jsonl_and_legacy_rows_untouched(
        self, monkeypatch, tmp_path, refusal,
    ):
        """A readable/writable JSONL copy is not permission to cut a refused chat."""
        import asyncio
        from unittest.mock import AsyncMock, Mock

        from app.api import commands
        from app.db import database, turns

        entries = [_user("u1", "first"), _assistant("a1"),
                   _user("u2", "second"), _assistant("a2")]
        _insert(entries)
        chat, turn_id = self._failed_chat()
        database.update_session(chat, sdk_session_id=_SID)
        for entry in entries:
            database.add_session_message(
                chat, entry["type"], entry["message"]["content"], jsonl_uuid=entry["uuid"],
            )
        transcript = tmp_path / "transcript.jsonl"
        original = "".join(json.dumps(entry) + "\n" for entry in entries).encode()
        transcript.write_bytes(original)
        before_rows = database.get_session_messages(chat)
        if refusal == "target":
            from app.core.history_backfill import ensure_backfilled
            from app.core.pg_session_store import _truncate_main_transcript_to_keep_count_sync

            # The UI/event replica still offers u2, but the authoritative store
            # no longer has it. Drive the real missing-target check, not a mock.
            await ensure_backfilled(chat)
            _truncate_main_transcript_to_keep_count_sync(_SID, 2)
        before_store = _load()
        monkeypatch.setattr(commands, "get_working_dir_for_project", lambda *args: str(tmp_path))
        monkeypatch.setattr(commands.jsonl_rewind_service, "_get_jsonl_path", lambda *args: transcript)
        sync = Mock(broadcast_user_session_updated=AsyncMock(), broadcast_session_rewound=AsyncMock())
        monkeypatch.setattr(commands, "sync_engine", sync)

        def refused(*args, **kwargs):
            raise RuntimeError("database away")

        try:
            with monkeypatch.context() as outage:
                if refusal == "reset":
                    # Fail after the DELETE, while writing the reset in that transaction.
                    outage.setattr(turns, "set_outcome_baseline", refused)
                response = await commands.execute_rewind(chat, commands.RewindRequestV2(
                    target_uuid="u2", include_response=False,
                ))
                await asyncio.sleep(0)

            assert response.success is False
            assert response.message == "Rewind failed"
            assert response.chat_rewound is False
            assert _load() == before_store
            assert (transcript.read_bytes(), database.get_session_messages(chat)) == (original, before_rows)
            assert response.messages_removed == 0
            assert "Store rewind failed:" in response.error
            assert turns.sessions_with_failed_latest_turn([chat]) == {chat}
            sync.broadcast_user_session_updated.assert_not_called()
            sync.broadcast_session_rewound.assert_not_called()

            # Retrying after the refusal is gone cuts all three copies and resets together.
            if refusal == "target":
                _insert(entries[2:])
            response = await commands.execute_rewind(chat, commands.RewindRequestV2(
                target_uuid="u2", include_response=False,
            ))
            await asyncio.sleep(0)
            assert response.success is True
            assert _load() == entries[:2]
            assert [json.loads(line) for line in transcript.read_text().splitlines()] == entries[:2]
            assert [row["jsonl_uuid"] for row in database.get_session_messages(chat)] == ["u1", "a1"]
            assert turns.sessions_with_failed_latest_turn([chat]) == set()
            turn = turns.get_turn(turn_id)
            assert (turn["state"], turn["error"]) == ("aborted", "boom")
        finally:
            database.delete_session(chat)


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
