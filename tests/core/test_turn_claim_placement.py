"""Q3 — cross-runner turn claim via the runs lease (V2 Phase 4).

Covers the coordinator-mandated seven cases:

1. Claim pass acts ONLY on sessions explicitly placed on this runner — never on
   pinned-elsewhere sessions and never on OWNERLESS NULL-placement ("local")
   sessions (the install-relative resolver returns "local" for those on EVERY
   install, so acting on them would let runner B steal runner A's local-default
   sessions). A11: a NULL-placement session that HAS an ``instance_id`` owner
   resolves as an implicit ``runner:<owner>`` pin — claimable, but only by its
   owner.
2. "anywhere" placement: only the deterministically elected lowest-id runner
   claims.
3. Lease fencing: a live foreign run lease blocks the claim (and a second
   ``claim_run`` returns None); an EXPIRED foreign lease does not block.
4. Sticky pin (OD-6/OD-9): the first ``start_turn`` winner of an "anywhere"
   session dual-writes ``placement={"where":"runner:<me>"}`` + legacy
   ``instance_id``; NULL placement is never pinned; an existing pin is never
   rewritten; turn 2 resolves to the same runner.
5. Interrupt observed: a FOREIGN durable running->interrupting flip is picked
   up by the observe pass, which drives the live runtime's interrupt exactly
   once — a second pass is a guarded no-op.
6. Flag OFF: ``handle_query`` takes the legacy cloud-relay path byte-for-byte
   (relay_message called, nothing enqueued locally) and ``claim_once`` is
   inert. Flag ON: a remote-placed query enqueues durably with NO local kick,
   strips the install-local 'claude-code' sentinel, fires the NOTIFY wake, and
   acks ``query_queued``.
7. O4 amendment: the interrupt idempotency guard is PER-TURN — interrupting
   turn 1, completing it, then interrupting turn 2 drives the runtime interrupt
   again (a sticky guard would swallow it).

Fixup coverage (A11/A12): the claim loop honors the A11 implicit instance_id
pin (owned NULL rows claimable only by their owner); the submit seam treats an
own-instance implicit pin as local (sentinel kept, kick fired); adopt_session
dual-writes placement + instance_id so the resolver routes to the adopter.

DB layer (turns/runs/sessions) is real pg-shim SQLite via the conftest
``mock_db``; the pump/recorder/idle-reader surround is patched exactly as in
test_session_runtime.py; the install registry is stubbed as in
test_placement.py (its online-derivation SQL is Postgres-only).
"""

import asyncio
import json
from contextlib import ExitStack, contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core import placement, session_runtime, turn_claim
from app.core.config import settings
from app.db import database
from app.db import runs as runs_db
from app.db import turns as turns_db

RUNNER_ME = "runner-me"
OTHER = "runner-other"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def pin_runner(monkeypatch):
    monkeypatch.setattr(
        type(settings), "effective_instance_id", property(lambda self: RUNNER_ME)
    )


@pytest.fixture(autouse=True)
def flag_on(monkeypatch):
    monkeypatch.setattr(settings, "v2_placement", True)


@pytest.fixture(autouse=True)
def _clean_module_state():
    """Reset session_runtime registries/seams and drain any observer-fired
    interrupt tasks so nothing leaks between tests."""
    session_runtime._reader_locks.clear()
    session_runtime._idle_readers.clear()
    session_runtime._live_runtimes.clear()
    session_runtime.register_scheduler(None)
    session_runtime.register_broadcaster(None)
    turn_claim._interrupt_tasks.clear()
    yield
    session_runtime._reader_locks.clear()
    session_runtime._idle_readers.clear()
    session_runtime._live_runtimes.clear()
    session_runtime.register_scheduler(None)
    session_runtime.register_broadcaster(None)
    for t in list(turn_claim._interrupt_tasks):
        t.cancel()
    turn_claim._interrupt_tasks.clear()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _stub_installs(monkeypatch, online_ids):
    rows = {
        iid: {"id": iid, "online": True, "capabilities": {}} for iid in online_ids
    }
    monkeypatch.setattr(database, "get_installs_map", lambda: rows)


