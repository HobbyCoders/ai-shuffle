"""Idle detection must not rebuild a transcript just to inspect task presence."""

from unittest.mock import Mock

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
