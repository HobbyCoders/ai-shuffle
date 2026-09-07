"""Worker start-up, ownership and end-of-turn reporting (2026-09-05 audit).

Five defects, one lifecycle:

* **A** a worker whose provider connection never opened sat "started" for
  ~900s before anyone was told — ten of them across seven sessions, about 2.7
  hours of parked time. It is now failed at a first-frame deadline and
  re-dispatched once.
* **B** a ``mode='sync'`` Task blocked its parent turn for 15m03s and 19m28s on
  the same zero-frame failure. It now has a hard cap and returns.
* **C** eleven workers died on one org's 500k tokens-per-minute ceiling.
  Capacity refusals are now rescheduled, and admission counts workers per
  provider credential — over the cap a dispatch queues instead of failing.
* **D** a worker's worker was registered, chipped, woken and steered on the
  ROOT session, which never dispatched it; the owner meanwhile ended its turn
  waiting for a wake that went somewhere else.
* **E** a worker that ended its turn with a Monitor still running was booked
  ``completed`` with its interim sentence as the whole report.

Nothing here spawns a session or touches a network: ``turn_manager`` and the DB
layer are stubbed exactly as in ``test_provider_agent``. The task registry is
the real one — its ownership predicates are the thing under test.
"""

import asyncio
from unittest.mock import patch

import pytest

from tests.conftest import async_twin

import app.core.task_dispatch as td
from app.core import provider_agent as pa
from app.core import provider_agent_supervisor as sup
from app.core.provider_agent import WorkerOutcome
from app.core.session_pool import AdmissionSnapshot
from app.core.task_registry import task_registry


# ---------------------------------------------------------------------------
# provider_agent-level fixtures
# ---------------------------------------------------------------------------

class _FakePool:
    """A pool with room; only the provider ceiling can hold anything back."""

    def admission_snapshot(self, lane="delegated"):
        return AdmissionSnapshot(
            refusal=None, in_pool=0, hard_cap=8, delegated_in_pool=0,
            native_in_pool=0, lane=lane,
        )

    def peek(self, session_id):
        return None


@pytest.fixture(autouse=True)
def _roomy_pool(monkeypatch):
    """Never let a real ``admission_snapshot`` (psutil, process walks) run here.

    Every ceiling under test in this module is a task-dispatch one; the pool's
    own guards are covered in ``test_task_dispatch_admission``.
    """
    from app.core import session_pool

    monkeypatch.setattr(session_pool, "_session_pool", _FakePool(), raising=False)


class _FakeTurnManager:
    def __init__(self):
        self.submitted = []
        self.interrupts = []
        #: Set by a test to stall ``wait_session_idle`` forever.
        self.hang = False
        #: Called with the worker's session id on the FIRST idle wait — the
        #: only hook a test has for "the worker's turn is under way", which is
        #: where a worker would start a Monitor of its own.
        self.on_wait = None
        self.waits = 0
        self._waited = False

    def submit(self, session_id, source, **kwargs):
        self.submitted.append({"session_id": session_id, "source": source})
        return "turn-1"

    async def wait_session_idle(self, session_id, timeout=300.0):
        self.waits += 1
        if self.on_wait is not None and not self._waited:
            self._waited = True
            self.on_wait(session_id)
        if self.hang:
            await asyncio.sleep(3600)
        return True

    def interrupt(self, session_id, mode, **kwargs):
        self.interrupts.append((session_id, mode))
        return None


@pytest.fixture
def worker_env(monkeypatch, tmp_path):
    """Stub every out-of-process dependency of ``dispatch_worker``."""
    from app.core import project_resolver
    from app.core import turn_manager as tm_mod
    from app.core import turn_scheduler as ts_mod
    from app.db import database as db

    fake_tm = _FakeTurnManager()
    checkout = tmp_path / "repo"
    checkout.mkdir(parents=True)
    captured_wakes = []

    async def _on_task_terminal(session_id, frame):
        captured_wakes.append((session_id, dict(frame)))
        return None

    monkeypatch.setattr(ts_mod, "on_task_terminal", _on_task_terminal)
    monkeypatch.setattr(tm_mod, "submit", fake_tm.submit)
    monkeypatch.setattr(tm_mod, "submit_async", async_twin(fake_tm.submit))
    monkeypatch.setattr(tm_mod, "wait_session_idle", fake_tm.wait_session_idle)
    monkeypatch.setattr(tm_mod, "interrupt", fake_tm.interrupt)
    monkeypatch.setattr(tm_mod, "interrupt_async", async_twin(fake_tm.interrupt))
    monkeypatch.setattr(db, "update_session", lambda session_id, **kw: None)
    monkeypatch.setattr(
        db, "get_session", lambda sid: {"id": sid, "worktree_path": str(checkout)}
    )
    monkeypatch.setattr(db, "create_session", lambda **kw: {"id": kw["session_id"]})
    monkeypatch.setattr(
        db, "set_session_branch", lambda session_id, branch, project_v2_id=None: None
    )
    monkeypatch.setattr(
        project_resolver, "resolve_existing_cwd", lambda row: str(checkout)
    )
    monkeypatch.setattr(
        pa, "final_turn_outcome",
        lambda sid: {
            "usage": {}, "error": None, "interrupted": False, "terminal_found": True,
        },
    )
    monkeypatch.setattr(pa, "final_assistant_text", lambda sid: "the report")
    monkeypatch.setattr(pa, "WAIT_SLICE_SECONDS", 0.01)
    monkeypatch.setattr(pa, "PARK_RECHECK_SECONDS", 0.01)

    class _Env:
        tm = fake_tm
        cwd = str(checkout)
        #: ``(session_id, frame)`` for every ``turn_scheduler.on_task_terminal``
        #: call — i.e. every parent this dispatch actually woke.
        wakes = captured_wakes

    yield _Env()

    for sid in ("root-1", "owner-1", "parent-1"):
        task_registry.clear_session(sid)
    sup.reset()
    pa.reset_worker_sessions()


