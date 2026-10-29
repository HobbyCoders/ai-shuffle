"""A mirror stays its owner's after this worker restarts (#782).

Older boot backfills wrote this install's ``instance_id`` onto every mirror
(the column is local-only, so a mirror arrives with NULL) and then pinned it
here. The mirror then looked owned by this worker: sends queued locally and
never ran. These tests rebuild that state on a real two-worker network.
"""
import json

import pytest
from starlette.requests import Request

from app.api.chat_query import submit_http_query
from app.core import placement, turn_manager
from app.core.config import settings
from app.db import database, turns
from app.network import control, engine, startup, transport
from tests.core.network.harness import converge, join
from tests.core.network.test_control import _new_session


def _stamp(worker, session_id, *, pin=False):
    """What an older build's migration pass left on a mirror."""
    me = settings.effective_instance_id
    placement_json = json.dumps({"where": "runner:" + me}) if pin else None
    worker.sql("UPDATE sessions SET instance_id = ?, placement = ? WHERE id = ?",
               (me, placement_json, session_id))


@pytest.fixture
def network(workers, monkeypatch):
    owner, viewer = workers("owner"), workers("viewer")
    from app.db import mobile_tool_identity
    for worker in (owner, viewer):
        with worker.active():
            mobile_tool_identity.init_schema()
    with owner.active():
        engine.create_network("Home", device_name="Owner")
    join(owner, viewer)
    _new_session(owner, "mirrored", "Owner's chat")
    converge([owner, viewer])
    # The owner answers: it is online from the viewer's side.
    monkeypatch.setattr(transport, "online_devices", lambda: {owner.device_id: 0.0})
    return owner, viewer


@pytest.mark.parametrize("pinned", [False, True], ids=["stamped", "stamped-and-pinned"])
async def test_stamped_mirror_send_is_forwarded_to_its_owner(network, monkeypatch, pinned):
    owner, viewer = network
    _stamp(viewer, "mirrored", pin=pinned)
    with viewer.active():
        startup.repair_stamped_mirror_pins(viewer.device_id)
        mirror = database.get_session("mirrored")
        assert mirror["instance_id"] == settings.effective_instance_id
        assert control.owner_of(mirror) == owner.device_id
        decision = placement.resolve_placement(mirror)
        assert (decision.kind, decision.runner_id) == ("runner", owner.device_id)

    monkeypatch.setattr(turn_manager, "_kick", lambda sid: None)
    delivered = []

    async def deliver(device, action, body):
        delivered.append((device, action))
        with owner.active():
            return await control._turns(body, viewer.device_id)

    monkeypatch.setattr(control, "call", deliver)
    request = Request({"type": "http", "headers": [], "state": {"auth_token": "local-owner"}})
    with viewer.active():
        await submit_http_query(request, "mirrored", {"prompt": "continue"}, compact_first=False)
        assert turns.get_queued_turns("mirrored") == []  # nothing queued on the mirror
    assert delivered == [(owner.device_id, "turns")]
    with owner.active():
        assert [t["prompt"] for t in turns.get_queued_turns("mirrored")] == ["continue"]


def test_repair_clears_only_backfill_pins_on_other_devices_sessions(network):
    owner, viewer = network
    me = settings.effective_instance_id
    _new_session(viewer, "viewer-own", "Viewer's chat")
    _new_session(owner, "handed", "Handed off")
    converge([owner, viewer])
    _stamp(viewer, "mirrored", pin=True)
    # This device's own session, pinned here: not a mirror, left alone.
    viewer.sql("UPDATE sessions SET placement = ? WHERE id = ?",
               (json.dumps({"where": "runner:" + me}), "viewer-own"))
    # A pin naming the owner device is the owner's own decision.
    owner_pin = json.dumps({"where": "runner:" + owner.device_id})
    viewer.sql("UPDATE sessions SET instance_id = ?, placement = ? WHERE id = ?",
               (me, owner_pin, "handed"))
    with viewer.active():
        assert startup.repair_stamped_mirror_pins(viewer.device_id) == 1
        assert startup.repair_stamped_mirror_pins(viewer.device_id) == 0
        assert database.get_session("mirrored")["placement"] is None
        assert json.loads(database.get_session("viewer-own")["placement"]) == {"where": "runner:" + me}
        assert json.loads(database.get_session("handed")["placement"]) == {"where": "runner:" + owner.device_id}
        own = placement.resolve_placement(database.get_session("viewer-own"))
        assert (own.kind, own.runner_id) == ("runner", me)
