"""Fork must inherit the parent's worktree binding and run binding.

Regression for "forking a worktree session forks to main / loads with wrong
model and no effort or permission": the fork endpoint used to create_session
with only profile/project/title, so the fork resolved its cwd to the project's
BASE checkout (mutating the main checkout instead of the parent's worktree) and
the composer showed registry defaults.
"""


import pytest
from psycopg2.extras import Json

from app.db import sessions as sessions_db


PARENT_SID = "sess-fork-parent"
PARENT_SDK = "ses_forkparent01"


def _msg(role, text):
    return {"role": role, "content": [{"kind": "text", "text": text}], "provider_meta": {}}


def _seed_parent():
    from app.db.base import get_cursor, get_db
    from app.db import database

    database.create_project(
        project_id="proj-fork-test", name="fork test", description=None, path="/proj"
    )
    with get_db() as conn:
        cur = get_cursor(conn)
        cur.execute(
            "INSERT INTO git_repositories (id, project_id, default_branch) VALUES (%s,%s,%s)",
            ("repo-1", "proj-fork-test", "main"),
        )
        cur.execute(
            "INSERT INTO worktrees (id, repository_id, session_id, branch_name, "
            "worktree_path, base_branch, status, created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            ("wt-1", "repo-1", None, "wt/fix-thing", "/wt/fix-thing", "main", "active", "2026-01-01T00:00:00Z"),
        )
    sessions_db.create_session(
        PARENT_SID,
        "default",
        title="parent",
        model="prov:provA:claude-x",
        effort="high",
        permission_mode="acceptEdits",
        context_window=100000,
        fast=True,
        provider_id="provA",
        derive_provider_from_profile=False,
        worktree_id="wt-1",
        worktree_branch="wt/fix-thing",
        worktree_path="/wt/fix-thing",
    )
    sessions_db.set_session_branch(PARENT_SID, "wt/fix-thing")
    sessions_db.update_session(PARENT_SID, sdk_session_id=PARENT_SDK)
    nodes = [
        ("n1", None, _msg("user", "hello")),
        ("n2", "n1", _msg("assistant", "hi")),
    ]
    from app.db.base import get_cursor, get_db

    with get_db() as conn:
        cur = get_cursor(conn)
        cur.execute(
            "INSERT INTO fluid_sessions (session_id, head_node_id, created_at) "
            "VALUES (%s,%s,%s)",
            (PARENT_SDK, "n2", 1000.0),
        )
        for i, (nid, pid, m) in enumerate(nodes):
            cur.execute(
                "INSERT INTO fluid_session_nodes "
                "(session_id,node_id,parent_id,ts,message) VALUES (%s,%s,%s,%s,%s)",
                (PARENT_SDK, nid, pid, 1000.0 + i, Json(m)),
            )