def _run(coro):
    return asyncio.run(coro)


# ===========================================================================
# A — the first-frame watchdog
# ===========================================================================

class TestFirstFrameWatchdog:
    """``frames_forwarded == 0`` is the harness saying "it never connected"."""

    def test_a_worker_that_streams_nothing_is_failed_at_the_deadline(
        self, worker_env, mock_db
    ):
        worker_env.tm.hang = True

        outcome = _run(pa.dispatch_worker(
            parent_session_id="parent-1",
            profile_id="p1",
            prompt="go",
            label="silent",
            first_frame_seconds=0.05,
        ))

        assert outcome.ok is False
        assert outcome.frames_forwarded == 0
        assert outcome.killed is False
        assert "forwarded no output" in outcome.error
        # The registry row carries the wording a parent can act on — the same
        # "worker failed to start" prefix the audit's stalled workers only got
        # fifteen minutes late.
        record = task_registry.get_task("parent-1", outcome.session_id)
        assert record["status"] == "error"
        assert record["summary"].startswith("worker failed to start:")
        # And the wedged turn is stopped rather than left running.
        assert worker_env.tm.interrupts == [(outcome.session_id, "stop")]

    def test_a_working_worker_is_never_bounded_by_it(self, worker_env, mock_db):
        """One frame retires the deadline; a slow worker is not a dead one."""
        seen = {}

        async def _wait(session_id, timeout=300.0):
            seen["timeout"] = timeout
            return True

        from app.core import turn_manager as tm_mod

        with patch.object(tm_mod, "wait_session_idle", _wait):
            outcome = _run(pa.dispatch_worker(
                parent_session_id="parent-1",
                profile_id="p1",
                prompt="go",
                first_frame_seconds=0.05,
            ))

        assert outcome.ok is True
        # The underlying idle wait still gets no ceiling of its own.
        assert seen["timeout"] == float("inf")

    def test_the_deadline_can_be_disabled(self, worker_env, mock_db, monkeypatch):
        from app.core import config as cfg

        monkeypatch.setattr(
            cfg.settings, "task_worker_first_frame_seconds", 0, raising=False
        )
        first, hard = pa._worker_deadlines(True, None, None)
        assert first == 0.0 and hard == 0.0


