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
