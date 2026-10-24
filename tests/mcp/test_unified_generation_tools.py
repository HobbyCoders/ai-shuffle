"""The real ai-tools server exposes one guarded tool per generation kind."""
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.generation_resolver import RunContext, UNKNOWN_PRICE
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
    from tests.core.test_native_first_request_budget import (
        FIRST_REQUEST_BUDGET, _first_request, _pin_host, _priced_before_the_turn,
    )
    # The Linux host facts on every machine, as the budget was measured.
    _pin_host(monkeypatch, "linux")
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
    assert tokens <= FIRST_REQUEST_BUDGET["linux"]
    assert tokens <= _priced_before_the_turn(previous, "hello")


# ---- Step 1 is additive: main's configured-model path stays the Settings default ----

def lane_tools(monkeypatch, entries, context, lane="fal", tool_env=None, names=("generateImage", "runFalModel")):
    from app.services import generation_sources
    backend = FakeBackend(entries)
    backend.local_worker = lambda: "worker-a"
    monkeypatch.setattr(generation_sources, "Sources", lambda *args, **kw: backend)
    legacy = AsyncMock(return_value={"content": [{"type": "text", "text": '{"success": true, "lane": "settings"}'}]})
    lanes = {name: (lane, legacy) for name in names}
    tools = {t.name: t for t in sdk._create_unified_tools(None, context, lanes=lanes, tool_env=tool_env)}
    return tools, backend, legacy


INTERACTIVE = RunContext(session_id="s", unattended=False, owner=True)
UNATTENDED = RunContext(session_id="s", unattended=True, owner=True)


@pytest.mark.parametrize("source", [None, "fal", "Fal.ai"])
async def test_a_fal_lane_never_answers_for_a_generation_tool(monkeypatch, source):
    # Main's unguarded fal handler would spend with no price, cap or
    # confirmation; the generation tools always go through the guard (#379).
    tools, backend, legacy = lane_tools(monkeypatch, [candidate(), candidate(account="b")], INTERACTIVE)
    output = payload(await tools["generateImage"].handler({"prompt": "tree", **({"source": source} if source else {})}))
    assert output["status"] == "choose"
    legacy.assert_not_awaited()
    assert "Settings default" not in tools["generateImage"].description


@pytest.mark.parametrize("args", [{"prompt": "tree"}, {"prompt": "tree", "model": "fal-ai/x"}, {"prompt": "tree", "source": "FAL"}])
async def test_unattended_runs_without_a_stored_choice_skip_instead_of_the_settings_path(monkeypatch, args):
    # Automations, worker/Task sessions, voice, hidden and API-key turns.
    tools, backend, legacy = lane_tools(monkeypatch, [candidate()], UNATTENDED)
    output = payload(await tools["generateImage"].handler(args))
    assert output["status"] == "skipped" and output["reason"] == "no_stored_choice"
    legacy.assert_not_awaited()
    assert backend.calls == []


async def test_unattended_new_parameters_still_skip_without_a_stored_choice(monkeypatch):
    tools, backend, legacy = lane_tools(monkeypatch, [candidate(source="supergrok")], UNATTENDED)
    output = payload(await tools["generateImage"].handler({"prompt": "tree", "source": "supergrok"}))
    assert output["status"] == "skipped" and output["reason"] == "no_stored_choice"
    legacy.assert_not_awaited()


async def test_named_fal_model_is_guarded(monkeypatch):
    tools, backend, legacy = lane_tools(monkeypatch, [candidate(model="fal-ai/x")], INTERACTIVE)
    output = payload(await tools["generateImage"].handler({"prompt": "tree", "model": "fal-ai/x"}))
    assert output["status"] == "completed" and output["choice"]["model"] == "fal-ai/x"
    legacy.assert_not_awaited()


async def test_named_fal_model_the_guard_cannot_price_is_refused_not_run_unguarded(monkeypatch):
    tools, backend, legacy = lane_tools(monkeypatch, [replace(candidate(model="fal-ai/x"), price=dict(UNKNOWN_PRICE))], INTERACTIVE)
    output = payload(await tools["generateImage"].handler({"prompt": "tree", "model": "fal-ai/x"}))
    assert output["status"] == "refused" and output["reason"] == "price_unavailable"
    legacy.assert_not_awaited()
    assert not any(c[0] in ("reserve", "execute") for c in backend.calls)


