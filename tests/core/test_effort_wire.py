"""Picker override -> AgentOptions -> real adapter/loop -> captured HTTP or CLI.

No live auth, HTTP or claude process: only credentials/transport and unrelated
workspace discovery are replaced. The request producer and serializer are real.
"""
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from app.core.model_effort import EFFORT_LEVELS, effort_capabilities
from app.core.provider import harness_adapter as ha
from app.core.provider.fluid_credentials import ProviderCredentials
from app.core.query_options import build_options_from_profile
from fluid.providers.base import Message
from fluid.store.base import MemoryStore
from fluid.tools.registry import ToolRegistry


def provider(kind, model, levels=None, default=None):
    entry = {"id": model, "label": model}
    if levels is not None:
        entry["reasoning_capabilities"] = {"version": 1, "effort_levels": levels,
                                          "default_effort": default}
    return {"id": "fixture", "enabled": True, "base_url": "http://fixture.invalid",
            "provider_type": kind, "models_json": [entry],
            "auth_mode": "chatgpt" if kind == "chatgpt" else "api_key",
            "api_dialect": "openai" if kind == "custom" else None}


# Explicit catalog fixtures stand in for the same metadata the picker consumes.
# Subscription/gateway capabilities are never inferred from the model name.
ROUTES = [
    ("anthropic", "claude-opus-4-7", None, None),
    ("delegated", "claude-opus-4-7", None, None),
    ("openai", "gpt-5.5", None, None),
    ("chatgpt", "gpt-6.1-sol", ["low", "medium", "high", "xhigh", "max", "ultra"], "low"),
    ("chatgpt", "catalog-future", list(EFFORT_LEVELS), "medium"),
    ("xai", "grok-4.6", None, None),
    ("openrouter", "openai/gpt-5.6-sol", list(EFFORT_LEVELS[:-1]), "medium"),
    # App local servers are custom OpenAI-dialect providers, not a separate UI type.
    ("custom", "local-model", [], None),
    ("custom", "local-reasoner", ["none", "low", "medium", "high"], "medium"),
]
CASES = []
for kind, model, levels, default in ROUTES:
    row = None if kind in ("anthropic", "delegated") else provider(kind, model, levels, default)
    route = "delegated" if kind == "delegated" else ha._provider_row_kind(kind, "api_key", (row or {}).get("api_dialect"))
    for effort in (effort_capabilities(model, row, route=route)["effort_levels"] or []) + [None]:
        CASES.append(pytest.param(kind, model, row, effort, effort, id=f"{kind}-{model}-{effort}"))
CASES.append(pytest.param("chatgpt", "gpt-6.1-sol",
    provider("chatgpt", "gpt-6.1-sol", ["low", "high"], "low"),
    "app-initial", "high", id="benchmarked-initial-picker-to-wire"))
# Remembered or inherited invalid picks, effortless and unknown models.
for kind in ("anthropic", "delegated"):
    CASES.extend([
        pytest.param(kind, "claude-opus-4-6", None, "xhigh", "high", id=f"{kind}-clamp"),
        pytest.param(kind, "claude-haiku-4-5", None, "high", None, id=f"{kind}-reject"),
        pytest.param(kind, "claude-future", None, "high", "high", id=f"{kind}-unknown"),
    ])
row = provider("anthropic", "claude-opus-4-6", ["low", "medium", "high", "max"], "high")
CASES.append(pytest.param("anthropic", "claude-opus-4-6", row, "xhigh", "high", id="api-bound-metadata-clamp"))
for pin in ("claude-opus-4-6[1m]", "claude-sonnet-4-6[1m]"):
    CASES.append(pytest.param("anthropic", pin, provider("anthropic", pin, ["low", "high"], "low"),
                              "high", "high", id=f"api-bound-context-marker-{pin}"))
for model, pick, expected in (("claude-opus-4-6", "xhigh", "high"),
                              ("claude-haiku-4-5", "high", None)):
    row = provider("anthropic", model)
    row["auth_mode"] = "subscription"
    CASES.append(pytest.param("delegated", model, row, pick, expected, id=f"cli-bound-{model}"))
