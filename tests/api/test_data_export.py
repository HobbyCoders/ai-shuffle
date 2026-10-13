"""Export everything (``GET /api/v1/data-export/all``) against the real app (#2158).

The ZIP carries every chat as the single-chat export's private JSONL and
Markdown, each agent's export file and the project list, and is owner only.
It must never carry a stored secret: provider sign-ins and API keys, tokens,
settings, the install key, ``.env`` content or agent environment variables.
"""
import io
import json
import zipfile

import pytest

from app.api.auth import hash_api_key
from app.db import database

KEY = "aih_data_export_key"
AUTH = {"Authorization": f"Bearer {KEY}"}

# One distinct marker per place a secret lives on an install.
SECRETS = {
    "agent_env": "sk-agentenv-0123456789abcdefghijklmnop",
    "agent_extra_args": "sk-extraargs-0123456789abcdefghijklmnop",
    "provider_token": "sk-provider-0123456789abcdefghijklmnop",
    "provider_extra_env": "sk-providerenv-0123456789abcdefghijk",
    "system_setting": "sk-setting-0123456789abcdefghijklmnop",
    "install_key": "SHUF-INSTALL-KEY-SECRET-0123",
    "dotenv": "sk-dotenv-0123456789abcdefghijklmnop",
    "oauth_file": "sk-ant-oat01-oauthfile0123456789abcdef",
    "process_env": "sk-processenv-0123456789abcdefghijklm",
}


@pytest.fixture
def world(client, tmp_path, monkeypatch):
    from app.api import auth
    from app.core import config, license

    monkeypatch.setattr(auth.owner_auth, "local_access",
                        lambda request: "authorization" not in request.headers)
    client.headers["Origin"] = "http://testserver"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(config.settings, "claude_projects_dir", tmp_path / "claude-projects")
    monkeypatch.setattr(config.settings, "workspace_dir", workspace)
    monkeypatch.setitem(config._runtime_overrides, "workspace_dir", workspace)

    # Secrets in every store an install keeps them in.
    database.create_profile("agent-a", "Agent A", "Writes code", {
        "env": {"OPENAI_API_KEY": SECRETS["agent_env"]},
        "extra_args": {"api-key": SECRETS["agent_extra_args"]},
        "system_prompt": {"type": "custom", "content": "Be brief."},
    })
    database.create_model_provider(
        "prov-a", "Gateway", "openai", "https://gateway.example/v1",
        auth_token=f"enc:{SECRETS['provider_token']}",
        extra_env={"GATEWAY_KEY": SECRETS["provider_extra_env"]},
    )
    database.set_system_setting("openai_api_key", SECRETS["system_setting"])
    monkeypatch.setattr(license, "_env_key", SECRETS["install_key"])
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / ".env").write_text(f"OPENAI_API_KEY={SECRETS['dotenv']}\n")
    monkeypatch.setattr(config.settings, "data_dir", data_dir)
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"accessToken": SECRETS["oauth_file"]}}))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRETS["process_env"])

    # An imported id is not validated; its file name must stay inside agents/.
    database.create_profile("../evil id", "Odd", "Odd id", {})
    database.create_project("proj-a", "Project A", "The first one", str(tmp_path / "proj-a"))
    database.create_session("chat-one", "agent-a", project_id="proj-a", title="Fix: the login/bug!")
    database.add_session_message("chat-one", "user", "please fix the login")
    database.add_session_message("chat-one", "assistant", "fixed the login")
    database.add_session_message("chat-one", "assistant", "", tool_name="Bash",
                                 tool_input={"command": "pytest -q"})
    # Two chats sharing a title and an id prefix still get their own folders.
    database.create_session("chat-two-a", "agent-a", title="Same")
    database.add_session_message("chat-two-a", "user", "first copy")
    database.create_session("chat-two-b", "agent-a", title="Same")
    database.add_session_message("chat-two-b", "user", "second copy")
    # A chat with nothing recorded is listed, with the reason, not dropped.
    database.create_session("chat-empty", "agent-a", title="Empty")
    return tmp_path


def _export(client, **kw) -> tuple:
    response = client.get("/api/v1/data-export/all", **kw)
    assert response.status_code == 200, response.text
    archive = zipfile.ZipFile(io.BytesIO(response.content))
    assert archive.testzip() is None
    return response, archive


