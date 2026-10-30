"""Real native registry -> resolver -> Sources admission, with zero provider calls."""
from __future__ import annotations

import json
from collections import OrderedDict
from dataclasses import dataclass, replace
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from fluid.tools.registry import ToolCtx, ToolRegistry

from app.core import generation_budget, generation_choices, generation_resolver, model_effort, query_options
from app.core.generation_resolver import (
    INCLUDED_PRICE,
    UNKNOWN_PRICE,
    Candidate,
    RunContext,
)
from app.core.native_generation_settings import load
from app.core.provider import harness_adapter as ha
from app.core.provider import native_images, native_video, supergrok_images
from app.core.provider.types import SessionOptions
from app.db import database, model_providers
from app.services.generation_sources import Sources

KINDS = [("GenerateNativeImage", "image"), ("GenerateNativeVideo", "video")]
IDENTITY = ("account", "test-owner")


@dataclass
class Probe:
    reserve: Mock
    image: AsyncMock
    video: AsyncMock
    events: list[str]

    async def registry(self, context: RunContext | None) -> ToolRegistry:
        # Match the engine's active-turn claim before dispatching pooled tools.
        generation_resolver.bind_turn_context(
            SimpleNamespace(aih_generation_context=context), session_id="session-a",
        )
        adapter = ha.HarnessAdapter()
        adapter._provider_kind = "openai_responses"
        options = SessionOptions(model="test-chat", extra={
            "provider_id": "plan-a", "session_mode": "chat", "generation_context": context,
        })
        adapter._options = options
        registry = await adapter._build_registry(options, native=True)
        assert registry is not None
        for name, _ in KINDS:
            registry.load_schema(name)
        return registry

    def no_submission(self) -> None:
        self.image.assert_not_awaited()
        self.video.assert_not_awaited()
        self.reserve.assert_not_called()

    def submitted_once(self, kind: str) -> None:
        self.reserve.assert_called_once()
        (self.image if kind == "image" else self.video).assert_awaited_once()
        (self.video if kind == "image" else self.image).assert_not_awaited()
        assert self.events == ["reserve", kind]


@pytest.fixture
def probe(monkeypatch: pytest.MonkeyPatch) -> Probe:
    from app.core import artifact_store
    from app.network import generation, runners

    monkeypatch.setattr(generation_resolver, "_TURN_CONTEXTS", OrderedDict())
    row = {"id": "plan-a", "provider_type": "grok", "enabled": True}
    monkeypatch.setattr(ha, "_lookup_provider_row", Mock(return_value=row))
    monkeypatch.setattr(database, "get_model_provider", Mock(return_value=row))
    monkeypatch.setattr(model_providers, "list_usable_model_providers", Mock(return_value=[row]))
    monkeypatch.setattr(runners, "self_device_id", Mock(return_value="worker-a"))
    monkeypatch.setattr(Sources, "fal_key", AsyncMock(return_value=None))
    monkeypatch.setattr(generation, "catalog", AsyncMock(return_value=[]))
    monkeypatch.setattr(generation.engine, "load_context", Mock(return_value=None))
    monkeypatch.setattr(model_effort, "_caller_preference_identity", Mock(return_value=IDENTITY))
    # A transport factory in the registry would reintroduce the original bypass.
    for module in (native_images, native_video):
        monkeypatch.setattr(module, "make_handler", Mock(side_effect=AssertionError("raw handler")))
    monkeypatch.setattr(native_images, "stream_images", AsyncMock(side_effect=AssertionError(
        "unexpected Responses transport",
    )))
    monkeypatch.setattr(native_images, "_save_artifacts", Mock(return_value=[("a" * 64, "")]))
    monkeypatch.setattr(artifact_store, "store_bytes", Mock(return_value={"hash": "b" * 64}))
    events: list[str] = []
    original = generation_budget.reserve

    def reserve(amount: Decimal) -> bool:
        events.append("reserve")
        return original(amount)

    async def image(*args: Any, **kwargs: Any) -> tuple[list[dict[str, Any]], None]:
        events.append("image")
        return [{"b64_json": "synthetic"}], None

    async def video(*args: Any, **kwargs: Any) -> dict[str, Any]:
        events.append("video")
        return {"success": True, "data": b"synthetic"}

    value = Probe(Mock(side_effect=reserve), AsyncMock(side_effect=image),
                  AsyncMock(side_effect=video), events)
    monkeypatch.setattr(generation_budget, "reserve", value.reserve)
    monkeypatch.setattr(supergrok_images, "generate", value.image)
    monkeypatch.setattr(native_video, "generate", value.video)
    return value


