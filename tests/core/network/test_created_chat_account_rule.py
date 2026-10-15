"""A chat created outside the chat socket keeps the account rule (#1877).

The chat socket records who started a chat it creates (``bind_session(...,
new=True)``). Chats created over REST (``POST /sessions``, with a worktree, a
fork, ``fork-here``) and chats another chat starts (a Task worker here or on
another worker) had no record and no label, so once transcripts followed the
image rule their creator was refused them. Now a paired creator is recorded,
and a child takes its parent's label: the creator keeps images, transcript and
socket on the worker that runs the chat and on a mirror, and other account
members are refused.

Every check runs the real creation code on in-process workers and publishes
through the real catalog path.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.db import database, mobile_tool_identity
from app.network import engine
from tests.core.network.harness import converge
from tests.core.network.test_mirrored_chat_image_access import (  # noqa: F401 - fixture
    CHATS, DIRECT_CHAT, PHONES, _agree, _direct_request, _network, _publish, _start_chats,
    _transcript_matrix, callers,
)

#: The chat each creator may already read (a fork's or a child's source).
SOURCE = {"member-phone": "member-chat", "direct-phone": "direct-chat"}
CREATORS = pytest.mark.parametrize("creator", ["member-phone", "direct-phone"])


def _creator_request(who):
    """A REST request from a paired controller, as ``require_auth`` leaves it."""
    # Authentication requires a persisted slot, not just a synthetic request
    # label. Creation/fork recheck this credential inside their transaction.
    with mobile_tool_identity.get_db() as conn:
        conn.cursor().execute("""INSERT INTO paired_clients
            (id, device_id, device_name, platform, session_token_hash, session_expires_at)
            VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT(id) DO NOTHING""",
            (who, who, who, 'browser', who + '-session-hash',
             (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()))
    return SimpleNamespace(state=SimpleNamespace(
        api_user=None, is_admin=False, paired_client={"id": who, "account_owner_admin": False},
        paired_credential_hash=who + "-session-hash"))


def _only_the_creator(worker_list, callers, session_id, creator):  # noqa: F811
    """The creator gets the chat's images, transcript and socket; other phones do not."""
    requests = {**callers, "direct-phone": _direct_request()}
    phones = {who: requests[who] for who in PHONES}
    for worker in worker_list:
        answers = _agree(_transcript_matrix(worker, phones, [session_id]))
        assert answers == {(who, session_id): who == creator for who in PHONES}, worker.name


def _share(source, *others):
    with source.active():
        engine.scan_all()
    converge([source, *others])


@pytest.fixture
def quiet(monkeypatch):
    """No live clients to tell about a new chat."""
    from app.core import sync_engine
    for name in ("broadcast_user_session_created", "broadcast_session_updated",
                 "broadcast_message_added", "broadcast_session_created"):
        if hasattr(sync_engine.sync_engine, name):
            monkeypatch.setattr(sync_engine.sync_engine, name, AsyncMock())


@CREATORS
def test_post_sessions(workers, callers, quiet, creator):  # noqa: F811
    from app.api.sessions_create import CreateSessionRequest, create_empty_session
    viewer, owner = _network(workers)
    with owner.active():
        sid = asyncio.run(create_empty_session(_creator_request(creator), CreateSessionRequest())).session_id
        assert mobile_tool_identity.read(sid)["principal_id"] == creator
        assert mobile_tool_identity.read(sid)["credential_hash"] == creator + "-session-hash"
    _share(owner, viewer)
    _only_the_creator([owner, viewer], callers, sid, creator)


@CREATORS
def test_post_sessions_with_worktree(workers, callers, quiet, monkeypatch, creator):  # noqa: F811
    from app.api import sessions_worktree
    from app.api.sessions_helpers import CreateWorktreeSessionRequest
    from app.core import checkout_manager, project_resolver
    from app.db import projects_v2
    viewer, owner = _network(workers)
    with owner.active():
        pv2 = projects_v2.upsert_project("git:example.com/repo", name="repo")
    monkeypatch.setattr(sessions_worktree, "require_origin_access",
                        lambda **kw: {"legacy": None, "project_v2": pv2})
    monkeypatch.setattr(project_resolver, "resolve_project_v2", lambda *a, **kw: pv2)
    monkeypatch.setattr(checkout_manager, "ensure_checkout", lambda *a, **kw: "/ws/checkouts/repo/feature")
    monkeypatch.setattr(checkout_manager, "local_branch_exists", lambda *a, **kw: False)
    monkeypatch.setattr(sessions_worktree.database, "set_session_branch", lambda *a, **kw: None)
    with owner.active():
        body = CreateWorktreeSessionRequest(project_v2_id=pv2["id"], branch_name="feature", base_branch="main",
                                            profile_id="default", create_new_branch=True)
        sid = asyncio.run(sessions_worktree.create_session_with_worktree(_creator_request(creator), body)).session_id
        assert mobile_tool_identity.read(sid)["principal_id"] == creator
    _share(owner, viewer)
    _only_the_creator([owner, viewer], callers, sid, creator)


