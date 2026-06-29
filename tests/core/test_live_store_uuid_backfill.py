"""
Regression tests for live-message uuid backfill in ``app.core.live_session_store``.

Bug #1 of the chat-rewind fix: messages produced during a live streaming turn
never carried a durable transcript uuid, so the client-side rewind guard (which
requires a uuid) blocked rewinding a just-sent message until a full reload.

``end_streaming`` now backfills those uuids from the canonical Postgres session
store (user prompts) and the turn metadata (final assistant message). These
tests cover the pure extraction/stamping helpers plus the async orchestration,
all without a database (the store load is patched).
"""

from unittest.mock import patch

import pytest

from app.core.live_session_store import (
    LiveMessage,
    LiveSession,
    LiveSessionStore,
    _extract_store_rewind_uuids,
    _user_entry_text,
)


# ─── transcript-entry / live-message builders ────────────────────────────────

def _store_user(uuid: str, text: str = "hi", **extra) -> dict:
    e = {"uuid": uuid, "type": "user",
         "message": {"role": "user", "content": text}}
    e.update(extra)
    return e


def _store_assistant(uuid: str, text: str = "ok") -> dict:
    return {"uuid": uuid, "type": "assistant",
            "message": {"role": "assistant", "content": text}}


def _store_assistant_toolonly(uuid: str) -> dict:
    return {
        "uuid": uuid,
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "t1", "name": "x", "input": {}}],
        },
    }


def _live_user(mid: str, text: str = "hi", uuid=None) -> LiveMessage:
    return LiveMessage(id=mid, role="user", type="text", content=text, uuid=uuid)


def _live_assistant(mid: str, text: str = "ok", uuid=None) -> LiveMessage:
    return LiveMessage(id=mid, role="assistant", type="text", content=text, uuid=uuid)


# ─── _user_entry_text ─────────────────────────────────────────────────────────

class TestUserEntryText:
    def test_plain_string(self):
        assert _user_entry_text("hello") == "hello"

    def test_list_of_text_blocks(self):
        content = [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]
        assert _user_entry_text(content) == "ab"

    def test_list_skips_non_text_blocks(self):
        content = [
            {"type": "tool_result", "content": "x"},
            {"type": "text", "text": "real"},
        ]
        assert _user_entry_text(content) == "real"

    def test_other_returns_empty(self):
        assert _user_entry_text(None) == ""
        assert _user_entry_text(42) == ""


# ─── _extract_store_rewind_uuids ─────────────────────────────────────────────

class TestExtractStoreRewindUuids:
    def test_extracts_users_and_assistants_in_order(self):
        entries = [
            _store_user("u1", "hello 1"),
            _store_assistant("a1", "hi 1"),
            _store_user("u2", "hello 2"),
            _store_assistant("a2", "hi 2"),
        ]
        users, assistants = _extract_store_rewind_uuids(entries)
        assert users == ["u1", "u2"]
        assert assistants == ["a1", "a2"]

    def test_skips_meta_and_sidechain_users(self):
        entries = [
            _store_user("m1", "meta", isMeta=True),
            _store_user("s1", "side", isSidechain=True),
            _store_user("u1", "real"),
        ]
        users, _ = _extract_store_rewind_uuids(entries)
        assert users == ["u1"]

    def test_skips_tool_result_user_entries(self):
        entries = [
            _store_user("u1", "real"),
            {
                "uuid": "utr",
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "o"}],
                },
            },
        ]
        users, _ = _extract_store_rewind_uuids(entries)
        assert users == ["u1"]

    def test_skips_command_wrapper_and_empty_prompts(self):
        entries = [
            _store_user("cmd", "<command-name>foo</command-name>"),
            _store_user("empty", ""),
            _store_user("u1", "genuine"),
        ]
        users, _ = _extract_store_rewind_uuids(entries)
        assert users == ["u1"]

    def test_skips_entries_without_uuid(self):
        entries = [
            {"type": "user", "message": {"role": "user", "content": "no uuid"}},
            _store_user("u1", "has uuid"),
        ]
        users, _ = _extract_store_rewind_uuids(entries)
        assert users == ["u1"]

    def test_assistant_tool_only_entry_excluded(self):
        entries = [
            _store_assistant("a1", "has text"),
            _store_assistant_toolonly("a2tool"),
            _store_assistant("a3", "more text"),
        ]
        _, assistants = _extract_store_rewind_uuids(entries)
        assert assistants == ["a1", "a3"]


# ─── _stamp_uuids_by_ordinal ─────────────────────────────────────────────────

