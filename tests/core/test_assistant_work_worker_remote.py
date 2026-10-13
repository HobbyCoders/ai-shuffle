"""The assistant's default worker with real remote dispatch (S5/S6).

Only the target's ``task-start`` answer is faked: the call goes remote when
the target accepts, falls back here on a refusal that started nothing, and is
never rerun here when the target may have started it.
"""
import pytest

from app.network import control
from tests.core.test_assistant_work_worker import (  # noqa: F401 - fixtures
    assistant_fixture, callers, dispatch, network, save,
)


def _target_answers(monkeypatch, behaviour):
    sent = []

    async def call(device_id, action, body, **kwargs):
        sent.append((device_id, action, body))
        return behaviour(body)
    monkeypatch.setattr(control, "call", call)
    return sent


@pytest.mark.parametrize("role", ["home", "job"])
def test_assistant_work_goes_to_the_default_worker_when_it_starts(callers, network, monkeypatch, role):
    save("dev-unraid")
    sent = _target_answers(monkeypatch, lambda body: {
        "worker_session_id": body["worker_session_id"], "owner_device_id": "dev-unraid",
        "permission_mode": "default", "checkout": {"mode": "none", "branch": None}})
    payload, calls = dispatch(callers[role])
    assert payload["ok"] is True and payload["mode"] == "remote" and calls == []
    assert payload["worker"] == {"device_id": "dev-unraid", "name": "ServerWorker"}
    assert "worker_note" not in payload
    [(device_id, action, _)] = sent
    assert (device_id, action) == ("dev-unraid", "task-start")


def test_a_pre_start_refusal_from_the_default_worker_runs_here(callers, network, monkeypatch):
    save("dev-unraid")

    def refuse(body):
        raise control.ControlRefused("The agent 'helper' does not exist on this worker; nothing was started.")
    _target_answers(monkeypatch, refuse)
    payload, calls = dispatch(callers["home"])
    assert payload["ok"] is True and len(calls) == 1
    assert payload["worker_note"] == ("Ran on this worker: ServerWorker refused the subagent: The agent "
                                      "'helper' does not exist on this worker.")


def test_a_default_worker_that_may_have_started_it_is_not_rerun_here(callers, network, monkeypatch):
    save("dev-unraid")

    def timeout(body):
        raise control.RunnerUnreachable("ServerWorker did not answer in time while handling a request from "
                                        "DesktopWorker. It may still have been applied; check before retrying.")
    _target_answers(monkeypatch, timeout)
    payload, calls = dispatch(callers["home"])
    assert payload["ok"] is False and "did not confirm the subagent" in payload["error"]
    assert calls == [] and "worker_note" not in payload