def _seed_session(sid, *, placement_val=None, instance_id=None):
    """A real sessions row with an explicit placement/instance_id."""
    database.create_session(session_id=sid, profile_id="default", title="t")
    from app.db.base import get_db

    with get_db() as conn:
        conn.cursor().execute(
            "UPDATE sessions SET placement = %s, instance_id = %s WHERE id = %s",
            (
                json.dumps(placement_val) if placement_val is not None else None,
                instance_id,
                sid,
            ),
        )


def _placement_of(sid):
    row = database.get_session(sid)
    return placement._normalize_placement(row.get("placement")), row.get("instance_id")


@pytest.fixture
def kicks(monkeypatch):
    calls = []
    monkeypatch.setattr("app.core.turn_scheduler.kick", lambda sid: calls.append(sid))
    return calls


# --- execute() surround (modeled on test_session_runtime._runtime_env) ------

def _make_pump(events):
    async def _impl(**kwargs):
        for e in events:
            yield e
    return _impl


def _blocking_pump(pump_entered: asyncio.Event, release: asyncio.Event):
    async def _impl(**kwargs):
        pump_entered.set()
        await release.wait()
        yield {"type": "done", "metadata": {}}
    return _impl


@contextmanager
def _runtime_env(*, pump):
    registry = MagicMock(name="task_registry")
    registry.get_active_background_work.return_value = []
    registry.get_background_launches.return_value = set()
    registry.reset_started_this_turn.return_value = None

    recorder = MagicMock(name="recorder")
    recorder.start = AsyncMock()
    recorder.on_event = AsyncMock()
    recorder.close = AsyncMock()

    session_runtime.register_broadcaster(AsyncMock(name="broadcaster"))
    session_runtime.register_scheduler(MagicMock(name="on_turn_settled"))

    with ExitStack() as stack:
        stack.enter_context(
            patch("app.core.query_engine._stream_to_websocket_impl", new=pump)
        )
        stack.enter_context(patch.object(
            session_runtime.TurnRecorder, "create_for_run",
            new=AsyncMock(return_value=recorder),
        ))
        stack.enter_context(
            patch("app.core.session_runtime.record_parked", new=AsyncMock())
        )
        stack.enter_context(
            patch("app.core.session_runtime.start_idle_reader", new=MagicMock())
        )
        stack.enter_context(
            patch("app.core.session_runtime.stop_idle_reader", new=AsyncMock())
        )
        stack.enter_context(patch("app.core.task_registry.task_registry", new=registry))
        stack.enter_context(
            patch("app.core.turn_finalize.persist_turn_end_state", new=AsyncMock())
        )
        yield


@contextmanager
def _interrupt_env():
    """Patch interrupt()'s heavy collaborators; yields the interrupt_session spy."""
    spy = AsyncMock(name="interrupt_session")
    with ExitStack() as stack:
        stack.enter_context(
            patch("app.core.session_runtime.record_interrupting", new=AsyncMock())
        )
        stack.enter_context(
            patch("app.core.query_session_lifecycle.interrupt_session", new=spy)
        )
        yield spy


async def _drain_observer_tasks():
    for t in list(turn_claim._interrupt_tasks):
        try:
            await asyncio.wait_for(t, 2.0)
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# 1. Claim pass — explicit ("runner", me) only.
# ---------------------------------------------------------------------------

async def test_claim_pass_kicks_session_pinned_to_me(mock_db, monkeypatch, kicks):
    _stub_installs(monkeypatch, [OTHER])
    _seed_session("s-mine", placement_val={"where": f"runner:{RUNNER_ME}"})
    turns_db.enqueue_turn("s-mine", "user", prompt="hi")

    claimed = await turn_claim.claim_once()

    assert claimed == 1
    assert kicks == ["s-mine"]


