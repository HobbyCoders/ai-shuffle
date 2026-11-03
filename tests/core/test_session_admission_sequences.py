"""Every short order of events around one chat's lease, checked against the rules.

Three reviews in a row found a new Stop or wait combination that the case-by-case
tests missed, so this walks the combinations instead. It drives the real module and
the real ``interrupt_session`` on a clock it controls, keeps what the rules say should
be true beside it, and compares the two after every step. The routes and engines are
not here: what a route does with a lease is in ``test_sse_interrupt_lifecycle`` and
``test_busy_write_guard_engine``.

The rules, each named where a broken one is raised:

- ``stop-reached``: a turn holds a Stop if and only if that Stop arrived while it
  waited and could reach it, so never one from before it arrived, and it uses it once.
- ``answer``: what ``interrupt_session`` says is what it did, and a Stop kept for a
  waiting turn leaves the engine alone.
- ``limit-overrun`` and ``limit-short``: a waiter is refused at its limit, counted from
  when it arrived: 15 s while only ending turns are in its way, 60 s once a rewind is.
- ``refused`` and ``waits-for-nothing``: a turn is refused when a running turn is in its
  way and no rewind is, and otherwise waits exactly as long as something is in its way.
- ``two-holders``: only one request gets the chat.
- ``ended-as``: a turn ends interrupted if and only if it was given a Stop.
- ``left-behind``: an idle chat leaves nothing in the module.
"""

from __future__ import annotations

import asyncio
import logging
import random
import selectors
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Dict, List, Optional, Tuple

import pytest

from app.core import query_session_lifecycle as lifecycle
from app.core.provider import session_admission as admission
from app.core.provider.session_admission import SessionBusyError

SID = "ses_sequences"
SHORT = admission._ENDING_WAIT_SECONDS
LONG = admission._REWIND_WAIT_SECONDS

# Every order of this many events is walked, with and without a pooled engine.
WALKED_LENGTH = 5
RANDOM_ORDERS = 20000
RANDOM_LENGTH = 14
SEED = 1027


class _NeverSleeps(selectors.DefaultSelector):
    def select(self, timeout=None):
        return super().select(0)


class _ClockLoop(asyncio.SelectorEventLoop):
    """An event loop whose time moves only when a test moves it."""

    def __init__(self) -> None:
        super().__init__(_NeverSleeps())
        self.now = 0.0

    def time(self) -> float:
        return self.now

    async def settle(self) -> None:
        """Run everything that can run. Only a later clock move can wake what is left."""
        await asyncio.sleep(0)
        while self._ready:
            await asyncio.sleep(0)


class _Broken(Exception):
    def __init__(self, rule: str, detail: str) -> None:
        super().__init__(detail)
        self.rule = rule


class _Engine:
    def __init__(self) -> None:
        self.is_connected = True
        self.is_streaming = False
        self.interrupt_requested = False
        self.interrupts = 0
        self.adapter = self

    async def interrupt(self) -> None:
        self.interrupts += 1


@dataclass(eq=False)
class _Request:
    """A turn or a rewind, and what the rules say about it."""
    name: str
    rewind: bool
    stoppable: bool
    arrived: float
    limit: float
    task: asyncio.Task
    lease: Optional[admission.Lease] = None
    waiting: bool = True
    closed: bool = False
    given_stop: bool = False
    started: bool = False
    finished: bool = False
    ending: bool = False
    taken: bool = False
    took_chat: bool = False
    engine: Optional[_Engine] = None


