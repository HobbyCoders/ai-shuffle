"""Built-in subagent seeding behavior (moved from test_subagents.py when the
/subagents API was hard-renamed away in P2b — the seeding logic lives on in
app.core.builtin_subagents over the unified agents table)."""

from unittest.mock import patch




async def test_retirement_preserves_key_scope_and_cancels_stored_continuations(mock_db):
    from app.core.builtin_subagents import seed_builtin_subagents
    from app.core.api_user_scope import worker_refusal
    from app.core.config import settings
    from app.core import wakeup_service, turn_scheduler
    from app.db import database, wakeups, turns, session_events

    database.create_profile("Explore", "Explore", None, {}, is_builtin=True)
    database.create_profile("retirement-other", "Other", None, {})
    database.create_api_user("retirement-only", "Explore only", "only-test-hash", profile_ids=["Explore"])
    database.create_api_user("retirement-mixed", "Mixed", "mixed-test-hash", profile_ids=["Explore", "retirement-other"])
    database.create_api_user("retirement-open", "Open", "open-test-hash")
    assert worker_refusal("retirement-only", "general-purpose") is not None
    database.create_session("retirement-chat", profile_id="Explore")
    wakeups.create_wakeup(id="retirement-wake", session_id="retirement-chat", profile_id="Explore",
                         prompt="continue", fire_at=9999999999)
    turn = turns.enqueue_turn("retirement-chat", "user", profile_id="Explore", prompt="work")
    with database.get_db() as conn:
        conn.cursor().execute("UPDATE turns SET state = 'parked' WHERE id = %s", (turn["id"],))
    seed_builtin_subagents()
    seed_builtin_subagents()
    assert database.get_api_user("retirement-only")["profile_ids"] == ["Explore"]
    assert worker_refusal("retirement-only", "general-purpose") is not None
    assert worker_refusal("retirement-mixed", "retirement-other") is None
    assert worker_refusal("retirement-mixed", "general-purpose") is not None
    assert worker_refusal("retirement-open", "general-purpose") is None
    # The assistant's connector authorization reads the raw junction table,
    # rather than database.get_api_user. It must stay restricted too.
    from app.db.assistant import connector_grant_available
    with patch("app.db.assistant.owner_connector", return_value=True), database.get_db() as conn:
        assert not connector_grant_available(conn, {"api_user_id": "retirement-only"}, "general-purpose")
        assert connector_grant_available(conn, {"api_user_id": "retirement-mixed"}, "retirement-other")
        assert connector_grant_available(conn, {"api_user_id": "retirement-open"}, "general-purpose")
    assert turns.get_turn(turn["id"])["state"] == "aborted"
    pending = [r for r in wakeups.get_pending_wakeups() if r["session_id"] == "retirement-chat"]
    assert len(pending) == 2
    assert all(r["instance_id"] == settings.effective_instance_id for r in pending)
    assert all(r["source"] == "profile_cancelled" and not r["profile_id"] and not r["prompt"] for r in pending)
    with patch("app.core.turn_manager.submit_async") as submit:
        for row in pending:
            await wakeup_service._fire(row)
        submit.assert_not_called()
    assert not [r for r in wakeups.get_pending_wakeups() if r["session_id"] == "retirement-chat"]
    notes = session_events.get_events("retirement-chat")
    assert any("Explore was removed" in str(n["payload"]) for n in notes)
    successor = await turn_scheduler._enqueue_context_wake("retirement-chat", "unpark", [], [], "continue")
    assert successor["id"] is None
    assert not turns.get_queued_turns("retirement-chat")
    # The owner's explicit reassignment can replace the obsolete restriction.
    database.update_api_user("retirement-only", profile_ids=["retirement-other"])
    assert worker_refusal("retirement-only", "retirement-other") is None
    assert worker_refusal("retirement-only", "general-purpose") is not None


