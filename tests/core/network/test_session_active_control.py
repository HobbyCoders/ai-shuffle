"""Settlement must change the owner, not only a replicated session row."""
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


async def test_remote_settlement_updates_owner_and_mirror_before_return(workers, monkeypatch):
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
            assert engine.scan_all() == 0  # viewing/scanning a mirror cannot republish it
        with a.active():
            assert bool(database.get_session("settle-remote")["is_active"]) is active
            # A later unrelated owner update cannot resurrect stale membership.
            database.update_session("settle-remote", title="Still on owner")
            engine.scan_all()
        converge([a, b])
        with b.active():
            assert bool(database.get_session("settle-remote")["is_active"]) is active


async def test_remote_settlement_offline_does_not_modify_mirror(workers):
    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Home", device_name="A")
    join(a, b)
    _new_session(a, "offline-settle", "On owner")
    converge([a, b])
    with b.active():
        store.touch_member(a.device_id, endpoints=[])
        transport._links.clear()
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as error:
            await settle("offline-settle", False)
        assert error.value.status_code == 503
        assert bool(database.get_session("offline-settle")["is_active"]) is True


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
        assert bool(database.get_session("reverse-active")["is_active"]) is True
        monkeypatch.setattr(control, "call", AsyncMock(return_value=first))
        row = await session_active.set_remote(a.device_id, "reverse-active", False)
        assert bool(row["is_active"]) is True


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


@pytest.mark.parametrize("active", [None, "false", 0, 1, []])
async def test_control_rejects_non_boolean_state(active):
    from app.network import session_active
    with pytest.raises(ValueError, match="boolean"):
        await session_active.handle({"session_id": "any", "is_active": active}, "peer")
