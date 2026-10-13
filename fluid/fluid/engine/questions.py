"""Durable AskUser outbox, shared by native and bundled delegated tools."""
from __future__ import annotations

import asyncio
from typing import Any

from fluid.context.steering import format_steer, format_system_notice
from fluid.events.model import QuestionRequest, QuestionAnswered, QuestionDelivered
from fluid.events.ops import RespondQuestion, UserTurn
from fluid.messages import Text
from fluid.store.base import iter_events, new_id
from fluid.tools.builtins.ask_user import _render_message, _resolve_answer
from fluid.tools.hardening import defang_harness_markers

class QuestionLifecycleMixin:
    def _init_questions(self) -> None:
        self._question_lock = asyncio.Lock()
        self._question_requests = {}
        self._question_answers = {}
        self._question_delivered = set()
        self._questions_loaded = False
        self._question_wake_task = None
        self._question_waiting_on = None
        self.ctx.extra["post_questions"] = self._post_questions

    async def _load_questions(self) -> None:
        if self._questions_loaded:
            return
        meta = await self._ensure_session()
        events = [event async for event in iter_events(self.store, meta.session_id, 0)
                  if isinstance(event, (QuestionRequest, QuestionAnswered, QuestionDelivered))]
        for event in events:
            if isinstance(event, QuestionRequest) and event.nonblocking:
                self._question_requests[event.request_id] = event
            elif isinstance(event, QuestionAnswered):
                self._question_answers[event.request_id] = event
                if event.external_delivery:
                    self._question_delivered.add(event.request_id)
            elif isinstance(event, QuestionDelivered):
                self._question_delivered.update(event.request_ids)
        self._questions_loaded = True

    async def resume_question_delivery(self) -> None:
        """Resume retained automatic answers after reopen; hosts may omit this."""
        async with self._question_lock:
            await self._load_questions()
        self._kick_question_wake()

    async def pending_questions(self) -> list[QuestionRequest]:
        async with self._question_lock:
            await self._load_questions()
            return [q for rid, q in self._question_requests.items()
                    if rid not in self._question_answers]

    async def pending_question_answers(self) -> list[QuestionAnswered]:
        async with self._question_lock:
            await self._load_questions()
            return [a for rid, a in self._question_answers.items()
                    if rid not in self._question_delivered and not a.external_delivery]

    async def _post_questions(
        self, questions: list[dict[str, Any]], *, tool_use_id: str | None = None,
    ) -> list[str]:
        async with self._question_lock:
            await self._load_questions()
            if self._background_closed or self._interrupted.is_set():
                raise RuntimeError("session stopped; questions were not posted")
            batch = tool_use_id or self.ctx.extra.get("tool_use_id") or new_id("ask")
            # One tool call is one durable interaction and one user response.
            # Keep the legacy single-question schema for terminal numeric picks.
            schema = questions[0] if len(questions) == 1 else {"questions": questions}
            event = QuestionRequest(
                session_id=self.session_id, turn_id=self._active_turn_id,
                request_id=new_id("q"), tool_use_id=batch, name="AskUser",
                message="\n\n".join(_render_message(q) for q in questions),
                schema=schema, nonblocking=True,
            )
            await self._emit(event)
            self._question_requests[event.request_id] = event
            return [event.request_id]

    async def _answer_question(self, op: RespondQuestion) -> bool:
        async with self._question_lock:
            await self._load_questions()
            if op.request_id not in self._question_requests:
                return False
            if op.request_id not in self._question_answers:
                event = QuestionAnswered(
                    session_id=self.session_id, request_id=op.request_id,
                    answer=op.answer, user_answered=op.user_answered,
                    wake=op.wake and not op.external_delivery,
                    external_delivery=op.external_delivery,
                )
                await self._emit(event)
                self._question_answers[op.request_id] = event
            if op.external_delivery and op.request_id not in self._question_delivered:
                await self._emit(QuestionDelivered(
                    session_id=self.session_id, request_ids=(op.request_id,),
                ))
                self._question_delivered.add(op.request_id)
        self._kick_question_wake()
        return True

    def _question_text(self, answers: list[QuestionAnswered]) -> str:
        sections = []
        for answer in answers:
            q = self._question_requests[answer.request_id]
            text = (f"Answer to AskUser {answer.request_id} ({q.message}): "
                    + _resolve_answer(answer.answer, q.schema or {}))
            # Even human text cannot inject a second harness marker.
            text = defang_harness_markers(text)
            sections.append(format_steer(text) if answer.user_answered
                            else format_system_notice("Unattributed question response: " + text))
        return "".join(sections)

    async def _question_turn_content(self, op: UserTurn) -> tuple[UserTurn, tuple[str, ...]]:
        answers = await self.pending_question_answers()
        if not answers:
            return op, ()
        from dataclasses import replace
        content = [Text(op.content)] if isinstance(op.content, str) else list(op.content)
        content.append(Text(self._question_text(answers)))
        return replace(op, content=content), tuple(a.request_id for a in answers)

    async def _mark_questions_delivered(self, ids):
        ids = tuple(ids)
        if not ids:
            return
        async with self._question_lock:
            await self._emit(QuestionDelivered(
                session_id=self.session_id, turn_id=self._active_turn_id,
                request_ids=tuple(ids),
            ))
            self._question_delivered.update(ids)

    def _kick_question_wake(self) -> None:
        if self._background_closed or self._interrupted.is_set():
            return
        if not any(a.wake and not a.external_delivery and rid not in self._question_delivered
                   for rid, a in self._question_answers.items()):
            return
        current = self._turn_task
        if current is not None and not current.done():
            if self._question_waiting_on is not current:
                self._question_waiting_on = current
                current.add_done_callback(lambda _: self._kick_question_wake())
            return
        if self._question_wake_task is not None and not self._question_wake_task.done():
            return
        self._question_wake_task = asyncio.create_task(self._wake_questions())

    async def _wake_questions(self) -> None:
        if self._background_closed or self._interrupted.is_set():
            return
        if not any(a.wake and not a.external_delivery and rid not in self._question_delivered
                   for rid, a in self._question_answers.items()):
            return
        current = self._turn_task
        if current is not None and not current.done():
            self._kick_question_wake()
            return
        # Reserve the turn synchronously, just like BackgroundLifecycleMixin.
        delivered_before = len(self._question_delivered)
        self._turn_task = asyncio.create_task(self._run_turn(UserTurn(content=[])))
        await self._turn_task
        self._question_wake_task = None
        # A failed turn leaves answers durable for an explicit retry, rather
        # than spinning on a broken provider/process.
        if len(self._question_delivered) > delivered_before:
            self._kick_question_wake()
