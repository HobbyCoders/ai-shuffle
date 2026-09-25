"""Session export / import routes against the real app and database.

``app.api.sessions_export`` renders a transcript as Markdown, JSON or JSONL and
imports an exported JSON file (or an on-disk Claude Code JSONL) as a new
session. These cover the renderers' message kinds, the import's validation and
scope rules, and the JSONL import's path confinement, with no mocks of the
database layer.
"""
import json

import pytest

from app.api.auth import hash_api_key
from app.db import database

KEY = "aih_export_import_key"
AUTH = {"Authorization": f"Bearer {KEY}"}


@pytest.fixture
def world(client, tmp_path, monkeypatch):
    from app.api import auth
    from app.core import config

    monkeypatch.setattr(auth.owner_auth, "local_access",
                        lambda request: "authorization" not in request.headers)
    client.headers["Origin"] = "http://testserver"
    projects_dir = tmp_path / "claude-projects"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(config.settings, "claude_projects_dir", projects_dir)
    monkeypatch.setattr(config.settings, "workspace_dir", workspace)
    monkeypatch.setitem(config._runtime_overrides, "workspace_dir", workspace)

    database.create_profile("agent-a", "Agent A", None, {"model": "sonnet"})
    database.create_profile("agent-b", "Agent B", None, {"model": "sonnet"})
    database.create_project("proj-a", "A", None, str(tmp_path / "proj-a"))
    return {"projects_dir": projects_dir, "workspace": workspace, "tmp": tmp_path}


def _key(project_id=None, profile_ids=None):
    database.create_api_user("k1", "Key", hash_api_key(KEY), project_id=project_id,
                             profile_ids=profile_ids)


def _session_with_messages(session_id="s1", **kw):
    database.create_session(session_id, "agent-a", title=kw.pop("title", "My: Chat/Title!"), **kw)
    database.add_session_message(session_id, "user", "hello there")
    database.add_session_message(session_id, "assistant", "hi, how can I help")
    database.add_session_message(session_id, "assistant", "", tool_name="Bash",
                                 tool_input={"command": "ls"})
    database.add_session_message(session_id, "system", "compacted", metadata={"k": 1})
    return session_id


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


class TestExport:
    def test_markdown_renders_each_legacy_message_kind(self, client, world):
        sid = _session_with_messages()
        database.update_session(sid, cost_increment=0.5, turn_increment=1)
        r = client.get(f"/api/v1/sessions/{sid}/export")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/markdown")
        assert 'filename="My-ChatTitle-' in r.headers["content-disposition"]
        body = r.text
        assert body.startswith("# My: Chat/Title!")
        assert "- **Cost:** $0.5000" in body
        assert "- **Turns:** 1" in body
        assert "### Human\n\nhello there" in body
        assert "### Assistant\n\nhi, how can I help" in body
        assert "### Tool: Bash" in body and '"command": "ls"' in body
        assert "*System ():* compacted" in body

    def test_json_carries_session_metadata(self, client, world):
        sid = _session_with_messages(project_id="proj-a")
        r = client.get(f"/api/v1/sessions/{sid}/export", params={"format": "json"})
        assert r.status_code == 200
        data = r.json()
        assert data["session"]["id"] == sid
        assert data["session"]["project_id"] == "proj-a"
        assert data["format_version"] == "1.0"
        kinds = [(m["role"], m.get("type")) for m in data["messages"]]
        assert kinds == [("user", None), ("assistant", "text"), ("assistant", "tool_use"),
                         ("system", None)]

    def test_jsonl_falls_back_to_legacy_rows(self, client, world):
        sid = _session_with_messages()
        r = client.get(f"/api/v1/sessions/{sid}/export", params={"format": "jsonl"})
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/x-ndjson")
        lines = [json.loads(line) for line in r.text.splitlines()]
        assert [line["content"] for line in lines][:2] == ["hello there", "hi, how can I help"]

    def test_jsonl_without_any_history_is_404(self, client, world):
        database.create_session("empty", "agent-a", title="Empty")
        r = client.get("/api/v1/sessions/empty/export", params={"format": "jsonl"})
        assert r.status_code == 404
        assert r.json()["detail"] == "No chat history found for this session"

    def test_untitled_session_gets_a_default_filename(self, client, world):
        database.create_session("s-untitled", "agent-a", title=None)
        r = client.get("/api/v1/sessions/s-untitled/export", params={"format": "json"})
        assert r.status_code == 200
        assert 'filename="untitled-session-' in r.headers["content-disposition"]

    def test_bad_format_is_400_before_the_lookup(self, client, world):
        r = client.get("/api/v1/sessions/missing/export", params={"format": "pdf"})
        assert r.status_code == 400

    def test_missing_session_is_404(self, client, world):
        r = client.get("/api/v1/sessions/missing/export")
        assert r.status_code == 404
        assert r.json()["detail"] == "Session not found: missing"

    def test_key_cannot_export_the_owners_session(self, client, world):
        _key()
        sid = _session_with_messages()
        r = client.get(f"/api/v1/sessions/{sid}/export", headers=AUTH)
        assert r.status_code in (403, 404)
        assert "hello there" not in r.text

    def test_key_exports_its_own_session(self, client, world):
        _key()
        sid = _session_with_messages(api_user_id="k1")
        r = client.get(f"/api/v1/sessions/{sid}/export", headers=AUTH,
                       params={"format": "json"})
        assert r.status_code == 200
        assert r.json()["session"]["id"] == sid