@pytest.mark.parametrize("why", ["daily_cap", "confirmation", "chat"])
async def test_guard_refusals_never_fall_back(monkeypatch, why):
    price = {"currency": "USD", "unit": "item", "amount": "0.50", "total": "0.50", "status": "estimated"}
    entry = replace(candidate(model="fal-ai/x"), price=price) if why == "confirmation" else candidate(model="fal-ai/x")
    context = replace(INTERACTIVE, chat_mode=True) if why == "chat" else INTERACTIVE
    tools, backend, legacy = lane_tools(monkeypatch, [entry], context)
    backend.cap = why != "daily_cap"
    output = payload(await tools["generateImage"].handler({"prompt": "tree", "model": "fal-ai/x"}))
    legacy.assert_not_awaited()
    assert output["status"] in ("refused", "needs-confirmation", "completed")
    assert not any(c[0] == "execute" for c in backend.calls) or why == "chat"


async def test_chat_mode_has_no_settings_default(monkeypatch):
    tools, backend, legacy = lane_tools(monkeypatch, [candidate(), candidate(account="b")], replace(INTERACTIVE, chat_mode=True))
    assert payload(await tools["generateImage"].handler({"prompt": "tree"}))["status"] == "choose"
    legacy.assert_not_awaited()
    assert "Settings default" not in tools["generateImage"].description


@pytest.mark.parametrize("unattended", [False, True])
@pytest.mark.parametrize("unreadable", [False, True])
@pytest.mark.parametrize("empty_catalog", [False, True])
async def test_chatgpt_setting_never_bypasses_choice_or_admission(
    monkeypatch: pytest.MonkeyPatch, unattended: bool, unreadable: bool, empty_catalog: bool,
) -> None:
    env = {"IMAGE_MODEL": "chatgpt-subscription"}
    entries = [] if empty_catalog else [candidate(source="chatgpt", account="plan-a")]
    context = replace(INTERACTIVE, unattended=unattended, choices_unreadable=unreadable)
    tools, backend, legacy = lane_tools(monkeypatch, entries, context,
                                        lane="chatgpt", tool_env=env, names=("generateImage",))
    backend.cap = False
    output = payload(await tools["generateImage"].handler({"prompt": "tree"}))
    assert output["status"] == ("skipped" if unattended else "choose")
    if unattended:
        assert output["reason"] == ("choices_unreadable" if unreadable else "no_stored_choice")
    legacy.assert_not_awaited()
    assert backend.calls == []
    assert "Settings default" not in tools["generateImage"].description


@pytest.mark.parametrize("cap", [False, True])
@pytest.mark.parametrize("unattended", [False, True])
async def test_chatgpt_included_choice_checks_cap_before_execution(
    monkeypatch: pytest.MonkeyPatch, cap: bool, unattended: bool,
) -> None:
    from app.core.generation_resolver import INCLUDED_PRICE
    entry = replace(candidate(source="chatgpt", account="plan-a"), price=dict(INCLUDED_PRICE))
    context = replace(INTERACTIVE, unattended=unattended, remembered_choice=entry.choice())
    tools, backend, legacy = lane_tools(monkeypatch, [entry], context, lane="chatgpt",
                                        tool_env={"IMAGE_MODEL": "chatgpt-subscription"})
    backend.cap = cap
    output = payload(await tools["generateImage"].handler({"prompt": "tree"}))
    assert output["price"] == INCLUDED_PRICE
    assert output["status"] == ("completed" if cap else "refused")
    assert ("reserve", "plan-a") in backend.calls
    assert (("execute", "plan-a") in backend.calls) is cap
    legacy.assert_not_awaited()




async def test_chatgpt_choice_card_model_id_can_be_submitted_through_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.generation_resolver import INCLUDED_PRICE
    entry = replace(
        candidate(source="chatgpt", account="plan-a", model="default"), price=dict(INCLUDED_PRICE),
    )
    tools, backend, legacy = lane_tools(monkeypatch, [entry], INTERACTIVE, lane="chatgpt",
                                        tool_env={"IMAGE_MODEL": "chatgpt-subscription"})
    output = payload(await tools["generateImage"].handler({"prompt": "tree", **entry.choice()}))
    assert output["status"] == "completed" and output["price"] == INCLUDED_PRICE
    assert [call[0] for call in backend.calls] == ["prepare", "reserve", "execute"]
    legacy.assert_not_awaited()


