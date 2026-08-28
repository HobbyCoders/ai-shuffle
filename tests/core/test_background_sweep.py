"""The unified cross-install background sweep driver (F5).

Four independent pollers (turn claim, git-ops claim, interaction expiry, plus
the daily git-ops retention purge) are multiplexed onto ONE timer in
``app.core.turn_claim``. These tests pin the two properties that matter:

  * every registered pass still runs, at its own cadence, off the shared timer
    (a missed claim is a stuck turn — the multiplexing must not drop a read);
  * the cadence stretches when the install registry shows no peers and snaps
    back the moment one appears.

Everything is driven with sub-second intervals against fake passes, so the
tests exercise the real driver without waiting out real cadences.
"""

import asyncio

import pytest

from app.core import turn_claim


@pytest.fixture(autouse=True)
def clean_driver():
    """Every test starts and ends with an empty, stopped driver."""
    turn_claim._passes.clear()
    turn_claim.invalidate_peer_cache()
    original_cooldown = turn_claim.WAKE_COOLDOWN_SECONDS
    turn_claim.WAKE_COOLDOWN_SECONDS = 0.005
    yield
    turn_claim.WAKE_COOLDOWN_SECONDS = original_cooldown
    turn_claim._passes.clear()
    turn_claim.invalidate_peer_cache()


async def _stop_driver():
    await turn_claim.stop()


def _counter():
    calls = []

    async def _run():
        calls.append(object())
        return 0

    return calls, _run


def _set_peers(monkeypatch, present: bool):
    async def _peers():
        return present

    monkeypatch.setattr(turn_claim, "_peers_present", _peers)