@pytest.fixture
async def automation_id() -> str:
    from app.core.automation_service import create_automation

    automation = await create_automation(
        name="Native admission fixture", trigger_type="interval",
        trigger_config={"seconds": 300}, profile_id="default", prompt="hi",
    )
    return automation["id"]


def context(
    *, unattended: bool, kind: str | None = None, job_id: str | None = None,
) -> RunContext:
    if kind:
        choice = {
            "source": "supergrok", "account": "plan-a", "worker": "worker-a",
            "model": "grok-imagine-image" if kind == "image" else "grok-imagine-video-1.5-lite",
            "options": {},
        }
        if job_id is not None:
            generation_choices.save_job(IDENTITY, job_id, kind, choice)
        else:
            generation_choices.save(IDENTITY, kind, choice, remember=True)
    # Exercise the saved-choice producing path even with only native grants.
    return query_options._generation_context(
        {}, {"id": "session-a", **({"automation_id": job_id} if job_id is not None else {})}, None,
        session_id="session-a", unattended=unattended, chat_mode=True, native_generation=True,
    )


async def call(
    registry: ToolRegistry, name: str, args: dict[str, Any] | None = None,
) -> dict[str, Any]:
    outcome = await registry.dispatch(name, {"prompt": "a square", **(args or {})}, ToolCtx())
    output = json.loads(outcome.text)
    assert outcome.result_fields == output
    return output


@pytest.mark.parametrize("name,kind", KINDS)
@pytest.mark.parametrize("unattended", [False, True])
async def test_no_implicit_account_before_admission(
    probe: Probe, name: str, kind: str, unattended: bool,
) -> None:
    registry = await probe.registry(context(unattended=unattended))
    output = await call(registry, name)
    assert output["status"] == ("skipped" if unattended else "choose")
    if unattended:
        assert output["reason"] == "no_stored_choice"
    else:
        assert output["options"][0]["price"] == (
            INCLUDED_PRICE if kind == "image" else native_video.estimate_price(load("plan-a").video)
        )
    probe.no_submission()


@pytest.mark.parametrize("name,kind", KINDS)
@pytest.mark.parametrize("unattended", [False, True])
async def test_real_saved_choice_reaches_budget_then_same_adapter(
    probe: Probe, name: str, kind: str, unattended: bool,
) -> None:
    registry = await probe.registry(context(unattended=unattended, kind=kind))
    output = await call(registry, name)
    assert output["status"] == "completed" and output["remembered"]
    assert output["price"]["status"] == ("included" if kind == "image" else "estimated")
    probe.submitted_once(kind)
    assert probe.reserve.call_args.args[0] == Decimal("0" if kind == "image" else "0.02")


@pytest.mark.parametrize("name,kind", KINDS)
async def test_unattended_job_choice_submits_once(
    probe: Probe, name: str, kind: str, automation_id: str,
) -> None:
    registry = await probe.registry(
        context(unattended=True, kind=kind, job_id=automation_id),
    )
    output = await call(registry, name)
    assert output["status"] == "completed" and not output.get("remembered")
    probe.submitted_once(kind)


@pytest.mark.parametrize("name,kind", KINDS)
async def test_unreadable_saved_choice_skips_unattended(
    probe: Probe, name: str, kind: str, automation_id: str,
) -> None:
    key = generation_choices._job_key(automation_id, kind)
    database.set_user_preference(*IDENTITY, key, {"broken": True})
    current = context(unattended=True, job_id=automation_id)
    assert current.choices_unreadable
    output = await call(await probe.registry(current), name, {"source": "plan-a"})
    assert output["status"] == "skipped" and output["reason"] == "choices_unreadable"
    probe.no_submission()


