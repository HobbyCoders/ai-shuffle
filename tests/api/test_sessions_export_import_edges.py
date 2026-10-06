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


@pytest.mark.asyncio
@pytest.mark.parametrize("share_safe", [False, True])
async def test_recorded_native_compaction_in_all_exports_and_transcript_pages(client, world, mock_db, monkeypatch, share_safe):
    from psycopg2.extras import Json
    from app.db.base import get_db
    from tests.core.test_native_compact_adapter import _multi_promotion_turn
    from app.core.provider.fluid_transcript import fluid_transcript_entries

    _key()
    sid, adapter, loop, _ = await _multi_promotion_turn(mock_db, monkeypatch, profile_id="agent-a")
    try:
        nodes = await loop.store.load_path(loop.session_id)
        from app.db import session_events
        [record] = [r for r in session_events.get_events(sid)
                    if r["event_type"] == "system.compaction_boundary"]
        # Prove export whitelisting excludes future/legacy summary additions,
        # not just that today's producer happens to omit them.
        record["payload"]["data"]["summary"] = "summary-must-not-appear"
        record["payload"]["data"]["compact_metadata"]["summary"] = "summary-must-not-appear"
        # The display fields a delegated card adds (#1886, #1907) survive.
        record["payload"]["data"]["compact_metadata"]["requested_by"] = "app"
        record["payload"]["data"]["compact_metadata"]["context_post_tokens"] = 70000
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE session_events SET payload = %s WHERE id = %s",
                           (Json(record["payload"]), record["id"]))
            cursor.execute("UPDATE sessions SET api_user_id = %s WHERE id = %s", ("k1", sid))
            cursor.execute("INSERT INTO fluid_sessions (session_id, head_node_id, created_at) VALUES (%s, %s, %s)",
                           (loop.session_id, nodes[-1].node_id, nodes[0].ts))
            for node in nodes:
                cursor.execute("INSERT INTO fluid_session_nodes (session_id, node_id, parent_id, ts, message) VALUES (%s, %s, %s, %s, %s)",
                               (loop.session_id, node.node_id, node.parent_id, node.ts, Json(node.message.to_dict())))
        database.update_session(sid, sdk_session_id=loop.session_id)
        r = client.get(f"/api/v1/sessions/{sid}/export", headers=AUTH,
                       params={"format": "json", "share_safe": share_safe})
        assert r.status_code == 200, r.text
        messages = r.json()["messages"]
        cards = [m for m in messages if m.get("subtype") == "compact_boundary"]
        assert len(cards) == 1, "the real recorder card disappeared or duplicated in JSON export"
        # The card falls between the new user query and its assistant response.
        index = messages.index(cards[0])
        assert messages[index - 1]["content"] == "next question"
        assert messages[index + len(cards)]["role"] == "assistant"
        for card in cards:
            assert set(card["systemData"]) == {"compact_metadata"}
            assert set(card["systemData"]["compact_metadata"]) == {
                "trigger", "pre_tokens", "post_tokens", "context_window",
                "requested_by", "context_post_tokens"}
            assert card["content"] == ""
        raw = client.get(f"/api/v1/sessions/{sid}/export", headers=AUTH,
                         params={"format": "jsonl", "share_safe": share_safe})
        assert raw.status_code == 200
        assert '"compact_boundary"' in raw.text
        md = client.get(f"/api/v1/sessions/{sid}/export", headers=AUTH,
                        params={"format": "markdown", "share_safe": share_safe})
        assert md.status_code == 200
        assert "Context Compacted" in md.text
        assert all("summary-must-not-appear" not in response.text for response in (r, raw, md))
        detail = client.get(f"/api/v1/sessions/{sid}", headers=AUTH)
        assert detail.status_code == 200, detail.text
        [detail_card] = [m for m in detail.json()["messages"] if m.get("subtype") == "compact_boundary"]
        # #1886: the detail response keeps the card's display metadata.
        assert detail_card["systemSubtype"] == "compact_boundary"
        detail_metadata = detail_card["systemData"]["compact_metadata"]
        assert {"trigger", "pre_tokens", "post_tokens", "context_window"} <= set(detail_metadata)
        assert detail_metadata["requested_by"] == "app"
        assert detail_metadata["context_post_tokens"] == 70000
        assert "summary-must-not-appear" not in detail.text
        page = client.get(f"/api/v1/sessions/{sid}/live", headers=AUTH, params={"message_limit": 1})
        assert page.status_code == 200, page.text
        pagination = page.json()["message_pagination"]
        older = client.get(f"/api/v1/sessions/{sid}/live", headers=AUTH,
                           params={"message_limit": 1, "before_seq": pagination["next_before_seq"]})
        assert older.status_code == 200, older.text
        assert any(m.get("systemSubtype") == "compact_boundary" for m in older.json()["messages"])
        # Read-only audit inserts never alter rewind's 1:1 node projection.
        assert len(fluid_transcript_entries(loop.session_id)) == len(nodes)
    finally:
        await adapter.disconnect()


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