async def test_retired_session_announces_the_cancelled_continuation_once(mock_db):
    """Every background task that finishes on a cancelled Explore session asks
    for a context wake; each refusal used to post another identical notice."""
    from app.core.builtin_subagents import seed_builtin_subagents
    from app.core import wakeup_service, turn_scheduler
    from app.db import database, wakeups, turns, session_events

    database.create_profile("Explore", "Explore", None, {}, is_builtin=True)
    database.create_session("retired-busy-chat", profile_id="Explore")
    wakeups.create_wakeup(id="retired-busy-wake", session_id="retired-busy-chat", profile_id="Explore",
                         prompt="continue", fire_at=9999999999)
    turn = turns.enqueue_turn("retired-busy-chat", "user", profile_id="Explore", prompt="work")
    with database.get_db() as conn:
        conn.cursor().execute("UPDATE turns SET state = 'parked' WHERE id = %s", (turn["id"],))
    seed_builtin_subagents()

    def dropped():
        return [e for e in session_events.get_events("retired-busy-chat")
                if e["payload"].get("subtype") == wakeup_service.DROPPED_NOTE_SUBTYPE]

    with patch("app.core.turn_manager.submit_async") as submit:
        for row in [r for r in wakeups.get_pending_wakeups() if r["session_id"] == "retired-busy-chat"]:
            await wakeup_service._fire(row)
        for task in ("task-a", "task-b", "task-c"):
            result = await turn_scheduler._enqueue_context_wake(
                "retired-busy-chat", "task_complete", [], [task], "continue")
            assert result["id"] is None
        submit.assert_not_called()
    assert not turns.get_queued_turns("retired-busy-chat")
    notes = dropped()
    assert len(notes) == 1, [n["payload"] for n in notes]
    assert "Explore was removed" in str(notes[0]["payload"])


def test_startup_seeding_keeps_keys_granted_to_built_ins(mock_db):
    """#1875 review: seeding runs on every start. Keys that name built-in
    agents keep their grants and stay switched on across restarts."""
    from app.core.builtin_subagents import BUILTIN_SUBAGENTS, seed_builtin_subagents
    from app.core.api_user_scope import worker_refusal
    from app.db import database

    builtin_ids = [d["id"] for d in BUILTIN_SUBAGENTS]
    seed_builtin_subagents()
    database.create_profile("seed-custom", "Custom", None, {})
    database.create_api_user("seed-only-gp", "GP only", "seed-gp-hash", profile_ids=[builtin_ids[0]])
    database.create_api_user("seed-mixed", "Mixed", "seed-mixed-hash", profile_ids=[*builtin_ids, "seed-custom"])
    for _restart in range(2):
        assert all(v == "exists" for k, v in seed_builtin_subagents().items() if k in builtin_ids)
    only, mixed = database.get_api_user("seed-only-gp"), database.get_api_user("seed-mixed")
    assert only["is_active"] and only["profile_ids"] == [builtin_ids[0]]
    assert mixed["is_active"] and sorted(mixed["profile_ids"]) == sorted([*builtin_ids, "seed-custom"])
    assert worker_refusal("seed-only-gp", builtin_ids[0]) is None


async def test_missing_stored_wake_profile_drops_once_with_visible_reason(mock_db):
    from app.core import wakeup_service
    from app.core.config import settings
    from app.db import database, wakeups, session_events

    database.create_profile("wake-fallback", "Existing fallback", None, {})
    database.create_session("missing-wake-chat", profile_id="wake-fallback")
    row = wakeups.create_wakeup(id="missing-agent-wake", session_id="missing-wake-chat", profile_id="deleted-agent",
                               prompt="continue", fire_at=0, instance_id=settings.effective_instance_id)
    assert await wakeup_service._resolve_profile_id(row) is None
    assert await wakeup_service._resolve_profile_id({"session_id": "missing-wake-chat"}) == "wake-fallback"
    with patch("app.core.turn_manager.submit_async") as submit:
        await wakeup_service._fire(row)
        await wakeup_service._fire(row)
        submit.assert_not_called()
    assert not [r for r in wakeups.get_pending_wakeups() if r["id"] == row["id"]]
    notes = session_events.get_events("missing-wake-chat")
    assert len(notes) == 1
    assert "deleted-agent" in str(notes[0]["payload"])
    assert wakeup_service._retry_attempts == {}


_CHS_DEF = {
    "id": "chat-history-summarizer",
    "name": "Chat History Summarizer",
    "description": "Summarizes chat history",
    "prompt": "Summarizer prompt",
    "tools": ["Read", "Grep", "Glob"],
    "model": "sonnet",
}


def test_retirement_preserves_history_and_disables_automations(mock_db):
    from app.core.builtin_subagents import seed_builtin_subagents
    from app.db import database

    database.create_profile("Explore", "Renamed built-in", None, {
        "system_prompt": {"type": "custom", "content": "Old search prompt"},
        "provider_models": {"__native__": "haiku", "__voice__": "opus"},
    }, is_builtin=True)
    database.create_session("old-search", profile_id="Explore")
    database.create_automation("old-search-job", "Saved search", "interval", {"seconds": 300},
                               "Explore", prompt="Find it", enabled=True)
    database.create_profile("other-builtin", "Other built-in profile", None, {}, is_builtin=True)

    assert seed_builtin_subagents()["Explore"] == "retired"
    assert database.get_profile("Explore") is None
    assert database.get_session("old-search")["profile_id"] is None
    assert database.get_profile("other-builtin") is not None
    automation = database.get_automation("old-search-job")
    assert automation["profile_id"] == "general-purpose"
    assert not automation["is_enabled"]
    assert automation["prompt"] == "Find it"
    assert "Explore was removed" in automation["last_error"]
    assert "Explore" not in seed_builtin_subagents()  # idempotent


