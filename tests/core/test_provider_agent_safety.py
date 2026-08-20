"""Safety-layer tests for ProviderAgent workers (chunk P3-A4).

The design case is the 2026-07-22 rate-limit storm: the parent turn ended at
19:58:23 and all 3,172 upstream requests happened after that, and both
registered roots reached a terminal status at 20:06:43 while 410 more requests
flowed over the next two minutes with 35 child processes alive. So these tests
insist on two things a mock-heavy suite would happily let through:

* teardown kills the worker's SESSION and subprocess, not just its registry row
  (a "killed" row with a live child is exactly what the incident looked like);
* the CUMULATIVE ``bg_launches`` park-gate set is purged for the torn-down
  worker, because a teardown that only zeroes running tasks still parks the turn
  — and every turn after it.

And one thing the ruling makes non-negotiable: **ordinary CLI-native subagent
parking is completely unchanged.** That is the regression that matters most, so
it is asserted directly rather than implied.

NOTHING here starts a session, a CLI, or a network call.
"""

import asyncio
from contextlib import ExitStack, contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core import provider_agent as pa
from app.core import provider_agent_feed as feed_mod
from app.core import provider_agent_supervisor as sup
from app.core import session_runtime
from app.core.task_registry import task_registry
from app.db import session_events as se
from app.db import turns as turns_db


PARENT = "parent-session"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db():
    """Route the DB layer at a fresh in-memory schema for this test."""
    from tests.pg_shim import new_sqlite_connection, route_db_to

    conn = new_sqlite_connection()
    with route_db_to(conn):
        yield conn
    conn.close()


@pytest.fixture(autouse=True)
def _clean_state():
    """Every registry this chunk touches is process-global — reset around each
    test so ordering can never carry state between them."""
    sup.reset()
    pa.reset_worker_sessions()
    feed_mod.reset_feed_groups()
    task_registry.reset_all()
    session_runtime._reader_locks.clear()
    session_runtime._idle_readers.clear()
    session_runtime._live_runtimes.clear()
    session_runtime.register_scheduler(None)
    session_runtime.register_broadcaster(None)
    yield
    sup.reset()
    pa.reset_worker_sessions()
    feed_mod.reset_feed_groups()
    task_registry.reset_all()
    session_runtime._reader_locks.clear()
    session_runtime._idle_readers.clear()
    session_runtime._live_runtimes.clear()
    session_runtime.register_scheduler(None)
    session_runtime.register_broadcaster(None)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_pump(events):
    async def _impl(**kwargs):
        for e in events:
            yield e
    return _impl


def _recorder_mock():
    rec = MagicMock(name="recorder")
    rec.start = AsyncMock()
    rec.on_event = AsyncMock()
    rec.close = AsyncMock()
    return rec


@contextmanager
def _runtime_env():
    """Patch everything around the DB EXCEPT the task registry.

    The registry is deliberately REAL here: the whole point is what the park
    gate reads after teardown has run, and a mocked registry would answer
    whatever the test told it to.
    """
    recorder = _recorder_mock()
    session_runtime.register_broadcaster(AsyncMock(name="broadcaster"))
    session_runtime.register_scheduler(MagicMock(name="on_turn_settled"))

    start_idle = MagicMock(name="start_idle_reader")

    with ExitStack() as stack:
        stack.enter_context(patch(
            "app.core.query_engine._stream_to_websocket_impl",
            new=_make_pump([{"type": "done", "metadata": {}}]),
        ))
        stack.enter_context(patch.object(
            session_runtime.TurnRecorder, "create_for_run",
            new=AsyncMock(return_value=recorder),
        ))
        stack.enter_context(patch(
            "app.core.session_runtime.record_parked", new=AsyncMock()))
        stack.enter_context(patch(
            "app.core.session_runtime.start_idle_reader", new=start_idle))
        stack.enter_context(patch(
            "app.core.session_runtime.stop_idle_reader", new=AsyncMock()))
        stack.enter_context(patch(
            "app.core.turn_finalize.persist_turn_end_state", new=AsyncMock()))
        yield {"start_idle": start_idle}


@contextmanager
def _kill_spies(evicted=True):
    """Spy on the two acts that actually END a worker: the durable interrupt and
    the pooled-client eviction that kills the CLI subprocess tree."""
    pool = MagicMock(name="session_pool")
    pool.remove_broken = AsyncMock(return_value=object() if evicted else None)
    interrupt = MagicMock(name="turn_manager.interrupt", return_value={"id": "t"})

    with ExitStack() as stack:
        stack.enter_context(patch(
            "app.core.session_pool.get_session_pool", return_value=pool))
        stack.enter_context(patch(
            "app.core.turn_manager.interrupt", new=interrupt))
        yield {"pool": pool, "interrupt": interrupt}


def _register_worker(worker_id, *, provider="prov-openai", model="gpt-5.5", feed=None):
    """Register a worker exactly the way ``dispatch_worker`` does."""
    task_registry.start_task(
        PARENT, worker_id, "agent", f"worker {worker_id}",
        summary=f"{provider} · {model}", tool_use_id=worker_id,
    )
    return sup.register_worker(sup.WorkerRecord(
        parent_session_id=PARENT,
        worker_session_id=worker_id,
        task_id=worker_id,
        tool_use_id=worker_id,
        provider_id=provider,
        model_id=model,
        feed=feed,
    ))


def _parent_types():
    return [r["event_type"] for r in se.get_events(PARENT)]


