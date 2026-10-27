"""#700 on a real two-worker network: a call on a mirror writes nothing shared.

The unit tests run without a network context, where ``set_session_active`` is
a plain local UPDATE. On a networked worker it is a shared write
(``session_active.write_shared``) that replicates to every worker, so these
tests drive the call's paths on a real mirror and check that nothing was
recorded for the owner's chat.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.core import voice_bridge, voice_remote, voice_stream
from app.db import database, session_events
from app.db.base import get_db
from app.network import control, engine
from tests.core.network.harness import converge, join
from tests.core.network.test_control import _new_session


def shared_active_rows(session_id):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM network_session_active WHERE session_id = %s", (session_id,))
        return [dict(r) for r in cursor.fetchall()]


def network(workers):
    owner, viewer = workers("gamingpc"), workers("unraid")
    with owner.active():
        engine.create_network("Home", device_name="GamingPC")
    join(owner, viewer)
    _new_session(owner, "peer-chat", "Rail icons")
    converge([owner, viewer])
    return owner, viewer


def test_a_capped_call_on_a_mirror_leaves_the_shared_active_flag_alone(workers):
    owner, viewer = network(workers)
    with viewer.active():
        row = database.get_session("peer-chat")
        assert voice_remote.owner_of(row) == owner.device_id
        before = shared_active_rows("peer-chat")

        voice_bridge.registry.mark_capped("peer-chat", 30)  # Both transports cap through here.

        assert shared_active_rows("peer-chat") == before
        assert database.get_session("peer-chat")["is_active"] == row["is_active"]
        assert voice_bridge.registry.capped_minutes("peer-chat") == 30  # The cap itself still holds.


def test_a_capped_call_on_a_chat_run_here_still_clears_it(workers):
    """The same path on the viewer's own chat is the real shared write."""
    _owner, viewer = network(workers)
    _new_session(viewer, "own-chat", "Viewer's chat")
    with viewer.active():
        voice_bridge.registry.mark_capped("own-chat", 30)

        rows = shared_active_rows("own-chat")
        assert rows and not rows[0]["is_active"]


async def test_a_streamed_exchange_on_a_mirror_writes_nothing(workers, monkeypatch):
    owner, viewer = network(workers)
    sent = []

    async def owner_call(device_id, action, body=None, **_kwargs):
        sent.append((device_id, action, body))
        return {"turn_id": "owner-turn-1"}

    monkeypatch.setattr(control, "call", owner_call)
    with viewer.active():
        events_before = session_events.get_events("peer-chat", from_seq=0, limit=50)
        socket = SimpleNamespace(send_json=AsyncMock(), send_bytes=AsyncMock(), close=AsyncMock())
        call = voice_stream.VoiceStream(socket, {"id": "peer-chat"}, {"model": "gpt-live-1", "voice": "marin"}, "k", 5)
        call.remote_owner = voice_remote.owner_of(database.get_session("peer-chat"))
        call.upstream = SimpleNamespace(send=AsyncMock())
        call.progress = SimpleNamespace(submitted=AsyncMock())
        await call.caption("user", "Make the rail icons bigger.")

        await call.run_delegation("delegation-1")
        await call.flush()

        assert [(d, a) for d, a, _ in sent] == [(owner.device_id, "turns")]
        assert session_events.get_events("peer-chat", from_seq=0, limit=50) == events_before
        assert shared_active_rows("peer-chat") == []
