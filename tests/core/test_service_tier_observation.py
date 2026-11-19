"""The opt-in service-tier observation: from the owner's preference to a stored turn.usage row.

The producing path is real: fluid's ``NativeLoop`` and Responses client run
against the scripted loopback provider, the adapter turns the call's Usage into
the app's usage record, and the turn recorder stores it as ``turn.usage``.
"""
import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fluid.engine.native import NativeLoop
from fluid.events import UserTurn
from fluid.events.model import Usage
from fluid.permissions.engine import PermissionEngine
from fluid.providers._retry import RetryPolicy
from fluid.providers.openai_responses.client import OpenAIResponsesClient
from fluid.store.base import MemoryStore
from fluid.tools.registry import ToolRegistry

from app.core import preference_identity
from app.core.provider import harness_adapter as ha
from app.core.turn_recorder import TurnRecorder
from app.db import database
from app.db import session_events
from app.db import sessions as sessions_db

KEY = ha.OBSERVE_SERVICE_TIER_PREFERENCE


def _load_fake_server():
    name = "fake_provider_server"
    if name in sys.modules:
        return sys.modules[name]
    path = Path(__file__).resolve().parents[2] / "fluid" / "tests" / "fixtures" / "fake_provider_server.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


fps = _load_fake_server()


@pytest.fixture
def owner(monkeypatch):
    monkeypatch.setattr(preference_identity, "principal_preference_identity", lambda _: ("account", "owner"))


@pytest.fixture
def server():
    srv = fps.FakeProviderServer()
    try:
        yield srv
    finally:
        srv.close()


def test_only_a_chat_the_owner_lists_is_observed(mock_db, owner):
    sessions_db.create_session("test-chat", "default")
    sessions_db.create_session("owner-chat", "default")
    assert not ha._observes_service_tier("test-chat")  # off by default
    database.set_user_preference("account", "owner", KEY, ["test-chat"])
    assert ha._observes_service_tier("test-chat")
    assert not ha._observes_service_tier("owner-chat")
    assert not ha._observes_service_tier("no-such-chat")
    assert not ha._observes_service_tier(None)
    database.set_user_preference("account", "owner", KEY, "test-chat")  # not a list
    assert not ha._observes_service_tier("test-chat")
    database.set_user_preference("account", "owner", KEY, [])
    assert not ha._observes_service_tier("test-chat")


def test_a_failed_check_means_off(monkeypatch):
    def broken(_):
        raise RuntimeError("database unavailable")
    monkeypatch.setattr(database, "get_session", broken)
    assert ha._observes_service_tier("test-chat") is False


async def test_every_turn_rereads_the_preference(monkeypatch):
    listed = {"test-chat"}
    monkeypatch.setattr(ha, "_observes_service_tier", lambda sid: sid in listed)
    adapter = ha.HarnessAdapter()
    adapter._provider_kind = "openai_responses"
    adapter._engine = engine = SimpleNamespace(submit=AsyncMock(), ctx=None, observe_service_tier=False)
    monkeypatch.setattr(adapter, "_resolve_app_session_id", lambda: "test-chat")
    await adapter._submit_turn(UserTurn(content="one"))
    assert engine.observe_service_tier is True
    listed.clear()  # the owner removes the chat; its next turn records nothing
    await adapter._submit_turn(UserTurn(content="two"))
    assert engine.observe_service_tier is False


async def test_a_delegated_turn_is_never_observed(monkeypatch):
    monkeypatch.setattr(ha, "_observes_service_tier", lambda sid: True)
    adapter = ha.HarnessAdapter()
    adapter._is_delegated = True
    adapter._engine = engine = SimpleNamespace(submit=AsyncMock(), ctx=None)
    monkeypatch.setattr(adapter, "_resolve_app_session_id", lambda: "test-chat")
    await adapter._submit_turn(UserTurn(content="one"))
    assert not hasattr(engine, "observe_service_tier")


async def _observed_call(server, *, asked: str | None, reported: str | None) -> Usage:
    """One observed turn on the real loop; its main call's Usage."""
    extra = {"service_tier": reported} if reported else {}
    server.script([fps.Responses.created(), *fps.Responses.message("msg_1", "zebra-reply-7"),
                   fps.Responses.completed(**extra)])
    loop = NativeLoop(
        provider=OpenAIResponsesClient(
            api_key="k", base_url=server.url + "/v1/responses",
            retry=RetryPolicy(max_retries=0, base_delay=0.01, max_delay=0.05, jitter=0)),
        store=MemoryStore(), model="gpt-6.1-sol", registry=ToolRegistry(),
        permissions=PermissionEngine("never_ask"),
    )
    loop.observe_service_tier = True
    if asked:
        loop.request_extra["service_tier"] = asked
    await loop.submit(UserTurn(content="zebra-prompt-7"))
    await asyncio.wait_for(loop.wait_turn(), 20.0)
    [usage] = [e for e in loop.bus.log(loop.session_id)
               if isinstance(e, Usage) and e.raw.get("model_call_index") is not None]
    await loop.aclose()
    return usage


@pytest.mark.parametrize("asked, reported, stored", [
    ("ultrafast", "default", ("ultrafast", "default")),  # an Ultrafast turn
    (None, None, ("none", "absent")),                     # a Standard turn
])
async def test_an_observed_call_reaches_the_stored_usage_row(mock_db, server, asked, reported, stored):
    usage = await _observed_call(server, asked=asked, reported=reported)
    [message] = ha.HarnessAdapter()._translate(usage)
    sessions_db.create_session("test-chat", "default")
    recorder = await TurnRecorder.maybe_create("test-chat")
    await recorder.on_event({"type": "turn_usage", "usage": message.usage})
    await recorder.close()
    [row] = [r for r in session_events.get_events("test-chat") if r["event_type"] == "turn.usage"]
    record = row["payload"]["usage"]
    assert (record["service_tier_requested"], record["service_tier_reported"]) == stored
    # Only the two tier tokens: no prompt or reply text rides along.
    assert "zebra" not in json.dumps(row["payload"])