class _Chat:
    """One chat: the real module on one side, what should be true on the other."""

    def __init__(self, loop: _ClockLoop, pool: SimpleNamespace, pooled: bool) -> None:
        self.loop = loop
        # Reset for each order: far enough along, a float clock cannot tell a timer due now.
        loop.now = 0.0
        self.pool = pool
        pool.engine = _Engine() if pooled else None
        self.requests: List[_Request] = []
        self.done: List[_Request] = []
        self.holder: Optional[_Request] = None
        self.rewind: Optional[_Request] = None

    # ─── what can happen next ───────────────────────────────────────────────

    def _waiting_turns(self) -> List[_Request]:
        return [r for r in self.requests if r.waiting and not r.rewind]

    def _open_turn(self) -> Optional[_Request]:
        return next((r for r in self.requests if r.lease and not r.rewind and not r.closed), None)

    def _running_turn(self) -> Optional[_Request]:
        holder = self.holder
        return holder if holder is not None and not holder.rewind and not holder.ending else None

    def possible(self) -> List[str]:
        events = ["arrive", "arrive-query", "stop", "begin-rewind", "pass-short", "pass-long"]
        running = self._running_turn()
        if running is not None and not running.started:
            events.append("start")
        if running is not None and running.started:
            events.append("end")
        if self._open_turn() is not None:
            events.append("close")
        waiting = self._waiting_turns()
        if waiting:
            events.append("cancel-first")
        if len(waiting) > 1:
            events.append("cancel-last")
        if self.rewind is not None:
            events.append("finish-rewind")
            if not self.rewind.took_chat:
                events.append("take")
        return events

    async def step(self, event: str) -> None:
        await getattr(self, "on_" + event.replace("-", "_"))()
        await self.loop.settle()
        self._take_in_results()
        self._check()

    # ─── the events ─────────────────────────────────────────────────────────

    def _arrive(self, name: str, work, *, rewind: bool, stoppable: bool) -> None:
        task = self.loop.create_task(work)
        request = _Request(
            f"{name}{len(self.requests) + 1}", rewind, stoppable, self.loop.now,
            LONG if rewind else SHORT, task,
        )
        self.requests.append(request)
        task.add_done_callback(lambda _: self.done.append(request))

    async def on_arrive(self) -> None:
        self._arrive("turn", admission.acquire(SID), rewind=False, stoppable=True)

    async def on_arrive_query(self) -> None:
        self._arrive("query", admission.acquire(SID, stoppable=False), rewind=False, stoppable=False)

    async def on_begin_rewind(self) -> None:
        self._arrive("rewind", admission.begin_rewind(SID), rewind=True, stoppable=False)

    async def on_pass_short(self) -> None:
        self.loop.now += SHORT - 5

    async def on_pass_long(self) -> None:
        self.loop.now += LONG - SHORT + 5

    async def on_start(self) -> None:
        turn = self.holder
        turn.started = True
        if not turn.stoppable:
            return  # runs off the pool
        turn.engine = self.pool.engine = self.pool.engine or _Engine()
        turn.engine.is_streaming = True
        admission.apply_stop(turn.lease, turn.engine)
        self._check_ended_as(turn)

    def _finish(self, turn: _Request) -> None:
        """What a route's own cleanup does with the engine as its turn finishes."""
        if turn.finished or turn.engine is None:
            return
        turn.finished = True
        if not turn.taken:
            self._check_ended_as(turn)
        turn.engine.interrupt_requested = False
        turn.engine.is_streaming = False

    def _check_ended_as(self, turn: _Request) -> None:
        if turn.engine.interrupt_requested != turn.given_stop:
            raise _Broken("ended-as", (
                f"{turn.name} would end {'interrupted' if turn.engine.interrupt_requested else 'done'}"
                f", given a Stop: {turn.given_stop}"
            ))

    async def on_end(self) -> None:
        turn = self.holder
        self._finish(turn)
        turn.ending = True
        admission.turn_ended(turn.lease)

    async def on_close(self) -> None:
        turn = self._open_turn()
        self._finish(turn)
        turn.closed = True
        if self.holder is turn:
            self.holder = None
        admission.close(turn.lease)

    async def on_cancel_first(self) -> None:
        self._waiting_turns()[0].task.cancel()

    async def on_cancel_last(self) -> None:
        self._waiting_turns()[-1].task.cancel()

    async def on_take(self) -> None:
        rewind = self.rewind
        rewind.took_chat = True
        previous = admission.take_for_rewind(rewind.lease)
        expected = self.holder.lease if self.holder else None
        if previous is not expected:
            raise _Broken("two-holders", "the rewind took the chat from the wrong lease")
        if self.holder is not None:
            self.holder.taken = True
        self.holder = rewind
        self.pool.engine = None  # a rewind evicts the engine

    async def on_finish_rewind(self) -> None:
        rewind = self.rewind
        self.rewind = None
        if self.holder is rewind:
            self.holder = None
        rewind.closed = True
        admission.close(rewind.lease)

    async def on_stop(self) -> None:
        engine = self.pool.engine
        before = (engine.interrupt_requested, engine.interrupts) if engine else None
        running = self._running_turn()
        waiters = [r for r in self._waiting_turns() if r.stoppable]
        kept = False
        if running is not None and running.stoppable:
            expected = engine is not None
            running.given_stop = running.given_stop or expected
        elif waiters:
            expected = kept = True
            for waiter in waiters:
                waiter.given_stop = True
        else:
            expected = engine is not None  # nothing to stop, as on an idle engine
        answer = await lifecycle.interrupt_session(SID)
        if answer is not expected:
            raise _Broken("answer", f"answered {answer}, should be {expected}")
        if engine is None:
            return
        if kept and (engine.interrupt_requested, engine.interrupts) != before:
            raise _Broken("answer", "a Stop kept for a waiting turn went to the engine")
        if not kept and not engine.interrupt_requested:
            raise _Broken("answer", "a Stop for the engine did not reach it")

    # ─── what must be true after each step ──────────────────────────────────

    def _take_in_results(self) -> None:
        for request in self.done:
            request.waiting = False
            if request.task.cancelled():
                request.closed = True
                continue
            error = request.task.exception()
            if isinstance(error, SessionBusyError):
                request.closed = True
                self._check_refusal(request)
            elif error is not None:
                raise error
            else:
                request.lease = request.task.result()
                self._check_got_chat(request)
            self._note_rewinds()
        self.done.clear()
        self._note_rewinds()

    def _note_rewinds(self) -> None:
        if self.rewind is not None:
            for request in self.requests:
                if request.waiting:
                    request.limit = LONG

    def _check_refusal(self, request: _Request) -> None:
        if self.loop.now >= request.arrived + request.limit:
            return
        running = self._running_turn()
        if request.rewind or self.rewind is not None or running is None:
            raise _Broken("limit-short", (
                f"{request.name} refused after {self.loop.now - request.arrived:.0f} s "
                f"of its {request.limit:.0f} s limit"
            ))

    def _check_got_chat(self, request: _Request) -> None:
        if request.rewind:
            if self.rewind is not None:
                raise _Broken("two-holders", f"{request.name} runs with another rewind")
            self.rewind = request
            return
        if self.rewind is not None or self.holder is not None:
            raise _Broken("two-holders", f"{request.name} got a chat something else holds")
        self.holder = request
        if request.lease.stop_requested is not request.given_stop:
            raise _Broken("stop-reached", (
                f"{request.name} got the chat with stop_requested={request.lease.stop_requested}, "
                f"given a Stop: {request.given_stop}"
            ))

    def _check(self) -> None:
        for request in self.requests:
            if not request.waiting:
                continue
            if self.loop.now >= request.arrived + request.limit:
                raise _Broken("limit-overrun", (
                    f"{request.name} still waits {self.loop.now - request.arrived:.0f} s "
                    f"after arriving, past its {request.limit:.0f} s limit"
                ))
            if request.rewind:
                in_the_way = self.rewind is not None
            else:
                in_the_way = self.rewind is not None or (self.holder is not None and self.holder.ending)
                if self.rewind is None and self._running_turn() is not None:
                    raise _Broken("refused", f"{request.name} waits behind a running turn")
            if not in_the_way:
                raise _Broken("waits-for-nothing", f"{request.name} waits with nothing in its way")
        idle = self.holder is None and self.rewind is None and not any(r.waiting for r in self.requests)
        if idle:
            for name, value in vars(admission).items():
                if isinstance(value, (dict, set)) and name != "_unattended" and SID in value:
                    raise _Broken("left-behind", f"an idle chat is still in {name}")

    async def clean_up(self) -> None:
        for request in self.requests:
            request.task.cancel()
        await self.loop.settle()
        for request in self.requests:
            if not request.task.cancelled():
                request.task.exception()
        admission.clear()