class TestZeroFrameRedispatch:
    """A worker that never started is re-dispatched with the same brief."""

    @pytest.fixture(autouse=True)
    def _clean(self):
        td._PENDING_ADMISSIONS.clear()
        td._ADMISSION_LOCK = None
        yield
        td._PENDING_ADMISSIONS.clear()
        td._ADMISSION_LOCK = None

    @staticmethod
    async def _run_retries(dispatcher, *, background=True, label="brief", **over):
        kwargs = dict(
            parent_session_id="root-1",
            profile_id="p1",
            prompt="the brief",
            label=label,
            worker_session_id="w-1",
        )
        return await td._dispatch_worker_with_retries(
            kwargs=kwargs,
            background=background,
            lane="native",
            provider_id=None,
            parent_session_id="root-1",
            dispatcher=dispatcher,
            **over,
        )

    def test_a_zero_frame_failure_is_re_dispatched_exactly_once(self, monkeypatch):
        from app.core import config as cfg

        monkeypatch.setattr(cfg.settings, "task_worker_start_retries", 1, raising=False)
        monkeypatch.setattr(td, "_START_RETRY_BACKOFF_SECONDS", 0.01)
        calls = []

        async def dispatcher(**kwargs):
            calls.append(kwargs)
            return WorkerOutcome(
                ok=False,
                session_id=kwargs["worker_session_id"],
                error="RemoteProtocolError: peer closed connection",
                frames_forwarded=0,
            )

        outcome = _run(self._run_retries(dispatcher))

        assert outcome.ok is False
        assert len(calls) == 2, "one retry, not a loop"
        # Same brief, new worker session, and the retry is visible as its own
        # task row rather than silently reusing the first attempt's card.
        assert calls[0]["prompt"] == calls[1]["prompt"] == "the brief"
        assert calls[0]["worker_session_id"] != calls[1]["worker_session_id"]
        assert calls[1]["label"] == "brief · retry 1"
        assert calls[1]["launch_tool_use_id"] is None

    def test_the_retry_gets_double_the_first_frame_leash(self, monkeypatch):
        """A late first token and a dead connection look identical from here.

        So the retry is given twice the deadline: a slow reasoning model
        survives it, a connection that never opens fails both.
        """
        from app.core import config as cfg

        monkeypatch.setattr(cfg.settings, "task_worker_start_retries", 1, raising=False)
        monkeypatch.setattr(
            cfg.settings, "task_worker_first_frame_seconds", 90.0, raising=False
        )
        monkeypatch.setattr(td, "_START_RETRY_BACKOFF_SECONDS", 0.01)
        calls = []

        async def dispatcher(**kwargs):
            calls.append(kwargs)
            return WorkerOutcome(
                ok=False,
                session_id=kwargs["worker_session_id"],
                error="The worker forwarded no output within 90s.",
                frames_forwarded=0,
            )

        _run(self._run_retries(dispatcher))

        assert calls[0].get("first_frame_seconds") is None, "attempt 1 takes the default"
        assert calls[1]["first_frame_seconds"] == 180.0

    def test_the_doomed_attempts_wake_is_suppressed(self, monkeypatch):
        """Only the attempt that settles the dispatch spends a parent turn."""
        from app.core import config as cfg

        monkeypatch.setattr(cfg.settings, "task_worker_start_retries", 1, raising=False)
        monkeypatch.setattr(td, "_START_RETRY_BACKOFF_SECONDS", 0.01)
        verdicts = []

        async def dispatcher(**kwargs):
            predicate = kwargs["suppress_caller_wake"]
            # This is what ``dispatch_worker``'s ``finally`` asks.
            verdicts.append(predicate("error", "connection reset", 0))
            return WorkerOutcome(
                ok=False,
                session_id=kwargs["worker_session_id"],
                error="connection reset",
                frames_forwarded=0,
            )

        _run(self._run_retries(dispatcher))

        assert verdicts == [True, False], (
            "the first attempt is about to be retried, the second is final"
        )

    def test_a_worker_that_ran_is_never_re_dispatched(self, monkeypatch):
        from app.core import config as cfg

        monkeypatch.setattr(cfg.settings, "task_worker_start_retries", 1, raising=False)
        calls = []

        async def dispatcher(**kwargs):
            calls.append(kwargs)
            return WorkerOutcome(
                ok=False,
                session_id="w-1",
                error="the model gave up half way",
                frames_forwarded=17,
            )

        outcome = _run(self._run_retries(dispatcher))

        assert outcome.ok is False
        assert len(calls) == 1, "repeating real work is not free"

    def test_a_torn_down_worker_is_never_re_dispatched(self, monkeypatch):
        from app.core import config as cfg

        monkeypatch.setattr(cfg.settings, "task_worker_start_retries", 1, raising=False)
        calls = []

        async def dispatcher(**kwargs):
            calls.append(kwargs)
            return WorkerOutcome(
                ok=False,
                session_id="w-1",
                error="The worker was stopped (stop-all) before it finished.",
                frames_forwarded=0,
                killed=True,
            )

        _run(self._run_retries(dispatcher))
        assert len(calls) == 1, "a Stop is the answer, not a transport hiccup"

    def test_a_hopeless_start_failure_is_never_re_dispatched(self, monkeypatch):
        from app.core import config as cfg

        monkeypatch.setattr(cfg.settings, "task_worker_start_retries", 1, raising=False)
        calls = []

        async def dispatcher(**kwargs):
            calls.append(kwargs)
            return WorkerOutcome(
                ok=False,
                session_id="w-1",
                error=(
                    "Model provider 'OpenAI' has no credentials configured, so "
                    "the worker would fail at connect."
                ),
                frames_forwarded=0,
            )

        _run(self._run_retries(dispatcher))
        assert len(calls) == 1

    def test_the_sync_lane_does_not_silently_retry(self, monkeypatch):
        """B: a live caller gets the failure to act on, not another block."""
        from app.core import config as cfg

        monkeypatch.setattr(cfg.settings, "task_worker_start_retries", 1, raising=False)
        calls = []

        async def dispatcher(**kwargs):
            calls.append(kwargs)
            return WorkerOutcome(
                ok=False, session_id="w-1", error="connection reset",
                frames_forwarded=0,
            )

        _run(self._run_retries(dispatcher, background=False))
        assert len(calls) == 1


# ===========================================================================
# B — the synchronous hard cap
# ===========================================================================

