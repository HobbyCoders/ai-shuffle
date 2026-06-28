"""Unit tests for the owner-side cluster queue endpoint.

When a user queues a "send later" message on a cloud-mode session, the
controlling install relays it to the owning install's
``/api/v1/cluster/sessions/{id}/queue_message`` endpoint. That endpoint must
store + drain the message *locally* (where the worktree lives) by reusing the
same server-side queue handler the live ``/ws/chat`` path uses — never relaying
again (which would loop) and never running the query on the wrong install.

Everything is mocked at the handler/DB boundary — no app, no DB, no network.
"""

import pytest
from fastapi import HTTPException

import app.api.cluster as cluster_api
from app.api.cluster import ClusterQueueMessageRequest, cluster_queue_message


async def _passthrough_run_sync(fn, *args, **kwargs):
    return fn(*args, **kwargs)


async def test_queue_message_forwards_to_local_queue_handler(monkeypatch):
    """Trims content, falls back to the session's project, runs as admin."""
    fake_session = {"id": "sess-1", "project_id": "proj-A", "instance_id": "owner"}
    monkeypatch.setattr(cluster_api, "run_sync", _passthrough_run_sync)
    monkeypatch.setattr(cluster_api.database, "get_session", lambda sid: fake_session)

    captured: dict = {}

    class FakeHandler:
        def __init__(self, *, user_id, api_user_id):
            captured["user_id"] = user_id
            captured["api_user_id"] = api_user_id
            self.current_session_id = None

        async def handle_queue_message(self, data):
            captured["session_at_call"] = self.current_session_id
            captured["data"] = data

    import app.core.headless_query_runner as hqr
    monkeypatch.setattr(hqr, "HeadlessChatHandler", FakeHandler)

    body = ClusterQueueMessageRequest(content="  build it  ", profile_id="claude-code")
    result = await cluster_queue_message("sess-1", body, install_id="peer-9")

    assert result == {"ok": True, "session_id": "sess-1"}
    # Runs as admin (the originating install already authed its own user).
    assert captured["user_id"] == "admin"
    assert captured["api_user_id"] is None
    assert captured["session_at_call"] == "sess-1"
    # Content trimmed; project falls back to the session's own project.
    assert captured["data"] == {
        "session_id": "sess-1",
        "content": "build it",
        "profile": "claude-code",
        "project": "proj-A",
    }


async def test_queue_message_prefers_explicit_project(monkeypatch):
    fake_session = {"id": "sess-1", "project_id": "proj-A", "instance_id": "owner"}
    monkeypatch.setattr(cluster_api, "run_sync", _passthrough_run_sync)
    monkeypatch.setattr(cluster_api.database, "get_session", lambda sid: fake_session)

    captured: dict = {}

    class FakeHandler:
        def __init__(self, *, user_id, api_user_id):
            self.current_session_id = None

        async def handle_queue_message(self, data):
            captured["data"] = data

    import app.core.headless_query_runner as hqr
    monkeypatch.setattr(hqr, "HeadlessChatHandler", FakeHandler)

    body = ClusterQueueMessageRequest(content="go", project_id="proj-B")
    await cluster_queue_message("sess-1", body, install_id="peer")

    assert captured["data"]["project"] == "proj-B"


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


async def test_queue_message_500_when_handler_raises(monkeypatch):
    """A store/handler failure surfaces as 500 (not a leaked traceback)."""
    fake_session = {"id": "sess-1", "project_id": None, "instance_id": "owner"}
    monkeypatch.setattr(cluster_api, "run_sync", _passthrough_run_sync)
    monkeypatch.setattr(cluster_api.database, "get_session", lambda sid: fake_session)

    class BoomHandler:
        def __init__(self, *, user_id, api_user_id):
            self.current_session_id = None

        async def handle_queue_message(self, data):
            raise RuntimeError("db down")

    import app.core.headless_query_runner as hqr
    monkeypatch.setattr(hqr, "HeadlessChatHandler", BoomHandler)

    with pytest.raises(HTTPException) as ei:
        await cluster_queue_message(
            "sess-1", ClusterQueueMessageRequest(content="hi"), install_id="x"
        )
    assert ei.value.status_code == 500