# ---------------------------------------------------------------------------
# 0. The claim this chunk is built on: turns.source IS stamped
# ---------------------------------------------------------------------------

class TestWorkerTurnSourceIsPersisted:
    """The persistent nesting guard rests entirely on ``turns.source`` actually
    carrying ``provider_agent``. Verified against the real turns table rather
    than assumed."""

    def test_submit_stamps_provider_agent_on_the_turn_row(self, db):
        from app.core import turn_manager

        with patch("app.core.turn_manager._kick"):
            turn_id = turn_manager.submit(
                "worker-1", pa.WORKER_TURN_SOURCE, prompt="do the thing"
            )

        assert turns_db.get_turn(turn_id)["source"] == "provider_agent"

    def test_the_stamp_survives_the_turn_going_live(self, db):
        from app.db import runs as runs_db

        turn = turns_db.enqueue_turn("worker-2", pa.WORKER_TURN_SOURCE, prompt="x")
        run = runs_db.claim_run("worker-2", "runner-a", 60)
        turns_db.start_turn(turn["id"], run["id"], "runner-a")

        live = turns_db.get_live_turn("worker-2")
        assert live is not None
        assert live["source"] == "provider_agent"


# ---------------------------------------------------------------------------
# 1. Turn-lifetime teardown — ProviderAgent workers ONLY
# ---------------------------------------------------------------------------

class TestTurnEndTeardown:

    async def test_workers_are_torn_down_at_normal_turn_end(self, db):
        turn = turns_db.enqueue_turn(PARENT, "user", prompt="hi")
        _register_worker("w1")

        with _runtime_env(), _kill_spies():
            outcome = await session_runtime.SessionRuntime(turn).execute()

        # The turn SETTLES rather than parking — the worker no longer survives it.
        assert outcome == "completed"
        assert turns_db.get_turn(turn["id"])["state"] == "completed"
        # Registry row terminal, with turn-end attribution (not a user stop).
        assert task_registry.get_task(PARENT, "w1")["status"] == "killed"
        assert task_registry.get_task(PARENT, "w1")["reason"] == sup.TURN_END_REASON
        assert sup.live_workers(PARENT) == []

    async def test_teardown_kills_the_session_not_just_the_registry_row(self, db):
        """The 2026-07-22 signature: rows terminal at 20:06:43, 35 child
        processes still alive and 410 more requests issued. Marking the row is
        NOT teardown — the pooled client (and its subprocess tree) must go."""
        turn = turns_db.enqueue_turn(PARENT, "user", prompt="hi")
        _register_worker("w1")

        with _runtime_env(), _kill_spies() as spies:
            await session_runtime.SessionRuntime(turn).execute()

        # Durable interrupt of the WORKER's own turn, with background teardown.
        spies["interrupt"].assert_called_once_with("w1", "stop", True)
        # And the pooled client evicted — this is what ends the CLI subprocess
        # and its descendants.
        spies["pool"].remove_broken.assert_awaited_once_with("w1")

    async def test_teardown_runs_on_the_crash_path_too(self, db):
        """Every normal, aborted and crashed turn funnels through ``_finalize``,
        which is why the hook lives there and not on the happy path."""
        turn = turns_db.enqueue_turn(PARENT, "user", prompt="hi")
        _register_worker("w1")

        async def _boom(**kwargs):
            if False:
                yield {}
            raise RuntimeError("pump boom")

        with _runtime_env(), _kill_spies() as spies:
            with patch("app.core.query_engine._stream_to_websocket_impl", new=_boom):
                outcome = await session_runtime.SessionRuntime(turn).execute()

        assert outcome == "aborted"
        spies["pool"].remove_broken.assert_awaited_once_with("w1")
        assert sup.live_workers(PARENT) == []

    async def test_many_workers_are_all_torn_down(self, db):
        """Fan-out is uncapped by ruling P12, so teardown must be too."""
        turn = turns_db.enqueue_turn(PARENT, "user", prompt="hi")
        for i in range(20):
            _register_worker(f"w{i}")

        with _runtime_env(), _kill_spies() as spies:
            outcome = await session_runtime.SessionRuntime(turn).execute()

        assert outcome == "completed"
        assert spies["pool"].remove_broken.await_count == 20
        assert sup.live_workers(PARENT) == []

    async def test_a_teardown_failure_never_changes_how_the_turn_settles(self, db):
        turn = turns_db.enqueue_turn(PARENT, "user", prompt="hi")
        _register_worker("w1")

        with _runtime_env():
            with patch(
                "app.core.provider_agent_supervisor.teardown_workers",
                new=AsyncMock(side_effect=RuntimeError("teardown boom")),
            ):
                outcome = await session_runtime.SessionRuntime(turn).execute()

        # Registry row untouched by the failed teardown -> the turn still parks,
        # which is the SAFE direction (work is assumed alive).
        assert outcome == "parked"

    async def test_a_turn_with_no_workers_costs_nothing(self, db):
        """The overwhelming majority of turns dispatch no workers; teardown must
        not so much as look at the pool for them."""
        turn = turns_db.enqueue_turn(PARENT, "user", prompt="hi")

        with _runtime_env(), _kill_spies() as spies:
            outcome = await session_runtime.SessionRuntime(turn).execute()

        assert outcome == "completed"
        spies["pool"].remove_broken.assert_not_awaited()
        spies["interrupt"].assert_not_called()


