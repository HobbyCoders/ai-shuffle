"""Nonblocking questions through the real tool producer and durable engines."""
import asyncio
import io
import json

import pytest

from fluid.cli.repl import Repl, stream_turn
from fluid.engine.delegated import DelegatedLoop
from fluid.engine.native import NativeLoop
from fluid.events import (
    Interrupt, QuestionAnswered, QuestionDelivered, QuestionRequest,
    RespondQuestion, ToolResult, TurnComplete, UserTurn,
)
from fluid.permissions.engine import PermissionEngine
from fluid.store.base import MemoryStore
from fluid.store.jsonl import JsonlStore
from fluid.tools.builtins import ask_user
from fluid.tools.mcp.bundled_server import _answer
from fluid.tools.registry import ToolCtx, ToolOutcome, ToolRegistry, ToolSpec
from tests.unit.test_tool_loop import ScriptedProvider, _text, _tool

QUESTIONS = [{"question": "Which region?", "options": [
    {"label": "eu-west"}, {"label": "us-east"},
]}]


def ask():
    return _tool("ask-call", "AskUser", json.dumps({"questions": QUESTIONS}))


def make_loop(provider, store=None, sid=None):
    registry = ToolRegistry()
    registry.register_builtin(ask_user.SPEC, ask_user.handler)
    return NativeLoop(provider=provider, store=store or MemoryStore(),
                      session_id=sid, model="test-model", registry=registry,
                      permissions=PermissionEngine("never_ask"))


async def test_post_continue_finish_then_answer_wakes_once():
    provider = ScriptedProvider([ask(), _tool("work", "Work", "{}"),
                                 _text("waiting for decision"), _text("answered")])
    loop = make_loop(provider)
    loop.question_timeout = 0  # Applies only to generic MRTR, never posted questions.
    worked = []

    async def work(input, ctx):
        worked.append(True)
        return ToolOutcome(text="independent work completed")

    loop.registry.register_builtin(ToolSpec(name="Work", description="work", input_schema={}), work)
    await loop.submit(UserTurn(content="start"))
    await asyncio.wait_for(loop.wait_turn(), 5)
    assert worked == [True]
    question, = await loop.pending_questions()
    assert question.nonblocking and question.tool_use_id == "ask-call"
    result = next(e for e in loop.bus.log(loop.session_id)
                  if isinstance(e, ToolResult) and e.name == "AskUser")
    assert json.loads(result.output)["request_ids"] == [question.request_id]
    assert provider.calls == 3
    await loop.submit(RespondQuestion(request_id=question.request_id, answer="1", user_answered=True))
    await asyncio.wait_for(loop.wait_turn(), 5)
    assert provider.calls == 4
    text = provider.requests[-1].messages[-1].text()
    assert "eu-west" in text and "[user interjection" in text
    await loop.submit(RespondQuestion(request_id=question.request_id, answer="2", user_answered=True))
    await loop.wait_turn()
    assert provider.calls == 4
    assert not await loop.pending_questions()
    assert not await loop.pending_question_answers()
    await loop.aclose()


async def test_midturn_delivery_uses_persisted_tool_result_not_an_extra_turn():
    entered, release = asyncio.Event(), asyncio.Event()
    provider = ScriptedProvider([ask(), _tool("work", "Work", "{}"), _text("done")])
    loop = make_loop(provider)

    async def work(input, ctx):
        entered.set()
        await release.wait()
        return ToolOutcome(text="worked")

    loop.registry.register_builtin(ToolSpec(name="Work", description="work", input_schema={}), work)
    await loop.submit(UserTurn(content="start"))
    await asyncio.wait_for(entered.wait(), 5)
    question, = await loop.pending_questions()
    await loop.submit(RespondQuestion(request_id=question.request_id, answer="2", user_answered=True))
    release.set()
    await asyncio.wait_for(loop.wait_turn(), 5)
    assert provider.calls == 3
    result_block = provider.requests[-1].messages[-1].content[-1]
    assert "us-east" in "".join(part.text for part in result_block.content)
    assert not await loop.pending_question_answers()
    history = await loop.store.load_path(loop.session_id)
    assert any("us-east" in part.text for n in history
               for block in n.message.content if hasattr(block, "content")
               for part in block.content if hasattr(part, "text"))
    await loop.aclose()


