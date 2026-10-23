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

    monkeypatch.setattr(streaming_status, "_has_active_run_db", lambda sid: False)
    pool = MagicMock()
    pool.peek = MagicMock(return_value=None)
    pool._is_awaiting_user_input = MagicMock(return_value=False)
    pool.remove = AsyncMock(return_value=None)
    monkeypatch.setattr("app.core.session_pool.get_session_pool", lambda: pool)
    broadcast = AsyncMock()
    monkeypatch.setattr("app.core.sync_engine.sync_engine.broadcast_user_session_updated", broadcast)
    monkeypatch.setattr(attach_api, "_owned_here", lambda session: True)
    monkeypatch.setattr(attach_api, "get_api_user_from_request", lambda request: None)
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

    monkeypatch.setattr(streaming_status, "_has_active_run_db", lambda sid: True)
    with pytest.raises(HTTPException) as exc:
        await _attach("attach-chat")
    assert exc.value.status_code == 409
    assert exc.value.detail == attach_api.BUSY
    assert db.get_session("attach-chat")["project_id"] is None


@pytest.mark.asyncio
async def test_an_unreadable_busy_state_counts_as_busy(seeded, quiet, monkeypatch):
    from app.core import streaming_status

    def broken(sid):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(streaming_status, "_has_active_run_db", broken)
    with pytest.raises(HTTPException) as exc:
        await _attach("attach-chat")
    assert exc.value.detail == attach_api.BUSY
    assert db.get_session("attach-chat")["mode"] == "chat"


@pytest.mark.asyncio
async def test_a_turn_live_in_this_process_is_busy_even_if_the_database_lags(seeded, quiet, monkeypatch):
    """``has_active_run`` is cached and its lease row may not be written yet;
    the in-memory runtime, executions and pooled client are read first."""
    from app.core import session_runtime

    monkeypatch.setattr(session_runtime, "get_live_runtime", lambda sid: object())
    with pytest.raises(HTTPException) as exc:
        await _attach("attach-chat")
    assert exc.value.detail == attach_api.BUSY

    monkeypatch.setattr(session_runtime, "get_live_runtime", lambda sid: None)
    pool, _ = quiet
    pool.peek.return_value = MagicMock(is_streaming=True, idle_reader_active=False)
    with pytest.raises(HTTPException) as exc:
        await _attach("attach-chat")
    assert exc.value.detail == attach_api.BUSY
    assert db.get_session("attach-chat")["project_id"] is None


@pytest.mark.asyncio
async def test_a_turn_queued_after_the_busy_check_blocks_the_write(seeded, quiet, monkeypatch):
    """The write re-checks under turn admission's session lock, so a turn
    admitted after the endpoint's check (here: right before the write) is
    never left running on the old client's ceiling or cut off."""
    from app.db import turns

    real = db.attach_session_project

    def queue_then_attach(*args):
        turns.enqueue_turn("attach-chat", "user", prompt="hi")
        return real(*args)

    monkeypatch.setattr(db, "attach_session_project", queue_then_attach)
    pool, _ = quiet
    with pytest.raises(HTTPException) as exc:
        await _attach("attach-chat")
    assert exc.value.status_code == 409
    assert exc.value.detail == attach_api.BUSY
    row = db.get_session("attach-chat")
    assert row["mode"] == "chat" and row["project_id"] is None
    pool.remove.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_turn_parked_on_background_work_blocks_and_says_so(seeded, quiet):
    """A parked turn still holds the session (its background work runs on the
    old client), but the list shows it as done: the refusal names background
    work rather than a running turn."""
    from app.db import turns
    from app.db.base import get_db

    turn = turns.enqueue_turn("attach-chat", "user", prompt="hi")
    with get_db() as conn:
        conn.cursor().execute("UPDATE turns SET state='parked' WHERE id=%s", (turn["id"],))
    with pytest.raises(HTTPException) as exc:
        await _attach("attach-chat")
    assert exc.value.status_code == 409
    assert exc.value.detail == attach_api.BUSY_BACKGROUND
    assert "background work" in exc.value.detail
    assert db.get_session("attach-chat")["project_id"] is None


@pytest.mark.asyncio
async def test_a_client_that_starts_streaming_after_the_write_is_not_cut_off(seeded, quiet):
    """Checked again right before the release: a turn that started on the
    pooled client after the write keeps it (its own options check rebuilds the
    client for the new cwd), and the attach still lands."""
    pool, _ = quiet
    calls = {"n": 0}

    def peek(sid):
        calls["n"] += 1
        return None if calls["n"] == 1 else MagicMock(is_streaming=True, idle_reader_active=False)

    pool.peek.side_effect = peek
    result = await _attach("attach-chat")
    assert result.project_id == "attach-proj"
    pool.remove.assert_not_awaited()


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


@pytest.mark.parametrize("before", ["bypassPermissions", "auto", "acceptEdits", None])
@pytest.mark.asyncio
async def test_attach_puts_the_session_back_on_ask(seeded, quiet, tmp_path, before):
    """Bypass and Auto were picked for a chat that could not reach a shell.
    After the attach the next turn's options must be Ask, in the project's
    folder, with the project tool set, whatever the old binding was. A NULL
    binding would fall back to the agent's default, here Bypass."""
    import app.core.query_options as qo
    from app.core.session_modes import CHAT_MODE_TOOL_SURFACE

    project_dir = tmp_path / "web-app"
    project_dir.mkdir()
    db.create_project(project_id="attach-real", name="Web app", description=None, path=str(project_dir))
    if before:
        db.set_session_run_binding("attach-chat", permission_mode=before)
        assert db.get_session("attach-chat")["permission_mode"] == before

    result = await _attach("attach-chat", "attach-real")
    assert result.permission_mode == "default"
    row = db.get_session("attach-chat")
    assert row["permission_mode"] == "default"

    with patch("app.core.session_orchestration.is_owner_session", return_value=False), patch.object(
        qo, "get_security_instructions", return_value=""
    ), patch.object(qo, "_rescue_pending_checkout", return_value=False):
        options, _ = qo.build_options_from_profile(
            profile={"id": "attach-agent", "config": {"model": "opus", "permission_mode": "bypassPermissions"}},
            project=db.get_project("attach-real"),
            session_id="attach-chat",
        )
    assert options.permission_mode == "default"
    assert str(options.cwd) == str(project_dir)
    assert not set(options.allowed_tools or []) <= set(CHAT_MODE_TOOL_SURFACE)