# ---------------------------------------------------------------------------
# 2. THE regression that matters most: ordinary subagent parking is unchanged
# ---------------------------------------------------------------------------

class TestOrdinarySubagentParkingIsUnchanged:
    """RULING 2026-07-25: turn-lifetime binding applies to ProviderAgent workers
    ONLY. Ordinary CLI-native subagent parking is a deliberate feature."""

    async def test_an_ordinary_background_subagent_still_parks_the_turn(self, db):
        turn = turns_db.enqueue_turn(PARENT, "user", prompt="hi")
        # A normal SDK background sub-agent: registry row, NO supervisor record.
        task_registry.start_task(PARENT, "ordinary-1", "agent", "Explore",
                                 tool_use_id="toolu_ordinary")

        with _runtime_env() as env, _kill_spies() as spies:
            outcome = await session_runtime.SessionRuntime(turn).execute()

        assert outcome == "parked"
        assert turns_db.get_turn(turn["id"])["state"] == "parked"
        assert turns_db.get_turn(turn["id"])["payload"]["task_ids"] == ["ordinary-1"]
        # Still running, still idle-read — untouched by this chunk.
        assert task_registry.get_task(PARENT, "ordinary-1")["status"] == "running"
        env["start_idle"].assert_called_once_with(PARENT)
        spies["pool"].remove_broken.assert_not_awaited()
        spies["interrupt"].assert_not_called()

    async def test_an_ordinary_launch_id_alone_still_parks_the_turn(self, db):
        """The park gate ORs the cumulative launch set; that arm is unchanged."""
        turn = turns_db.enqueue_turn(PARENT, "user", prompt="hi")
        task_registry.mark_background_launch(PARENT, "toolu_ordinary")

        with _runtime_env(), _kill_spies():
            outcome = await session_runtime.SessionRuntime(turn).execute()

        assert outcome == "parked"
        assert task_registry.get_background_launches(PARENT) == {"toolu_ordinary"}

    async def test_ordinary_work_parks_even_while_workers_are_torn_down(self, db):
        """Mixed turn: the worker dies, the ordinary sub-agent lives and parks."""
        turn = turns_db.enqueue_turn(PARENT, "user", prompt="hi")
        _register_worker("w1")
        task_registry.start_task(PARENT, "ordinary-1", "agent", "Explore",
                                 tool_use_id="toolu_ordinary")

        with _runtime_env(), _kill_spies() as spies:
            outcome = await session_runtime.SessionRuntime(turn).execute()

        assert outcome == "parked"
        assert turns_db.get_turn(turn["id"])["payload"]["task_ids"] == ["ordinary-1"]
        assert task_registry.get_task(PARENT, "w1")["status"] == "killed"
        assert task_registry.get_task(PARENT, "ordinary-1")["status"] == "running"
        spies["pool"].remove_broken.assert_awaited_once_with("w1")


# ---------------------------------------------------------------------------
# 3. The cumulative bg_launches trap
# ---------------------------------------------------------------------------

class TestCumulativeLaunchSetTrap:
    """``_background_launches`` is never reset per turn and is cleared only by
    ``clear_session``. A teardown that only zeroes running tasks leaves the
    worker's id there, so the turn parks anyway — and so does every turn after
    it, forever. This is the single most likely way the whole chunk silently
    does nothing."""

    async def test_a_worker_launch_id_does_not_park_the_turn_it_died_with(self, db):
        turn = turns_db.enqueue_turn(PARENT, "user", prompt="hi")
        _register_worker("w1")
        # The worker's launch id in the CUMULATIVE set — the trap.
        task_registry.mark_background_launch(PARENT, "w1")

        with _runtime_env(), _kill_spies():
            outcome = await session_runtime.SessionRuntime(turn).execute()

        assert outcome == "completed"
        assert task_registry.get_background_launches(PARENT) == set()

    async def test_later_turns_are_not_permanently_parked(self, db):
        """The tail of the trap: even if turn N somehow settled, turn N+1 would
        park forever on the residue. Two consecutive clean turns prove it gone."""
        first = turns_db.enqueue_turn(PARENT, "user", prompt="one")
        _register_worker("w1")
        task_registry.mark_background_launch(PARENT, "w1")

        with _runtime_env(), _kill_spies():
            assert await session_runtime.SessionRuntime(first).execute() == "completed"

        second = turns_db.enqueue_turn(PARENT, "user", prompt="two")
        with _runtime_env(), _kill_spies():
            assert await session_runtime.SessionRuntime(second).execute() == "completed"

        assert turns_db.get_turn(second["id"])["state"] == "completed"

    async def test_the_purge_is_surgical(self, db):
        """Only the torn-down worker's own id goes. An ordinary sub-agent's
        launch id is left exactly where it was."""
        turn = turns_db.enqueue_turn(PARENT, "user", prompt="hi")
        _register_worker("w1")
        task_registry.mark_background_launch(PARENT, "w1")
        task_registry.mark_background_launch(PARENT, "toolu_ordinary")

        with _runtime_env(), _kill_spies():
            outcome = await session_runtime.SessionRuntime(turn).execute()

        assert outcome == "parked"  # the ordinary id still parks — as designed
        assert task_registry.get_background_launches(PARENT) == {"toolu_ordinary"}

    def test_discard_leaves_the_subagent_lane_consistent(self):
        """The discard covers BOTH cumulative sets and reports what it did."""
        task_registry.mark_background_launch(PARENT, "w1")
        task_registry.mark_subagent_background_launch(PARENT, "w1")

        assert task_registry.discard_background_launch(PARENT, "w1") is True
        assert task_registry.get_background_launches(PARENT) == set()
        assert task_registry.was_subagent_background_launch(PARENT, "w1") is False
        # Idempotent, and honest about a miss.
        assert task_registry.discard_background_launch(PARENT, "w1") is False
        assert task_registry.discard_background_launch(PARENT, "") is False


