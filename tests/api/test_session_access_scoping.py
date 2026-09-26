"""Per-session access on the sync socket, interrupt, streaming list and
JSONL import routes.

A restricted API user (project_id/profile_ids set) may only reach sessions in
its scope; importing a server-side JSONL file is owner-only and confined to
the transcript roots.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.api import query as query_api
from app.api import ws_sync
from app.api.sessions_export import ImportJSONLRequest, import_jsonl_session
from tests.api.helpers import fake_request

FOREIGN_USER = {"id": "u1", "project_id": None, "profile_ids": ["other-profile"]}


# -- /ws/sessions/{id} --------------------------------------------------------


def _socket():
    socket = MagicMock()
    socket.accept = AsyncMock()
    socket.close = AsyncMock()
    socket.send_json = AsyncMock()
    return socket


@pytest.mark.asyncio
async def test_sync_socket_closes_4003_for_foreign_session(mock_db):
    from app.db import database

    database.create_session("sess-ws", "default")
    socket = _socket()
    with patch.object(ws_sync, "authenticate_websocket", AsyncMock(return_value=(True, FOREIGN_USER))), \
            patch.object(ws_sync, "sync_engine") as engine:
        await ws_sync.session_sync_websocket(socket, "sess-ws", device_id="d1", token="aih_x")
    socket.close.assert_awaited_once_with(code=4003, reason="Access denied")
    socket.send_json.assert_not_awaited()
    engine.register_device.assert_not_called()


@pytest.mark.asyncio
async def test_sync_socket_still_404s_unknown_session(mock_db):
    socket = _socket()
    with patch.object(ws_sync, "authenticate_websocket", AsyncMock(return_value=(True, FOREIGN_USER))), \
            patch.object(ws_sync, "sync_engine"):
        await ws_sync.session_sync_websocket(socket, "sess-missing", device_id="d1", token="aih_x")
    socket.close.assert_awaited_once_with(code=4004, reason="Session not found")


# -- POST /session/{id}/interrupt and GET /streaming/active ----------------


@pytest.mark.asyncio
async def test_interrupt_denies_foreign_session(mock_db):
    from app.db import database

    database.create_session("sess-int", "default")
    with patch.object(query_api, "interrupt_session", AsyncMock(return_value=True)) as interrupt:
        with pytest.raises(HTTPException) as exc:
            await query_api.interrupt(fake_request(FOREIGN_USER), "sess-int", token="t")
    assert exc.value.status_code == 403
    interrupt.assert_not_awaited()


@pytest.mark.asyncio
async def test_interrupt_allows_owner_and_in_scope_user(mock_db):
    from app.db import database

    database.create_api_user("u2", "In scope", "hash-u2")
    database.create_session("sess-own", "default", api_user_id="u2")
    in_scope = {"id": "u2", "project_id": None, "profile_ids": ["default"]}
    with patch.object(query_api, "interrupt_session", AsyncMock(return_value=True)):
        for request in (fake_request(None), fake_request(in_scope)):
            result = await query_api.interrupt(request, "sess-own", token="t")
            assert result == {"status": "interrupted", "session_id": "sess-own"}


@pytest.mark.asyncio
async def test_streaming_active_is_filtered_for_api_users(mock_db):
    from app.db import database

    database.create_profile(profile_id="other-profile", name="Other", description=None, config={})
    database.create_api_user("u1", "Foreign", "hash-u1")
    database.create_session("sess-mine", "other-profile", api_user_id="u1")
    database.create_session("sess-theirs", "default", api_user_id="u1")
    database.create_session("sess-owners", "other-profile")
    active = ["sess-mine", "sess-theirs", "sess-owners", "sess-gone"]
    with patch("app.core.query_engine.get_streaming_sessions", return_value=active):
        owner = await query_api.list_active_sessions(fake_request(None), token="t")
        scoped = await query_api.list_active_sessions(fake_request(FOREIGN_USER), token="t")
    assert owner == {"active_sessions": active}
    assert scoped == {"active_sessions": ["sess-mine"]}


# -- POST /sessions/import-jsonl ----------------------------------------------


def test_import_jsonl_rejects_api_keys(client):
    from app.api import auth
    from app.db import database

    database.create_api_user("integration", "Integration", auth.hash_api_key("aih_import"))
    response = client.post(
        "/api/v1/sessions/import-jsonl",
        json={"jsonl_path": "/etc/passwd"},
        headers={"Authorization": "Bearer aih_import"},
    )
    assert response.status_code == 403


@pytest.fixture
def transcript_roots(tmp_path, monkeypatch):
    projects = tmp_path / "claude" / "projects"
    workspace = tmp_path / "workspace"
    (projects / "-workspace-app").mkdir(parents=True)
    workspace.mkdir()
    from app.core import config

    monkeypatch.setattr(config.settings, "claude_projects_dir", projects)
    monkeypatch.setitem(config._runtime_overrides, "workspace_dir", workspace)
    return projects, workspace


@pytest.mark.asyncio
@pytest.mark.parametrize("make_path", [
    lambda projects, outside: str(projects / ".." / ".." / "outside" / "abc.jsonl"),
    lambda projects, outside: str(outside / "abc.jsonl"),
    lambda projects, outside: "/etc/passwd.jsonl",
])
async def test_import_jsonl_rejects_paths_outside_roots(mock_db, transcript_roots, tmp_path, make_path):
    projects, _ = transcript_roots
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "abc.jsonl").write_text("{}\n")
    body = ImportJSONLRequest(jsonl_path=make_path(projects, outside))
    with pytest.raises(HTTPException) as exc:
        await import_jsonl_session(fake_request(), body, token="owner")
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_import_jsonl_rejects_symlink_escaping_root(mock_db, transcript_roots, tmp_path):
    projects, _ = transcript_roots
    secret = tmp_path / "secret.jsonl"
    secret.write_text("{}\n")
    link = projects / "-workspace-app" / "link.jsonl"
    link.symlink_to(secret)
    with pytest.raises(HTTPException) as exc:
        await import_jsonl_session(fake_request(), ImportJSONLRequest(jsonl_path=str(link)), token="owner")
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_import_jsonl_accepts_transcript_inside_projects_dir(mock_db, transcript_roots):
    projects, _ = transcript_roots
    transcript = projects / "-workspace-app" / "11111111-2222-3333-4444-555555555555.jsonl"
    transcript.write_text('{"type": "user", "message": {"role": "user", "content": "hello"}}\n')
    result = await import_jsonl_session(
        fake_request(), ImportJSONLRequest(jsonl_path=str(transcript)), token="owner"
    )
    assert result.status == "success"
    assert result.sdk_session_id == transcript.stem
