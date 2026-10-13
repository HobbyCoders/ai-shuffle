"""Count upstream requests from independent worker databases through the peer API."""

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import network_usage
from app.core import usage_polling as polling
from app.network import engine, tokens, transport, usage_budget
from tests.pg_shim import ShimConnection, close_connection, new_sqlite_connection

URL = "https://provider.test/usage"
HEADERS = {"Authorization": "Bearer secret-shared-between-workers"}
KEY = polling.identity(URL, HEADERS)


@pytest.fixture
def cluster(monkeypatch):
    device = ContextVar("usage_test_device", default="founder")
    members = {name: SimpleNamespace(status="active", kind="worker")
               for name in ("founder", "first", "second", "consumer")}
    members["consumer"].kind = "consumer"
    contexts = {name: SimpleNamespace(
        network_id="test-network", generation=1, device_id=name,
        founder_device_id="founder", member=members.get,
    ) for name in members}
    databases = {name: new_sqlite_connection() for name in contexts}
    now = [1000.0]
    upstream, peer_requests = [], []
    mode = {"status": 200, "offline": False, "cancel": False, "lose_finish": False,
            "lose_claim": False, "redirect": False}
    monkeypatch.setattr(polling.time, "time", lambda: now[0])
    monkeypatch.setattr(polling.random, "uniform", lambda *_: 0)
    monkeypatch.setattr(engine, "context", lambda **_: contexts[device.get()])
    monkeypatch.setattr(usage_budget, "coordinator", lambda: contexts[device.get()])
    monkeypatch.setattr(transport, "note_contact", lambda *_: None)

    @contextmanager
    def database():
        shim = ShimConnection(databases[device.get()])
        yield shim
        shim.commit()

    monkeypatch.setattr(polling, "get_db", database)
    app = FastAPI()
    app.include_router(network_usage.router, prefix="/api/v1/network")
    client = TestClient(app)
    original_client = httpx.AsyncClient

    def authorization(name):
        return {"Authorization": "Bearer " + tokens._mint(name, 100000, tokens._scope(contexts[name]))}

    async def link_for(_owner):
        ctx = contexts[device.get()]
        link = transport.PeerLink("founder", ["http://budget.test"])
        link.base_url = "http://budget.test"
        link.token = authorization(ctx.device_id)["Authorization"][7:]
        link.token_expires = 100000
        return ctx, link

    monkeypatch.setattr(usage_budget, "peer_link", link_for)

    async def dispatch(request):
        if request.url.host == "budget.test":
            peer_requests.append(request)
            if mode["redirect"]:
                return httpx.Response(307, headers={"Location": "https://unexpected.test"}, json={"lease": "f" * 32})
            if mode["offline"] or (mode["lose_finish"] and "/finish/" in request.url.path):
                raise httpx.ConnectError("offline", request=request)
            caller = device.set("founder")
            try:
                result = client.request(request.method, request.url.path,
                                        headers=dict(request.headers), content=request.content)
                if mode["lose_claim"] and "/claim/" in request.url.path:
                    mode["lose_claim"] = False
                    raise httpx.ReadError("lost after commit", request=request)
                return httpx.Response(result.status_code, content=result.content, headers=result.headers)
            finally:
                device.reset(caller)
        assert request.url.host in ("provider.test", "api.anthropic.com")
        upstream.append(request)
        await asyncio.sleep(0)
        if mode["cancel"]:
            raise asyncio.CancelledError()
        return httpx.Response(mode["status"], json={"usage": 42},
                              headers={"Retry-After": "1200"} if mode["status"] != 200 else {})

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original_client(
        **kwargs, transport=httpx.MockTransport(dispatch)))

    async def fetch(name, headers=HEADERS, interval=300):
        caller = device.set(name)
        try:
            return await polling.usage_get(URL, headers=headers, interval=interval)
        finally:
            device.reset(caller)

    yield SimpleNamespace(fetch=fetch, upstream=upstream, requests=peer_requests,
                          mode=mode, now=now, db=databases, device=device,
                          client=client, authorization=authorization, contexts=contexts)
    client.close()
    for raw in databases.values():
        close_connection(raw)


@pytest.mark.asyncio
async def test_workers_with_separate_databases_share_one_budget(cluster):
    results = await asyncio.gather(*(cluster.fetch(name) for name in ["first", "second", "founder"] * 4),
                                   return_exceptions=True)
    assert sum(isinstance(result, httpx.Response) for result in results) == 1
    assert sum(isinstance(result, polling.UsageDeferred) for result in results) == 11
    assert len(cluster.upstream) == 1
    for name in ("first", "second"):
        assert cluster.db[name].execute("SELECT COUNT(*) FROM usage_poll_state").fetchone()[0] == 0
    for request in cluster.requests:
        assert HEADERS["Authorization"] not in request.content.decode()
        assert "provider.test" not in str(request.url)
    cluster.now[0] += 300
    assert (await cluster.fetch("second")).status_code == 200
    assert len(cluster.upstream) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 429, 500])