# ---------------------------------------------------------------------------
# 4. Chip suppression under wide fan-out
# ---------------------------------------------------------------------------

class TestChipSuppression:
    """``complete_task`` fires a durable terminal chip per killed task. Fan-out
    is uncapped (P12), so a wide turn ending would surface a burst of them."""

    async def test_teardown_fires_no_per_worker_chips_and_one_note(self, db):
        chips = []
        task_registry.set_terminal_notification_callback(
            lambda sid, task: chips.append((sid, task["id"]))
        )
        turn = turns_db.enqueue_turn(PARENT, "user", prompt="hi")
        for i in range(20):
            _register_worker(f"w{i}")
            # Make each worker chip-ELIGIBLE (``is_own_background_task``), so the
            # test proves suppression rather than accidental ineligibility.
            task_registry.mark_background_launch(PARENT, f"w{i}")

        with _runtime_env(), _kill_spies():
            await session_runtime.SessionRuntime(turn).execute()

        assert chips == []
        # Exactly ONE coalesced note replaces the twenty chips.
        notes = [
            r for r in se.get_events(PARENT)
            if r["event_type"] == "system.note"
            and r["payload"].get("subtype") == sup.NOTE_SUBTYPE
        ]
        assert len(notes) == 1
        assert notes[0]["payload"]["data"]["count"] == 20
        assert notes[0]["payload"]["data"]["reason"] == sup.TURN_END_REASON

    def test_an_ordinary_completion_still_chips(self):
        """The ``notify`` kwarg defaults to True — no existing caller changes."""
        chips = []
        task_registry.set_terminal_notification_callback(
            lambda sid, task: chips.append(task["id"])
        )
        task_registry.start_task(PARENT, "ordinary-1", "agent", "Explore",
                                 tool_use_id="toolu_ordinary")
        task_registry.mark_background_launch(PARENT, "toolu_ordinary")

        task_registry.complete_task(PARENT, "ordinary-1", "error")

        assert chips == ["ordinary-1"]

    def test_suppressed_completion_still_records_the_terminal_state(self):
        """Only the chip is skipped — the row and the retention snapshot are
        still written, or /live would show a task that never died."""
        chips = []
        task_registry.set_terminal_notification_callback(
            lambda sid, task: chips.append(task["id"])
        )
        task_registry.start_task(PARENT, "w1", "agent", "worker", tool_use_id="w1")
        task_registry.mark_background_launch(PARENT, "w1")

        task_registry.complete_task(PARENT, "w1", "killed", notify=False)

        assert chips == []
        assert task_registry.get_task(PARENT, "w1")["status"] == "killed"
        assert [t["id"] for t in task_registry.get_visible(PARENT)] == ["w1"]


# ---------------------------------------------------------------------------
# 5. Cards settle rather than spinning forever
# ---------------------------------------------------------------------------

class TestCardsSettle:
    """``turn.completed`` is emitted BEFORE ``_finalize`` runs, and the reducer
    speculatively settles running cards only on ``turn.completed`` — never on
    ``turn.aborted``. A card killed after that point gets no second chance."""

    async def test_teardown_settles_the_worker_card(self, db):
        feed = feed_mod.WorkerFeed(
            parent_session_id=PARENT, worker_session_id="w1",
            provider_id="prov-openai", model_id="gpt-5.5", prompt="go",
        )
        await feed.started()
        turn = turns_db.enqueue_turn(PARENT, "user", prompt="hi")
        _register_worker("w1", feed=feed)

        with _runtime_env(), _kill_spies():
            await session_runtime.SessionRuntime(turn).execute()

        types = _parent_types()
        assert "subagent.started" in types
        assert "subagent.completed" in types
        done = next(r for r in se.get_events(PARENT)
                    if r["event_type"] == "subagent.completed")
        assert done["payload"]["status"] == "failed"
        assert done["payload"]["task_id"] == "w1"
        # The task.updated settle frame joins the card through tool_use_id.
        settle = next(r for r in se.get_events(PARENT)
                      if r["event_type"] == "task.updated")
        assert settle["payload"]["tool_use_id"] == "w1"
        assert settle["payload"]["state"] == "killed"


# ---------------------------------------------------------------------------
# 6. Persistent nesting guard (replaces the in-process set)
# ---------------------------------------------------------------------------

