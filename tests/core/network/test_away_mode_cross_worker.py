"""Away mode on a session another worker owns.

The flag lives on the owner's session row. A viewing worker serves the
composer from its mirror of that row, so the flag has to travel with the
session catalog; a toggle from the viewer goes to the owner over the peer
control route and comes back through the mirror. An owner on an older build
that does not publish the field leaves the viewer at "unknown", which reads
as off.
"""
from app.api import sessions as sessions_api
from app.api.sessions_permissions import AwayModeUpdate, apply_away_mode, update_away_mode
from app.core.models import Session
from app.db import database
from app.db import network as store
from app.db.base import get_db
from app.network import engine, policy
from tests.api.helpers import fake_request
from tests.core.network.harness import converge, join
from tests.core.network.test_remote_session_api_owner import _serve

SESSION = "s-away-remote"


def _network(workers):
    """DesktopWorker views a session that ServerWorker owns and runs."""
    viewer, owner = workers("desktopworker"), workers("unraid")
    with viewer.active():
        engine.create_network("Home", device_name="DesktopWorker")
    join(viewer, owner)
    with owner.active():
        database.create_session(session_id=SESSION, profile_id="default", project_id=None,
                                title="Remote chat", is_active=True)
        engine.scan_all()
    converge([viewer, owner])
    with viewer.active():
        row = database.get_session(SESSION)
        assert row is not None and row["owner_device_id"] == owner.device_id
    return viewer, owner


def _publish(owner, viewer):
    with owner.active():
        engine.scan_all()
    converge([viewer, owner])


async def _served_away(viewer) -> object:
    with viewer.active():
        detail = await sessions_api.get_session(fake_request(), SESSION)
    return Session.model_validate(detail).away_mode


def test_away_mode_travels_with_the_session_catalog():
    coll = policy.collection("sessions")
    assert "away_mode" in coll.fields
    assert "away_mode" not in coll.local_fields
    assert engine.serialize_row(coll, {"id": "s1", "away_mode": True})["away_mode"] is True


async def test_viewer_reads_the_owners_away_state(workers):
    viewer, owner = _network(workers)
    with owner.active():
        await apply_away_mode(SESSION, True)
    _publish(owner, viewer)
    with viewer.active():
        assert database.get_session(SESSION)["away_mode"]
    # The composer reads the flag from the session GET the viewer serves.
    assert await _served_away(viewer) is True

    with owner.active():
        await apply_away_mode(SESSION, False)
    _publish(owner, viewer)
    assert await _served_away(viewer) is False


async def test_viewer_toggle_lands_on_the_owner_and_comes_back(workers, monkeypatch):
    viewer, owner = _network(workers)
    served = _serve(monkeypatch, owner, "http://unraid.local:8000")
    with viewer.active():
        store.touch_member(owner.device_id, endpoints=[served.base_url])
        result = await update_away_mode(fake_request(), SESSION, AwayModeUpdate(away=True), token="t")
    assert result["away"] is True
    assert ("POST", "/api/v1/network/peer/control/away-mode") in served.requests
    with owner.active():
        assert database.get_session(SESSION)["away_mode"]
    _publish(owner, viewer)
    assert await _served_away(viewer) is True

    with viewer.active():
        result = await update_away_mode(fake_request(), SESSION, AwayModeUpdate(away=False), token="t")
    assert result["away"] is False
    with owner.active():
        assert not database.get_session(SESSION)["away_mode"]
    _publish(owner, viewer)
    assert await _served_away(viewer) is False


async def test_owner_on_an_older_build_reads_as_off(workers):
    """A record without the field (an older owner) leaves the mirror unknown."""
    viewer, _owner = _network(workers)
    coll = policy.collection("sessions")
    with viewer.active():
        record = engine.serialize_row(coll, database.get_session(SESSION))
        record.pop("away_mode", None)
        record["title"] = "Renamed by an older owner"
        with get_db() as conn:
            engine.materialize(conn.cursor(), coll, SESSION, record)
        assert database.get_session(SESSION)["title"] == "Renamed by an older owner"
    assert not await _served_away(viewer)


async def test_global_away_reaches_an_offline_worker_and_releases_its_local_cards(workers, monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock, patch

    from app.core import away_mode, global_away_mode
    from app.core.global_events import global_events
    from app.db import interactions
    from app.network import startup
    from tests.core.network.harness import _ACTIVE

    assert policy.is_shared_setting_key("global_away_mode")
    viewer, owner = _network(workers)
    from app.db import mobile_tool_identity
    for worker in (viewer, owner):
        with worker.active():
            mobile_tool_identity.init_schema()
    with owner.active():
        question = interactions.create_interaction(SESSION, "question", {
            "request_id": "q", "questions": [{"question": "Track?", "options": ["Stable", "Nightly (Recommended)"]}],
        }, 300)
        approval = interactions.create_interaction(SESSION, "permission", {
            "request_id": "p", "tool_name": "Bash",
        }, 300)
    with viewer.active():
        assert (await global_away_mode.apply(True))["deferred_approvals"] == 0
        engine.scan_all()
    # No exchange yet: the worker that owns the cards was offline.
    with owner.active():
        database.invalidate_system_setting_memo("global_away_mode")
        assert not away_mode.global_away_enabled()
        assert interactions.get_interaction(approval["id"])["state"] == "pending"

    scheduled = []
    def schedule(coro, loop):
        scheduled.append((_ACTIVE.get(), coro))

    monkeypatch.setattr(global_events, "_loop", asyncio.get_running_loop())
    monkeypatch.setattr(startup.asyncio, "run_coroutine_threadsafe", schedule)
    unsubscribe = engine.on_applied(startup._on_applied)
    try:
        with patch.object(away_mode, "record_decision", AsyncMock()):
            converge([viewer, owner])
            for worker, coro in scheduled:
                with worker.active():
                    await coro
            assert scheduled, "the real sync apply must schedule the release"
            with owner.active():
                assert database.get_system_setting("global_away_mode") == "1"
                assert away_mode.is_away(SESSION)
                assert interactions.get_interaction(question["id"])["state"] == "answered"
                assert interactions.get_interaction(approval["id"])["answer"] == {
                    "decision": "deny", "behavior": "deny", "message": away_mode.DEFERRED_MESSAGE}
                assert (await global_away_mode.release_local_pending())["deferred_approvals"] == 0
                assert database.get_session(SESSION)["away_mode"] is None
    finally:
        unsubscribe()