def _members(archive) -> dict:
    [root] = {name.split("/", 1)[0] for name in archive.namelist()}
    return {name.split("/", 1)[1]: archive.read(name).decode("utf-8") for name in archive.namelist()}, root


def test_zip_holds_every_chat_agent_and_project(client, world):
    response, archive = _export(client)
    assert response.headers["content-type"] == "application/zip"
    assert response.headers["x-transcript-privacy"] == "private-raw"
    assert response.headers["content-disposition"].endswith('-private.zip"')
    members, root = _members(archive)
    assert root.startswith("ai-shuffle-export-")

    manifest = json.loads(members["manifest.json"])
    assert manifest["type"] == "ai-shuffle-export"
    chats = {chat["id"]: chat for chat in manifest["chats"]}
    assert set(chats) == {"chat-one", "chat-two-a", "chat-two-b", "chat-empty"}
    assert manifest["counts"]["chats"] == 4

    one = chats["chat-one"]
    assert one["title"] == "Fix: the login/bug!"
    assert one["project_id"] == "proj-a"
    assert one["files"]["jsonl"].endswith("-Fix-the-loginbug-chat-one/chat-private.jsonl")
    jsonl = [json.loads(line) for line in members[one["files"]["jsonl"]].splitlines()]
    assert [m["content"] for m in jsonl][:2] == ["please fix the login", "fixed the login"]
    markdown = members[one["files"]["markdown"]]
    assert markdown.startswith("# Fix: the login/bug!")
    assert "### Human" in markdown and "please fix the login" in markdown
    assert "### Tool: Bash" in markdown and "pytest -q" in markdown

    folders = {chats[s]["files"]["markdown"].split("/")[1] for s in ("chat-two-a", "chat-two-b")}
    assert len(folders) == 2

    empty = chats["chat-empty"]
    assert "jsonl" not in empty["files"]
    assert empty["problems"] == ["No chat history found for this session"]
    assert members[empty["files"]["markdown"]].startswith("# Empty")

    agent = json.loads(members["agents/agent-a-agent.json"])
    assert agent["type"] == "ai-shuffle-agent"
    assert agent["agent"]["name"] == "Agent A"
    assert agent["agent"]["system_prompt_content"] == "Be brief."
    assert agent["agent"]["env"] is None
    assert {"id": "agent-a", "name": "Agent A", "file": "agents/agent-a-agent.json"} in manifest["agents"]
    assert {"id": "../evil id", "name": "Odd", "file": "agents/evilid-agent.json"} in manifest["agents"]
    assert all(".." not in name for name in archive.namelist())

    projects = json.loads(members["projects.json"])
    assert [(p["id"], p["name"], p["session_count"]) for p in projects] == [("proj-a", "Project A", 1)]
    assert "README.txt" in members


def test_zip_never_includes_a_stored_secret(client, world):
    _response, archive = _export(client)
    blob = "\n".join(archive.read(name).decode("utf-8") for name in archive.namelist())
    leaked = {where: secret for where, secret in SECRETS.items() if secret in blob}
    assert leaked == {}
    # Nothing from the credential stores is bundled as a file either.
    names = " ".join(archive.namelist()).lower()
    for fragment in (".env", "credential", "oauth", "settings", "provider", "license"):
        assert fragment not in names


def test_agent_env_still_exports_through_the_single_agent_export(client, world):
    # The bulk export drops env; the one-agent export keeps its documented shape.
    response = client.get("/api/v1/agents/agent-a/export")
    assert response.status_code == 200
    assert response.json()["agent"]["env"] == {"OPENAI_API_KEY": SECRETS["agent_env"]}


def test_api_users_cannot_export_everything(client, world):
    database.create_api_user("k1", "Key", hash_api_key(KEY))
    response = client.get("/api/v1/data-export/all", headers=AUTH)
    assert response.status_code == 403


def test_no_credentials_is_refused(client, world, monkeypatch):
    from app.api import auth

    monkeypatch.setattr(auth.owner_auth, "local_access", lambda request: False)
    response = client.get("/api/v1/data-export/all")
    assert response.status_code in (401, 403)
