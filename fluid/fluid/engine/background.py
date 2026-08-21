"""Durable park/wake lifecycle for host-owned background tasks."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from typing import Any

from fluid.events.model import (
    ErrorEvent,
    Event,
    Interrupted,
    SessionParked,
    SessionWoken,
    SubagentEvent,
    TurnComplete,
)
from fluid.events.ops import BackgroundTaskFinished, UserTurn
from fluid.tools.registry import BackgroundTaskReceipt

_TERMINAL_EVENTS = (TurnComplete, Interrupted, ErrorEvent)
_TERMINAL_TASK_KINDS = {"finished", "failed", "cancelled"}
_BACKGROUND_SPAWN_SETTLE_TIMEOUT = 5.0


class BackgroundLifecycleMixin:
    """Engine mixin implementing durable background-task park and wake.

    The embedding host owns worker execution. Fluid owns the session semantics:
    a structured spawn receipt becomes a durable ``SubagentEvent(started)``;
    terminal turns with live workers emit ``SessionParked``; and a trusted host
    submits ``BackgroundTaskFinished`` to persist the result and start a fresh
    continuation turn. Fluid tracks only the short spawn handshake, never the
    host worker itself.
    """

    def _init_background_lifecycle(self) -> None:
        self._background_pending: dict[str, BackgroundTaskReceipt] = {}
        self._background_terminal: set[str] = set()
        self._background_early: dict[str, BackgroundTaskFinished] = {}
        self._background_completions: dict[str, BackgroundTaskFinished] = {}
        self._background_rehydrated = False
        self._background_lock = asyncio.Lock()
        self._background_wake_task: asyncio.Task | None = None
        self._background_waiting_on: asyncio.Task | None = None
        self._background_wake_blocked = False
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
            events = await self.store.events_since(meta.session_id, 0)
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
                        )
                elif event.kind == "completion_pending":
                    if event.task_id not in self._background_terminal:
                        self._background_early[event.task_id] = BackgroundTaskFinished(
                            task_id=event.task_id,
                            status=event.status or "finished",
                            summary=event.summary,
                            worker_session_id=event.worker_session_id,
                            wake=event.wake,
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
                ))
                early = self._background_early.pop(receipt.task_id, None)
                if early is not None:
                    await self._finalize_background_task_unlocked(early)
                elif self._background_turn_terminal:
                    await self._emit(SessionParked(
                        session_id=meta.session_id,
                        turn_id=self._active_turn_id,
                        pending_task_ids=tuple(sorted(self._background_pending)),
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
        ))
        if op.wake:
            self._background_completions[op.task_id] = BackgroundTaskFinished(
                task_id=op.task_id,
                status=op.status,
                summary=op.summary,
                worker_session_id=worker_session_id,
                wake=True,
            )

    async def _emit_terminal_with_park(self, event: Event) -> Event:
        if not isinstance(event, _TERMINAL_EVENTS):
            return await self._emit(event)
        async with self._background_lock:
            await self._rehydrate_background_lifecycle_unlocked()
            if self._background_pending:
                await self._emit(SessionParked(
                    session_id=event.session_id,
                    node_id=event.node_id,
                    turn_id=event.turn_id,
                    pending_task_ids=tuple(sorted(self._background_pending)),
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
            self._background_wake_blocked = False

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
            # If the engine failed before persisting the synthetic user node and
            # its SessionWoken marker, retain the completion for restart/retry but
            # do not spin in-process. A fresh engine may attempt delivery again.
            if any(task_id in self._background_completions for task_id in task_ids):
                self._background_wake_blocked = True

    @staticmethod
    def _background_wake_content(
        completions: Iterable[BackgroundTaskFinished],
    ) -> str:
        sections = [
            "<system-reminder>",
            (
                "Background work completed while this session was parked. Continue "
                "from the results below. Do not repeat work that is already complete."
            ),
        ]
        for item in completions:
            sections.append(f"\nTask {item.task_id} ({item.status}):")
            sections.append(item.summary or "(no summary was provided)")
        sections.append("</system-reminder>")
        return "\n".join(sections)

    async def _wait_background_turns(self) -> None:
        observed: set[asyncio.Task[Any]] = set()
        while True:
            tasks: list[asyncio.Task[Any]] = []
            for candidate in (self._background_wake_task, self._turn_task):
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
            if (
                self._background_pending
                and not self._background_turn_terminal
                and self._session is not None
            ):
                await self._emit(SessionParked(
                    session_id=self._session.session_id,
                    turn_id=self._active_turn_id,
                    pending_task_ids=tuple(sorted(self._background_pending)),
                    reason="engine_closed",
                ))
