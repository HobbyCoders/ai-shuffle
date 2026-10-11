"""
Tests for ``app.db.sessions.delete_orphan_user_turn``.

When a user presses Stop before the model produces any output, the prompt was
already eagerly persisted to ``session_messages`` (it is written before the SDK
runs). Under ``SESSION_STORE_MODE=postgres_only`` the durable store is the
source of truth the ``/live`` poller reads once streaming ends, so that lone
trailing ``user`` row re-surfaces in the chat. ``delete_orphan_user_turn``
removes just that row.

Critical invariant: the SESSION ROW is left intact. A worktree-backed session
that gets stopped on its first turn must keep its worktree link, so deleting the
session is never acceptable here — only the orphan message goes.
"""

import sqlite3
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from app.db import base
from app.db import database as db
from app.db import sessions as sessions_mod
from tests.pg_shim import ShimConnection


# =============================================================================
# Fixtures (mirrors tests/db/test_sessions_rewind.py)
# =============================================================================

def _create_schema(cursor: sqlite3.Cursor) -> None:
    cursor.execute(
        """
        CREATE TABLE agents (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            config JSON NOT NULL DEFAULT '{}',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            profile_id TEXT NOT NULL,
            last_user_message_at TIMESTAMP,
            last_user_message_seq BIGINT,
            last_user_message_epoch TEXT,
            api_user_id TEXT,
            title TEXT,
            status TEXT DEFAULT 'active',
            worktree_id TEXT,
            sdk_session_id TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    # SDK transcript mirror (postgres_only read source). ``session_id`` here is
    # the SDK session UUID — NOT sessions.id. Mirror just the columns the cleanup
    # touches; ``entry`` is the raw SDK line (JSON text in sqlite).
    cursor.execute(
        """
        CREATE TABLE session_store_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_key TEXT NOT NULL,
            session_id TEXT NOT NULL,
            subpath TEXT,
            entry TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE session_store_summaries (
            project_key TEXT NOT NULL,
            session_id TEXT NOT NULL,
            data TEXT NOT NULL,
            mtime_ms INTEGER NOT NULL,
            PRIMARY KEY (project_key, session_id)
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE session_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            tool_name TEXT,
            tool_input JSON,
            metadata JSON,
            profile_id TEXT,
            jsonl_uuid TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        )
        """
    )


@pytest.fixture
def test_db():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    _create_schema(conn.cursor())
    conn.execute(
        "CREATE TABLE composer_suggestions (session_id TEXT PRIMARY KEY, "
        "turn_id TEXT NOT NULL, suggestion TEXT NOT NULL, "
        "FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE)"
    )
    conn.commit()
    yield conn
    conn.close()


@pytest.fixture
def mock_db(test_db):
    # Use the real commit boundary, including after-commit notifications.
    mock_get_db = base.get_db
    shim = ShimConnection(test_db)

    def mock_get_cursor(conn):
        return conn.cursor()

    with patch.object(base, "get_connection", lambda: shim), \
         patch.object(base, "_close_connection", lambda conn: None), \
         patch.object(sessions_mod, "get_db", mock_get_db), \
         patch.object(sessions_mod, "get_cursor", mock_get_cursor), \
         patch.object(sessions_mod, "ph", lambda: "?"), \
         patch.object(db, "get_db", mock_get_db):
        now = datetime.now(timezone.utc).isoformat()
        test_db.execute(
            "INSERT INTO agents (id, name, config) VALUES (?, ?, ?)",
            ("p1", "Test", "{}"),
        )
        # s1: plain session.  s2: worktree-backed session (worktree_id set) used
        # to prove the session row + its worktree link survive the cleanup.
        test_db.execute(
            "INSERT INTO sessions (id, profile_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?)",
            ("s1", "p1", now, now),
        )
        test_db.execute(
            "INSERT INTO sessions (id, profile_id, worktree_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("s2", "p1", "wt-123", now, now),
        )
        test_db.commit()
        yield test_db


def _session_exists(test_db, session_id: str) -> bool:
    cur = test_db.execute("SELECT COUNT(*) FROM sessions WHERE id = ?", (session_id,))
    return cur.fetchone()[0] == 1


def _worktree_id(test_db, session_id: str):
    cur = test_db.execute("SELECT worktree_id FROM sessions WHERE id = ?", (session_id,))
    row = cur.fetchone()
    return row[0] if row else None


# =============================================================================
# Tests
# =============================================================================

class TestDeleteOrphanUserTurn:
    def test_deletes_lone_trailing_user_message(self, mock_db):
        """The classic case: send 'hello', Stop before output -> orphan removed."""
        db.add_session_message(session_id="s1", role="user", content="hello")

        discarded = db.delete_orphan_user_turn("s1")

        assert discarded == "hello"
        assert db.get_session_messages("s1") == []

    def test_keeps_session_row(self, mock_db):
        """Only the message is deleted — the session row must survive."""
        db.add_session_message(session_id="s1", role="user", content="hello")

        db.delete_orphan_user_turn("s1")

        assert _session_exists(mock_db, "s1") is True

    def test_preserves_worktree_link(self, mock_db):
        """A worktree session stopped on its first turn keeps its worktree_id."""
        db.add_session_message(session_id="s2", role="user", content="do the thing")

        discarded = db.delete_orphan_user_turn("s2")

        assert discarded == "do the thing"
        assert _session_exists(mock_db, "s2") is True
        assert _worktree_id(mock_db, "s2") == "wt-123"

    def test_no_delete_when_trailing_message_is_assistant(self, mock_db):
        """A turn that produced a response must be kept intact."""
        db.add_session_message(session_id="s1", role="user", content="hello")
        db.add_session_message(session_id="s1", role="assistant", content="hi there")

        discarded = db.delete_orphan_user_turn("s1")

        assert discarded is None
        assert [r["content"] for r in db.get_session_messages("s1")] == ["hello", "hi there"]

    def test_deletes_only_latest_orphan_keeping_prior_complete_turn(self, mock_db):
        """u1/a1 is a complete turn; u2 is the stopped orphan -> drop only u2."""
        db.add_session_message(session_id="s1", role="user", content="u1")
        db.add_session_message(session_id="s1", role="assistant", content="a1")
        db.add_session_message(session_id="s1", role="user", content="u2")

        discarded = db.delete_orphan_user_turn("s1")

        assert discarded == "u2"
        assert [r["content"] for r in db.get_session_messages("s1")] == ["u1", "a1"]

    def test_ignores_task_notification_user_row(self, mock_db):
        """A <task-notification> callback row is not a real prompt — never delete it."""
        db.add_session_message(
            session_id="s1",
            role="user",
            content="<task-notification>background agent finished</task-notification>",
        )

        discarded = db.delete_orphan_user_turn("s1")

        assert discarded is None
        assert len(db.get_session_messages("s1")) == 1

    def test_empty_session_returns_none(self, mock_db):
        assert db.delete_orphan_user_turn("s1") is None

    def test_scoped_to_session_id(self, mock_db):
        """Cleanup on one session must never touch another session's messages."""
        db.add_session_message(session_id="s1", role="user", content="s1-orphan")
        db.add_session_message(session_id="s2", role="user", content="s2-keep")

        discarded = db.delete_orphan_user_turn("s1")

        assert discarded == "s1-orphan"
        assert db.get_session_messages("s1") == []
        assert [r["content"] for r in db.get_session_messages("s2")] == ["s2-keep"]


# =============================================================================
# discard_orphan_first_turn — the postgres_only-aware first-turn revert.
#
# Under postgres_only the chat is read back from session_store_entries (the SDK
# transcript), so reverting a first-turn orphan must wipe that transcript and
# unlink sdk_session_id — not just drop the session_messages row.
# =============================================================================

def _set_sdk_session_id(test_db, session_id: str, sdk_session_id):
    test_db.execute(
        "UPDATE sessions SET sdk_session_id = ? WHERE id = ?",
        (sdk_session_id, session_id),
    )
    test_db.commit()


def _sdk_session_id(test_db, session_id: str):
    cur = test_db.execute("SELECT sdk_session_id FROM sessions WHERE id = ?", (session_id,))
    row = cur.fetchone()
    return row[0] if row else None


def _add_store_entry(test_db, sdk_session_id: str, role: str, subpath=None):
    test_db.execute(
        "INSERT INTO session_store_entries (project_key, session_id, subpath, entry) "
        "VALUES (?, ?, ?, ?)",
        ("proj", sdk_session_id, subpath, f'{{"role": "{role}"}}'),
    )
    test_db.commit()


def _add_store_summary(test_db, sdk_session_id: str):
    test_db.execute(
        "INSERT INTO session_store_summaries (project_key, session_id, data, mtime_ms) "
        "VALUES (?, ?, ?, ?)",
        ("proj", sdk_session_id, "{}", 1),
    )
    test_db.commit()


def _store_entry_count(test_db, sdk_session_id: str) -> int:
    cur = test_db.execute(
        "SELECT COUNT(*) FROM session_store_entries WHERE session_id = ?",
        (sdk_session_id,),
    )
    return cur.fetchone()[0]


def _store_summary_count(test_db, sdk_session_id: str) -> int:
    cur = test_db.execute(
        "SELECT COUNT(*) FROM session_store_summaries WHERE session_id = ?",
        (sdk_session_id,),
    )
    return cur.fetchone()[0]


class TestDiscardOrphanFirstTurn:
    def test_wipes_transcript_unlinks_sdk_and_drops_user_row(self, mock_db):
        """The full revert: a first turn minted an SDK session + mirrored the
        user line, then was stopped. Everything that the chat reads back must go,
        but the session row + worktree link stay."""
        _set_sdk_session_id(mock_db, "s2", "sdk-aaa")
        _add_store_entry(mock_db, "sdk-aaa", "user")
        _add_store_summary(mock_db, "sdk-aaa")
        db.add_session_message(session_id="s2", role="user", content="hello")

        discarded = db.discard_orphan_first_turn("s2")

        assert discarded == "hello"
        assert _store_entry_count(mock_db, "sdk-aaa") == 0
        assert _store_summary_count(mock_db, "sdk-aaa") == 0
        assert _sdk_session_id(mock_db, "s2") is None
        assert db.get_session_messages("s2") == []
        # Session + worktree link survive (the whole point of the constraint).
        assert _session_exists(mock_db, "s2") is True
        assert _worktree_id(mock_db, "s2") == "wt-123"

    def test_sweeps_subagent_transcripts_for_the_turn(self, mock_db):
        """Subagent rows (subpath != NULL) sharing the SDK session id are part of
        the empty turn and must be swept too."""
        _set_sdk_session_id(mock_db, "s1", "sdk-bbb")
        _add_store_entry(mock_db, "sdk-bbb", "user")
        _add_store_entry(mock_db, "sdk-bbb", "assistant", subpath="agents/sub-1")
        db.add_session_message(session_id="s1", role="user", content="hi")

        db.discard_orphan_first_turn("s1")

        assert _store_entry_count(mock_db, "sdk-bbb") == 0

    def test_does_not_touch_other_sessions_transcripts(self, mock_db):
        """Cleanup keyed on the SDK UUID must never sweep another session's
        transcript."""
        _set_sdk_session_id(mock_db, "s1", "sdk-ccc")
        _add_store_entry(mock_db, "sdk-ccc", "user")
        # A different, healthy session's transcript.
        _add_store_entry(mock_db, "sdk-other", "user")
        _add_store_entry(mock_db, "sdk-other", "assistant")

        db.discard_orphan_first_turn("s1")

        assert _store_entry_count(mock_db, "sdk-ccc") == 0
        assert _store_entry_count(mock_db, "sdk-other") == 2

    def test_no_sdk_session_id_only_drops_user_row(self, mock_db):
        """If the turn was stopped before the SDK even minted a session id, there
        is no transcript to wipe — just drop the eager user row."""
        db.add_session_message(session_id="s1", role="user", content="hello")

        discarded = db.discard_orphan_first_turn("s1")

        assert discarded == "hello"
        assert db.get_session_messages("s1") == []
        assert _session_exists(mock_db, "s1") is True

    def test_keeps_turn_that_produced_a_response(self, mock_db):
        """Defense in depth: even if mis-called, a trailing assistant row means a
        real turn — the user row is kept (transcript still wiped is the caller's
        responsibility to gate, so assert only the message guard here)."""
        _set_sdk_session_id(mock_db, "s1", "sdk-ddd")
        db.add_session_message(session_id="s1", role="user", content="hello")
        db.add_session_message(session_id="s1", role="assistant", content="hi there")

        discarded = db.discard_orphan_first_turn("s1")

        assert discarded is None
        assert [r["content"] for r in db.get_session_messages("s1")] == ["hello", "hi there"]

    def test_ignores_task_notification_user_row(self, mock_db):
        """A <task-notification> callback row is not a real prompt."""
        _set_sdk_session_id(mock_db, "s1", "sdk-eee")
        _add_store_entry(mock_db, "sdk-eee", "user")
        db.add_session_message(
            session_id="s1",
            role="user",
            content="<task-notification>done</task-notification>",
        )

        discarded = db.discard_orphan_first_turn("s1")

        assert discarded is None
        # The transcript is still wiped + unlinked (the turn produced no output).
        assert _store_entry_count(mock_db, "sdk-eee") == 0
        assert _sdk_session_id(mock_db, "s1") is None
        assert len(db.get_session_messages("s1")) == 1