for kind in ("openai", "chatgpt", "xai", "openrouter", "custom"):
    CASES.extend([
        pytest.param(kind, "unknown-model", provider(kind, "unknown-model"), "high", "high", id=f"{kind}-unknown"),
        pytest.param(kind, "unknown-model", provider(kind, "unknown-model"), None, None, id=f"{kind}-unknown-default"),
        pytest.param(kind, "no-effort", provider(kind, "no-effort", []), "high", None, id=f"{kind}-reject"),
    ])


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,model,row,pick,expected", CASES)
@pytest.mark.parametrize("source", ["session", "override"])
async def test_picker_to_wire(mock_db, monkeypatch, tmp_path, record_property,
                              kind, model, row, pick, expected, source,
                              thinking_mode="default", expected_thinking=None, local_extra=None):
    from app.core.config import settings
    captured = []

    def capture(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              text="data: [DONE]\n\n")

    http = httpx.AsyncClient(transport=httpx.MockTransport(capture))
    monkeypatch.setattr(ha, "_lookup_provider_row", lambda _: row)
    monkeypatch.setattr(ha, "_lookup_provider_row_shape", lambda _: (
        row["provider_type"], row["auth_mode"], row.get("api_dialect")))
    monkeypatch.setattr(ha, "resolve_provider_credentials", AsyncMock(return_value=ProviderCredentials(
        api_key="fixture-only", base_url="http://fixture.invalid", use_codex_backend=kind == "chatgpt")))
    monkeypatch.setattr(ha, "_usage_capture_http_client", lambda *a: http)
    monkeypatch.setattr(ha, "_session_provider_http", lambda *a: http)
    monkeypatch.setattr(ha.HarnessAdapter, "_build_store", lambda *a: MemoryStore())
    monkeypatch.setattr(ha.HarnessAdapter, "_build_registry", AsyncMock(return_value=ToolRegistry()))
    monkeypatch.setattr(ha.HarnessAdapter, "_build_workspace_card", lambda *a: None)
    monkeypatch.setattr(ha.HarnessAdapter, "_compile_prompt_sections", lambda *a, **kw: None)
    monkeypatch.setattr(ha.HarnessAdapter, "_resolve_cli_session_id", AsyncMock(return_value=None))
    monkeypatch.setattr(ha, "_worker_identity_text", lambda: "")
    monkeypatch.setattr(ha, "_delegated_append_text", lambda *a, **kw: "")
    monkeypatch.setattr(ha, "_resolve_claude_cmd", lambda: ["claude"])
    monkeypatch.setattr(settings, "fluid_code_mode", False)
    monkeypatch.setattr(settings, "fluid_continual", False)
    # The UI's effort override enters the exact config builder used by profiles.
    if pick == "app-initial":
        pick = effort_capabilities(model, row)["initial_effort"]
    from app.db import database, model_providers
    monkeypatch.setattr(model_providers, "get_model_provider", lambda *a, **kw: row)
    monkeypatch.setattr(database, "get_model_provider", lambda *a, **kw: row)
    bound_model = f"prov:fixture:{model}" if row else model
    database.create_profile("wire-profile", "Wire", None, {"model": bound_model})
    database.create_session(session_id="wire", profile_id="wire-profile", model=bound_model,
                            effort=pick if source == "session" else None)
    opts, _ = build_options_from_profile(
        {"id": "wire-profile", "name": "Wire",
         "config": {"model": bound_model, "thinking_mode": thinking_mode}},
        session_id="wire", overrides={"cwd": str(tmp_path),
            **({"effort": pick} if source == "override" and pick else {})},
    )
    if row:
        opts.aih_provider_id = "fixture"
        opts.aih_provider_type = row["provider_type"]
        opts.aih_provider_auth_mode = row["auth_mode"]
        opts.aih_provider_dialect = row.get("api_dialect")
    elif kind == "anthropic":
        # Native API route, as opposed to the no-row subscription default.
        opts.aih_provider_id = "fixture"
        opts.aih_provider_type = "anthropic"
        monkeypatch.setattr(ha, "_lookup_provider_row", lambda _: provider("anthropic", model))
    local_extra = local_extra or {}
    if kind == "custom" and model == "local-reasoner":
        row["models_json"][0]["local_settings"] = {
            "server": "llama_cpp", "reasoning": True,
            "sampling": {"temperature": 0.6, "top_p": 0.95, "top_k": 20,
                         "min_p": 0.0, "repeat_penalty": 1.1},
        }
        local_extra = {"temperature": 0.6, "top_p": 0.95, "top_k": 20,
                       "min_p": 0.0, "repeat_penalty": 1.1,
                       "chat_template_kwargs": {"enable_thinking": True}}
    adapter = ha.HarnessAdapter()
    try:
        await adapter.connect_with_sdk_options(opts)
        loop = adapter._engine
        if kind == "delegated":
            assert loop.effort == expected
            argv = loop._build_argv()
            assert (argv[argv.index("--effort") + 1] if "--effort" in argv else None) == expected
            assert loop._setting_request("apply_flag_settings")["settings"]["effortLevel"] == expected
            record_property("wire", {"route": kind, "model": model, "pick": pick,
                                     "source": source, "thinking_mode": thinking_mode, "parameters": {"--effort": expected}})
        else:
            # Keep the real client's stream; inject only its HTTP transport.
            loop.provider._http = http
            session = await loop._ensure_session()
            await loop._resolve_session_thinking(session.session_id)
            request = loop._build_request([Message(role="user", content="hello")])
            async for _ in loop.provider.stream(request):
                pass
            assert len(captured) == 1
            body = captured[0]
            record_property("wire", {"route": kind, "model": model, "pick": pick,
                "source": source, "thinking_mode": thinking_mode, "parameters": {k: v for k, v in body.items()
                               if k in ("model", "reasoning", "output_config", "thinking",
                                        "temperature", "top_p", "top_k", "min_p",
                                        "repeat_penalty", "repetition_penalty", "chat_template_kwargs",
                                        "max_tokens", "max_output_tokens", "service_tier")}})
            for key, value in local_extra.items():
                assert body.get(key) == value
            if local_extra:
                await adapter.set_model("local-other")
                async for _ in loop.provider.stream(loop._build_request([])):
                    pass
                assert all(key not in captured[-1] for key in local_extra)
                await adapter.set_model(model)
                async for _ in loop.provider.stream(loop._build_request([])):
                    pass
                assert all(captured[-1].get(key) == value for key, value in local_extra.items())
            assert body["model"] == (model.removesuffix("[1m]") if kind == "anthropic" else model)
            # Strip only the app context marker; never upgrade a concrete pin.
            if kind == "anthropic":
                # The pooled-client hot-swap path must preserve the same pin.
                await adapter.set_model("claude-opus-4-6")
                assert loop._build_request([]).model == "claude-opus-4-6"
                assert body.get("thinking") == expected_thinking
            key = "output_config" if kind == "anthropic" else "reasoning"
            if expected is None:
                assert key not in body
            else:
                assert body[key]["effort"] == expected
                if kind in ("openai", "chatgpt", "xai"):
                    assert body[key] == {"effort": expected, "summary": "auto"}
    finally:
        await adapter.disconnect()
        await http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("model,pick,mode,thinking", [
    ("claude-opus-4-7", "xhigh", "think", {"type": "adaptive"}),
    ("claude-opus-4-6", "xhigh", "adaptive", {"type": "adaptive"}),
    ("claude-opus-4-5-20251101", "max", "disabled", {"type": "disabled"}),
    ("claude-fable-5-1", "max", "disabled", None),
    ("claude-opus-5", "xhigh", "disabled", None),
])
async def test_thinking_and_effort_to_wire(mock_db, monkeypatch, tmp_path, record_property,
                                         model, pick, mode, thinking):
    from fluid.providers.anthropic.thinking import clamp_effort
    await test_picker_to_wire(mock_db, monkeypatch, tmp_path, record_property,
        "anthropic", model, None, pick, clamp_effort(model, pick), "session",
        thinking_mode=mode, expected_thinking=thinking)


