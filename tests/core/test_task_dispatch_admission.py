"""Task dispatch decides pool admission BEFORE it answers (2026-09-04 audit).

Two sessions fanned out at once; seven ``Task`` calls each returned a
background receipt ("End the turn now… you will be woken"), and 0.3 seconds
later each came back as "worker failed to start: Server is at capacity —
session pool at hard cap (8/8)". The parent then re-dispatched and failed the
same way. The receipt is a promise the parent will be woken, and a worker that
never started keeps no promises, so admission is now decided synchronously in
the dispatch path: brief wait for a slot, then a real ``ok=False`` error.
"""

import asyncio
from unittest.mock import patch

import pytest

import app.core.task_dispatch as td
from app.core.provider_agent import WorkerOutcome
from app.core.session_pool import AdmissionSnapshot


@pytest.fixture()
def agents(mock_db):
    from app.db import database

    database.create_profile("root-agent", "Root Agent", None, {"model": "opus"})
    database.create_profile("helper", "Helper", None, {"model": "sonnet"})
    database.create_session(
        session_id="s-root", profile_id="root-agent", title="root"
    )
    return database


@pytest.fixture(autouse=True)
def _clean_admission_state():
    td._PENDING_ADMISSIONS.clear()
    td._ADMISSION_LOCK = None
    yield
    td._PENDING_ADMISSIONS.clear()
    td._ADMISSION_LOCK = None


class _FakePool:
    """A pool whose occupancy the test drives directly.

    ``peek`` answers "is this session really pooled yet" — the signal that
    retires an admission reservation — and stays empty here, so a reservation
    only ever ages out or is released by the dispatch itself.
    """

    def __init__(self, in_pool=0, hard_cap=8, refusal=None):
        self.in_pool = in_pool
        self.hard_cap = hard_cap
        self.refusal = refusal
        self.snapshots = 0

    def admission_snapshot(self):
        self.snapshots += 1
        return AdmissionSnapshot(
            refusal=self.refusal, in_pool=self.in_pool, hard_cap=self.hard_cap
        )

    def peek(self, session_id):
        return None


def _install_pool(monkeypatch, pool):
    from app.core import session_pool

    monkeypatch.setattr(session_pool, "_session_pool", pool, raising=False)
    return pool


async def _dispatch(args, session_id="s-root"):
    return await td.dispatch_task_tool(args, caller_session_id=session_id)


# ── at the ceiling: a real error, never a receipt ────────────────────────────


async def test_background_dispatch_at_hard_cap_is_refused(agents, monkeypatch):
    _install_pool(monkeypatch, _FakePool(in_pool=8, hard_cap=8))
    monkeypatch.setattr(td, "_ADMISSION_WAIT_SECONDS", 0.05)
    started = []

    async def fake_worker(**kwargs):
        started.append(kwargs)
        return WorkerOutcome(ok=True, session_id=kwargs["worker_session_id"])

    with patch("app.core.provider_agent.dispatch_worker", side_effect=fake_worker):
        payload = await _dispatch(
            {"agent": "helper", "prompt": "go", "mode": "background"}
        )

    assert payload["ok"] is False
    assert payload["reason"] == "capacity"
    assert payload["retryable"] is True
    assert "8/8" in payload["error"]
    assert "NOT" in payload["error"]
    assert "task_id" not in payload and "note" not in payload
    assert started == [], "no worker may be launched at the ceiling"
    assert td._PENDING_ADMISSIONS == {}


async def test_sync_dispatch_at_hard_cap_is_refused(agents, monkeypatch):
    _install_pool(monkeypatch, _FakePool(in_pool=8, hard_cap=8))
    monkeypatch.setattr(td, "_ADMISSION_WAIT_SECONDS", 0.05)
    started = []

    async def fake_worker(**kwargs):
        started.append(kwargs)
        return WorkerOutcome(ok=True, session_id="w", report="done")

    with patch("app.core.provider_agent.dispatch_worker", side_effect=fake_worker):
        payload = await _dispatch({"agent": "helper", "prompt": "go", "mode": "sync"})

    assert payload["ok"] is False and payload["reason"] == "capacity"
    assert started == []


async def test_non_pool_refusal_is_surfaced_verbatim(agents, monkeypatch):
    """A memory/process ceiling refuses with its own reason, not a cap count."""
    _install_pool(
        monkeypatch,
        _FakePool(in_pool=1, hard_cap=8, refusal="host low on memory: 400MB available"),
    )
    monkeypatch.setattr(td, "_ADMISSION_WAIT_SECONDS", 0.05)

    with patch("app.core.provider_agent.dispatch_worker"):
        payload = await _dispatch({"agent": "helper", "prompt": "go"})

    assert payload["ok"] is False
    assert "host low on memory: 400MB available" in payload["error"]


# ── a transient cap costs seconds, not a failed dispatch ─────────────────────