def test_the_database_write_refuses_a_row_that_already_has_a_project(seeded):
    assert db.attach_session_project("attach-code", "attach-proj", None) is None
    assert db.attach_session_project("attach-chat", "attach-proj", None)["project_id"] == "attach-proj"
    assert db.attach_session_project("attach-chat", "attach-proj", None) is None


def test_the_route_is_served(seeded, quiet):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.auth import require_admin
    from app.api.sessions import router

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_admin] = lambda: "token"
    with patch.object(attach_api, "require_session", AsyncMock(return_value=db.get_session("attach-code"))):
        response = TestClient(app).post(
            "/api/v1/sessions/attach-code/attach-project", json={"project_id": "attach-proj"})
    # Reached the handler: refused because it already has a project.
    assert response.status_code == 409
    assert response.json()["detail"] == attach_api.ALREADY_HAS_PROJECT


# =============================================================================
# Who may attach: the owner only
# =============================================================================

OWNER_PAIRED = "shc_owner-device-session"
MEMBER_PAIRED = "shc_member-device-session"


class _Surface:
    """Marks the listener a request arrived on, as ``app.serve`` does."""

    def __init__(self, app, ingress="local"):
        self.app, self.ingress = app, ingress

    async def __call__(self, scope, receive, send):
        scope["shuffle.ingress"] = self.ingress
        await self.app(scope, receive, send)


@pytest.fixture
def routed(seeded, quiet, tmp_path, monkeypatch):
    """The real sessions router behind the real ``require_admin``."""
    from fastapi import FastAPI

    from app.api import auth
    from app.api.sessions import router
    from app.core import owner_auth
    from app.core.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "cookie_secure", False)
    monkeypatch.setattr(owner_auth, "trusted_lan", lambda: False)
    paired = {OWNER_PAIRED: {"id": "pc-owner", "account_owner_admin": True},
              MEMBER_PAIRED: {"id": "pc-member", "account_owner_admin": False}}
    monkeypatch.setattr(auth, "get_paired_client_from_token",
                        lambda token, touch=True: dict(paired[token]) if token in paired else None)
    monkeypatch.setattr(attach_api, "require_session",
                        AsyncMock(side_effect=lambda request, sid: db.get_session(sid)))
    app = FastAPI()
    app.include_router(router)
    owner_token, _ = owner_auth.issue_session()
    return app, owner_token


def _client(app, *, host="192.168.1.20:8000", peer="192.168.1.50", bearer=None, cookies=None):
    from fastapi.testclient import TestClient

    base = f"http://{host}"
    headers = {"Origin": base}
    if bearer:
        headers["Authorization"] = "Bearer " + bearer
    tc = TestClient(_Surface(app), base_url=base, client=(peer, 12345), headers=headers)
    for name, value in (cookies or {}).items():
        tc.cookies.set(name, value)
    return tc


def _post(tc):
    return tc.post("/api/v1/sessions/attach-chat/attach-project", json={"project_id": "attach-proj"})


def _untouched():
    row = db.get_session("attach-chat")
    return row["mode"] == "chat" and row["project_id"] is None


def test_an_api_key_user_is_refused(routed):
    from app.api import auth
    from app.db import api_users
    import os

    app, _ = routed
    key = "aiu_" + os.urandom(16).hex()
    api_users.create_api_user("attach-user", "Integration", auth.hash_api_key(key))
    response = _post(_client(app, bearer=key))
    assert response.status_code == 403
    assert _untouched()


def test_a_non_owner_paired_client_is_refused(routed):
    app, _ = routed
    response = _post(_client(app, bearer=MEMBER_PAIRED))
    assert response.status_code == 403
    assert _untouched()


def test_a_preview_cookie_is_refused_even_alongside_the_owner(routed):
    from app.core import preview_access

    app, owner_token = routed
    preview = {preview_access.cookie_name(): preview_access.mint("agent-session")}
    # The agent browser's grant alone, from a LAN peer...
    assert _post(_client(app, cookies=preview)).status_code == 403
    # ...or riding along with an owner cookie on loopback: still refused.
    both = _client(app, host="127.0.0.1:8000", peer="127.0.0.1",
                   cookies={**preview, "session": owner_token})
    assert _post(both).status_code == 403
    assert _untouched()
    # The same owner cookie without the grant is admitted: the grant refused it.
    owner = _client(app, host="127.0.0.1:8000", peer="127.0.0.1", cookies={"session": owner_token})
    assert _post(owner).status_code == 200


def test_the_owner_and_an_owner_paired_phone_may_attach(routed):
    app, owner_token = routed
    response = _post(_client(app, bearer=OWNER_PAIRED))
    assert response.status_code == 200, response.text
    assert response.json()["permission_mode"] == "default"
    assert db.get_session("attach-chat")["project_id"] == "attach-proj"
