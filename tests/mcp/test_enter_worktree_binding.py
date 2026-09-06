"""EnterWorktree records the branch on the session — round-7 audit.

The tool materialized a checkout and told nobody. ``sessions.branch`` stayed
NULL, so: the sidebar showed the session on no branch; the turn-finalize
resync could not repair it (with no branch it resolves the BASE checkout, whose
branch it deliberately refuses to adopt); and PR reconciliation, which matches
on session_id, pr_number or project_id+branch, could match nothing. One audited
session created a worktree, opened PR #1102 and merged it with every one of
those columns still empty.

The HTTP intake paths (``app/api/sessions_worktree.py``) have always called
``database.set_session_branch``. This is the same write from the tool.
"""

import asyncio
import json
from unittest.mock import patch

import pytest


SESSION = "s-enter"
BRANCH = "fix/voice-transcription-settle"
CHECKOUT = "/checkouts/ai-shuffle/fix-voice-transcription-settle"


def _handlers(session_id=SESSION, project_id="proj-1"):
    import claude_agent_sdk as sdk

    from app.mcp import builtin_tools

    captured = {}
    real_create = sdk.create_sdk_mcp_server

    def _spy(name, version, tools):
        captured["tools"] = tools
        return real_create(name=name, version=version, tools=tools)

    with patch.object(sdk, "create_sdk_mcp_server", side_effect=_spy):
        builtin_tools.create_builtin_tools_server(
            session_id=session_id, project_id=project_id
        )

    return {tool.name: tool for tool in captured["tools"]}


def _body(result):
    return json.loads(result["content"][0]["text"])


@pytest.fixture
def checkout(mock_db, monkeypatch):
    """A session row plus a resolvable v2 checkout."""
    from app.core import checkout_manager
    from app.core import project_resolver
    from app.db import database

    database.create_session(session_id=SESSION, profile_id=None, title="work")
    monkeypatch.setattr(
        project_resolver, "resolve_project_v2", lambda spec: {"id": "pv2-1"}
    )
    monkeypatch.setattr(
        checkout_manager, "ensure_checkout",
        lambda pv2, branch, base_branch=None, create_new_branch=False: CHECKOUT,
    )
    return database


def _enter(**args):
    payload = {"branch_name": BRANCH}
    payload.update(args)
    return _body(asyncio.run(_handlers()["EnterWorktree"].handler(payload)))


def test_entering_a_worktree_binds_the_session_to_the_branch(checkout):
    body = _enter()

    assert body["success"] is True
    assert body["worktree_path"] == CHECKOUT
    assert body["session_branch_recorded"] is True

    row = checkout.get_session(SESSION)
    assert row["branch"] == BRANCH
    assert row["project_v2_id"] == "pv2-1"


def test_a_second_entry_moves_the_session_with_it(checkout):
    _enter()
    _enter(branch_name="feat/second")

    assert checkout.get_session(SESSION)["branch"] == "feat/second"


def test_a_failed_bind_never_costs_the_checkout(checkout, monkeypatch):
    """The path exists on disk either way; bookkeeping must not undo that."""
    from app.db import database

    def _boom(*_a, **_kw):
        raise RuntimeError("db is down")

    monkeypatch.setattr(database, "set_session_branch", _boom)

    body = _enter()

    assert body["success"] is True
    assert body["worktree_path"] == CHECKOUT
    assert body["session_branch_recorded"] is False


def test_the_description_says_it_records_the_branch(mock_db):
    description = _handlers()["EnterWorktree"].description

    assert "records the branch on this session" in description
