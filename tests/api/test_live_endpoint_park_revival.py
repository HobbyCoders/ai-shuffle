"""
U1 regression: a parked session's /live delta poll must surface activeTasks,
never a bare 304.

The /live endpoint is the sole authority the session poller uses to decide
whether to keep a parked session alive. When a delta poll (since_update_id > 0)
finds no MESSAGE updates but the session still has active background work (a
sub-agent / SDK-backgrounded shell) — or an unpark follow-up mid-handoff — the
endpoint MUST return a lightweight 200 body carrying activeTasks / unpark_pending
instead of a 304 that would hide the state and let the poller stop, stranding the
parked UI until a full reload.

This guard runs on the OWNING install. The cluster /live endpoint delegates to
this same function (app.api.cluster.cluster_live -> get_live_session), and
cloud_relay.relay_live forwards the owner's 200 body verbatim — so locking the
owner-side behavior here also pins cloud-session revival across the relay (U1).
"""

from unittest.mock import AsyncMock, MagicMock, patch

from starlette.responses import Response

from app.api.sessions import get_live_session
from app.core.task_registry import task_registry
from app.core.live_session_store import live_session_store


def _owner_path_patches(session):
    """Force the LOCAL owner path: not a remote/relayed session, no worktree DB."""
    return [
        patch("app.api.sessions.require_session", new=AsyncMock(return_value=session)),
        patch("app.api.sessions.enrich_session_worktree_meta", new=MagicMock(return_value={})),
        patch("app.core.cloud_relay.is_remote_session", new=MagicMock(return_value=False)),
    ]


async def test_parked_session_delta_poll_returns_active_tasks_not_304():
    """Active background work + no message delta -> 200 body with activeTasks."""
    session_id = "u1-parked-sess"
    session = {"id": session_id, "title": "Parked"}
    # A background sub-agent is in flight; the parent turn ended (not streaming).
    task_registry.start_task(session_id, "task-1", "agent", "bg work")

    patches = _owner_path_patches(session)
    for p in patches:
        p.start()
    try:
        with patch.object(live_session_store, "has_session", return_value=True), \
             patch.object(live_session_store, "is_streaming", return_value=False), \
             patch.object(live_session_store, "get_session_since", return_value=None):
            result = await get_live_session(
                request=MagicMock(), session_id=session_id,
                since_update_id=7, token="tok",
            )
    finally:
        for p in patches:
            p.stop()
        task_registry.clear_session(session_id)

    # NOT a 304: a dict body carrying the live activeTasks so the poller keeps
    # the parked session alive.
    assert not isinstance(result, Response)
    assert isinstance(result, dict)
    assert result["activeTasks"], "parked session must surface its active task"
    assert result["activeTasks"][0]["id"] == "task-1"
    assert result["is_streaming"] is False


async def test_unpark_pending_delta_poll_returns_body_not_304():
    """Unpark follow-up mid-handoff (no task yet) still returns a body, not 304."""
    session_id = "u1-unpark-sess"
    session = {"id": session_id, "title": "Unparking"}
    task_registry.mark_unpark_pending(session_id)

    patches = _owner_path_patches(session)
    for p in patches:
        p.start()
    try:
        with patch.object(live_session_store, "has_session", return_value=True), \
             patch.object(live_session_store, "is_streaming", return_value=False), \
             patch.object(live_session_store, "get_session_since", return_value=None):
            result = await get_live_session(
                request=MagicMock(), session_id=session_id,
                since_update_id=3, token="tok",
            )
    finally:
        for p in patches:
            p.stop()
        task_registry.clear_session(session_id)

    assert not isinstance(result, Response)
    assert isinstance(result, dict)
    assert result["unpark_pending"] is True


async def test_idle_session_delta_poll_returns_304():
    """Negative control: no work + no message delta -> genuine 304."""
    session_id = "u1-idle-sess"
    session = {"id": session_id, "title": "Idle"}
    task_registry.clear_session(session_id)  # ensure no stray tasks

    patches = _owner_path_patches(session)
    for p in patches:
        p.start()
    try:
        with patch.object(live_session_store, "has_session", return_value=True), \
             patch.object(live_session_store, "is_streaming", return_value=False), \
             patch.object(live_session_store, "get_session_since", return_value=None):
            result = await get_live_session(
                request=MagicMock(), session_id=session_id,
                since_update_id=7, token="tok",
            )
    finally:
        for p in patches:
            p.stop()

    assert isinstance(result, Response)
    assert result.status_code == 304


# =============================================================================
# Cloud relay passthrough (U1's original framing): a parked CLOUD session must
# revive on poll. The owner runs the same guard above; relay_live forwards its
# 200 body verbatim, so activeTasks reaches the viewing install untouched.
# =============================================================================

async def test_cloud_relay_passes_through_owner_active_tasks():
    """Relayed session: the owner's 200 body (with activeTasks) flows through."""
    session_id = "u1-cloud-sess"
    session = {"id": session_id, "title": "Cloud", "cloud_branch": "feature/x"}
    owner_body = {
        "session_id": session_id,
        "is_streaming": False,
        "last_update_id": 12,
        "messages": [],
        "activeTasks": [{"id": "remote-task", "status": "running", "kind": "agent"}],
        "unpark_pending": False,
        "session_meta": {"title": "Cloud"},
    }
    with patch("app.api.sessions.require_session", new=AsyncMock(return_value=session)), \
         patch("app.core.cloud_relay.is_remote_session", new=MagicMock(return_value=True)), \
         patch("app.core.cloud_relay.relay_live", new=AsyncMock(return_value=(200, owner_body))):
        result = await get_live_session(
            request=MagicMock(), session_id=session_id,
            since_update_id=5, token="tok",
        )

    assert not isinstance(result, Response)
    assert isinstance(result, dict)
    assert result["activeTasks"][0]["id"] == "remote-task"
    # Relay block stamps cloud meta onto the owner's body.
    assert result["session_meta"]["execution_mode"] == "cloud"
    assert result["session_meta"]["device_online"] is True


async def test_cloud_relay_304_when_owner_idle():
    """Relayed session: owner 304 (no work) passes straight through as 304."""
    session_id = "u1-cloud-idle"
    session = {"id": session_id, "title": "Cloud"}
    with patch("app.api.sessions.require_session", new=AsyncMock(return_value=session)), \
         patch("app.core.cloud_relay.is_remote_session", new=MagicMock(return_value=True)), \
         patch("app.core.cloud_relay.relay_live", new=AsyncMock(return_value=(304, None))):
        result = await get_live_session(
            request=MagicMock(), session_id=session_id,
            since_update_id=5, token="tok",
        )

    assert isinstance(result, Response)
    assert result.status_code == 304
