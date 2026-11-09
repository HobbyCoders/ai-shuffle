"""@-mention lists come from the worker running the chat (issue 1330)."""
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api import projects, session_files
from app.api.auth import require_auth
from app.network import control


SHARED = "pv2-shared"
LOCAL = "local-copy"
SESSION = "session-b"


def _row(**over):
    return {"id": SESSION, "project_id": None, "project_v2_id": SHARED,
            "owner_device_id": "worker-b", **over}


@pytest.fixture
def api(monkeypatch):
    """The project routes as a viewer that does not run the chat."""
    row = _row()
    monkeypatch.setattr(projects.database, "get_session", lambda sid: row if sid == SESSION else None)
    monkeypatch.setattr(projects, "_check_listing_project_access", lambda request, project_id: None)
    monkeypatch.setattr(projects, "check_session_access_for_api_user", lambda api_user, session: None)
    monkeypatch.setattr(projects, "_is_shared_copy_of_project", lambda session, project_id: project_id == LOCAL)
    monkeypatch.setattr(control, "owner_of", lambda session: session.get("owner_device_id"))
    app = FastAPI()
    app.include_router(projects.router)
    app.dependency_overrides[require_auth] = lambda: "authorized-viewer"
    return row, TestClient(app)


def _files_url(project=SHARED, **query):
    params = "&".join(f"{k}={v}" for k, v in {"session_id": SESSION, **query}.items())
    return f"/api/v1/projects/{project}/files?{params}"


@pytest.mark.parametrize("project", [SHARED, LOCAL])
def test_list_asks_the_running_worker_for_either_project_key(api, monkeypatch, project):
    """The shared id (after a reload) and the local copy (before) agree."""
    _, client = api
    listing = {"files": [{"name": "ONLY_ON_B.md", "type": "file", "path": "ONLY_ON_B.md"}], "path": ""}
    call = AsyncMock(return_value={"status": 200, "result": listing})
    monkeypatch.setattr(control, "call", call)
    resolve = AsyncMock(side_effect=AssertionError("the viewer must not resolve its own checkout"))
    monkeypatch.setattr("app.core.project_resolver.resolve_existing_cwd", resolve)

    response = client.get(_files_url(project, path="src"))

    assert response.status_code == 200, response.text
    assert response.json() == {**listing, "projectId": project}
    target, action, body = call.call_args.args
    assert (target, action, body["action"], body["session_id"]) == ("worker-b", "session-files", "list", SESSION)
    assert body["params"] == {"path": "src", "show_hidden": False}
    resolve.assert_not_called()


def test_chat_history_asks_the_running_worker(api, monkeypatch):
    _, client = api
    item = {"name": "s1", "type": "chat_history", "path": "/owner/s1.jsonl", "sdk_session_id": "s1",
            "title": None, "preview": None, "message_count": 1,
            "modified_at": "2026-11-09T00:00:00", "size_bytes": 10}
    call = AsyncMock(return_value={"status": 200, "result": {"sessions": [item]}})
    monkeypatch.setattr(control, "call", call)

    response = client.get(f"/api/v1/projects/{SHARED}/chat-history?session_id={SESSION}&limit=30&search=a")

    assert response.status_code == 200, response.text
    assert response.json()["sessions"][0]["path"] == "/owner/s1.jsonl"
    assert call.call_args.args[2]["params"] == {"search": "a", "limit": 30}


def test_other_projects_session_is_refused_before_any_forward(api, monkeypatch):
    row, client = api
    row["project_v2_id"] = "pv2-another"
    call = AsyncMock()
    monkeypatch.setattr(control, "call", call)

    response = client.get(_files_url(SHARED))

    assert response.status_code == 404
    call.assert_not_called()


def test_session_access_denied_is_refused_before_any_forward(api, monkeypatch):
    _, client = api
    def deny(api_user, session):
        raise HTTPException(403, "Access denied")
    monkeypatch.setattr(projects, "check_session_access_for_api_user", deny)
    call = AsyncMock()
    monkeypatch.setattr(control, "call", call)

    assert client.get(_files_url(SHARED)).status_code == 403
    call.assert_not_called()


def test_shared_id_project_access_is_checked(monkeypatch):
    calls = []
    monkeypatch.setattr("app.db.projects_v2.get_project_v2", lambda pid: {"id": pid})
    monkeypatch.setattr(projects, "require_origin_access", lambda **kw: calls.append(kw))
    projects._check_listing_project_access("request", SHARED)
    assert calls == [{"request": "request", "project_v2_id": SHARED}]


def test_local_project_access_keeps_its_check(monkeypatch):
    calls = []
    monkeypatch.setattr("app.db.projects_v2.get_project_v2", lambda pid: None)
    monkeypatch.setattr(projects, "check_project_access", lambda request, pid: calls.append(pid))
    projects._check_listing_project_access("request", LOCAL)
    assert calls == [LOCAL]