class TestSyncCap:

    def test_a_sync_worker_that_never_finishes_returns_instead_of_blocking(
        self, worker_env, mock_db
    ):
        worker_env.tm.hang = True

        outcome = _run(pa.dispatch_worker(
            parent_session_id="parent-1",
            profile_id="p1",
            prompt="go",
            background=False,
            # No first-frame watchdog, so only the wall-clock cap can end this.
            first_frame_seconds=0,
            max_wall_seconds=0.05,
        ))

        assert outcome.ok is False
        assert "did not finish within the" in outcome.error
        assert "in the background" in outcome.error
        assert worker_env.tm.interrupts == [(outcome.session_id, "stop")]

    def test_background_gets_no_hard_cap_by_default(self):
        first, hard = pa._worker_deadlines(True, None, None)
        assert hard == 0.0, "a parked parent can be woken; it needs no ceiling"
        assert first > 0

    def test_sync_gets_one(self):
        first, hard = pa._worker_deadlines(False, None, None)
        assert hard > 0
        assert first > 0

    def test_the_cap_bounds_the_retries_too(self, monkeypatch):
        """A "capacity, retrying" loop must not become the unbounded block."""
        import time as _time

        calls = []

        async def dispatcher(**kwargs):
            calls.append(kwargs)
            return WorkerOutcome(
                ok=False,
                session_id="w-1",
                error="Rate limit reached ... Please try again in 30s.",
                frames_forwarded=0,
            )

        from app.core import config as cfg

        monkeypatch.setattr(
            cfg.settings, "task_worker_rate_limit_retries", 5, raising=False
        )
        outcome = _run(td._dispatch_worker_with_retries(
            kwargs=dict(
                parent_session_id="root-1", profile_id="p1", prompt="go",
                label="x", worker_session_id="w-1",
            ),
            background=False,
            lane="native",
            provider_id=None,
            parent_session_id="root-1",
            dispatcher=dispatcher,
            overall_deadline=_time.monotonic() + 0.2,
        ))

        assert outcome.ok is False
        assert len(calls) == 1, "the 30s backoff does not fit inside the cap"


# ===========================================================================
# C — provider capacity
# ===========================================================================

class TestRateLimitParsing:

    @pytest.mark.parametrize("text,expected", [
        (
            "Rate limit reached for gpt-5.6-luna in organization org-JTwK2 on "
            "tokens per min (TPM): Limit 500000, Used 478023, Requested 30722. "
            "Please try again in 1.049s.",
            1.049,
        ),
        ("Rate limit reached ... Please try again in 980ms.", 0.98),
        ("429 Too Many Requests {'retry-after': '7'}", 7.0),
    ])
    def test_the_providers_own_hint_is_used(self, text, expected):
        assert pa.rate_limit_retry_after(text) == pytest.approx(expected)

    def test_a_transport_failure_is_not_a_capacity_refusal(self):
        text = "RemoteProtocolError: peer closed connection without sending body"
        assert pa.looks_rate_limited(text) is False
        assert pa.rate_limit_retry_after(text) is None

    def test_a_capacity_refusal_with_no_hint_is_still_capacity(self):
        assert pa.looks_rate_limited("Rate limit reached for gpt-5.6-luna") is True
        assert pa.rate_limit_retry_after("Rate limit reached for gpt-5.6-luna") is None

    def test_an_absurd_hint_is_refused(self):
        assert pa.rate_limit_retry_after(
            "Rate limit reached. Please try again in 4000s."
        ) is None


