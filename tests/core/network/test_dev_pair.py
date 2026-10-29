"""A dev-loop instance pairs only with the other instance its launcher started.

``python -m scripts.devloop up --pair`` gives both instances one pair key and
each the other's address (:mod:`app.network.dev_pair`). These run the real
invite flow between in-process workers: a dev instance must refuse a real
worker (or another launcher's instance) in either role, before a code is
spent or sent, and two instances of one launcher must still pair.
"""
import asyncio

import httpx
import pytest

from app.core import pairing as pairing_core
from app.network import dev_pair, engine, enrollment, tokens, transport
from tests.core.network import harness
from tests.core.network.harness import Worker
from tests.core.network.test_transport import ServedBy

KEY = "a" * 64
OTHER_KEY = "b" * 64
A_URL = "http://127.0.0.1:41001"


@pytest.fixture
def workers(tmp_path):
    made = []

    def make(name):
        worker = Worker(name, tmp_path)
        made.append(worker)
        return worker

    yield make
    for worker in made:
        worker.close()
    engine.reset_for_tests()
    tokens.reset_for_tests()
    transport.reset_for_tests()
    enrollment._task = None


@pytest.fixture
def dev(monkeypatch):
    """Mark workers as dev instances, each with its own pair key (by name)."""
    keys: dict[str, str] = {}

    def active_key() -> str | None:
        worker = harness._ACTIVE.get()
        return keys.get(worker.name) if worker is not None else None

    monkeypatch.setattr(dev_pair, "guarded", lambda: active_key() is not None)
    monkeypatch.setattr(dev_pair, "_key", lambda: (active_key() or "").encode() or None)
    monkeypatch.setenv(dev_pair.PEERS_ENV, A_URL)
    return keys


@pytest.fixture
def served(monkeypatch):
    """Route enrollment's HTTP to in-process worker ``a`` at A_URL."""
    monkeypatch.setattr(enrollment, "POLL_INTERVAL_SECONDS", 0.01)
    real_async, real_sync = httpx.AsyncClient, httpx.Client
    holder: dict[str, ServedBy] = {}

    def async_client(*args, **kwargs):
        kwargs["transport"] = holder["a"].transport()
        return real_async(**kwargs)

    def sync_client(*args, **kwargs):
        kwargs["transport"] = holder["a"].transport()
        return real_sync(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", async_client)
    monkeypatch.setattr(httpx, "Client", sync_client)

    def serve(worker: Worker) -> ServedBy:
        holder["a"] = ServedBy(worker, A_URL)
        return holder["a"]

    return serve


def _invite(worker: Worker) -> dict:
    with worker.active():
        engine.create_network("Dev", device_name=worker.name.upper())
        return pairing_core.create_grant(endpoint_url=A_URL, purpose="network:join")


async def _join(worker: Worker, grant: dict, endpoint: str = A_URL):
    with worker.active():
        started = await enrollment.start_enrollment(
            endpoint_url=endpoint, code=grant["code"],
            inviter_fingerprint=grant["join"]["fingerprint"], device_name=worker.name.upper())
        return started, enrollment._task


def test_real_workers_are_unaffected(monkeypatch):
    monkeypatch.delenv("AI_SHUFFLE_PREVIEW", raising=False)
    assert dev_pair.guarded() is False
    assert dev_pair.join_proof("ABCDEFGHJKMN") is None
    assert dev_pair.admission_proof("ABCDEFGHJKMN", "join_1") is None
    dev_pair.check_join_target("http://10.0.0.5:8000")
    dev_pair.check_join("ABCDEFGHJKMN", None)
    dev_pair.check_admission("ABCDEFGHJKMN", "join_1", None)


def test_a_dev_instance_without_a_pair_refuses_every_join(monkeypatch):
    monkeypatch.setattr(dev_pair, "guarded", lambda: True)
    monkeypatch.delenv(dev_pair.KEY_ENV, raising=False)
    with pytest.raises(dev_pair.DevPairRefused, match="without a pair"):
        dev_pair.check_join_target(A_URL)
    with pytest.raises(dev_pair.DevPairRefused, match="without a pair"):
        dev_pair.check_join("ABCDEFGHJKMN", "anything")
    monkeypatch.setenv(dev_pair.KEY_ENV, "short")
    with pytest.raises(dev_pair.DevPairRefused):
        dev_pair.join_proof("ABCDEFGHJKMN")


def test_dev_joiner_never_sends_its_code_to_an_address_outside_its_pair(workers, dev, served):
    a, b = workers("a"), workers("b")
    dev["b"] = KEY
    sent = served(a)
    grant = _invite(a)
    with pytest.raises(enrollment.EnrollmentError, match="not that instance"):
        asyncio.run(_join(b, grant, endpoint="http://10.0.0.251:8000"))
    assert sent.requests == []  # refused before anything was sent


def test_dev_inviter_refuses_a_real_worker_without_spending_the_code(workers, dev, served):
    a, real = workers("a"), workers("real")
    dev["a"] = KEY  # only the inviter is a dev instance
    served(a)
    grant = _invite(a)
    with pytest.raises(enrollment.EnrollmentError, match="joining worker is not that instance"):
        asyncio.run(_join(real, grant))
    with a.active():
        assert [m.device_id for m in engine.context().members.values()] == [a.device_id]
    # The code was not consumed: the launcher's own instance can still use it.
    dev["real"] = KEY
    started, poller = asyncio.run(_join(real, grant))
    assert started["status"] == "pending"


def test_dev_joiner_refuses_a_real_inviter(workers, dev, served):
    real, b = workers("real"), workers("b")
    dev["b"] = KEY  # the inviter is a real worker at a peer address
    served(real)
    grant = _invite(real)
    with pytest.raises(enrollment.EnrollmentError, match="worker that answered is not that instance"):
        asyncio.run(_join(b, grant))
    with b.active():
        assert engine.context() is None
        assert enrollment.enrollment_status() is None


def test_instances_of_another_launcher_refuse_each_other(workers, dev, served):
    a, b = workers("a"), workers("b")
    dev["a"], dev["b"] = KEY, OTHER_KEY
    served(a)
    grant = _invite(a)
    with pytest.raises(enrollment.EnrollmentError, match="not that instance"):
        asyncio.run(_join(b, grant))
    with a.active():
        assert b.device_id not in engine.context().members


def test_two_instances_of_one_launcher_pair(workers, dev, served):
    a, b = workers("a"), workers("b")
    dev["a"] = dev["b"] = KEY
    served(a)
    grant = _invite(a)

    async def join_and_wait():
        started, poller = await _join(b, grant)
        with b.active():
            await asyncio.wait_for(poller, timeout=10)
        return started

    started = asyncio.run(join_and_wait())
    assert started["inviter"]["device_id"] == a.device_id
    with a.active():
        assert engine.context().member(b.device_id).status == "active"
    with b.active():
        assert enrollment.enrollment_status()["status"] == "complete"
        assert engine.context().bootstrap_state == "ready"
