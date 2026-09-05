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

    ``in_pool`` is the DELEGATED (subprocess) occupancy — the number the hard
    cap actually counts. Native occupancy is passed separately because it gates
    nothing: a native dispatch is refused only by ``refusal`` (the real resource
    guards), which is what ``admission_check`` would have returned for that lane.

    ``peek`` answers "is this session really pooled yet" — the signal that
    retires an admission reservation — and stays empty here, so a reservation
    only ever ages out or is released by the dispatch itself.
    """

    def __init__(self, in_pool=0, hard_cap=8, refusal=None, native_in_pool=0,
                 native_refusal=None):
        self.in_pool = in_pool
        self.hard_cap = hard_cap
        self.refusal = refusal
        self.native_in_pool = native_in_pool
        self.native_refusal = native_refusal
        self.snapshots = 0
        self.lanes = []

    def admission_snapshot(self, lane="delegated"):
        self.snapshots += 1
        self.lanes.append(lane)
        native = lane == "native"
        return AdmissionSnapshot(
            refusal=self.native_refusal if native else self.refusal,
            in_pool=self.in_pool + self.native_in_pool,
            hard_cap=self.hard_cap,
            delegated_in_pool=self.in_pool,
            native_in_pool=self.native_in_pool,
            lane=lane,
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
    # monotonic 0 is always in the past
    td._PENDING_ADMISSIONS["stale"] = td._Reservation(expires=0.0)
    assert td._pending_admission_count() == 0
    assert td._PENDING_ADMISSIONS == {}


async def test_pooled_worker_stops_holding_its_reservation(monkeypatch):
    class _Pooled(_FakePool):
        def peek(self, session_id):
            return object()

    _install_pool(monkeypatch, _Pooled())
    td._PENDING_ADMISSIONS["w-live"] = td._Reservation(expires=float("inf"))
    assert td._pending_admission_count() == 0


async def test_reservations_are_counted_per_lane_and_per_parent(monkeypatch):
    """A native reservation holds no subprocess slot; a parent's holds its own."""
    _install_pool(monkeypatch, _FakePool())
    far = float("inf")
    td._PENDING_ADMISSIONS.update(
        {
            "d-1": td._Reservation(expires=far, lane="delegated", parent="s-root"),
            "n-1": td._Reservation(expires=far, lane="native", parent="s-root"),
            "n-2": td._Reservation(expires=far, lane="native", parent="s-other"),
        }
    )

    assert td._pending_admission_count() == 3
    assert td._pending_admission_count(lane="delegated") == 1
    assert td._pending_admission_count(lane="native") == 2
    assert td._pending_admission_count(parent="s-root") == 2


# ── an unconfigured / empty pool never blocks a dispatch ─────────────────────


# ── lanes: the subprocess cap only gates the lane that owns a subprocess ─────


@pytest.fixture()
def native_agent(agents):
    """A subagent bound to a native (in-process) model."""
    agents.create_profile("gpt-helper", "GPT Helper", None, {"model": "gpt-6-astra"})
    return agents


async def test_native_worker_is_admitted_past_the_subprocess_hard_cap(
    native_agent, monkeypatch
):
    """The 2026-09-04 audit case: a native fan-out refused by a cap it cannot fill.

    The pool is wedged at 8/8 SUBPROCESS sessions and the parent dispatches more
    native workers than that cap has slots. Each is coroutines in this process,
    so each must be admitted.
    """
    from app.core import config as cfg

    _install_pool(monkeypatch, _FakePool(in_pool=8, hard_cap=8))
    monkeypatch.setattr(td, "_ADMISSION_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(
        cfg.settings, "subagent_max_concurrent_per_session", 0, raising=False
    )
    started = []

    async def fake_worker(**kwargs):
        started.append(kwargs["worker_session_id"])
        return WorkerOutcome(ok=True, session_id=kwargs["worker_session_id"])

    with patch("app.core.provider_agent.dispatch_worker", side_effect=fake_worker), \
         patch.object(td, "_await_worker_registration", return_value=None):
        payloads = [
            await _dispatch(
                {"agent": "gpt-helper", "prompt": f"go {i}", "mode": "background"}
            )
            for i in range(10)
        ]

    for _ in range(50):  # let the last background task reach its worker
        if len(started) == 10:
            break
        await asyncio.sleep(0.01)

    assert all(p["ok"] for p in payloads), payloads
    assert len(started) == 10


async def test_delegated_worker_is_still_refused_at_the_delegated_cap(
    native_agent, monkeypatch
):
    """Exempting natives must not soften the lane that does cost a subprocess."""
    _install_pool(
        monkeypatch, _FakePool(in_pool=8, hard_cap=8, native_in_pool=12)
    )
    monkeypatch.setattr(td, "_ADMISSION_WAIT_SECONDS", 0.05)

    with patch("app.core.provider_agent.dispatch_worker") as worker:
        payload = await _dispatch({"agent": "helper", "prompt": "go"})

    assert payload["ok"] is False and payload["reason"] == "capacity"
    assert "8/8" in payload["error"]
    worker.assert_not_called()


