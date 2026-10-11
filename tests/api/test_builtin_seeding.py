"""Built-in subagent seeding behavior (moved from test_subagents.py when the
/subagents API was hard-renamed away in P2b — the seeding logic lives on in
app.core.builtin_subagents over the unified agents table)."""

from unittest.mock import patch

import pytest
from tests.builtin_fixtures import create_unchanged_builtin




@pytest.mark.parametrize("retired_id", ["Explore", "Plan", "general-purpose", "frontend-designer"])
async def test_retirement_preserves_key_scope_and_cancels_stored_continuations(mock_db, retired_id):
    from app.core.builtin_subagents import seed_builtin_subagents
    from app.core.api_user_scope import worker_refusal
    from app.core.config import settings
    from app.core import wakeup_service, turn_scheduler
    from app.db import database, wakeups, turns, session_events

    create_unchanged_builtin(retired_id)
    database.create_profile("retirement-other", "Other", None, {})
    database.create_api_user("retirement-only", "Explore only", "only-test-hash", profile_ids=[retired_id])
    database.create_api_user("retirement-mixed", "Mixed", "mixed-test-hash", profile_ids=[retired_id, "retirement-other"])
    database.create_api_user("retirement-open", "Open", "open-test-hash")
    assert worker_refusal("retirement-only", "unrelated-agent") is not None
    database.create_session("retirement-chat", profile_id=retired_id)
    wakeups.create_wakeup(id="retirement-wake", session_id="retirement-chat", profile_id=retired_id,
                         prompt="continue", fire_at=9999999999)
    turn = turns.enqueue_turn("retirement-chat", "user", profile_id=retired_id, prompt="work")
    with database.get_db() as conn:
        conn.cursor().execute("UPDATE turns SET state = 'parked' WHERE id = %s", (turn["id"],))
    seed_builtin_subagents()
    seed_builtin_subagents()
    assert database.get_api_user("retirement-only")["profile_ids"] == [retired_id]
    assert worker_refusal("retirement-only", "unrelated-agent") is not None
    assert worker_refusal("retirement-mixed", "retirement-other") is None
    assert worker_refusal("retirement-mixed", "unrelated-agent") is not None
    assert worker_refusal("retirement-open", "unrelated-agent") is None
    # The assistant's connector authorization reads the raw junction table,
    # rather than database.get_api_user. It must stay restricted too.
    from app.db.assistant import connector_grant_available
    with patch("app.db.assistant.owner_connector", return_value=True), database.get_db() as conn:
        assert not connector_grant_available(conn, {"api_user_id": "retirement-only"}, "unrelated-agent")
        assert connector_grant_available(conn, {"api_user_id": "retirement-mixed"}, "retirement-other")
        assert connector_grant_available(conn, {"api_user_id": "retirement-open"}, "unrelated-agent")
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
    assert any(f"{retired_id} was removed" in str(n["payload"]) for n in notes)
    successor = await turn_scheduler._enqueue_context_wake("retirement-chat", "unpark", [], [], "continue")
    assert successor["id"] is None
    assert not turns.get_queued_turns("retirement-chat")
    # The owner's explicit reassignment can replace the obsolete restriction.
    database.update_api_user("retirement-only", profile_ids=["retirement-other"])
    assert worker_refusal("retirement-only", "retirement-other") is None
    assert worker_refusal("retirement-only", "unrelated-agent") is not None


@pytest.mark.parametrize("retired_id", ["Explore", "Plan", "general-purpose", "frontend-designer"])
async def test_retired_session_announces_the_cancelled_continuation_once(mock_db, retired_id):
    """Every background task that finishes on a cancelled retired-agent session asks
    for a context wake; each refusal used to post another identical notice."""
    from app.core.builtin_subagents import seed_builtin_subagents
    from app.core import wakeup_service, turn_scheduler
    from app.db import database, wakeups, turns, session_events

    create_unchanged_builtin(retired_id)
    database.create_session("retired-busy-chat", profile_id=retired_id)
    wakeups.create_wakeup(id="retired-busy-wake", session_id="retired-busy-chat", profile_id=retired_id,
                         prompt="continue", fire_at=9999999999)
    turn = turns.enqueue_turn("retired-busy-chat", "user", profile_id=retired_id, prompt="work")
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
    assert f"{retired_id} was removed" in str(notes[0]["payload"])


