"""A restricted API key stays inside its own scope.

Regression tests ported from the security sub-review's probes (sec2/api-user):
each case was reproduced through the real code paths before the fix.

1. A Task worker started from an API key's session runs as that key and only
   on the key's own agents (it used to run as the owner, on any agent).
2. The app-memory and automation builtins do not cross tenants: SessionSearch
   / SessionView see only the key's sessions; GetAppLogs, GetAppErrors,
   CronList, CronCreate and RemoteTrigger refuse an API key's session.
3. ``GET /mcp/servers`` masks secret values for a non-owner.
4. A project-limited key cannot create a projectless session over the chat
   WebSocket.
5. Notes belong to whoever wrote them; a key cannot read or delete the
   owner's.
6. ``POST /sessions/import`` honours the key's project and agent scope.
"""

import json
from unittest.mock import AsyncMock, patch

import pytest

from app.api.auth import hash_api_key
from app.db import database
from app.db import notes as notes_db

KEY = "aih_scope_regression_key"
AUTH = {"Authorization": f"Bearer {KEY}"}


def _scoped_key(project_id=None, profile_ids=("own-profile",)):
    database.create_api_user(
        "u1", "U1", hash_api_key(KEY), project_id=project_id,
        profile_ids=list(profile_ids) if profile_ids else None,
    )
    return database.get_api_user("u1")


# ---------------------------------------------------------------------------
# 1. Task workers run as the key, on the key's agents
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_task_worker_inherits_the_api_key(mock_db, monkeypatch):
    from app.core import provider_agent, turn_manager
    from app.core.session_runtime import SessionRuntime

    database.create_profile("own-profile", "Own", None, {"model": "sonnet"})
    _scoped_key()
    database.create_session("u1-sess", "own-profile", title="mine", api_user_id="u1")

    captured = {}

    async def fake_submit(session_id, source, **kw):
        captured.update(kw)
        raise RuntimeError("stop after the submit is captured")

    monkeypatch.setattr(turn_manager, "submit_async", fake_submit)
    await provider_agent.dispatch_worker(
        parent_session_id="u1-sess", profile_id="own-profile", prompt="hi",
        worker_session_id="worker-1", first_frame_seconds=1, max_wall_seconds=2,
    )

    row = database.get_session("worker-1")
    assert row["api_user_id"] == "u1"
    assert (captured.get("payload") or {}).get("api_user_id") == "u1"
    runtime = SessionRuntime({
        "id": "t1", "session_id": "worker-1", "payload": captured.get("payload") or {},
    })
    assert runtime.api_user_id == "u1"


@pytest.mark.asyncio
async def test_task_worker_refuses_an_agent_outside_the_key(mock_db, monkeypatch):
    from app.core import provider_agent, turn_manager

    database.create_profile("own-profile", "Own", None, {"model": "sonnet"})
    database.create_profile("owner-admin-agent", "Owner Admin", None, {"model": "sonnet"})
    _scoped_key()
    database.create_session("u1-sess", "own-profile", title="mine", api_user_id="u1")
    submit = AsyncMock()
    monkeypatch.setattr(turn_manager, "submit_async", submit)

    out = await provider_agent.dispatch_worker(
        parent_session_id="u1-sess", profile_id="owner-admin-agent", prompt="hi",
        worker_session_id="worker-x", first_frame_seconds=1, max_wall_seconds=2,
    )

    assert out.ok is False and "owner-admin-agent" in (out.error or "")
    assert database.get_session("worker-x") is None
    submit.assert_not_called()


@pytest.mark.asyncio
async def test_task_tool_refuses_an_agent_outside_the_key(mock_db, monkeypatch):
    """The Task tool (and the voice ``dispatch_agent``, which calls it) answers
    before a worker is launched."""
    from app.core import agent_provider_models as apm
    from app.core import task_dispatch as td

    database.create_profile("own-profile", "Own", None, {"model": "sonnet"})
    database.create_profile("owner-admin-agent", "Owner Admin", None, {"model": "sonnet"})
    _scoped_key()
    database.create_session("u1-sess", "own-profile", title="mine", api_user_id="u1")
    monkeypatch.setattr(td, "_credential_gap", lambda provider_id: None)
    worker = AsyncMock()
    monkeypatch.setattr("app.core.provider_agent.dispatch_worker", worker)

    payload = await td.dispatch_task_tool(
        {"agent": "owner-admin-agent", "prompt": "go"},
        caller_session_id="u1-sess",
        caller_provider_id=None,
        roster=(apm.SubagentTarget("owner-admin-agent", "Owner Admin", "", "sonnet"),),
    )

    assert payload["ok"] is False and "not allowed" in payload["error"]
    worker.assert_not_called()


