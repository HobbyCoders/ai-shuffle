"""Durable park/wake lifecycle for host-owned background tasks."""

from __future__ import annotations

import asyncio
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
from fluid.store.base import iter_events
from fluid.tools.hardening import defang_harness_markers, fence_shell_output
from fluid.tools.registry import BackgroundTaskReceipt

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


class BackgroundLifecycleMixin(QuestionLifecycleMixin):
    """Engine mixin implementing durable background-task park and wake.

    The embedding host owns worker execution. Fluid owns the session semantics:
    a structured spawn receipt becomes a durable ``SubagentEvent(started)``;
    terminal turns with live workers emit ``SessionParked``; and a trusted host
    submits ``BackgroundTaskFinished`` to persist the result and start a fresh
    continuation turn. Fluid tracks only the short spawn handshake, never the
    host worker itself.
    """

    def _init_background_lifecycle(self) -> None:
        self._init_questions()
        self._background_pending: dict[str, BackgroundTaskReceipt] = {}
        self._background_terminal: set[str] = set()
        self._background_early: dict[str, BackgroundTaskFinished] = {}
        self._background_completions: dict[str, BackgroundTaskFinished] = {}
        self._background_rehydrated = False
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

    async def _rehydrate_background_lifecycle_unlocked(self) -> None:
        if self._background_rehydrated:
            return
        meta = await self._ensure_session()
        try:
            events = [event async for event in iter_events(self.store, meta.session_id, 0)
                      if isinstance(event, (SubagentEvent, SessionWoken))]
        except Exception:  # noqa: BLE001 — stores without replay degrade in memory
            self._background_rehydrated = True
            return

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

        for task_id in tuple(self._background_early):
            if task_id in self._background_pending:
                op = self._background_early.pop(task_id)
                await self._finalize_background_task_unlocked(op)

        recover_host = self.ctx.extra.get("recover_background")
        if recover_host is not None:
            for op in await recover_host(tuple(self._background_pending)):
                if op.task_id in self._background_pending:
                    await self._finalize_background_task_unlocked(op)

    def _park_pending_ids(self) -> tuple[str, ...]:
        """The pending task ids a park is actually WAITING on.

        A ``serves`` receipt (a dev server, a tunnel — see
        ``BackgroundTaskReceipt.serves``) is tracked and surfaced like any
        other background task, but it is not work whose result the session
        wants: its only "completion" is its own death. Counting it parked the
        session on every turn that had a server running, so it is excluded
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
                self._background_pending[receipt.task_id] = receipt
                meta = await self._ensure_session()
                await self._emit(SubagentEvent(
                    session_id=meta.session_id,
                    turn_id=self._active_turn_id,
                    task_id=receipt.task_id,
                    kind="started",
                    description=receipt.description,
                    worker_session_id=receipt.worker_session_id,
                    # A task that has only just started owes nobody a wake yet.
                    # The default wake=True here was the dataclass default, not
                    # a claim about anything.
                    wake_disposition=WAKE_NOT_APPLICABLE,
                    serves=receipt.serves,
                ))
                early = self._background_early.pop(receipt.task_id, None)
                if early is not None:
                    await self._finalize_background_task_unlocked(early)
                elif self._background_turn_terminal:
                    parked_on = self._park_pending_ids()
                    if parked_on:
                        await self._emit(SessionParked(
                            session_id=meta.session_id,
                            turn_id=self._active_turn_id,
                            pending_task_ids=parked_on,
                            reason="background_tasks",
                        ))
            should_wake = bool(self._background_completions)
        if should_wake:
            self._kick_background_wake()

    async def _finish_background_task(self, op: BackgroundTaskFinished) -> None:
        if op.status not in _TERMINAL_TASK_KINDS:
            raise ValueError(
                "background task status must be finished, failed, or cancelled"
            )

        async with self._background_lock:
            await self._rehydrate_background_lifecycle_unlocked()
            if op.task_id not in self._background_terminal:
                if op.task_id in self._background_pending:
                    await self._finalize_background_task_unlocked(op)
                elif op.task_id not in self._background_early:
                    self._background_early[op.task_id] = op
                    meta = await self._ensure_session()
                    await self._emit(SubagentEvent(
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
                    ))
            should_wake = bool(self._background_completions)
        if should_wake:
            self._kick_background_wake()

    async def _finalize_background_task_unlocked(
        self, op: BackgroundTaskFinished
    ) -> None:
        meta = await self._ensure_session()
        receipt = self._background_pending.pop(op.task_id, None)
        worker_session_id = op.worker_session_id or (
            receipt.worker_session_id if receipt is not None else None
        )
        self._background_terminal.add(op.task_id)
        await self._emit(SubagentEvent(
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
        ))
        if op.wake:
            self._background_completions[op.task_id] = BackgroundTaskFinished(
                task_id=op.task_id,
                status=op.status,
                summary=op.summary,
                worker_session_id=worker_session_id,
                wake=True,
                report_path=op.report_path,
            )

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
        batch = tuple(completions)
        task_ids = tuple(item.task_id for item in batch)
        self._interrupted.clear()
        try:
            await self._run_turn(UserTurn(
                content=self._background_wake_content(batch),
                wake_task_ids=task_ids,
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

        A *synchronous* Task hands the model the worker's report directly. A
        background one used to hand it whatever summary the host chose to
        include, with no way back to the rest — so a report that arrived elided
        cost two HistoryExpand round-trips to recover text that had never been
        in history. When the host names a report file, this carries the same
        contract the sync path does: a head of the report plus the handle that
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
            if not self._background_completions or self._background_wake_blocked:
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
            try:
                await wake
            except asyncio.CancelledError:
                pass

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