@CREATORS
def test_fork(workers, callers, quiet, monkeypatch, creator):  # noqa: F811
    from app.api import sessions_fork
    from app.core.provider import fluid_transcript
    viewer, owner = _network(workers)
    _start_chats(owner, {**CHATS, **DIRECT_CHAT})
    monkeypatch.setattr(fluid_transcript, "is_fluid_session", lambda sdk: True)
    monkeypatch.setattr(fluid_transcript, "fluid_transcript_entries", lambda sdk: [{"uuid": "node-1"}])
    monkeypatch.setattr(fluid_transcript, "fork_fluid_session", lambda *a, **kw: ("fluid-fork", 1))
    with owner.active():
        database.update_session(session_id=SOURCE[creator], sdk_session_id="fluid-source")
        fork = asyncio.run(sessions_fork.fork_session(_creator_request(creator), SOURCE[creator],
                                                      sessions_fork.ForkSessionRequest()))
        assert mobile_tool_identity.read(fork.session_id)["principal_id"] == creator
    _share(owner, viewer)
    _only_the_creator([owner, viewer], callers, fork.session_id, creator)


@CREATORS
def test_fork_here(workers, callers, quiet, monkeypatch, creator):  # noqa: F811
    """A copy of a chat another worker runs is made, and run, on the viewer."""
    from app.api import network as network_api
    viewer, owner = _network(workers)
    _start_chats(owner, {**CHATS, **DIRECT_CHAT})
    _share(owner, viewer)
    monkeypatch.setattr(engine, "kick", lambda: None)
    with viewer.active():
        sid = asyncio.run(network_api.fork_session_here(SOURCE[creator], _creator_request(creator)))["session_id"]
        assert mobile_tool_identity.read(sid)["principal_id"] == creator
    _share(viewer, owner)
    with owner.active():
        assert database.get_session(sid)["owner_device_id"] == viewer.device_id
    _only_the_creator([viewer, owner], callers, sid, creator)


@CREATORS
def test_task_worker_child(workers, callers, quiet, monkeypatch, creator):  # noqa: F811
    """A local Task worker of a phone's chat: the phone reads it, others do not."""
    from app.core import provider_agent, turn_manager, turn_scheduler
    viewer, owner = _network(workers)
    _start_chats(owner, {**CHATS, **DIRECT_CHAT})
    submitted = []
    monkeypatch.setattr(turn_manager, "submit", lambda sid, source, **kw: submitted.append(sid) or "turn-1")
    monkeypatch.setattr(turn_manager, "submit_async", AsyncMock(side_effect=lambda sid, source, **kw:
                                                                 submitted.append(sid) or "turn-1"))
    monkeypatch.setattr(turn_scheduler, "kick", lambda *a, **kw: None)
    with owner.active():
        outcome = asyncio.run(provider_agent.dispatch_worker(
            parent_session_id=SOURCE[creator], profile_id="default", prompt="Check the disk.",
            background=True))
        child = outcome.session_id
        assert submitted == [child]
        row = database.get_session(child)
        assert row["is_worker"] and row["parent_session_id"] == SOURCE[creator]
        # Only the label: a child gets no record, so no Android tool authority.
        assert mobile_tool_identity.read(child) is None
        assert row["provenance_owner"] == CHATS.get(SOURCE[creator], DIRECT_CHAT.get(SOURCE[creator]))["owner"]
    _share(owner, viewer)
    _only_the_creator([owner, viewer], callers, child, creator)


@CREATORS
def test_remote_task_child(workers, callers, monkeypatch, creator):  # noqa: F811
    """A Task child that runs on another worker takes the parent's label there."""
    from tests.core.network.test_remote_task_control import dispatch, network
    monkeypatch.setattr("app.core.turn_scheduler.kick", lambda *a, **kw: None)
    a, b, _ = network(workers, monkeypatch)  # b runs the parent chat, a runs the child
    for worker in (a, b):
        with worker.active():
            mobile_tool_identity.init_schema()
    with b.active():
        mobile_tool_identity.bind("caller", {**CHATS, **DIRECT_CHAT}[SOURCE[creator]], new=True)
    _share(b, a)
    with b.active():
        result = dispatch(a, b, caller_row=database.get_session("caller"))
    assert result["ok"] is True, result
    child = result["worker_session_id"]
    with a.active():
        assert mobile_tool_identity.read(child) is None
        assert database.get_session(child)["provenance_owner"] == database.get_session("caller")["provenance_owner"]
    _share(a, b)
    _only_the_creator([a, b], callers, child, creator)


@CREATORS
def test_a_chat_started_for_another_worker(workers, callers, monkeypatch, creator):  # noqa: F811
    """The chat socket (or a remote worktree compose) creates the chat on the
    worker that will run it; that worker labels it from the request (#1877)."""
    from app.network import control
    from tests.core.network.test_remote_task_control import network
    a, b, _ = network(workers, monkeypatch)  # b's phone starts a chat that a runs
    for worker in (a, b):
        with worker.active():
            mobile_tool_identity.init_schema()
    label = {**CHATS, **DIRECT_CHAT}[SOURCE[creator]]["owner"]
    with b.active():
        asyncio.run(control.create_session(a.device_id, {
            "id": "remote-chat", "profile_id": "default", "title": "Started on b",
            "provenance_owner": label}, None))
    with a.active():
        assert database.get_session("remote-chat")["owner_device_id"] == a.device_id
        assert database.get_session("remote-chat")["provenance_owner"] == label
    _share(a, b)
    _only_the_creator([a, b], callers, "remote-chat", creator)
    with a.active():
        # A label this build cannot read is not written.
        asyncio.run(control._sessions({"session": {"id": "odd-chat", "profile_id": "default",
                                                   "provenance_owner": "someone"}}, b.device_id))
        assert database.get_session("odd-chat")["provenance_owner"] is None