async def test_server_wires_chatgpt_plan_lane_without_fal(monkeypatch):
    from app.core import fal_credential
    from app.core.provider import subscription_images as sub
    captured = []
    monkeypatch.setattr(sdk, "patch_sdk_server", lambda server, tools: server)
    monkeypatch.setattr(sdk, "create_tool_server", lambda **kw: captured.extend(kw["tools"]) or kw)
    monkeypatch.setattr(fal_credential, "is_enabled", lambda: False)
    run = AsyncMock(return_value={"content": [{"type": "text", "text": '{"success": true, "source": "chatgpt_subscription"}'}]})
    monkeypatch.setattr(sub, "run", run)
    sdk.create_ai_tools_server({"image_generation": True}, "unused-tools-dir", tool_env={"IMAGE_MODEL": "chatgpt-subscription"},
                               generation_context=UNATTENDED)
    image = next(t for t in captured if t.name == "generateImage")
    output = payload(await image.handler({"prompt": "tree"}))
    assert output["status"] == "skipped" and output["reason"] == "no_stored_choice"
    run.assert_not_awaited()


async def test_run_fal_model_keeps_non_text_models(monkeypatch):
    from app.services.fal_schema import ModelSchema, ParamField, schema_service
    schema = ModelSchema("fal-ai/i2v", [ParamField("prompt", is_primary_text=True), ParamField("image_url", type="file")], "prompt")
    monkeypatch.setattr(schema_service, "get_schema", AsyncMock(return_value=schema))
    entry = candidate(model="fal-ai/i2v", kind="video")
    tools, backend, legacy = lane_tools(monkeypatch, [entry], INTERACTIVE)
    backend.fal_key = AsyncMock(return_value="key")
    args = {"endpoint_id": "fal-ai/i2v", "input": {"prompt": "move", "image_url": "https://x/y.png"}, "output_type": "video"}
    output = payload(await tools["runFalModel"].handler(args))
    assert output["status"] == "completed"  # priced: same guard as the generation tools
    assert backend.calls[0] == ("prepare", entry.account, {"size": "square", "image_url": "https://x/y.png"})
    legacy.assert_not_awaited()


@pytest.mark.parametrize("unattended", [False, True])
@pytest.mark.parametrize("invalid", ["promptless", "missing_schema", "unknown_kind", "invalid_input"])
async def test_run_fal_model_unsupported_inputs_never_use_the_raw_handler(monkeypatch, unattended, invalid):
    from app.services.fal_schema import ModelSchema, ParamField, schema_service
    schema = ModelSchema("fal-ai/x", [ParamField("prompt", is_primary_text=True)], "prompt")
    args = {"endpoint_id": "fal-ai/x", "input": {"prompt": "p"}, "output_type": "image"}
    if invalid == "promptless":
        schema = ModelSchema("fal-ai/x", [ParamField("audio_url", type="file")], None)
        args["input"] = {"audio_url": "https://x/a.wav"}
    elif invalid == "missing_schema":
        schema = None
    elif invalid == "unknown_kind":
        args["output_type"] = "unknown"
    else:
        args["input"] = None
    monkeypatch.setattr(schema_service, "get_schema", AsyncMock(return_value=schema))
    tools, backend, legacy = lane_tools(monkeypatch, [], replace(INTERACTIVE, unattended=unattended))
    backend.fal_key = AsyncMock(return_value="key")
    output = payload(await tools["runFalModel"].handler(args))
    assert output["status"] == ("skipped" if unattended else "refused")
    assert output["reason"] == "invalid_request"
    assert "Nothing was submitted" in output["message"]
    legacy.assert_not_awaited()
    assert backend.calls == []


@pytest.mark.parametrize("unreadable", [False, True])
async def test_run_fal_model_unattended_without_a_choice_skips_and_reports(monkeypatch, unreadable):
    from app.services.fal_schema import ModelSchema, ParamField, schema_service
    schema = ModelSchema("fal-ai/x", [ParamField("prompt", is_primary_text=True)], "prompt")
    monkeypatch.setattr(schema_service, "get_schema", AsyncMock(return_value=schema))
    tools, backend, legacy = lane_tools(monkeypatch, [candidate(model="fal-ai/x")],
                                        replace(UNATTENDED, choices_unreadable=unreadable))
    backend.fal_key = AsyncMock(return_value="key")
    output = payload(await tools["runFalModel"].handler({"endpoint_id": "fal-ai/x", "input": {"prompt": "p"}, "output_type": "image"}))
    assert output["status"] == "skipped"
    assert output["reason"] == ("choices_unreadable" if unreadable else "no_stored_choice")
    assert output["message"]
    legacy.assert_not_awaited()
    assert backend.calls == []


