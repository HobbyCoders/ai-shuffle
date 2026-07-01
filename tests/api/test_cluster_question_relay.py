"""Unit tests for the owner-side cluster AskUserQuestion relay endpoints.

A pending question lives ONLY in the owning install's in-memory
``user_question_handler``. When a user drives a cloud-mode session from a
*different* install, that viewer install:

- relays ``get_pending_questions`` to the owner's
  ``/api/v1/cluster/sessions/{id}/pending_questions`` so the modal can surface, and
- relays the answer to the owner's
  ``/api/v1/cluster/sessions/{id}/question_response`` so the owner's awaiting
  ``request_answers`` coroutine unblocks.

These endpoints must operate on the LOCAL handler (they run on the owner) and
never re-relay. Everything is mocked at the handler/DB boundary — no app, no DB,
no network.
"""

import pytest
from fastapi import HTTPException

import app.api.cluster as cluster_api
from app.api.cluster import (
    ClusterQuestionResponseRequest,
    cluster_pending_questions,
    cluster_question_response,
)


async def _passthrough_run_sync(fn, *args, **kwargs):
    return fn(*args, **kwargs)


async def test_pending_questions_returns_local_handler_set(monkeypatch):
    """Reads the owner's LOCAL pending questions and returns them by shared id."""
    fake_session = {"id": "sess-1", "instance_id": "owner"}
    monkeypatch.setattr(cluster_api, "run_sync", _passthrough_run_sync)
    monkeypatch.setattr(cluster_api.database, "get_session", lambda sid: fake_session)

    captured = {}
    pending = [{"request_id": "q-1", "session_id": "sess-1", "questions": []}]

    from app.core import user_question_handler as uqh_mod

    def fake_get_pending(session_id):
        captured["session_id"] = session_id
        return pending

    monkeypatch.setattr(
        uqh_mod.user_question_handler, "get_pending_questions", fake_get_pending
    )

    result = await cluster_pending_questions("sess-1", install_id="peer-9")

    assert result == {"session_id": "sess-1", "questions": pending}
    assert captured["session_id"] == "sess-1"


async def test_pending_questions_404_when_session_missing(monkeypatch):
    monkeypatch.setattr(cluster_api, "run_sync", _passthrough_run_sync)
    monkeypatch.setattr(cluster_api.database, "get_session", lambda sid: None)

    with pytest.raises(HTTPException) as ei:
        await cluster_pending_questions("missing", install_id="x")
    assert ei.value.status_code == 404


async def test_question_response_resolves_local_waiter(monkeypatch):
    """Delivers the answer to the owner's LOCAL handler.respond and reports success."""
    fake_session = {"id": "sess-1", "instance_id": "owner"}
    monkeypatch.setattr(cluster_api, "run_sync", _passthrough_run_sync)
    monkeypatch.setattr(cluster_api.database, "get_session", lambda sid: fake_session)

    captured = {}

    from app.core import user_question_handler as uqh_mod

    async def fake_respond(*, request_id, session_id, answers):
        captured["request_id"] = request_id
        captured["session_id"] = session_id
        captured["answers"] = answers
        return True

    monkeypatch.setattr(uqh_mod.user_question_handler, "respond", fake_respond)

    body = ClusterQuestionResponseRequest(request_id="q-1", answers={"Approval": "yes"})
    result = await cluster_question_response("sess-1", body, install_id="peer-9")

    assert result == {"ok": True, "session_id": "sess-1", "request_id": "q-1"}
    assert captured == {
        "request_id": "q-1",
        "session_id": "sess-1",
        "answers": {"Approval": "yes"},
    }


async def test_question_response_reports_false_when_no_pending(monkeypatch):
    """A stale/unknown request_id surfaces ok=False (respond returned False)."""
    fake_session = {"id": "sess-1", "instance_id": "owner"}
    monkeypatch.setattr(cluster_api, "run_sync", _passthrough_run_sync)
    monkeypatch.setattr(cluster_api.database, "get_session", lambda sid: fake_session)

    from app.core import user_question_handler as uqh_mod

    async def fake_respond(*, request_id, session_id, answers):
        return False

    monkeypatch.setattr(uqh_mod.user_question_handler, "respond", fake_respond)

    body = ClusterQuestionResponseRequest(request_id="stale", answers={"x": "y"})
    result = await cluster_question_response("sess-1", body, install_id="x")

    assert result == {"ok": False, "session_id": "sess-1", "request_id": "stale"}


async def test_question_response_404_when_session_missing(monkeypatch):
    monkeypatch.setattr(cluster_api, "run_sync", _passthrough_run_sync)
    monkeypatch.setattr(cluster_api.database, "get_session", lambda sid: None)

    with pytest.raises(HTTPException) as ei:
        await cluster_question_response(
            "missing",
            ClusterQuestionResponseRequest(request_id="q-1", answers={"a": "b"}),
            install_id="x",
        )
    assert ei.value.status_code == 404