@pytest.fixture
def fork_client(mock_db):
    """TestClient over the sessions fork router with the real (shimmed) DB."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.auth import require_auth
    from app.api.sessions_fork import fork_router

    application = FastAPI()
    application.include_router(fork_router)
    application.dependency_overrides[require_auth] = lambda: "t"
    with TestClient(application) as c:
        yield c


def test_fork_inherits_worktree_and_run_binding(fork_client):
    _seed_parent()

    resp = fork_client.post(f"/{PARENT_SID}/fork", json={})
    assert resp.status_code == 200, resp.text
    fork_sid = resp.json()["session_id"]

    row = sessions_db.get_session(fork_sid)
    assert row["parent_session_id"] == PARENT_SID

    # Worktree binding: cwd resolution follows these, not the base checkout.
    assert row["worktree_id"] == "wt-1"
    assert row["worktree_branch"] == "wt/fix-thing"
    assert row["worktree_path"] == "/wt/fix-thing"
    assert row["branch"] == "wt/fix-thing"

    # Run binding: composer restores what the parent ran.
    assert row["model"] == "prov:provA:claude-x"
    assert row["effort"] == "high"
    assert row["permission_mode"] == "acceptEdits"
    assert row["context_window"] == 100000
    assert bool(row["fast"]) is True
    assert row["provider_id"] == "provA"

    # The transcript itself came over (fluid node tree copy).
    from app.core.provider.fluid_transcript import fluid_live_messages

    msgs, _ = fluid_live_messages(row["sdk_session_id"], "/workspace")
    assert [m.get("content") for m in msgs] == ["hello", "hi"]


@pytest.mark.parametrize("preview", [False, True])
def test_no_project_fork_reseeds_its_own_runtime_cwd(fork_client, monkeypatch, tmp_path, preview):
    from app.core import config, query_options, session_orchestration
    from app.core.provider import fluid_transcript
    from app.db import database
    from app.db.base import get_cursor, get_db

    _seed_parent()
    database.create_profile("fork-cwd-agent", "Agent", None, {"model": "sonnet"})
    monkeypatch.setitem(config._runtime_overrides, "workspace_dir", tmp_path / "workspace")
    monkeypatch.setattr(config.settings, "claude_projects_dir", tmp_path / "cli-projects")
    monkeypatch.setattr(query_options, "get_security_instructions", lambda *a, **kw: "")
    principal = {"owner": "install-admin", "kind": "admin", "principal_id": "preview"}
    # The parent was the preview's chat; the fork's record is the real one
    # ``record_creator`` writes for the preview cookie the fork request carries.
    read_principal = session_orchestration._principal
    monkeypatch.setattr(
        session_orchestration, "_principal",
        lambda sid: principal if preview and sid == PARENT_SID else read_principal(sid),
    )
    if preview:
        from app.core import preview_access
        monkeypatch.setattr(preview_access, "grant", lambda request: {"token": "preview-cookie"})
    with get_db() as conn:
        get_cursor(conn).execute(
            "UPDATE sessions SET profile_id='fork-cwd-agent', mode='chat', branch=NULL, worktree_id=NULL, "
            "worktree_branch=NULL, worktree_path=NULL, model='sonnet', provider_id=NULL WHERE id=%s", (PARENT_SID,),
        )
    fluid_transcript._set_cli_session_id(PARENT_SDK, "parent-cli")
    parent_options, _ = query_options.build_options_from_profile(
        database.get_profile("fork-cwd-agent"), session_id=PARENT_SID,
    )
    response = fork_client.post(f"/{PARENT_SID}/fork", json={})
    assert response.status_code == 200, response.text
    fork_id = response.json()["session_id"]
    row = database.get_session(fork_id)
    options, _ = query_options.build_options_from_profile(
        database.get_profile("fork-cwd-agent"), session_id=fork_id,
    )
    # Exercise the runtime producer against the exact persisted fork fields,
    # including the inherited chat lane and the recorded parent session.
    assert row["mode"] == "chat" and row["parent_session_id"] == PARENT_SID
    from app.core.session_modes import CHAT_MODE_TOOL_SURFACE, chat_uploads_dir, owner_chat_dir, runtime_session_mode
    # The owner's fork runs as its parent does: the code tools, in the owner's
    # no-project folder. A preview chat's fork keeps the chat ceiling in the
    # chat lane's shared folder, like its parent.
    assert options.cwd == parent_options.cwd
    if preview:
        assert runtime_session_mode(row) == "chat" and options.cwd == str(chat_uploads_dir())
        # The chat ceiling names every tool it allows; the shell is not one.
        assert "Read" in options.allowed_tools and "Bash" not in options.allowed_tools
        assert "Bash" not in CHAT_MODE_TOOL_SURFACE and "run_python" in options.disallowed_tools
    else:
        assert runtime_session_mode(row) == "code" and options.cwd == str(owner_chat_dir())
        assert "Read" not in (options.allowed_tools or []) and not options.disallowed_tools
    handle = fluid_transcript.get_cli_session_id(row["sdk_session_id"])
    assert handle
    expected = fluid_transcript._cli_project_dir(tmp_path / "cli-projects", options.cwd) / f"{handle}.jsonl"
    assert expected.is_file(), f"fork reseed must be where its runtime resumes: {expected}"
    import json
    entries = [json.loads(line) for line in expected.read_text().splitlines()]
    assert {entry["cwd"] for entry in entries} == {options.cwd}
    assert [entry["message"]["content"] for entry in entries if entry["type"] == "user"] == ["hello"]


def test_fluid_fork_stays_hidden_until_its_history_is_copied(fork_client, monkeypatch):
    """A fork row visible mid-copy can take a turn whose event log then blocks
    the inherited one for good; the row appears only once the copy is done."""
    from app.core.provider import fluid_transcript

    _seed_parent()
    real_fork = fluid_transcript.fork_fluid_session
    rows_during_copy = []

    def paused_copy(*args, **kwargs):
        from app.db.base import get_cursor, get_db

        with get_db() as conn:
            cur = get_cursor(conn)
            cur.execute("SELECT id FROM sessions WHERE parent_session_id=%s", (PARENT_SID,))
            rows_during_copy.extend(r["id"] for r in cur.fetchall())
        return real_fork(*args, **kwargs)

    monkeypatch.setattr(fluid_transcript, "fork_fluid_session", paused_copy)
    response = fork_client.post(f"/{PARENT_SID}/fork", json={})
    assert response.status_code == 200, response.text
    assert rows_during_copy == []
    row = sessions_db.get_session(response.json()["session_id"])
    # Created already bound to its copied transcript.
    assert row["sdk_session_id"] and row["sdk_session_id"] != PARENT_SDK
    assert len(fluid_transcript.fluid_transcript_entries(row["sdk_session_id"])) == 2


def test_voice_fork_is_a_normal_conversation(fork_client):
    from app.db.base import get_cursor, get_db

    _seed_parent()
    with get_db() as conn:
        get_cursor(conn).execute("UPDATE sessions SET mode='voice' WHERE id=%s", (PARENT_SID,))
    response = fork_client.post(f"/{PARENT_SID}/fork", json={})
    assert response.status_code == 200, response.text
    assert sessions_db.get_session(response.json()["session_id"])["mode"] is None


def test_owner_fork_gets_the_owner_tools_but_not_orchestration(fork_client, monkeypatch):
    from app.core import session_orchestration
    from app.core.session_modes import owner_workspace_chat
    from app.db.base import get_cursor, get_db

    _seed_parent()
    monkeypatch.setattr(session_orchestration, "_principal", lambda sid: None)
    with get_db() as conn:
        get_cursor(conn).execute(
            "UPDATE sessions SET mode='chat', project_id=NULL, project_v2_id=NULL WHERE id=%s", (PARENT_SID,),
        )
    response = fork_client.post(f"/{PARENT_SID}/fork", json={})
    assert response.status_code == 200, response.text
    row = sessions_db.get_session(response.json()["session_id"])
    assert owner_workspace_chat(row)
    assert not session_orchestration.is_owner_session(row, principal=None)
    # A worker naming the same parent stays out of the owner's tools, and so
    # does a legacy fork stored without a lane (its folder holds its resume).
    assert not owner_workspace_chat({**row, "is_worker": True})
    assert not owner_workspace_chat({**row, "mode": None})


def test_fork_copies_app_event_log_with_turn_ids(fork_client):
    """Whole-session fork must replay the live session_events, not backfill.

    The V2 chat groups work by turn_id. Reconstructing from the node tree
    stamps every event turn_id='backfill' and collapses every work group
    into one.
    """
    from app.db import session_events as se

    _seed_parent()
    se.append_events(PARENT_SID, [
        {"type": "turn.started", "turn_id": "turn-1", "payload": {"profile_id": "p"}},
        {"type": "message.user", "turn_id": "turn-1", "payload": {"content": "hello"}},
        {"type": "tool.use", "turn_id": "turn-1", "payload": {"name": "Read"}},
        {"type": "tool.result", "turn_id": "turn-1", "payload": {"is_error": False}},
        {"type": "message.assistant", "turn_id": "turn-1", "payload": {"content": "hi"}},
        {"type": "turn.completed", "turn_id": "turn-1", "payload": {}},
        {"type": "turn.started", "turn_id": "turn-2", "payload": {"profile_id": "p"}},
        {"type": "message.user", "turn_id": "turn-2", "payload": {"content": "more"}},
        {"type": "turn.completed", "turn_id": "turn-2", "payload": {}},
    ])

    resp = fork_client.post(f"/{PARENT_SID}/fork", json={})
    assert resp.status_code == 200, resp.text
    fork_sid = resp.json()["session_id"]

    copied = se.get_events(fork_sid)
    assert [e["event_type"] for e in copied] == [
        "turn.started", "message.user", "tool.use", "tool.result",
        "message.assistant", "turn.completed",
        "turn.started", "message.user", "turn.completed",
    ]
    assert [e["turn_id"] for e in copied] == [
        "turn-1", "turn-1", "turn-1", "turn-1", "turn-1", "turn-1",
        "turn-2", "turn-2", "turn-2",
    ]
    assert "backfill" not in {e["turn_id"] for e in copied}
    # Parent log stays put.
    assert se.get_max_seq(PARENT_SID) == 9
    assert se.get_max_seq(fork_sid) == 9
