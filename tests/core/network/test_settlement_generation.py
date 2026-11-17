"""Double wake and coalesced settle through signed membership on two PG workers."""
from datetime import datetime, timezone

import pytest

from app.core import limit_resume, session_snooze
from app.db import database, network as store
from app.network import engine, session_active
from tests.core.network.harness import converge, exchange, join
from tests.core.network.test_control import _new_session
from tests.core.network.test_pending_apply_budget import pg_workers, private_cluster  # noqa: F401


@pytest.mark.parametrize("settles", [False, True])
async def test_late_peer_wake_or_coalesced_settle(pg_workers, settles):
    a, b = pg_workers("owner"), pg_workers("peer")
    with a.active():
        engine.create_network("Home")
    join(a, b)
    _new_session(a, "s", "Snoozed")
    due = datetime.now(timezone.utc)
    with a.active():
        session_active.write_shared("s", False, session_snooze.canonical(due))
        fence = limit_resume._once_fence("s")
    converge([a, b])
    with a.active():
        assert await session_snooze.wake_due_once(due) == 1
    with b.active():
        assert await session_snooze.wake_due_once(due) == 0
        # The delayed peer is the winning writer; A's wake has not arrived.
        with database.get_db() as conn:
            store.observe_clock(conn.cursor(), 10000)
        engine.hold_outbox(True)
        try:
            if settles:
                session_active.write_shared("s", False)
                session_active.write_shared("s", True)
            else:
                assert await session_snooze.wake_due_once(due + session_snooze.PEER_GRACE) == 1
            assert engine.scan_all() == 0
        finally:
            engine.hold_outbox(False)
        engine.scan_all()
    result = exchange(b, a)
    assert result.applied and not result.retry and not result.rejected
    with a.active():
        assert limit_resume._same_fence(fence, limit_resume._once_fence("s")) is not settles
