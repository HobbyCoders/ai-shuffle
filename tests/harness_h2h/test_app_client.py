"""Idle detection must not rebuild a transcript just to inspect task presence."""

from contextlib import contextmanager
from itertools import count
from unittest.mock import Mock

import httpx
import pytest

from scripts.harness_h2h import app_client


@pytest.mark.parametrize("busy", [
    {"has_running_tasks": True, "parked_task_kind": None},
    {"has_running_tasks": False, "parked_task_kind": "monitor"},
])
def test_wait_idle_uses_transcript_free_metadata(monkeypatch, busy):
    client = object.__new__(app_client.AppClient)
    client._req = Mock(side_effect=[busy, {
        "has_running_tasks": False, "parked_task_kind": None,
    }])
    client.state = Mock(return_value={"is_streaming": False})
    monkeypatch.setattr(app_client, "IDLE_GRACE_S", 0)
    monkeypatch.setattr(app_client.time, "time", lambda: 1.0)
    monkeypatch.setattr(app_client.time, "sleep", lambda _delay: None)

    assert client.wait_idle("chat", deadline=10) == (1.0, False)
    assert client._req.call_count == 2
    for call in client._req.call_args_list:
        assert call.args == ("GET", "/sessions/chat/live")
        assert call.kwargs == {"params": {"include_messages": "false"}}
    # Work pins the busy state without polling /state until it has finished.
    client.state.assert_called_once_with("chat")


def test_wait_idle_still_checks_streaming_and_deadline(monkeypatch):
    client = object.__new__(app_client.AppClient)
    client._req = Mock(return_value={"has_running_tasks": False, "parked_task_kind": None})
    client.state = Mock(return_value={"is_streaming": True})
    clock = iter([1.0, 2.0])
    monkeypatch.setattr(app_client.time, "time", lambda: next(clock))
    monkeypatch.setattr(app_client.time, "sleep", lambda _delay: None)

    assert client.wait_idle("chat", deadline=2) == (None, True)
    client.state.assert_called_once_with("chat")
    client._req.assert_called_once_with("GET", "/sessions/chat/live", params={"include_messages": "false"})


@contextmanager
def _http_client(handler):
    client = object.__new__(app_client.AppClient)
    app_client.HttpWorkerClient.__init__(
        client, "http://worker.test", transport=httpx.MockTransport(handler),
    )
    try:
        yield client
    finally:
        client.close()


@pytest.mark.parametrize("status,metadata", [
    (200, {}),
    (200, {"has_running_tasks": False}),
    (200, {"parked_task_kind": None}),
    (404, {"detail": "Not found"}),
    (405, {"detail": "Method not allowed"}),
    (501, {"detail": "Not implemented"}),
])
def test_wait_idle_falls_back_for_older_workers(monkeypatch, status, metadata):
    requests = []
    legacy = iter([
        {"has_running_tasks": False, "parked_task_kind": "monitor"},
        {"has_running_tasks": False, "parked_task_kind": None},
    ])

    def handle(request):
        requests.append((request.url.path, dict(request.url.params)))
        if request.url.path.endswith("/live"):
            return httpx.Response(status, json=metadata)
        if request.url.path.endswith("/state"):
            return httpx.Response(200, json={"is_streaming": False})
        assert request.url.path == "/api/v1/sessions/chat"
        return httpx.Response(200, json=next(legacy))

    clock = count(1)
    monkeypatch.setattr(app_client, "IDLE_GRACE_S", 0)
    monkeypatch.setattr(app_client.time, "time", lambda: next(clock))
    monkeypatch.setattr(app_client.time, "sleep", lambda _delay: None)
    with _http_client(handle) as client:
        idle_at, timed_out = client.wait_idle("chat", deadline=10)
    assert not timed_out
    assert idle_at is not None
    # The parked marker keeps the first poll busy even though /state is idle.
    assert requests == [
        ("/api/v1/sessions/chat/live", {"include_messages": "false"}),
        ("/api/v1/sessions/chat", {"message_limit": "5"}),
        ("/api/v1/sessions/chat/live", {"include_messages": "false"}),
        ("/api/v1/sessions/chat", {"message_limit": "5"}),
        ("/api/v1/sessions/chat/state", {}),
    ]


@pytest.mark.parametrize("status", [400, 401, 403, 429, 500, 502, 503])
def test_wait_idle_never_falls_back_on_auth_or_server_failure(monkeypatch, status):
    requests = []

    def handle(request):
        requests.append(request.url.path)
        # Even a misleading status in the body must not drive fallback.
        return httpx.Response(status, json={"detail": "HTTP 404: other failure"})

    monkeypatch.setattr(app_client.time, "time", lambda: 1.0)
    with _http_client(handle) as client, pytest.raises(app_client.ClientError) as refused:
        client.wait_idle("chat", deadline=10)
    assert requests == ["/api/v1/sessions/chat/live"]
    assert refused.value.status_code == status


def test_wait_idle_does_not_fall_back_on_transport_failure(monkeypatch):
    requests = []

    def handle(request):
        requests.append(request.url.path)
        raise httpx.ConnectError("Synthetic connection failure", request=request)

    monkeypatch.setattr(app_client.time, "time", lambda: 1.0)
    with _http_client(handle) as client, pytest.raises(app_client.ClientError, match="unreachable"):
        client.wait_idle("chat", deadline=10)
    assert requests == ["/api/v1/sessions/chat/live"]
