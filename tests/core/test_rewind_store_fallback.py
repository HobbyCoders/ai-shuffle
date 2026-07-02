"""Bug D regression: rewind anchor resolution must work without the JSONL.

Under ``postgres_only`` the on-disk transcript lives on tmpfs and is frequently
absent, while user rows in ``session_messages`` are persisted with
``jsonl_uuid=NULL`` (the uuid only gets backfilled FROM the JSONL). Every
anchor-resolution path in ``rewind_db_sync`` read the JSONL exclusively, so
with it gone the anchor came back ``None`` and the legacy row silently
survived every rewind — then resurfaced through the empty-store legacy
fallback after a full rewind (the e2e "rewind leftover" finding).

Store ``entry`` JSONB rows are the exact JSONL-shaped dicts
(``uuid``/``type``/``message``), so the fix is a transcript-source fallback:
JSONL when present, else the session store's main transcript. No Postgres —
the store loader and the database module are both mocked.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import app.core.rewind_db_sync as rds
from app.core.config import settings
from app.core.jsonl_rewind import jsonl_rewind_service


SID = "app-sess-1"
SDK_SID = "11111111-2222-3333-4444-555555555555"


def _u(uuid: str, text: str) -> dict:
    return {"uuid": uuid, "type": "user",
            "message": {"role": "user", "content": text}}


def _a(uuid: str, text: str) -> dict:
    return {"uuid": uuid, "type": "assistant",
            "message": {"role": "assistant",
                        "content": [{"type": "text", "text": text}]}}


TRANSCRIPT = [
    _u("u1", "hello 1"),
    _a("a1", "hi 1"),
    _u("u2", "hello 2"),
    _a("a2", "hi 2"),
]

DB_USER_ROWS = [
    {"id": 10, "role": "user", "content": "hello 1", "jsonl_uuid": None},
    {"id": 20, "role": "user", "content": "hello 2", "jsonl_uuid": None},
]


@pytest.fixture
def no_jsonl():
    """The tmpfs transcript is gone — the Bug D precondition."""
    with patch.object(jsonl_rewind_service, "_get_jsonl_path", return_value=None):
        yield


@pytest.fixture
def store_entries():
    """Session store main transcript, loadable by session_id."""
    with patch.object(settings, "session_store_mode", "postgres_only"), \
         patch("app.core.pg_session_store._load_entries_by_session_id_sync",
               return_value=list(TRANSCRIPT)) as loader:
        yield loader


@pytest.fixture
def mock_db():
    with patch.object(rds, "database") as db:
        db.get_session_messages.return_value = [dict(r) for r in DB_USER_ROWS]
        db.set_message_jsonl_uuid.return_value = True
        yield db


class TestLoadTranscriptEntries:
    def test_prefers_jsonl_when_present(self, store_entries, tmp_path):
        jsonl = [_u("j1", "from jsonl")]
        with patch.object(jsonl_rewind_service, "_get_jsonl_path",
                          return_value=Path(tmp_path / "s.jsonl")), \
             patch.object(jsonl_rewind_service, "_parse_jsonl_entries",
                          return_value=jsonl):
            got = rds._load_transcript_entries(SDK_SID, "/workspace")

        assert got == jsonl
        store_entries.assert_not_called()

    def test_falls_back_to_store_when_jsonl_missing(self, no_jsonl, store_entries):
        got = rds._load_transcript_entries(SDK_SID, "/workspace")

        assert got == TRANSCRIPT
        store_entries.assert_called_once_with(SDK_SID, None)

    def test_no_store_query_when_store_mode_disabled(self, no_jsonl):
        """The store tables (and %s placeholders) are Postgres-only — a
        filesystem-mode install must never touch them."""
        with patch.object(settings, "session_store_mode", "disabled"), \
             patch("app.core.pg_session_store._load_entries_by_session_id_sync") as loader:
            got = rds._load_transcript_entries(SDK_SID, "/workspace")

        assert got == []
        loader.assert_not_called()

    def test_store_failure_returns_empty(self, no_jsonl):
        with patch.object(settings, "session_store_mode", "postgres_only"), \
             patch("app.core.pg_session_store._load_entries_by_session_id_sync",
                   side_effect=RuntimeError("pg down")):
            assert rds._load_transcript_entries(SDK_SID, "/workspace") == []


class TestAnchorFromStoreEntries:
    """The exact e2e scenario: jsonl_uuid=None rows + no JSONL. The anchor must
    resolve through the store transcript instead of silently returning None."""

    def test_user_anchor_resolves_via_store_content_fallback(
        self, no_jsonl, store_entries, mock_db
    ):
        with patch.object(rds, "_resolve_anchor_by_uuid", return_value=None):
            anchor = rds.resolve_rewind_anchor(
                session_id=SID,
                sdk_session_id=SDK_SID,
                working_dir="/workspace",
                target_uuid="u2",
            )

        assert anchor == 20

    def test_backfill_writes_uuids_from_store_entries(
        self, no_jsonl, store_entries, mock_db
    ):
        updated = rds.backfill_user_jsonl_uuids(SID, SDK_SID, "/workspace")

        assert updated == 2
        pairs = [c.args for c in mock_db.set_message_jsonl_uuid.call_args_list]
        assert pairs == [(10, "u1"), (20, "u2")]

    def test_content_mismatch_still_refuses_anchor(
        self, no_jsonl, store_entries, mock_db
    ):
        """The safety net stays: if the store text looks nothing like the DB
        row, refuse to delete rather than truncate the wrong history."""
        mock_db.get_session_messages.return_value = [
            {"id": 10, "role": "user", "content": "hello 1", "jsonl_uuid": None},
            {"id": 20, "role": "user", "content": "совершенно другое", "jsonl_uuid": None},
        ]
        with patch.object(rds, "_resolve_anchor_by_uuid", return_value=None):
            anchor = rds.resolve_rewind_anchor(
                session_id=SID,
                sdk_session_id=SDK_SID,
                working_dir="/workspace",
                target_uuid="u2",
            )

        assert anchor is None

    def test_no_transcript_anywhere_returns_none(self, no_jsonl, mock_db):
        with patch.object(settings, "session_store_mode", "postgres_only"), \
             patch("app.core.pg_session_store._load_entries_by_session_id_sync",
                   return_value=[]), \
             patch.object(rds, "_resolve_anchor_by_uuid", return_value=None):
            anchor = rds.resolve_rewind_anchor(
                session_id=SID,
                sdk_session_id=SDK_SID,
                working_dir="/workspace",
                target_uuid="u2",
            )

        assert anchor is None
        mock_db.set_message_jsonl_uuid.assert_not_called()
