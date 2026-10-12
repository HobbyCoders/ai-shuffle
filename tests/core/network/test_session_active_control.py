"""Sidebar membership works offline and converges independently of its owner."""
import httpx
import pytest
from starlette.requests import Request

from app.api import sessions as api
from app.db import database
from app.db import network as store
from app.network import control, engine, transport
from tests.core.network.harness import converge, join
from tests.core.network.test_control import _new_session
from tests.core.network.test_transport import ServedBy


def connect_owner(monkeypatch, owner):
    served = ServedBy(owner, "http://owner.local:8000")
    real_async = httpx.AsyncClient

    def async_client(*args, **kwargs):
        kwargs["transport"] = served.transport()
        return real_async(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", async_client)
    return served


async def settle(session_id, active):
    return await api.set_session_active(
        Request({"type": "http", "headers": []}), session_id,
        api.SessionActiveUpdate(is_active=active), token="admin",
    )


async def test_shared_settlement_updates_mirror_then_owner_on_sync(workers, monkeypatch):
    a, b = workers("a"), workers("b")
    served = connect_owner(monkeypatch, a)
    with a.active():
        engine.create_network("Home", device_name="A")
    join(a, b)
    _new_session(a, "settle-remote", "On owner")
    converge([a, b])
    # The control reply must not skip a missing predecessor in the owner stream.
    with a.active():
        database.update_session("settle-remote", title="Unseen owner update")
        engine.scan_all()
    for active in (False, True, False):
        with b.active():
            store.touch_member(a.device_id, endpoints=[served.base_url])
            result = await settle("settle-remote", active)
            assert bool(result["is_active"]) is active
            assert bool(database.get_session("settle-remote")["is_active"]) is active
            # Only shared membership travels from the viewer, never ownership.
            engine.scan_all()
        converge([a, b])
        with a.active():
            assert bool(database.get_session("settle-remote")["is_active"]) is active
            # A later unrelated owner update cannot resurrect stale membership.
            database.update_session("settle-remote", title="Still on owner")
            engine.scan_all()
        converge([a, b])
        with b.active():
            assert bool(database.get_session("settle-remote")["is_active"]) is active


async def test_remote_settlement_offline_updates_mirror_and_reconciles(workers, monkeypatch):
    from unittest.mock import AsyncMock

    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Home", device_name="A")
    join(a, b)
    _new_session(a, "offline-settle", "On owner")
    converge([a, b])
    call = AsyncMock(side_effect=AssertionError("Sidebar edits must not call the execution owner"))
    monkeypatch.setattr(control, "call", call)
    with b.active():
        store.touch_member(a.device_id, endpoints=[])
        transport._links.clear()
        before = database.get_session("offline-settle")
        result = await settle("offline-settle", False)
        assert bool(result["is_active"]) is False
        assert bool(database.get_session("offline-settle")["is_active"]) is False
        for field in ("owner_device_id", "instance_id", "placement", "status", "updated_at"):
            assert result[field] == before[field]
        # Sidebar filtering reads the projected row, not an in-memory override.
        assert not database.get_sessions(active_only=True)
    call.assert_not_called()
    with a.active():
        assert bool(database.get_session("offline-settle")["is_active"]) is True
        # Owner publishes stale is_active before it learns about the offline edit.
        database.update_session("offline-settle", title="Owner restarted")
        engine.scan_all()
    converge([a, b])
    for worker in (a, b):
        with worker.active():
            row = database.get_session("offline-settle")
            assert bool(row["is_active"]) is False
            assert row["title"] == "Owner restarted"
            assert row["owner_device_id"] == a.device_id


async def test_reversed_responses_cannot_restore_old_membership(workers, monkeypatch):
    from unittest.mock import AsyncMock

    from app.network import session_active
    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Home", device_name="A")
    join(a, b)
    _new_session(a, "reverse-active", "On owner")
    converge([a, b])
    with a.active():
        first = await session_active.handle({"session_id": "reverse-active", "is_active": False}, b.device_id)
        second = await session_active.handle({"session_id": "reverse-active", "is_active": True}, b.device_id)
    with b.active():
        assert engine.apply_batch([second["envelope"]]).pending == 1
        engine.apply_batch([first["envelope"]])
    # Shared membership precedes each legacy owner reply in the same stream.
    converge([a, b])
    with b.active():
        assert bool(database.get_session("reverse-active")["is_active"]) is True
        monkeypatch.setattr(control, "call", AsyncMock(return_value=first))
        row = await session_active.set_remote(a.device_id, "reverse-active", False)
        assert bool(row["is_active"]) is True


async def test_legacy_owner_error_is_not_called_unreachable(workers, monkeypatch):
    """#1621: an owner that answers 500 was reached; keep that distinct from offline."""
    from app.network import session_active
    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Home", device_name="A")
    join(a, b)
    _new_session(a, "error-settle", "On owner")
    converge([a, b])

    async def failing_call(*_args, **_kwargs):
        raise control.RunnerUnreachable("HTTP 500")

    with b.active():
        monkeypatch.setattr(control, "call", failing_call)
        with pytest.raises(control.RunnerUnreachable) as error:
            await session_active.set_remote(a.device_id, "error-settle", False)
        assert str(error.value).startswith("A, the worker that runs this session, hit an error")
        assert "can't be reached" not in str(error.value)
        assert bool(database.get_session("error-settle")["is_active"]) is True


async def test_unpublishable_edit_rolls_back_and_nonowner_refuses(workers, monkeypatch):
    from app.network import session_active
    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Home", device_name="A")
    join(a, b)
    _new_session(a, "guard-active", "On owner")
    converge([a, b])
    body = {"session_id": "guard-active", "is_active": False}
    with b.active():
        with pytest.raises(PermissionError, match="does not own"):
            await session_active.handle(body, a.device_id)
        assert bool(database.get_session("guard-active")["is_active"]) is True
    with a.active():
        monkeypatch.setattr(engine, "record", lambda *args, **kwargs: None)
        with pytest.raises(PermissionError, match="not ready"):
            await session_active.handle(body, b.device_id)
        assert bool(database.get_session("guard-active")["is_active"]) is True


async def test_local_settle_commits_while_publishing_is_held(workers):
    """#1621: a worker that restarts while its only peer is offline holds its
    outbox until it can compare streams with that peer, which lasted ~7 hours
    on the owner's server. Its own UI must still settle and reopen the
    sessions it owns; the row is published once the hold lifts."""
    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Home", device_name="A")
    join(a, b)
    _new_session(a, "held-settle", "On owner")
    converge([a, b])
    with a.active():
        engine.hold_outbox(True)  # restarted; peer b unreachable
        try:
            for active in (False, True, False):
                result = await settle("held-settle", active)
                assert bool(result["is_active"]) is active
                assert bool(database.get_session("held-settle")["is_active"]) is active
            assert engine.scan_all() == 0  # still held: nothing is signed yet
        finally:
            engine.hold_outbox(False)
        assert engine.scan_all() > 0
    converge([a, b])
    with b.active():
        assert bool(database.get_session("held-settle")["is_active"]) is False


async def test_local_settle_commits_while_joining_worker_bootstraps(workers, monkeypatch):
    """#1621: during startup sync no envelope can be signed; local settles
    must not fail with 409 for that reason, while a peer asking this worker
    still gets a refusal it can show."""
    from app.network import session_active
    a = workers("a")
    with a.active():
        engine.create_network("Home", device_name="A")
    _new_session(a, "boot-settle", "Local")
    with a.active():
        monkeypatch.setattr(engine, "record", lambda *args, **kwargs: None)
        row = await session_active.set_local("boot-settle", False)
        assert bool(row["is_active"]) is False
        assert bool(database.get_session("boot-settle")["is_active"]) is False
        with pytest.raises(PermissionError, match="still syncing"):
            await session_active.handle({"session_id": "boot-settle", "is_active": True}, "peer")
        assert bool(database.get_session("boot-settle")["is_active"]) is False




@pytest.mark.parametrize("via", ["local", "control", "mirror", "replication"])
@pytest.mark.parametrize("kind", ["permission", "question", "prose", "private"])
async def test_settlement_dismisses_wait_on_execution_owner(workers, monkeypatch, via, kind):
    import asyncio
    from unittest.mock import AsyncMock
    from app.core import chat_access, interaction_events
    from app.core.global_events import global_events
    from app.core.permission_handler import permission_handler
    from app.core.user_question_handler import user_question_handler
    from app.db import interactions, session_events
    from app.network import session_active, session_runtime, startup
    from tests.core.network.harness import exchange

    a, b = workers("owner"), workers("mirror")
    served = connect_owner(monkeypatch, a)
    with a.active():
        engine.create_network("Home")
    join(a, b)
    _new_session(a, "waiting", "Waiting on owner")
    converge([a, b])
    broker = chat_access.Broker()
    monkeypatch.setattr(chat_access, "broker", broker)
    monkeypatch.setattr(chat_access, "session_owner", AsyncMock(return_value=(None, "owner", None)))
    waiter = None
    with a.active():
        if kind == "permission":
            registered = asyncio.Event()
            emit = interaction_events.emit_interaction_requested

            async def requested(*args, **kwargs):
                await emit(*args, **kwargs)
                registered.set()
            monkeypatch.setattr(interaction_events, "emit_interaction_requested", requested)
            waiter = asyncio.create_task(permission_handler.request_permission(
                "request", "waiting", "default", "Bash", {"command": "true"}, AsyncMock()))
            await asyncio.wait_for(registered.wait(), 5)
        elif kind == "question":
            await user_question_handler.post_question(request_id="request", tool_use_id="tool",
                session_id="waiting", questions=[{"question": "Proceed?", "header": "Confirm"}],
                broadcast_func=AsyncMock())
        elif kind == "prose":
            session_events.append_events("waiting", [{"type": "message.assistant", "payload": {"content": "Proceed?"}}])
        else:
            await broker.request("waiting", "service", "Read a service")
        session_runtime.publish_owned(engine.context(), session_ids=["waiting"])
    converge([a, b])
    try:
        if via == "local":
            with a.active():
                await session_active.set_local("waiting", False)
        elif via == "control":
            with b.active():
                store.touch_member(a.device_id, endpoints=[served.base_url])
                await session_active.set_remote(a.device_id, "waiting", False)
        elif via == "mirror":
            with b.active():
                store.touch_member(a.device_id, endpoints=[served.base_url])
                await settle("waiting", False)
        else:
            with b.active():
                store.touch_member(a.device_id, endpoints=[])
                transport._links.clear()
                await settle("waiting", False)
            with a.active():
                assert database.get_session("waiting")["is_active"]
            # Use the real applied-record hook, not a direct dismissal call.
            finished = asyncio.Event()
            real_dismiss = session_active.dismiss_replicated

            async def dismissed(ids):
                await real_dismiss(ids)
                finished.set()
            monkeypatch.setattr(session_active, "dismiss_replicated", dismissed)
            monkeypatch.setattr(global_events, "_loop", asyncio.get_running_loop())
            unsubscribe = engine.on_applied(startup._on_applied)
            try:
                exchange(b, a)
                with a.active():
                    await asyncio.wait_for(finished.wait(), 5)
            finally:
                unsubscribe()
        with a.active():
            assert not database.get_session("waiting")["is_active"]
            assert not interactions.get_pending_interactions("waiting")
            assert database.get_session("waiting")["last_assistant_question_seq"] is None
            assert not broker.pending_count("waiting")
            if waiter:
                assert (await asyncio.wait_for(waiter, 5)).behavior == "deny"
            assert session_runtime.for_sessions(["waiting"])["waiting"]["state"] == "idle"
        converge([a, b])
        with b.active():
            assert session_runtime.for_sessions(["waiting"])["waiting"]["state"] == "idle"
            assert database.get_session("waiting")["last_assistant_question_seq"] is None
            await settle("waiting", True)
        converge([a, b])
        with a.active():
            assert database.get_session("waiting")["is_active"]
    finally:
        if waiter and not waiter.done():
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
        broker.dismiss_pending("waiting")
        with a.active():
            await asyncio.gather(*broker._notifications)




@pytest.mark.parametrize("reopened", [True, False])
async def test_delayed_fast_path_does_not_rewrite_newer_membership(workers, monkeypatch, reopened):
    import asyncio
    from unittest.mock import AsyncMock
    from app.core import chat_access
    from app.core.user_question_handler import user_question_handler
    from app.db import interactions, session_events
    from app.db.base import get_db
    from app.network import session_active

    a, b = workers("owner"), workers("mirror")
    served = connect_owner(monkeypatch, a)
    with a.active():
        engine.create_network("Home")
    join(a, b)
    _new_session(a, "late-settle", "Waiting")
    converge([a, b])
    calls = []
    real_call = control.call

    async def delayed(owner, command, payload):
        calls.append((owner, command, payload))
        raise control.RunnerUnreachable("Delivery delayed")
    monkeypatch.setattr(control, "call", delayed)
    with b.active():
        store.touch_member(a.device_id, endpoints=[served.base_url])
        await settle("late-settle", False)
    assert len(calls) == 1
    assert calls[0][1] == "session-settle-dismiss"  # Old owners safely refuse, never re-settle.
    assert "settled_membership" in calls[0][2]
    converge([a, b])
    broker = chat_access.Broker()
    monkeypatch.setattr(chat_access, "broker", broker)
    monkeypatch.setattr(chat_access, "session_owner", AsyncMock(return_value=(None, "owner", None)))
    with a.active():
        await session_active.set_local("late-settle", True)
        if not reopened:
            await session_active.set_local("late-settle", False)
        permission = interactions.create_interaction("late-settle", "permission", {"request_id": "fresh"}, 300)
        await user_question_handler.post_question(request_id="fresh-question", tool_use_id="tool",
            session_id="late-settle", questions=[{"question": "Fresh?"}], broadcast_func=AsyncMock())
        private = await broker.request("late-settle", "service", "Fresh access")
        session_events.append_events("late-settle", [{"type": "message.assistant", "payload": {"content": "Fresh prose?"}}])
        with get_db() as conn:
            before = store.get_record(conn.cursor(), "session_active", "late-settle")
    try:
        with b.active():
            reply = await real_call(*calls[0])
        assert reply == {"dismissed": False}
        with a.active():
            assert bool(database.get_session("late-settle")["is_active"]) is reopened
            assert interactions.get_interaction(permission["id"])["state"] == "pending"
            assert len(interactions.get_pending_interactions("late-settle")) == 2
            assert private["request_id"] in broker.requests
            assert database.get_session("late-settle")["last_assistant_question_seq"] is not None
            with get_db() as conn:
                assert store.get_record(conn.cursor(), "session_active", "late-settle") == before
    finally:
        with a.active():
            broker.dismiss_pending("late-settle")
            await asyncio.gather(*broker._notifications)


@pytest.mark.parametrize("active", [None, "false", 0, 1, []])
async def test_control_rejects_non_boolean_state(active):
    from app.network import session_active
    with pytest.raises(ValueError, match="boolean"):
        await session_active.handle({"session_id": "any", "is_active": active}, "peer")


async def test_remote_archive_and_restore_update_owner_and_mirror(workers, monkeypatch):
    a, b = workers("a"), workers("b")
    served = connect_owner(monkeypatch, a)
    with a.active():
        engine.create_network("Home", device_name="A")
    join(a, b)
    _new_session(a, "archive-remote", "On owner")
    converge([a, b])
    with b.active():
        store.touch_member(a.device_id, endpoints=[served.base_url])
        # A shared reopen leaves a local membership record saying Active; the
        # archive must not let it project back onto the archived mirror.
        await api.set_session_active(
            Request({"type": "http", "headers": []}), "archive-remote",
            api.SessionActiveUpdate(is_active=True), token="admin",
        )
        result = await api.set_session_active(
            Request({"type": "http", "headers": []}), "archive-remote",
            api.SessionActiveUpdate(is_active=True, archived=True), token="admin",
        )
        # Archiving always leaves Active, whatever is_active asked for.
        assert result["status"] == "archived" and not result["is_active"]
        assert not database.get_session("archive-remote")["is_active"]
    with a.active():
        assert database.get_session("archive-remote")["status"] == "archived"
    with b.active():
        result = await api.set_session_active(
            Request({"type": "http", "headers": []}), "archive-remote",
            api.SessionActiveUpdate(is_active=False, archived=False), token="admin",
        )
        assert result["status"] == "active" and not result["is_active"]
    with a.active():
        assert database.get_session("archive-remote")["status"] == "active"