@pytest.mark.usefixtures("legacy_builtin_catalog")
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


@pytest.mark.parametrize("retired_id", ["Explore", "Plan", "general-purpose", "frontend-designer"])
def test_retirement_preserves_history_and_disables_automations(mock_db, retired_id):
    from app.core.builtin_subagents import seed_builtin_subagents
    from app.db import database

    create_unchanged_builtin(retired_id)
    database.create_session("old-search", profile_id=retired_id)
    database.create_automation("old-search-job", "Saved search", "interval", {"seconds": 300},
                               retired_id, prompt="Find it", enabled=True)
    database.create_profile("other-builtin", "Other built-in profile", None, {}, is_builtin=True)

    assert seed_builtin_subagents()[retired_id] == "retired"
    assert database.get_profile(retired_id) is None
    assert database.get_session("old-search")["profile_id"] is None
    assert database.get_profile("other-builtin") is not None
    automation = database.get_automation("old-search-job")
    assert automation["profile_id"] is None
    assert not automation["is_enabled"]
    assert automation["prompt"] == "Find it"
    assert f"{retired_id} was removed" in automation["last_error"]
    assert retired_id not in seed_builtin_subagents()  # idempotent


@pytest.mark.parametrize("retired_id", ["Explore", "Plan", "general-purpose", "frontend-designer"])
def test_retirement_keeps_owner_created_agents(mock_db, retired_id):
    from app.core.builtin_subagents import seed_builtin_subagents
    from app.db import database

    database.create_profile(retired_id, retired_id, None, {"model": "opus"})
    database.create_profile("custom-search", retired_id, None, {"model": "sonnet"})
    database.create_automation("custom-job", "Custom search", "interval", {"seconds": 300},
                               retired_id, prompt="Find it", enabled=True)
    seed_builtin_subagents()
    assert not database.get_profile(retired_id)["is_builtin"]
    assert database.get_profile("custom-search") is not None
    assert database.get_automation("custom-job")["profile_id"] == retired_id
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
            mock_db.refresh_builtin_subagent_defaults.return_value = []  # nothing to refresh

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
            mock_db.refresh_builtin_subagent_defaults.return_value = []  # nothing to refresh

            results = seed_builtin_subagents()

            assert results.get("chat-history-summarizer") == "exists"
            # No modifications should be made
            mock_db.set_subagent_builtin.assert_not_called()
            mock_db.create_subagent.assert_not_called()


# ---------------------------------------------------------------------------
# Start-up refresh of uncustomised built-in default text (#1870 item 1)
# ---------------------------------------------------------------------------
#
# Each test drives ``seed_builtin_subagents`` (what ``startup.init_core``
# runs) against the test database, with the stored row shaped as an install
# that existed before the change.

import json

import pytest

from app.core.agent_templates import GENERAL_PURPOSE_PROMPT, FRONTEND_DESIGNER_PROMPT


@pytest.fixture
def legacy_builtin_catalog(monkeypatch):
    """Simulate a pre-retirement release only for defaults-refresh contracts.

    Production ships an empty catalog; these tests still protect the generic
    refresh machinery without keeping legacy defaults in application code.
    """
    from app.core import builtin_subagents
    from app.core.agent_templates import STARTER_TEAM_AGENTS
    from app.network import engine

    monkeypatch.setattr(builtin_subagents, "BUILTIN_SUBAGENTS", STARTER_TEAM_AGENTS)
    monkeypatch.setattr(builtin_subagents, "RETIRED_BUILTIN_IDS", ("Explore", "Plan"))
    monkeypatch.setattr(engine, "RETIRED_BUILTIN_IDS", ("Explore", "Plan"))
    monkeypatch.setattr(builtin_subagents, "_PRE_RECORD_DEFAULTS", LEGACY_DEFAULTS)


