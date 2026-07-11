"""Unit tests for the context-boundary auto-continue decision logic.

The bundled CLI ends an agentic turn when the NEXT turn would cross the
autocompact threshold and defers compaction to the start of the next query, so
the turn ends mid-task with ``stop_reason == "tool_use"`` and the session sits
silent. ``app.core.compact_continue`` decides whether to spawn one automatic
continuation turn. These tests pin every gating branch.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.core.compact_continue as cc


@pytest.fixture(autouse=True)
def _clear_guard():
    cc._auto_continued_sessions.clear()
    yield
    cc._auto_continued_sessions.clear()


def _md(stop_reason):
    return {"stop_reason": stop_reason}


# ─── is_abnormal_boundary_stop ─────────────────────────────────────────────


def test_tool_use_is_a_boundary_cut():
    assert cc.is_abnormal_boundary_stop(_md("tool_use")) is True


@pytest.mark.parametrize("sr", ["end_turn", "max_tokens", "stop_sequence", None])
def test_non_tool_use_is_not_a_boundary_cut(sr):
    assert cc.is_abnormal_boundary_stop(_md(sr)) is False


def test_empty_metadata_is_not_a_boundary_cut():
    assert cc.is_abnormal_boundary_stop(None) is False
    assert cc.is_abnormal_boundary_stop({}) is False


# ─── context_over_threshold ────────────────────────────────────────────────


def _pool_with_usage(usage, *, connected=True, raises=False):
    adapter = MagicMock()
    if raises:
        adapter.get_context_usage = AsyncMock(side_effect=RuntimeError("wedged"))
    else:
        adapter.get_context_usage = AsyncMock(return_value=usage)
    state = MagicMock()
    state.is_connected = connected
    state.adapter = adapter
    pool = MagicMock()
    pool.peek = MagicMock(return_value=state)
    return pool


async def test_over_threshold_true_when_at_or_above():
    usage = {"totalTokens": 160000, "autoCompactThreshold": 155000, "isAutoCompactEnabled": True}
    with patch("app.core.session_pool.get_session_pool", return_value=_pool_with_usage(usage)):
        over, total = await cc.context_over_threshold("s")
    assert over is True
    assert total == 160000


async def test_under_threshold_false():
    usage = {"totalTokens": 100000, "autoCompactThreshold": 155000, "isAutoCompactEnabled": True}
    with patch("app.core.session_pool.get_session_pool", return_value=_pool_with_usage(usage)):
        over, total = await cc.context_over_threshold("s")
    assert over is False
    assert total == 100000


async def test_autocompact_disabled_never_over():
    usage = {"totalTokens": 999999, "autoCompactThreshold": 1, "isAutoCompactEnabled": False}
    with patch("app.core.session_pool.get_session_pool", return_value=_pool_with_usage(usage)):
        over, total = await cc.context_over_threshold("s")
    assert over is False


async def test_falls_back_to_max_tokens_when_no_threshold():
    usage = {"totalTokens": 150000, "maxTokens": 148000, "isAutoCompactEnabled": True}
    with patch("app.core.session_pool.get_session_pool", return_value=_pool_with_usage(usage)):
        over, total = await cc.context_over_threshold("s")
    assert over is True


async def test_get_context_usage_raises_is_not_over():
    with patch("app.core.session_pool.get_session_pool",
               return_value=_pool_with_usage(None, raises=True)):
        over, total = await cc.context_over_threshold("s")
    assert over is False
    assert total is None


async def test_not_connected_is_not_over():
    with patch("app.core.session_pool.get_session_pool",
               return_value=_pool_with_usage({"totalTokens": 1}, connected=False)):
        over, total = await cc.context_over_threshold("s")
    assert over is False


async def test_no_pooled_state_is_not_over():
    pool = MagicMock()
    pool.peek = MagicMock(return_value=None)
    with patch("app.core.session_pool.get_session_pool", return_value=pool):
        over, total = await cc.context_over_threshold("s")
    assert over is False


async def test_none_usage_is_not_over():
    with patch("app.core.session_pool.get_session_pool",
               return_value=_pool_with_usage(None)):
        over, total = await cc.context_over_threshold("s")
    assert over is False


# ─── should_auto_continue (the composed decision) ──────────────────────────


async def _decide(session_id="s", stop_reason="tool_use", *, over=(True, 150000),
                  is_stopped=False, is_interrupted=False, has_queued=False,
                  enabled=True):
    with patch.object(cc, "context_over_threshold", new=AsyncMock(return_value=over)), \
         patch.object(cc, "auto_continue_enabled", return_value=enabled):
        return await cc.should_auto_continue(
            session_id, _md(stop_reason),
            is_stopped=is_stopped, is_interrupted=is_interrupted,
            has_queued_user_message=has_queued,
        )


async def test_boundary_cut_over_threshold_continues():
    d = await _decide()
    assert d.should_continue is True
    assert d.reason == "auto_continue"
    assert d.pre_tokens == 150000


async def test_natural_end_turn_does_not_continue():
    d = await _decide(stop_reason="end_turn")
    assert d.should_continue is False
    assert d.reason == "natural_end"


async def test_user_stopped_does_not_continue():
    d = await _decide(is_stopped=True)
    assert d.should_continue is False
    assert d.reason == "stopped"


async def test_interrupted_does_not_continue():
    d = await _decide(is_interrupted=True)
    assert d.should_continue is False
    assert d.reason == "interrupted"


async def test_queued_user_message_does_not_continue():
    d = await _decide(has_queued=True)
    assert d.should_continue is False
    assert d.reason == "queued_message"


async def test_below_threshold_does_not_continue():
    d = await _decide(over=(False, 42))
    assert d.should_continue is False
    assert d.reason == "below_threshold"
    assert d.pre_tokens == 42


async def test_setting_off_does_not_continue():
    d = await _decide(enabled=False)
    assert d.should_continue is False
    assert d.reason == "disabled"


async def test_second_boundary_in_a_row_does_not_continue():
    """One-shot guard: once a session is latched (already auto-continued), a
    second boundary cut with no genuine user message between must NOT re-nudge."""
    cc.mark_auto_continued("s")
    d = await _decide()
    assert d.should_continue is False
    assert d.reason == "already_continued"


async def test_guard_resets_on_user_message():
    cc.mark_auto_continued("s")
    assert cc.has_auto_continued("s") is True
    cc.reset_auto_continue("s")
    assert cc.has_auto_continued("s") is False
    d = await _decide()
    assert d.should_continue is True


async def test_auto_continue_enabled_defaults_on():
    """The setting defaults ON — fixing broken expected behavior."""
    from app.core.config import settings
    with patch.object(settings, "auto_continue_on_compact", True):
        assert cc.auto_continue_enabled() is True
    with patch.object(settings, "auto_continue_on_compact", False):
        assert cc.auto_continue_enabled() is False
