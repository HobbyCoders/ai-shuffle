"""Persisted pins follow only the active account in the same slot."""
from unittest.mock import patch

import pytest

from app.core import encryption, model_override, query_options
from app.core import automation_service as automations
from app.core.agent_provider_models import model_for_provider
from app.core.provider.fluid_credentials import resolve_provider_credentials
from app.core.provider_agent import validate_provider_model
from app.db import database as db
from tests.core.helpers import make_profile


@pytest.fixture
def accounts(mock_db):
    encryption.set_encryption_key("test-password", db)
    for id in ("old", "active"):
        db.create_model_provider(id, id, "openrouter", "https://openrouter.ai/api",
                                 auth_token=encryption.encrypt_value("key-" + id),
                                 models=[{"id": "same-model", "context_window": 200000}],
                                 default_model="same-model", enabled=(id == "old"))
    return "prov:old:same-model"


def switch():
    db.set_model_provider_enabled("active", True)


def test_session_turn_and_usage_follow_active_account(accounts, tmp_path):
    profile = make_profile("pinned", {"provider": "old", "model": "same-model", "cwd": str(tmp_path)})
    db.create_session("chat", profile_id="pinned")
    db.set_session_run_binding("chat", model=accounts)
    switch()
    with patch.object(query_options, "get_security_instructions", return_value=""):
        options, _ = query_options.build_options_from_profile(profile=profile, session_id="chat")
    assert options.aih_provider_id == "active"
    assert options.aih_provider_pin_id == "old"
    assert options.model == "same-model"
    assert db.get_session("chat")["model"] == accounts
    assert db.get_session("chat")["provider_id"] == "old"
    from app.core.query_persistence import _turn_usage_attribution, USAGE_PROVIDER_KEY
    # Billing books the captured turn account, not the stored pin or current slot.
    db.set_model_provider_enabled("old", True)
    assert _turn_usage_attribution("chat", {USAGE_PROVIDER_KEY: options.aih_provider_id})["provider_id"] == "active"


@pytest.mark.asyncio
async def test_automation_producer_and_prefetched_model_follow_slot(accounts):
    automation = await automations.create_automation(
        name="Pinned", trigger_type="interval", trigger_config={"seconds": 60}, profile_id="default",
        prompt="hello", model=accounts)
    switch()
    for lookups in (automations.ModelLookups(), automations.ModelLookups().prefetch()):
        result = automations.resolve_automation_model(automation, lookups=lookups)
        assert result["error"] is None
        assert result["provider_id"] == "active"
        assert result["model"] == "same-model"
        assert result["selection"] == accounts  # preserve the durable original pin


def test_validation_and_task_default_normalize_to_active_account(accounts):
    from app.core import subagent_default
    from app.core.task_dispatch import _credential_gap
    switch()
    assert validate_provider_model("old", "same-model").provider["id"] == "active"
    assert subagent_default.normalize_model(accounts) == ("prov:active:same-model", None)
    assert model_override.resolve(accounts).provider_id == "active"
    assert _credential_gap("old") is None
    assert model_for_provider({"provider_models": {"old": accounts}}, "old") == accounts


@pytest.mark.asyncio
async def test_generation_credentials_follow_active_key(accounts):
    switch()
    credentials = await resolve_provider_credentials("openrouter", "old")
    assert credentials.api_key == "key-active"
    assert credentials.provider_id == "active"


def test_voice_funding_pin_follows_active_sibling(mock_db):
    from app.api.voice import _eligible_provider_row
    for id in ("old", "active"):
        db.create_model_provider(id, id, "openai", "https://api.openai.com/v1", auth_token="enc:fake")
    assert _eligible_provider_row("old")["id"] == "active"


def test_disabled_slot_without_active_sibling_still_refuses(accounts):
    db.set_model_provider_enabled("old", False)
    validation = validate_provider_model("old", "same-model")
    assert not validation.ok
    assert validation.code == "provider_disabled"