async def _wait_until(predicate, timeout=3.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


# ─── Every pass still runs ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_driver_runs_every_registered_pass(monkeypatch):
    _set_peers(monkeypatch, True)
    a_calls, a_run = _counter()
    b_calls, b_run = _counter()
    c_calls, c_run = _counter()

    turn_claim.register_pass("a", a_run, 0.02)
    turn_claim.register_pass("b", b_run, 0.02)
    turn_claim.register_pass("c", c_run, 0.02)
    try:
        assert await _wait_until(
            lambda: a_calls and b_calls and c_calls
        ), "a multiplexed pass was dropped"
    finally:
        await _stop_driver()


@pytest.mark.asyncio
async def test_a_slow_pass_does_not_run_at_the_fast_cadence(monkeypatch):
    """Each pass keeps its OWN interval on the shared timer."""
    _set_peers(monkeypatch, True)
    fast_calls, fast_run = _counter()
    slow_calls, slow_run = _counter()

    turn_claim.register_pass("fast", fast_run, 0.02)
    turn_claim.register_pass("slow", slow_run, 30.0)
    try:
        assert await _wait_until(lambda: len(fast_calls) >= 3)
        assert slow_calls == [], "the slow pass ran at the fast pass's cadence"
    finally:
        await _stop_driver()


@pytest.mark.asyncio
async def test_kick_makes_a_pass_due_immediately(monkeypatch):
    _set_peers(monkeypatch, True)
    calls, run = _counter()
    turn_claim.register_pass("late", run, 60.0)
    try:
        await asyncio.sleep(0.05)
        assert calls == []
        turn_claim.kick("late")
        assert await _wait_until(lambda: len(calls) == 1)
    finally:
        await _stop_driver()


@pytest.mark.asyncio
async def test_notify_runs_only_the_notify_driven_passes(monkeypatch):
    """A gateway NOTIFY is the turn-claim latency path, not everyone's."""
    _set_peers(monkeypatch, True)
    notify_calls, notify_run = _counter()
    other_calls, other_run = _counter()

    turn_claim.register_pass("claim", notify_run, 60.0, wake_on_notify=True)
    turn_claim.register_pass("other", other_run, 60.0)
    try:
        await asyncio.sleep(0.05)
        turn_claim._mark_notify()
        assert await _wait_until(lambda: len(notify_calls) == 1)
        assert other_calls == [], "a NOTIFY tick woke a non-NOTIFY pass"
    finally:
        await _stop_driver()


@pytest.mark.asyncio
async def test_a_failing_pass_does_not_starve_the_others(monkeypatch):
    _set_peers(monkeypatch, True)
    good_calls, good_run = _counter()

    async def _boom():
        raise RuntimeError("pass exploded")

    turn_claim.register_pass("boom", _boom, 0.02)
    turn_claim.register_pass("good", good_run, 0.02)
    try:
        assert await _wait_until(lambda: len(good_calls) >= 2)
    finally:
        await _stop_driver()


@pytest.mark.asyncio
async def test_unregister_stops_a_pass_without_stopping_the_driver(monkeypatch):
    _set_peers(monkeypatch, True)
    doomed_calls, doomed_run = _counter()
    keeper_calls, keeper_run = _counter()

    turn_claim.register_pass("doomed", doomed_run, 0.02)
    turn_claim.register_pass("keeper", keeper_run, 0.02)
    try:
        assert await _wait_until(lambda: doomed_calls and keeper_calls)
        turn_claim.unregister_pass("doomed")
        seen = len(doomed_calls)
        before = len(keeper_calls)
        await asyncio.sleep(0.2)
        assert len(doomed_calls) == seen
        assert len(keeper_calls) > before
    finally:
        await _stop_driver()


@pytest.mark.asyncio
async def test_a_slow_pass_does_not_block_the_others(monkeypatch):
    """No head-of-line blocking.

    Before the multiplexing these were separate loops, so a git-ops claim
    sitting in a 30s worktree checkout could not delay a turn claim. Sharing one
    timer must not quietly reintroduce that coupling: passes run as independent
    tasks.
    """
    _set_peers(monkeypatch, True)
    fast_calls, fast_run = _counter()
    started = asyncio.Event()

    async def _slow():
        started.set()
        await asyncio.sleep(5)

    turn_claim.register_pass("slow", _slow, 0.02)
    turn_claim.register_pass("fast", fast_run, 0.02)
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        assert await _wait_until(lambda: len(fast_calls) >= 3), (
            "a long-running pass stalled the whole sweep"
        )
    finally:
        await _stop_driver()


@pytest.mark.asyncio
async def test_a_pass_never_overlaps_itself(monkeypatch):
    """One execution at a time — the property each private loop had."""
    _set_peers(monkeypatch, True)
    concurrent = {"now": 0, "max": 0}

    async def _slow():
        concurrent["now"] += 1
        concurrent["max"] = max(concurrent["max"], concurrent["now"])
        await asyncio.sleep(0.3)
        concurrent["now"] -= 1

    turn_claim.register_pass("slow", _slow, 0.01)
    try:
        await asyncio.sleep(0.5)
        assert concurrent["max"] == 1, (
            f"pass re-entered itself ({concurrent['max']} concurrent runs)"
        )
    finally:
        await _stop_driver()


@pytest.mark.asyncio
async def test_shutdown_cancels_an_in_flight_pass(monkeypatch):
    _set_peers(monkeypatch, True)
    cancelled = {"yes": False}
    started = asyncio.Event()

    async def _slow():
        started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled["yes"] = True
            raise

    turn_claim.register_pass("slow", _slow, 0.01)
    await asyncio.wait_for(started.wait(), timeout=2)

    await _stop_driver()

    assert cancelled["yes"], "shutdown left a pass running against a closing pool"


# ─── Single-install detection ─────────────────────────────────────────────


def test_effective_interval_stretches_only_without_peers(monkeypatch):
    sweep = turn_claim.SweepPass(name="x", run=None, tight_interval=5.0)
    monkeypatch.setattr(turn_claim, "_stretch_interval", lambda: 60.0)

    assert turn_claim._effective_interval(sweep, peers=True) == 5.0
    assert turn_claim._effective_interval(sweep, peers=False) == 60.0


def test_stretch_never_shortens_an_already_slow_pass(monkeypatch):
    daily = turn_claim.SweepPass(name="daily", run=None, tight_interval=86400.0)
    monkeypatch.setattr(turn_claim, "_stretch_interval", lambda: 60.0)

    assert turn_claim._effective_interval(daily, peers=False) == 86400.0


@pytest.mark.asyncio
async def test_single_install_stretches_and_a_peer_tightens(monkeypatch):
    """The cadence follows the install registry, in both directions."""
    peers = {"present": False}

    async def _peers():
        return peers["present"]

    monkeypatch.setattr(turn_claim, "_peers_present", _peers)
    monkeypatch.setattr(turn_claim, "_stretch_interval", lambda: 0.3)

    calls, run = _counter()
    turn_claim.register_pass("claim", run, 0.02)
    try:
        # Stretched to 0.3s instead of the pass's own 0.02s cadence.
        await asyncio.sleep(0.45)
        stretched = len(calls)
        assert 1 <= stretched <= 4, (
            f"single-install stretch not applied (ran {stretched}x in 0.45s "
            f"at a 0.3s stretched cadence)"
        )

        # A peer appears -> the tight cadence comes back.
        peers["present"] = True
        await asyncio.sleep(0.45)
        tightened = len(calls) - stretched
        assert tightened > stretched * 2, (
            f"cadence did not tighten after a peer appeared "
            f"(stretched={stretched}, tightened={tightened})"
        )
    finally:
        await _stop_driver()


@pytest.mark.asyncio
async def test_found_work_invalidates_a_stale_no_peers_answer(monkeypatch):
    """Real work on a "single install" is evidence the answer is stale."""
    monkeypatch.setattr(turn_claim, "_stretch_interval", lambda: 5.0)
    checks = {"n": 0}

    async def _peers():
        checks["n"] += 1
        return False

    monkeypatch.setattr(turn_claim, "_peers_present", _peers)

    async def _run():
        return 1  # claimed something

    turn_claim.register_pass("claim", _run, 0.02)
    try:
        assert await _wait_until(lambda: checks["n"] >= 2)
    finally:
        await _stop_driver()


# ─── The peer read itself ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_peers_present_reads_the_install_registry(monkeypatch):
    from app.core.config import settings
    from app.db import installs as installs_db

    me = settings.effective_instance_id
    calls = {"n": 0}

    def _rows_solo():
        calls["n"] += 1
        return [{"id": me}]

    monkeypatch.setattr(installs_db, "get_installs", _rows_solo)
    turn_claim.invalidate_peer_cache()
    assert await turn_claim._peers_present() is False
    # Cached: no second read inside the TTL.
    assert await turn_claim._peers_present() is False
    assert calls["n"] == 1

    monkeypatch.setattr(
        installs_db, "get_installs", lambda: [{"id": me}, {"id": "peer-2"}]
    )
    turn_claim.invalidate_peer_cache()
    assert await turn_claim._peers_present() is True


@pytest.mark.asyncio
async def test_peers_present_fails_safe_to_multi_install(monkeypatch):
    """A registry error must keep the SAFE (tight) cadence, not the cheap one."""
    from app.db import installs as installs_db

    def _boom():
        raise RuntimeError("registry down")

    monkeypatch.setattr(installs_db, "get_installs", _boom)
    turn_claim.invalidate_peer_cache()

    assert await turn_claim._peers_present() is True


# ─── Registration wiring of the real owners ───────────────────────────────


@pytest.mark.asyncio
async def test_git_ops_and_interaction_sweeper_register_their_passes():
    from app.core import git_ops_runner, interaction_sweeper

    try:
        git_ops_runner.start()
        interaction_sweeper.start()

        assert set(turn_claim._passes) == {
            "git_ops", "git_ops_retention", "interaction_expiry",
        }
        assert turn_claim._passes["git_ops"].tight_interval == (
            git_ops_runner.CLAIM_POLL_SECONDS
        )
        assert turn_claim._passes["git_ops_retention"].tight_interval == (
            git_ops_runner.SWEEP_INTERVAL_SECONDS
        )
        # None of them are NOTIFY-driven: only the turn claim is.
        assert not any(p.wake_on_notify for p in turn_claim._passes.values())

        await git_ops_runner.stop()
        await interaction_sweeper.stop()
        assert turn_claim._passes == {}
    finally:
        await _stop_driver()


@pytest.mark.asyncio
async def test_turn_claim_start_registers_a_notify_driven_pass(monkeypatch):
    # The gateway is optional; make its absence explicit rather than incidental.
    import app.core.turn_claim as tc

    monkeypatch.setattr(
        tc, "_on_gateway_tick", tc._on_gateway_tick, raising=False
    )
    try:
        turn_claim.start()
        sweep = turn_claim._passes["turn_claim"]
        assert sweep.wake_on_notify is True
        assert sweep.tight_interval == turn_claim.CLAIM_POLL_SECONDS
        assert sweep.run is turn_claim.claim_once
    finally:
        await _stop_driver()
    assert "turn_claim" not in turn_claim._passes
