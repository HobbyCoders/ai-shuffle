"""Park-notification gate in handle_done (lifecycle batch, item 5).

A turn that ends while background work is still running (the session PARKS,
awaiting the work's terminal notification) is NOT finished — yet handle_done
fired the "Session finished" mobile push AND the in-app success toast at every
ResultMessage. A parent that parked/unparked three times notified three times,
each one lying that the session was done. DECISION: notify only when the turn
ends fully idle — skip BOTH ``fire_and_forget_notify`` and
``emit_notification`` when ``task_registry.has_active_background_work``
(the pool-eviction predicate) is true for the session.

Drives the REAL ``handle_done`` (real registry transitions decide the gate);
only the two notification sinks are spied. The permission (#3) and
user-question (#4) notification sites are deliberately untouched.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.api.ws_event_handlers import EventContext, MutableStreamState, handle_done
from app.core.streaming_context import StreamingContext
from app.core.task_registry import task_registry


SESSION = "sess-done-gate"


def _ectx():
    ctx = StreamingContext(
        session_id=SESSION,
        device_id="dev-1",
        message_id="m-1",
        user_id="admin",
    )
    return EventContext(
        ctx=ctx,
        session_id=SESSION,
        message_id="m-1",
        user_id="admin",
        project_id=None,
        worktree_id=None,
    )


@pytest.fixture(autouse=True)
def _isolate():
    task_registry.reset_all()
    yield
    task_registry.reset_all()


def _notify_spies():
    fire = MagicMock()
    emit = AsyncMock()
    return (
        fire,
        emit,
        patch("app.services.push_notification.fire_and_forget_notify", fire),
        patch("app.services.notification_service.emit_notification", emit),
    )


async def _run_done():
    state = MutableStreamState(stream_started=True)
    await handle_done({"type": "done", "metadata": {}}, _ectx(), state)


async def test_done_with_active_background_work_notifies_nothing():
    # REAL registry transition: a background sub-agent is still running when
    # the parent's ResultMessage lands → the session parks, it is NOT done.
    task_registry.start_task(SESSION, "t-bg", "agent", "bg work",
                             tool_use_id="tu-bg")
    assert task_registry.has_active_background_work(SESSION) is True

    fire, emit, p_fire, p_emit = _notify_spies()
    with p_fire, p_emit:
        await _run_done()

    fire.assert_not_called()
    emit.assert_not_awaited()


async def test_done_fully_idle_notifies_both():
    # Background work already terminal → the turn genuinely finished.
    task_registry.start_task(SESSION, "t-bg", "agent", "bg work",
                             tool_use_id="tu-bg")
    task_registry.complete_task(SESSION, "t-bg", "completed")
    assert task_registry.has_active_background_work(SESSION) is False

    fire, emit, p_fire, p_emit = _notify_spies()
    with p_fire, p_emit:
        await _run_done()

    fire.assert_called_once()
    assert fire.call_args.kwargs["data"]["session_id"] == SESSION
    emit.assert_awaited_once()


async def test_interrupted_turn_still_notifies_nothing():
    """Existing behavior preserved: user-initiated interrupts never notify."""
    fire, emit, p_fire, p_emit = _notify_spies()
    state = MutableStreamState(stream_started=True)
    with p_fire, p_emit:
        await handle_done({"type": "interrupted", "metadata": {}}, _ectx(), state)

    fire.assert_not_called()
    emit.assert_not_awaited()
