# ruff: noqa: F811
"""Private access for a mirrored session routes to the worker that runs it.

Two simulated workers: A owns a Chat session, B shows its mirror. B's access
API forwards every operation to A over the authenticated control channel; the
grant, the approval and the SSH credentials exist only on A. Both workers'
HTTP surfaces run in-process on this event loop, so A's broker futures and
B's forwarded calls interleave the way they do in production.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from app.api import chat_access as access_api
from app.api import network as network_api
from app.api.auth import require_admin, require_auth
from app.core import chat_access
from app.db import database
from app.db import network as store
from app.db import session_events as events_db
from app.network import chat_access_routing as routing
from app.network import control, engine
from tests.core.network.harness import converge, join
from tests.core.test_chat_access import pending, ssh_target  # noqa: F401 - ssh_target is a fixture
from tests.core.test_chat_services import pki, service, spec, trust  # noqa: F401 - shared TLS fixtures

pytestmark = pytest.mark.asyncio

A_URL = "https://a.example"
B_URL = "https://b.example"
SESSION = "mirrored-chat"
PASSWORD = "test-only-password"
BASE = f"/api/v1/chat-access/{SESSION}"


def _app():
    app = FastAPI()
    app.include_router(network_api.router)
    app.include_router(access_api.router)
    app.dependency_overrides[require_admin] = lambda: "admin"
    app.dependency_overrides[require_auth] = lambda: "admin"
    return app


class Network:
    """Routes httpx calls by base URL into each worker's app, under that worker."""

    def __init__(self, monkeypatch, servers):
        self.servers = {base: (worker, httpx.ASGITransport(app=_app())) for base, worker in servers.items()}
        self.seen = []
        self.real_client = httpx.AsyncClient
        monkeypatch.setattr(httpx, "AsyncClient", self._client)

    def _client(self, *args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(self._handle)
        return self.real_client(*args, **kwargs)

    async def _handle(self, request):
        for base, (worker, asgi) in self.servers.items():
            if str(request.url).startswith(base):
                self.seen.append((base, request.url.path, request.content))
                with worker.active():
                    response = await asgi.handle_async_request(request)
                    content = await response.aread()
                return httpx.Response(response.status_code, content=content,
                                      headers={"content-type": response.headers.get("content-type", "")})
        raise httpx.ConnectError("unreachable", request=request)

    def ui(self):
        """The viewing device's browser talking to its own worker over HTTPS."""
        return self.real_client(transport=httpx.ASGITransport(app=_app()), base_url=B_URL)

    def control_paths(self):
        return [path for _, path, _ in self.seen if path.startswith("/api/v1/network/peer/control/")]


@pytest_asyncio.fixture
async def pair(workers, monkeypatch):
    a, b = workers("owner"), workers("viewer")
    with a.active():
        engine.create_network("Home", device_name="Owner PC")
    join(a, b, name="Viewer")
    with a.active():
        database.create_session(session_id=SESSION, profile_id="default", project_id=None, title="Admin chat",
                                is_active=True, mode="chat")
        events_db.append_events(SESSION, [{"type": "message.user",
                                           "payload": {"type": "message.user", "content": "check the server"}}])
        engine.scan_all()
    converge([a, b])
    with b.active():
        assert control.owner_of(database.get_session(SESSION)) == a.device_id

    # The owner's broker reads A's database (synchronously, so no other
    # worker can become active mid-lookup). On B the real lookup refuses.
    async def session_owner(session_id, **_):
        with a.active():
            return chat_access.owner_from_row(database.get_session(session_id))

    owner_broker = chat_access.Broker()
    monkeypatch.setattr(chat_access, "broker", owner_broker)
    monkeypatch.setattr(chat_access, "session_owner", session_owner)
    monkeypatch.setattr(chat_access, "session_bypasses_confirmation", AsyncMock(return_value=False))

    class ViewerHasNoBroker:
        def __getattr__(self, name):
            raise AssertionError(f"the viewer used its own broker ({name})")

    monkeypatch.setattr(access_api, "broker", ViewerHasNoBroker())
    real_push, pushes = routing._push, []

    async def record_push(key, saved):
        pushes.append((key, saved))

    monkeypatch.setattr(routing, "_push", record_push)
    routing._watchers.clear()
    routing._scheduled.clear()
    yield SimpleNamespace(a=a, b=b, broker=owner_broker, pushes=pushes, real_push=real_push)
    for grant in list(owner_broker.grants.values()):
        owner_broker.revoke(grant.session_id, grant.id)
    await asyncio.gather(*owner_broker._notifications, return_exceptions=True)
    routing._watchers.clear()
    routing._scheduled.clear()


def _reach(viewer, owner, url):
    with viewer.active():
        store.touch_member(owner.device_id, endpoints=[url])


async def _wait_for(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.01)


def _connection(spec):
    return {**spec.model_dump(exclude=set(chat_access.SECRET_FIELDS)), "password": PASSWORD}


async def _request(pair, purpose="Inspect the server"):
    with pair.a.active():
        task = asyncio.create_task(pair.broker.request(SESSION, "machine", purpose))
    await _wait_for(lambda: pair.broker.requests)
    return task, next(iter(pair.broker.requests))


async def test_viewer_card_grant_approval_and_revocation_run_on_the_owner(pair, ssh_target, monkeypatch):
    spec, commands, _ = ssh_target
    network = Network(monkeypatch, {A_URL: pair.a})
    _reach(pair.b, pair.a, A_URL)
    with pair.b.active():
        async with network.ui() as ui:
            assert (await ui.get(BASE)).json()["requests"] == []
            # The model asks on the owner; the viewer that looked is told to refresh.
            requested, _ = await _request(pair)
            await _wait_for(lambda: pair.pushes)
            assert ((SESSION, pair.b.device_id), False) in pair.pushes
            snapshot = (await ui.get(BASE)).json()
            assert [r["purpose"] for r in snapshot["requests"]] == ["Inspect the server"]
            request_id = snapshot["requests"][0]["id"]

            response = await ui.post(f"{BASE}/requests/{request_id}", json={"connection": _connection(spec)})
            assert response.status_code == 200, response.text
            handle = await requested
            assert set(handle) == {"id", "kind", "expires_at", "writable"}
            grant = pair.broker.grants[handle["id"]]
            assert grant.session_id == SESSION and grant.owner[0] == pair.a.device_id

            # A command on the owner waits for the viewer's one-shot decision.
            with pair.a.active():
                running = asyncio.create_task(
                    pair.broker.run(SESSION, handle["id"], "command", {"command": "uptime"}))
            action = await pending(pair.broker)
            listed = (await ui.get(BASE)).json()
            assert [x["arguments"] for x in listed["actions"]] == [{"command": "uptime"}]
            assert [g["target"] for g in listed["grants"]] == [grant.target]
            response = await ui.post(f"{BASE}/actions/{action.id}", json={"allow": True})
            assert response.status_code == 200, response.text
            assert (await running)["stdout"] == "remote:uptime"
            assert commands == ["uptime"]
            # An answered approval cannot be replayed from the viewer.
            response = await ui.post(f"{BASE}/actions/{action.id}", json={"allow": True})
            assert response.status_code == 409

            response = await ui.delete(f"{BASE}/grants/{handle['id']}")
            assert response.status_code == 200, response.text
            assert not pair.broker.grants
            with pair.a.active():
                with pytest.raises(chat_access.AccessDenied):
                    await pair.broker.run(SESSION, handle["id"], "command", {"command": "uptime"})
            assert (await ui.get(BASE)).json()["grants"] == []
            assert commands == ["uptime"]

    # Every operation crossed as a control call; the password only over HTTPS, only to A.
    assert set(network.control_paths()) == {"/api/v1/network/peer/control/chat-access"}
    assert [base for base, _, body in network.seen if PASSWORD.encode() in body] == [A_URL]
    assert not [row for row in pair.b.sql("SELECT key FROM system_settings")
                if row["key"].startswith("chat_connection:")]


async def test_resumable_service_setup_routes_to_owner_with_retryable_and_terminal_statuses(pair, service, trust, monkeypatch):
    from app.core import chat_services

    network = Network(monkeypatch, {A_URL: pair.a})
    _reach(pair.b, pair.a, A_URL)
    with pair.a.active():
        prepared = await pair.broker.request(SESSION, "service", "Read inventory", service_provider="cloudflare")
    assert prepared["status"] == "awaiting_user" and not pair.broker.grants
    connection = {"kind": "service", "label": "Test", "url": spec(service).url, "secret": PASSWORD}
    with pair.b.active():
        async with network.ui() as ui:
            snapshot = (await ui.get(BASE)).json()
            card = snapshot["requests"][0]
            assert card["id"] == prepared["request_id"]
            assert card["suggested_url"] == chat_access.CLOUDFLARE_API_URL
            endpoint = f"{BASE}/requests/{card['id']}"
            real_verify = chat_services.verify_origin
            monkeypatch.setattr(chat_services, "verify_origin", AsyncMock(side_effect=chat_access.AccessDenied("Certificate could not be verified.")))
            failed = await ui.post(endpoint, json={"connection": connection})
            assert failed.status_code == 400 and PASSWORD not in failed.text
            assert not pair.broker.grants and card["id"] in pair.broker.requests
            monkeypatch.setattr(chat_services, "verify_origin", real_verify)
            granted = await ui.post(endpoint, json={"connection": connection})
            assert granted.status_code == 200 and granted.json()["granted"]
            assert not pair.broker.requests and len(pair.broker.grants) == 1
            replay = await ui.post(endpoint, json={"connection": connection})
            assert replay.status_code == 409 and PASSWORD not in replay.text
            grant_id = next(iter(pair.broker.grants))
            assert pair.broker.grants[grant_id].owner[0] == pair.a.device_id
            await ui.delete(f"{BASE}/grants/{grant_id}")
    assert [base for base, _, body in network.seen if PASSWORD.encode() in body] == [A_URL, A_URL, A_URL]
    assert service.requests == []
    assert not [row for row in pair.b.sql("SELECT key FROM system_settings")
                if row["key"].startswith("chat_connection:")]


async def test_private_inputs_never_cross_plain_http_but_answers_do(pair, ssh_target, monkeypatch):
    spec, commands, _ = ssh_target
    plain = "http://a.local:8000"
    network = Network(monkeypatch, {plain: pair.a})
    _reach(pair.b, pair.a, plain)
    requested, request_id = await _request(pair)
    with pair.b.active():
        async with network.ui() as ui:
            response = await ui.post(f"{BASE}/requests/{request_id}", json={"connection": _connection(spec)})
            assert response.status_code == 409 and response.json()["detail"] == routing.INSECURE_INPUTS
            assert not any(PASSWORD.encode() in body for _, _, body in network.seen)
            assert pair.broker.requests and not pair.broker.grants and not commands
            # Declining carries no private input, so the LAN route may carry it.
            response = await ui.delete(f"{BASE}/requests/{request_id}")
            assert response.status_code == 200, response.text
    with pytest.raises(chat_access.AccessDenied, match="declined"):
        await requested


async def test_owner_refuses_private_inputs_that_arrived_without_https(pair, ssh_target):
    spec, commands, _ = ssh_target
    requested, request_id = await _request(pair)
    body = {"op": "grant", "session_id": SESSION, "request_id": request_id}
    with pair.a.active():
        with pytest.raises(PermissionError, match="only over HTTPS"):
            await routing.handle({**body, "submission": {"connection": _connection(spec)}}, pair.b.device_id)
        with pytest.raises(ValueError) as invalid:
            await routing.handle({**body, "submission": {"password": PASSWORD}}, pair.b.device_id)
        assert PASSWORD not in str(invalid.value)
        assert not pair.broker.grants
        token = control.inbound_https.set(True)
        try:
            wrong = {**_connection(spec), "password": "wrong-" + PASSWORD}
            rejected = await routing.handle({**body, "submission": {"connection": wrong}}, pair.b.device_id)
            assert rejected["grant_error"]["status"] == 400
            assert rejected["grant_error"]["detail"].startswith("SSH sign-in was rejected")
            assert PASSWORD not in str(rejected) and not pair.broker.grants
            assert request_id in pair.broker.requests
            granted = await routing.handle({**body, "submission": {"connection": _connection(spec)}},
                                           pair.b.device_id)
        finally:
            control.inbound_https.reset(token)
    assert granted == {"granted": True, "saved": False}
    assert (await requested)["id"] in pair.broker.grants
    # The viewer holds only a mirror: it never serves the session's access itself.
    with pair.b.active():
        with pytest.raises(PermissionError):
            await routing.handle({"op": "snapshot", "session_id": SESSION}, pair.a.device_id)
    assert not commands


async def test_unreachable_owner_shows_nothing_and_refuses_changes(pair, monkeypatch):
    network = Network(monkeypatch, {})  # nobody answers
    _reach(pair.b, pair.a, A_URL)
    with pair.b.active():
        async with network.ui() as ui:
            response = await ui.get(BASE)
            assert response.status_code == 200
            assert response.json() == routing.EMPTY_SNAPSHOT
            response = await ui.post(f"{BASE}/actions/{'a' * 32}", json={"allow": True})
            assert response.status_code == 503


async def test_owner_push_refreshes_the_viewer_only_from_the_real_owner(pair, monkeypatch):
    Network(monkeypatch, {A_URL: pair.a, B_URL: pair.b})
    _reach(pair.b, pair.a, A_URL)
    _reach(pair.a, pair.b, B_URL)
    published = []

    async def publish(session_id, api_user_id, *, saved=False, pending=None):
        published.append((session_id, api_user_id, saved, pending))

    monkeypatch.setattr(chat_access, "publish_change", publish)
    monkeypatch.setattr(routing, "PUSH_DELAY", 0)
    # The owner's waiting count travels with the push, so the viewer's sidebar
    # can mark the mirrored session as needing the owner (#1807).
    monkeypatch.setattr(chat_access.broker, "pending_count", lambda session_id: 1)
    with pair.a.active():
        routing.watch(SESSION, pair.b.device_id)
        await pair.real_push((SESSION, pair.b.device_id), True)
    assert published == [(SESSION, None, True, 1)]
    assert (SESSION, pair.b.device_id) in routing._watchers
    # Only the device that runs the session may ask a viewer to refresh.
    with pair.b.active():
        with pytest.raises(PermissionError):
            await routing.handle_changed({"session_id": SESSION}, pair.b.device_id)
        with pytest.raises(LookupError):
            await routing.handle_changed({"session_id": "unknown"}, pair.a.device_id)
    assert published == [(SESSION, None, True, 1)]
    # An older owner sends no count, and a malformed one is dropped, never trusted.
    with pair.b.active():
        await routing.handle_changed({"session_id": SESSION}, pair.a.device_id)
        await routing.handle_changed({"session_id": SESSION, "pending": True}, pair.a.device_id)
        await routing.handle_changed({"session_id": SESSION, "pending": -1}, pair.a.device_id)
    assert published[1:] == [(SESSION, None, False, None)] * 3


async def test_undeliverable_push_forgets_the_watcher(pair, monkeypatch):
    Network(monkeypatch, {})
    _reach(pair.a, pair.b, B_URL)
    monkeypatch.setattr(routing, "PUSH_DELAY", 0)
    with pair.a.active():
        routing.watch(SESSION, pair.b.device_id)
        await pair.real_push((SESSION, pair.b.device_id), False)
    assert not routing._watchers


async def test_watchers_are_bounded_and_expire(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(routing.time, "monotonic", lambda: now[0])
    sent = []
    monkeypatch.setattr(routing, "_push", AsyncMock(side_effect=lambda key, saved: sent.append(key)))
    routing._watchers.clear()
    try:
        for index in range(routing.WATCH_LIMIT + 5):
            now[0] += 1
            routing.watch(f"s{index}", "viewer")
        assert len(routing._watchers) == routing.WATCH_LIMIT
        assert ("s0", "viewer") not in routing._watchers
        now[0] += routing.WATCH_SECONDS + 1
        routing.notify_watchers("s10")
        await asyncio.sleep(0)
        assert sent == [] and not routing._watchers  # expired watchers are dropped, not told
    finally:
        routing._watchers.clear()
        routing._scheduled.clear()