# ---------------------------------------------------------------------------
# Import (JSON export file)
# ---------------------------------------------------------------------------


def _upload(client, payload, name="export.json", headers=None):
    raw = payload if isinstance(payload, (bytes, str)) else json.dumps(payload)
    return client.post("/api/v1/sessions/import", headers=headers or {},
                       files={"file": (name, raw, "application/json")})


def _export(session=None, messages=None):
    return {
        "session": {"title": "Imported chat", "profile_id": "agent-b", **(session or {})},
        "messages": messages if messages is not None else [
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "a1"},
            {"role": "assistant", "type": "tool_use", "content": "", "toolName": "Read",
             "toolInput": {"path": "x"}, "toolResult": "file body"},
            {"role": "assistant", "type": "subagent", "agentType": "Explore",
             "agentDescription": "look around"},
            {"role": "system", "subtype": "init", "content": "boot"},
            {"role": "user", "content": "q2"},
        ],
    }


class TestImport:
    def test_round_trip_creates_session_and_transcript(self, client, world):
        r = _upload(client, _export())
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "success" and body["message_count"] == 6
        row = database.get_session(body["session_id"])
        assert row["profile_id"] == "agent-b"
        assert row["title"] == "Imported chat"
        assert row["turn_count"] == 2
        files = list(world["projects_dir"].rglob("*.jsonl"))
        assert len(files) == 1
        entries = [json.loads(line) for line in files[0].read_text().splitlines()]
        assert [e["type"] for e in entries] == [
            "user", "assistant", "assistant", "user", "assistant", "system", "user"]
        assert entries[3]["message"]["content"][0]["type"] == "tool_result"
        assert entries[4]["message"]["content"][0]["name"] == "Agent"

    def test_json_export_imports_back_in_full(self, client, world):
        """Legacy rows export ``metadata: null``; the import must still write
        every message to the transcript instead of stopping at the first."""
        sid = _session_with_messages()
        exported = client.get(f"/api/v1/sessions/{sid}/export", params={"format": "json"}).json()
        assert any(m.get("metadata") is None for m in exported["messages"])

        r = _upload(client, exported)
        assert r.status_code == 200, r.text
        (jsonl,) = world["projects_dir"].rglob("*.jsonl")
        entries = [json.loads(line) for line in jsonl.read_text().splitlines()]
        assert [e["type"] for e in entries] == ["user", "assistant", "assistant", "system"]
        assert entries[2]["message"]["content"][-1]["name"] == "Bash"

    def test_unknown_agent_falls_back_to_an_existing_one(self, client, world):
        r = _upload(client, _export(session={"profile_id": "gone-agent"}))
        assert r.status_code == 200
        profile_id = database.get_session(r.json()["session_id"])["profile_id"]
        assert profile_id != "gone-agent" and database.get_profile(profile_id)

    def test_unknown_project_is_dropped_not_a_500(self, client, world):
        """An export from another install names a project this one lacks."""
        r = _upload(client, _export(session={"project_id": "elsewhere"}))
        assert r.status_code == 200, r.text
        assert database.get_session(r.json()["session_id"])["project_id"] is None

    def test_known_project_is_kept(self, client, world):
        r = _upload(client, _export(session={"project_id": "proj-a"}))
        assert r.status_code == 200
        assert database.get_session(r.json()["session_id"])["project_id"] == "proj-a"

    @pytest.mark.parametrize("payload,detail", [
        ({"session": {}}, "Invalid export format. Missing 'session' or 'messages' field."),
        ({"session": [], "messages": []},
         "Invalid export format. 'session' must be an object and 'messages' must be an array."),
        ({"session": {}, "messages": ["not an object"]},
         "Invalid export format. Every message must be an object."),
    ])
    def test_malformed_structure_is_400(self, client, world, payload, detail):
        r = _upload(client, payload)
        assert r.status_code == 400
        assert r.json()["detail"] == detail

    @pytest.mark.parametrize("raw", [b"null", b"5", b'"session messages"', b"true"])
    def test_a_top_level_non_object_is_400(self, client, world, raw):
        """R12-2: only an object can carry 'session' and 'messages'."""
        r = _upload(client, raw)
        assert r.status_code == 400, r.text
        assert r.json()["detail"] == "Invalid export format. The file must hold a JSON object."

    @pytest.mark.parametrize("field,value", [
        ("title", 5), ("title", ["a"]), ("profile_id", 7), ("project_id", {"id": "proj-a"}),
    ])
    def test_non_string_identity_fields_are_400_before_any_session(
        self, client, world, field, value,
    ):
        """R12-2: a bad title used to fail the response after the session row
        was saved, leaving an orphan session behind a 500."""
        r = _upload(client, _export(session={field: value}))
        assert r.status_code == 400, r.text
        assert r.json()["detail"] == f"Invalid export format. '{field}' must be a string."
        assert database.get_sessions() == []
        assert not world["projects_dir"].exists()

    @pytest.mark.parametrize("message,detail", [
        ({"role": "user", "content": "hi", "metadata": "not-an-object"}, "'metadata' must be an object"),
        ({"role": "user", "content": "hi", "metadata": ["x"]}, "'metadata' must be an object"),
        ({"role": "user", "content": "hi", "metadata": {"timestamp": 5}},
         "'metadata.timestamp' must be a string"),
        ({"role": ["user"], "content": "hi"}, "'role' must be a string"),
        ({"role": "assistant", "type": "tool_use", "toolName": {"x": 1}}, "'toolName' must be a string"),
        ({"role": "user", "content": {"text": "hi"}}, "'content' must be a string or an array"),
        ({"role": "user", "content": "hi", "id": ["a"]}, "'id' must be a string"),
    ], ids=["meta-str", "meta-list", "timestamp-int", "role-list", "tool-name-obj",
            "content-obj", "id-list"])
    def test_malformed_message_is_400_before_any_session(self, client, world, message, detail):
        """R13-9: a bad message used to fail the transcript writer after the
        session row was saved; the route still answered 200."""
        r = _upload(client, _export(messages=[{"role": "user", "content": "ok"}, message]))
        assert r.status_code == 400, r.text
        assert r.json()["detail"] == f"Invalid export format. Message 2: {detail}."
        assert database.get_sessions() == []
        assert not world["projects_dir"].exists()

    def test_transcript_write_failure_saves_nothing(self, client, world):
        """R13-9: a transcript that cannot be written is a failed import, not
        an empty session behind a "success"."""
        world["projects_dir"].write_text("a file where the folder should be")
        r = _upload(client, _export())
        assert r.status_code == 500, r.text
        assert database.get_sessions() == []

    @pytest.mark.parametrize("field", ["total_cost_usd", "total_tokens_in", "turn_count"])
    def test_non_numeric_stats_are_400(self, client, world, field):
        r = _upload(client, _export(session={field: "lots"}))
        assert r.status_code == 400
        assert r.json()["detail"] == f"Invalid export format. '{field}' must be a number."
        assert not world["projects_dir"].exists()

    def test_non_json_extension_is_400(self, client, world):
        r = _upload(client, _export(), name="export.txt")
        assert r.status_code == 400
        assert r.json()["detail"] == "Invalid file type. Please upload a JSON file."

    def test_invalid_json_is_400(self, client, world):
        r = _upload(client, b"{not json")
        assert r.status_code == 400
        assert r.json()["detail"].startswith("Invalid JSON file:")

    def test_non_utf8_is_400(self, client, world):
        r = _upload(client, b"\xff\xfe\x00")
        assert r.status_code == 400

    def test_oversize_file_is_413(self, client, world, monkeypatch):
        from starlette.datastructures import UploadFile

        async def huge_read(self, size=-1):
            self._n = getattr(self, "_n", 0) + 1
            return b"x" * (1024 * 1024) if self._n <= 51 else b""

        monkeypatch.setattr(UploadFile, "read", huge_read)
        r = _upload(client, b"{}")
        assert r.status_code == 413
        assert r.json()["detail"] == "File too large. Maximum size is 50MB."

    def test_no_agents_configured_is_409(self, client, world, monkeypatch):
        monkeypatch.setattr(database, "get_profile", lambda pid: None)
        monkeypatch.setattr(database, "get_all_profiles", lambda: [])
        r = _upload(client, _export())
        assert r.status_code == 409

    def test_key_import_lands_in_its_project(self, client, world):
        _key(project_id="proj-a", profile_ids=["agent-a"])
        r = _upload(client, _export(session={"profile_id": None}), headers=AUTH)
        assert r.status_code == 200, r.text
        row = database.get_session(r.json()["session_id"])
        assert (row["project_id"], row["profile_id"], row["api_user_id"]) == ("proj-a", "agent-a", "k1")

    def test_key_cannot_import_into_another_project(self, client, world):
        database.create_project("proj-b", "B", None, str(world["tmp"] / "proj-b"))
        _key(project_id="proj-a", profile_ids=["agent-a"])
        r = _upload(client, _export(session={"profile_id": "agent-a", "project_id": "proj-b"}),
                    headers=AUTH)
        assert r.status_code == 403
        assert r.json()["detail"] == "Access denied to this project"


