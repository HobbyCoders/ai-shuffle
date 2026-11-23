"""POST /session/{id}/stop reaches the worker that runs the chat.

The running-tasks list stops a chat this one started with this route. The chat
may run on another worker; there it is a read-only mirror here, so the Stop
must be relayed to its owner, as the chat's own Stop button does.
"""
from types import SimpleNamespace

import httpx

from app.db import database
from app.db import network as store
from app.network import control, engine
from tests.core.network.harness import converge, join
from tests.core.network.test_transport import ServedBy


def _owner_request():
    return SimpleNamespace(state=SimpleNamespace(api_user=None))


async def test_stop_on_a_mirror_stops_the_chat_on_its_owner(workers, monkeypatch):
    a, b = workers("runs-chat"), workers("shows-chat")
    served = ServedBy(a, "http://a.local:8000")
    real_async = httpx.AsyncClient

    def async_client(*args, **kwargs):
        kwargs["transport"] = served.transport()
        return real_async(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", async_client)
    interrupted, swept = [], []

    async def fake_interrupt(session_id, mode, stop_background=False, turn_id=None, source=None):
        interrupted.append((session_id, mode, stop_background, turn_id, source))
        return {"id": "turn_1"}

    async def fake_sweep(session_id, **kwargs):
        swept.append(session_id)
        return []

    from app.core import query_session_lifecycle, turn_manager
    monkeypatch.setattr(turn_manager, "interrupt_async", fake_interrupt)
    monkeypatch.setattr(turn_manager, "capture_stop_receipt", lambda *_args: {"stop_seq": 1})
    monkeypatch.setattr(query_session_lifecycle, "stop_all_background_tasks", fake_sweep)

    with a.active():
        engine.create_network("Home", device_name="A")
    join(a, b)
    with a.active():
        database.create_session(session_id="child", profile_id="default", project_id=None,
                                title="Started chat", is_active=True)
        engine.scan_all()
    converge([a, b])
    with a.active():
        store.touch_member(b.device_id, endpoints=[])
    with b.active():
        store.touch_member(a.device_id, endpoints=["http://a.local:8000"])
        assert control.owner_of(database.get_session("child")) == a.device_id
        from app.api.query import stop
        result = await stop(_owner_request(), "child", "token")

    assert result == {"status": "stopped", "session_id": "child"}
    # The owner ran the chat's own Stop: the turn and its background work.
    assert interrupted == [("child", "stop", True, None, "user_stop")]
    assert swept == ["child"]
