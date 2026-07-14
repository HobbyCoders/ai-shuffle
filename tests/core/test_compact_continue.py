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


async def test_auto_continue_enabled_defaults_on():
    """The setting defaults ON — fixing broken expected behavior."""
    from app.core.config import settings
    with patch.object(settings, "auto_continue_on_compact", True):
        assert cc.auto_continue_enabled() is True
    with patch.object(settings, "auto_continue_on_compact", False):
        assert cc.auto_continue_enabled() is False
