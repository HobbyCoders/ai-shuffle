"""The per-turn model-call cap holds for every provider dispatch.

native.md Medium #4: the cap was checked only before tool dispatch and the
max_tokens continuation, so an empty-response retry or a Stop-hook
continuation sent request N+1 on a turn capped at N. Every dispatch now counts
(any of them can bill) and the cap is checked before each one.
"""

from __future__ import annotations

import pytest

from fluid.engine import native
from fluid.events import HookEvent, ProviderRetry, TurnComplete
from fluid.hooks.bus import TURN_END, HookBus, HookDecision
from tests.unit.test_empty_completion_retry import ScriptedProvider, _empty, _run, _text


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(native, "_empty_retry_delay", lambda attempt: 0.0)


def _complete(loop, sid) -> TurnComplete:
    done = [e for e in loop.bus.log(sid) if isinstance(e, TurnComplete)]
    assert len(done) == 1
    return done[0]


async def test_an_empty_response_retry_does_not_exceed_the_cap() -> None:
    provider = ScriptedProvider([_empty(), _text("late answer")])
    loop, _store, sid = await _run(provider, max_model_calls_per_turn=1)

    assert provider.calls == 1
    assert not [e for e in loop.bus.log(sid) if isinstance(e, ProviderRetry)]
    done = _complete(loop, sid)
    assert done.stop_reason == "max_calls"
    assert done.num_model_calls == 1


async def test_the_cap_still_allows_the_retries_it_has_room_for() -> None:
    provider = ScriptedProvider([_empty(), _text("answer")])
    loop, _store, sid = await _run(provider, max_model_calls_per_turn=2)

    assert provider.calls == 2
    done = _complete(loop, sid)
    assert done.stop_reason == "end_turn"
    assert done.num_model_calls == 2


async def test_a_stop_hook_continuation_does_not_exceed_the_cap() -> None:
    hooks = HookBus()
    fired: list[str] = []

    async def keep_going(event, payload):
        fired.append(event)
        return HookDecision(action="deny", note="tests are not run yet")

    hooks.on(TURN_END, keep_going)
    provider = ScriptedProvider([_text("first"), _text("second")])
    loop, _store, sid = await _run(provider, hooks=hooks, max_model_calls_per_turn=1)

    assert provider.calls == 1
    assert fired == [TURN_END]  # the TurnEnd hooks ran once, not again at the end
    warnings = [
        e.message for e in loop.bus.log(sid)
        if isinstance(e, HookEvent) and e.hook_event == TURN_END
    ]
    assert any("model-call limit" in m for m in warnings)
    assert _complete(loop, sid).num_model_calls == 1