class TestRateLimitReschedule:

    @pytest.fixture(autouse=True)
    def _clean(self):
        td._PENDING_ADMISSIONS.clear()
        td._ADMISSION_LOCK = None
        yield
        td._PENDING_ADMISSIONS.clear()
        td._ADMISSION_LOCK = None

    def test_a_429_at_connect_is_rescheduled_with_backoff(self, monkeypatch):
        from app.core import config as cfg

        monkeypatch.setattr(
            cfg.settings, "task_worker_rate_limit_retries", 3, raising=False
        )
        monkeypatch.setattr(
            cfg.settings, "task_worker_rate_limit_backoff_cap_seconds", 0.2,
            raising=False,
        )
        monkeypatch.setattr(cfg.settings, "task_worker_start_retries", 0, raising=False)
        slept = []
        calls = []
        real_sleep = asyncio.sleep

        async def _sleep(delay, *a, **kw):
            slept.append(delay)
            return await real_sleep(0)

        monkeypatch.setattr(td.asyncio, "sleep", _sleep)

        async def dispatcher(**kwargs):
            calls.append(kwargs)
            if len(calls) < 3:
                return WorkerOutcome(
                    ok=False,
                    session_id=kwargs["worker_session_id"],
                    error=(
                        "Rate limit reached for gpt-5.6-luna in organization "
                        "org-JTwK2 on tokens per min (TPM): Limit 500000, Used "
                        "494784, Requested 16743. Please try again in 1.383s."
                    ),
                    frames_forwarded=0,
                )
            return WorkerOutcome(ok=True, session_id=kwargs["worker_session_id"])

        outcome = _run(td._dispatch_worker_with_retries(
            kwargs=dict(
                parent_session_id="root-1", profile_id="p1", prompt="go",
                label="research", worker_session_id="w-1",
            ),
            background=True,
            lane="native",
            provider_id=None,
            parent_session_id="root-1",
            dispatcher=dispatcher,
        ))

        assert outcome.ok is True, "a rescheduled worker eventually runs"
        assert len(calls) == 3
        # Backoff is seeded from the provider's own 1.383s and doubles, clamped
        # at the configured cap.
        assert slept == [pytest.approx(0.2), pytest.approx(0.2)]
        assert calls[1]["label"] == "research · retry 1"
        assert calls[2]["label"] == "research · retry 2"

    def test_the_reschedule_budget_is_bounded(self, monkeypatch):
        from app.core import config as cfg

        monkeypatch.setattr(
            cfg.settings, "task_worker_rate_limit_retries", 2, raising=False
        )
        monkeypatch.setattr(
            cfg.settings, "task_worker_rate_limit_backoff_cap_seconds", 0.01,
            raising=False,
        )
        calls = []

        async def dispatcher(**kwargs):
            calls.append(kwargs)
            return WorkerOutcome(
                ok=False,
                session_id=kwargs["worker_session_id"],
                error="Rate limit reached. Please try again in 1s.",
                frames_forwarded=0,
            )

        outcome = _run(td._dispatch_worker_with_retries(
            kwargs=dict(
                parent_session_id="root-1", profile_id="p1", prompt="go",
                label="x", worker_session_id="w-1",
            ),
            background=True,
            lane="native",
            provider_id=None,
            parent_session_id="root-1",
            dispatcher=dispatcher,
        ))

        assert outcome.ok is False
        assert len(calls) == 3, "the original plus two reschedules, then stop"


class TestProviderOrgAdmission:
    """C: session ceilings say nothing about a credential's token budget."""

    @pytest.fixture(autouse=True)
    def _clean(self):
        td._PENDING_ADMISSIONS.clear()
        td._ADMISSION_LOCK = None
        yield
        td._PENDING_ADMISSIONS.clear()
        td._ADMISSION_LOCK = None
        sup.reset()

    @staticmethod
    def _register(worker_id, provider_id):
        sup.register_worker(sup.WorkerRecord(
            parent_session_id="root-1",
            worker_session_id=worker_id,
            task_id=worker_id,
            tool_use_id=worker_id,
            provider_id=provider_id,
        ))

    def test_workers_are_counted_per_provider_row(self, monkeypatch):
        self._register("w-a", "prov-openai")
        self._register("w-b", "prov-openai")
        self._register("w-c", "prov-anthropic")

        assert td._live_worker_count_for_provider("prov-openai") == 2
        assert td._live_worker_count_for_provider("prov-anthropic") == 1
        assert td._live_worker_count_for_provider(None) == 0

    def test_a_dispatch_over_the_cap_queues_and_is_then_admitted(
        self, monkeypatch
    ):
        from app.core import config as cfg

        monkeypatch.setattr(
            cfg.settings, "task_worker_max_per_provider", 2, raising=False
        )
        monkeypatch.setattr(
            cfg.settings, "task_worker_provider_queue_seconds", 30.0, raising=False
        )
        monkeypatch.setattr(
            cfg.settings, "subagent_max_concurrent_per_session", 0, raising=False
        )
        monkeypatch.setattr(td, "_ADMISSION_POLL_SECONDS", 0.01)
        self._register("w-a", "prov-openai")
        self._register("w-b", "prov-openai")

        async def scenario():
            waiter = asyncio.ensure_future(td._reserve_admission(
                "w-new", lane="native", parent_session_id="root-1",
                provider_id="prov-openai",
            ))
            await asyncio.sleep(0.05)
            assert not waiter.done(), "the third worker must WAIT, not fail"
            # A sibling settles; the queued dispatch takes its slot.
            sup.unregister_worker("root-1", "w-a")
            return await asyncio.wait_for(waiter, timeout=5)

        refusal, queued = _run(scenario())

        assert refusal is None, "queued, never refused"
        assert queued > 0, "and the wait is reported so the model can see it"
        assert td._PENDING_ADMISSIONS["w-new"].provider == "prov-openai"

    def test_the_cap_refuses_only_after_the_long_queue_budget(self, monkeypatch):
        from app.core import config as cfg

        monkeypatch.setattr(
            cfg.settings, "task_worker_max_per_provider", 1, raising=False
        )
        monkeypatch.setattr(
            cfg.settings, "task_worker_provider_queue_seconds", 0.05, raising=False
        )
        monkeypatch.setattr(
            cfg.settings, "subagent_max_concurrent_per_session", 0, raising=False
        )
        monkeypatch.setattr(td, "_ADMISSION_POLL_SECONDS", 0.01)
        # The provider queue never refuses FASTER than the general admission
        # wait (it is a max of the two), so both have to be short here.
        monkeypatch.setattr(td, "_ADMISSION_WAIT_SECONDS", 0.05)
        self._register("w-a", "prov-openai")

        refusal, _queued = _run(td._reserve_admission(
            "w-new", lane="native", parent_session_id="root-1",
            provider_id="prov-openai",
        ))

        assert refusal is not None
        assert "Shuffle's configured TASK_WORKER_MAX_PER_PROVIDER limit 1" in refusal
        assert "token-per-minute" not in refusal

    def test_the_delegated_claude_leg_is_exempt(self, monkeypatch):
        from app.core import config as cfg

        monkeypatch.setattr(
            cfg.settings, "task_worker_max_per_provider", 1, raising=False
        )
        monkeypatch.setattr(
            cfg.settings, "subagent_max_concurrent_per_session", 0, raising=False
        )
        self._register("w-a", "")
        self._register("w-b", "")

        refusal, _queued = _run(td._reserve_admission(
            "w-new", lane="native", parent_session_id="root-1", provider_id=None,
        ))

        assert refusal is None


