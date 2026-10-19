"""Which addresses a control call tries, and when it may try another.

A control call may move to the owner's next address only while nothing has
reached the owner: authentication failed, or the connection for the action
could not be opened. Once the action may have been sent, a timeout or dropped
connection is final.
"""
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core import turn_manager
from app.db import database
from app.db import network as store
from app.network import control, engine, transport
from tests.core.network.harness import converge, join
from tests.core.network.test_control import _new_session
from tests.core.network.test_transport import _app

DEAD = "https://dead.a.example"
TUNNEL = "https://tunnel.a.example"
SECOND = "https://second.a.example"
INTERRUPT = "/api/v1/network/peer/control/interrupt"
CHALLENGE = "/api/v1/network/peer/auth/challenge"


def refuse(owner, request):
    raise httpx.ConnectError("[Errno 111] Connection refused", request=request)


def sent_then_timed_out(owner, request):
    owner.forward(request)  # the owner applied it; the answer never arrived
    raise httpx.ReadTimeout("timed out", request=request)


def sent_then_dropped(owner, request):
    owner.forward(request)
    raise httpx.RemoteProtocolError("Server disconnected without sending a response.", request=request)


def tunnel_without_worker(owner, request):
    return httpx.Response(530)


class Owner:
    """Answers as ``worker`` at every address unless a rule fails that address."""

    def __init__(self, worker):
        self.worker = worker
        self.client = TestClient(_app())
        self.log = []
        self.rules = {}
        self.timeouts = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        base = f"{request.url.scheme}://{request.url.host}"
        self.log.append((base, request.url.path))
        self.timeouts.append((request.url.path, request.extensions.get("timeout")))
        rule = self.rules.get((base, request.url.path)) or self.rules.get(base)
        if rule is not None:
            return rule(self, request)
        return self.forward(request)

    def forward(self, request: httpx.Request) -> httpx.Response:
        # The simulated worker owns these configured listeners; request
        # headers must not determine which endpoint bindings it accepts.
        with self.worker.active(), patch.object(engine, "_own_endpoints", return_value=[DEAD, TUNNEL, SECOND]):
            response = self.client.request(request.method, request.url.path, content=request.content,
                                           headers={k: v for k, v in request.headers.items()
                                                    if k.lower() in ("authorization", "content-type", "accept")})
        return httpx.Response(response.status_code, content=response.content,
                              headers={"content-type": response.headers.get("content-type", "application/json")})

    def actions(self):
        return [base for base, path in self.log if path == INTERRUPT]