def test_local_session_is_listed_without_forwarding(api, monkeypatch, tmp_path):
    row, client = api
    row["owner_device_id"] = None
    (tmp_path / "here.md").write_text("x")
    monkeypatch.setattr("app.core.project_resolver.resolve_existing_cwd", lambda session: str(tmp_path))
    call = AsyncMock()
    monkeypatch.setattr(control, "call", call)

    response = client.get(_files_url(SHARED))

    assert response.status_code == 200, response.text
    assert [f["path"] for f in response.json()["files"]] == ["here.md"]
    call.assert_not_called()


@pytest.mark.parametrize("failure,status,detail", [
    (control.RunnerUnreachable("down"), 503, session_files.WORKER_UNREACHABLE),
    (control.ControlRefused("Unknown control action"), 409, session_files.WORKER_OUTDATED),
    (control.ControlRefused("This worker does not own the session"), 409, "This worker does not own the session"),
])
def test_forward_failures_say_why(api, monkeypatch, failure, status, detail):
    _, client = api
    monkeypatch.setattr(control, "call", AsyncMock(side_effect=failure))

    response = client.get(_files_url(SHARED))

    assert (response.status_code, response.json()["detail"]) == (status, detail)


def test_owner_refusal_reaches_the_viewer(api, monkeypatch):
    _, client = api
    monkeypatch.setattr(control, "call", AsyncMock(return_value={"status": 400, "detail": "Path escapes session checkout"}))

    response = client.get(_files_url(SHARED, path="../other"))

    assert (response.status_code, response.json()["detail"]) == (400, "Path escapes session checkout")


# The running worker's side ---------------------------------------------------


@pytest.fixture
def owner(monkeypatch, tmp_path):
    session_dir = tmp_path / "checkout"
    (session_dir / "src").mkdir(parents=True)
    (session_dir / "README.md").write_text("hi")
    (session_dir / "src" / "a.py").write_text("x")
    (session_dir / ".hidden").write_text("x")
    (tmp_path / "secret.txt").write_text("no")
    (session_dir / "escape").symlink_to(tmp_path)
    monkeypatch.setattr(control, "_require_owned", lambda sid: _row(id=sid, owner_device_id=None))
    monkeypatch.setattr("app.core.project_resolver.resolve_existing_cwd", lambda session: str(session_dir))
    return session_dir


async def _ask(action="list", **params):
    return await session_files.handle_session_files(
        {"action": action, "session_id": SESSION, "params": params}, "worker-a")


@pytest.mark.asyncio
async def test_owner_lists_its_own_checkout_with_relative_paths(owner):
    result = await _ask(path="")
    assert result["status"] == 200
    paths = {f["name"]: f["path"] for f in result["result"]["files"]}
    assert paths["README.md"] == "README.md" and paths["src"] == "src"
    assert ".hidden" not in paths

    nested = await _ask(path="src")
    assert [f["path"] for f in nested["result"]["files"]] == [str(Path("src") / "a.py")]


@pytest.mark.parametrize("path", ["..", "../secret.txt", "src/../..", "escape", "/etc"])
@pytest.mark.asyncio
async def test_owner_refuses_paths_that_leave_the_checkout(owner, path):
    result = await _ask(path=path)
    assert result["status"] == 400
    assert result["detail"] == "Path escapes session checkout"


@pytest.mark.asyncio
async def test_owner_says_when_the_checkout_is_missing(owner, monkeypatch):
    monkeypatch.setattr("app.core.project_resolver.resolve_existing_cwd", lambda session: None)
    assert await _ask(path="") == {"status": 409, "detail": session_files.NO_CHECKOUT}


@pytest.mark.asyncio
async def test_owner_refuses_a_session_it_does_not_run(monkeypatch):
    def not_mine(session_id):
        raise PermissionError("This worker does not own the session")
    monkeypatch.setattr(control, "_require_owned", not_mine)
    with pytest.raises(PermissionError):
        await _ask(path="")


@pytest.mark.parametrize("body", [
    {"action": "delete", "session_id": SESSION, "params": {}},
    {"action": "list", "session_id": SESSION, "params": "nope"},
    {"action": "list", "session_id": SESSION, "params": {"path": 7}},
    {"action": "chat-history", "session_id": SESSION, "params": {"limit": 100000}},
])
@pytest.mark.asyncio
async def test_owner_rejects_malformed_requests(owner, body):
    with pytest.raises(ValueError):
        await session_files.handle_session_files(body, "worker-a")


@pytest.mark.asyncio
async def test_owner_chat_history_uses_the_session_directory(owner, monkeypatch):
    seen = {}
    def history(working_dir, search, limit):
        seen.update(working_dir=working_dir, search=search, limit=limit)
        return []
    monkeypatch.setattr("app.core.jsonl_parser.list_chat_history_sessions", history)
    result = await _ask("chat-history", search="x", limit=5)
    assert result == {"status": 200, "result": {"sessions": []}}
    assert seen == {"working_dir": str(owner), "search": "x", "limit": 5}


def test_action_is_registered():
    assert control.known_action("session-files")