async def test_claim_pass_ignores_pinned_elsewhere_and_local_default(
    mock_db, monkeypatch, kicks
):
    """Neither a foreign pin, a foreign-OWNED NULL row (A11 implicit pin), nor
    an ownerless NULL row ('local') is ever claimed by this loop — 'local'
    decisions belong to the in-process submit path of whichever install owns
    them, and owned rows belong to their owner's loop."""
    _stub_installs(monkeypatch, [OTHER])
    _seed_session("s-other", placement_val={"where": f"runner:{OTHER}"})
    _seed_session("s-null", placement_val=None, instance_id=None)
    _seed_session("s-null-foreign", placement_val=None, instance_id=OTHER)
    turns_db.enqueue_turn("s-other", "user", prompt="hi")
    turns_db.enqueue_turn("s-null", "user", prompt="hi")
    turns_db.enqueue_turn("s-null-foreign", "user", prompt="hi")

    claimed = await turn_claim.claim_once()

    assert claimed == 0
    assert kicks == []
    # All turns untouched — still queued for their rightful runners.
    assert turns_db.get_queued_turns("s-other")[0]["state"] == "queued"
    assert turns_db.get_queued_turns("s-null")[0]["state"] == "queued"
    assert turns_db.get_queued_turns("s-null-foreign")[0]["state"] == "queued"


async def test_claim_pass_kicks_null_placement_session_owned_by_me(
    mock_db, monkeypatch, kicks
):
    """A11: a NULL-placement row with instance_id=me resolves as an implicit
    runner:me pin — MY claim loop picks it up (e.g. a turn a peer enqueued
    durably for a post-v61 create_session row that was never explicitly
    pinned)."""
    _stub_installs(monkeypatch, [OTHER])
    _seed_session("s-null-mine", placement_val=None, instance_id=RUNNER_ME)
    turns_db.enqueue_turn("s-null-mine", "user", prompt="hi")

    claimed = await turn_claim.claim_once()

    assert claimed == 1
    assert kicks == ["s-null-mine"]


# ---------------------------------------------------------------------------
# 2. "anywhere" — only the elected lowest-id runner claims.
# ---------------------------------------------------------------------------

async def test_anywhere_session_claimed_only_by_elected_lowest_id(
    mock_db, monkeypatch, kicks
):
    _seed_session("s-any", placement_val={"where": "anywhere"})
    turns_db.enqueue_turn("s-any", "user", prompt="hi")

    # A lower-id peer is online -> IT is the elected runner, not me: no kick.
    _stub_installs(monkeypatch, ["runner-aaa"])
    assert await turn_claim.claim_once() == 0
    assert kicks == []

    # Only higher-id peers online -> I am the elected lowest-id: claim.
    _stub_installs(monkeypatch, ["runner-zzz"])
    assert await turn_claim.claim_once() == 1
    assert kicks == ["s-any"]


# ---------------------------------------------------------------------------
# 3. Lease fencing — a live foreign lease blocks; an expired one does not.
# ---------------------------------------------------------------------------

async def test_live_foreign_lease_blocks_claim_and_double_claim(
    mock_db, monkeypatch, kicks
):
    _stub_installs(monkeypatch, [OTHER])
    _seed_session("s-lease", placement_val={"where": f"runner:{RUNNER_ME}"})
    turns_db.enqueue_turn("s-lease", "user", prompt="hi")

    run = runs_db.claim_run("s-lease", OTHER, 60)
    assert run is not None

    # Anti-joined out of the claimable set while the foreign lease is live.
    assert await turn_claim.claim_once() == 0
    assert kicks == []
    # And the lease itself fences a direct double-claim (partial unique index).
    assert runs_db.claim_run("s-lease", RUNNER_ME, 60) is None