class TestPersistentNestingGuard:
    """PR #762 guarded nesting with a process-local set. Under V2 placement a
    worker whose turn is claimed by a DIFFERENT runner is not in that runner's
    set, so cross-runner recursion was possible."""

    def _go_live(self, session_id, source):
        from app.db import runs as runs_db

        turn = turns_db.enqueue_turn(session_id, source, prompt="x")
        run = runs_db.claim_run(session_id, "runner-b", 60)
        turns_db.start_turn(turn["id"], run["id"], "runner-b")
        return turn

    def test_a_worker_turn_claimed_by_another_runner_is_still_a_worker(self, db):
        # "Another runner" == this process never registered the id.
        self._go_live("worker-x", pa.WORKER_TURN_SOURCE)

        assert pa.is_worker_session("worker-x") is False   # the in-process hole
        assert pa.is_worker_turn("worker-x") is True       # the persisted signal
        assert pa.is_provider_agent_worker("worker-x") is True

    def test_an_ordinary_session_is_not_a_worker(self, db):
        self._go_live("human-1", "user")

        assert pa.is_provider_agent_worker("human-1") is False

    def test_a_resumed_worker_session_running_a_user_turn_is_not_a_worker(self, db):
        """A worker session a human later resumes and types into is running a
        ``user`` turn — it is no longer acting as a worker and gets the tool
        back."""
        self._go_live("worker-y", "user")

        assert pa.is_worker_turn("worker-y") is False

    def test_the_in_process_set_still_covers_the_pre_turn_window(self, db):
        """Before the worker's turn row goes live there is nothing persisted to
        read, which is why the union keeps the set."""
        pa.register_worker_session("worker-z")

        assert pa.is_worker_turn("worker-z") is False
        assert pa.is_provider_agent_worker("worker-z") is True

    def test_an_unreadable_turns_table_degrades_to_the_set(self):
        """No DB routed at all: the read fails, the guard must not raise."""
        assert pa.is_worker_turn("whatever") is False
        pa.register_worker_session("whatever")
        assert pa.is_provider_agent_worker("whatever") is True

    def test_no_session_id_is_never_a_worker(self):
        assert pa.is_provider_agent_worker(None) is False
        assert pa.is_provider_agent_worker("") is False


class TestNestingGuardWiring:
    """The guard has to be the one the options build and the tool actually use."""

    def test_cross_runner_worker_keeps_task_but_not_the_old_server(self, db, monkeypatch):
        """(P2c) Workers keep the unified Task tool — nesting is depth-gated in
        task_dispatch, not denied at registration — but the retired
        provider-agent server must never come back."""
        from app.core.config import settings
        from app.db import runs as runs_db
        import app.core.query_options as qo

        monkeypatch.setattr(settings, "provider_agent_enabled", True)
        turn = turns_db.enqueue_turn("worker-x", pa.WORKER_TURN_SOURCE, prompt="x")
        run = runs_db.claim_run("worker-x", "runner-b", 60)
        turns_db.start_turn(turn["id"], run["id"], "runner-b")

        with patch.object(qo, "get_security_instructions", return_value=""):
            opts, _ = qo.build_options_from_profile(
                profile={"id": "t", "config": {"model": "opus",
                                               "tools_preset": "claude_code"}},
                session_id="worker-x",
            )

        # Nothing in this process ever registered worker-x.
        assert pa.is_worker_session("worker-x") is False
        assert "mcp__builtins__Task" in set(opts.allowed_tools or [])
        servers = opts.mcp_servers if isinstance(opts.mcp_servers, dict) else {}
        assert "provider-agent" not in servers

    def test_ask_user_question_is_stripped_from_a_cross_runner_worker(self, db, monkeypatch):
        """Ruling A3: the question exclusion rides the SAME guard, so it holds
        for a resumed worker whose turn is claimed by another runner (nothing
        in this process registered the id)."""
        from app.core.config import settings
        from app.db import runs as runs_db
        import app.core.query_options as qo

        monkeypatch.setattr(settings, "provider_agent_enabled", True)
        turn = turns_db.enqueue_turn("worker-q", pa.WORKER_TURN_SOURCE, prompt="x")
        run = runs_db.claim_run("worker-q", "runner-b", 60)
        turns_db.start_turn(turn["id"], run["id"], "runner-b")

        with patch.object(qo, "get_security_instructions", return_value=""):
            opts, _ = qo.build_options_from_profile(
                profile={"id": "t", "config": {"model": "opus",
                                               "tools_preset": "claude_code"}},
                session_id="worker-q",
            )

        assert pa.is_worker_session("worker-q") is False
        assert qo.WORKER_DISALLOWED_TOOL in (opts.disallowed_tools or [])

    async def test_task_dispatch_refuses_beyond_max_depth(self, db, monkeypatch):
        """(P2c) The depth gate replaced the old one-level handler refusal:
        a caller already at settings.task_tool_max_depth cannot dispatch."""
        from app.core.config import settings
        from app.core import task_dispatch as td

        monkeypatch.setattr(settings, "task_tool_max_depth", 5)
        # Simulate a caller five worker-levels deep.
        monkeypatch.setattr(
            td, "caller_chain", lambda sid: ([], "root-1", 5)
        )
        from app.core import agent_provider_models as apm

        monkeypatch.setattr(
            apm,
            "available_subagents",
            lambda caller, provider: (
                apm.SubagentTarget("helper", "Helper", "", "sonnet"),
            ),
        )

        payload = await td.dispatch_task_tool(
            {"agent": "helper", "prompt": "go"},
            caller_session_id="worker-deep",
            caller_profile_id="caller",
        )
        assert payload["ok"] is False
        assert "Nesting limit" in payload["error"]


# ---------------------------------------------------------------------------
# 7. Poller consolidation
# ---------------------------------------------------------------------------

def _text_row(text):
    return {"type": "assistant.text_delta", "payload": {"text": text}}


def _tool_row(tool_id="t1", name="Read", **kw):
    return {"type": "tool.use",
            "payload": {"tool_use_id": tool_id, "name": name, "input": kw}}