async def test_failures_and_restart_remain_shared(cluster, status):
    cluster.mode["status"] = status
    assert (await cluster.fetch("first")).status_code == status
    # Each call constructs a new peer link/client: no process-local cooldown.
    cluster.now[0] += 1199
    with pytest.raises(polling.UsageDeferred):
        await cluster.fetch("second")
    assert len(cluster.upstream) == 1
    cluster.now[0] += 1
    if status in (401, 403):
        with pytest.raises(polling.UsageDeferred):
            await cluster.fetch("second")
        assert (await cluster.fetch("second", {"Authorization": "Bearer changed"})).status_code == status
    else:
        assert (await cluster.fetch("second")).status_code == status
    assert len(cluster.upstream) == 2


@pytest.mark.asyncio
async def test_offline_authority_never_falls_back_to_local_poll(cluster):
    cluster.mode["offline"] = True
    with pytest.raises(polling.UsageDeferred):
        await cluster.fetch("first")
    assert not cluster.upstream
    assert cluster.db["first"].execute("SELECT COUNT(*) FROM usage_poll_state").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_uncertain_claim_is_not_replayed_and_redirect_is_rejected(cluster):
    cluster.mode["lose_claim"] = True
    with pytest.raises(polling.UsageDeferred):
        await cluster.fetch("first")
    assert len(cluster.requests) == 1
    with pytest.raises(polling.UsageDeferred):
        await cluster.fetch("second")
    cluster.mode["redirect"] = True
    with pytest.raises(polling.UsageDeferred):
        await cluster.fetch("second", {"Authorization": "Bearer new-key"})
    assert not cluster.upstream


@pytest.mark.asyncio
async def test_lost_completion_keeps_lease_and_cancellation_backs_off(cluster):
    cluster.mode["lose_finish"] = True
    assert (await cluster.fetch("first")).status_code == 200
    cluster.now[0] += 599
    with pytest.raises(polling.UsageDeferred):
        await cluster.fetch("second")
    cluster.now[0] += 1
    cluster.mode.update(lose_finish=False, cancel=True)
    with pytest.raises(asyncio.CancelledError):
        await cluster.fetch("second")
    cluster.mode["cancel"] = False
    with pytest.raises(polling.UsageDeferred):
        await cluster.fetch("first")
    assert len(cluster.upstream) == 2


@pytest.mark.asyncio
async def test_lost_completion_cannot_shorten_longer_source_budget(cluster):
    cluster.mode["lose_finish"] = True
    await cluster.fetch("first", interval=900)
    cluster.now[0] += 929
    with pytest.raises(polling.UsageDeferred):
        await cluster.fetch("second", interval=900)
    cluster.now[0] += 1
    await cluster.fetch("second", interval=900)
    assert len(cluster.upstream) == 2


def test_peer_authorization_validation_and_lease_fencing(cluster):
    path = f"{usage_budget.PREFIX}/claim/{KEY}"
    assert cluster.client.post(path).status_code == 401
    assert cluster.client.post(path, headers=cluster.authorization("consumer")).status_code == 403
    caller = cluster.device.set("second")
    try:
        assert cluster.client.post(path, headers=cluster.authorization("first")).status_code == 409
    finally:
        cluster.device.reset(caller)
    headers = cluster.authorization("first")
    assert cluster.client.post(path.replace(KEY, "invalid"), headers=headers).status_code == 422
    old = cluster.client.post(path, headers=headers).json()["lease"]
    cluster.now[0] += 600
    new = cluster.client.post(path, headers=headers).json()["lease"]
    assert old != new
    finish_path = f"{usage_budget.PREFIX}/finish/{KEY}"
    assert cluster.client.post(finish_path, headers=headers, json={"lease": old, "status": 403}).status_code == 200
    row = cluster.db["founder"].execute("SELECT * FROM usage_poll_state").fetchone()
    assert row["lease_token"] == new and not row["paused"]
    assert cluster.client.post(finish_path, headers=headers, json={"lease": new, "status": 200, "interval": 0}).status_code == 422
    assert cluster.client.post(finish_path, headers=headers, json={"lease": new, "status": 200}).status_code == 200
    # Replaying a completion cannot overwrite a successfully finished attempt.
    cluster.client.post(finish_path, headers=headers, json={"lease": new, "status": 403})
    assert not cluster.db["founder"].execute("SELECT paused FROM usage_poll_state").fetchone()[0]


@pytest.mark.asyncio
async def test_explicit_resume_reaches_authority_without_bypassing_success_cooldown(cluster):
    cluster.mode["status"] = 403
    await cluster.fetch("first")
    caller = cluster.device.set("second")
    try:
        await usage_budget.resume_authentication(URL, HEADERS)
        cluster.mode["status"] = 200
        await polling.usage_get(URL, headers=HEADERS)
        await usage_budget.resume_authentication(URL, HEADERS)
        with pytest.raises(polling.UsageDeferred):
            await polling.usage_get(URL, headers=HEADERS)
    finally:
        cluster.device.reset(caller)
    assert len(cluster.upstream) == 2


