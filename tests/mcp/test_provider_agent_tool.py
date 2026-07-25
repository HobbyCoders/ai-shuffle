"""Tests for the ``ProviderAgent`` in-process SDK MCP tool (P3-A).

The tool must NEVER hang the parent and NEVER raise into the SDK: every failure
mode returns a failed tool result naming the reason. Worker dispatch itself is
mocked — nothing here spawns a session, a CLI or a network call.
"""

import asyncio
import json

import pytest

from app.core import provider_agent as pa
from app.mcp import provider_agent_sdk as pas


def _decode(result: dict) -> dict:
    return json.loads(result["content"][0]["text"])


def _provider(
    provider_id="prov-openai",
    name="My OpenAI",
    provider_type="openai",
    enabled=True,
    model_ids=("gpt-5.5", "gpt-5.5-mini"),
    default_model=None,
):
    return {
        "id": provider_id,
        "name": name,
        "provider_type": provider_type,
        "enabled": enabled,
        "default_model": default_model,
        "models_json": [{"id": m, "label": m} for m in model_ids],
    }


@pytest.fixture(autouse=True)
def _flag_on(monkeypatch):
    """The feature ships OFF; these tests exercise the ON path explicitly."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "provider_agent_enabled", True)
    pa.reset_worker_sessions()
    yield
    pa.reset_worker_sessions()


@pytest.fixture
def providers(monkeypatch):
    rows = [_provider()]
    from app.db import database as db

    monkeypatch.setattr(db, "get_enabled_model_providers", lambda *a, **k: list(rows))
    return rows


@pytest.fixture
def dispatched(monkeypatch):
    """Record dispatch_worker calls and return a canned success."""
    calls = []

    async def _fake(**kwargs):
        calls.append(kwargs)
        return pa.WorkerOutcome(
            ok=True, session_id="worker-1", report="all done", truncated=False
        )

    monkeypatch.setattr(pa, "dispatch_worker", _fake)
    return calls


def _call(args, session_id="parent-1", profile_id="default"):
    tool = pas.build_provider_agent_tool(session_id, profile_id)
    assert tool is not None
    return asyncio.run(tool.handler(args))


# ---------------------------------------------------------------------------
# Shape / registration
# ---------------------------------------------------------------------------

def test_tool_name_and_schema():
    tool = pas.build_provider_agent_tool("parent-1", "default")

    assert tool.name == "ProviderAgent"
    assert tool.input_schema["required"] == ["instance", "prompt"]
    assert set(tool.input_schema["properties"]) == {"instance", "prompt", "model", "label"}


def test_allowed_tool_name():
    assert pas.get_provider_agent_tool_names() == ["mcp__provider-agent__ProviderAgent"]


def test_server_is_wrapped_by_the_mcp_compat_patch(monkeypatch):
    """Mandatory: without patch_sdk_server, mcp >= 1.12 mis-iterates the SDK's
    CallToolResult and the tool silently breaks."""
    seen = {}
    import app.mcp.sdk_compat as sdk_compat

    real = sdk_compat.patch_sdk_server

    def _spy(cfg, tools):
        seen["names"] = [t.name for t in tools]
        return real(cfg, tools)

    monkeypatch.setattr(sdk_compat, "patch_sdk_server", _spy)

    cfg = pas.create_provider_agent_server("parent-1", "default")

    assert cfg["name"] == "provider-agent"
    assert seen["names"] == ["ProviderAgent"]


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_success_returns_report_and_worker_session(providers, dispatched):
    body = _decode(_call({"instance": "prov-openai", "prompt": "review this", "label": "rev"}))

    assert body["success"] is True
    assert body["report"] == "all done"
    assert body["worker_session_id"] == "worker-1"
    assert body["provider"] == "prov-openai"
    assert body["model"] == "gpt-5.5"
    assert body["truncated"] is False

    assert dispatched[0]["parent_session_id"] == "parent-1"
    assert dispatched[0]["profile_id"] == "default"
    assert dispatched[0]["provider_id"] == "prov-openai"
    assert dispatched[0]["model_id"] == "gpt-5.5"
    assert dispatched[0]["prompt"] == "review this"
    assert dispatched[0]["label"] == "rev"


def test_model_defaults_to_the_providers_default(providers, dispatched):
    providers[0]["default_model"] = "gpt-5.5-mini"

    body = _decode(_call({"instance": "My OpenAI", "prompt": "go"}))

    assert body["model"] == "gpt-5.5-mini"


def test_truncated_report_says_so_and_points_at_the_worker(providers, monkeypatch):
    async def _fake(**kwargs):
        return pa.WorkerOutcome(
            ok=True, session_id="worker-9", report="tail text", truncated=True
        )

    monkeypatch.setattr(pa, "dispatch_worker", _fake)

    body = _decode(_call({"instance": "prov-openai", "prompt": "go"}))

    assert body["truncated"] is True
    assert "truncated" in body["report"].lower()
    assert "worker-9" in body["report"]
    assert body["report"].endswith("tail text")


# ---------------------------------------------------------------------------
# Failure modes — every one returns a failed result, none raises
# ---------------------------------------------------------------------------

def test_disabled_flag_refuses(providers, dispatched, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "provider_agent_enabled", False)

    body = _decode(_call({"instance": "prov-openai", "prompt": "go"}))

    assert body["success"] is False
    assert "disabled" in body["error"]
    assert dispatched == []


def test_missing_session_context_refuses(providers, dispatched):
    body = _decode(_call({"instance": "prov-openai", "prompt": "go"}, session_id=None))

    assert body["success"] is False
    assert "active session context" in body["error"]
    assert dispatched == []


def test_worker_may_not_dispatch_a_worker(providers, dispatched):
    pa.register_worker_session("worker-parent")

    body = _decode(
        _call({"instance": "prov-openai", "prompt": "go"}, session_id="worker-parent")
    )

    assert body["success"] is False
    assert "one nesting level only" in body["error"]
    assert dispatched == []


def test_blank_prompt_refuses(providers, dispatched):
    body = _decode(_call({"instance": "prov-openai", "prompt": "   "}))

    assert body["success"] is False
    assert "prompt is required" in body["error"]
    assert dispatched == []


def test_unknown_instance_fails_fast(providers, dispatched):
    body = _decode(_call({"instance": "ghost", "prompt": "go"}))

    assert body["success"] is False
    assert "ghost" in body["error"]
    assert body["candidates"] == ["prov-openai"]
    assert dispatched == []


def test_ambiguous_instance_lists_candidates(providers, dispatched):
    providers.append(_provider(provider_id="prov-2", name="My OpenAI"))

    body = _decode(_call({"instance": "My OpenAI", "prompt": "go"}))

    assert body["success"] is False
    assert sorted(body["candidates"]) == ["prov-2", "prov-openai"]
    assert dispatched == []


def test_provider_with_no_curated_models_fails_fast(providers, dispatched):
    providers[0]["models_json"] = []

    body = _decode(_call({"instance": "prov-openai", "prompt": "go"}))

    assert body["success"] is False
    assert "no curated models" in body["error"]
    assert dispatched == []


def test_non_servable_provider_fails_fast(providers, dispatched):
    providers[0]["provider_type"] = "openrouter"

    body = _decode(_call({"instance": "prov-openai", "prompt": "go"}))

    assert body["success"] is False
    assert body["code"] == pa.PROVIDER_NOT_SERVABLE
    assert dispatched == []


def test_uncurated_model_fails_fast(providers, dispatched):
    body = _decode(
        _call({"instance": "prov-openai", "prompt": "go", "model": "gpt-nope"})
    )

    assert body["success"] is False
    assert body["code"] == pa.MODEL_NOT_CURATED
    assert body["curated_models"] == ["gpt-5.5", "gpt-5.5-mini"]
    assert dispatched == []


def test_worker_failure_surfaces_with_the_worker_session(providers, monkeypatch):
    async def _fake(**kwargs):
        return pa.WorkerOutcome(
            ok=False, session_id="worker-dead", error="the worker died mid-task"
        )

    monkeypatch.setattr(pa, "dispatch_worker", _fake)

    body = _decode(_call({"instance": "prov-openai", "prompt": "go"}))

    assert body["success"] is False
    assert body["error"] == "the worker died mid-task"
    assert body["worker_session_id"] == "worker-dead"
