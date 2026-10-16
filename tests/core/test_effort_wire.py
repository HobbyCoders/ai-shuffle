"""Picker override -> AgentOptions -> real adapter/loop -> captured HTTP or CLI.

No live auth, HTTP or claude process: only credentials/transport and unrelated
workspace discovery are replaced. The request producer and serializer are real.
"""
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from app.core.agent_types import AgentOptions
from app.core.model_effort import EFFORT_LEVELS, effort_capabilities
from app.core.provider import harness_adapter as ha
from app.core.provider.fluid_credentials import ProviderCredentials
from app.core.query_options import _build_thinking_config
from fluid.providers.base import Message
from fluid.store.base import MemoryStore
from fluid.tools.registry import ToolRegistry


def provider(kind, model, levels=None, default=None):
    entry = {"id": model}
    if levels is not None:
        entry["reasoning_capabilities"] = {"version": 1, "effort_levels": levels,
                                          "default_effort": default}
    return {"id": "fixture", "provider_type": kind, "models_json": [entry],
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
]
CASES = []
for kind, model, levels, default in ROUTES:
    row = None if kind in ("anthropic", "delegated") else provider(kind, model, levels, default)
    route = "delegated" if kind == "delegated" else ha._provider_row_kind(kind, "api_key")
    for effort in (effort_capabilities(model, row, route=route)["effort_levels"] or []) + [None]:
        CASES.append(pytest.param(kind, model, row, effort, effort, id=f"{kind}-{effort}"))
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
async def test_picker_to_wire(monkeypatch, tmp_path, kind, model, row, pick, expected):
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
    config = _build_thinking_config({}, model, {"effort": pick} if pick else {})
    opts = AgentOptions(model=model, cwd=str(tmp_path), **config)
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
    adapter = ha.HarnessAdapter()
    try:
        await adapter.connect_with_sdk_options(opts)
        loop = adapter._engine
        if kind == "delegated":
            assert loop.effort == expected
            argv = loop._build_argv()
            assert (argv[argv.index("--effort") + 1] if "--effort" in argv else None) == expected
            assert loop._setting_request("apply_flag_settings")["settings"]["effortLevel"] == expected
        else:
            # Keep the real client's stream; inject only its HTTP transport.
            loop.provider._http = http
            request = loop._build_request([Message(role="user", content="hello")])
            async for _ in loop.provider.stream(request):
                pass
            assert len(captured) == 1
            body = captured[0]
            assert body["model"] == model  # never silently upgrade a catalog pin
            if kind == "anthropic":
                # The pooled-client hot-swap path must preserve the same pin.
                await adapter.set_model("claude-opus-4-6")
                assert loop._build_request([]).model == "claude-opus-4-6"
                assert "thinking" not in body  # default thinking remains independent
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