# Frozen defaults of the release simulated by legacy_builtin_catalog.
LEGACY_DEFAULTS = {
    "general-purpose": {
        "name": frozenset({
            "fe9fa5778e6299904ef31d5f6e87aed494f00eb96d5aba3ab8b23beed99759c9",  # e118eec68
        }),
        "description": frozenset({
            "0138b6fdaf12f08409e1b4ad09de1e6273ed0a945b2469e1e7d6a2099765d8db",  # e118eec68
            "5d73f8e4a5b3920448ae7fa9da33c37ac43be167b08c4d54b7d2a3abab58eb63",  # a35139f4f
            "f9356bec2757c7f31c0861ff41e9f02d8c84c9e87e07a32a1592ab60e8b25f7d",  # 4870adeab
            "cfee2cc5fb38c8f7bf9e8281d35af699e97e21669307f094f30b7b60aaea6976",  # ee6e421a4
        }),
        "prompt": frozenset({
            "cb5321c2af621df53183143ff79fcbba004fbde2cf48e141149030900817f7d1",  # e118eec68
            "08ce19676ce38692007623e6df41b1e9fe5294a7205c8a3210fa6ea080025162",  # a35139f4f
            "0a14d917ae27494cfece2b875ee3c1c1cf58be69f07be49350102efc6a71c979",  # 908a2999e
            "0502c1f5f30d0d1cbd12d11e3b8cb741eb35c1cf8c1aa1c685d3c1a3be7007fd",  # 4870adeab
            "8afc9aab8cd543ab6af037be132e41096ef04cabe8749bc202c5d003ae1d5a8a",  # 3e561728c
            "67e3baea6ff863526dbb55f5ed9ba0182baad683d634834c809db0f180d20a8f",  # ee6e421a4
        }),
    },
    "frontend-designer": {
        "name": frozenset({
            "41d3f28a34f2c7426e52245f950142fff0bf3e2227c7da0074ce47139797c63c",  # e118eec68
        }),
        "description": frozenset({
            "09f16a8955e221e916db851147adde1fbe3a840b09e8b8b6f8f5ca6b13968d8c",  # e118eec68
            "9169238cdbaf672377f55bcecd4883e3a0830e82cfffc9608f4ddff8df09628f",  # 3e561728c
        }),
        "prompt": frozenset({
            "b225623790310d30ef2a327f9cf6906775a07f969c974a86471bb0134d7d57fe",  # e118eec68
            "539e87b1f955e24a33e6fe9d4e9a697931758695a2236e85b9bd8ff8c14f1497",  # a35139f4f
            "cdaf54192f93b351e2d76eb056bb0ac97f24870cf87514d26babe538c70fd80d",  # b91ec8191
            "575bcd55879747d5c82bd4055bf9afdc94188590ace973e59e4f9d747534dc95",  # 3e561728c
            "a30c0b784fbed4b77818e5ddd4c385af30499f1ebff8995ed96b9ebe1a4bfef3",  # ee6e421a4
        }),
    },
}


# The General Purpose text shipped before #1861 (git show ee6e421a4^), which
# still sends searches to the removed Explore agent.
_OLD_GP_DESCRIPTION = (
    "Coding agent for backend and general implementation work: features, bug fixes, refactors, "
    "scripts, tests, migrations, configuration, and tooling. It edits code, runs the checks, and "
    "reports what changed. Do NOT use it to search the codebase or answer questions (use Explore), "
    "to design an approach (use Plan), or for anything visual or UI-related (use Frontend Designer). "
    "Give it a concrete change to make, with the files or behavior involved when known."
)
_OLD_GP_SCOPE = (
    "- Your job is to change code, not to answer questions about it. Codebase questions and broad "
    "searches belong to the Explore agent, implementation plans belong to the Plan agent, and anything "
    "visual (components, pages, layouts, styling, UX) belongs to the Frontend Designer. If your task "
    "turns out to be one of those, say so in your report instead of doing that work here.\n"
    "- Read only what you need to make the change safely: the files you are changing, their callers, "
    "and the tests that cover them."
)
# Owner choices that are not default text; a refresh must keep every one.
_OWNER_SETTINGS = {
    "model": "opus",
    "allowed_tools": ["Read", "Grep", "Bash"],
    "provider_models": {"__native__": "sonnet", "prov-x": "prov:prov-x:big"},
    "memory": "The owner prefers small PRs.",
    "skills": ["unslop"],
    "mcp_servers": [{"name": "docs"}],
    "enabled_agents": ["frontend-designer"],
    "agent_invoke_mode": "custom",
}