@pytest.mark.parametrize("unattended", [False, True])
@pytest.mark.parametrize("reason", ["price_unavailable", "no_match", "unavailable", "invalid_request", "daily_cap", "needs_confirmation", None])
async def test_run_fal_model_always_uses_the_spending_guard(monkeypatch, unattended, reason):
    from app.services.fal_schema import ModelSchema, ParamField, schema_service
    schema = ModelSchema("fal-ai/x", [ParamField("prompt", is_primary_text=True)], "prompt")
    monkeypatch.setattr(schema_service, "get_schema", AsyncMock(return_value=schema))
    entry = candidate(model="fal-ai/x")
    if reason == "price_unavailable":
        entry = replace(entry, price=dict(UNKNOWN_PRICE))
    elif reason == "unavailable":
        entry = replace(entry, available=False, reason="Unavailable")
    elif reason == "needs_confirmation":
        entry = replace(entry, price={**entry.price, "amount": "1", "total": "1"})
    context = replace(INTERACTIVE, unattended=unattended, job_choice=entry.choice() if unattended else None)
    tools, backend, legacy = lane_tools(monkeypatch, [] if reason == "no_match" else [entry], context)
    backend.fal_key = AsyncMock(return_value="key")
    backend.cap = reason != "daily_cap"
    if reason == "invalid_request":
        backend.prepare = AsyncMock(side_effect=ValueError("Invalid model options"))
    output = payload(await tools["runFalModel"].handler({"endpoint_id": "fal-ai/x", "input": {"prompt": "p"}, "output_type": "image"}))
    legacy.assert_not_awaited()
    if reason is None:
        assert output["status"] == "completed"
        assert output["price"] == entry.price
        assert [c[0] for c in backend.calls] == ["prepare", "reserve", "execute"]
    else:
        assert not any(c[0] == "execute" for c in backend.calls)
        if reason == "needs_confirmation" and not unattended:
            assert output["status"] == "needs-confirmation"
            assert output["confirmation_token"]
        else:
            assert output["status"] == ("skipped" if unattended else "refused")
            assert output["reason"] == reason
            if unattended:
                assert output["message"]
        if reason != "daily_cap":
            assert not any(c[0] == "reserve" for c in backend.calls)


async def test_run_fal_model_without_a_lane_refuses_what_it_cannot_price(monkeypatch):
    from app.services import generation_sources
    from app.services.fal_schema import schema_service
    backend = FakeBackend([])
    backend.fal_key = AsyncMock(return_value=None)
    monkeypatch.setattr(generation_sources, "Sources", lambda *args, **kw: backend)
    monkeypatch.setattr(schema_service, "get_schema", AsyncMock(return_value=None))
    tools = {t.name: t for t in sdk._create_unified_tools(None, INTERACTIVE)}
    output = payload(await tools["runFalModel"].handler({"endpoint_id": "fal-ai/x", "input": {"prompt": "p"}}))
    assert output["status"] == "refused"


# ---- Re-review probes (#409), kept as regressions ----

PLAN_ENV = {"IMAGE_MODEL": "chatgpt-subscription"}


@pytest.mark.parametrize("worker", ["worker-a", "worker-b"])
@pytest.mark.parametrize("args", [{"model": "fal-ai/flux/dev"}, {"source": "fal"}, {"source": "Fal-AI", "model": "fal-ai/flux/dev"}])
async def test_plan_image_default_never_spends_on_fal(monkeypatch, worker, args):
    # Main refused any fal image while images are set to the ChatGPT plan (#1796),
    # including a fal key held by a peer and a fal choose card.
    fal = replace(candidate(model="fal-ai/flux/dev"), worker=worker)
    tools, backend, legacy = lane_tools(monkeypatch, [fal], INTERACTIVE, lane="chatgpt", tool_env=PLAN_ENV, names=("generateImage",))
    output = payload(await tools["generateImage"].handler({"prompt": "tree", **args}))
    assert output["status"] == "refused" and output["reason"] == "image_provider_restricted"
    assert not any(c[0] in ("prepare", "reserve", "execute") for c in backend.calls)
    legacy.assert_not_awaited()