@pytest.fixture
def network(workers, monkeypatch):
    a, b = workers("a"), workers("b")
    owner = Owner(a)
    real_async = httpx.AsyncClient

    def async_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(owner.handler)
        return real_async(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", async_client)
    monkeypatch.setattr(control, "CONNECT_RETRY_DELAY", 0)
    interrupt = AsyncMock(return_value={"id": "turn_1"})
    monkeypatch.setattr(turn_manager, "interrupt_async", interrupt)
    with a.active():
        engine.create_network("Home", device_name="A")
    join(a, b)
    _new_session(a, "s-routes", "On A")
    converge([a, b])
    return a, b, owner, interrupt


def _link(b, a, endpoints):
    with b.active():
        link = transport.PeerLink(a.device_id, endpoints)
        transport._links[a.device_id] = link
        return link


async def test_connect_failure_falls_through_to_the_next_route_and_that_route_is_preferred(network):
    a, b, owner, interrupt = network
    owner.rules[DEAD] = refuse
    link = _link(b, a, [DEAD, TUNNEL])
    with b.active():
        assert (await control.interrupt(a.device_id, "s-routes", "stop"))["interrupted"] is True
        assert owner.log[0] == (DEAD, CHALLENGE)
        assert owner.actions() == [TUNNEL]
        assert link.base_url == TUNNEL and link.routes() == [TUNNEL, DEAD]
        owner.log.clear()
        await control.interrupt(a.device_id, "s-routes", "stop")
        # The route that worked is tried first; the dead one is not dialed at all.
        assert {base for base, _ in owner.log} == {TUNNEL}
    assert interrupt.await_count == 2


async def test_connection_refused_for_the_action_itself_moves_to_the_next_route(network):
    a, b, owner, interrupt = network
    owner.rules[(TUNNEL, INTERRUPT)] = refuse  # authentication there works, the action cannot connect
    link = _link(b, a, [TUNNEL, SECOND])
    with b.active():
        assert (await control.interrupt(a.device_id, "s-routes", "stop"))["interrupted"] is True
    assert owner.actions() == [TUNNEL, SECOND]
    assert link.base_url == SECOND
    interrupt.assert_awaited_once()


@pytest.mark.parametrize("rule, wording", [
    (sent_then_timed_out, "did not answer in time"),
    (sent_then_dropped, "dropped the connection"),
])
async def test_action_that_may_have_been_sent_is_never_resent(network, rule, wording):
    a, b, owner, interrupt = network
    owner.rules[(TUNNEL, INTERRUPT)] = rule
    _link(b, a, [TUNNEL, SECOND])
    with b.active():
        with pytest.raises(control.RunnerUnreachable) as raised:
            await control.interrupt(a.device_id, "s-routes", "stop")
    assert owner.actions() == [TUNNEL]
    assert SECOND not in {base for base, _ in owner.log}
    interrupt.assert_awaited_once()  # applied once by the owner, not repeated
    assert str(raised.value) == (f"A {wording} while handling a request from b. "
                                 "It may still have been applied; check before retrying.")


async def test_refused_on_every_route_retries_once_then_names_both_workers(network):
    a, b, owner, interrupt = network
    owner.rules[DEAD] = refuse
    owner.rules[TUNNEL] = refuse
    link = _link(b, a, [DEAD, TUNNEL])
    with b.active():
        with pytest.raises(control.RunnerUnreachable) as raised:
            await control.interrupt(a.device_id, "s-routes", "stop")
    assert str(raised.value) == "Couldn't reach A from b (connection refused on all 2 routes)."
    assert owner.log == [(DEAD, CHALLENGE), (TUNNEL, CHALLENGE)] * control.CONNECT_ROUNDS
    interrupt.assert_not_awaited()
    assert link.routes() == [DEAD, TUNNEL]


async def test_transient_connect_failure_recovers_on_the_second_round(network):
    a, b, owner, interrupt = network
    attempts = []

    def flaky(owner_, request):
        attempts.append(request.url.path)
        if len(attempts) == 1:
            refuse(owner_, request)
        return owner_.forward(request)

    owner.rules[TUNNEL] = flaky
    _link(b, a, [TUNNEL])
    with b.active():
        assert (await control.interrupt(a.device_id, "s-routes", "stop"))["interrupted"] is True
    interrupt.assert_awaited_once()


async def test_tunnel_that_answers_without_its_worker_is_not_retried(network):
    a, b, owner, interrupt = network
    owner.rules[TUNNEL] = tunnel_without_worker
    _link(b, a, [TUNNEL])
    with b.active():
        with pytest.raises(control.RunnerUnreachable) as raised:
            await control.interrupt(a.device_id, "s-routes", "stop")
    assert str(raised.value) == "Couldn't reach A from b (HTTP 530 on its only route)."
    assert owner.log == [(TUNNEL, CHALLENGE)]


async def test_owner_refusal_of_the_action_is_still_a_refusal(network):
    a, b, owner, interrupt = network
    _link(b, a, [TUNNEL, SECOND])
    with b.active():
        with pytest.raises(control.ControlRefused):
            await control.interrupt(a.device_id, "not-owned-anywhere", "stop")
    assert owner.actions() == [TUNNEL]


async def test_long_action_timeout_does_not_stretch_the_connect_timeout(network):
    a, b, owner, interrupt = network
    _link(b, a, [TUNNEL])
    with b.active():
        await control.call(a.device_id, "interrupt", {"session_id": "s-routes", "mode": "stop"}, timeout=600)
    action = [t for path, t in owner.timeouts if path == INTERRUPT][-1]
    assert action["connect"] == control.CONNECT_TIMEOUT and action["read"] == 600
    assert transport.connect_timeout.get() is None  # restored for the caller


async def test_authenticating_at_another_route_leaves_the_working_one_alone(network):
    a, b, owner, interrupt = network
    owner.rules[DEAD] = refuse
    link = _link(b, a, [TUNNEL, DEAD])
    with b.active():
        ctx = engine.require_context()
        async with httpx.AsyncClient() as client:
            await link.authenticate(client, ctx)
            token = link.token
            with pytest.raises(transport.PeerConnectFailed):
                await link.authenticate(client, ctx, at=DEAD)
    assert (link.base_url, link.token) == (TUNNEL, token)


async def test_peer_link_reports_connect_failure_only_when_nothing_answered(network):
    a, b, owner, interrupt = network
    owner.rules[DEAD] = refuse
    owner.rules[TUNNEL] = tunnel_without_worker
    link = _link(b, a, [DEAD, TUNNEL])
    with b.active():
        ctx = engine.require_context()
        async with httpx.AsyncClient() as client:
            with pytest.raises(transport.PeerError) as raised:
                await link.authenticate(client, ctx)
    assert not isinstance(raised.value, transport.PeerConnectFailed)
    assert link.base_url is None and link.token is None


@pytest.mark.parametrize("error, reason", [
    (httpx.ConnectError("[Errno -3] Temporary failure in name resolution"), "address lookup failed"),
    (httpx.ConnectError("[Errno -2] Name or service not known"), "address lookup failed"),
    (httpx.ConnectError("[Errno 111] Connection refused"), "connection refused"),
    (httpx.ConnectError("[WinError 10061] No connection could be made"), "connection refused"),
    (httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed"), "TLS handshake failed"),
    (httpx.ConnectError("All connection attempts failed"), "connection failed"),
    (httpx.ConnectTimeout("timed out"), "connection timed out"),
    (transport.PeerError("HTTP 502 Bad Gateway"), "HTTP 502"),
])
def test_route_failure_reasons_are_plain_words(error, reason):
    assert control._route_reason(error) == reason
    wrapped = transport.PeerConnectFailed(f"{type(error).__name__}: {error}")
    wrapped.__cause__ = error
    assert control._route_reason(wrapped) == reason


def test_unreachable_message_lists_distinct_reasons():
    failures = [httpx.ConnectTimeout("t"), transport.PeerError("HTTP 530"), httpx.ConnectTimeout("t")]
    assert (control._unreachable_message("DesktopWorker", "ServerWorker", failures, 3)
            == "Couldn't reach DesktopWorker from ServerWorker (connection timed out / HTTP 530 on all 3 routes).")


async def test_unknown_names_fall_back_to_neutral_words(network):
    a, b, owner, interrupt = network
    with b.active():
        ctx = engine.require_context()
        assert control._names(ctx, a.device_id) == ("A", "b")
        assert control._names(ctx, "dev_unknown")[0] == "the other worker"
        assert store.get_member(a.device_id)["name"] == "A"
        assert database.get_session("s-routes") is not None