def test_other_kinds_and_gateways_never_take_over(accounts):
    db.set_model_provider_enabled("old", False)
    db.create_model_provider("subscription", "Sub", "chatgpt", "https://example.com")
    assert db.resolve_model_provider("old")["id"] == "old"
    db.create_model_provider("custom1", "One", "custom", "https://example.com", enabled=False)
    db.create_model_provider("custom2", "Two", "custom", "https://example.com")
    assert db.resolve_model_provider("custom1")["id"] == "custom1"


@pytest.mark.asyncio
@pytest.mark.parametrize("late_switch", [False, True])
async def test_pinned_chat_turn_reaches_fake_provider_with_active_accounts_key(accounts, tmp_path, monkeypatch, late_switch):
    import asyncio
    from scripts.devloop.fake_wire import Chat, FakeProviderServer
    from app.core.provider.harness_adapter import HarnessAdapter
    from app.core.provider.events import ResultMessage

    # Real HTTP, real app adapter and native loop, not a stub of the resolver.
    # Only external model output is scripted; no credential/provider call is paid.
    home = tmp_path / "home"
    home.mkdir()
    from pathlib import Path
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    with FakeProviderServer(Chat.text("active account answered")) as server:
        for id in ("old", "active"):
            db.update_model_provider(id, base_url=server.url)
        profile = make_profile("wire-pin", {"model": "same-model", "provider": "old", "cwd": str(tmp_path)})
        db.create_session("wire-chat", profile_id="wire-pin")
        db.set_session_run_binding("wire-chat", model=accounts)
        if not late_switch:
            switch()
        with patch.object(query_options, "get_security_instructions", return_value=""):
            options, _ = query_options.build_options_from_profile(profile=profile, session_id="wire-chat")
        # This text-only assertion does not start unrelated MCP tool servers.
        options.mcp_servers = {}
        if late_switch:
            # Interleave the real DB switch AFTER the SDK read, BEFORE HTTP
            # credentials are fetched. Both credentials and attribution must
            # capture the same new account, not the old SDK snapshot.
            from app.core.provider import harness_adapter
            original_resolver = harness_adapter.resolve_provider_credentials
            async def switch_before_credentials(kind, provider_id):
                switch()
                return await original_resolver(kind, provider_id)
            monkeypatch.setattr(harness_adapter, "resolve_provider_credentials", switch_before_credentials)
        adapter = HarnessAdapter()
        try:
            await adapter.connect_with_sdk_options(options)
            assert adapter._options.extra["provider_id"] == "active"
            assert adapter._options.extra["provider_pin_id"] == "old"
            await adapter.send_query("Which account answers?")
            async def collect():
                async for event in adapter.receive_events():
                    if isinstance(event, ResultMessage):
                        return event
                raise AssertionError("turn ended without result")
            final = await asyncio.wait_for(collect(), timeout=15)
            assert not final.is_error
            assert adapter._options.extra["provider_id"] == "active"
        finally:
            await adapter.disconnect()
        assert len(server.requests) == 1
        request = server.requests[0]
        assert request.body["model"] == "same-model"
        headers = {k.lower(): v for k, v in request.headers.items()}
        assert headers["authorization"] == "Bearer key-active"
        assert db.get_session("wire-chat")["provider_id"] == "old"