async def test_expired_foreign_lease_does_not_block_claim(
    mock_db, monkeypatch, kicks
):
    _stub_installs(monkeypatch, [OTHER])
    _seed_session("s-dead", placement_val={"where": f"runner:{RUNNER_ME}"})
    turns_db.enqueue_turn("s-dead", "user", prompt="hi")

    run = runs_db.claim_run("s-dead", OTHER, 60)
    assert run is not None
    # Lapse the lease (crashed owner): still state='active', but expired.
    from app.db.base import get_db

    with get_db() as conn:
        conn.cursor().execute(
            "UPDATE runs SET lease_expires_at = '2000-01-01T00:00:00+00:00' "
            "WHERE id = %s",
            (run["id"],),
        )

    assert await turn_claim.claim_once() == 1
    assert kicks == ["s-dead"]


# ---------------------------------------------------------------------------
# 4. Sticky pin (OD-6/OD-9) via the real execute() path.
# ---------------------------------------------------------------------------

async def test_first_winner_sticky_pins_anywhere_session(mock_db, monkeypatch):
    _stub_installs(monkeypatch, [])  # only me online
    _seed_session("s-pin", placement_val={"where": "anywhere"}, instance_id=OTHER)
    turn = turns_db.enqueue_turn("s-pin", "user", prompt="hi")

    with _runtime_env(pump=_make_pump([{"type": "done", "metadata": {}}])):
        outcome = await session_runtime.SessionRuntime(turn).execute()

    assert outcome == "completed"
    pin, instance_id = _placement_of("s-pin")
    # Dual-write: canonical pin + legacy owner column (cloud_branch untouched).
    assert pin == {"where": f"runner:{RUNNER_ME}"}
    assert instance_id == RUNNER_ME
    assert database.get_session("s-pin").get("cloud_branch") in (None, "")
    # Turn 2 of the same session now resolves to the SAME runner (stickiness).
    decision = placement.resolve_placement(database.get_session("s-pin"))
    assert decision.kind == "runner" and decision.runner_id == RUNNER_ME


async def test_null_placement_is_never_pinned(mock_db, monkeypatch):
    """A9: NULL placement stays NULL — the winner must not invent an explicit
    pin for it (routing is covered at read time by the resolver's A11
    instance_id fallback; here the row is self-owned so the gate passes)."""
    _stub_installs(monkeypatch, [])
    _seed_session("s-nullpin", placement_val=None, instance_id=RUNNER_ME)
    turn = turns_db.enqueue_turn("s-nullpin", "user", prompt="hi")

    with _runtime_env(pump=_make_pump([{"type": "done", "metadata": {}}])):
        outcome = await session_runtime.SessionRuntime(turn).execute()

    assert outcome == "completed"
    pin, instance_id = _placement_of("s-nullpin")
    assert pin is None
    assert instance_id == RUNNER_ME  # untouched


async def test_existing_runner_pin_is_never_rewritten(mock_db, monkeypatch):
    _stub_installs(monkeypatch, [])
    _seed_session(
        "s-repin",
        placement_val={"where": f"runner:{RUNNER_ME}"},
        instance_id=OTHER,  # deliberately stale — a rewrite would clobber it
    )
    turn = turns_db.enqueue_turn("s-repin", "user", prompt="hi")

    with _runtime_env(pump=_make_pump([{"type": "done", "metadata": {}}])):
        outcome = await session_runtime.SessionRuntime(turn).execute()

    assert outcome == "completed"
    pin, instance_id = _placement_of("s-repin")
    assert pin == {"where": f"runner:{RUNNER_ME}"}
    assert instance_id == OTHER  # pin path skipped — no dual-write happened


async def test_execute_gate_leaves_foreign_placed_turn_queued(mock_db, monkeypatch):
    """The correctness backstop: even if a local kick reaches execute(), a
    session placed on another runner is refused BEFORE the lease is claimed."""
    _stub_installs(monkeypatch, [OTHER])
    _seed_session("s-gate", placement_val={"where": f"runner:{OTHER}"})
    turn = turns_db.enqueue_turn("s-gate", "user", prompt="hi")

    with _runtime_env(pump=_make_pump([{"type": "done", "metadata": {}}])):
        outcome = await session_runtime.SessionRuntime(turn).execute()

    assert outcome is None
    assert turns_db.get_turn(turn["id"])["state"] == "queued"
    assert runs_db.get_active_run("s-gate") is None  # lease never claimed