# ---------------------------------------------------------------------------
# Import (Claude Code JSONL on disk) — owner only
# ---------------------------------------------------------------------------


def _jsonl(world, name="0f0e0d0c-aaaa-bbbb-cccc-000000000001.jsonl", lines=None):
    d = world["projects_dir"] / "-work-proj"
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text("".join(json.dumps(x) + "\n" for x in (lines or [
        {"type": "user", "message": {"role": "user", "content": "Refactor the parser"},
         "uuid": "u1", "timestamp": "2025-01-01T00:00:00Z"},
    ])))
    return p


class TestImportJsonl:
    def test_imports_then_reports_already_exists(self, client, world):
        p = _jsonl(world)
        r = client.post("/api/v1/sessions/import-jsonl", json={"jsonl_path": str(p)})
        assert r.status_code == 200, r.text
        first = r.json()
        assert first["status"] == "success"
        assert first["sdk_session_id"] == p.stem
        again = client.post("/api/v1/sessions/import-jsonl", json={"jsonl_path": str(p)}).json()
        assert again["status"] == "already_exists"
        assert again["session_id"] == first["session_id"]

    def test_outside_the_roots_is_400(self, client, world):
        outside = world["tmp"] / "elsewhere.jsonl"
        outside.write_text("{}\n")
        r = client.post("/api/v1/sessions/import-jsonl", json={"jsonl_path": str(outside)})
        assert r.status_code == 400
        assert "must be inside" in r.json()["detail"]

    def test_traversal_is_400(self, client, world):
        _jsonl(world)
        sneaky = str(world["projects_dir"] / ".." / "elsewhere.jsonl")
        r = client.post("/api/v1/sessions/import-jsonl", json={"jsonl_path": sneaky})
        assert r.status_code == 400

    def test_missing_file_is_404(self, client, world):
        p = world["projects_dir"] / "-x" / "nope.jsonl"
        r = client.post("/api/v1/sessions/import-jsonl", json={"jsonl_path": str(p)})
        assert r.status_code == 404

    def test_wrong_suffix_and_agent_logs_are_400(self, client, world):
        txt = _jsonl(world, name="notes.txt")
        r = client.post("/api/v1/sessions/import-jsonl", json={"jsonl_path": str(txt)})
        assert (r.status_code, r.json()["detail"]) == (400, "File must be a JSONL file")
        agent = _jsonl(world, name="agent-123.jsonl")
        r = client.post("/api/v1/sessions/import-jsonl", json={"jsonl_path": str(agent)})
        assert (r.status_code, r.json()["detail"]) == (400, "Cannot import agent files (subagent logs)")

    def test_unknown_project_is_404(self, client, world):
        p = _jsonl(world)
        r = client.post("/api/v1/sessions/import-jsonl",
                        json={"jsonl_path": str(p), "project_id": "ghost"})
        assert r.status_code == 404
        assert r.json()["detail"] == "Project not found: ghost"

    def test_api_key_is_refused(self, client, world):
        _key()
        p = _jsonl(world)
        r = client.post("/api/v1/sessions/import-jsonl", headers=AUTH, json={"jsonl_path": str(p)})
        assert r.status_code == 403
