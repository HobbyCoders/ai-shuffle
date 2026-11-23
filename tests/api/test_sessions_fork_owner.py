"""Forking a chat another worker runs happens on that worker.

Regression: the sessions Fork action on a mirror (a chat whose owner is
another worker in the network) answered 400. The mirror has no transcript of
its own (no fluid node tree, no CLI JSONL, often no sdk_session_id), so the
local copy could never work. The fork now runs on the owner, beside the
transcript and the parent's worktree, under an id the mirror chose, and the
mirror keeps a stamped copy so the UI can open it at once.
"""

import pytest

from app.db import sessions as sessions_db


PARENT_SID = "sess-fork-remote-parent"
OWNER = "dev-owner-0001"


@pytest.fixture
def fork_client(mock_db):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.auth import require_auth
    from app.api.sessions_fork import fork_router

    application = FastAPI()
    application.include_router(fork_router)
    application.dependency_overrides[require_auth] = lambda: "t"
    with TestClient(application) as c:
        yield c


@pytest.fixture
def remote_parent(monkeypatch):
    """A mirror row: owned by OWNER, no sdk_session_id here."""
    from app.network import control, runners

    sessions_db.create_session(PARENT_SID, "default", title="parent", model="sonnet", effort="high")
    monkeypatch.setattr(control, "owner_of",
                        lambda row: OWNER if (row or {}).get("owner_device_id") == OWNER or
                        (row or {}).get("id") == PARENT_SID else None)
    monkeypatch.setattr(runners, "resolve_runner_id", lambda target: target)
    monkeypatch.setattr(runners, "find", lambda device: {"device_id": device} if device == OWNER else None)
    return PARENT_SID


def test_fork_of_a_mirror_runs_on_its_owner(fork_client, remote_parent, monkeypatch):
    from app.network import control

    calls = []

    async def fake_call(device_id, action, body=None, **kwargs):
        calls.append((device_id, action, body))
        return {"session_id": body["new_session_id"], "title": "Fork of parent",
                "parent_session_id": body["session_id"], "fork_point_message_index": 3,
                "message_count": 4, "status": "success"}

    monkeypatch.setattr(control, "call", fake_call)

    resp = fork_client.post(f"/{remote_parent}/fork", json={"message_index": 3})
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert len(calls) == 1
    device, action, body = calls[0]
    assert (device, action) == (OWNER, "session-fork")
    assert body["session_id"] == remote_parent
    assert body["message_index"] == 3
    assert body["new_session_id"] == data["session_id"]

    # The mirror holds a copy stamped with the owner, so the UI can open it now
    # and its turns go to the owner.
    row = sessions_db.get_session(data["session_id"])
    assert row is not None
    assert row["parent_session_id"] == remote_parent
    assert row["owner_device_id"] == OWNER
    assert row["model"] == "sonnet"
    assert data["fork_point_message_index"] == 3


def test_unreachable_owner_leaves_no_copy(fork_client, remote_parent, monkeypatch):
    from app.network import control

    async def offline(device_id, action, body=None, **kwargs):
        raise control.RunnerUnreachable("The owning worker is offline")

    monkeypatch.setattr(control, "call", offline)
    before = {s["id"] for s in sessions_db.get_sessions(limit=100)}

    resp = fork_client.post(f"/{remote_parent}/fork", json={})
    assert resp.status_code == 503, resp.text
    assert "offline" in resp.json()["detail"]
    assert {s["id"] for s in sessions_db.get_sessions(limit=100)} == before


def test_owner_forks_its_chat_under_the_mirrors_id(mock_db, monkeypatch):
    """The owner side: a real fork (node tree copied) under the requested id,
    idempotent when the mirror asks again after a lost answer."""
    import asyncio
    from app.api import sessions_fork
    from app.network import catalog, control
    from tests.api.test_sessions_fork_inheritance import PARENT_SID as OWNED_SID, _seed_parent

    _seed_parent()
    published = []
    monkeypatch.setattr(catalog, "publish_owned", lambda **kw: published.append(kw) or 0)
    labels = []
    monkeypatch.setattr(control, "stamp_provenance", lambda sid, label: labels.append((sid, label)) or True)

    body = {"session_id": OWNED_SID, "new_session_id": "ses-0123456789abcdef", "message_index": None,
            "api_user_id": "unknown-key", "provenance_owner": "account:acct-1"}
    result = asyncio.run(sessions_fork.handle_fork(body, "dev-mirror"))

    assert result["session_id"] == "ses-0123456789abcdef"
    assert result["fork_point_message_index"] == 1
    row = sessions_db.get_session("ses-0123456789abcdef")
    assert row["parent_session_id"] == OWNED_SID
    assert row["worktree_id"] == "wt-1" and row["branch"] == "wt/fix-thing"
    # A key this worker does not know is dropped: the fork is the owner's.
    assert row["api_user_id"] is None
    assert labels == [("ses-0123456789abcdef", "account:acct-1")]
    assert published == [{"session_ids": ["ses-0123456789abcdef"]}]
    from app.core.provider.fluid_transcript import fluid_live_messages
    msgs, _ = fluid_live_messages(row["sdk_session_id"], "/workspace")
    assert [m.get("content") for m in msgs] == ["hello", "hi"]

    # Asked again (the first answer was lost): the same fork, not a second one.
    count = len(sessions_db.get_sessions(limit=100))
    again = asyncio.run(sessions_fork.handle_fork(body, "dev-mirror"))
    assert again["session_id"] == "ses-0123456789abcdef"
    assert len(sessions_db.get_sessions(limit=100)) == count


def test_owner_refuses_a_chat_it_only_mirrors(mock_db, remote_parent):
    import asyncio
    from app.api import sessions_fork

    with pytest.raises(PermissionError):
        asyncio.run(sessions_fork.handle_fork(
            {"session_id": remote_parent, "new_session_id": "ses-0123456789abcdef"}, "dev-other"))
    with pytest.raises(ValueError):
        asyncio.run(sessions_fork.handle_fork(
            {"session_id": remote_parent, "new_session_id": "../bad id"}, "dev-other"))


def test_session_fork_is_a_known_control_action():
    from app.network import control

    assert control.known_action("session-fork")


def test_owner_refusal_leaves_no_copy(fork_client, remote_parent, monkeypatch):
    from app.network import control

    async def refused(device_id, action, body=None, **kwargs):
        raise control.ControlRefused("Cannot fork an empty session")

    monkeypatch.setattr(control, "call", refused)
    before = {s["id"] for s in sessions_db.get_sessions(limit=100)}

    resp = fork_client.post(f"/{remote_parent}/fork", json={})
    assert resp.status_code == 409, resp.text
    assert {s["id"] for s in sessions_db.get_sessions(limit=100)} == before
