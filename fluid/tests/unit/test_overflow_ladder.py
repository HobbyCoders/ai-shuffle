"""Overflow recovery: the ladder's guard-off rungs, the trigger clamp on small
windows, and status-aware overflow detection.

The provider rejecting a prompt is the one signal the estimator cannot argue
with. Recovery has to be able to shrink the newest turn — the recency guards
that keep the normal ladder cache-safe are exactly what stood in the way.
"""

import pytest

from fluid.context.compaction import (
    CompactionConfig,
    Compactor,
    Promotion,
    derive_trigger_tokens,
)
from fluid.context.trim import TrimConfig, estimate_tokens_of_messages
from fluid.engine.native import _is_context_overflow
from fluid.messages import Message, Text, ToolResultBlock, ToolUseBlock
from fluid.store.base import SessionNode

WINDOW = 20_000
RESERVE = 32_000  # the engine's default output reserve, larger than the window


def _node(nid: str, role: str, blocks: list) -> SessionNode:
    return SessionNode(node_id=nid, parent_id=None,
                       message=Message(role=role, content=blocks))


def _oversize_turn(
    t: int, *, results: int = 3, result_chars: int = 40_000, error_at: int = -1
) -> list[SessionNode]:
    """One in-flight turn whose fresh tool results alone outgrow WINDOW.

    Each result is under ``tool_result_stub_chars`` (50k), so the stateless
    stub leaves it alone, and there are fewer than ``keep_recent_tool_results``
    (5) of them, so the guarded tier-1 rung leaves it alone too.
    """
    nodes = [_node(f"u{t}", "user", [Text(f"turn {t}")])]
    for k in range(results):
        nodes.append(_node(f"a{t}_{k}", "assistant", [
            ToolUseBlock(id=f"call{t}_{k}", name="Read", input={"file_path": f"{k}.py"}),
        ]))
        nodes.append(_node(f"r{t}_{k}", "user", [
            ToolResultBlock(
                tool_use_id=f"call{t}_{k}",
                content=[Text(f"payload-{k} " + "x" * result_chars)],
                is_error=(k == error_at),
            )
        ]))
    return nodes


def _texts(messages: list[Message]) -> str:
    return "\n".join(
        getattr(b, "text", "")
        for m in messages
        for b in list(m.content) + [
            c for b in m.content for c in getattr(b, "content", None) or []
        ]
        if isinstance(getattr(b, "text", None), str)
    )


def _target(window: int) -> int:
    return derive_trigger_tokens(
        base=120_000, window_ratio=0.6, context_window=window,
        headroom_tokens=16_000, output_reserve_tokens=RESERVE,
    )


# ─── CTX-6: trigger clamp ────────────────────────────────────────────────────


def test_trigger_stays_inside_every_window_and_grows_with_it() -> None:
    """Defaults (headroom 16k, reserve 32k) used to give a 32k window a
    trigger of -16,000 and an 8k window -39,808."""
    windows = [8_000, 16_000, 32_000, 64_000, 128_000, 200_000, 1_000_000]
    got = [_target(w) for w in windows]
    for window, trigger in zip(windows, got):
        assert 0 < trigger < window, (window, trigger)
    assert got == sorted(got)
    assert got[windows.index(200_000)] == 120_000  # the historical trigger
    assert got[windows.index(1_000_000)] == 600_000
    # Small windows sit on the half-window floor; the ceiling cannot bind.
    assert got[windows.index(8_000)] == 4_000
    assert got[windows.index(32_000)] == 16_000


def test_trigger_floor_never_lifts_a_pinned_base() -> None:
    """ratio 0 pins ``base``; the floor rescues a collapsed ceiling only."""
    assert derive_trigger_tokens(
        base=120_000, window_ratio=0.0, context_window=1_000_000,
        headroom_tokens=16_000, output_reserve_tokens=0,
    ) == 120_000


# ─── CTX-1: guard-off rungs ──────────────────────────────────────────────────


def test_escalate_with_a_window_trims_the_newest_turn_in_one_call() -> None:
    nodes = _oversize_turn(0)
    compactor = Compactor(config=CompactionConfig())  # keep 5 results, 3 turns
    assert estimate_tokens_of_messages(compactor.view(nodes)) > WINDOW

    promos = compactor.escalate(
        nodes, context_window=WINDOW, output_reserve_tokens=RESERVE
    )
    assert [(p.tier, p.reason) for p in promos] == [(1, "overflow")]
    assert len(promos[0].trimmed) == 3
    assert promos[0].reclaimed_chars > 100_000
    after = estimate_tokens_of_messages(compactor.view(nodes))
    assert after <= _target(WINDOW) < WINDOW
    # Every result went to the recoverable placeholder, not the digest.
    assert _texts(compactor.view(nodes)).count("old tool result trimmed") == 3
    assert compactor._digested == {}


