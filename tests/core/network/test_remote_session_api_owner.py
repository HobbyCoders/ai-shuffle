"""R6-2: a session an API key places on a remote runner stays the key's.

The hub writes the row with the key's ``api_user_id`` and asks the runner to
create it. The runner is the row's owner and the only device that publishes
it, so its copy must carry the same attribution, and a mirrored copy that
carries none must never erase the hub's.
"""
import httpx

from app.api.sessions_helpers import api_user_owns_session
from app.db import database
from app.db import network as store
from app.network import control, engine
from tests.core.network.harness import converge, join
from tests.core.network.test_transport import ServedBy


def _serve(monkeypatch, server, base_url):
    served = ServedBy(server, base_url)
    real_async = httpx.AsyncClient

    def async_client(*args, **kwargs):
        kwargs["transport"] = served.transport()
        return real_async(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", async_client)
    return served


def _network_with_key(workers):
    hub, runner = workers("hub"), workers("runner")
    with hub.active():
        engine.create_network("Home", device_name="Hub")
    join(hub, runner)
    with hub.active():
        database.create_api_user(user_id="u-key", name="Key", api_key_hash="k" * 64)
        engine.scan_all()
    converge([hub, runner])
    with runner.active():
        assert database.get_api_user("u-key") is not None
    return hub, runner


async def test_key_session_placed_on_runner_keeps_its_owner_after_sync(workers, monkeypatch):
    hub, runner = _network_with_key(workers)
    served = _serve(monkeypatch, runner, "http://runner.local:8000")
    with hub.active():
        store.touch_member(runner.device_id, endpoints=[served.base_url])
        database.create_session(session_id="s-key", profile_id="default", project_id=None, title="Keyed",
                                api_user_id="u-key", is_active=True, target_instance_id=runner.device_id)
        assert database.get_session("s-key")["owner_device_id"] == runner.device_id
        # The same payload the chat WebSocket sends for a remotely placed session.
        await control.create_session(runner.device_id, {
            "id": "s-key", "profile_id": "default", "title": "Keyed", "api_user_id": "u-key",
        }, None)
    with runner.active():
        row = database.get_session("s-key")
        assert row["owner_device_id"] == runner.device_id
        assert row["api_user_id"] == "u-key"
        engine.scan_all()
    converge([hub, runner])
    with hub.active():
        row = database.get_session("s-key")
        assert row["api_user_id"] == "u-key"
        assert api_user_owns_session({"id": "u-key"}, row)


async def test_runner_refuses_an_api_user_it_does_not_know(workers, monkeypatch):
    hub, runner = _network_with_key(workers)
    served = _serve(monkeypatch, runner, "http://runner.local:8000")
    with hub.active():
        store.touch_member(runner.device_id, endpoints=[served.base_url])
        await control.create_session(runner.device_id, {
            "id": "s-ghost", "profile_id": "default", "title": "Ghost", "api_user_id": "u-nobody",
        }, None)
    with runner.active():
        assert database.get_session("s-ghost")["api_user_id"] is None


def test_owner_copy_without_attribution_never_erases_the_hubs(workers):
    """A runner that predates the fix (or lost the field) publishes api_user_id NULL."""
    hub, runner = _network_with_key(workers)
    with runner.active():
        database.create_session(session_id="s-legacy", profile_id="default", project_id=None, title="Legacy",
                                is_active=True)
        engine.scan_all()
    converge([hub, runner])
    hub.sql("UPDATE sessions SET api_user_id = ? WHERE id = ?", ("u-key", "s-legacy"))
    with hub.active():
        assert database.get_session("s-legacy")["api_user_id"] == "u-key"
    with runner.active():
        database.update_session(session_id="s-legacy", title="Legacy, renamed")
        engine.scan_all()
    converge([hub, runner])
    with hub.active():
        row = database.get_session("s-legacy")
        assert row["title"] == "Legacy, renamed"
        assert row["api_user_id"] == "u-key"


def test_key_session_created_on_the_runner_mirrors_with_its_owner(workers):
    hub, runner = _network_with_key(workers)
    with runner.active():
        database.create_session(session_id="s-born-remote", profile_id="default", project_id=None,
                                title="Born on runner", api_user_id="u-key", is_active=True)
        engine.scan_all()
    converge([hub, runner])
    with hub.active():
        row = database.get_session("s-born-remote")
        assert row["owner_device_id"] == runner.device_id
        assert api_user_owns_session({"id": "u-key"}, row)
