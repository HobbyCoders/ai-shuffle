"""The delegated engine reports a stop at the context limit explicitly.

Claude Code ends a turn whose conversation no longer fits with its own
``result.terminal_reason`` (``blocking_limit`` / ``prompt_too_long``). The
mapper turns that into the ``needs_compaction`` outcome on the terminal event,
so the host compacts and continues the work instead of guessing from a stop
reason. Synthetic frames only: no CLI is spawned.
"""

from __future__ import annotations

import pytest

from fluid.engine.delegated_proto import StreamJsonMapper, context_outcome
from fluid.events.model import NEEDS_COMPACTION, TurnComplete


def _result(**fields):
    return {"type": "result", "subtype": "success", "is_error": False, "num_turns": 3,
            "result": "", "usage": {}, "total_cost_usd": 0.0, **fields}


@pytest.mark.parametrize("reason", ["blocking_limit", "prompt_too_long"])
def test_context_limit_terminal_reasons_need_compaction(reason):
    assert context_outcome(_result(terminal_reason=reason, is_error=True)) == NEEDS_COMPACTION


@pytest.mark.parametrize("reason", [
    "completed", "max_turns", "hook_stopped", "tool_deferred", "aborted_tools",
    "model_error", "api_error",
    # The CLI's own breaker for a compaction that refills at once: compacting
    # and continuing would restart the loop it broke.
    "rapid_refill_breaker",
])
def test_other_terminal_reasons_do_not(reason):
    # A CLI that reports terminal_reason is believed over the stop reason.
    assert context_outcome(_result(terminal_reason=reason, stop_reason="tool_use")) is None


def test_old_cli_without_terminal_reason_falls_back_to_the_tool_use_cut():
    assert context_outcome(_result(stop_reason="tool_use")) == NEEDS_COMPACTION
    assert context_outcome(_result(stop_reason="end_turn")) is None
    assert context_outcome(_result(stop_reason="tool_use", is_error=True)) is None
    assert context_outcome(_result(stop_reason="tool_use", subtype="error_max_turns")) is None


def _mapper():
    return StreamJsonMapper(session_id="s", turn_id="t", model="claude-test", user_node_id="u")


def test_mapper_puts_the_outcome_on_turn_complete():
    mapper = _mapper()
    mapped = mapper.feed(_result(terminal_reason="completed", stop_reason="tool_use"))
    [done] = [e for e in mapped.events if isinstance(e, TurnComplete)]
    assert done.outcome is None

    mapper = _mapper()
    mapped = mapper.feed(_result(stop_reason="tool_use"))  # an older CLI's cut
    [done] = [e for e in mapped.events if isinstance(e, TurnComplete)]
    assert done.outcome == NEEDS_COMPACTION


def test_mapper_keeps_the_outcome_for_a_failed_result():
    mapper = _mapper()
    mapped = mapper.feed(_result(is_error=True, terminal_reason="blocking_limit",
                                 result="Prompt is too long"))
    assert mapped.is_result and mapped.error
    assert not [e for e in mapped.events if isinstance(e, TurnComplete)]
    assert mapper.failed_outcome == NEEDS_COMPACTION


# ── the host's emergency compaction may fall back to Claude Code's summarizer ──


async def test_only_the_emergency_compaction_may_fall_back_on_a_no_op():
    from fluid.engine.delegated import NOTHING_TO_COMPACT
    from fluid.events.ops import UserTurn

    from tests.unit.test_claude_mod_compaction_cycles import Chat, _loop_with, _request

    chat = Chat(result_chars=100)
    chat.grow(3)
    loop, _ = await _loop_with(chat)
    try:
        request = {'v': 1, 'trigger': 'manual', 'messages': chat.cli}
        # A /compact a person sent: an honest no-op, never the summarizer.
        loop._turn_compact_requested, loop._turn_emergency_compaction = True, False
        assert await _request(loop._socket_path, request, loop._mod_token) == (
            200, {'skip': NOTHING_TO_COMPACT})
        assert loop._compact_noop
        # The host's emergency compaction at the context limit: fall back.
        loop._compact_noop = False
        loop._turn_emergency_compaction = True
        assert await _request(loop._socket_path, request, loop._mod_token) == (
            200, {'fallback': NOTHING_TO_COMPACT})
        assert not loop._compact_noop
    finally:
        await loop.aclose()
    # The op carries the flag; off unless the host sets it.
    assert UserTurn(content=[]).emergency_compaction is False