def test_text_generation_setting_follows_slot(accounts):
    from app.core.generation_options import (
        TEXT_GENERATION_MODEL_KEY, build_generation_options, text_generation_config,
    )
    db.set_system_setting(TEXT_GENERATION_MODEL_KEY, accounts)
    switch()
    options = build_generation_options(text_generation_config())
    assert options.aih_provider_id == "active"
    assert options.model == "same-model"
    assert db.get_system_setting(TEXT_GENERATION_MODEL_KEY) == accounts


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["chatgpt", "grok"])
async def test_subscription_credentials_follow_actual_account(mock_db, mode):
    encryption.set_encryption_key("test-password", db)
    for id in ("old", "active"):
        db.create_model_provider(id, id, "custom", "https://example.com", enabled=False)
        db.set_codex_token_bundle(id, access_token=encryption.encrypt_value("token-" + id),
                                 refresh_token=encryption.encrypt_value("refresh-" + id),
                                 account_id=id, auth_mode=mode, activate=True)
    credentials = await resolve_provider_credentials("openai_responses", "old")
    assert credentials.auth.provider_id == "active"
    assert await credentials.auth.get_access_token() == "token-active"


@pytest.mark.asyncio
async def test_saved_media_account_choice_follows_slot_without_changing_model(accounts):
    from app.core.generation_resolver import RunContext
    from app.services.generation_sources import Sources
    switch()
    backend = Sources(None, RunContext(owner=True))
    choice = {"source": "old", "account": "old", "model": "same-image-model", "worker": "peer"}
    result = await backend.follow_account_choice(choice)
    assert result == {**choice, "source": "active", "account": "active"}
    assert choice["account"] == "old"


@pytest.mark.asyncio
@pytest.mark.parametrize("stored_field", ["job_choice", "remembered_choice"])
async def test_unattended_media_resolver_follows_saved_account_slot(accounts, stored_field):
    from app.core.generation_resolver import Candidate, INCLUDED_PRICE, Resolver, RunContext
    from app.services.generation_sources import Sources
    for id in ("old", "active"):
        db.update_model_provider(id, provider_type="openai")
    switch()
    entry = Candidate("openai", "active", "worker", "same-image-model", "image", True,
                      price=dict(INCLUDED_PRICE))
    class Backend(Sources):
        async def catalog(self, kind, **kwargs):
            assert kwargs["source"] == "openai"
            assert kwargs["model"] == "same-image-model"
            return [entry]
        async def prepare(self, candidate, options, prompt):
            return candidate
        async def reserve(self, candidate, context):
            return True
        async def execute(self, candidate, prompt, context):
            assert candidate.account == "active"
            return {"success": True, "output_url": "/fake-image"}
    saved = {"source": "openai", "account": "old", "worker": "worker", "model": "same-image-model"}
    context = RunContext(owner=True, unattended=True, **{stored_field: saved})
    result = await Resolver(Backend(None, context)).generate("image", {"prompt": "make it"}, context)
    assert result["status"] == "completed", result
    assert result["choice"]["account"] == "active"
    assert saved["account"] == "old"


def test_task_contract_keeps_original_model_map_when_accounts_switch(accounts, tmp_path):
    from app.core.provider.harness_adapter import _native_task_contract
    from app.core.provider.types import SessionOptions
    make_profile("caller", {"enabled_agents": ["child"]})
    make_profile("child", {"provider_models": {
        "old": accounts, "active": "prov:active:different-model",
    }})
    db.update_model_provider("active", models_json=[{"id": "same-model"}, {"id": "different-model"}])
    switch()
    options = SessionOptions(model="same-model", cwd=str(tmp_path), extra={
        "provider_id": "active", "provider_pin_id": "old", "profile_id": "caller",
    })
    contract = _native_task_contract(options)
    assert contract is not None
    target = next(t for t in contract.targets if t.id == "child")
    assert target.model == "same-model"


def test_changing_original_pin_reconnects_task_contract_with_same_active_account():
    from app.core.agent_types import AgentOptions
    from app.core.session_pool import SessionState, reconnect_option_names
    old, new = AgentOptions(model="same-model"), AgentOptions(model="same-model")
    old.aih_provider_id = new.aih_provider_id = "active"
    old.aih_provider_pin_id, new.aih_provider_pin_id = "old", "active"
    assert "aih_provider_pin_id" in reconnect_option_names(SessionState(adapter=object(), connect_options=old), new)