# ===========================================================================
# D — nested worker ownership
# ===========================================================================

class TestNestedWorkerOwnership:
    """The root routes the card; the DISPATCHER owns the notification lanes."""

    def _dispatch_nested(self, background=True):
        return _run(pa.dispatch_worker(
            parent_session_id="root-1",       # routing parent (the human's UI)
            context_session_id="owner-1",     # the worker that called Task
            profile_id="p1",
            prompt="research this",
            label="nested research",
            background=background,
        ))

    def test_the_root_is_not_woken_for_a_worker_it_never_dispatched(
        self, worker_env, mock_db
    ):
        outcome = self._dispatch_nested()

        assert outcome.ok is True
        # The wake goes to the owner...
        wake_sessions = [sid for sid, _frame in worker_env.wakes]
        assert wake_sessions == ["owner-1"]
        # ...and the root fails the ownership gate that ``on_task_terminal``
        # already suppresses on, so even a stray frame could not wake it.
        assert task_registry.is_own_background_task("root-1", outcome.session_id) is False
        assert task_registry.is_own_background_task("owner-1", outcome.session_id) is True

    def test_the_root_still_renders_the_card(self, worker_env, mock_db):
        outcome = self._dispatch_nested()

        root_row = task_registry.get_task("root-1", outcome.session_id)
        assert root_row is not None and root_row["status"] == "completed"
        assert root_row["subject"] == "nested research"

    def test_the_owner_gets_its_own_row_and_it_settles(self, worker_env, mock_db):
        outcome = self._dispatch_nested()

        owner_row = task_registry.get_task("owner-1", outcome.session_id)
        assert owner_row is not None
        assert owner_row["status"] == "completed"
        # Nothing is left holding the owner's park gate.
        assert task_registry.has_unresolved_background_launches("owner-1") is False

    def test_a_depth_one_dispatch_is_unchanged(self, worker_env, mock_db):
        outcome = _run(pa.dispatch_worker(
            parent_session_id="root-1",
            context_session_id="root-1",
            profile_id="p1",
            prompt="go",
            background=True,
        ))

        assert [sid for sid, _f in worker_env.wakes] == ["root-1"]
        assert task_registry.is_own_background_task("root-1", outcome.session_id) is True

    def test_the_owners_park_gate_is_armed_while_the_child_runs(
        self, worker_env, mock_db
    ):
        """The half of D that stranded a worker mid-file.

        The owner's park gate is what makes it PARK instead of ending its turn
        while its research child is still running; arming the root's instead is
        why one audited worker was terminated with
        ``app/core/composer_suggestions.py`` missing two function definitions.
        """
        seen = {}

        def _snapshot(_sid):
            seen["owner_launch"] = (
                task_registry.has_unresolved_background_launches("owner-1")
            )
            seen["root_launch"] = (
                task_registry.has_unresolved_background_launches("root-1")
            )
            seen["owner_active"] = [
                r["id"] for r in task_registry.get_active("owner-1")
            ]

        worker_env.tm.on_wait = _snapshot

        outcome = self._dispatch_nested()

        assert seen["owner_launch"] is True, "the dispatching worker must park"
        assert seen["root_launch"] is False, "the root never launched this"
        assert seen["owner_active"] == [outcome.session_id]

    def test_the_root_keeps_the_worker_budget(self, worker_env, mock_db):
        """A worker's worker is still a worker on this box (documented D)."""
        sup.register_worker(sup.WorkerRecord(
            parent_session_id="root-1",
            worker_session_id="nested-1",
            task_id="nested-1",
            tool_use_id="nested-1",
        ))
        assert td._live_worker_count_for_root("root-1") == 1


# ===========================================================================
# E — a worker may not end with its own work still running
# ===========================================================================

