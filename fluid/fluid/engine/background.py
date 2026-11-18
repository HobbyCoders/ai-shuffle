"""Durable park/wake lifecycle for host-owned background tasks."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
import uuid
from collections.abc import Iterable
from typing import Any

from fluid.events.model import (
    ErrorEvent,
    Event,
    Interrupted,
    WAKE_AUTO,
    WAKE_NOT_APPLICABLE,
    WAKE_SUPPRESSED,
    SessionParked,
    SessionWoken,
    SubagentEvent,
    TurnComplete,
)
from fluid.events.ops import BackgroundTaskFinished, UserTurn
from fluid.engine.questions import QuestionLifecycleMixin
from fluid.store.base import iter_events_of_class
from fluid.tools.hardening import defang_harness_markers, fence_shell_output
from fluid.tools.registry import BackgroundTaskReceipt

log = logging.getLogger(__name__)

_TERMINAL_EVENTS = (TurnComplete, Interrupted, ErrorEvent)
# How much of a long worker report rides the wake turn when the rest is one
# Read away. Only applied when there IS a report path to point at.
_REPORT_HEAD_CHARS = 4_000
_TERMINAL_TASK_KINDS = {"finished", "failed", "cancelled"}
_BACKGROUND_SPAWN_SETTLE_TIMEOUT = 5.0
# A wake that fails before its SessionWoken marker is retried this many times
# (with doubling backoff from the base delay) before delivery is latched off.
# The latch lifts on the next user turn, so one bad spell never blocks every
# later completion for the life of the process.
_BACKGROUND_WAKE_MAX_ATTEMPTS = 3
_BACKGROUND_WAKE_RETRY_BASE_SECONDS = 0.5
# A failed read of the lifecycle history is retried by a later lifecycle call,
# but not before this backoff (doubling per failure, capped); in between the
# lifecycle runs from memory. The warning repeats at most once per interval.
_REPLAY_RETRY_BASE_SECONDS = 0.5
_REPLAY_RETRY_MAX_SECONDS = 30.0
_REPLAY_WARN_INTERVAL_SECONDS = 60.0
# Lifecycle writes that raised are remembered until settled, within bounds: the
# newest attempts per step, and the steps most recently written.
_UNCERTAIN_ATTEMPTS_PER_STEP = 8
_UNCERTAIN_STEPS = 256


def _kind_group(kind: str) -> str:
    """A task has one event per group: started, completion_pending, terminal."""
    return "terminal" if kind in _TERMINAL_TASK_KINDS else kind


def _still_current(carrier: Any) -> bool:
    """False for an op or receipt its host has since superseded."""
    is_current = getattr(carrier, "is_current", None)
    return is_current is None or bool(is_current())


async def await_cancelled_child(task: asyncio.Task[Any]) -> None:
    """Wait for a child task this caller just cancelled.

    The child's cancellation ends here, but one aimed at the caller, such as a
    close deadline firing during the child's cleanup, must carry on. Compared
    against the count at entry, because a close can run from a task already being
    cancelled.
    """
    current = asyncio.current_task()
    cancels_before = current.cancelling() if current is not None else 0
    try:
        await task
    except asyncio.CancelledError:
        if current is not None and current.cancelling() > cancels_before:
            raise


class BackgroundLifecycleMixin(QuestionLifecycleMixin):
    """Engine mixin implementing durable background-task park and wake.

    The embedding host owns worker execution. Fluid owns the session semantics:
    a structured spawn receipt becomes a durable ``SubagentEvent(started)``;
    terminal turns with live workers emit ``SessionParked``; and a trusted host
    submits ``BackgroundTaskFinished`` to persist the result and start a fresh
    continuation turn. Fluid tracks only the short spawn handshake, never the
    host worker itself.
    """

    async def _seed_bus_seq(self, sid: str) -> None:
        """Continue event numbering after the rows already persisted.

        A fresh bus counts from 1, so a resumed session would re-issue seqs
        the events file already holds and ``events_since`` would skip the
        resumed run's first events. A store may offer ``last_event_seq``;
        otherwise the maximum is taken from a full ``events_since`` read.
        """
        last_event_seq = getattr(self.store, "last_event_seq", None)
        # Failure must abort resume: starting at zero would silently collide
        # with durable events in stores that deduplicate by (session_id, seq).
        if callable(last_event_seq):
            last = int(await last_event_seq(sid))
        else:
            events = await self.store.events_since(sid, 0)
            last = max((e.seq for e in events), default=0)
        self.bus.seed(sid, last)

    def _init_background_lifecycle(self) -> None:
        self._init_questions()
        self._background_pending: dict[str, BackgroundTaskReceipt] = {}
        self._background_terminal: set[str] = set()
        self._background_early: dict[str, BackgroundTaskFinished] = {}
        self._background_completions: dict[str, BackgroundTaskFinished] = {}
        # History loaded, and the recovery it calls for done: separate, so a
        # refused write during recovery is retried by the next lifecycle call.
        self._background_rehydrated = False
        self._background_recovered = False
        # Completions the host's recovery hook gave, until each is stored.
        self._background_host_recovery: dict[str, BackgroundTaskFinished] | None = None
        self._background_replay_failures = 0
        self._background_replay_retry_at = 0.0
        self._background_replay_warned_at: float | None = None
        # Lifecycle writes that raised, so may or may not have committed, by
        # (task id, kind group): the bus seq before the first such write, where
        # a retry looks in the store, and each attempt's write id with what the
        # caller keeps for it (its op or receipt).
        self._background_uncertain: dict[
            tuple[str, str], tuple[int, list[tuple[str, Any]]]
        ] = {}
        self._background_lock = asyncio.Lock()
        self._background_wake_task: asyncio.Task | None = None
        self._background_waiting_on: asyncio.Task | None = None
        self._background_wake_blocked = False
        self._background_wake_failures = 0
        self._background_wake_retry: asyncio.TimerHandle | None = None
        self._background_spawn_tasks: set[asyncio.Task] = set()
        self._background_closed = False
        self._background_turn_terminal = False
        self._active_turn_id: str | None = None
        self.ctx.extra["background_task_started"] = self._background_task_started
        self.ctx.extra["background_spawn_tasks"] = self._background_spawn_tasks

    async def _rehydrate_background_lifecycle_unlocked(
        self, live_task_id: str | None = None
    ) -> None:
        """``live_task_id``: a task whose completion the caller is about to
        apply, which recovery leaves to it."""
        if (
            not self._background_rehydrated
            and not await self._load_background_history_unlocked()
        ):
            return
        if not self._background_recovered:
            await self._recover_background_unlocked(live_task_id)

    async def _load_background_history_unlocked(self) -> bool:
        """Rebuild lifecycle state from the store. False when it was not read."""
        if time.monotonic() < self._background_replay_retry_at:
            return False  # backing off after a failed read
        meta = await self._ensure_session()
        try:
            events = [event async for event in iter_events_of_class(
                self.store, meta.session_id, 0, (SubagentEvent, SessionWoken))]
        except NotImplementedError:
            # A store without an event log: the lifecycle runs from memory.
            self._background_rehydrated = True
            self._background_recovered = True
            return False
        except Exception as exc:  # noqa: BLE001 — retried by a later lifecycle call
            # A failed read is not an empty log: leave the latch down so a
            # later call reads again and finds what is still owed. Until then
            # the lifecycle runs from memory, as for a store without a log.
            self._note_replay_failure(meta.session_id, exc)
            return False
        self._background_replay_failures = 0
        self._background_replay_retry_at = 0.0

        for event in events:
            if isinstance(event, SubagentEvent):
                if event.kind == "started":
                    if event.task_id not in self._background_terminal:
                        self._background_pending[event.task_id] = BackgroundTaskReceipt(
                            task_id=event.task_id,
                            worker_session_id=event.worker_session_id,
                            description=event.description,
                            serves=event.serves,
                        )
                elif event.kind == "completion_pending":
                    if event.task_id not in self._background_terminal:
                        self._background_early[event.task_id] = BackgroundTaskFinished(
                            task_id=event.task_id,
                            status=event.status or "finished",
                            summary=event.summary,
                            worker_session_id=event.worker_session_id,
                            wake=event.wake,
                            report_path=event.report_path,
                        )
                elif event.kind in _TERMINAL_TASK_KINDS:
                    self._background_pending.pop(event.task_id, None)
                    self._background_early.pop(event.task_id, None)
                    self._background_terminal.add(event.task_id)
                    if event.wake:
                        self._background_completions[event.task_id] = BackgroundTaskFinished(
                            task_id=event.task_id,
                            status=event.kind,
                            summary=event.summary,
                            worker_session_id=event.worker_session_id,
                            wake=True,
                            report_path=event.report_path,
                        )
                    else:
                        self._background_completions.pop(event.task_id, None)
            elif isinstance(event, SessionWoken):
                for task_id in event.task_ids:
                    self._background_completions.pop(task_id, None)
        self._background_rehydrated = True
        return True

    def _note_replay_failure(self, session_id: str, exc: BaseException) -> None:
        """Back off before the next read and warn, at most once an interval."""
        self._background_replay_failures += 1
        now = time.monotonic()
        # The exponent is capped before the power is taken: a long outage
        # counts failures without bound, and 2.0 ** 1024 overflows.
        doublings = min(self._background_replay_failures - 1, 32)
        self._background_replay_retry_at = now + min(
            _REPLAY_RETRY_MAX_SECONDS, _REPLAY_RETRY_BASE_SECONDS * 2 ** doublings,
        )
        warned = self._background_replay_warned_at
        if warned is None or now - warned >= _REPLAY_WARN_INTERVAL_SECONDS:
            self._background_replay_warned_at = now
            log.warning(
                "could not read session %s's background task history (%s, %d "
                "failure(s) in a row); a later lifecycle call retries",
                session_id, type(exc).__name__, self._background_replay_failures,
            )

    async def _recover_background_unlocked(self, live_task_id: str | None) -> None:
        """Finish what the history left open, once it has been read.

        Marked done only when every step got through: a refused terminal write
        here leaves it to the next lifecycle call to try again. The host's
        answer for a task is an inference; a live completion for that task
        wins over it, so ``live_task_id`` is never recovered here.
        """
        for task_id in tuple(self._background_early):
            if task_id in self._background_pending:
                # Finalizing pops it, once its terminal event is stored.
                await self._finalize_background_task_unlocked(
                    self._background_early[task_id])

        recover_host = self.ctx.extra.get("recover_background")
        if self._background_host_recovery is not None and live_task_id is not None:
            self._background_host_recovery.pop(live_task_id, None)
        if recover_host is not None:
            if self._background_host_recovery is None:
                # Asked once: the host's answer can change between calls (a
                # run can become active again), so completions it gave are
                # kept until their terminal writes land, not asked for again.
                accepted: dict[str, BackgroundTaskFinished] = {}
                for op in await recover_host(tuple(self._background_pending)):
                    # A non-terminal status would be recorded as "terminal"
                    # here but replay as still pending after the next restart.
                    if (
                        op.task_id in self._background_pending
                        and op.task_id != live_task_id
                        and op.status in _TERMINAL_TASK_KINDS
                    ):
                        accepted.setdefault(op.task_id, op)
                self._background_host_recovery = accepted
            for task_id, op in tuple(self._background_host_recovery.items()):
                # Checked again on every try: by a retry the task may have
                # ended another way, or the host superseded the op.
                if task_id in self._background_pending and _still_current(op):
                    await self._finalize_background_task_unlocked(op)
                del self._background_host_recovery[task_id]
        self._background_recovered = True

    def _park_pending_ids(self) -> tuple[str, ...]:
        """The pending task ids a park is actually WAITING on.

        A ``serves`` receipt (a dev server, a tunnel — see
        ``BackgroundTaskReceipt.serves``) is tracked and surfaced like any
        other background task, but it is not work whose result the session
        wants: its only "completion" is its own death. Counting it would park
        the session on every turn that had a server running, so it is excluded
        here — the one place park accounting is decided. A turn whose only
        live tasks are serving ones therefore parks on nothing and emits no
        ``SessionParked`` at all.
        """
        return tuple(sorted(
            task_id
            for task_id, receipt in self._background_pending.items()
            if not receipt.serves
        ))

    async def _background_task_started(self, receipt: BackgroundTaskReceipt) -> None:
        should_wake = False
        async with self._background_lock:
            await self._rehydrate_background_lifecycle_unlocked()
            known = (
                receipt.task_id in self._background_pending
                or receipt.task_id in self._background_terminal
            )
            if not known:
                meta = await self._ensure_session()
                # State changes only once the event is stored, so a refused
                # write leaves the receipt retryable. If an earlier attempt
                # turns out to have committed, its receipt is the one kept.
                _, receipt = await self._emit_lifecycle(SubagentEvent(
                    session_id=meta.session_id,
                    turn_id=self._active_turn_id,
                    task_id=receipt.task_id,
                    kind="started",
                    description=receipt.description,
                    worker_session_id=receipt.worker_session_id,
                    # A task that has only just started owes nobody a wake yet.
                    wake_disposition=WAKE_NOT_APPLICABLE,
                    serves=receipt.serves,
                ), receipt)
                self._background_pending[receipt.task_id] = receipt
                if (
                    receipt.task_id not in self._background_early
                    and self._background_turn_terminal
                ):
                    parked_on = self._park_pending_ids()
                    if parked_on:
                        await self._emit(SessionParked(
                            session_id=meta.session_id,
                            turn_id=self._active_turn_id,
                            pending_task_ids=parked_on,
                            reason="background_tasks",
                        ))
            # An early completion is finalized here, and again by a retried
            # receipt if its terminal write was refused the first time.
            early = self._background_early.get(receipt.task_id)
            if early is not None and receipt.task_id in self._background_pending:
                await self._finalize_background_task_unlocked(early)
            should_wake = bool(self._background_completions)
        if should_wake:
            self._kick_background_wake()

    async def _finish_background_task(self, op: BackgroundTaskFinished) -> None:
        if op.status not in _TERMINAL_TASK_KINDS:
            raise ValueError(
                "background task status must be finished, failed, or cancelled"
            )

        async with self._background_lock:
            await self._rehydrate_background_lifecycle_unlocked(live_task_id=op.task_id)
            if op.is_current is not None and not op.is_current():
                return
            if op.task_id not in self._background_terminal:
                if op.task_id in self._background_pending:
                    await self._finalize_background_task_unlocked(op)
                elif op.task_id not in self._background_early:
                    meta = await self._ensure_session()
                    if op.is_current is not None and not op.is_current():
                        return
                    _, early = await self._emit_lifecycle(SubagentEvent(
                        session_id=meta.session_id,
                        turn_id=self._active_turn_id,
                        task_id=op.task_id,
                        kind="completion_pending",
                        summary=op.summary,
                        worker_session_id=op.worker_session_id,
                        wake=op.wake,
                        status=op.status,
                        # Still a placeholder — the real terminal event carries
                        # the disposition that counts.
                        wake_disposition=WAKE_NOT_APPLICABLE,
                        report_path=op.report_path,
                    ), op)
                    # The completion that was stored, if an earlier attempt
                    # committed one, not necessarily the one just submitted.
                    self._background_early[op.task_id] = early
            should_wake = bool(self._background_completions)
        if should_wake:
            self._kick_background_wake()

    async def _finalize_background_task_unlocked(
        self, op: BackgroundTaskFinished
    ) -> None:
        meta = await self._ensure_session()
        if op.is_current is not None and not op.is_current():
            # A superseded completion is dropped, not kept for a later retry.
            if self._background_early.get(op.task_id) is op:
                del self._background_early[op.task_id]
            return
        receipt = self._background_pending.get(op.task_id)
        worker_session_id = op.worker_session_id or (
            receipt.worker_session_id if receipt is not None else None
        )
        # Store the terminal event before the task leaves pending: a refused
        # write must leave the completion retryable, and the next attempt
        # must not find the task already terminal and skip it (#1769). What
        # follows applies the event that was stored: an earlier attempt that
        # committed after all decides the status and the wake, not this op.
        committed, _ = await self._emit_lifecycle(SubagentEvent(
            session_id=meta.session_id,
            turn_id=self._active_turn_id,
            task_id=op.task_id,
            kind=op.status,
            description=receipt.description if receipt is not None else None,
            summary=op.summary,
            worker_session_id=worker_session_id,
            wake=op.wake,
            # The one place the distinction is real: a terminal completion the
            # engine will wake for, versus one where a wake is owed but the
            # host performs it instead of us.
            wake_disposition=WAKE_AUTO if op.wake else WAKE_SUPPRESSED,
            report_path=op.report_path,
        ), op)
        self._background_pending.pop(op.task_id, None)
        self._background_early.pop(op.task_id, None)
        self._background_terminal.add(op.task_id)
        # Its earlier steps are settled too: a terminal task writes neither.
        for group in ("started", "completion_pending"):
            self._background_uncertain.pop((op.task_id, group), None)
        if committed.wake:
            self._background_completions[op.task_id] = BackgroundTaskFinished(
                task_id=op.task_id,
                status=committed.kind,
                summary=committed.summary,
                worker_session_id=committed.worker_session_id,
                wake=True,
                report_path=committed.report_path,
            )

    async def _emit_lifecycle(
        self, event: SubagentEvent, carrier: Any
    ) -> tuple[SubagentEvent, Any]:
        """Write a task's started, completion-pending or terminal event once.

        Returns the event that counts and the ``carrier`` (the caller's op or
        receipt) of the attempt that wrote it. The callers change state only
        after this returns, so a write that raises leaves the step retryable
        (#1769 case 1). A write can raise and still have committed (a lost
        acknowledgement, or a cancellation while it ran), and the retry would
        be a second copy under a new seq. So every attempt carries a write id
        (a retry of the same op or receipt keeps it), each attempt that raised
        is remembered, and the next attempt first looks in the store, from the
        bus seq before the first such write, for one of those ids. A stored
        attempt whose carrier is still current is not written again: the
        stored event and that carrier are returned, whatever the retry
        submitted. One its host has since superseded stands in for nothing,
        and this attempt is written as usual. A lookup that fails raises, so
        the step stays retryable rather than guessing.

        Still publish-first through ``_emit``: a refused write was already
        seen live, and its retry is seen again (#1769 case 4 stays open).
        """
        key = (event.task_id, _kind_group(event.kind))
        uncertain = self._background_uncertain.get(key)
        write_id: str | None = None
        if uncertain is not None:
            for stored, stored_carrier in await self._stored_lifecycle_attempts(
                event.session_id, key, *uncertain
            ):
                if _still_current(stored_carrier):
                    del self._background_uncertain[key]
                    return stored, stored_carrier
            write_id = next(
                (wid for wid, kept in uncertain[1] if kept is carrier), None)
        event = dataclasses.replace(event, write_id=write_id or uuid.uuid4().hex)
        bus = getattr(self, "bus", None)
        before = bus.last_seq(event.session_id) if bus is not None else 0
        try:
            await self._emit(event)
        except BaseException:
            self._remember_uncertain(key, before, event.write_id, carrier)
            raise
        self._background_uncertain.pop(key, None)
        return event, carrier

    def _remember_uncertain(
        self, key: tuple[str, str], before: int, write_id: str, carrier: Any
    ) -> None:
        since, attempts = self._background_uncertain.pop(key, (before, []))
        if all(wid != write_id for wid, _ in attempts):
            attempts = [*attempts, (write_id, carrier)][-_UNCERTAIN_ATTEMPTS_PER_STEP:]
        self._background_uncertain[key] = (min(since, before), attempts)
        while len(self._background_uncertain) > _UNCERTAIN_STEPS:
            del self._background_uncertain[next(iter(self._background_uncertain))]

    async def _stored_lifecycle_attempts(
        self,
        session_id: str,
        key: tuple[str, str],
        since: int,
        attempts: list[tuple[str, Any]],
    ) -> list[tuple[SubagentEvent, Any]]:
        """The remembered attempts found stored, oldest first, by write id."""
        task_id, group = key
        carriers = dict(attempts)
        found: list[tuple[SubagentEvent, Any]] = []
        try:
            async for stored in iter_events_of_class(
                self.store, session_id, since, (SubagentEvent,)
            ):
                if (
                    stored.task_id == task_id
                    and _kind_group(stored.kind) == group
                    and stored.write_id in carriers
                ):
                    found.append((stored, carriers[stored.write_id]))
        except NotImplementedError:
            return []  # no event log to look in: nothing was stored there
        return found

    async def _emit_terminal_with_park(self, event: Event) -> Event:
        if not isinstance(event, _TERMINAL_EVENTS):
            return await self._emit(event)
        async with self._background_lock:
            await self._rehydrate_background_lifecycle_unlocked()
            parked_on = self._park_pending_ids()
            if parked_on:
                await self._emit(SessionParked(
                    session_id=event.session_id,
                    node_id=event.node_id,
                    turn_id=event.turn_id,
                    pending_task_ids=parked_on,
                    reason="background_tasks",
                ))
            emitted = await self._emit(event)
            self._background_turn_terminal = True
            should_wake = bool(self._background_completions)
        if should_wake:
            self._kick_background_wake()
        return emitted

    async def _mark_background_woken(
        self, op: UserTurn, session_id: str, node_id: str, turn_id: str
    ) -> None:
        # Reached by every user turn once its node is written, wake or not: a
        # turn that got this far shows turns can start again, so a latch left
        # by earlier failed wakes lifts here and the retry budget resets.
        self._background_wake_failures = 0
        self._background_wake_blocked = False
        if not op.wake_task_ids:
            return
        async with self._background_lock:
            await self._emit(SessionWoken(
                session_id=session_id,
                node_id=node_id,
                turn_id=turn_id,
                task_ids=op.wake_task_ids,
                reason="background_task_completion",
            ))
            for task_id in op.wake_task_ids:
                self._background_completions.pop(task_id, None)

    def _schedule_background_wake_retry(self) -> None:
        """Kick delivery again after a failed wake, backing off per failure.

        ``wait_turn`` re-kicks on its own as soon as the failed wake settles,
        so the timer only matters when nobody is waiting on the engine.
        """
        if self._background_closed:
            return
        delay = _BACKGROUND_WAKE_RETRY_BASE_SECONDS * 2 ** (
            self._background_wake_failures - 1
        )
        if self._background_wake_retry is not None:
            self._background_wake_retry.cancel()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._background_wake_retry = loop.call_later(
            delay, self._background_wake_retry_due
        )

    def _background_wake_retry_due(self) -> None:
        self._background_wake_retry = None
        self._kick_background_wake()

    def _kick_background_wake(self) -> None:
        if (
            self._background_closed
            or self._background_wake_blocked
            or not self._background_completions
        ):
            return
        current = self._turn_task
        if current is not None and not current.done():
            if self._background_waiting_on is current:
                return
            self._background_waiting_on = current
            current.add_done_callback(self._background_turn_finished)
            return
        if self._background_wake_task is not None and not self._background_wake_task.done():
            return
        try:
            self._background_wake_task = asyncio.create_task(
                self._claim_background_wake()
            )
        except RuntimeError:
            return

    def _background_turn_finished(self, task: asyncio.Task) -> None:
        if self._background_waiting_on is task:
            self._background_waiting_on = None
        self._kick_background_wake()

    async def _claim_background_wake(self) -> None:
        async with self._background_lock:
            if self._background_closed or not self._background_completions:
                return
            current = self._turn_task
            if current is not None and not current.done():
                self._kick_background_wake()
                return
            completions = tuple(self._background_completions.values())
            wake_turn = asyncio.create_task(self._run_background_wake(completions))
            self._turn_task = wake_turn

    async def _run_background_wake(
        self, completions: Iterable[BackgroundTaskFinished]
    ) -> None:
        # Close can win after the wake was claimed but before this task runs.
        # Do not clear its stop flag or start a turn on the closing engine.
        if self._background_closed:
            return
        batch = tuple(completions)
        task_ids = tuple(item.task_id for item in batch)
        self._interrupted.clear()
        try:
            await self._run_turn(UserTurn(
                content=self._background_wake_content(batch),
                wake_task_ids=task_ids,
                source="unpark",
            ))
        finally:
            # The engine failed before persisting the synthetic user node and
            # its SessionWoken marker. Keep the completion and try again a
            # bounded number of times; after that, latch delivery off until a
            # user turn shows the engine can run turns again (or a fresh
            # engine picks the completion up from the store).
            if any(task_id in self._background_completions for task_id in task_ids):
                self._background_wake_failures += 1
                if self._background_wake_failures >= _BACKGROUND_WAKE_MAX_ATTEMPTS:
                    self._background_wake_blocked = True
                else:
                    self._schedule_background_wake_retry()

    @staticmethod
    def _background_wake_content(
        completions: Iterable[BackgroundTaskFinished],
    ) -> str:
        """The synthetic user turn a completed background worker wakes into.

        When the host names a report file, this carries the same contract the
        synchronous Task path does: a head of the report plus the handle that
        opens the whole of it.
        """
        sections = [
            "<system-reminder>",
            (
                "Background work completed while this session was parked. Continue "
                "from the results below. Do not repeat work that is already complete."
            ),
        ]
        for item in completions:
            # Everything below comes from the worker, and this text sits inside
            # the trusted reminder fence: a summary carrying its own closing tag
            # or a steer marker would forge a control channel. Defang all of it.
            task_id = defang_harness_markers(str(item.task_id))
            status = defang_harness_markers(str(item.status))
            sections.append(f"\nTask {task_id} ({status}):")
            summary = defang_harness_markers(item.summary or "(no summary was provided)")
            path = defang_harness_markers((item.report_path or "").strip())
            if not path:
                # Nothing to point at, so nothing may be dropped: an elided
                # summary with no recovery handle is the failure being fixed.
                sections.append(fence_shell_output(summary, f"background task {task_id} output"))
                continue
            if len(summary) > _REPORT_HEAD_CHARS:
                sections.append(fence_shell_output(
                    summary[:_REPORT_HEAD_CHARS].rstrip() + "…",
                    f"background task {task_id} output",
                ))
                sections.append(
                    f"[report head only — {len(summary)} chars in full. The "
                    f"complete report is at {path}: Read it. HistoryExpand "
                    f"cannot help here (the rest was never in this "
                    f"conversation), and re-running the task is never the "
                    f"recovery.]"
                )
            else:
                sections.append(fence_shell_output(summary, f"background task {task_id} output"))
                sections.append(f"[full report: {path} — Read it for detail.]")
        sections.append("</system-reminder>")
        return "\n".join(sections)

    async def _wait_background_turns(self) -> None:
        observed: set[asyncio.Task[Any]] = set()
        while True:
            tasks: list[asyncio.Task[Any]] = []
            for candidate in (self._background_wake_task, self._question_wake_task, self._turn_task):
                if candidate is not None and candidate not in observed and candidate not in tasks:
                    tasks.append(candidate)
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=False)
                observed.update(tasks)
                continue
            await asyncio.sleep(0)
            if (self._background_closed or not self._background_completions
                    or self._background_wake_blocked):
                return
            self._kick_background_wake()

    async def _close_background_lifecycle(self) -> None:
        self._background_closed = True
        question_wake = self._question_wake_task
        if question_wake is not None and not question_wake.done():
            question_wake.cancel()
            await asyncio.gather(question_wake, return_exceptions=True)
        if self._background_wake_retry is not None:
            self._background_wake_retry.cancel()
            self._background_wake_retry = None
        wake = self._background_wake_task
        if wake is not None and not wake.done():
            wake.cancel()
            await await_cancelled_child(wake)

        spawns = set(self._background_spawn_tasks)
        if spawns:
            _, unsettled = await asyncio.wait(
                spawns, timeout=_BACKGROUND_SPAWN_SETTLE_TIMEOUT
            )
            for task in unsettled:
                task.cancel()
            if unsettled:
                await asyncio.gather(*unsettled, return_exceptions=True)

        async with self._background_lock:
            parked_on = self._park_pending_ids()
            if (
                parked_on
                and not self._background_turn_terminal
                and self._session is not None
            ):
                await self._emit(SessionParked(
                    session_id=self._session.session_id,
                    turn_id=self._active_turn_id,
                    pending_task_ids=parked_on,
                    reason="engine_closed",
                ))