async def _replay(loop, pool, pooled: bool, order: Tuple[str, ...]) -> Tuple[Optional[_Broken], List[str]]:
    """Run one order from an empty process; what broke, and what could come next."""
    chat = _Chat(loop, pool, pooled)
    try:
        for event in order:
            await chat.step(event)
        return None, chat.possible()
    except _Broken as broken:
        return broken, []
    finally:
        await chat.clean_up()


@dataclass
class _Found:
    orders: int = 0
    broken: Optional[Dict[str, List[Tuple[Tuple[str, ...], bool, str]]]] = None
    seen_events: Optional[set] = None

    def __post_init__(self) -> None:
        self.broken = {}
        self.seen_events = set()

    def note(self, order, pooled, broken) -> None:
        self.orders += 1
        self.seen_events.update(order)
        if broken is not None:
            self.broken.setdefault(broken.rule, []).append((order, pooled, str(broken)))

    def report(self) -> str:
        lines = []
        for rule, orders in sorted(self.broken.items()):
            order, pooled, detail = min(orders, key=lambda found: len(found[0]))
            lines.append(
                f"{rule}: {len(orders)} orders, shortest ({'engine' if pooled else 'no engine'}): "
                f"{' > '.join(order)}: {detail}"
            )
        return "\n".join(lines)