async def test_plan_image_default_choose_card_never_offers_fal(monkeypatch):
    entries = [candidate(), candidate(source="supergrok", account="plan-b"), candidate(source="openai", account="api-a")]
    tools, backend, legacy = lane_tools(monkeypatch, entries, INTERACTIVE, lane="chatgpt", tool_env=PLAN_ENV, names=("generateImage",))
    output = payload(await tools["generateImage"].handler({"prompt": "tree", "worker": "worker-a"}))
    assert output["status"] == "choose"
    assert {o["source"] for o in output["options"]} == {"supergrok", "openai"}


@pytest.mark.parametrize("configured,model", [("chatgpt-subscription", "chatgpt-subscription"),
                                              ("chatgpt-subscription/gpt-image-1", "chatgpt-subscription/gpt-image-1"),
                                              ("chatgpt-subscription/gpt-image-1", "gpt-image-1")])
async def test_unresolved_legacy_plan_model_is_not_a_handler_bypass(
    monkeypatch: pytest.MonkeyPatch, configured: str, model: str,
) -> None:
    env = {"IMAGE_MODEL": configured}
    tools, backend, legacy = lane_tools(monkeypatch, [], INTERACTIVE, lane="chatgpt", tool_env=env, names=("generateImage",))
    output = payload(await tools["generateImage"].handler({"prompt": "tree", "model": model}))
    assert output["status"] == "refused" and output["reason"] == "no_match"
    legacy.assert_not_awaited()
    assert backend.calls == []


async def test_old_peer_does_not_make_a_named_request_ambiguous(monkeypatch):
    from app.core.generation_resolver import Candidate
    here = candidate(model="fal-ai/x")
    old = Candidate("unavailable", "dev-old", "dev-old", "fal-ai/x", "image", False, available=False, reason="Update that worker")
    tools, backend, legacy = lane_tools(monkeypatch, [here, old], INTERACTIVE, names=())
    output = payload(await tools["generateImage"].handler({"prompt": "tree", "model": "fal-ai/x"}))
    assert output["status"] == "completed" and output["choice"]["worker"] == "worker-a"


async def test_search_models_keeps_main_fal_discovery_without_pricing_everything(monkeypatch):
    from app.services.fal_catalog import catalog_service, CatalogModel
    from app.services import generation_sources
    from app.services.fal_schema import schema_service
    lyria = CatalogModel("fal-ai/lyria2", "Lyria 2", "text-to-audio", "Google music generation", output_type="audio")
    upscaler = CatalogModel("fal-ai/clarity-upscaler", "Clarity Upscaler", "image-to-image", "upscale images", output_type="image")
    many = [CatalogModel(f"fal-ai/img-{i}", f"Image {i}", "text-to-image", "upscale art", output_type="image") for i in range(20)]
    monkeypatch.setattr(catalog_service, "_cache", [lyria, upscaler, *many])
    monkeypatch.setattr(type(catalog_service), "is_stale", property(lambda self: False))
    monkeypatch.setattr(schema_service, "get_schema", AsyncMock(return_value=None))
    context = RunContext(session_id="s", unattended=False, owner=False, api_user_id="u")
    source = generation_sources.Sources(None, context)
    source.fal_key = AsyncMock(return_value="k")
    source.fal_price = AsyncMock()
    monkeypatch.setattr(generation_sources, "Sources", lambda *a, **kw: source)
    monkeypatch.setattr(sdk, "_get_fal_api_key", lambda: "k")
    tools = {t.name: t for t in sdk._create_unified_tools(None, context)}
    music = payload(await tools["searchModels"].handler({"query": "music"}))
    assert [m["endpoint_id"] for m in music["fal_models"]] == ["fal-ai/lyria2"]
    upscale = payload(await tools["searchModels"].handler({"query": "upscale"}))
    assert "fal-ai/clarity-upscaler" in [m["endpoint_id"] for m in upscale["fal_models"]]
    assert upscale["fal_total_matches"] == 21
    # Only the top model per kind is looked up for a quote, not all 21 matches.
    assert schema_service.get_schema.await_count <= 4 + 4


