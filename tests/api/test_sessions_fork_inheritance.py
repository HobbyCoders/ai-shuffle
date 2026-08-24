"""Fork must inherit the parent's worktree binding and run binding.

Regression for "forking a worktree session forks to main / loads with wrong
model and no effort or permission": the fork endpoint used to create_session
with only profile/project/title, so the fork resolved its cwd to the project's
BASE checkout (mutating the main checkout instead of the parent's worktree) and
the composer showed registry defaults.
"""

import json

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