# ---------------------------------------------------------------------------
# 4b. A12 — adopt_session dual-writes placement alongside instance_id.
# ---------------------------------------------------------------------------

def test_adopt_dual_writes_placement_and_resolver_routes_to_adopter(
    mock_db, monkeypatch
):
    """A12: 'Resume here' on a runner-pinned-elsewhere session must flip BOTH
    columns in one UPDATE — otherwise the v61 backfill's runner:<old-owner>
    pin survives the adopt and the resolver keeps routing turns to the OLD
    owner. cloud_branch stays untouched (OD-9)."""
    _stub_installs(monkeypatch, [OTHER])
    _seed_session(
        "s-adopt", placement_val={"where": f"runner:{OTHER}"}, instance_id=OTHER
    )

    from app.db import sessions as sessions_db

    row = sessions_db.adopt_session("s-adopt", RUNNER_ME)

    assert row is not None
    pin, instance_id = _placement_of("s-adopt")
    assert pin == {"where": f"runner:{RUNNER_ME}"}
    assert instance_id == RUNNER_ME
    assert database.get_session("s-adopt").get("cloud_branch") in (None, "")
    decision = placement.resolve_placement(database.get_session("s-adopt"))
    assert decision.kind == "runner" and decision.runner_id == RUNNER_ME


# ---------------------------------------------------------------------------
# 5. Interrupt observed — foreign flip drives the live runtime exactly once.
# ---------------------------------------------------------------------------

async def test_foreign_interrupt_flip_observed_and_driven_once(mock_db, monkeypatch):
    _stub_installs(monkeypatch, [OTHER])
    _seed_session("s-int", placement_val={"where": f"runner:{RUNNER_ME}"})
    turn = turns_db.enqueue_turn("s-int", "user", prompt="hi")

    pump_entered = asyncio.Event()
    release = asyncio.Event()

    with _runtime_env(pump=_blocking_pump(pump_entered, release)), \
            _interrupt_env() as interrupt_spy:
        exec_task = asyncio.create_task(session_runtime.SessionRuntime(turn).execute())
        await asyncio.wait_for(pump_entered.wait(), 1.0)

        # FOREIGN durable flip: another install's WS handler ran
        # turns.interrupt_turn — its in-process executor cannot reach the
        # runtime living here, so only the observe pass can deliver it.
        assert turns_db.interrupt_turn("s-int", "stop") is not None

        await turn_claim.claim_once()
        await asyncio.sleep(0.05)  # let the fire-and-forget task reach the SDK signal
        interrupt_spy.assert_awaited_once_with("s-int")

        # Second observation of the same flip: guarded no-op (O4).
        await turn_claim.claim_once()
        await asyncio.sleep(0.05)
        assert interrupt_spy.await_count == 1

        release.set()
        await asyncio.wait_for(exec_task, 2.0)
    await _drain_observer_tasks()


async def test_observer_skips_turns_not_live_in_this_process(mock_db, monkeypatch):
    """An interrupting row stamped with my id but with NO live runtime here
    (post-crash stale row) is left to the reap sweep — never crashes the pass."""
    _stub_installs(monkeypatch, [OTHER])
    _seed_session("s-stale", placement_val={"where": f"runner:{RUNNER_ME}"})
    turn = turns_db.enqueue_turn("s-stale", "user", prompt="hi")
    run = runs_db.claim_run("s-stale", RUNNER_ME, 60)
    assert turns_db.start_turn(turn["id"], run["id"], RUNNER_ME) is not None
    assert turns_db.interrupt_turn("s-stale", "stop") is not None
    # No live runtime registered for the session.
    assert session_runtime.get_live_runtime("s-stale") is None

    await turn_claim.claim_once()  # must not raise, must not spawn a driver
    assert not turn_claim._interrupt_tasks


