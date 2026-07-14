"""Profile resolution for the owner-side cluster message endpoint.

Regression tests for the "claude-code" sentinel leak: the controlling install's
WS handler defaults an omitted profile to the literal sentinel ``claude-code``
(chat_ws_handler.handle_query), which is NOT a real profile row. When that
sentinel crossed the relay verbatim, the owner-side ``cluster_message`` truthy
or-chain could never fall back to the session's own profile, and the worker's
execute_query died on an unlogged "Profile not found" yield. Under V2 the relay
submits a durable ``source=intake`` turn (``turn_manager.submit``) whose
``profile_id`` drives the eventual spawn, so the owner endpoint must still treat
the sentinel as "no explicit profile" and fall back to the session row.

Everything is mocked at the handler/DB boundary — no app, no DB, no network.
"""

from unittest.mock import AsyncMock, MagicMock

import app.api.cluster as cluster_api
import app.core.turn_manager as turn_manager
from app.api.cluster import ClusterMessageRequest, cluster_message


async def _passthrough_run_sync(fn, *args, **kwargs):
    return fn(*args, **kwargs)


def _wire(monkeypatch, session):
    monkeypatch.setattr(cluster_api, "run_sync", _passthrough_run_sync)
    monkeypatch.setattr(cluster_api.database, "get_session", lambda sid: session)
    monkeypatch.setattr(
        cluster_api.database, "add_session_message", lambda **kw: None, raising=False
    )

    import app.core.sync_engine as se
    monkeypatch.setattr(se.sync_engine, "broadcast_message_added", AsyncMock())

    # cluster_message imports ``from app.core import turn_manager`` inside the
    # function, so the binding is ``turn_manager.submit`` (a sync call returning
    # the new turn id).
    submit = MagicMock(return_value="turn-1")
    monkeypatch.setattr(turn_manager, "submit", submit)
    return submit


async def test_sentinel_profile_falls_back_to_session_profile(monkeypatch):
    """body.profile_id='claude-code' is the controller's "no profile" sentinel —
    the owner must submit the turn with the session's own profile, not the
    sentinel."""
    session = {
        "id": "sess-1", "project_id": "proj-A", "profile_id": "default-ai",
        "instance_id": "owner", "worktree_id": "wt-1",
    }
    submit = _wire(monkeypatch, session)

    body = ClusterMessageRequest(prompt="follow up", profile_id="claude-code")
    result = await cluster_message("sess-1", body, install_id="peer-9")

    assert result == {"ok": True, "session_id": "sess-1"}
    assert submit.call_args.kwargs["profile_id"] == "default-ai"
    # Server-originated relay turn.
    assert submit.call_args.args[1] == "intake"


async def test_omitted_profile_falls_back_to_session_profile(monkeypatch):
    session = {
        "id": "sess-1", "project_id": "proj-A", "profile_id": "default-ai",
        "instance_id": "owner", "worktree_id": None,
    }
    submit = _wire(monkeypatch, session)

    body = ClusterMessageRequest(prompt="follow up")
    await cluster_message("sess-1", body, install_id="peer-9")

    assert submit.call_args.kwargs["profile_id"] == "default-ai"


async def test_real_profile_wins_over_session_profile(monkeypatch):
    session = {
        "id": "sess-1", "project_id": "proj-A", "profile_id": "default-ai",
        "instance_id": "owner", "worktree_id": None,
    }
    submit = _wire(monkeypatch, session)

    body = ClusterMessageRequest(prompt="follow up", profile_id="principal-engineer")
    await cluster_message("sess-1", body, install_id="peer-9")

    assert submit.call_args.kwargs["profile_id"] == "principal-engineer"


async def test_queue_sentinel_profile_falls_back_to_session_profile(monkeypatch):
    """Same sentinel handling on the queue relay endpoint: the queued turn must
    carry the session's profile (the drain runs with the turn's profile_id)."""
    from app.api.cluster import ClusterQueueMessageRequest, cluster_queue_message

    session = {
        "id": "sess-1", "project_id": "proj-A", "profile_id": "default-ai",
        "instance_id": "owner",
    }
    monkeypatch.setattr(cluster_api, "run_sync", _passthrough_run_sync)
    monkeypatch.setattr(cluster_api.database, "get_session", lambda sid: session)

    submit = MagicMock(return_value="turn-q")
    monkeypatch.setattr(turn_manager, "submit", submit)

    import app.api.chat_ws_handler as ws
    monkeypatch.setattr(ws, "_v2_queue_view", lambda sid: [])
    import app.core.sync_engine as se
    monkeypatch.setattr(se.sync_engine, "broadcast_queue_updated", AsyncMock())

    body = ClusterQueueMessageRequest(content="later", profile_id="claude-code")
    await cluster_queue_message("sess-1", body, install_id="peer-9")

    assert submit.call_args.kwargs["profile_id"] == "default-ai"
    assert submit.call_args.args[1] == "queue"


# ─── execute_query observability: not-found paths must LOG, never yield silently ──


async def test_execute_query_logs_profile_not_found(monkeypatch, caplog):
    """The error yield is invisible on the socketless spawn path (no 'error'
    dispatch handler) — the log line is the ONLY trace."""
    import logging

    import app.core.query_engine as qe

    monkeypatch.setattr(qe, "get_profile", lambda pid: None)

    gen = qe.stream_to_websocket(
        prompt="hi", session_id="sess-x", profile_id="nope", project_id=None
    )
    with caplog.at_level(logging.ERROR, logger="app.core.query_engine"):
        event = await gen.__anext__()

    assert event == {"type": "error", "message": "Profile not found: nope"}
    assert any("Profile not found" in r.message and "sess-x" in r.message
               for r in caplog.records)