@pytest.mark.asyncio
@pytest.mark.parametrize("server,penalty,top_k", [
    ("llama_cpp", "repeat_penalty", 0), ("vllm", "repetition_penalty", -1),
])
async def test_api_local_settings_to_wire(authenticated_client, mock_db, monkeypatch,
                                         tmp_path, record_property, server, penalty, top_k):
    from app.db import model_providers

    response = authenticated_client.post("/api/v1/model-providers", json={
        "name": "Local", "provider_type": "custom", "api_dialect": "openai",
        "base_url": "http://fixture.invalid", "models": [{
            "id": "local", "label": "Local", "reasoning_capabilities": {
                "version": 1, "effort_levels": [], "default_effort": None},
            "local_settings": {"server": server, "preset": "qwen3-thinking", "reasoning": False,
                               "sampling": {"repeat_penalty": 1.1, "top_k": 0}},
        }],
    })
    assert response.status_code == 200, response.text
    # Read exactly what the real API persisted; do not seed the consumer fixture.
    stored = model_providers.get_model_provider(response.json()["id"])
    await test_picker_to_wire(mock_db, monkeypatch, tmp_path, record_property,
        "custom", "local", stored, None, None, "session", local_extra={
            "temperature": 0.6, "top_p": 0.95, "top_k": top_k, "min_p": 0.0,
            penalty: 1.1, "chat_template_kwargs": {"enable_thinking": False}})
