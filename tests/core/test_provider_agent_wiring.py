"""Wiring tests for the ProviderAgent MCP server in ``query_options`` (P3-A).

Three gates decide whether the tool ever reaches the model:

  1. ``settings.provider_agent_enabled`` — OFF by default; the safety chunk that
     permits turning it on has not landed, so a default build must expose
     NOTHING (no allowed-tool entry, no registered server).
  2. a real ``session_id`` — the server needs a parent to bind workers to.
  3. the parent must not itself be a ProviderAgent worker — one nesting level
     only (ruling P14).

The server factory is stubbed to a sentinel so these tests exercise the wiring
branch without building a real SDK server.
"""

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

import app.core.query_options as qo
from app.core import provider_agent as pa
from app.mcp.provider_agent_sdk import get_provider_agent_tool_names

_PA_TOOLS = set(get_provider_agent_tool_names())
_SERVER_KEY = "provider-agent"


@pytest.fixture(autouse=True)
def _clean_registry():
    pa.reset_worker_sessions()
    yield
    pa.reset_worker_sessions()


def _profile():
    return {"id": "t", "config": {"model": "opus", "tools_preset": "claude_code"}}


@contextmanager
def _stubbed_server():
    with patch(
        "app.mcp.provider_agent_sdk.create_provider_agent_server",
        return_value=MagicMock(name="provider-agent-server"),
    ):
        yield


def _build(session_id=None):
    with patch.object(qo, "get_security_instructions", return_value=""):
        opts, _ = qo.build_options_from_profile(
            profile=_profile(), session_id=session_id
        )
    return opts


def _servers(opts):
    return opts.mcp_servers if isinstance(opts.mcp_servers, dict) else {}


def test_flag_off_by_default_exposes_nothing():
    """The default build must not allow-list the tool or register the server."""
    from app.core.config import settings

    assert settings.provider_agent_enabled is False

    with _stubbed_server():
        opts = _build(session_id="parent-1")

    assert not _PA_TOOLS & set(opts.allowed_tools or [])
    assert _SERVER_KEY not in _servers(opts)


def test_flag_on_allowlists_and_registers(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "provider_agent_enabled", True)

    with _stubbed_server():
        opts = _build(session_id="parent-1")

    assert _PA_TOOLS.issubset(set(opts.allowed_tools or []))
    assert _SERVER_KEY in _servers(opts)


def test_worker_session_is_skipped(monkeypatch):
    """A worker never gets the tool it was itself dispatched by."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "provider_agent_enabled", True)
    pa.register_worker_session("worker-1")

    with _stubbed_server():
        opts = _build(session_id="worker-1")

    assert not _PA_TOOLS & set(opts.allowed_tools or [])
    assert _SERVER_KEY not in _servers(opts)


def test_sessionless_build_is_skipped(monkeypatch):
    """Session-less builds (tests / project-only spawns) have no parent to bind
    workers to, so the tool is not offered."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "provider_agent_enabled", True)

    with _stubbed_server():
        opts = _build(session_id=None)

    assert not _PA_TOOLS & set(opts.allowed_tools or [])
    assert _SERVER_KEY not in _servers(opts)


def test_server_factory_failure_does_not_break_the_build(monkeypatch):
    """A broken factory must degrade to "no ProviderAgent", never a failed spawn."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "provider_agent_enabled", True)

    with patch(
        "app.mcp.provider_agent_sdk.create_provider_agent_server",
        side_effect=RuntimeError("boom"),
    ):
        opts = _build(session_id="parent-1")

    assert _SERVER_KEY not in _servers(opts)