async def test_reopen_host_owned_answer_stop_and_explicit_next_turn(tmp_path):
    store = JsonlStore(tmp_path)
    loop = make_loop(ScriptedProvider([ask(), _text("done")]), store)
    await loop.submit(UserTurn(content="start"))
    await loop.wait_turn()
    question, = await loop.pending_questions()
    sid = loop.session_id
    await loop.submit(Interrupt())
    await loop.aclose()
    provider = ScriptedProvider([_text("answer used")])
    reopened = make_loop(provider, JsonlStore(tmp_path), sid)
    assert [q.request_id for q in await reopened.pending_questions()] == [question.request_id]
    await reopened.submit(RespondQuestion(request_id=question.request_id, answer="1",
                                         user_answered=True, wake=False))
    await reopened.wait_turn()
    assert provider.calls == 0
    await reopened.aclose()
    final = make_loop(provider, JsonlStore(tmp_path), sid)
    await final.submit(UserTurn(content="continue"))
    await final.wait_turn()
    assert "eu-west" in provider.requests[-1].messages[-1].text()
    assert not await final.pending_question_answers()
    await final.aclose()


@pytest.mark.parametrize("external", [False, True])
async def test_unattributed_and_externally_delivered_answers(external):
    provider = ScriptedProvider([ask(), _text("done"), _text("followup")])
    loop = make_loop(provider)
    await loop.submit(UserTurn(content="start"))
    await loop.wait_turn()
    q, = await loop.pending_questions()
    await loop.submit(RespondQuestion(request_id=q.request_id, answer="policy answer",
                                     external_delivery=external))
    await loop.wait_turn()
    assert provider.calls == (2 if external else 3)
    if not external:
        text = provider.requests[-1].messages[-1].text()
        assert "[harness notice" in text and "[user interjection" not in text
    assert not await loop.pending_question_answers()
    await loop.aclose()


async def test_answer_during_terminal_publication_is_not_lost():
    provider = ScriptedProvider([ask(), _text("done"), _text("followup")])
    loop = make_loop(provider)
    emit = loop._emit

    async def during_terminal(event):
        if isinstance(event, TurnComplete) and provider.calls == 2:
            q, = await loop.pending_questions()
            await loop.submit(RespondQuestion(request_id=q.request_id, answer="1", user_answered=True))
        return await emit(event)

    loop._emit = during_terminal
    await loop.submit(UserTurn(content="start"))
    await asyncio.wait_for(loop.wait_turn(), 5)
    assert provider.calls == 3
    assert not await loop.pending_question_answers()
    await loop.aclose()


async def test_stop_accepts_answer_without_restarting():
    provider = ScriptedProvider([ask(), _text("done"), _text("followup")])
    loop = make_loop(provider)
    await loop.submit(UserTurn(content="start"))
    await loop.wait_turn()
    q, = await loop.pending_questions()
    await loop.submit(Interrupt())
    await loop.submit(RespondQuestion(request_id=q.request_id, answer="1", user_answered=True))
    await loop.wait_turn()
    assert provider.calls == 2
    assert len(await loop.pending_question_answers()) == 1
    await loop.submit(UserTurn(content="continue"))
    await loop.wait_turn()
    assert provider.calls == 3
    await loop.aclose()


async def test_bundled_delegated_builtin_posts_without_mrtr():
    registry = ToolRegistry()
    registry.register_builtin(ask_user.SPEC, ask_user.handler)
    loop = DelegatedLoop(store=MemoryStore(), model="test", registry=registry)
    result = await _answer(registry, loop.ctx, {
        "op": "call", "name": "AskUser", "arguments": {"questions": QUESTIONS},
    })
    assert not result["is_error"]
    q, = await loop.pending_questions()
    assert json.loads(result["text"])["request_ids"] == [q.request_id]
    await loop.submit(RespondQuestion(request_id=q.request_id, answer="1", wake=False))
    assert len(await loop.pending_question_answers()) == 1
    await loop.aclose()