class TestPollerConsolidation:
    """One poller per PARENT TURN, not one per worker. Ruling P12 forbids a
    worker cap, so twenty per-worker pollers at 1.5s meant ~13 DB reads/second
    through the sync-executor pool — against a codebase with a documented
    history of PoolError bursts from executor count exceeding pool max."""

    def _feeds(self, n):
        return [
            feed_mod.WorkerFeed(
                parent_session_id=PARENT, worker_session_id=f"w{i}",
                label=f"worker-{i}", provider_id="prov-openai",
                model_id="gpt-5.5", prompt="go",
            )
            for i in range(n)
        ]

    async def test_a_group_tick_produces_the_same_frames_as_per_worker_drains(self, db):
        """Plumbing change, not a behaviour change: the parent's log must come
        out identical either way."""
        for i in range(3):
            se.append_events(f"w{i}", [
                _text_row(f"hello from {i}"),
                _tool_row(tool_id=f"t{i}", name="Read", file_path=f"/f{i}"),
                _text_row(f"done {i}"),
            ])

        # Per-worker: each feed reads and forwards on its own.
        for f in self._feeds(3):
            await f.drain()
        per_worker = [(r["event_type"], r["payload"]) for r in se.get_events(PARENT)]
        assert per_worker  # the comparison would be vacuous otherwise

        # Consolidated: one group, one batched read, onto a SECOND parent so the
        # two logs can be compared row for row.
        other = PARENT + "-b"
        group = feed_mod.WorkerFeedGroup(other)
        for i in range(3):
            group._feeds[f"w{i}"] = feed_mod.WorkerFeed(
                parent_session_id=other, worker_session_id=f"w{i}",
                label=f"worker-{i}", provider_id="prov-openai",
                model_id="gpt-5.5", prompt="go",
            )
        await group.tick()
        consolidated = [
            (r["event_type"], r["payload"]) for r in se.get_events(other)
        ]

        assert consolidated == per_worker

    async def test_one_query_per_tick_regardless_of_worker_count(self, db):
        """The whole point: N reads per interval become 1."""
        for i in range(20):
            se.append_events(f"w{i}", [_text_row(f"hi {i}")])

        group = feed_mod.WorkerFeedGroup(PARENT)
        for f in self._feeds(20):
            group._feeds[f.worker_session_id] = f

        calls = []
        real_multi = se.get_events_multi

        def _spy_multi(cursors, limit=500):
            calls.append(dict(cursors))
            return real_multi(cursors, limit)

        with patch.object(se, "get_events_multi", _spy_multi), \
                patch.object(se, "get_events",
                             MagicMock(side_effect=AssertionError("per-worker read"))):
            emitted = await group.tick()

        assert len(calls) == 1
        assert len(calls[0]) == 20
        assert emitted == 20

    async def test_per_worker_bounds_are_preserved(self, db):
        """A member still takes at most DRAIN_BATCH rows per inner batch and at
        most MAX_DRAIN_BATCHES batches per tick — the pre-consolidation bound."""
        from app.core.provider_agent_feed import DRAIN_BATCH, MAX_DRAIN_BATCHES

        rows = [_text_row("x") for _ in range(DRAIN_BATCH * (MAX_DRAIN_BATCHES + 2))]
        se.append_events("w0", rows)

        group = feed_mod.WorkerFeedGroup(PARENT)
        feed = self._feeds(1)[0]
        group._feeds["w0"] = feed

        sizes = []
        real_multi = se.get_events_multi

        def _spy(cursors, limit=500):
            out = real_multi(cursors, limit)
            sizes.append(len(out))
            return out

        with patch.object(se, "get_events_multi", _spy):
            await group.tick()

        assert len(sizes) == MAX_DRAIN_BATCHES
        assert all(s <= DRAIN_BATCH for s in sizes)
        # Cursor advanced exactly as far as what was consumed.
        assert feed.cursor == DRAIN_BATCH * MAX_DRAIN_BATCHES

    async def test_rows_are_never_forwarded_twice(self, db):
        """The cursor is filtered synchronously inside ``forward``, so a group
        tick and a self-drain overlapping cannot duplicate a row."""
        se.append_events("w0", [_text_row("only once")])
        group = feed_mod.WorkerFeedGroup(PARENT)
        feed = self._feeds(1)[0]
        group._feeds["w0"] = feed

        await group.tick()
        await feed.drain()
        await group.tick()

        chunks = [r for r in se.get_events(PARENT)
                  if r["event_type"] == "subagent.progress"]
        assert len(chunks) == 1

    async def test_members_share_one_group_and_it_stops_with_the_last(self, db):
        feeds = self._feeds(3)
        for f in feeds:
            f.start_following()

        group = feed_mod.get_feed_group(PARENT)
        assert group is not None and group.size == 3
        assert all(f._group is group for f in feeds)

        for f in feeds[:-1]:
            await f.stop_following(final_drain=False)
        assert feed_mod.get_feed_group(PARENT).size == 1

        await feeds[-1].stop_following(final_drain=False)
        assert feed_mod.get_feed_group(PARENT) is None

    async def test_following_still_forwards_without_an_explicit_drain(self, db, monkeypatch):
        monkeypatch.setattr(feed_mod, "POLL_INTERVAL_SECONDS", 0.01)
        se.append_events("w0", [_text_row("live")])
        feed = self._feeds(1)[0]

        feed.start_following()
        await asyncio.sleep(0.08)
        await feed.stop_following()

        assert any(r["event_type"] == "subagent.progress" for r in se.get_events(PARENT))

    def test_start_following_without_a_loop_is_a_no_op(self, db):
        feed = self._feeds(1)[0]
        feed.start_following()  # no running loop — must not raise
        group = feed_mod.get_feed_group(PARENT)
        assert group is not None and group._task is None

    async def test_a_batched_read_failure_degrades_to_no_progress(self, db):
        group = feed_mod.WorkerFeedGroup(PARENT)
        group._feeds["w0"] = self._feeds(1)[0]

        with patch.object(se, "get_events_multi", side_effect=RuntimeError("db down")):
            assert await group.tick() == 0

        assert se.get_events(PARENT) == []