class TestStampUuidsByOrdinal:
    def setup_method(self):
        self.store = LiveSessionStore()

    def test_stamps_matching_counts(self):
        session = LiveSession(session_id="s1", messages=[
            _live_user("m1", "hello 1"),
            _live_assistant("m2", "hi 1"),
            _live_user("m3", "hello 2"),
            _live_assistant("m4", "hi 2"),
        ])
        self.store._stamp_uuids_by_ordinal(session, ["u1", "u2"], ["a1", "a2"])
        assert [m.uuid for m in session.messages] == ["u1", "a1", "u2", "a2"]

    def test_does_not_clobber_existing_uuid(self):
        session = LiveSession(session_id="s1", messages=[
            _live_user("m1", "hello 1", uuid="seeded-u1"),
            _live_user("m2", "hello 2"),
        ])
        self.store._stamp_uuids_by_ordinal(session, ["u1", "u2"], [])
        # Seeded uuid preserved; only the uuid-less message is filled.
        assert session.messages[0].uuid == "seeded-u1"
        assert session.messages[1].uuid == "u2"

    def test_user_count_mismatch_skips_stamping(self):
        """Guard against wrong-target rewind: unequal counts → no user stamp."""
        session = LiveSession(session_id="s1", messages=[
            _live_user("m1", "hello 1"),
            _live_user("m2", "hello 2"),
        ])
        # Only one store uuid but two live user messages → mismatch → skip.
        self.store._stamp_uuids_by_ordinal(session, ["u1"], [])
        assert [m.uuid for m in session.messages] == [None, None]

    def test_assistant_count_mismatch_skips_only_assistants(self):
        session = LiveSession(session_id="s1", messages=[
            _live_user("m1", "hello 1"),
            _live_assistant("m2", "hi 1"),
            _live_assistant("m3", "hi 1 cont"),
        ])
        # Users align (1==1) → stamped; assistants mismatch (2 live vs 1) → skipped.
        self.store._stamp_uuids_by_ordinal(session, ["u1"], ["a1"])
        assert session.messages[0].uuid == "u1"
        assert session.messages[1].uuid is None
        assert session.messages[2].uuid is None


# ─── _stamp_last_assistant_uuid ──────────────────────────────────────────────

class TestStampLastAssistantUuid:
    def setup_method(self):
        self.store = LiveSessionStore()

    def test_stamps_final_assistant_from_metadata(self):
        session = LiveSession(session_id="s1", messages=[
            _live_user("m1", "q"),
            _live_assistant("m2", "answer"),
        ])
        self.store._stamp_last_assistant_uuid(session, {"last_assistant_uuid": "final"})
        assert session.messages[1].uuid == "final"

    def test_does_not_override_present_uuid(self):
        session = LiveSession(session_id="s1", messages=[
            _live_assistant("m2", "answer", uuid="already"),
        ])
        self.store._stamp_last_assistant_uuid(session, {"last_assistant_uuid": "final"})
        assert session.messages[0].uuid == "already"

    def test_no_metadata_is_noop(self):
        session = LiveSession(session_id="s1", messages=[_live_assistant("m2", "a")])
        self.store._stamp_last_assistant_uuid(session, None)
        assert session.messages[0].uuid is None


# ─── _backfill_message_uuids (async orchestration) ───────────────────────────

@pytest.mark.asyncio
class TestBackfillMessageUuids:
    async def test_backfills_user_and_final_assistant(self):
        store = LiveSessionStore()
        session = LiveSession(session_id="local-1", messages=[
            _live_user("m1", "hello 1"),
            _live_assistant("m2", "hi 1"),
            _live_user("m3", "hello 2"),
            _live_assistant("m4", "hi 2"),
        ])
        store_entries = [
            _store_user("u1", "hello 1"),
            _store_assistant("a1", "hi 1"),
            _store_user("u2", "hello 2"),
            # Final assistant entry not yet mirrored — comes from metadata.
        ]
        with patch("app.db.database.get_session",
                   return_value={"sdk_session_id": "sdk-1"}), \
             patch("app.core.pg_session_store._load_entries_by_session_id_sync",
                   return_value=store_entries):
            await store._backfill_message_uuids(
                session, {"last_assistant_uuid": "a2-final"}
            )
        uuids = [m.uuid for m in session.messages]
        # u1/u2 from the store; a1 from the store; a2 from metadata override.
        assert uuids[0] == "u1"
        assert uuids[2] == "u2"
        assert uuids[3] == "a2-final"

    async def test_no_sdk_session_still_stamps_metadata(self):
        """If the session has no sdk_session_id yet, the store read is skipped
        but the final assistant is still made rewindable via metadata."""
        store = LiveSessionStore()
        session = LiveSession(session_id="local-1", messages=[
            _live_user("m1", "hello"),
            _live_assistant("m2", "hi"),
        ])
        with patch("app.db.database.get_session", return_value=None):
            await store._backfill_message_uuids(
                session, {"last_assistant_uuid": "final"}
            )
        assert session.messages[0].uuid is None  # no store → user not stamped
        assert session.messages[1].uuid == "final"

    async def test_store_failure_is_swallowed(self):
        """A store-load exception must not break end_streaming; metadata still
        applies."""
        store = LiveSessionStore()
        session = LiveSession(session_id="local-1", messages=[
            _live_assistant("m2", "hi"),
        ])
        with patch("app.db.database.get_session",
                   return_value={"sdk_session_id": "sdk-1"}), \
             patch("app.core.pg_session_store._load_entries_by_session_id_sync",
                   side_effect=RuntimeError("db down")):
            await store._backfill_message_uuids(
                session, {"last_assistant_uuid": "final"}
            )
        assert session.messages[0].uuid == "final"
