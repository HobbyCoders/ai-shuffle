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

# execute_query consumes the provider MIRROR vocabulary (adapter.receive_events()
# already translated), not the raw SDK types.
from app.core.provider.events import SystemMessage

import app.core.query_engine as qe
from app.db import turns as turns_db

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

    async def quick_events():
        yield _init_message()

    # execute_query now spawns through the provider seam (make_adapter), so the
    # stub replaces the adapter rather than the raw SDK query().
    adapter = MagicMock()
    adapter.connect_with_sdk_options = AsyncMock()
    adapter.send_query = AsyncMock()
    adapter.disconnect = AsyncMock()
    adapter.receive_events = MagicMock(side_effect=lambda: quick_events())

    with patch.object(qe, "get_profile", return_value=profile), patch.object(
        qe, "database"
    ) as mock_db, patch("app.core.sdk_options.build_options_from_profile", return_value=(options, {})
    ), patch.object(
        qe, "resolve_note_references", side_effect=lambda p: p
    ), patch.object(
        qe, "reconcile_memory", _noop
    ), patch.object(
        qe, "push_memory", _noop
    ), patch(
        "app.core.provider.factory.make_adapter", return_value=adapter
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
async def test_automation_session_is_not_active(engine_mocks):
    """Automation-created sessions run headless and must NOT land in the
    user's Active group (is_active False)."""
    await qe.execute_query(
        prompt="nightly report",
        profile_id="principal-engineer",
        automation_id=AUTOMATION_ID,
    )

    engine_mocks.create_session.assert_called_once()
    assert engine_mocks.create_session.call_args.kwargs["is_active"] is False


@pytest.mark.asyncio
async def test_interactive_session_has_no_automation_id(engine_mocks):
    await qe.execute_query(
        prompt="hello",
        profile_id="principal-engineer",
    )

    engine_mocks.create_session.assert_called_once()
    assert engine_mocks.create_session.call_args.kwargs["automation_id"] is None
    # Interactive (non-automation) sessions ARE active.
    assert engine_mocks.create_session.call_args.kwargs["is_active"] is True


# ---------------------------------------------------------------------------
# Ad-hoc turn accounting
# ---------------------------------------------------------------------------
# execute_query's record_adhoc_turn call is deliberately wrapped in try/except
# so a failed accounting write can never break a turn. That also means a
# drifted kwarg or a broken import degrades into a log line nobody reads, with
# every existing test still green. tests/db/test_turns.py covers the writer;
# only a call-site test that reads the row back covers the wiring.


@pytest.mark.asyncio
async def test_execute_query_records_an_adhoc_turn(engine_mocks):
    result = await qe.execute_query(
        prompt="hello",
        profile_id="principal-engineer",
    )

    row = turns_db.get_latest_turn(result["session_id"])
    assert row is not None
    assert row["state"] == "completed"
    assert row["source"] == "api_query"
    assert row["prompt"] == "hello"
    assert row["profile_id"] == "principal-engineer"
    # The turn ran outside the scheduler, so there was never a lease to point at.
    assert row["run_id"] is None


@pytest.mark.asyncio
async def test_adhoc_turn_from_an_automation_is_sourced_automation_step(engine_mocks):
    """Automation steps go through the same call as ad-hoc REST queries, so
    ``source`` is the only thing separating their spend in the turns history."""
    result = await qe.execute_query(
        prompt="nightly report",
        profile_id="principal-engineer",
        automation_id=AUTOMATION_ID,
    )

    row = turns_db.get_latest_turn(result["session_id"])
    assert row is not None
    assert row["source"] == "automation_step"
    assert row["state"] == "completed"
    assert row["prompt"] == "nightly report"
    assert row["run_id"] is None