@pytest.mark.parametrize("name,kind", KINDS)
@pytest.mark.parametrize("unattended", [False, True])
async def test_exhausted_cap_refuses_even_included_images(
    probe: Probe, name: str, kind: str, unattended: bool,
) -> None:
    generation_budget.record_overrun("20.01")
    output = await call(await probe.registry(context(unattended=unattended, kind=kind)), name)
    assert output["status"] == "refused" and output["reason"] == "daily_cap"
    probe.reserve.assert_called_once()
    probe.image.assert_not_awaited()
    probe.video.assert_not_awaited()


@pytest.mark.parametrize("name,kind", KINDS)
async def test_positive_remaining_allowance_below_video_quote_refuses(
    probe: Probe, name: str, kind: str,
) -> None:
    if kind == "image":
        generation_budget.record_overrun("20.01")
    else:
        generation_budget.record_overrun("19.99")
    output = await call(await probe.registry(context(unattended=True, kind=kind)), name)
    assert output["reason"] == "daily_cap"
    probe.image.assert_not_awaited()
    probe.video.assert_not_awaited()


@pytest.mark.parametrize("name,kind", KINDS)
@pytest.mark.parametrize("unattended", [False, True])
async def test_above_threshold_requires_admission_before_reserve_or_transport(
    probe: Probe, monkeypatch: pytest.MonkeyPatch, name: str, kind: str, unattended: bool,
) -> None:
    original = Sources.prepare

    async def prepare(
        self: Sources, candidate: Candidate, options: dict[str, Any], prompt: str,
    ) -> Candidate:
        prepared = await original(self, candidate, options, prompt)
        # Synthetic quote only: no claim of a verified subscription price change.
        return replace(prepared, price={
            "currency": "USD", "unit": "item", "amount": "0.60", "total": "0.60",
            "status": "estimated",
        })

    monkeypatch.setattr(Sources, "prepare", prepare)
    current = context(unattended=unattended, kind=kind if unattended else None)
    args = {} if unattended else {"source": "plan-a"}
    output = await call(await probe.registry(current), name, args)
    assert output["status"] == ("skipped" if unattended else "needs-confirmation")
    probe.no_submission()
    if unattended:
        assert output["reason"] == "needs_confirmation" and "confirmation_token" not in output
    else:
        # The card's exact approved retry survives the next turn's registry rebuild.
        output = await call(await probe.registry(current), name, output["retry"])
        assert output["status"] == "completed"
        probe.submitted_once(kind)


@pytest.mark.parametrize("name,kind", KINDS)
async def test_unknown_price_and_unavailable_account_never_submit(
    probe: Probe, monkeypatch: pytest.MonkeyPatch, name: str, kind: str,
) -> None:
    entry = (await Sources(None, RunContext(owner=True)).local_catalog(kind))[0]
    for candidate in (replace(entry, price=UNKNOWN_PRICE), replace(entry, available=False)):
        monkeypatch.setattr(Sources, "catalog", AsyncMock(return_value=[candidate]))
        output = await call(await probe.registry(context(unattended=False)), name,
                            {"source": "plan-a"})
        assert output["status"] == "refused"
        probe.no_submission()


@pytest.mark.parametrize("name,kind", KINDS)
async def test_missing_host_context_cannot_be_overridden_by_arguments(
    probe: Probe, name: str, kind: str,
) -> None:
    output = await call(await probe.registry(None), name, {
        "source": "plan-a", "confirmed": True,
    })
    assert output["status"] == "skipped" and output["reason"] == "no_stored_choice"
    probe.no_submission()


async def test_native_image_legacy_options_are_validated_and_conflicts_refuse(probe: Probe) -> None:
    registry = await probe.registry(context(unattended=False))
    for args in (
        {"image_model": "unverified"},
        {"source": "plan-a", "quality": "unverified"},
        {"image_model": "grok-imagine-image", "model": "different"},
        {"size": "1k", "options": {"size": "2k"}},
        {"options": []},
    ):
        output = await call(registry, "GenerateNativeImage", args)
        assert output["status"] == "refused"
        probe.no_submission()
    output = await call(registry, "GenerateNativeImage", {
        "source": "plan-a", "image_model": "grok-imagine-image", "size": "1k", "format": "jpeg",
    })
    assert output["status"] == "completed"
    probe.submitted_once("image")