def test_retirement_keeps_owner_created_explore(mock_db):
    from app.core.builtin_subagents import seed_builtin_subagents
    from app.db import database

    database.create_profile("Explore", "Explore", None, {"model": "opus"})
    database.create_profile("custom-search", "Explore", None, {"model": "sonnet"})
    database.create_automation("custom-job", "Custom search", "interval", {"seconds": 300},
                               "Explore", prompt="Find it", enabled=True)
    seed_builtin_subagents()
    assert not database.get_profile("Explore")["is_builtin"]
    assert database.get_profile("custom-search") is not None
    assert database.get_automation("custom-job")["profile_id"] == "Explore"
    assert database.get_automation("custom-job")["is_enabled"]


class TestBuiltinSubagentSeeding:
    """Test the seed_builtin_subagents function behavior."""

    def test_seed_creates_builtin_when_not_exists(self):
        """Seeding creates built-in subagent when it doesn't exist."""
        from app.core.builtin_subagents import seed_builtin_subagents

        with patch("app.core.builtin_subagents.database") as mock_db, \
             patch("app.core.builtin_subagents.BUILTIN_SUBAGENTS", [_CHS_DEF]):
            mock_db.get_subagent.return_value = None
            mock_db.create_subagent.return_value = {
                "id": "chat-history-summarizer",
                "is_builtin": True
            }

            results = seed_builtin_subagents()

            assert results.get("chat-history-summarizer") == "created"
            mock_db.create_subagent.assert_called_once()
            # Verify is_builtin=True was passed
            call_kwargs = mock_db.create_subagent.call_args[1]
            assert call_kwargs["is_builtin"] is True
            assert call_kwargs["store_defaults"] is True

    def test_seed_promotes_non_builtin_to_builtin(self):
        """Seeding promotes subagent to built-in if it exists with is_builtin=False."""
        from app.core.builtin_subagents import seed_builtin_subagents

        # Subagent exists but is_builtin is False (the bug condition)
        existing_subagent = {
            "id": "chat-history-summarizer",
            "name": "Old Name",
            "is_builtin": False,  # Bug: should be True
            "default_prompt": None
        }

        with patch("app.core.builtin_subagents.database") as mock_db, \
             patch("app.core.builtin_subagents.BUILTIN_SUBAGENTS", [_CHS_DEF]):
            mock_db.get_subagent.return_value = existing_subagent

            results = seed_builtin_subagents()

            assert results.get("chat-history-summarizer") == "promoted"
            # Verify set_subagent_builtin was called to fix the flag
            mock_db.set_subagent_builtin.assert_called_once_with("chat-history-summarizer", True)

    def test_seed_existing_builtin_without_defaults_is_left_alone(self):
        """v71: defaults live in code — a builtin row missing the old
        default_prompt column needs no 'repair' pass any more."""
        from app.core.builtin_subagents import seed_builtin_subagents

        existing_subagent = {
            "id": "chat-history-summarizer",
            "name": "Chat History Summarizer",
            "is_builtin": True,
        }

        with patch("app.core.builtin_subagents.database") as mock_db, \
             patch("app.core.builtin_subagents.BUILTIN_SUBAGENTS", [_CHS_DEF]):
            mock_db.get_subagent.return_value = existing_subagent

            results = seed_builtin_subagents()

            assert results.get("chat-history-summarizer") == "exists"
            mock_db.create_subagent.assert_not_called()

    def test_seed_skips_existing_builtin_with_defaults(self):
        """Seeding skips properly configured built-in subagents."""
        from app.core.builtin_subagents import seed_builtin_subagents

        # Properly configured built-in — defaults match the code definition,
        # so seeding should detect no change and report "exists".
        existing_subagent = {
            "id": "chat-history-summarizer",
            "name": "Chat History Summarizer",
            "is_builtin": True,
            "default_prompt": _CHS_DEF["prompt"],
            "default_model": _CHS_DEF["model"],
        }

        with patch("app.core.builtin_subagents.database") as mock_db, \
             patch("app.core.builtin_subagents.BUILTIN_SUBAGENTS", [_CHS_DEF]):
            mock_db.get_subagent.return_value = existing_subagent

            results = seed_builtin_subagents()

            assert results.get("chat-history-summarizer") == "exists"
            # No modifications should be made
            mock_db.set_subagent_builtin.assert_not_called()
            mock_db.create_subagent.assert_not_called()
