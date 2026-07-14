"""
Regression tests: sessions created by automation runs must carry
``automation_id`` provenance.

Automation sessions run headless through execute_query (no live watch
surface), so the sessions list badges them to differentiate from interactive
chats. That badge keys on ``sessions.automation_id``, which execute_query
must forward to database.create_session when it creates the session itself.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from claude_agent_sdk import SystemMessage

import app.core.query_engine as qe

AUTOMATION_ID = "auto-nightly-report"


def _init_message():
    return SystemMessage(
        subtype="init", data={"session_id": "sdk-11111111-2222"}
    )


async def _noop_coro():
    return None


@pytest.fixture()
def engine_mocks():
    profile = {"id": "principal-engineer", "name": "PE"}
    options = MagicMock()
    options.cwd = None

    async def _noop(*args, **kwargs):
        return None

    async def quick_query(prompt, options):
        yield _init_message()

    with patch.object(qe, "get_profile", return_value=profile), patch.object(
        qe, "database"
    ) as mock_db, patch("app.core.sdk_options.build_options_from_profile", return_value=(options, {})
    ), patch.object(
        qe, "resolve_note_references", side_effect=lambda p: p
    ), patch.object(
        qe, "reconcile_memory", _noop
    ), patch.object(
        qe, "push_memory", _noop
    ), patch.object(
        qe.direct_oauth, "ensure_fresh_credentials", _noop
    ), patch.object(
        qe, "query", quick_query
    ), patch.object(
        qe, "_persist_turn_results",
        MagicMock(side_effect=lambda **kw: _noop_coro()),
    ), patch(
        "app.core.sync_engine.sync_engine.broadcast_user_session_created",
        new=AsyncMock(),
    ):
        mock_db.create_session.return_value = {"id": "new-session"}
        yield mock_db


@pytest.mark.asyncio
async def test_created_session_is_stamped_with_automation_id(engine_mocks):
    await qe.execute_query(
        prompt="nightly report",
        profile_id="principal-engineer",
        automation_id=AUTOMATION_ID,
    )

    engine_mocks.create_session.assert_called_once()
    assert (
        engine_mocks.create_session.call_args.kwargs["automation_id"]
        == AUTOMATION_ID
    )


@pytest.mark.asyncio
async def test_interactive_session_has_no_automation_id(engine_mocks):
    await qe.execute_query(
        prompt="hello",
        profile_id="principal-engineer",
    )

    engine_mocks.create_session.assert_called_once()
    assert engine_mocks.create_session.call_args.kwargs["automation_id"] is None
