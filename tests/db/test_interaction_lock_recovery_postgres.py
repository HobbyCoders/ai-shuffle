"""The real permission producer must survive a rolled-back event append (#1089)."""
import asyncio
from unittest.mock import AsyncMock

import pytest

from app.core import history_backfill, turn_recorder
from app.core.permission_handler import PermissionHandler
from app.db import interactions, session_events
from tests.core.interaction_harness import (  # noqa: F401
    no_connector_delivery, no_gateway_listener, no_push,
    reset_backfill_memo_on_setup, reset_interaction_wait_state,
)
from tests.db.postgres_harness import requires_real_pg
from tests.db.test_event_write_lock_timeout_postgres import (  # noqa: F401
    cluster, connect, session_id,
)

pytestmark = requires_real_pg


async def test_permission_card_arrives_once_after_counter_lock_timeout(connect, session_id):
    await history_backfill.ensure_backfilled(session_id)
    handler = PermissionHandler()
    broadcast = AsyncMock()
    holder = connect()
    holder.cursor().execute(
        "INSERT INTO session_event_counters (session_id, next_seq) VALUES (%s, 0) "
        "ON CONFLICT DO NOTHING", (session_id,),
    )
    holder.commit()
    holder.cursor().execute(
        "SELECT next_seq FROM session_event_counters WHERE session_id=%s FOR UPDATE", (session_id,),
    )
    task = asyncio.create_task(handler.request_permission(
        request_id="lock-request", session_id=session_id, profile_id="default",
        tool_name="Bash", tool_input={"command": "pwd"}, broadcast_func=broadcast,
    ))
    try:
        # The row is already durable while the event writer is blocked.
        async with asyncio.timeout(5):
            while not interactions.get_pending_interactions(session_id):
                await asyncio.sleep(0.01)
        await asyncio.sleep(2.3)  # past this private cluster's 2-second lock_timeout
        assert not session_events.get_events(session_id)
        holder.rollback()
        async with asyncio.timeout(4):
            while not session_events.get_events(session_id):
                await asyncio.sleep(0.01)
        rows = session_events.get_events(session_id)
        assert [r["event_type"] for r in rows] == ["interaction.requested"]
        assert rows[0]["payload"]["request"]["tool_name"] == "Bash"
        await handler.respond("lock-request", session_id, "deny", broadcast_func=broadcast)
        await asyncio.wait_for(task, 4)
        assert [r["event_type"] for r in session_events.get_events(session_id)] == [
            "interaction.requested", "interaction.answered",
        ]
    finally:
        holder.rollback()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        tail = turn_recorder._live_unfenced_write(session_id)
        if tail:
            await tail.task
