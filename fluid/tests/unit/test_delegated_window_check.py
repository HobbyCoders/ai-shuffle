"""#754: after Claude Code starts, the delegated leg compares the context
window the CLI really uses (its first context-usage report) with the one the
host pinned, and reports a mismatch on the session."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fluid.engine.delegated import DelegatedLoop
from fluid.events.model import DelegatedDiagnostic
from fluid.events.ops import UserTurn
from fluid.store.base import MemoryStore

FAKE = str(Path(__file__).parent.parent / "fixtures" / "fake_claude.py")


@pytest.fixture(autouse=True)
def _no_inherited_pins(monkeypatch) -> None:
    """The CLI child inherits the host environment: a pin the test runner
    itself runs under must not stand in for the one each test sets."""
    for name in ("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "CLAUDE_CODE_MAX_CONTEXT_TOKENS",
                 "CLAUDE_CODE_BLOCKING_LIMIT_OVERRIDE"):
        monkeypatch.delenv(name, raising=False)


def _mismatches(loop: DelegatedLoop) -> list[DelegatedDiagnostic]:
    return [e for e in loop.bus.log(loop.session_id)
            if isinstance(e, DelegatedDiagnostic) and e.kind == "context_window_mismatch"]


async def _run(env: dict[str, str], compaction_window: int) -> DelegatedLoop:
    """One turn against the fake CLI, whose context-usage report names a
    200,000-token window (scenario j)."""
    loop = DelegatedLoop(
        store=MemoryStore(), model="claude-fake", claude_cmd=[sys.executable, FAKE],
        env={"FAKE_CLAUDE_SCENARIO": "j", **env}, compaction_window=compaction_window,
    )
    try:
        await loop.submit(UserTurn(content="hi"))
        await loop.wait_turn()
    finally:
        await loop.aclose()
    return loop


async def test_a_pinned_window_the_cli_does_not_use_is_reported_once() -> None:
    loop = await _run({"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "400000"}, 400_000)
    [found] = _mismatches(loop)
    assert found.data["expected"] == 400_000 and found.data["effective"] == 200_000
    assert found.data["pinned"] is True and found.data["raw_max_tokens"] == 200_000
    assert "200,000-token context window" in found.message
    assert "400,000 tokens this chat is set to" in found.message
    # Fluid's compactor follows the smaller window the CLI really uses.
    assert loop.compaction_window == 200_000


async def test_a_matching_window_reports_nothing() -> None:
    loop = await _run({"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "200000"}, 200_000)
    assert _mismatches(loop) == []
    assert loop.compaction_window == 200_000


async def test_without_a_pin_the_compactor_window_is_compared_unpinned() -> None:
    loop = await _run({}, 1_000_000)
    [found] = _mismatches(loop)
    assert found.data["pinned"] is False and found.data["expected"] == 1_000_000
    assert loop.compaction_window == 200_000


async def test_only_the_first_report_of_a_process_is_checked() -> None:
    loop = DelegatedLoop(store=MemoryStore(), model="m", claude_cmd=["true"],
                         compaction_window=300_000)
    await loop._ensure_session()
    sid = loop.session_id
    loop._window_pinned, loop._window_check_due = 300_000, True
    # A report with no window leaves the check for the next one.
    await loop._check_effective_window(sid, "t", {"totalTokens": 5})
    assert loop._window_check_due is True
    await loop._check_effective_window(sid, "t", {"maxTokens": 300_000})
    assert loop._window_check_due is False
    await loop._check_effective_window(sid, "t", {"maxTokens": 100_000})
    assert _mismatches(loop) == []
    # The same mismatch on a respawned CLI is not reported twice.
    for _ in range(2):
        loop._window_check_due = True
        await loop._check_effective_window(sid, "t", {"maxTokens": 350_000,
                                                      "autocompactSource": "env"})
    [found] = _mismatches(loop)
    assert found.data["source"] == "env"