async def test_peer_catalog_is_skipped_when_pinned_here_and_parallel_otherwise(monkeypatch):
    import asyncio
    from app.network import generation, runners
    from app.services.generation_sources import Sources
    monkeypatch.setattr(runners, "self_device_id", lambda: "worker-a")
    source = Sources(None, RunContext(session_id="s", unattended=False, owner=True))
    source.local_catalog = AsyncMock(return_value=[])
    peers = AsyncMock(return_value=[])
    monkeypatch.setattr(generation, "catalog", peers)
    await source.catalog("image", model="fal-ai/x", worker="worker-a")
    peers.assert_not_awaited()
    await source.catalog("image", model="fal-ai/x", worker="worker-b")
    source.local_catalog.assert_awaited_once()  # the pinned peer request skips local discovery
    assert peers.await_args.kwargs["worker"] == "worker-b"
    # Peers are asked concurrently: two slow peers cost one delay, not two.
    monkeypatch.undo()
    monkeypatch.setattr(runners, "other_workers", lambda: [{"device_id": "p1", "online": True}, {"device_id": "p2", "online": True}])
    async def slow(worker, operation, **body):
        await asyncio.sleep(0.2)
        return {"candidates": []}
    monkeypatch.setattr(generation, "request", slow)
    started = asyncio.get_running_loop().time()
    assert await generation.catalog("image") == []
    assert asyncio.get_running_loop().time() - started < 0.35


@pytest.mark.parametrize("chat_mode,serve_peer,expected", [(False, False, True), (True, False, False), (False, True, False)])
async def test_guarded_fal_result_keeps_workspace_path_locally(monkeypatch, tmp_path, chat_mode, serve_peer, expected):
    from app.services import generation_sources
    from app.services.fal_schema import ModelSchema, ParamField, schema_service
    media = tmp_path / "out.png"
    media.write_bytes(b"\x89PNG....")
    node = SimpleNamespace(call=AsyncMock(return_value={"success": True, "image_url": "https://fal/x.png", "file_path": str(media), "mime_type": "image/png"}))
    monkeypatch.setattr(schema_service, "get_schema", AsyncMock(return_value=ModelSchema("fal-ai/x", [ParamField("prompt", is_primary_text=True)], "prompt")))
    import app.core.artifact_store as store
    monkeypatch.setattr(store, "store_bytes", lambda data, **kw: {"hash": "abc"})
    context = RunContext(session_id="s", unattended=False, owner=True, chat_mode=chat_mode)
    source = generation_sources.Sources(node, context, serve_peer=serve_peer)
    source.fal_key = AsyncMock(return_value="k")
    output = await source.execute_local(replace(candidate(model="fal-ai/x"), options={}), "tree", context)
    assert output["image_url"].startswith("/api/v1/artifacts/abc")
    assert (output.get("file_path") == str(media)) is expected
    assert ("file_path" in output["outputs"][0]) is expected


# ---- Final review probes (#409), kept as regressions ----

async def test_malformed_peer_catalog_keeps_main_fal_discovery(monkeypatch):
    from app.services.fal_catalog import catalog_service, CatalogModel
    from app.services import generation_sources
    from app.services.fal_schema import schema_service
    from app.network import generation, runners
    lyria = CatalogModel("fal-ai/lyria2", "Lyria 2", "text-to-audio", "Google music generation", output_type="audio")
    monkeypatch.setattr(catalog_service, "_cache", [lyria])
    monkeypatch.setattr(type(catalog_service), "is_stale", property(lambda self: False))
    monkeypatch.setattr(schema_service, "get_schema", AsyncMock(return_value=None))
    context = RunContext(session_id="s", unattended=False, owner=True)
    source = generation_sources.Sources(None, context)
    source.fal_key = AsyncMock(return_value="k")
    source.fal_price = AsyncMock()
    monkeypatch.setattr(generation_sources, "Sources", lambda *a, **kw: source)
    monkeypatch.setattr(sdk, "_get_fal_api_key", lambda: "k")
    monkeypatch.setattr(runners, "self_device_id", lambda: "worker-a")
    monkeypatch.setattr(runners, "other_workers", lambda: [{"device_id": "p1", "online": True}])
    monkeypatch.setattr("app.db.model_providers.list_usable_model_providers", lambda: [])
    async def bad(worker, operation, **body):
        return {"version": 2, "candidates": [{"source": "fal"}]}
    monkeypatch.setattr(generation, "request", bad)
    tools = {t.name: t for t in sdk._create_unified_tools(None, context)}
    output = payload(await tools["searchModels"].handler({"query": "music"}))
    assert [m["endpoint_id"] for m in output["fal_models"]] == ["fal-ai/lyria2"]
    listed = payload(await tools["searchModels"].handler({"output_type": "audio"}))
    assert any(o["worker"] == "p1" and not o["available"] for o in listed["options"])
    # Even if the whole priced lookup fails, main's discovery is still returned.
    from app.core.generation_resolver import Resolver
    monkeypatch.setattr(Resolver, "catalog", AsyncMock(side_effect=ValueError("Invalid generation catalog")))
    output = payload(await tools["searchModels"].handler({"query": "music"}))
    assert [m["endpoint_id"] for m in output["fal_models"]] == ["fal-ai/lyria2"]
    assert output["options"] == [] and output["priced_lookup_error"]