async def test_dispatch_waits_for_a_slot_and_then_starts(agents, monkeypatch):
    pool = _install_pool(monkeypatch, _FakePool(in_pool=8, hard_cap=8))
    monkeypatch.setattr(td, "_ADMISSION_WAIT_SECONDS", 5.0)
    monkeypatch.setattr(td, "_ADMISSION_POLL_SECONDS", 0.01)

    async def free_a_slot():
        await asyncio.sleep(0.05)
        pool.in_pool = 7

    async def fake_worker(**kwargs):
        return WorkerOutcome(ok=True, session_id=kwargs["worker_session_id"],
                             report="done")

    freeing = asyncio.create_task(free_a_slot())
    with patch("app.core.provider_agent.dispatch_worker", side_effect=fake_worker):
        payload = await _dispatch({"agent": "helper", "prompt": "go", "mode": "sync"})
    await freeing

    assert payload["ok"] is True
    assert pool.snapshots > 1, "the dispatch never re-checked while waiting"


# ── the reservation is what stops a fan-out overshooting the cap ─────────────


async def test_concurrent_dispatches_do_not_all_take_the_same_slot(
    agents, monkeypatch
):
    """One free slot, two simultaneous dispatches: one starts, one is refused.

    Without the reservation both see ``in_pool < hard_cap`` (a worker joins the
    pool only when its turn connects, long after this check) and both return a
    receipt — which is exactly how seven receipts turned into seven capacity
    failures.
    """
    _install_pool(monkeypatch, _FakePool(in_pool=7, hard_cap=8))
    monkeypatch.setattr(td, "_ADMISSION_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(td, "_ADMISSION_POLL_SECONDS", 0.01)
    running = asyncio.Event()

    async def fake_worker(**kwargs):
        await running.wait()
        return WorkerOutcome(ok=True, session_id=kwargs["worker_session_id"])

    with patch("app.core.provider_agent.dispatch_worker", side_effect=fake_worker), \
         patch.object(td, "_await_worker_registration", return_value=None):
        first, second = await asyncio.gather(
            _dispatch({"agent": "helper", "prompt": "a", "mode": "background"}),
            _dispatch({"agent": "helper", "prompt": "b", "mode": "background"}),
        )
    running.set()
    await asyncio.sleep(0)

    outcomes = sorted([bool(first["ok"]), bool(second["ok"])])
    assert outcomes == [False, True], (first, second)
    refused = first if not first["ok"] else second
    assert refused["reason"] == "capacity"
    assert "already admitted" in refused["error"]


async def test_reservation_is_released_when_a_dispatch_dies(agents, monkeypatch):
    """A worker that never reaches the pool must not hold its slot forever."""
    _install_pool(monkeypatch, _FakePool(in_pool=0, hard_cap=8))

    async def fake_worker(**kwargs):
        return WorkerOutcome(
            ok=False, session_id=kwargs["worker_session_id"],
            error="could not create worker",
        )

    with patch("app.core.provider_agent.dispatch_worker", side_effect=fake_worker):
        payload = await _dispatch(
            {"agent": "helper", "prompt": "go", "mode": "background"}
        )

    assert payload["ok"] is False
    for _ in range(50):
        await asyncio.sleep(0.01)
        if not td._PENDING_ADMISSIONS:
            break
    assert td._PENDING_ADMISSIONS == {}


async def test_sync_dispatch_releases_its_reservation(agents, monkeypatch):
    _install_pool(monkeypatch, _FakePool(in_pool=0, hard_cap=8))

    async def fake_worker(**kwargs):
        assert td._PENDING_ADMISSIONS, "the slot must be held while starting"
        return WorkerOutcome(ok=True, session_id=kwargs["worker_session_id"],
                             report="done")

    with patch("app.core.provider_agent.dispatch_worker", side_effect=fake_worker):
        payload = await _dispatch({"agent": "helper", "prompt": "go", "mode": "sync"})

    assert payload["ok"] is True
    assert td._PENDING_ADMISSIONS == {}


async def test_expired_reservations_stop_counting(monkeypatch):
    td._PENDING_ADMISSIONS["stale"] = 0.0  # monotonic 0 is always in the past
    assert td._pending_admission_count() == 0
    assert td._PENDING_ADMISSIONS == {}


async def test_pooled_worker_stops_holding_its_reservation(monkeypatch):
    class _Pooled(_FakePool):
        def peek(self, session_id):
            return object()

    _install_pool(monkeypatch, _Pooled())
    td._PENDING_ADMISSIONS["w-live"] = float("inf")
    assert td._pending_admission_count() == 0


# ── an unconfigured / empty pool never blocks a dispatch ─────────────────────


async def test_no_pool_yet_admits_immediately(agents, monkeypatch):
    from app.core import session_pool

    monkeypatch.setattr(session_pool, "_session_pool", None, raising=False)

    async def fake_worker(**kwargs):
        return WorkerOutcome(ok=True, session_id=kwargs["worker_session_id"],
                             report="done")

    with patch("app.core.provider_agent.dispatch_worker", side_effect=fake_worker):
        payload = await _dispatch({"agent": "helper", "prompt": "go", "mode": "sync"})

    assert payload["ok"] is True
