"""Unit tests for the owner-side cluster queue endpoint.

When a user queues a "send later" message on a cloud-mode session, the
controlling install relays it to the owning install's
``/api/v1/cluster/sessions/{id}/queue_message`` endpoint. Under V2 that endpoint
enqueues a durable ``source=queue`` turn in the shared turns table
(``turn_manager.submit``) — the same thing the local ``/ws/chat`` queue handler
does — then re-sources the ``queue_updated`` frame from the turns table (B3) and
broadcasts it. It must never relay again (which would loop) and must run the
work on the owning install (where the worktree lives).

Everything is mocked at the handler/DB boundary — no app, no DB, no network.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

import app.api.cluster as cluster_api
import app.core.turn_manager as turn_manager
from app.api.cluster import ClusterQueueMessageRequest, cluster_queue_message


async def _passthrough_run_sync(fn, *args, **kwargs):
    return fn(*args, **kwargs)


def _wire(monkeypatch, session, *, queue_view=None):
    monkeypatch.setattr(cluster_api, "run_sync", _passthrough_run_sync)
    monkeypatch.setattr(cluster_api.database, "get_session", lambda sid: session)

    submit = MagicMock(return_value="turn-q")
    monkeypatch.setattr(turn_manager, "submit", submit)

    import app.api.chat_ws_handler as ws
    view = queue_view if queue_view is not None else []
    monkeypatch.setattr(ws, "_v2_queue_view", lambda sid: view)

    import app.core.sync_engine as se
    broadcast = AsyncMock()
    monkeypatch.setattr(se.sync_engine, "broadcast_queue_updated", broadcast)
    return submit, broadcast


async def test_queue_message_forwards_to_local_queue_handler(monkeypatch):
    """Trims content, falls back to the session's project + profile, submits a
    ``source=queue`` turn, and broadcasts the re-sourced queue view. The
    'claude-code' default-profile sentinel resolves to the session's own
    profile."""
    fake_session = {
        "id": "sess-1", "project_id": "proj-A", "instance_id": "owner",
        "profile_id": "default-ai",
    }
    view = [{"id": "turn-q", "content": "build it", "position": 1}]
    submit, broadcast = _wire(monkeypatch, fake_session, queue_view=view)

    body = ClusterQueueMessageRequest(content="  build it  ", profile_id="claude-code")
    result = await cluster_queue_message("sess-1", body, install_id="peer-9")

    assert result == {"ok": True, "session_id": "sess-1"}
    # Durable queue turn: source, trimmed content, session-profile fallback,
    # session-project fallback.
    assert submit.call_args.args[0] == "sess-1"
    assert submit.call_args.args[1] == "queue"
    assert submit.call_args.kwargs["prompt"] == "build it"
    assert submit.call_args.kwargs["profile_id"] == "default-ai"
    assert submit.call_args.kwargs["project_id"] == "proj-A"
    # The re-sourced queue view is broadcast to devices.
    broadcast.assert_awaited_once_with("sess-1", view)


async def test_queue_message_prefers_explicit_project(monkeypatch):
    fake_session = {"id": "sess-1", "project_id": "proj-A", "instance_id": "owner"}
    submit, _ = _wire(monkeypatch, fake_session)

    body = ClusterQueueMessageRequest(content="go", project_id="proj-B")
    await cluster_queue_message("sess-1", body, install_id="peer")

    assert submit.call_args.kwargs["project_id"] == "proj-B"


async def test_queue_message_rejects_empty_content():
    # Validated before any DB/handler work, so no mocks needed.
    with pytest.raises(HTTPException) as ei:
        await cluster_queue_message(
            "sess-1", ClusterQueueMessageRequest(content="   "), install_id="x"
        )
    assert ei.value.status_code == 400


async def test_queue_message_404_when_session_missing(monkeypatch):
    monkeypatch.setattr(cluster_api, "run_sync", _passthrough_run_sync)
    monkeypatch.setattr(cluster_api.database, "get_session", lambda sid: None)

    with pytest.raises(HTTPException) as ei:
        await cluster_queue_message(
            "missing", ClusterQueueMessageRequest(content="hi"), install_id="x"
        )
    assert ei.value.status_code == 404


async def test_queue_message_500_when_submit_raises(monkeypatch):
    """A submit/broadcast failure surfaces as 500 (not a leaked traceback)."""
    fake_session = {"id": "sess-1", "project_id": None, "instance_id": "owner"}
    monkeypatch.setattr(cluster_api, "run_sync", _passthrough_run_sync)
    monkeypatch.setattr(cluster_api.database, "get_session", lambda sid: fake_session)

    def _boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(turn_manager, "submit", _boom)

    with pytest.raises(HTTPException) as ei:
        await cluster_queue_message(
            "sess-1", ClusterQueueMessageRequest(content="hi"), install_id="x"
        )
    assert ei.value.status_code == 500