# ---------------------------------------------------------------------------
# 2. Builtins do not cross tenants
# ---------------------------------------------------------------------------


def _builtins(session_id, *, is_admin=False):
    import app.mcp.inproc as sdk
    from app.mcp import builtin_tools

    captured = {}
    real = sdk.create_tool_server

    def spy(name, version, tools):
        captured["tools"] = tools
        return real(name=name, version=version, tools=tools)

    with patch.object(sdk, "create_tool_server", side_effect=spy):
        builtin_tools.create_builtin_tools_server(
            session_id=session_id, is_admin=is_admin, cwd="/tmp",
        )
    return {t.name: t for t in captured["tools"]}


def _seed_transcripts(mock_db):
    database.create_profile("own-profile", "Own", None, {})
    _scoped_key()
    database.create_session("owner-sess", "own-profile", title="Owner private chat")
    database.create_session("u1-sess", "own-profile", title="mine", api_user_id="u1")
    database.create_session("u1-old", "own-profile", title="my older chat", api_user_id="u1")
    for sid, sdk_id, text in (
        ("owner-sess", "sdk-owner", "my AWS key is AKIA-OWNER-SECRET"),
        ("u1-old", "sdk-u1-old", "my AWS key is AKIA-U1-OWN"),
    ):
        mock_db.execute("UPDATE sessions SET sdk_session_id=? WHERE id=?", (sdk_id, sid))
        mock_db.execute(
            "INSERT INTO session_store_entries (project_key, session_id, subpath, entry, created_at) "
            "VALUES (?,?,?,?,?)",
            ("p", sdk_id, None,
             json.dumps({"type": "user", "message": {"content": text}}),
             "2026-09-01 10:00:00"),
        )
    mock_db.commit()


