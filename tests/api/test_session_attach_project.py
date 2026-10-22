"""POST /sessions/{id}/attach-project: give a no-project session its project.

Issue #371 step 4. The attach turns the row into an ordinary project session
(mode NULL, project columns set), is one-way, and refuses sessions it must not
touch: one that already has a project, a busy one, one run by another worker.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from starlette.requests import Request

import app.api.sessions_attach as attach_api
from app.api.sessions_attach import AttachProjectRequest, attach_project
from app.db import database as db


def _request():
    return Request({"type": "http", "headers": []})


@pytest.fixture
def seeded():
    db.create_profile(profile_id="attach-agent", name="Agent", description=None, config={})
    db.create_project(project_id="attach-proj", name="Web app", description=None, path="/attach-proj")
    db.create_session(session_id="attach-chat", profile_id="attach-agent", mode="chat")
    db.create_session(session_id="attach-code", profile_id="attach-agent", project_id="attach-proj")
    yield


@pytest.fixture
def quiet(monkeypatch):
    """No live run, no pooled client, no sync fan-out, owned here."""
    from app.core import streaming_status

    monkeypatch.setattr(streaming_status, "has_active_run", lambda sid: False)
    pool = MagicMock()
    pool.remove = AsyncMock(return_value=None)
    monkeypatch.setattr("app.core.session_pool.get_session_pool", lambda: pool)
    broadcast = AsyncMock()
    monkeypatch.setattr("app.core.sync_engine.sync_engine.broadcast_user_session_updated", broadcast)
    monkeypatch.setattr(attach_api, "_owned_here", lambda session: True)
    monkeypatch.setattr(attach_api, "get_api_user_from_request", lambda request: None)
    monkeypatch.setattr(attach_api, "refuse_assistant_home_mutation", AsyncMock())
    return pool, broadcast


async def _attach(session_id, project_id="attach-proj"):
    session = db.get_session(session_id)
    with patch.object(attach_api, "require_session", AsyncMock(return_value=session)):
        return await attach_project(_request(), session_id, AttachProjectRequest(project_id=project_id), "token")


@pytest.mark.asyncio
async def test_attach_turns_a_no_project_session_into_a_project_session(seeded, quiet):
    pool, broadcast = quiet
    result = await _attach("attach-chat")
    assert result.project_id == "attach-proj"
    assert result.mode == "code"
    row = db.get_session("attach-chat")
    assert row["project_id"] == "attach-proj"
    # Code sessions store NULL (session_modes.storage_session_mode).
    assert row["mode"] is None
    # The live client carried the no-project ceiling; it is closed, not ended.
    pool.remove.assert_awaited_once_with("attach-chat", preserve_session=True)
    broadcast.assert_awaited_once()
    assert broadcast.await_args.kwargs["updates"]["project_id"] == "attach-proj"


@pytest.mark.asyncio
async def test_attach_is_one_way(seeded, quiet):
    await _attach("attach-chat")
    db.create_project(project_id="attach-other", name="Other", description=None, path="/attach-other")
    with pytest.raises(HTTPException) as exc:
        await _attach("attach-chat", "attach-other")
    assert exc.value.status_code == 409
    assert db.get_session("attach-chat")["project_id"] == "attach-proj"


@pytest.mark.asyncio
async def test_a_project_session_is_refused(seeded, quiet):
    with pytest.raises(HTTPException) as exc:
        await _attach("attach-code")
    assert exc.value.status_code == 409
    assert db.get_session("attach-code")["project_id"] == "attach-proj"


@pytest.mark.asyncio
async def test_a_busy_session_is_refused_without_writing(seeded, quiet, monkeypatch):
    from app.core import streaming_status

    monkeypatch.setattr(streaming_status, "has_active_run", lambda sid: True)
    with pytest.raises(HTTPException) as exc:
        await _attach("attach-chat")
    assert exc.value.status_code == 409
    assert exc.value.detail == attach_api.BUSY
    assert db.get_session("attach-chat")["project_id"] is None


@pytest.mark.asyncio
async def test_a_session_run_by_another_worker_is_refused(seeded, quiet, monkeypatch):
    monkeypatch.setattr(attach_api, "_owned_here", lambda session: False)
    with pytest.raises(HTTPException) as exc:
        await _attach("attach-chat")
    assert exc.value.status_code == 409
    assert exc.value.detail == attach_api.NOT_HERE
    assert db.get_session("attach-chat")["mode"] == "chat"


@pytest.mark.asyncio
async def test_an_unknown_project_is_refused(seeded, quiet):
    with pytest.raises(HTTPException) as exc:
        await _attach("attach-chat", "no-such-project")
    assert exc.value.status_code == 404
    assert db.get_session("attach-chat")["project_id"] is None


def test_the_database_write_refuses_a_row_that_already_has_a_project(seeded):
    assert db.attach_session_project("attach-code", "attach-proj", None) is None
    assert db.attach_session_project("attach-chat", "attach-proj", None)["project_id"] == "attach-proj"
    assert db.attach_session_project("attach-chat", "attach-proj", None) is None


def test_the_route_is_served(seeded, quiet):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.auth import require_auth
    from app.api.sessions import router

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_auth] = lambda: "token"
    with patch.object(attach_api, "require_session", AsyncMock(return_value=db.get_session("attach-code"))):
        response = TestClient(app).post(
            "/api/v1/sessions/attach-code/attach-project", json={"project_id": "attach-proj"})
    # Reached the handler: refused because it already has a project.
    assert response.status_code == 409
    assert response.json()["detail"] == attach_api.ALREADY_HAS_PROJECT