def _old_gp_prompt() -> str:
    from app.core.agent_templates import GENERAL_PURPOSE_PROMPT

    start = GENERAL_PURPOSE_PROMPT.index("Scope:\n") + len("Scope:\n")
    end = GENERAL_PURPOSE_PROMPT.index("\n\nHow to work:")
    return GENERAL_PURPOSE_PROMPT[:start] + _OLD_GP_SCOPE + GENERAL_PURPOSE_PROMPT[end:]


def _old_fd_prompt() -> str:
    from app.core.agent_templates import FRONTEND_DESIGNER_PROMPT

    return FRONTEND_DESIGNER_PROMPT.replace(
        "You are the specialist for frontend", "You are the sole agent responsible for all frontend", 1)


def _raw(agent_id: str) -> dict:
    from app.db import database

    with database.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM agents WHERE id = %s", (agent_id,))
        row = dict(cursor.fetchone())
    if isinstance(row["config"], str):
        row["config"] = json.loads(row["config"])
    return row


def _store_existing_row(agent_id: str, *, description: str, prompt: str, record=None,
                        settings: dict = _OWNER_SETTINGS) -> dict:
    """Shape the stored row like one written before this change (no record)."""
    from app.db import database

    config = {"system_prompt": {"type": "custom", "content": prompt}, **settings}
    if record is not None:
        config["builtin_defaults"] = record
    with database.get_db() as conn:
        conn.cursor().execute(
            "UPDATE agents SET description = %s, config = %s, avatar_color = %s, updated_at = %s "
            "WHERE id = %s",
            (description, json.dumps(config), "rose", "2026-09-01T00:00:00+00:00", agent_id),
        )
    return _raw(agent_id)


def _assert_owner_settings_kept(row: dict, settings: dict = _OWNER_SETTINGS) -> None:
    for key, value in settings.items():
        assert row["config"].get(key) == value, key
    assert row["avatar_color"] == "rose"
    assert row["is_builtin"]


@pytest.mark.usefixtures("legacy_builtin_catalog")
def test_old_general_purpose_fixture_is_the_shipped_text():
    """Guards the fixture: it must be the pre-#1861 default byte for byte."""
    import hashlib

    from app.core.builtin_subagents import _PRE_RECORD_DEFAULTS

    shipped = _PRE_RECORD_DEFAULTS["general-purpose"]
    assert hashlib.sha256(_OLD_GP_DESCRIPTION.encode()).hexdigest() in shipped["description"]
    assert hashlib.sha256(_old_gp_prompt().encode()).hexdigest() in shipped["prompt"]
    assert hashlib.sha256(_old_fd_prompt().encode()).hexdigest() in _PRE_RECORD_DEFAULTS[
        "frontend-designer"]["prompt"]


@pytest.mark.usefixtures("legacy_builtin_catalog")
def test_startup_refreshes_unmodified_old_general_purpose(mock_db):
    from app.core.builtin_subagents import (
        BUILTIN_DEFAULTS_REVISION, get_builtin_subagent_definition,
        seed_builtin_subagents,
    )

    seed_builtin_subagents()
    _store_existing_row("general-purpose", description=_OLD_GP_DESCRIPTION, prompt=_old_gp_prompt())

    assert seed_builtin_subagents()["general-purpose"] == "refreshed"

    row = _raw("general-purpose")
    assert row["description"] == get_builtin_subagent_definition("general-purpose")["description"]
    assert row["config"]["system_prompt"] == {"type": "custom", "content": GENERAL_PURPOSE_PROMPT}
    assert "Explore" not in row["description"] + row["config"]["system_prompt"]["content"]
    _assert_owner_settings_kept(row)
    assert row["config"]["builtin_defaults"]["revision"] == BUILTIN_DEFAULTS_REVISION
    assert set(row["config"]["builtin_defaults"]["sha256"]) == {"name", "description", "prompt"}


@pytest.mark.usefixtures("legacy_builtin_catalog")
def test_startup_refreshes_unmodified_old_frontend_designer_prompt(mock_db):
    from app.core.builtin_subagents import (
        get_builtin_subagent_definition, seed_builtin_subagents,
    )

    seed_builtin_subagents()
    description = get_builtin_subagent_definition("frontend-designer")["description"]
    _store_existing_row("frontend-designer", description=description, prompt=_old_fd_prompt())

    assert seed_builtin_subagents()["frontend-designer"] == "refreshed"
    row = _raw("frontend-designer")
    assert row["config"]["system_prompt"]["content"] == FRONTEND_DESIGNER_PROMPT
    _assert_owner_settings_kept(row)


