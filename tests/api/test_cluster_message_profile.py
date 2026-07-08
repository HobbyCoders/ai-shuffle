"""Profile resolution for the owner-side cluster message endpoint.

Regression tests for the "claude-code" sentinel leak: the controlling install's
WS handler defaults an omitted profile to the literal sentinel ``claude-code``
(chat_ws_handler.handle_query), which is NOT a real profile row. When that
sentinel crossed the relay verbatim, ``cluster_message``'s truthy or-chain
could never fall back to the session's own profile, and the worker's
execute_query died on an unlogged "Profile not found" yield — a silent no-op
on the headless path (live store force-cleaned ~8ms after launch, no error_log
row, no ring-buffer line). The owner endpoint must treat the sentinel as
"no explicit profile" and fall back to the session row.

Everything is mocked at the handler/DB boundary — no app, no DB, no network.
"""

from unittest.mock import AsyncMock

import app.api.cluster as cluster_api
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

    import app.core.headless_query_runner as hqr
    launch = AsyncMock()
    monkeypatch.setattr(hqr, "launch_headless_query", launch)
    return launch


async def test_sentinel_profile_falls_back_to_session_profile(monkeypatch):
    """body.profile_id='claude-code' is the controller's "no profile" sentinel —
    the owner must run with the session's own profile, not the sentinel."""
    session = {
        "id": "sess-1", "project_id": "proj-A", "profile_id": "default-ai",
        "instance_id": "owner", "worktree_id": "wt-1",
    }
    launch = _wire(monkeypatch, session)

    body = ClusterMessageRequest(prompt="follow up", profile_id="claude-code")
    result = await cluster_message("sess-1", body, install_id="peer-9")

    assert result == {"ok": True, "session_id": "sess-1"}
    assert launch.await_args.kwargs["profile_id"] == "default-ai"


async def test_omitted_profile_falls_back_to_session_profile(monkeypatch):
    session = {
        "id": "sess-1", "project_id": "proj-A", "profile_id": "default-ai",
        "instance_id": "owner", "worktree_id": None,
    }
    launch = _wire(monkeypatch, session)

    body = ClusterMessageRequest(prompt="follow up")
    await cluster_message("sess-1", body, install_id="peer-9")

    assert launch.await_args.kwargs["profile_id"] == "default-ai"


async def test_real_profile_wins_over_session_profile(monkeypatch):
    session = {
        "id": "sess-1", "project_id": "proj-A", "profile_id": "default-ai",
        "instance_id": "owner", "worktree_id": None,
    }
    launch = _wire(monkeypatch, session)

    body = ClusterMessageRequest(prompt="follow up", profile_id="principal-engineer")
    await cluster_message("sess-1", body, install_id="peer-9")

    assert launch.await_args.kwargs["profile_id"] == "principal-engineer"


async def test_queue_sentinel_profile_falls_back_to_session_profile(monkeypatch):
    """Same sentinel handling on the queue relay endpoint: the queued row must
    carry the session's profile (drain runs with the row's profile_id)."""
    from app.api.cluster import ClusterQueueMessageRequest, cluster_queue_message

    session = {
        "id": "sess-1", "project_id": "proj-A", "profile_id": "default-ai",
        "instance_id": "owner",
    }
    monkeypatch.setattr(cluster_api, "run_sync", _passthrough_run_sync)
    monkeypatch.setattr(cluster_api.database, "get_session", lambda sid: session)

    captured: dict = {}

    class FakeHandler:
        def __init__(self, *, user_id, api_user_id):
            self.current_session_id = None

        async def handle_queue_message(self, data):
            captured["data"] = data

    import app.core.headless_query_runner as hqr
    monkeypatch.setattr(hqr, "HeadlessChatHandler", FakeHandler)

    body = ClusterQueueMessageRequest(content="later", profile_id="claude-code")
    await cluster_queue_message("sess-1", body, install_id="peer-9")

    assert captured["data"]["profile"] == "default-ai"


# ─── execute_query observability: not-found paths must LOG, never yield silently ──


async def test_execute_query_logs_profile_not_found(monkeypatch, caplog):
    """The error yield is invisible on the headless path (no 'error' dispatch
    handler, live store force-cleaned) — the log line is the ONLY trace."""
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