async def test_natives_in_the_pool_do_not_block_a_delegated_worker(
    native_agent, monkeypatch
):
    """Only the delegated occupancy is compared against the cap."""
    _install_pool(monkeypatch, _FakePool(in_pool=2, hard_cap=8, native_in_pool=30))

    async def fake_worker(**kwargs):
        return WorkerOutcome(ok=True, session_id=kwargs["worker_session_id"],
                             report="done")

    with patch("app.core.provider_agent.dispatch_worker", side_effect=fake_worker):
        payload = await _dispatch({"agent": "helper", "prompt": "go", "mode": "sync"})

    assert payload["ok"] is True


async def test_rss_guard_still_refuses_a_native_worker(native_agent, monkeypatch):
    """The real resource ceiling applies to every lane, and says what it was."""
    _install_pool(
        monkeypatch,
        _FakePool(
            in_pool=0,
            hard_cap=8,
            native_refusal="server busy: tree RSS 9000MB >= ceiling 8000MB",
        ),
    )
    monkeypatch.setattr(td, "_ADMISSION_WAIT_SECONDS", 0.05)

    with patch("app.core.provider_agent.dispatch_worker") as worker:
        payload = await _dispatch({"agent": "gpt-helper", "prompt": "go"})

    assert payload["ok"] is False and payload["reason"] == "capacity"
    assert "tree RSS 9000MB" in payload["error"]
    worker.assert_not_called()


# ── the per-parent fan-out limit is the native lane's backpressure ───────────


async def test_per_parent_fanout_limit_refuses_with_the_capacity_shape(
    native_agent, monkeypatch
):
    from app.core import config as cfg, provider_agent_supervisor as sup

    _install_pool(monkeypatch, _FakePool(in_pool=0, hard_cap=8))
    monkeypatch.setattr(td, "_ADMISSION_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(
        cfg.settings, "subagent_max_concurrent_per_session", 2, raising=False
    )
    monkeypatch.setattr(
        sup, "_LIVE", {"s-root": {"w-1": object(), "w-2": object()}}, raising=False
    )

    with patch("app.core.provider_agent.dispatch_worker") as worker:
        payload = await _dispatch({"agent": "gpt-helper", "prompt": "go"})

    assert payload["ok"] is False
    assert payload["reason"] == "capacity" and payload["retryable"] is True
    assert "limit 2" in payload["error"]
    worker.assert_not_called()


async def test_per_parent_fanout_limit_counts_unregistered_dispatches(
    native_agent, monkeypatch
):
    """Reservations count too — the race a simultaneous fan-out actually loses.

    ``task_tool_max_workers`` sees only workers that already registered, so two
    dispatches racing each other both read zero. The reservation is what makes
    the second one wait.
    """
    from app.core import config as cfg, provider_agent_supervisor as sup

    _install_pool(monkeypatch, _FakePool(in_pool=0, hard_cap=8))
    monkeypatch.setattr(td, "_ADMISSION_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(td, "_ADMISSION_POLL_SECONDS", 0.01)
    monkeypatch.setattr(
        cfg.settings, "subagent_max_concurrent_per_session", 1, raising=False
    )
    monkeypatch.setattr(sup, "_LIVE", {}, raising=False)
    running = asyncio.Event()

    async def fake_worker(**kwargs):
        await running.wait()
        return WorkerOutcome(ok=True, session_id=kwargs["worker_session_id"])

    with patch("app.core.provider_agent.dispatch_worker", side_effect=fake_worker), \
         patch.object(td, "_await_worker_registration", return_value=None):
        first, second = await asyncio.gather(
            _dispatch({"agent": "gpt-helper", "prompt": "a", "mode": "background"}),
            _dispatch({"agent": "gpt-helper", "prompt": "b", "mode": "background"}),
        )
    running.set()
    await asyncio.sleep(0)

    assert sorted([bool(first["ok"]), bool(second["ok"])]) == [False, True]
    refused = first if not first["ok"] else second
    assert refused["reason"] == "capacity"
    assert "already running for this session" in refused["error"]


async def test_no_pool_yet_admits_immediately(agents, monkeypatch):
    from app.core import session_pool

    monkeypatch.setattr(session_pool, "_session_pool", None, raising=False)

    async def fake_worker(**kwargs):
        return WorkerOutcome(ok=True, session_id=kwargs["worker_session_id"],
                             report="done")

    with patch("app.core.provider_agent.dispatch_worker", side_effect=fake_worker):
        payload = await _dispatch({"agent": "helper", "prompt": "go", "mode": "sync"})

    assert payload["ok"] is True