@pytest.mark.usefixtures("legacy_builtin_catalog")
def test_second_startup_changes_nothing(mock_db):
    from app.core.builtin_subagents import seed_builtin_subagents

    seed_builtin_subagents()
    _store_existing_row("general-purpose", description=_OLD_GP_DESCRIPTION, prompt=_old_gp_prompt())
    assert seed_builtin_subagents()["general-purpose"] == "refreshed"
    after_first = {agent_id: _raw(agent_id) for agent_id in ("general-purpose", "frontend-designer")}

    results = seed_builtin_subagents()

    assert {results[k] for k in after_first} == {"exists"}
    assert {agent_id: _raw(agent_id) for agent_id in after_first} == after_first


@pytest.mark.usefixtures("legacy_builtin_catalog")
def test_customised_prompt_is_kept_while_untouched_description_moves(mock_db):
    from app.core.builtin_subagents import get_builtin_subagent_definition, seed_builtin_subagents

    seed_builtin_subagents()
    _store_existing_row("general-purpose", description=_OLD_GP_DESCRIPTION, prompt="My own prompt.")

    assert seed_builtin_subagents()["general-purpose"] == "refreshed"
    row = _raw("general-purpose")
    assert row["config"]["system_prompt"]["content"] == "My own prompt."
    assert row["description"] == get_builtin_subagent_definition("general-purpose")["description"]
    assert set(row["config"]["builtin_defaults"]["sha256"]) == {"name", "description"}
    _assert_owner_settings_kept(row)


@pytest.mark.usefixtures("legacy_builtin_catalog")
def test_customised_description_is_kept_while_untouched_prompt_moves(mock_db):
    from app.core.agent_templates import GENERAL_PURPOSE_PROMPT
    from app.core.builtin_subagents import seed_builtin_subagents

    seed_builtin_subagents()
    _store_existing_row("general-purpose", description="My routing advice.", prompt=_old_gp_prompt())

    assert seed_builtin_subagents()["general-purpose"] == "refreshed"
    row = _raw("general-purpose")
    assert row["description"] == "My routing advice."
    assert row["config"]["system_prompt"]["content"] == GENERAL_PURPOSE_PROMPT
    _assert_owner_settings_kept(row)


@pytest.mark.usefixtures("legacy_builtin_catalog")
def test_refresh_failure_does_not_stop_startup_seeding(mock_db, monkeypatch):
    from app.core.builtin_subagents import BUILTIN_SUBAGENTS, seed_builtin_subagents
    from app.db import database

    seed_builtin_subagents()

    def broken(subagent_id):
        raise RuntimeError("refresh failed")

    monkeypatch.setattr(database, "refresh_builtin_subagent_defaults", broken)
    results = seed_builtin_subagents()
    assert {results[d["id"]] for d in BUILTIN_SUBAGENTS} == {"exists"}


@pytest.mark.usefixtures("legacy_builtin_catalog")
def test_fully_customised_row_is_not_written(mock_db):
    from app.core.builtin_subagents import seed_builtin_subagents

    seed_builtin_subagents()
    before = _store_existing_row("general-purpose", description="Mine.", prompt="Also mine.")

    assert seed_builtin_subagents()["general-purpose"] == "exists"
    assert _raw("general-purpose") == before


@pytest.mark.usefixtures("legacy_builtin_catalog")
def test_owner_text_matching_an_older_default_is_kept_once_recorded(mock_db):
    """A valid record decides alone: text set after seeding is the owner's,
    even when it equals an older shipped default."""
    from app.core.builtin_subagents import seed_builtin_subagents
    from app.db import database

    seed_builtin_subagents()
    database.revert_subagent_to_defaults("general-purpose")  # a current record, in any order
    database.update_subagent("general-purpose", description=_OLD_GP_DESCRIPTION, prompt=_old_gp_prompt())

    assert seed_builtin_subagents()["general-purpose"] == "exists"
    row = _raw("general-purpose")
    assert row["description"] == _OLD_GP_DESCRIPTION
    assert row["config"]["system_prompt"]["content"] == _old_gp_prompt()


