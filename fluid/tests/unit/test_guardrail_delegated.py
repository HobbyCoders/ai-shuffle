"""The delegated Claude CLI leg of the provider safety check.

The CLI's own refusal fallback is switched off (``FORCED_CHILD_ENV``); a
declined reply raises the same ``ModelGuardrail`` prompt the native loop does.
Continue switches the CLI to the offered model, re-sends the turn's message and
keeps it in the same fluid turn; the next turn's drain puts the CLI back on the
session's model. Stop ends the turn with ``stop_reason="refusal"``.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from fluid.engine.delegated import DelegatedLoop
from fluid.engine.delegated_proto import FORCED_CHILD_ENV, StreamJsonMapper
from fluid.engine.guardrail import GUARDRAIL_TOOL
from fluid.events.model import (
    GuardrailBlocked,
    ModelServed,
    PermissionRequest,
    TurnComplete,
)
from fluid.events.ops import RespondPermission, UserTurn
from fluid.store.base import MemoryStore

SCRIPTED = str(Path(__file__).parent.parent / "fixtures" / "fake_claude_script.py")

_NO_FALLBACK = {
    "type": "system", "subtype": "model_refusal_no_fallback", "uuid": "u-1",
    "original_model": "claude-opus-5-5", "request_id": "req_1",
    "api_refusal_category": "cyber", "api_refusal_explanation": None,
    "refused_user_message_uuid": "u-0", "content": "Declined by the safety check",
}
_REFUSED = {
    "type": "assistant", "isApiErrorMessage": True, "message": {
        "id": "msg_refused", "model": "claude-opus-5-5", "role": "assistant",
        "content": [{"type": "text", "text": "API Error: the request was declined"}],
        "stop_reason": "refusal",
        "stop_details": {"type": "refusal", "category": "cyber", "explanation": None},
        "usage": {"input_tokens": 10, "output_tokens": 1},
    },
}


def _refused_turn() -> list[dict]:
    return [
        {"op": "frame", "frame": _NO_FALLBACK},
        {"op": "frame", "frame": _REFUSED},
        {"op": "result"},
    ]


def _loop(tmp_path, turns: list[list[dict]]) -> tuple[DelegatedLoop, Path]:
    script = tmp_path / "script.json"
    record = tmp_path / "record.jsonl"
    script.write_text(json.dumps({"turns": turns}), encoding="utf-8")
    loop = DelegatedLoop(
        store=MemoryStore(), model="claude-opus-5-5", claude_cmd=[sys.executable, SCRIPTED],
        env={"FAKE_CLAUDE_SCRIPT": str(script), "FAKE_CLAUDE_RECORD": str(record)},
        cwd=str(tmp_path),
    )
    return loop, record


def _records(record: Path, kind: str) -> list[dict]:
    if not record.exists():
        return []
    rows = [json.loads(line) for line in record.read_text(encoding="utf-8").splitlines() if line]
    return [r for r in rows if r.get("kind") == kind]


async def _answer(loop: DelegatedLoop, decision: str, **extra) -> PermissionRequest:
    while True:
        log = loop.bus.log(loop.session_id) if loop._session is not None else []
        event = next(
            (e for e in log if isinstance(e, PermissionRequest) and e.name == GUARDRAIL_TOOL),
            None,
        )
        if event is not None:
            break
        await asyncio.sleep(0.02)
    await loop.submit(RespondPermission(request_id=event.request_id, decision=decision, **extra))
    return event


def test_the_cli_auto_fallback_is_forced_off() -> None:
    assert FORCED_CHILD_ENV["CLAUDE_CODE_DISABLE_REFUSAL_FALLBACK"] == "1"


def test_mapper_reads_refusals_and_reroutes() -> None:
    mapper = StreamJsonMapper(
        session_id="s", turn_id="t", model="claude-opus-5-5", user_node_id="n",
    )
    mapper.feed(_NO_FALLBACK)
    assert mapper.refusal == {
        "category": "cyber", "explanation": None,
        "requested_model": "claude-opus-5-5", "request_id": "req_1",
    }
    fallback = mapper.feed({
        "type": "system", "subtype": "model_refusal_fallback", "trigger": "refusal",
        "direction": "retry", "scope": "session", "original_model": "claude-opus-5-5",
        "fallback_model": "claude-opus-4-8", "api_refusal_category": "cyber",
        "content": "", "uuid": "u-2",
    })
    served = [e for e in fallback.events if isinstance(e, ModelServed)]
    assert served and served[0].rerouted and served[0].reason == "refusal_fallback"
    local = mapper.feed({
        "type": "system", "subtype": "model_fallback", "scope": "local",
        "original_model": "claude-opus-5-5", "fallback_model": "claude-haiku-5",
    })
    assert local.events == []

    alias = StreamJsonMapper(session_id="s", turn_id="t", model="opus", user_node_id="n")
    mapped = alias.feed({"type": "assistant", "message": {
        "id": "m1", "model": "claude-opus-5-5-20261001", "role": "assistant",
        "content": [{"type": "text", "text": "hi"}],
    }})
    served = [e for e in mapped.events if isinstance(e, ModelServed)]
    assert len(served) == 1 and not served[0].rerouted


async def test_continue_resends_on_the_suggested_model_then_restores(tmp_path) -> None:
    loop, record = _loop(tmp_path, [
        _refused_turn(),
        [{"op": "text", "text": "retried answer", "model": "claude-opus-4-8"}, {"op": "result"}],
        [{"op": "text", "text": "next turn", "model": "claude-opus-5-5"}, {"op": "result"}],
    ])
    watcher = asyncio.create_task(_answer(loop, "allow"))
    try:
        await loop.submit(UserTurn(content="audit this binary"))
        prompt = await asyncio.wait_for(watcher, 20)
        await asyncio.wait_for(loop.wait_turn(), 20)
        events = loop.bus.log(loop.session_id)
        assert prompt.input["suggested_model"] == "claude-opus-4-8"
        assert prompt.input["category"] == "cyber"
        blocked = next(e for e in events if isinstance(e, GuardrailBlocked))
        assert blocked.outcome == "continued" and blocked.retry_model == "claude-opus-4-8"
        done = [e for e in events if isinstance(e, TurnComplete)]
        assert len(done) == 1 and done[0].stop_reason == "end_turn"
        served = [e for e in events if isinstance(e, ModelServed)]
        assert [s.served_model for s in served] == ["claude-opus-5-5", "claude-opus-4-8"]
        assert not any(s.rerouted for s in served)  # the retry asked for 4.8
        users = [r for r in _records(record, "stdin") if r.get("frame", {}).get("type") == "user"]
        assert len(users) == 2  # the turn's message, sent again after the switch
        assert loop.model == "claude-opus-5-5"

        await loop.submit(UserTurn(content="thanks"))
        await asyncio.wait_for(loop.wait_turn(), 20)
        applied = [r["value"] for r in _records(record, "applied") if r["subtype"] == "set_model"]
        assert applied == ["claude-opus-4-8", "claude-opus-5-5"]
    finally:
        watcher.cancel()
        await loop.aclose()


async def test_stop_ends_the_turn_as_a_refusal(tmp_path) -> None:
    loop, record = _loop(tmp_path, [_refused_turn()])
    watcher = asyncio.create_task(_answer(loop, "deny", message="declined"))
    try:
        await loop.submit(UserTurn(content="audit this binary"))
        await asyncio.wait_for(watcher, 20)
        await asyncio.wait_for(loop.wait_turn(), 20)
        events = loop.bus.log(loop.session_id)
        blocked = next(e for e in events if isinstance(e, GuardrailBlocked))
        assert blocked.outcome == "stopped" and blocked.reason == "declined"
        done = next(e for e in events if isinstance(e, TurnComplete))
        assert done.stop_reason == "refusal"
        assert _records(record, "applied") == []
    finally:
        watcher.cancel()
        await loop.aclose()
