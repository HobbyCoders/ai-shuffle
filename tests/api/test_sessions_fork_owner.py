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
    # Not a "legacy held" row: the catalog scan must not publish this copy to
    # the owner while it is still making the real fork.
    assert row["instance_id"] is None
    from app.db.base import get_cursor, get_db
    from app.network import catalog

    with get_db() as conn:
        held = catalog._owned_session_ids(get_cursor(conn), "dev-mirror-self", recent_only=False,
                                          include_held=True)
    assert data["session_id"] not in held


def test_owner_does_not_acknowledge_a_fork_without_a_transcript(mock_db, monkeypatch):
    """A row under the requested id with no transcript yet (a fork still being
    made, or a copy that reached the owner by sync) is not a finished fork."""
    import asyncio
    from app.api import sessions_fork
    from tests.api.test_sessions_fork_inheritance import PARENT_SID as OWNED_SID, _seed_parent

    _seed_parent()
    sessions_db.create_session("ses-placeholder0001", "default", title="Fork of parent",
                               parent_session_id=OWNED_SID)
    monkeypatch.setattr(sessions_fork, "_FORK_IN_PROGRESS_WAIT", 0.0)
    with pytest.raises(ValueError, match="still being made"):
        asyncio.run(sessions_fork.handle_fork(
            {"session_id": OWNED_SID, "new_session_id": "ses-placeholder0001"}, "dev-mirror"))


def test_owner_builds_the_history_of_a_fork_short_of_the_head(mock_db, monkeypatch):
    """The mirror has no transcript to build a fork's history from, so the
    owner builds the event log before it publishes the fork."""
    import asyncio
    from app.api import sessions_fork
    from app.db import session_events as events_db
    from app.network import catalog, control
    from tests.api.test_sessions_fork_inheritance import PARENT_SID as OWNED_SID, _seed_parent

    _seed_parent()
    seq_at_publish = []
    monkeypatch.setattr(catalog, "publish_owned",
                        lambda **kw: seq_at_publish.append(events_db.get_max_seq(kw["session_ids"][0])) or 0)
    monkeypatch.setattr(control, "stamp_provenance", lambda sid, label: True)

    asyncio.run(sessions_fork.handle_fork(
        {"session_id": OWNED_SID, "new_session_id": "ses-0123456789abcdef", "message_index": 0}, "dev-mirror"))

    assert seq_at_publish and seq_at_publish[0] > 0


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


def test_preview_link_cannot_fork_a_chat_another_worker_runs(fork_client, remote_parent, monkeypatch):
    """The owner cannot see the preview cookie's principal, so its fork would
    run there without the preview's ceiling: refused before anything is made."""
    from app.core import preview_access
    from app.network import control

    async def never(*args, **kwargs):
        raise AssertionError("the owner must not be asked")

    monkeypatch.setattr(control, "call", never)
    monkeypatch.setattr(preview_access, "grant", lambda request: {"token": "preview-cookie"})
    before = {s["id"] for s in sessions_db.get_sessions(limit=100)}

    resp = fork_client.post(f"/{remote_parent}/fork", json={})
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == "The preview link can't fork a chat another worker runs."
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


def test_owner_asked_again_mid_copy_makes_one_fork(mock_db, monkeypatch):
    """The row appears only after the copy, so a repeat that arrives while the
    first request is still copying (its answer was lost) waits for that fork
    instead of copying the history a second time, and both get the fork."""
    import asyncio
    import threading
    from app.api import sessions_fork
    from app.core.provider import fluid_transcript
    from app.network import catalog, control
    from tests.api.test_sessions_fork_inheritance import PARENT_SID as OWNED_SID, _seed_parent

    _seed_parent()
    monkeypatch.setattr(catalog, "publish_owned", lambda **kw: 0)
    monkeypatch.setattr(control, "stamp_provenance", lambda sid, label: True)
    real_fork = fluid_transcript.fork_fluid_session
    copies, release = [], threading.Event()

    def slow_copy(*args, **kwargs):
        copies.append(args)
        release.wait(5)
        return real_fork(*args, **kwargs)

    monkeypatch.setattr(fluid_transcript, "fork_fluid_session", slow_copy)
    body = {"session_id": OWNED_SID, "new_session_id": "ses-0123456789abcdef"}

    async def scenario():
        first = asyncio.ensure_future(sessions_fork.handle_fork(body, "dev-mirror"))
        while not copies:
            await asyncio.sleep(0.01)
        # The first request's connection drops mid-copy; the mirror asks again.
        first.cancel()
        again = asyncio.ensure_future(sessions_fork.handle_fork(body, "dev-mirror"))
        await asyncio.sleep(0.05)
        release.set()
        return await again

    result = asyncio.run(scenario())
    assert result["session_id"] == "ses-0123456789abcdef"
    assert len(copies) == 1
    assert sessions_db.get_session("ses-0123456789abcdef")["sdk_session_id"]
    assert sessions_fork._INFLIGHT_FORKS == {}