async def test_sdk_options_project_same_trusted_context_to_native_aliases(
    probe: Probe, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.agent_types import AgentOptions

    current = context(unattended=True, kind="video")
    sdk_options = AgentOptions(model="test-chat")
    sdk_options.aih_generation_context = current
    adapter = ha.HarnessAdapter()
    connect = AsyncMock()
    monkeypatch.setattr(adapter, "connect", connect)
    await adapter.connect_with_sdk_options(sdk_options)
    projected = connect.call_args.args[0]
    assert projected.extra["generation_context"] is current


@pytest.mark.parametrize("explicit_none", [False, True])
async def test_sdk_options_without_generation_context_leave_extra_empty(
    monkeypatch: pytest.MonkeyPatch, explicit_none: bool,
) -> None:
    from app.core.agent_types import AgentOptions

    sdk_options = AgentOptions(model="test-chat")
    if explicit_none:
        sdk_options.aih_generation_context = None
    adapter = ha.HarnessAdapter()
    connect = AsyncMock()
    monkeypatch.setattr(adapter, "connect", connect)
    await adapter.connect_with_sdk_options(sdk_options)
    assert connect.call_args.args[0].extra == {}


@pytest.mark.parametrize("name,kind", KINDS)
async def test_native_grants_do_not_offer_or_execute_fal(
    probe: Probe, monkeypatch: pytest.MonkeyPatch, name: str, kind: str,
) -> None:
    entry = (await Sources(None, RunContext(owner=True)).local_catalog(kind))[0]
    monkeypatch.setattr(Sources, "catalog", AsyncMock(return_value=[
        entry, replace(entry, source="fal", account="fal-ai", model="test/model"),
    ]))
    registry = await probe.registry(context(unattended=False))
    output = await call(registry, name)
    assert output["status"] == "choose"
    assert {option["source"] for option in output["options"]} == {"supergrok"}
    output = await call(registry, name, {"source": "fal", "model": "test/model"})
    assert output["status"] == "refused"
    probe.no_submission()


@pytest.mark.parametrize("name,kind", KINDS)
async def test_uncertain_outcome_keeps_reservation_without_fallback(
    probe: Probe, name: str, kind: str,
) -> None:
    transport = probe.image if kind == "image" else probe.video
    transport.side_effect = RuntimeError("synthetic uncertain outcome")
    output = await call(await probe.registry(context(unattended=False, kind=kind)), name)
    assert output["status"] == "failed"
    assert "no fallback or automatic retry" in output["result"]["error"]
    probe.reserve.assert_called_once()
    transport.assert_awaited_once()
    (probe.video if kind == "image" else probe.image).assert_not_awaited()


@pytest.mark.parametrize("unattended", [False, True])
async def test_options_builder_produces_trusted_context_for_native_only_grants(
    probe: Probe, monkeypatch: pytest.MonkeyPatch, unattended: bool,
) -> None:
    from tests.core.helpers import query_profile

    current = context(unattended=unattended, kind="video")
    # A ceiling chat (one the preview cookie sends): the owner's own no-project
    # chat is a normal session and has no chat-mode context (#641).
    from app.core import session_orchestration
    monkeypatch.setattr(session_orchestration, "_principal", lambda sid: {
        "owner": "install-admin", "kind": "admin", "principal_id": "preview"})
    monkeypatch.setattr(database, "get_session", Mock(return_value={
        "id": "session-a", "mode": "chat", "model": "opus",
    }))
    monkeypatch.setattr(query_options, "get_security_instructions", Mock(return_value=""))
    options, _ = query_options.build_options_from_profile(
        query_profile({"tool_selection_mode": "allow", "allowed_tools": [
            "GenerateNativeImage", "GenerateNativeVideo",
        ]}),
        overrides={"turn_source": "automation" if unattended else "user"},
        session_id="session-a",
    )
    produced = options.aih_generation_context
    # The builder also stamps the turn's source (prompt skips key on it).
    from dataclasses import replace
    assert produced == replace(current, turn_source="automation" if unattended else "user")
    assert produced.remembered_choices["video"]["account"] == "plan-a"