# ---------------------------------------------------------------------------
# Separate sharing artifacts — exercise the real producing paths.
# ---------------------------------------------------------------------------

SYNTHETIC_PREVIEW = "v1.c3ludGhldGlj.1790942400." + "b" * 64
SYNTHETIC_PASSWORD = "fixture-url-password"


def _sharing_secret_text():
    encoded = SYNTHETIC_PREVIEW.replace(".", "%252E")
    return ("Useful result " + SYNTHETIC_PREVIEW + " encoded=" + encoded +
            " postgres://user:" + SYNTHETIC_PASSWORD + "@host/db")


def _create_sharing_source(source, world, client):
    import asyncio

    text = _sharing_secret_text()
    nested = {"headers": {"Authorization": "fixture-opaque-auth"},
              "nested": [{"preview": SYNTHETIC_PREVIEW, "api_key": "fixture-api-key"}]}
    if source == "legacy":
        database.create_session("share-legacy", "agent-a", title="Title " + SYNTHETIC_PREVIEW)
        database.add_session_message("share-legacy", "user", text, metadata=nested)
        database.add_session_message("share-legacy", "assistant", "", tool_name="Bash",
                                     tool_input={"command": text, "credentials": nested})
        database.add_session_message("share-legacy", "system", text, metadata=nested)
        return "share-legacy", None

    if source == "fluid":
        from app.core.provider.fluid_pg_store import PgFluidStore
        from fluid.messages.content import Message, Text, ToolUseBlock, ToolResultBlock
        from fluid.store.base import SessionNode

        async def produce():
            store = PgFluidStore()
            await store.create_session("ses_sharefixture")
            await store.append("ses_sharefixture", SessionNode(
                node_id="n1", parent_id=None,
                message=Message(role="assistant", content=[
                    Text(text=text), ToolUseBlock(id="t1", name="Bash",
                                                input={"command": text, "nested": nested}),
                ], provider_meta=nested),
            ))
            await store.append("ses_sharefixture", SessionNode(
                node_id="n2", parent_id="n1",
                message=Message(role="user", content=[
                    ToolResultBlock(tool_use_id="t1", content=[Text(text=text)], is_error=True),
                ], provider_meta=nested),
            ))
        asyncio.run(produce())
        database.create_session("share-fluid", "agent-a", title="Title " + SYNTHETIC_PREVIEW)
        database.update_session("share-fluid", sdk_session_id="ses_sharefixture")
        return "share-fluid", None

    path = _jsonl(world, lines=[
        {"type": "user", "uuid": "u1", "message": {"role": "user", "content": text},
         "metadata": nested},
        {"type": "assistant", "uuid": "a1", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": text, **nested}},
        ]}},
        {"type": "user", "uuid": "u2", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": text, "is_error": True},
        ]}},
    ])
    # Noncanonical spacing and no final newline make byte-fidelity meaningful.
    raw = path.read_text().replace('"type":', '"type" :').rstrip("\n")
    path.write_text(raw)
    response = client.post("/api/v1/sessions/import-jsonl", json={"jsonl_path": str(path)})
    assert response.status_code == 200, response.text
    return response.json()["session_id"], path


