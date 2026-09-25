"""The parked marker has to be cleared between turns, not only at turn end.

``sessions.parked_task_kind`` was written by ``turn_finalize`` at turn end and
cleared by the NEXT turn's end or a boot reconcile. When the last background
worker settles BETWEEN turns with no wake behind it there is no next turn, so
the marker stayed ``agent`` for the life of the process — two sessions in the
2026-09-05 audit sat exactly that way with both their tasks terminal, while
``lifecycle_state._detect_drift`` named the state (``parked-marker-while-idle``)
and only logged it.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch


from app.core.config import settings
from app.core.turn_finalize import clear_parked_marker_if_idle
from app.db import database


def _parked_session(test_db, kind="agent"):
    session_id = f"marker-{uuid.uuid4().hex[:12]}"
    database.create_session(session_id=session_id, profile_id="default")
    test_db.execute(
        "UPDATE sessions SET instance_id = ?, parked_task_kind = ? WHERE id = ?",
        (settings.effective_instance_id, kind, session_id),
    )
    test_db.commit()
    return session_id


def _marker(test_db, session_id):
    return test_db.execute(
        "SELECT parked_task_kind FROM sessions WHERE id = ?", (session_id,)
    ).fetchone()["parked_task_kind"]


def _registry(work):
    registry = MagicMock(name="task_registry")
    registry.get_active_background_work.return_value = work
    return registry


async def test_marker_cleared_once_the_last_worker_is_gone(mock_db, test_db):
    session_id = _parked_session(test_db)

    with patch("app.core.task_registry.task_registry", new=_registry([])), \
            patch("app.core.sync_engine.sync_engine") as sync:
        sync.broadcast_user_session_updated = AsyncMock()
        cleared = await clear_parked_marker_if_idle(session_id)

    assert cleared is True
    assert _marker(test_db, session_id) is None
    # The sidebar has to hear about it, same as the turn-end write.
    sync.broadcast_user_session_updated.assert_awaited_once()


async def test_marker_survives_while_other_work_is_still_running(mock_db, test_db):
    session_id = _parked_session(test_db)

    with patch(
        "app.core.task_registry.task_registry",
        new=_registry([{"id": "still-going", "kind": "agent"}]),
    ):
        cleared = await clear_parked_marker_if_idle(session_id)

    assert cleared is False
    assert _marker(test_db, session_id) == "agent"


async def test_clear_is_a_no_op_without_a_marker(mock_db, test_db):
    session_id = _parked_session(test_db, kind=None)

    with patch("app.core.task_registry.task_registry", new=_registry([])):
        assert await clear_parked_marker_if_idle(session_id) is False


async def test_clear_never_sets_the_marker(mock_db, test_db):
    """Setting it stays the TURN's decision — only a turn knows about launches
    with no registry record yet, and a terminal callback that could also set it
    would race a turn that is still running."""
    session_id = _parked_session(test_db, kind=None)

    with patch(
        "app.core.task_registry.task_registry",
        new=_registry([{"id": "t", "kind": "agent"}]),
    ):
        await clear_parked_marker_if_idle(session_id)

    assert _marker(test_db, session_id) is None


async def test_unknown_session_is_survivable(mock_db):
    with patch("app.core.task_registry.task_registry", new=_registry([])):
        assert await clear_parked_marker_if_idle("no-such-session") is False
    assert await clear_parked_marker_if_idle("") is False


# ---------------------------------------------------------------------------
# The terminal-task callback path
# ---------------------------------------------------------------------------

async def test_terminal_task_callback_settles_the_marker(mock_db, test_db):
    """The audited strand: the last worker reaches a terminal status, the
    scheduler schedules no wake, and nothing else would ever clear the row."""
    from app.core import idle_reader

    session_id = _parked_session(test_db)
    idle_reader.register_on_task_terminal(lambda sid, frame: None)
    try:
        with patch("app.core.task_registry.task_registry", new=_registry([])), \
                patch("app.core.sync_engine.sync_engine") as sync:
            sync.broadcast_user_session_updated = AsyncMock()
            await idle_reader._fire_on_task_terminal(
                session_id, {"task_id": "worker", "status": "killed"}
            )
    finally:
        idle_reader.register_on_task_terminal(None)

    assert _marker(test_db, session_id) is None


async def test_terminal_settle_runs_even_with_no_hook_registered(mock_db, test_db):
    from app.core import idle_reader

    session_id = _parked_session(test_db)
    idle_reader.register_on_task_terminal(None)

    with patch("app.core.task_registry.task_registry", new=_registry([])), \
            patch("app.core.sync_engine.sync_engine") as sync:
        sync.broadcast_user_session_updated = AsyncMock()
        result = await idle_reader._fire_on_task_terminal(
            session_id, {"task_id": "worker", "status": "completed"}
        )

    assert result is None
    assert _marker(test_db, session_id) is None


async def test_a_failing_hook_still_leaves_the_marker_settled(mock_db, test_db):
    from app.core import idle_reader

    session_id = _parked_session(test_db)

    def _boom(sid, frame):
        raise RuntimeError("scheduler exploded")

    idle_reader.register_on_task_terminal(_boom)
    try:
        with patch("app.core.task_registry.task_registry", new=_registry([])), \
                patch("app.core.sync_engine.sync_engine") as sync:
            sync.broadcast_user_session_updated = AsyncMock()
            result = await idle_reader._fire_on_task_terminal(
                session_id, {"task_id": "worker", "status": "error"}
            )
    finally:
        idle_reader.register_on_task_terminal(None)

    assert result is None
    assert _marker(test_db, session_id) is None


async def test_the_scheduler_signal_still_reaches_the_reader(mock_db, test_db):
    """The CLI-owned-wake signal is what arms the reader's grace window; the
    added settle must not swallow it."""
    from app.core import idle_reader

    session_id = _parked_session(test_db)
    idle_reader.register_on_task_terminal(
        lambda sid, frame: idle_reader.CLI_OWNED_WAKE_SIGNAL
    )
    try:
        with patch("app.core.task_registry.task_registry", new=_registry([])), \
                patch("app.core.sync_engine.sync_engine") as sync:
            sync.broadcast_user_session_updated = AsyncMock()
            result = await idle_reader._fire_on_task_terminal(
                session_id, {"task_id": "worker", "status": "completed"}
            )
    finally:
        idle_reader.register_on_task_terminal(None)

    assert result == idle_reader.CLI_OWNED_WAKE_SIGNAL


# ---------------------------------------------------------------------------
# The periodic backstop
# ---------------------------------------------------------------------------
#
# A task can also be settled by stop_session_task, by force cleanup, or by a
# worker supervisor — none of which pass through the idle reader. The 5-minute
# sweep catches whatever marker those leave behind.

async def test_sweep_clears_a_marker_with_nothing_left_to_park_on(mock_db, test_db):
    from app.core.startup_reconcile import sweep_stale_parked_markers

    session_id = _parked_session(test_db)

    with patch("app.core.task_registry.task_registry", new=_registry([])), \
            patch("app.core.sync_engine.sync_engine") as sync:
        sync.broadcast_user_session_updated = AsyncMock()
        assert await sweep_stale_parked_markers() == 1

    assert _marker(test_db, session_id) is None


async def test_sweep_keeps_a_marker_whose_durable_worker_outlived_the_registry(
    mock_db, test_db
):
    """A registry eviction must not be read as "the work is over" — the durable
    row is the surviving truth."""
    from app.core.startup_reconcile import sweep_stale_parked_markers
    from app.db import background_tasks as background_tasks_db

    session_id = _parked_session(test_db)
    background_tasks_db.upsert_background_task({
        "task_id": "survivor",
        "session_id": session_id,
        "kind": "agent",
        "status": "running",
        "title": "survivor",
        "started_at": "2026-09-05T00:00:00+00:00",
        "updated_at": "2026-09-05T00:00:01+00:00",
    })

    with patch("app.core.task_registry.task_registry", new=_registry([])):
        assert await sweep_stale_parked_markers() == 0

    assert _marker(test_db, session_id) == "agent"


async def test_sweep_leaves_another_installs_sessions_alone(mock_db, test_db):
    from app.core.startup_reconcile import sweep_stale_parked_markers

    session_id = _parked_session(test_db)
    test_db.execute(
        "UPDATE sessions SET instance_id = ? WHERE id = ?",
        ("some-other-install", session_id),
    )
    test_db.commit()

    with patch("app.core.task_registry.task_registry", new=_registry([])):
        assert await sweep_stale_parked_markers() == 0

    assert _marker(test_db, session_id) == "agent"