def test_escalate_without_a_window_climbs_one_rung_per_call() -> None:
    """The engine retries after each call, so an unknown window keeps the
    original one-rung cadence: guarded rungs first, then the guard-off ones."""
    nodes = _oversize_turn(0)
    compactor = Compactor(config=CompactionConfig())
    assert [p.tier for p in compactor.escalate(nodes)] == [1]  # guard-off trim
    assert [p.tier for p in compactor.escalate(nodes)] == [2]  # digest in-flight turn
    assert [p.tier for p in compactor.escalate(nodes)] == [3]  # fold that digest
    assert compactor.escalate(nodes) == []


def test_escalate_prefers_old_history_before_the_guard_comes_off() -> None:
    """With older turns available the guarded rungs still go first, and with
    a window the call stops as soon as the estimate is under the trigger."""
    nodes = _oversize_turn(0, results=1) + _oversize_turn(1, results=1) \
        + _oversize_turn(2, results=1) + _oversize_turn(3, results=1)
    cfg = CompactionConfig(
        keep_recent_turns=1, trim=TrimConfig(keep_recent_tool_results=2),
    )
    compactor = Compactor(config=cfg)
    promos = compactor.escalate(
        nodes, context_window=WINDOW, output_reserve_tokens=RESERVE
    )
    tiers = [p.tier for p in promos]
    # Ladder order: guarded trim, guarded digest, fold, then the guard-off
    # trim and digest. Here the first three could not get under the target,
    # so the guard-off trim ran too; the in-flight turn was never digested.
    assert tiers == [1, 2, 3, 1]
    assert all(p.reason == "overflow" for p in promos)
    assert estimate_tokens_of_messages(compactor.view(nodes)) <= _target(WINDOW)
    assert "turn 3" in _texts(compactor.view(nodes))


def test_escalation_is_sticky_and_rehydrates_to_the_same_view() -> None:
    nodes = _oversize_turn(0)
    compactor = Compactor(config=CompactionConfig())
    promos = compactor.escalate(
        nodes, context_window=WINDOW, output_reserve_tokens=RESERVE
    )
    view = compactor.view(nodes)
    assert compactor.view(nodes) == view  # byte-identical on re-render

    fresh = Compactor(config=CompactionConfig())
    fresh.rehydrate(
        Promotion(tier=p.tier, reason=p.reason, trimmed=p.trimmed, spans=p.spans)
        for p in promos
    )
    assert fresh.view(nodes) == view


def test_guard_off_rung_still_keeps_errors_verbatim() -> None:
    """Dropping the recency guard does not drop the error guard: the error
    result stays live and the two successes beside it take the placeholder."""
    nodes = _oversize_turn(0, result_chars=30_000, error_at=1)
    compactor = Compactor(config=CompactionConfig())
    promos = compactor.escalate(
        nodes, context_window=WINDOW, output_reserve_tokens=RESERVE
    )
    assert [p.tier for p in promos] == [1]
    trimmed = {tuple(p) for pr in promos for p in pr.trimmed}
    assert trimmed == {("r0_0", "call0_0"), ("r0_2", "call0_2")}
    assert "payload-1 " + "x" * 30_000 in _texts(compactor.view(nodes))
    assert compactor._digested == {}


def test_oversize_newest_result_is_stubbed_before_any_rung() -> None:
    """A 200k-char result renders as the stateless stub even in the in-flight
    turn, so no shorter emergency stub is needed for that case."""
    nodes = _oversize_turn(0, results=1, result_chars=200_000)
    compactor = Compactor(config=CompactionConfig())
    text = _texts(compactor.view(nodes))
    assert "[large tool result: " in text
    assert estimate_tokens_of_messages(compactor.view(nodes)) < 1_000


def test_trim_only_escalation_gets_the_guard_off_trim_and_nothing_else() -> None:
    nodes = _oversize_turn(0)
    compactor = Compactor(
        config=CompactionConfig(enabled=False, trim=TrimConfig())
    )
    promos = compactor.escalate(
        nodes, context_window=WINDOW, output_reserve_tokens=RESERVE
    )
    assert [p.tier for p in promos] == [1]
    assert compactor._digested == {}
    assert compactor.escalate(
        nodes, context_window=WINDOW, output_reserve_tokens=RESERVE
    ) == []


# ─── CTX-7: overflow detection ───────────────────────────────────────────────


@pytest.mark.parametrize(
    ("message", "status", "expected"),
    [
        ("Payload Too Large", 413, True),
        ("<html>413 Request Entity Too Large</html>", 413, True),
        (
            "The input token count (1200000) exceeds the maximum number of "
            "tokens allowed (1048576).",
            None,
            True,
        ),
        ("prompt is too long: 210000 tokens > 200000 maximum", 400, True),
        ("This model's maximum context length is 128000 tokens", 400, True),
        ("too many tokens in request", None, True),
        ("upstream exploded", 500, False),
        ("upstream exploded", None, False),
        ("", None, False),
    ],
)
def test_overflow_detection(message: str, status: int | None, expected: bool) -> None:
    assert _is_context_overflow(message, status) is expected
