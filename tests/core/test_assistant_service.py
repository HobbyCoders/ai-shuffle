"""Retained owner identity and ordinary chat behavior after research retirement."""
import pytest
from app.core import assistant_service as service, task_registry
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
    turns.enqueue_turn(sid, "user", prompt="user continued")
    assert not service.session_stopped(sid)