async def test_repl_does_not_auto_answer_persistent_questions():
    provider = ScriptedProvider([ask(), _text("done"), _text("followup")])
    loop = make_loop(provider)
    sid = (await loop._ensure_session()).session_id
    out = io.StringIO()

    async def forbidden(event):
        raise AssertionError("persistent questions must not block the renderer")

    await stream_turn(loop, sid, "start", out=out, on_question=forbidden)
    q, = await loop.pending_questions()
    assert "/answer " + q.request_id in out.getvalue()
    repl = Repl(loop, sid, out=out)
    await repl.dispatch_slash("/questions")
    await repl.dispatch_slash(f"/answer {q.request_id} 1")
    await loop.wait_turn()
    assert "eu-west" in provider.requests[-1].messages[-1].text()
    await loop.aclose()


async def test_delegated_native_question_control_posts_and_idle_answer_wakes():
    from fluid.events import PermissionRequest
    from tests.unit.test_delegated import _make_loop

    loop = _make_loop("q")
    await loop.submit(UserTurn(content="start"))
    await asyncio.wait_for(loop.wait_turn(), 5)
    q, = await loop.pending_questions()
    events = loop.bus.log(loop.session_id)
    assert not any(isinstance(e, PermissionRequest) for e in events)
    assert any(isinstance(e, ToolResult) and '"status": "queued"' in e.output for e in events)
    await loop.submit(RespondQuestion(request_id=q.request_id, answer="eu-west", user_answered=True))
    await asyncio.wait_for(loop.wait_turn(), 5)
    history = await loop.store.load_path(loop.session_id)
    assert any("eu-west" in n.message.text() for n in history)
    assert not await loop.pending_question_answers()
    await loop.aclose()


async def test_question_persistence_failure_never_returns_queued():
    loop = make_loop(ScriptedProvider([ask(), _text("done")]))
    append = loop.store.append_event

    async def fail(sid, event):
        if isinstance(event, QuestionRequest):
            raise OSError("disk full")
        return await append(sid, event)

    loop.store.append_event = fail
    await loop.submit(UserTurn(content="start"))
    await loop.wait_turn()
    results = [e for e in loop.bus.log(loop.session_id) if isinstance(e, ToolResult)]
    assert results and results[0].is_error
    assert '"status": "queued"' not in results[0].output
    assert not await loop.pending_questions()
    await loop.aclose()


async def test_concurrent_duplicate_answers_are_accepted_once():
    loop = make_loop(ScriptedProvider([ask(), _text("done")]))
    await loop.submit(UserTurn(content="start"))
    await loop.wait_turn()
    q, = await loop.pending_questions()
    await asyncio.gather(*(loop.submit(RespondQuestion(
        request_id=q.request_id, answer=str(i), wake=False,
    )) for i in range(8)))
    answers = [e for e in loop.bus.log(loop.session_id) if isinstance(e, QuestionAnswered)]
    assert len(answers) == 1
    await loop.aclose()


async def test_reopen_queries_do_not_wake_until_host_resumes_delivery(tmp_path):
    loop = make_loop(ScriptedProvider([ask(), _text("done")]), JsonlStore(tmp_path))
    await loop.submit(UserTurn(content="start"))
    await loop.wait_turn()
    q, = await loop.pending_questions()
    sid = loop.session_id
    await loop.submit(Interrupt())
    await loop.submit(RespondQuestion(request_id=q.request_id, answer="1", user_answered=True))
    await loop.aclose()
    provider = ScriptedProvider([_text("answer consumed")])
    reopened = make_loop(provider, JsonlStore(tmp_path), sid)
    assert not await reopened.pending_questions()
    assert len(await reopened.pending_question_answers()) == 1
    await reopened.wait_turn()
    assert provider.calls == 0
    await reopened.resume_question_delivery()
    await asyncio.wait_for(reopened.wait_turn(), 5)
    assert provider.calls == 1
    assert "eu-west" in provider.requests[-1].messages[-1].text()
    await reopened.aclose()


async def test_external_retirement_after_automatic_wake_queued_starts_no_empty_turn():
    provider = ScriptedProvider([ask(), _text("done"), _text("unexpected")])
    loop = make_loop(provider)
    await loop.submit(UserTurn(content="start"))
    await loop.wait_turn()
    q, = await loop.pending_questions()
    await loop.submit(RespondQuestion(request_id=q.request_id, answer="1", user_answered=True))
    await loop.submit(RespondQuestion(request_id=q.request_id, answer="1", user_answered=True,
                                     external_delivery=True))
    await loop.wait_turn()
    assert provider.calls == 2
    await loop.aclose()