async def _walk(loop, pool, pooled: bool, order: Tuple[str, ...], length: int, found: _Found) -> None:
    broken, possible = await _replay(loop, pool, pooled, order)
    found.note(order, pooled, broken)
    if broken is None and len(order) < length:
        for event in possible:
            await _walk(loop, pool, pooled, order + (event,), length, found)


async def _random_orders(loop, pool, count: int, length: int, seed: int, found: _Found) -> None:
    rng = random.Random(seed)
    for _ in range(count):
        pooled = rng.random() < 0.5
        chat = _Chat(loop, pool, pooled)
        order: List[str] = []
        broken = None
        try:
            for _ in range(length):
                event = rng.choice(chat.possible())
                order.append(event)
                await chat.step(event)
        except _Broken as caught:
            broken = caught
        finally:
            await chat.clean_up()
        found.note(tuple(order), pooled, broken)


def _on_a_clock(monkeypatch, run) -> None:
    pool = SimpleNamespace(engine=None)
    pool.peek = lambda _: pool.engine
    monkeypatch.setattr(admission, "get_session_pool", lambda: pool)
    monkeypatch.setattr(lifecycle, "get_session_pool", lambda: pool)
    loop = _ClockLoop()
    logging.disable(logging.CRITICAL)
    try:
        loop.run_until_complete(run(loop, pool))
    finally:
        logging.disable(logging.NOTSET)
        loop.close()
        admission.clear()


def explore(monkeypatch, *, length: int = WALKED_LENGTH, orders: int = RANDOM_ORDERS) -> _Found:
    found = _Found()

    async def run(loop, pool):
        for pooled in (True, False):
            await _walk(loop, pool, pooled, (), length, found)
        await _random_orders(loop, pool, orders, RANDOM_LENGTH, SEED, found)

    _on_a_clock(monkeypatch, run)
    return found


def test_every_short_order_and_a_long_random_sample_keep_the_rules(monkeypatch):
    found = explore(monkeypatch)
    assert not found.broken, found.report()
    assert found.orders > 10_000, "the walk stopped early, so it proves little"
    assert found.seen_events >= {
        "arrive", "arrive-query", "stop", "start", "end", "close", "cancel-first", "cancel-last",
        "begin-rewind", "take", "finish-rewind", "pass-short", "pass-long",
    }


ORDERS_THAT_WENT_WRONG = {
    # A Stop kept for one waiter must leave with it, whoever else is still waiting.
    "stop-outlives-waiter-that-timed-out": (
        "begin-rewind", "arrive", "stop", "pass-long", "arrive", "pass-short", "finish-rewind",
    ),
    "stop-outlives-waiter-that-was-cancelled": (
        "begin-rewind", "arrive", "stop", "arrive", "cancel-first", "finish-rewind",
    ),
    # A rewind arriving during a wait for an ending turn gives the waiter the longer limit.
    "rewind-during-ending-wait": (
        "arrive", "start", "end", "arrive", "begin-rewind", "pass-short", "pass-short",
    ),
    # A holder no Stop can reach must not keep one from the turn waiting behind the rewind.
    "stop-past-a-query-holder": ("arrive-query", "begin-rewind", "arrive", "stop", "finish-rewind"),
    # Found by the random walk. A Stop on the engine for a turn that left before it started
    # is not for the turn that was already waiting when it was pressed.
    "stop-on-the-engine-outlives-its-turn": (
        "arrive", "stop", "begin-rewind", "arrive", "close", "finish-rewind", "start",
    ),
    # A waiter asleep on one lease missed that the rewind took it and left.
    "waiter-asleep-on-a-taken-holder": (
        "arrive", "start", "end", "arrive", "begin-rewind", "take", "finish-rewind",
    ),
    # A turn no Stop can reach must not use up the one for the turn beside it.
    "query-takes-a-waiters-stop": ("begin-rewind", "arrive-query", "arrive", "stop", "finish-rewind"),
}


@pytest.mark.parametrize("pooled", [True, False], ids=["engine", "no-engine"])
@pytest.mark.parametrize("name", ORDERS_THAT_WENT_WRONG)
def test_orders_that_once_went_wrong(monkeypatch, name, pooled):
    broken = []

    async def run(loop, pool):
        broken.append((await _replay(loop, pool, pooled, ORDERS_THAT_WENT_WRONG[name]))[0])

    _on_a_clock(monkeypatch, run)
    assert broken == [None], str(broken[0])
