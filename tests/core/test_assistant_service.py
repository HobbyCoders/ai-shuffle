"""Retained owner identity and ordinary chat behavior after research retirement."""

import ast
from pathlib import Path
from typing import Any

import pytest
from app.core import owner_services as service, task_registry
from app.db import database, turns
from tests.db.test_assistant_store import assistant_fixture

# ---------------------------------------------------------------------------
# Prompt assembly: every chat gets the same delegation text
# ---------------------------------------------------------------------------

async def _assemble(session_id, profile_id):
    from unittest.mock import AsyncMock
    from app.core.provider import harness_adapter as ha
    from app.core.query_options import build_options_from_profile
    options, _ = build_options_from_profile(database.get_profile(profile_id), session_id=session_id)
    adapter = ha.HarnessAdapter()
    adapter.connect = AsyncMock()
    await adapter.connect_with_sdk_options(options)
    projected = adapter.connect.await_args.args[0]
    return options, projected, "\n\n".join(ha._workflow_prompt_blocks(projected, None))


@pytest.mark.asyncio
async def test_no_chat_gets_a_personal_assistant_prompt(assistant_fixture):
    from app.core.agent_provider_models import DELEGATE_WHEN_AUTHORIZED
    for session_id, profile_id in ((assistant_fixture["id"], "assistant-agent"),
                                   (database.create_session("on-builtin", service.AGENT_ID, is_active=True)["id"],
                                    service.AGENT_ID)):
        options, projected, workflow = await _assemble(session_id, profile_id)
        assert "# Personal assistant" not in str(options.system_prompt)
        assert "assistant_home" not in projected.extra
        assert DELEGATE_WHEN_AUTHORIZED in workflow


def test_any_session_can_settle(assistant_fixture):
    assert not database.set_session_active(assistant_fixture["id"], False)["is_active"]
    on_builtin = database.create_session("on-builtin", service.AGENT_ID, is_active=True, hidden=True)
    assert not database.set_session_active(on_builtin["id"], False)["is_active"]


def test_stop_latch_and_durable_stop_are_honored(assistant_fixture):
    sid = assistant_fixture["id"]
    task_registry.task_registry.mark_user_stopped(sid)
    assert service.session_stopped(sid)
    task_registry.task_registry.clear_user_stop(sid)
    turn = turns.enqueue_turn(sid, "user", prompt="run")
    turns.settle_turn(turn["id"], "aborted", stop_reason="user_stopped")
    assert service.session_stopped(sid)
    # A held progress note after the Stop never ran and does not mask it.
    note = turns.enqueue_turn(sid, "agent_message", prompt="tests 40/80")
    turns.remove_queued_turn(note["id"], stop_reason="held_progress")
    assert service.session_stopped(sid)
    turns.enqueue_turn(sid, "user", prompt="user continued")
    assert not service.session_stopped(sid)


def test_retired_service_cannot_seed_a_home_or_research_agent() -> None:
    for name in (
        "ensure_home", "ensure_agent", "AGENT_NAME", "AGENT_DESCRIPTION", "AGENT_CONFIG",
        "register_proactive_guard", "proactive_guard_available",
    ):
        assert not hasattr(service, name), name


def test_no_production_imports_reintroduce_retired_research_services() -> None:
    app = Path(__file__).resolve().parents[2] / "app"
    retired = {
        "app.core.assistant_manager", "app.core.assistant_research",
        "app.core.assistant_brief", "app.db.assistant_sources",
        "app.core.assistant_service", "app.core.assistant_introduction",
        "app.core.assistant_confirm", "app.core.assistant_permissions",
        "app.core.assistant_limits", "app.db.assistant_limits",
        "app.db.assistant", "app.api.assistant",
    }
    for path in app.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            imported: list[str] = []
            if isinstance(node, ast.Import):
                imported = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported = [node.module, *(node.module + "." + a.name for a in node.names)]
            assert not retired.intersection(imported), str(path)
            # Also catch importlib / __import__ and string-based patch targets.
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert not any(
                    node.value == name or node.value.startswith(name + ".")
                    for name in retired
                ), str(path)


def test_legacy_dedicated_metadata_is_not_live_identity_policy(assistant_fixture: Any) -> None:
    before = database.get_profile("assistant-agent")
    config = {**before["config"], "assistant_dedicated": True, "avatar_id": "spark"}
    database.update_profile("assistant-agent", config=config)
    identity = next(a for a in service.agent_identities() if a["id"] == "assistant-agent")
    assert identity["dedicated"] is False
    assert identity["avatar_id"] == "spark"
    service.update_agent_identity("assistant-agent", name="Ordinary agent")
    assert database.get_profile("assistant-agent")["config"] == config
