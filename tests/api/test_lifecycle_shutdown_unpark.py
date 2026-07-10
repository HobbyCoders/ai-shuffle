"""Unpark delivery must not start follow-up turns during shutdown (item 1).

Drives the REAL ``_deliver_unpark_impl`` (no fixture-planted shortcuts): when
``app.core.startup.is_shutting_down`` is set, the delivery path must refuse to
hand off a new ``run_query`` follow-up turn — the pooled clients are being torn
down underneath it — and must drop the ``unpark_pending`` latch so a polling
client is not held park-polling for a follow-up that will never stream.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

import app.core.startup as startup
import app.api.chat_ws_handler as cwh
from app.core.task_registry import task_registry


@pytest.fixture(autouse=True)
def _reset_state():
    startup.is_shutting_down = False
    task_registry.reset_all()
    yield
    startup.is_shutting_down = False
    task_registry.reset_all()


def _pending(task_id="t-1", status="killed"):
    return [{
        "task_id": task_id,
        "status": status,
        "summary": None,
        "output_file": None,
        "uuid": None,
        "usage": None,
        "tool_use_id": None,
    }]


async def test_deliver_unpark_refuses_new_followup_during_shutdown():
    startup.is_shutting_down = True
    run_query = AsyncMock()
    set_query_task = MagicMock()
    task_registry.mark_unpark_pending("sess-shutdown")

    delivered = await cwh._deliver_unpark_impl(
        pending=_pending(),
        session_id="sess-shutdown",
        profile_id="prof-1",
        project_id=None,
        worktree_id=None,
        ctx=MagicMock(),
        run_query=run_query,
        set_query_task=set_query_task,
    )

    assert delivered is False
    run_query.assert_not_called()
    set_query_task.assert_not_called()
    # The latch is dropped so a park-poller isn't stranded waiting on a
    # follow-up that will never stream.
    assert task_registry.is_unpark_pending("sess-shutdown") is False