@pytest.mark.parametrize("serve_peer,chat_mode", [(True, False), (False, True), (True, True)])
async def test_sessionless_peer_or_chat_fal_request_returns_no_local_path(monkeypatch, tmp_path, serve_peer, chat_mode):
    from app.services import generation_sources
    from app.services.fal_schema import ModelSchema, ParamField, schema_service
    media = tmp_path / "out.png"
    media.write_bytes(b"x")
    node = SimpleNamespace(call=AsyncMock(return_value={"success": True, "file_path": str(media), "mime_type": "image/png"}))
    monkeypatch.setattr(schema_service, "get_schema", AsyncMock(return_value=ModelSchema("fal-ai/x", [ParamField("prompt", is_primary_text=True)], "prompt")))
    context = RunContext(session_id=None, unattended=False, owner=True, chat_mode=chat_mode)
    source = generation_sources.Sources(node, context, serve_peer=serve_peer)
    source.fal_key = AsyncMock(return_value="k")
    output = await source.execute_local(replace(candidate(model="fal-ai/x"), options={}), "tree", context)
    assert output["success"] is False and str(tmp_path) not in json.dumps(output)
    node.call.assert_not_awaited()  # refused before anything was submitted


# ---- Steps 2-3 (#379): remembered choices replace the implicit Settings default ----

def _legacy(*names):
    return {name: SimpleNamespace(handler=AsyncMock(name=name)) for name in names}


def test_generation_tools_never_get_a_fal_lane():
    legacy = _legacy("generateImage", "generateVideo", "runFalModel", "searchModels")
    assert set(sdk._settings_lanes(legacy, {}, True)) == {"runFalModel"}
    # A saved fal default is migrated into a remembered choice, never a lane.
    assert set(sdk._settings_lanes(legacy, {"IMAGE_MODEL": "fal-ai/flux/dev"}, True)) == {"runFalModel"}
    assert set(sdk._settings_lanes(legacy, {"IMAGE_MODEL": "fal-ai/flux/dev"}, False)) == set()
    # The ChatGPT-plan image setting is a provider rule (#1796), never migrated away.
    plan = sdk._settings_lanes(legacy, {"IMAGE_MODEL": "chatgpt-subscription"}, False)
    assert plan == {}  # Included images must also pass through admission.


async def test_unspecified_interactive_request_without_a_saved_setting_shows_the_card(monkeypatch):
    from app.services import generation_sources
    backend = FakeBackend([candidate(), candidate(account="b")])
    monkeypatch.setattr(generation_sources, "Sources", lambda *args, **kw: backend)
    lanes = sdk._settings_lanes(_legacy("generateImage", "runFalModel"), {}, True)
    tools = {t.name: t for t in sdk._create_unified_tools(None, INTERACTIVE, lanes=lanes, tool_env={})}
    output = payload(await tools["generateImage"].handler({"prompt": "tree"}))
    assert output["status"] == "choose" and "choice card" in output["message"]
    assert backend.calls == []


async def test_unattended_request_without_a_remembered_choice_skips_and_reports(monkeypatch):
    from app.services import generation_sources
    backend = FakeBackend([candidate()])
    monkeypatch.setattr(generation_sources, "Sources", lambda *args, **kw: backend)
    lanes = sdk._settings_lanes(_legacy("generateImage", "runFalModel"), {}, True)
    tools = {t.name: t for t in sdk._create_unified_tools(None, UNATTENDED, lanes=lanes, tool_env={})}
    output = payload(await tools["generateImage"].handler({"prompt": "tree"}))
    assert output["status"] == "skipped" and output["reason"] == "no_stored_choice"
    assert backend.calls == []


@pytest.mark.parametrize("unattended", [False, True])
async def test_remembered_choice_is_reused_without_asking(monkeypatch, unattended):
    from app.services import generation_sources
    a, b = candidate(), candidate(account="account-b", model="model-b")
    backend = FakeBackend([a, b])
    monkeypatch.setattr(generation_sources, "Sources", lambda *args, **kw: backend)
    context = replace(INTERACTIVE, unattended=unattended, remembered_choices={"image": {**b.choice(), "origin": "card"}})
    legacy = AsyncMock()
    tools = {t.name: t for t in sdk._create_unified_tools(None, context, lanes={"generateImage": ("fal", legacy)},
                                                          tool_env={"IMAGE_MODEL": "model-a"})}
    output = payload(await tools["generateImage"].handler({"prompt": "tree"}))
    assert output["status"] == "completed" and output["remembered"] is True
    assert output["choice"]["account"] == "account-b"
    legacy.assert_not_awaited()