@pytest.mark.usefixtures("legacy_builtin_catalog")
def test_fresh_install_seeds_current_defaults_with_a_record(bare_db):
    from app.core.builtin_subagents import (
        BUILTIN_DEFAULTS_REVISION, BUILTIN_SUBAGENTS, seed_builtin_subagents,
    )
    from app.db import database

    results = seed_builtin_subagents()
    assert {results[d["id"]] for d in BUILTIN_SUBAGENTS} == {"created"}
    first = {}
    for definition in BUILTIN_SUBAGENTS:
        stored = database.get_subagent(definition["id"])
        for field in ("name", "description", "prompt", "tools", "model"):
            assert stored[field] == definition[field], (definition["id"], field)
        row = _raw(definition["id"])
        assert row["config"]["builtin_defaults"]["revision"] == BUILTIN_DEFAULTS_REVISION
        assert set(row["config"]["builtin_defaults"]["sha256"]) == {"name", "description", "prompt"}
        first[definition["id"]] = row

    results = seed_builtin_subagents()
    assert {results[d["id"]] for d in BUILTIN_SUBAGENTS} == {"exists"}
    assert {k: _raw(k) for k in first} == first


@pytest.mark.usefixtures("legacy_builtin_catalog")
def test_record_from_a_newer_build_is_never_downgraded(mock_db):
    """A newer worker's refresh synced here: this older build leaves it alone,
    even a field that still holds text this build recognises as shipped."""
    from app.core.builtin_subagents import BUILTIN_DEFAULTS_REVISION, seed_builtin_subagents

    seed_builtin_subagents()
    newer = {"revision": BUILTIN_DEFAULTS_REVISION + 1, "sha256": {"prompt": "f" * 64}}
    before = _store_existing_row("general-purpose", description=_OLD_GP_DESCRIPTION,
                                 prompt="Newer shipped prompt", record=newer)

    assert seed_builtin_subagents()["general-purpose"] == "exists"
    assert _raw("general-purpose") == before


@pytest.mark.usefixtures("legacy_builtin_catalog")
def test_plan_retirement_refreshes_recorded_general_purpose_description(mock_db, monkeypatch):
    from app.core import builtin_subagents
    from app.db import database

    previous = [dict(definition) for definition in builtin_subagents.BUILTIN_SUBAGENTS]
    previous[0]["description"] = previous[0]["description"].replace(
        "rendered UI work fits", "implementation plans fit Plan, and rendered UI work fits")
    with monkeypatch.context() as old_build:
        old_build.setattr(builtin_subagents, "BUILTIN_SUBAGENTS", previous)
        old_build.setattr(builtin_subagents, "BUILTIN_DEFAULTS_REVISION", 1)
        builtin_subagents.seed_builtin_subagents()
    assert "implementation plans fit Plan" in database.get_subagent("general-purpose")["description"]
    database.update_subagent("general-purpose", prompt="Owner prompt", model="owner-model")

    assert builtin_subagents.seed_builtin_subagents()["general-purpose"] == "refreshed"
    agent = database.get_subagent("general-purpose")
    assert agent["description"] == builtin_subagents.BUILTIN_SUBAGENTS[0]["description"]
    assert agent["prompt"] == "Owner prompt"
    assert agent["model"] == "owner-model"
    assert builtin_subagents.seed_builtin_subagents()["general-purpose"] == "exists"


