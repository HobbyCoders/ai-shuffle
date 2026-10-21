"""The real ai-tools server exposes one guarded tool per generation kind."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.generation_resolver import RunContext
from app.mcp import ai_tools_sdk as sdk
from app.services.generation_sources import Sources
from tests.core.test_generation_resolver import candidate, FakeBackend


def payload(response):
    return json.loads(response["content"][0]["text"])


async def test_sdk_server_names_are_unique_and_have_unified_parameters(monkeypatch):
    captured = []
    monkeypatch.setattr(sdk, "patch_sdk_server", lambda server, tools: server)
    monkeypatch.setattr(sdk, "create_tool_server", lambda **kw: captured.extend(kw["tools"]) or kw)
    server = sdk.create_ai_tools_server({"image_generation": True, "video_generation": True,
        "audio_generation": True, "text_to_3d": True}, "unused-tools-dir", generation_context=RunContext(unattended=False, owner=True))
    names = [t.name for t in captured]
    assert len(names) == len(set(names))
    assert set(sdk.UNIFIED_TOOL_KINDS) <= set(names)
    for t in captured:
        if t.name in sdk.UNIFIED_TOOL_KINDS:
            assert {"source", "model", "options", "confirmed", "confirmation_token"} <= t.input_schema["properties"].keys()
            assert "configured model" not in t.description
    assert "searchModels" in names


async def test_catalog_only_permission_does_not_enable_generation(monkeypatch):
    from app.core.tool_catalog import ai_tools_config_from_names
    captured = []
    monkeypatch.setattr(sdk, "patch_sdk_server", lambda server, tools: server)
    monkeypatch.setattr(sdk, "create_tool_server", lambda **kw: captured.extend(kw["tools"]) or kw)
    sdk.create_ai_tools_server(ai_tools_config_from_names(["mcp__ai-tools__searchModels"]), "unused-tools-dir")
    assert [t.name for t in captured] == ["searchModels"]


async def test_default_context_cannot_be_made_interactive_by_tool_arguments():
    tools = {t.name: t for t in sdk._create_unified_tools(None)}
    output = payload(await tools["generateImage"].handler({"prompt": "tree", "source": "fal", "unattended": False}))
    assert output["status"] == "skipped" and output["reason"] == "no_stored_choice"


async def test_sdk_legacy_option_alias_is_validated_and_not_silently_lost(monkeypatch):
    from app.services import generation_sources
    backend = FakeBackend([candidate(kind="video")])
    monkeypatch.setattr(generation_sources, "Sources", lambda *args, **kw: backend)
    tools = {t.name: t for t in sdk._create_unified_tools(None, RunContext(unattended=False))}
    output = payload(await tools["generateVideo"].handler({"prompt": "tree", "source": "fal", "duration": 4}))
    assert output["status"] == "completed"
    assert backend.calls[0][2]["duration"] == 4
    output = payload(await tools["generateVideo"].handler({"prompt": "tree", "source": "fal", "duration": 4, "options": {"duration": 8}}))
    assert output["status"] == "refused"


async def test_unified_metadata_does_not_expand_first_request(monkeypatch, tmp_path, record_property):
    from tests.core.test_native_first_request_budget import _first_request, _priced_before_the_turn, FIRST_REQUEST_BUDGET
    modern, legacy = tmp_path / "modern", tmp_path / "legacy"
    modern.mkdir()
    legacy.mkdir()
    with monkeypatch.context() as scoped:
        _, current, _ = await _first_request(scoped, modern, code_mode=False)
    with monkeypatch.context() as scoped:
        # Compare the same harness with the prior ai-tools builders. No native
        # alias is restored, and no paid provider is used by either request.
        scoped.setattr(sdk, "UNIFIED_TOOL_NAMES", set())
        scoped.setattr(sdk, "_create_unified_tools", lambda *args, **kw: [])
        _, previous, _ = await _first_request(scoped, legacy, code_mode=False)
    tokens = _priced_before_the_turn(current, "hello")
    record_property("unified_json_first_request_tokens", tokens)
    assert tokens <= FIRST_REQUEST_BUDGET
    assert tokens <= _priced_before_the_turn(previous, "hello")