class TestWorkerEndsWithOutstandingWork:

    def test_an_outstanding_monitor_fails_the_worker(
        self, worker_env, mock_db, monkeypatch
    ):
        from app.core import config as cfg

        monkeypatch.setattr(
            cfg.settings, "task_worker_background_settle_seconds", 0.05, raising=False
        )
        # The worker starts a Monitor and then ends its turn without waiting
        # for it. Its session id is allocated by the dispatch, so the shell is
        # registered from the idle-wait hook.
        worker_env.tm.on_wait = lambda sid: task_registry.start_task(
            sid, "mon-1", "shell", "pytest tests/core"
        )
        monkeypatch.setattr(
            pa, "final_assistant_text",
            lambda sid: "Baseline tests are running. No files changed yet.",
        )
        from app.mcp import builtin_tools
        stopped = []
        async def stop_owned_shell(task_id, session_id, **kwargs):
            stopped.append((session_id, task_id))
            task_registry.complete_task(session_id, task_id, "killed")
            return {"running": False}
        monkeypatch.setattr(builtin_tools, "stop_background_shell_task", stop_owned_shell)

        outcome = _run(pa.dispatch_worker(
            parent_session_id="parent-1",
            profile_id="p1",
            prompt="go",
            background=True,
        ))

        assert outcome.ok is False, "an interim note is not a result"
        assert "worker ended with background work outstanding" in outcome.error
        assert "mon-1" in outcome.error
        record = task_registry.get_task("parent-1", outcome.session_id)
        assert record["status"] == "error"
        assert record["status"] != "completed"
        # The abandoned Monitor row is settled — nothing will ever read it.
        monitor = task_registry.get_task(outcome.session_id, "mon-1")
        assert monitor["status"] == "killed"
        assert stopped == [(outcome.session_id, "mon-1")]
        task_registry.clear_session(outcome.session_id)

    def test_work_that_drains_in_time_still_completes(
        self, worker_env, mock_db, monkeypatch
    ):
        from app.core import config as cfg

        monkeypatch.setattr(
            cfg.settings, "task_worker_background_settle_seconds", 5.0, raising=False
        )
        state = {}

        def _start_monitor(sid):
            state["sid"] = sid
            task_registry.start_task(sid, "mon-2", "shell", "npm test")

            async def _finish():
                await asyncio.sleep(0.02)
                task_registry.complete_task(sid, "mon-2", "completed")

            state["task"] = asyncio.ensure_future(_finish())

        worker_env.tm.on_wait = _start_monitor
        monkeypatch.setattr(pa, "final_assistant_text", lambda sid: "All 42 tests pass.")

        outcome = _run(pa.dispatch_worker(
            parent_session_id="parent-1",
            profile_id="p1",
            prompt="go",
            background=True,
        ))

        assert outcome.ok is True
        record = task_registry.get_task("parent-1", outcome.session_id)
        assert record["status"] == "completed"
        assert record["summary"] == "All 42 tests pass."
        # The drain re-enters the idle wait, so a WAKE turn the settling work
        # triggers is waited out and read as the worker's real final turn —
        # rather than the interim text the first turn ended on.
        assert worker_env.tm.waits > 1
        task_registry.clear_session(outcome.session_id)

    def test_a_clean_worker_pays_nothing_for_the_drain(self, worker_env, mock_db):
        outcome = _run(pa.dispatch_worker(
            parent_session_id="parent-1", profile_id="p1", prompt="go",
        ))
        assert outcome.ok is True


# ===========================================================================
# E — the prompt and receipt strings the model actually reads
# ===========================================================================

class TestWorkerDoctrine:

    def test_the_subagent_prompt_says_there_is_no_wake_seam(self, monkeypatch):
        monkeypatch.setattr(td, "_is_worker_session", lambda sid: True)
        monkeypatch.setattr(td, "caller_chain", lambda sid: ([], "root-1", 1))

        prompt = td.subagent_system_prompt("w-1")

        assert "NO WAKE SEAM" in prompt
        assert "Never end with" in prompt
        assert "ONE sentence stating the result" in prompt

    def test_the_task_receipt_says_to_disambiguate_before_fanning_out(
        self, mock_db, monkeypatch
    ):
        from app.db import database

        database.create_profile("root-agent", "Root Agent", None, {"model": "opus"})
        database.create_profile("helper", "Helper", None, {"model": "sonnet"})
        database.create_session(
            session_id="s-receipt", profile_id="root-agent", title="root"
        )

        async def fake_worker(**kwargs):
            return WorkerOutcome(ok=True, session_id=kwargs["worker_session_id"])

        with patch("app.core.provider_agent.dispatch_worker", side_effect=fake_worker), \
             patch.object(td, "_await_worker_registration", return_value=None):
            payload = _run(td.dispatch_task_tool(
                {"agent": "helper", "prompt": "go", "mode": "background"},
                caller_session_id="s-receipt",
            ))

        note = payload["note"]
        assert "resolve any ambiguity" in note
        assert "End the turn now" in note, "the park-first wording still leads"