class TestGetEventsMulti:

    def test_each_session_is_tailed_from_its_own_cursor(self, db):
        se.append_events("a", [_text_row("a1"), _text_row("a2")])
        se.append_events("b", [_text_row("b1"), _text_row("b2")])

        rows = se.get_events_multi({"a": 1, "b": 0}, 100)

        assert [(r["session_id"], r["seq"]) for r in rows] == [
            ("b", 1), ("a", 2), ("b", 2)
        ]

    def test_an_empty_cursor_map_never_touches_the_db(self, db):
        assert se.get_events_multi({}, 100) == []

    def test_payloads_are_parsed_like_the_single_session_read(self, db):
        se.append_events("a", [_text_row("hello")])

        multi = se.get_events_multi({"a": 0}, 100)[0]
        single = se.get_events("a", 0, 100)[0]

        assert multi["payload"] == single["payload"] == {"text": "hello"}


# ---------------------------------------------------------------------------
# 8. Stop-all + attribution
# ---------------------------------------------------------------------------

class TestStopAll:

    async def test_stop_all_kills_every_live_worker(self, db):
        for i in range(5):
            _register_worker(f"w{i}")

        with _kill_spies() as spies:
            result = await sup.stop_all_workers(PARENT)

        assert result["stopped"] == 5
        assert spies["pool"].remove_broken.await_count == 5
        assert sup.live_workers(PARENT) == []
        for i in range(5):
            row = task_registry.get_task(PARENT, f"w{i}")
            assert row["status"] == "killed"
            assert row["reason"] == sup.STOP_ALL_REASON

    async def test_stop_all_with_nothing_live_is_a_no_op(self, db):
        with _kill_spies() as spies:
            result = await sup.stop_all_workers(PARENT)

        assert result == {"stopped": 0, "workers": []}
        spies["pool"].remove_broken.assert_not_awaited()

    async def test_stop_all_reports_what_it_stopped(self, db):
        _register_worker("w1", provider="prov-codex", model="gpt-5.5-codex")

        with _kill_spies():
            result = await sup.stop_all_workers(PARENT)

        assert result["workers"] == [{
            "workerSessionId": "w1",
            "taskId": "w1",
            "provider": "prov-codex",
            "model": "gpt-5.5-codex",
            "outcome": "killed",
        }]

    async def test_an_eviction_failure_still_completes_the_teardown(self, db):
        """A dead pool must not strand the registry row or the supervisor."""
        _register_worker("w1")

        with _kill_spies(evicted=False):
            result = await sup.stop_all_workers(PARENT)

        assert result["stopped"] == 1
        assert result["workers"][0]["outcome"] == "interrupted"
        assert task_registry.get_task(PARENT, "w1")["status"] == "killed"
        assert sup.live_workers(PARENT) == []


class TestKilledWorkersReleaseTheirPrompts:
    """A killed worker's approval prompts live on the PARENT's session id, so
    killing the worker's own session does not touch them: without the sweep they
    stay on the user's screen — asked by a worker that no longer exists — until
    the 5-minute TTL, and the worker's detached waiter stays blocked."""

    @staticmethod
    def _worker_prompt(worker_session_id, request_id):
        from app.db import interactions

        return interactions.create_interaction(
            PARENT, "permission",
            {
                "request_id": request_id,
                "tool_name": "Bash",
                "tool_input": {"command": "ls"},
                "provider_agent": {
                    "worker_session_id": worker_session_id,
                    "parent_session_id": PARENT,
                    "label": "reviewer",
                },
            },
            300,
        )

    @staticmethod
    def _state(interaction_id):
        from app.db import interactions

        return interactions.get_interaction(interaction_id)["state"]

    async def test_turn_end_teardown_sweeps_the_prompts(self, db, monkeypatch):
        from app.core import interaction_wait

        monkeypatch.setattr(interaction_wait, "notify_changed", lambda sid: None)
        turn = turns_db.enqueue_turn(PARENT, "user", prompt="hi")
        _register_worker("w1")
        _register_worker("w2")
        mine = self._worker_prompt("w1", "r-1")
        sibling = self._worker_prompt("w2", "r-2")

        with _runtime_env(), _kill_spies():
            await session_runtime.SessionRuntime(turn).execute()

        # Both workers died at turn end, so both prompts go — but each through
        # its OWN teardown, which is what keeps a surviving sibling's prompt
        # alive on any other kill path.
        assert self._state(mine["id"]) == "cancelled"
        assert self._state(sibling["id"]) == "cancelled"

    async def test_stop_all_sweeps_the_prompts(self, db, monkeypatch):
        from app.core import interaction_wait

        monkeypatch.setattr(interaction_wait, "notify_changed", lambda sid: None)
        _register_worker("w1")
        row = self._worker_prompt("w1", "r-1")

        with _kill_spies():
            await sup.stop_all_workers(PARENT)

        assert self._state(row["id"]) == "cancelled"

    async def test_a_surviving_workers_prompt_is_left_alone(self, db, monkeypatch):
        """Only the worker being killed loses its prompts."""
        from app.core import interaction_wait

        monkeypatch.setattr(interaction_wait, "notify_changed", lambda sid: None)
        doomed = _register_worker("w1")
        _register_worker("w2")
        mine = self._worker_prompt("w1", "r-1")
        sibling = self._worker_prompt("w2", "r-2")

        with _kill_spies():
            await sup.teardown_worker(
                doomed, reason=sup.STOP_ALL_REASON, summary="s", preview="p",
            )

        assert self._state(mine["id"]) == "cancelled"
        assert self._state(sibling["id"]) == "pending"

    async def test_a_sweep_failure_never_breaks_the_teardown(self, db, monkeypatch):
        from app.core import provider_agent_approvals as approvals

        def _boom(worker, parent):
            raise RuntimeError("db down")

        monkeypatch.setattr(approvals, "cancel_worker_prompts", _boom)
        _register_worker("w1")

        with _kill_spies():
            result = await sup.stop_all_workers(PARENT)

        assert result["stopped"] == 1
        assert task_registry.get_task(PARENT, "w1")["status"] == "killed"


