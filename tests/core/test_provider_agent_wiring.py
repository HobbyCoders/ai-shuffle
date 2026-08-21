"""Wiring tests for the unified Task tool surface in ``query_options`` (P2c).

The standalone ProviderAgent MCP server is retired (rulings A6/A13): the Task
tool rides the ``builtins`` server (``mcp__builtins__Task``) on every session
— workers included, since nesting is depth-gated in ``app.core.task_dispatch``
rather than denied at registration. What remains session-scoped here:

  * workers still lose ``AskUserQuestion`` (ruling A3 — questions are not
    redirected to the parent, so a worker asking one would just stall);
  * the retired ``provider-agent`` server key must never be registered.
"""

from unittest.mock import patch

import pytest

import app.core.query_options as qo
from app.core import provider_agent as pa

_SERVER_KEY = "provider-agent"


@pytest.fixture(autouse=True)
def _clean_registry():
    pa.reset_worker_sessions()
    yield
    pa.reset_worker_sessions()


def _profile():
    return {"id": "t", "config": {"model": "opus", "tools_preset": "claude_code"}}


def _build(session_id=None):
    with patch.object(qo, "get_security_instructions", return_value=""):
        opts, _ = qo.build_options_from_profile(
            profile=_profile(), session_id=session_id
        )
    return opts


def _servers(opts):
    return opts.mcp_servers if isinstance(opts.mcp_servers, dict) else {}


def test_provider_agent_server_is_retired():
    """No build registers the old provider-agent server any more."""
    opts = _build(session_id="parent-1")
    assert _SERVER_KEY not in _servers(opts)


def test_task_tool_rides_the_builtins_server():
    """The unified Task tool reaches the model via mcp__builtins__Task."""
    opts = _build(session_id="parent-1")
    assert "builtins" in _servers(opts)
    assert "mcp__builtins__Task" in set(opts.allowed_tools or [])


def test_worker_sessions_keep_the_task_tool():
    """Depth gating happens in task_dispatch — a worker still sees the tool."""
    pa.register_worker_session("worker-1")
    opts = _build(session_id="worker-1")
    assert "mcp__builtins__Task" in set(opts.allowed_tools or [])


def test_worker_cannot_ask_user_questions():
    """Ruling A3: a worker's ``AskUserQuestion`` would stall against the
    question TTL (approvals are redirected to the parent, questions are not),
    so the tool is stripped from worker sessions at every depth."""
    pa.register_worker_session("worker-1")
    opts = _build(session_id="worker-1")
    assert qo.WORKER_DISALLOWED_TOOL in (opts.disallowed_tools or [])


def test_non_worker_session_keeps_ask_user_question():
    opts = _build(session_id="parent-1")
    assert qo.WORKER_DISALLOWED_TOOL not in (opts.disallowed_tools or [])


# ─── CE12: the tool-surface override reaches the options object ─────────────


def _build_with(overrides, session_id="worker-1"):
    with patch.object(qo, "get_security_instructions", return_value=""):
        opts, _ = qo.build_options_from_profile(
            profile=_profile(), session_id=session_id, overrides=overrides
        )
    return opts


def test_tool_allowlist_override_is_stamped_for_the_harness():
    """``dispatch_worker`` puts a file agent's ``tools:`` list on the turn's
    overrides; ``build_options_from_profile`` stamps it as
    ``aih_tool_allowlist``, which the HarnessAdapter turns into
    ``NativeLoop(allowed_tools=...)`` (ruling CE12)."""
    opts = _build_with({"tool_allowlist": ["Read", "Grep"]})
    assert opts.aih_tool_allowlist == ["Read", "Grep"]


def test_no_override_leaves_the_surface_unrestricted():
    """Absent / empty / all-blank all mean "inherit the profile's toolset"."""
    assert _build_with({}).aih_tool_allowlist is None
    assert _build_with({"tool_allowlist": []}).aih_tool_allowlist is None
    assert _build_with({"tool_allowlist": ["", "  "]}).aih_tool_allowlist is None


def test_background_completion_frames_are_stamped_for_fluid():
    frames = [{"task_id": "worker-1", "status": "completed", "summary": "done"}]
    opts = _build_with({"background_task_completions": frames})
    assert opts.aih_background_task_completions == frames


def test_the_permission_allowlist_is_not_the_tool_surface():
    """``allowed_tools`` keeps its SDK PERMISSION meaning — it is full of MCP
    names and (under a tools preset) holds no builtins at all, so it must never
    be read as the session's tool surface."""
    opts = _build_with({"tool_allowlist": ["Read"]})
    assert "mcp__builtins__Task" in set(opts.allowed_tools or [])
    assert "Read" not in set(opts.allowed_tools or [])