@pytest.mark.parametrize("origin", ["migrated", "card"])
@pytest.mark.parametrize("unattended", [True, False])
async def test_unpriced_remembered_choice_never_runs_unguarded(monkeypatch, origin, unattended):
    # A migrated Settings default gets no unguarded fal fallback: the saved
    # model has no known price, so main's handler would spend an unknown amount.
    from app.services import generation_sources
    entry = replace(candidate(model="fal-ai/x"), price=dict(UNKNOWN_PRICE))
    backend = FakeBackend([entry, candidate(account="account-b", model="model-b")])
    monkeypatch.setattr(generation_sources, "Sources", lambda *args, **kw: backend)
    remembered = {"source": "fal", "account": entry.account, "model": "fal-ai/x", "options": {}, "origin": origin}
    context = replace(UNATTENDED, unattended=unattended, remembered_choices={"image": remembered})
    legacy = _legacy("generateImage", "runFalModel")
    lanes = {"generateImage": ("fal", legacy["generateImage"].handler), **sdk._settings_lanes(legacy, {"IMAGE_MODEL": "fal-ai/x"}, True)}
    tools = {t.name: t for t in sdk._create_unified_tools(None, context, lanes=lanes, tool_env={"IMAGE_MODEL": "fal-ai/x"})}
    output = payload(await tools["generateImage"].handler({"prompt": "tree"}))
    if unattended:
        assert output["status"] == "skipped" and output["reason"] == "stored_choice_unpriced"
        assert "Settings > Default models" in output["message"]
    else:
        assert output["status"] == "choose" and output["remembered_unpriced"] is True
    legacy["generateImage"].handler.assert_not_awaited()
    assert not any(call[0] in ("reserve", "execute") for call in backend.calls)


@pytest.mark.parametrize("unattended", [True, False])
async def test_unreadable_choices_never_open_the_unguarded_fal_path(monkeypatch, unattended):
    # A preference read failure (database, account service, identity) with a
    # saved fal default: nothing runs unasked, and main's handler never runs.
    from app.core import fal_credential, generation_choices
    from app.services import generation_sources
    backend = FakeBackend([candidate(model="fal-ai/x"), candidate(account="account-b", model="model-b")])
    monkeypatch.setattr(generation_sources, "Sources", lambda *args, **kw: backend)
    captured = []
    monkeypatch.setattr(sdk, "patch_sdk_server", lambda server, tools: server)
    monkeypatch.setattr(sdk, "create_tool_server", lambda **kw: captured.extend(kw["tools"]) or kw)
    monkeypatch.setattr(fal_credential, "is_enabled", lambda: True)
    monkeypatch.setattr(sdk, "_get_fal_api_key", lambda: "key")
    worker = SimpleNamespace(call=AsyncMock(return_value={"success": True}))
    monkeypatch.setattr(sdk, "NodeWorker", lambda **kw: worker)
    monkeypatch.setattr(generation_choices, "load", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("db down")))
    choices = generation_choices.for_session({"id": "s"})
    assert choices["unreadable"] is True and choices["remembered"] == {}
    context = replace(UNATTENDED, unattended=unattended, choices_unreadable=choices["unreadable"])
    sdk.create_ai_tools_server({"image_generation": True}, "unused-tools-dir", tool_env={"IMAGE_MODEL": "fal-ai/x"},
                               generation_context=context)
    image = next(t for t in captured if t.name == "generateImage")
    assert "Settings default" not in image.description
    for args in ({"prompt": "tree"}, {"prompt": "tree", "model": "fal-ai/x"}):
        output = payload(await image.handler(args))
        if unattended:
            assert output["status"] == "skipped" and output["reason"] == "choices_unreadable"
            assert "could not be read" in output["message"]
        elif "model" in args:
            assert output["status"] == "completed"  # named and priced: the guard ran it
        else:
            assert output["status"] == "choose" and output["choices_unreadable"] is True
    assert not any(c[0] == "execute" for c in backend.calls) or not unattended
    worker.call.assert_not_awaited()  # main's fal handler never submitted anything