# ---------------------------------------------------------------------------
# 6. Flag gating — legacy path byte-identical when OFF; placed path when ON.
# ---------------------------------------------------------------------------

def _make_handler():
    import app.api.chat_ws_handler as cwh

    handler = cwh.ChatWebSocketHandler(
        websocket=MagicMock(),
        device_id="dev-1",
        user_id="admin",
        api_user_id=None,
    )
    handler.send_json = AsyncMock(return_value=True)
    return handler


@contextmanager
def _handler_env():
    """Patch the WS-only collaborators (sync engine, run_sync passthrough) —
    turn_manager / turns / database stay real (test_chat_ws_v2_turns pattern)."""
    import app.api.chat_ws_handler as cwh

    async def _run_sync(fn, *a, **k):
        return fn(*a, **k)

    with ExitStack() as stack:
        sync = stack.enter_context(patch.object(cwh, "sync_engine"))
        for name in (
            "register_device",
            "unregister_device",
            "broadcast_message_added",
            "broadcast_queue_updated",
            "broadcast_session_updated",
            "broadcast_user_session_created",
        ):
            setattr(sync, name, AsyncMock())
        stack.enter_context(
            patch.object(cwh, "run_sync", new=AsyncMock(side_effect=_run_sync))
        )
        yield sync


def _sent(handler, ftype):
    return [
        c.args[0]
        for c in handler.send_json.call_args_list
        if c.args and isinstance(c.args[0], dict) and c.args[0].get("type") == ftype
    ]


async def test_flag_off_claim_once_is_inert(mock_db, monkeypatch):
    monkeypatch.setattr(settings, "v2_placement", False)

    def _boom():
        raise AssertionError("claim loop must be inert with the flag off")

    monkeypatch.setattr("app.db.turns.list_claimable_sessions", _boom)
    assert await turn_claim.claim_once() == 0


async def test_flag_on_remote_placed_query_enqueues_durably_no_kick(
    mock_db, monkeypatch, kicks
):
    """The placed submit seam: no HTTP relay, no local kick — a durable queued
    turn (sentinel stripped), a NOTIFY wake, and the query_queued ack."""
    _stub_installs(monkeypatch, [OTHER])
    _seed_session("s-place", placement_val={"where": f"runner:{OTHER}"})

    notifies = []
    monkeypatch.setattr(
        "app.core.turn_claim.notify_turns_changed", lambda sid: notifies.append(sid)
    )
    with _handler_env():
        handler = _make_handler()
        await handler.handle_query({
            "prompt": "run remotely",
            "session_id": "s-place",
            "profile": "claude-code",
            "project": None,
        })

    queued = turns_db.get_queued_turns("s-place")
    assert [(t["source"], t["prompt"]) for t in queued] == [("user", "run remotely")]
    assert queued[0]["profile_id"] is None  # sentinel never crosses installs
    assert queued[0]["instance_id"] is None  # stamped only by start_turn (Q3-E1)
    assert kicks == []  # no_kick: the owner's claim loop starts it
    assert notifies == ["s-place"]
    assert _sent(handler, "query_queued") == [
        {"type": "query_queued", "session_id": "s-place"}
    ]