class TestTeardownReasonIsLatched:
    """``torn_down_reason`` is what lets ``dispatch_worker`` say WHY a worker
    failed instead of deriving a false success from an empty event log."""

    async def test_turn_end_latches_the_turn_end_reason(self, db):
        turn = turns_db.enqueue_turn(PARENT, "user", prompt="hi")
        record = _register_worker("w1")

        with _runtime_env(), _kill_spies():
            await session_runtime.SessionRuntime(turn).execute()

        assert record.torn_down is True
        assert record.torn_down_reason == sup.TURN_END_REASON

    async def test_stop_all_latches_the_stop_all_reason(self, db):
        record = _register_worker("w1")

        with _kill_spies():
            await sup.stop_all_workers(PARENT)

        assert record.torn_down is True
        assert record.torn_down_reason == sup.STOP_ALL_REASON

    async def test_an_untouched_worker_has_no_reason(self, db):
        record = _register_worker("w1")

        assert record.torn_down is False
        assert record.torn_down_reason is None


class TestAttribution:

    def test_usage_is_grouped_by_provider(self):
        _register_worker("w1", provider="prov-openai", model="gpt-5.5")
        _register_worker("w2", provider="prov-openai", model="gpt-5.5-mini")
        _register_worker("w3", provider="prov-codex", model="gpt-5.5-codex")
        sup.record_usage(PARENT, "w1", {"tokens_in": 100, "tokens_out": 20,
                                        "total_cost_usd": 0.5})
        sup.record_usage(PARENT, "w2", {"tokens_in": 50, "tokens_out": 5,
                                        "total_cost_usd": 0.25})
        sup.record_usage(PARENT, "w3", {"tokens_in": 10, "tokens_out": 1,
                                        "total_cost_usd": 0.1})

        report = sup.attribution(PARENT)

        assert report["totals"]["workers"] == 3
        assert report["totals"]["tokens_in"] == 160
        assert report["totals"]["total_cost_usd"] == pytest.approx(0.85)
        assert report["providers"]["prov-openai"]["workers"] == 2
        assert report["providers"]["prov-openai"]["tokens_in"] == 150
        assert report["providers"]["prov-codex"]["tokens_in"] == 10

    def test_a_snapshot_seen_twice_does_not_double_count(self):
        """Usage arrives as a WHOLE-turn snapshot (the feed's live capture and
        ``dispatch_worker``'s final read see the same numbers)."""
        _register_worker("w1")
        sup.record_usage(PARENT, "w1", {"tokens_in": 100})
        sup.record_usage(PARENT, "w1", {"tokens_in": 100})

        assert sup.attribution(PARENT)["totals"]["tokens_in"] == 100

    def test_attribution_is_readable_while_the_turn_runs(self):
        _register_worker("w1")
        sup.record_usage(PARENT, "w1", {"tokens_in": 7})

        report = sup.attribution(PARENT)

        assert sup.has_live_workers(PARENT) is True
        assert report["workers"][0]["usage"]["tokens_in"] == 7
        assert report["workers"][0]["provider"] == "prov-openai"

    def test_nothing_live_reports_empty_rather_than_absent(self):
        report = sup.attribution(PARENT)

        assert report["workers"] == []
        assert report["totals"]["workers"] == 0
        assert sup.has_live_workers(PARENT) is False

    def test_usage_for_an_unknown_worker_is_ignored(self):
        sup.record_usage(PARENT, "ghost", {"tokens_in": 999})

        assert sup.attribution(PARENT)["totals"]["tokens_in"] == 0

    async def test_the_feed_mirrors_usage_while_the_worker_runs(self, db):
        """Live attribution: the group poller sees the worker's turn.completed
        and records its usage without waiting for the tool call to return."""
        feed = feed_mod.WorkerFeed(
            parent_session_id=PARENT, worker_session_id="w1",
            provider_id="prov-openai", model_id="gpt-5.5", prompt="go",
        )
        _register_worker("w1", feed=feed)
        se.append_events("w1", [
            _text_row("working"),
            {"type": "turn.completed",
             "payload": {"usage": {"tokens_in": 42, "tokens_out": 8}}},
        ])

        await feed.drain()

        assert sup.attribution(PARENT)["totals"]["tokens_in"] == 42