@pytest.mark.parametrize("source", ["legacy", "fluid", "raw-cli"])
@pytest.mark.parametrize("format,extension,media", [
    ("markdown", "md", "text/markdown"), ("json", "json", "application/json"),
    ("jsonl", "jsonl", "application/x-ndjson"),
])
def test_separate_share_safe_export_from_real_producers(client, world, source, format, extension, media):
    sid, path = _create_sharing_source(source, world, client)
    url = f"/api/v1/sessions/{sid}/export"
    before_session = database.get_session(sid)
    before_messages = database.get_session_messages(sid)
    before_fluid = None
    if source == "fluid":
        import asyncio
        from app.core.provider.fluid_pg_store import PgFluidStore
        before_fluid = asyncio.run(PgFluidStore().load_path("ses_sharefixture"))
    raw = client.get(url, params={"format": format})
    raw_jsonl = client.get(url, params={"format": "jsonl"})
    assert raw.status_code == raw_jsonl.status_code == 200
    assert SYNTHETIC_PREVIEW in raw_jsonl.text
    if source != "raw-cli" or format == "jsonl":
        assert SYNTHETIC_PREVIEW in raw.text
    # Fresh CLI imports need not have a store-backed Markdown/JSON view yet;
    # sharing renders the strictly parsed file without changing that raw path.
    assert raw.headers["x-transcript-privacy"] == "private-raw"
    if path:
        assert raw_jsonl.content == path.read_bytes()

    safe = client.get(url, params={"format": format, "share_safe": "true"})
    assert safe.status_code == 200, safe.text
    assert safe.headers["content-type"].startswith(media)
    assert safe.headers["x-transcript-privacy"] == "share-safe"
    assert f'-share-safe.{extension}"' in safe.headers["content-disposition"]
    for secret in (SYNTHETIC_PREVIEW, SYNTHETIC_PREVIEW.replace(".", "%252E"),
                   SYNTHETIC_PASSWORD, "fixture-opaque-auth", "fixture-api-key"):
        assert secret not in safe.text and secret not in safe.headers["content-disposition"]
    assert "Useful result" in safe.text and "Bash" in safe.text
    if format == "markdown":
        assert "review before posting" in safe.text
    elif format == "json":
        assert safe.json()["session"]["id"] == sid
    else:
        assert all(isinstance(json.loads(line), dict) for line in safe.text.splitlines())

    private_again = client.get(url, params={"format": format, "share_safe": "false"})
    if format == "json":
        before_data, after_data = raw.json(), private_again.json()
        # Export timestamps intentionally change on every render.
        assert before_data.pop("exported_at") and after_data.pop("exported_at")
        assert after_data == before_data
    elif format == "markdown":
        assert private_again.text.rsplit("*Exported from AI Shuffle on", 1)[0] == raw.text.rsplit("*Exported from AI Shuffle on", 1)[0]
    else:
        assert private_again.content == raw.content
    assert client.get(url, params={"format": "jsonl"}).content == raw_jsonl.content
    assert database.get_session(sid) == before_session
    assert database.get_session_messages(sid) == before_messages
    if source == "fluid":
        # The cold store still carries the fixture tokens after all exports.
        import asyncio
        from app.core.provider.fluid_pg_store import PgFluidStore
        nodes = asyncio.run(PgFluidStore().load_path("ses_sharefixture"))
        assert nodes == before_fluid
        persisted = json.dumps([node.message.to_dict() for node in nodes])
        assert SYNTHETIC_PREVIEW in persisted and "fixture-opaque-auth" in persisted
        assert nodes[1].message.content[0].is_error is True
    if path:
        assert path.read_bytes() == raw_jsonl.content


@pytest.mark.parametrize("format", ["markdown", "json", "jsonl"])
def test_share_safe_sdk_handle_with_legacy_fallback(client, world, format):
    sid = _session_with_messages()
    database.update_session(sid, sdk_session_id="missing-sdk-history")
    response = client.get(f"/api/v1/sessions/{sid}/export",
                          params={"format": format, "share_safe": "true"})
    assert response.status_code == 200, response.text
    assert "hello there" in response.text and "Bash" in response.text


@pytest.mark.parametrize("format", ["markdown", "json", "jsonl"])
@pytest.mark.parametrize("malformed", ['{broken', 'null', '{"secret": NaN}'])
def test_share_safe_raw_cli_fails_closed_without_changing_private_backup(client, world, format, malformed):
    sid, path = _create_sharing_source("raw-cli", world, client)
    raw = path.read_bytes() + ("\n" + malformed + " " + "\n").encode()
    path.write_bytes(raw)
    url = f"/api/v1/sessions/{sid}/export"
    response = client.get(url, params={"format": format, "share_safe": "true"})
    assert response.status_code == 422, response.text
    assert response.json()["detail"].startswith("Cannot create sharing export: malformed JSONL history.")
    assert SYNTHETIC_PREVIEW not in response.text
    assert client.get(url, params={"format": "jsonl"}).content == raw
    assert path.read_bytes() == raw