async def test_flag_on_own_instance_null_placement_query_runs_local(
    mock_db, monkeypatch, kicks
):
    """(1c) A11 sanity: a NULL-placement session OWNED BY ME resolves
    ("runner", me) — NOT placed_remote. The submit seam takes the plain local
    path: sentinel kept, kick fired, no NOTIFY wake, no placed-remote ack."""
    from app.core import turn_manager

    _stub_installs(monkeypatch, [OTHER])
    _seed_session("s-own", placement_val=None, instance_id=RUNNER_ME)
    # Pin the kick seam so the local submit's kick is observable (and inert).
    monkeypatch.setattr(turn_manager, "_kick_hook", lambda sid: kicks.append(sid))

    notifies = []
    monkeypatch.setattr(
        "app.core.turn_claim.notify_turns_changed", lambda sid: notifies.append(sid)
    )
    with _handler_env():
        handler = _make_handler()
        await handler.handle_query({
            "prompt": "run here",
            "session_id": "s-own",
            "profile": "claude-code",
            "project": None,
        })

    queued = turns_db.get_queued_turns("s-own")
    assert [(t["source"], t["prompt"]) for t in queued] == [("user", "run here")]
    assert queued[0]["profile_id"] == "claude-code"  # local turns keep the sentinel
    assert kicks == ["s-own"]  # no_kick=False — the local path kicks
    assert notifies == []  # no placed-remote NOTIFY wake
    assert _sent(handler, "query_queued") == []  # idle local session: no placed ack
    assert _sent(handler, "error") == []


async def test_flag_on_query_errors_honestly_when_no_capable_runner(
    mock_db, monkeypatch, kicks
):
    """Pinned owner offline -> kind='none' -> honest error, nothing enqueued."""
    _stub_installs(monkeypatch, [])  # pinned runner NOT online (only me, via self)
    _seed_session("s-off", placement_val={"where": f"runner:{OTHER}"})

    with _handler_env():
        handler = _make_handler()
        await handler.handle_query({
            "prompt": "hello",
            "session_id": "s-off",
            "profile": "claude-code",
            "project": None,
        })

    errors = _sent(handler, "error")
    assert errors and "No capable device online" in errors[0]["message"]
    assert turns_db.get_queued_turns("s-off") == []
    assert kicks == []


async def test_flag_on_queue_message_remote_placed_no_kick(
    mock_db, monkeypatch, kicks
):
    _stub_installs(monkeypatch, [OTHER])
    _seed_session("s-qplace", placement_val={"where": f"runner:{OTHER}"})

    notifies = []
    monkeypatch.setattr(
        "app.core.turn_claim.notify_turns_changed", lambda sid: notifies.append(sid)
    )
    with _handler_env():
        handler = _make_handler()
        await handler.handle_queue_message({
            "content": "queued remotely",
            "session_id": "s-qplace",
            "profile": "claude-code",
            "project": None,
        })

    queued = turns_db.get_queued_turns("s-qplace")
    assert [(t["source"], t["prompt"]) for t in queued] == [
        ("queue", "queued remotely")
    ]
    assert queued[0]["profile_id"] is None
    assert kicks == []
    assert notifies == ["s-qplace"]


# ---------------------------------------------------------------------------
# 7. O4 — the interrupt guard is per-turn, not sticky.
# ---------------------------------------------------------------------------

async def test_interrupt_guard_resets_for_the_next_turn(mock_db, monkeypatch):
    """Interrupt turn 1 via the observer, complete it, interrupt turn 2 — the
    runtime interrupt must be driven AGAIN (a sticky flag would swallow it)."""
    _stub_installs(monkeypatch, [OTHER])
    _seed_session("s-o4", placement_val={"where": f"runner:{RUNNER_ME}"})

    with _interrupt_env() as interrupt_spy:
        for expected_count in (1, 2):
            turn = turns_db.enqueue_turn("s-o4", "user", prompt=f"t{expected_count}")
            pump_entered = asyncio.Event()
            release = asyncio.Event()
            with _runtime_env(pump=_blocking_pump(pump_entered, release)):
                exec_task = asyncio.create_task(
                    session_runtime.SessionRuntime(turn).execute()
                )
                await asyncio.wait_for(pump_entered.wait(), 1.0)
                assert turns_db.interrupt_turn("s-o4", "stop") is not None

                await turn_claim.claim_once()
                await asyncio.sleep(0.05)
                assert interrupt_spy.await_count == expected_count

                release.set()
                await asyncio.wait_for(exec_task, 2.0)
            await _drain_observer_tasks()
            turn_claim._interrupt_tasks.clear()

    assert interrupt_spy.await_count == 2