@pytest.mark.asyncio
async def test_session_recall_is_scoped_to_the_api_key(mock_db, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(type(settings), "managed_database_available", lambda self: True)
    _seed_transcripts(mock_db)

    tools = _builtins("u1-sess")
    search = json.dumps(await tools["SessionSearch"].handler({"query": "AWS key"}))
    listing = json.dumps(await tools["SessionSearch"].handler({}))
    view = json.dumps(await tools["SessionView"].handler({"session_id": "sdk-owner"}))
    own_view = json.dumps(await tools["SessionView"].handler({"session_id": "sdk-u1-old"}))

    assert "AKIA-OWNER-SECRET" not in search and "AKIA-U1-OWN" in search
    assert "Owner private chat" not in listing
    assert "AKIA-OWNER-SECRET" not in view
    assert "AKIA-U1-OWN" in own_view


@pytest.mark.asyncio
async def test_owner_session_recall_is_unchanged(mock_db, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(type(settings), "managed_database_available", lambda self: True)
    _seed_transcripts(mock_db)
    database.create_session("owner-2", "own-profile", title="owner second")

    tools = _builtins("owner-2", is_admin=True)
    search = json.dumps(await tools["SessionSearch"].handler({"query": "AWS key"}))
    view = json.dumps(await tools["SessionView"].handler({"session_id": "sdk-u1-old"}))

    assert "AKIA-OWNER-SECRET" in search and "AKIA-U1-OWN" in search
    assert "AKIA-U1-OWN" in view


@pytest.mark.asyncio
async def test_owner_level_builtins_refuse_an_api_key_session(mock_db, monkeypatch):
    import logging

    import app.api.logs  # noqa: F401 — installs the ring handler
    from app.core.automation_manager import automation_manager
    from app.db import automations as auto_db

    database.create_profile("own-profile", "Own", None, {})
    auto_db.create_automation(
        automation_id="owner-auto", name="Owner nightly deploy", trigger_type="cron",
        trigger_config={"expression": "0 3 * * *"}, profile_id="own-profile",
        prompt="OWNER PROMPT", enabled=True,
    )
    logging.getLogger("app.core.query_engine").warning("owner prompt: OWNER-LOG-SECRET")
    fired = []

    async def fake_execute(aid, trigger_data):
        fired.append(aid)

    monkeypatch.setattr(automation_manager, "_execute", fake_execute)

    tools = _builtins("u1-sess", is_admin=False)
    logs = json.dumps(await tools["GetAppLogs"].handler({"search": "OWNER-LOG-SECRET"}))
    errors = await tools["GetAppErrors"].handler({})
    listing = json.dumps(await tools["CronList"].handler({}))
    created = await tools["CronCreate"].handler(
        {"name": "x", "expression": "* * * * *", "prompt": "p"})
    trigger = await tools["RemoteTrigger"].handler({"automation_id": "owner-auto"})

    assert "OWNER-LOG-SECRET" not in logs
    assert "Owner nightly deploy" not in listing
    for result in (errors, created, trigger):
        assert result.get("isError") is True
    assert fired == []
    assert len(auto_db.get_all_automations()) == 1


# ---------------------------------------------------------------------------
# 3. MCP server secrets
# ---------------------------------------------------------------------------


def _mcp_servers(tmp_path, monkeypatch):
    from app.core import mcp_service
    from app.core.mcp_service import McpScope, McpServerConfig, McpServerType

    monkeypatch.setattr(mcp_service, "_get_claude_dir", lambda: tmp_path / ".claude")
    monkeypatch.setattr(mcp_service, "_get_user_claude_json", lambda: tmp_path / ".claude.json")
    monkeypatch.setattr(
        "app.api.mcp.get_mcp_service", lambda *a, **k: mcp_service.McpService(*a, **k))
    svc = mcp_service.McpService()
    svc.add_server("github", McpServerConfig(
        name="github", type=McpServerType.STDIO, command="npx", args=["gh-mcp"],
        env={"GITHUB_TOKEN": "ghp_OWNER_SECRET"}, scope="user"), McpScope.USER)
    svc.add_server("remote", McpServerConfig(
        name="remote", type=McpServerType.HTTP, url="https://bob:URL_SECRET@mcp.example/x",
        headers={"Authorization": "Bearer OWNER_HTTP_SECRET"}, scope="user"), McpScope.USER)


def test_mcp_server_secrets_are_masked_for_an_api_key(client, mock_db, tmp_path, monkeypatch):
    _mcp_servers(tmp_path, monkeypatch)
    database.create_api_user("u1", "U1", hash_api_key(KEY), project_id=None, profile_ids=None)

    r = client.get("/api/v1/mcp/servers", headers=AUTH)

    assert r.status_code == 200
    for secret in ("ghp_OWNER_SECRET", "OWNER_HTTP_SECRET", "URL_SECRET"):
        assert secret not in r.text
    by_name = {s["name"]: s for s in r.json()}
    assert set(by_name["github"]["env"]) == {"GITHUB_TOKEN"}
    assert set(by_name["remote"]["headers"]) == {"Authorization"}


def test_mcp_server_secrets_stay_visible_to_the_owner(client, mock_db, tmp_path, monkeypatch):
    from app.api import auth

    _mcp_servers(tmp_path, monkeypatch)
    monkeypatch.setattr(auth.owner_auth, "local_access", lambda request: True)

    r = client.get("/api/v1/mcp/servers")

    assert r.status_code == 200
    assert "ghp_OWNER_SECRET" in r.text and "OWNER_HTTP_SECRET" in r.text


# ---------------------------------------------------------------------------
# 4. No projectless session for a project-limited key (chat WebSocket)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_project_scoped_key_cannot_create_a_projectless_ws_session(mock_db):
    import app.api.chat_ws_handler as cwh
    from tests.api.test_chat_ws_v2_turns import _Patches, _make_handler
    from tests.helpers import sent_frames

    database.create_project("own-project", "Own", None, "own-project")
    database.create_api_user("api-1", "Key", "hash-api-1", project_id="own-project")
    handler = _make_handler()
    handler.api_user_id = "api-1"
    handler._authorize_mobile_session = AsyncMock(return_value=True)
    user = database.get_api_user("api-1")

    with _Patches(), patch.object(cwh.database, "get_api_user", return_value=user):
        await handler.handle_query({"prompt": "list ../", "profile": "default"})

    created = sent_frames(handler, "session_created")
    assert created, sent_frames(handler, "error")
    row = database.get_session(created[0]["session_id"])
    assert row["project_id"] == "own-project"
    assert row["api_user_id"] == "api-1"


def test_owner_projectless_session_is_unchanged(mock_db):
    database.create_profile("own-profile", "Own", None, {})
    row = database.create_session("owner-free", "own-profile", title="free")
    assert row["project_id"] is None


# ---------------------------------------------------------------------------
# 5. Notes ownership
# ---------------------------------------------------------------------------


def test_api_key_cannot_read_or_delete_owner_notes(client, mock_db):
    notes_db.create_note("n1", "Owner private plan", "OWNER-NOTE-SECRET")
    database.create_profile("own-profile", "Own", None, {})
    database.create_project("own-project", "Own", None, "own-project")
    _scoped_key(project_id="own-project")

    assert client.get("/api/v1/notes/n1", headers=AUTH).status_code == 404
    assert client.patch("/api/v1/notes/n1", headers=AUTH, json={"content": "x"}).status_code == 404
    assert client.delete("/api/v1/notes/n1", headers=AUTH).status_code == 404
    assert client.get("/api/v1/notes/n1/attachments", headers=AUTH).status_code == 404
    listing = client.get("/api/v1/notes", headers=AUTH)
    assert listing.status_code == 200 and "OWNER-NOTE-SECRET" not in listing.text
    auto = client.get("/api/v1/notes/autocomplete", headers=AUTH)
    assert auto.status_code == 200 and auto.json() == []
    assert notes_db.get_note("n1")["content"] == "OWNER-NOTE-SECRET"


def test_api_key_notes_are_its_own_and_visible_to_the_owner(client, mock_db, monkeypatch):
    from app.api import auth

    database.create_profile("own-profile", "Own", None, {})
    _scoped_key()

    created = client.post("/api/v1/notes", headers=AUTH, json={"title": "Key note", "content": "k"})
    assert created.status_code == 201
    note_id = created.json()["id"]
    assert notes_db.get_note(note_id)["api_user_id"] == "u1"
    assert client.get(f"/api/v1/notes/{note_id}", headers=AUTH).status_code == 200
    assert [n["id"] for n in client.get("/api/v1/notes", headers=AUTH).json()] == [note_id]
    # The owner sees every note, its own and the key's.
    notes_db.create_note("n-owner", "Owner", "o")
    monkeypatch.setattr(auth.owner_auth, "local_access", lambda request: True)
    owner_ids = {n["id"] for n in client.get("/api/v1/notes").json()}
    assert owner_ids == {note_id, "n-owner"}


# ---------------------------------------------------------------------------
# 6. Session import honours the key's scope
# ---------------------------------------------------------------------------


def _import(client, session, headers):
    body = json.dumps({"session": session, "messages": [{"role": "user", "content": "hello"}]})
    return client.post(
        "/api/v1/sessions/import", headers=headers,
        files={"file": ("s.json", body.encode(), "application/json")},
    )


@pytest.fixture
def import_world(mock_db, tmp_path, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(
        type(settings), "get_claude_projects_dir",
        property(lambda self: tmp_path / "claude-projects"), raising=False,
    )
    database.create_profile("own-profile", "Own", None, {})
    database.create_profile("secret-profile", "Secret", None, {})
    database.create_project("own-project", "Own", None, "own-project")
    database.create_project("victim-project", "Victim", None, "victim-project")
    _scoped_key(project_id="own-project")


def test_import_refuses_a_foreign_agent_or_project(client, import_world):
    foreign_agent = _import(
        client, {"project_id": "own-project", "profile_id": "secret-profile"}, AUTH)
    foreign_project = _import(
        client, {"project_id": "victim-project", "profile_id": "own-profile"}, AUTH)

    assert foreign_agent.status_code == 403
    assert foreign_project.status_code == 403


def test_import_files_the_session_inside_the_key_scope(client, import_world):
    r = _import(client, {"title": "mine"}, AUTH)

    assert r.status_code == 200, r.text
    row = database.get_session(r.json()["session_id"])
    assert (row["project_id"], row["profile_id"], row["api_user_id"]) == (
        "own-project", "own-profile", "u1")


# ---------------------------------------------------------------------------
# 7. Assignments that outlive their agent (#1870)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path, body", [
    ("/api/v1/sessions", {}),
    ("/api/v1/conversation", {"prompt": "hi"}),
    ("/api/v1/conversation/start", {"prompt": "hi"}),
    ("/api/v1/query", {"prompt": "hi"}),
])
def test_key_whose_only_agent_was_deleted_gets_a_clear_refusal(client, path, body):
    """No agent named: the key's only assigned agent is gone, so it has no
    usable agent. That is a 409 that says so, never a server error."""
    database.create_profile("gone-agent", "Gone", None, {"model": "sonnet"})
    database.create_profile("someone-else", "Other", None, {"model": "sonnet"})
    _scoped_key(profile_ids=("gone-agent",))
    database.delete_profile("gone-agent")

    response = client.post(path, json=body, headers=AUTH)

    assert response.status_code == 409, response.text
    assert "no usable agent" in response.json()["detail"]
    assert "gone-agent" in response.json()["detail"]
    assert database.get_sessions(limit=10) == []


def test_recreated_agent_id_does_not_inherit_the_deleted_agents_key_grants(mock_db):
    """Assignments outlive a deleted agent so a key never silently widens. A
    NEW agent that later takes the same id must not pick them up: a key with
    other agents loses only that grant, and a key restricted to that id alone
    is switched off rather than left unrestricted."""
    from app.core.api_user_scope import worker_refusal

    database.create_profile("helper", "Helper", None, {})
    database.create_profile("other", "Other", None, {})
    database.create_api_user("only-helper", "Only", "only-hash", profile_ids=["helper"])
    database.create_api_user("helper-and-other", "Mixed", "mixed-hash", profile_ids=["helper", "other"])
    database.create_api_user("open-key", "Open", "open-hash")
    database.delete_profile("helper")
    assert database.get_api_user("only-helper")["profile_ids"] == ["helper"]

    database.create_profile("helper", "A different helper", None, {})

    only = database.get_api_user("only-helper")
    assert not only["is_active"]
    assert database.get_api_user_by_key_hash("only-hash") is None
    assert worker_refusal("only-helper", "helper") is not None
    assert worker_refusal("only-helper", "other") is not None
    mixed = database.get_api_user("helper-and-other")
    assert mixed["is_active"] and mixed["profile_ids"] == ["other"]
    assert worker_refusal("helper-and-other", "helper") is not None
    assert worker_refusal("open-key", "helper") is None
    # Seeding an id that already exists changes nothing.
    database.create_profile("other", "Other", None, {}, if_absent=True)
    assert database.get_api_user("helper-and-other")["profile_ids"] == ["other"]


@pytest.mark.parametrize("profile_ids", [("gone-agent", "someone-else"), None])
def test_conversation_naming_a_deleted_agent_is_a_clear_400(client, profile_ids):
    """#1875 review: naming a deleted agent explicitly on /conversation was a
    500 (the new session row's agent foreign key). It now gets the same 400
    as /sessions, before any session row exists."""
    database.create_profile("gone-agent", "Gone", None, {"model": "sonnet"})
    database.create_profile("someone-else", "Other", None, {"model": "sonnet"})
    _scoped_key(profile_ids=profile_ids)
    database.delete_profile("gone-agent")

    response = client.post("/api/v1/conversation", json={"prompt": "hi", "profile": "gone-agent"}, headers=AUTH)

    assert response.status_code == 400, response.text
    assert "no longer exists" in response.json()["detail"]
    assert database.get_sessions(limit=10) == []