def _cli_parent(monkeypatch, tmp_path, parent):
    """A CLI-backed parent with a two-turn JSONL transcript and no app events.
    Returns the list each publish appends the fork's (type, payload) events to."""
    import json
    from app.core import jsonl_parser
    from app.core.config import settings
    from app.db import session_events as events_db
    from app.network import catalog, control

    sessions_db.create_session(parent, "default", title="cli parent")
    sessions_db.update_session(parent, sdk_session_id="sdk-cli-parent")
    rows = [
        ("u1", None, "user", "first question"),
        ("a1", "u1", "assistant", "first answer"),
        ("u2", "a1", "user", "second question"),
        ("a2", "u2", "assistant", "second answer"),
    ]
    source = tmp_path / "parent.jsonl"
    source.write_text("\n".join(json.dumps({
        "type": kind, "uuid": uid, "parentUuid": pid, "sessionId": "sdk-cli-parent",
        "timestamp": f"2026-01-01T00:00:0{i}Z",
        "message": {"role": kind, "content": [{"type": "text", "text": text}]},
    }) for i, (uid, pid, kind, text) in enumerate(rows)) + "\n")
    monkeypatch.setattr(settings, "claude_projects_dir", tmp_path / "projects", raising=False)
    monkeypatch.setattr(jsonl_parser, "get_session_jsonl_path", lambda sdk, wd: source)

    published = []

    def capture(**kw):
        sid = kw["session_ids"][0]
        published.append([(e.get("event_type"), json.dumps(e.get("payload"))) for e in events_db.get_events(sid)])
        return 0

    monkeypatch.setattr(catalog, "publish_owned", capture)
    monkeypatch.setattr(control, "stamp_provenance", lambda sid, label: True)
    return published


def test_owner_seeds_a_cli_forks_history_before_publishing(mock_db, monkeypatch, tmp_path):
    """A CLI-backed chat forked at an earlier message: the store holds nothing
    for the new transcript and heals from its JSONL only once the file has sat
    idle, so the owner seeds the fork's events from the lines it wrote."""
    import asyncio
    from app.api import sessions_fork

    parent = "sess-cli-fork-parent"
    published = _cli_parent(monkeypatch, tmp_path, parent)

    asyncio.run(sessions_fork.handle_fork(
        {"session_id": parent, "new_session_id": "ses-cli0123456789ab", "message_index": 1}, "dev-mirror"))

    assert len(published) == 1
    seeded = " ".join(payload for _type, payload in published[0])
    assert "first question" in seeded and "first answer" in seeded
    assert "second question" not in seeded


def test_owner_seeds_a_whole_cli_fork_whose_log_holds_only_cancellations(mock_db, monkeypatch, tmp_path):
    """The parent's log has a cancelled queued turn and no history yet: the
    whole-chat copy brings only that, so the fork still needs seeding."""
    import asyncio
    from app.api import sessions_fork
    from app.db import session_events as events_db

    parent = "sess-cli-cancelled-parent"
    published = _cli_parent(monkeypatch, tmp_path, parent)
    events_db.append_events(parent, [{"type": "queue.cancelled", "payload": {"cancelled_turn_ids": ["peer"]}}])

    asyncio.run(sessions_fork.handle_fork(
        {"session_id": parent, "new_session_id": "ses-clicancel01234a"}, "dev-mirror"))

    assert len(published) == 1
    assert published[0][0][0] == "queue.cancelled"
    seeded = " ".join(payload for _type, payload in published[0])
    assert "first question" in seeded and "second answer" in seeded


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