@pytest.mark.usefixtures("legacy_builtin_catalog")
def test_future_default_change_refreshes_through_the_record(mock_db, monkeypatch):
    """A later revision needs no new fingerprints: the record written at seed
    time and kept through an owner edit in the Agents editor decides."""
    from app.core import builtin_subagents
    from app.core.builtin_subagents import BUILTIN_SUBAGENTS, seed_builtin_subagents
    from app.db import database

    seed_builtin_subagents()
    # The owner edits only the model and the Frontend Designer prompt via the agent store.
    database.update_subagent("frontend-designer", prompt="Owner design prompt", model="sonnet")
    assert _raw("frontend-designer")["config"]["builtin_defaults"]["sha256"].get("prompt")

    changed = []
    for definition in BUILTIN_SUBAGENTS:
        definition = dict(definition)
        definition["description"] = definition["description"] + " (v2)"
        definition["prompt"] = definition["prompt"] + "\n(v2)"
        changed.append(definition)
    monkeypatch.setattr(builtin_subagents, "BUILTIN_SUBAGENTS", changed)
    monkeypatch.setattr(builtin_subagents, "BUILTIN_DEFAULTS_REVISION",
                        builtin_subagents.BUILTIN_DEFAULTS_REVISION + 1)

    results = seed_builtin_subagents()
    assert results["general-purpose"] == results["frontend-designer"] == "refreshed"
    gp = database.get_subagent("general-purpose")
    assert gp["description"].endswith(" (v2)") and gp["prompt"].endswith("\n(v2)")
    designer = database.get_subagent("frontend-designer")
    assert designer["prompt"] == "Owner design prompt"
    assert designer["model"] == "sonnet"
    assert designer["description"].endswith(" (v2)")
    assert seed_builtin_subagents()["frontend-designer"] == "exists"


@pytest.mark.usefixtures("legacy_builtin_catalog")
def test_owner_edit_in_the_agents_editor_keeps_the_record(authenticated_client, monkeypatch):
    from app.core import builtin_subagents
    from app.core.builtin_subagents import BUILTIN_SUBAGENTS, seed_builtin_subagents

    seed_builtin_subagents()
    profile = authenticated_client.get("/api/v1/agents/general-purpose").json()
    config = dict(profile["config"])
    config["system_prompt"] = {"type": "custom", "content": "Edited in the editor."}
    resp = authenticated_client.put("/api/v1/agents/general-purpose",
                                    json={"name": profile["name"], "config": config})
    assert resp.status_code == 200, resp.text
    assert "builtin_defaults" not in resp.json()["config"]  # internal: never exposed
    assert _raw("general-purpose")["config"]["builtin_defaults"]

    changed = [dict(d, description=d["description"] + " (v2)", prompt=d["prompt"] + " (v2)")
               for d in BUILTIN_SUBAGENTS]
    monkeypatch.setattr(builtin_subagents, "BUILTIN_SUBAGENTS", changed)
    monkeypatch.setattr(builtin_subagents, "BUILTIN_DEFAULTS_REVISION",
                        builtin_subagents.BUILTIN_DEFAULTS_REVISION + 1)
    seed_builtin_subagents()
    row = _raw("general-purpose")
    assert row["config"]["system_prompt"]["content"] == "Edited in the editor."
    assert row["description"].endswith(" (v2)")


@pytest.mark.usefixtures("legacy_builtin_catalog")
def test_restore_defaults_stamps_the_record(authenticated_client):
    from app.core.builtin_subagents import BUILTIN_DEFAULTS_REVISION, seed_builtin_subagents

    seed_builtin_subagents()
    _store_existing_row("general-purpose", description="Mine.", prompt="Also mine.")
    assert "builtin_defaults" not in _raw("general-purpose")["config"]

    resp = authenticated_client.post("/api/v1/agents/general-purpose/restore")
    assert resp.status_code == 200, resp.text
    record = _raw("general-purpose")["config"]["builtin_defaults"]
    assert record["revision"] == BUILTIN_DEFAULTS_REVISION
    assert set(record["sha256"]) == {"name", "description", "prompt"}


# Bump BUILTIN_DEFAULTS_REVISION whenever a built-in's name, description or
# prompt changes, then update this pin to (new revision, new digest).
_DEFAULTS_PIN = (3, "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945")


def _current_defaults_digest() -> str:
    import hashlib

    from app.core.builtin_subagents import BUILTIN_SUBAGENTS, DEFAULT_TEXT_FIELDS

    text = json.dumps([[d["id"]] + [d[f] for f in DEFAULT_TEXT_FIELDS] for d in BUILTIN_SUBAGENTS])
    return hashlib.sha256(text.encode()).hexdigest()


def test_default_text_changes_bump_the_revision():
    from app.core.builtin_subagents import BUILTIN_DEFAULTS_REVISION

    revision, digest = _DEFAULTS_PIN
    assert (BUILTIN_DEFAULTS_REVISION, _current_defaults_digest()) == (revision, digest), (
        "A built-in's default text changed: bump BUILTIN_DEFAULTS_REVISION so start-up refreshes "
        "uncustomised rows, then update _DEFAULTS_PIN.")