class TestCapacityIsSaidOutLoud:
    """C: the parent must read "capacity", not a provider's header dump."""

    def test_the_registry_summary_names_capacity(self):
        summary = pa._failure_summary(
            "error",
            "Rate limit reached for gpt-5.6-luna in organization org-JTwK2 on "
            "tokens per min (TPM): Limit 500000, Used 494784.",
            0,
        )
        assert summary.startswith(
            "worker failed to start (provider at capacity, already retried):"
        )

    def test_a_transport_failure_keeps_the_plain_prefix(self):
        summary = pa._failure_summary(
            "error", "RemoteProtocolError: peer closed connection", 0
        )
        assert summary.startswith("worker failed to start:")
        assert "capacity" not in summary

    def test_the_returned_error_explains_what_to_do(self):
        outcome = WorkerOutcome(
            ok=False,
            session_id="w-1",
            error="Rate limit reached for gpt-5.6-luna. Please try again in 3s.",
            frames_forwarded=0,
        )
        reworded = td._capacity_worded(outcome, 3)
        assert reworded.error.startswith("Provider capacity:")
        assert "rescheduled 3 time(s)" in reworded.error
        assert "the brief is fine" in reworded.error
        # The provider's own text is kept, behind the explanation.
        assert "Please try again in 3s." in reworded.error

    def test_a_worker_that_ran_is_left_alone(self):
        outcome = WorkerOutcome(
            ok=False, session_id="w-1", error="Rate limit reached mid-run",
            frames_forwarded=9,
        )
        assert td._capacity_worded(outcome, 1) is outcome


class TestAbandonedRetryStillTellsTheParent:
    """A suppressed wake plus a retry that never happens = a stranded parent."""

    @pytest.fixture(autouse=True)
    def _clean(self):
        td._PENDING_ADMISSIONS.clear()
        td._ADMISSION_LOCK = None
        yield
        td._PENDING_ADMISSIONS.clear()
        td._ADMISSION_LOCK = None
        task_registry.clear_session("root-1")
        sup.reset()

    def test_a_retry_refused_at_admission_wakes_the_caller_anyway(
        self, mock_db, monkeypatch
    ):
        from app.core import config as cfg

        monkeypatch.setattr(cfg.settings, "task_worker_start_retries", 1, raising=False)
        monkeypatch.setattr(td, "_START_RETRY_BACKOFF_SECONDS", 0.01)
        announced = []

        async def _announce(owner, worker, summary):
            announced.append((owner, worker, summary))

        monkeypatch.setattr(pa, "announce_abandoned_retry", _announce)

        async def _refuse(worker_id, **kwargs):
            return "Server is at capacity — nothing freed.", 0.0

        monkeypatch.setattr(td, "_reserve_admission", _refuse)

        async def dispatcher(**kwargs):
            # The predicate says "I am about to retry", so the wake is skipped.
            assert kwargs["suppress_caller_wake"]("error", "reset", 0) is True
            return WorkerOutcome(
                ok=False, session_id=kwargs["worker_session_id"],
                error="reset", frames_forwarded=0,
            )

        outcome = _run(td._dispatch_worker_with_retries(
            kwargs=dict(
                parent_session_id="root-1", context_session_id="owner-1",
                profile_id="p1", prompt="go", label="x",
                worker_session_id="w-1",
            ),
            background=True,
            lane="native",
            provider_id=None,
            parent_session_id="root-1",
            dispatcher=dispatcher,
        ))

        assert outcome.ok is False
        assert "could not be admitted" in outcome.error
        assert len(announced) == 1
        owner, worker, _summary = announced[0]
        assert owner == "owner-1", "the dispatching session, not the routing root"
        assert worker == "w-1"

    def test_the_announcement_reaches_the_scheduler(self, worker_env, mock_db):
        task_registry.start_task("owner-1", "w-9", "agent", "stranded")

        _run(pa.announce_abandoned_retry("owner-1", "w-9", "it never started"))

        assert [sid for sid, _f in worker_env.wakes] == ["owner-1"]
        _sid, frame = worker_env.wakes[0]
        assert frame["task_id"] == "w-9"
        assert frame["status"] == "error"
        assert task_registry.get_task("owner-1", "w-9")["status"] == "error"


class TestRateLimitMisclassification:
    """Every failure message carries a session UUID; some contain "429"."""

    def test_a_uuid_containing_429_is_not_a_rate_limit(self):
        text = (
            "The worker forwarded no output within 90s, so its provider "
            "connection never opened. Its session is "
            "d59fe8cf-d16d-429c-b22f-bcd47a15d6fc."
        )
        assert pa.looks_rate_limited(text) is False
        assert pa._failure_summary("error", text, 0).startswith(
            "worker failed to start:"
        )

    def test_a_real_429_still_reads_as_one(self):
        assert pa.looks_rate_limited("HTTP 429 from the provider") is True
        assert pa.looks_rate_limited("status=429, body=...") is True
