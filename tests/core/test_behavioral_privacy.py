"""Privacy regression tests with the actual Responses HTTP boundary intercepted."""
import asyncio
from collections import OrderedDict
import json
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest
from fluid.engine.native import NativeLoop
from fluid.events.ops import UserTurn
from fluid.store.base import MemoryStore

from app.api import generation
from app.core import behavioral_learning as learning, behavioral_provenance as provenance
from app.core import guidance_surface, session_runtime
from app.core.config import settings
from app.core.provider import harness_adapter as harness
from app.core.provider.types import SessionOptions
from app.db import database, turns
from tests.core.test_session_runtime import _runtime_env
from tests.shared_fixtures import pin_runner  # noqa: F401

SID = "privacy-source"
PROVIDER = "privacy-subscription"
MODEL = "gpt-test-source"
ACCOUNT = "test-account-a"
ROUTE = {"provider": PROVIDER, "model": "gpt-test-reviewer", "effort": "low"}
PRIVATE = "Synthetic private transcript: investigate routine failures before reporting."
CODEX_URL = "https://chatgpt.com/backend-api/codex/responses"


def _event_stream(text, model):
    events = (
        {"type": "response.created", "response": {"id": "fake-resp", "model": model}},
        {"type": "response.output_text.delta", "delta": text},
        {"type": "response.completed", "response": {"status": "completed", "output": [], "usage": {}}},
    )
    return "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events)


@pytest.fixture
def wire(mock_db, monkeypatch):
    row = {"id": PROVIDER, "provider_type": "openai", "enabled": True,
           "auth_mode": "chatgpt", "codex_account_id": ACCOUNT,
           "codex_access_token": "synthetic-encrypted-token"}
    monkeypatch.setattr(database, "resolve_model_provider", lambda *_a, **_k: row)
    monkeypatch.setattr(database, "get_model_provider", lambda *_a, **_k: row)
    monkeypatch.setattr(database, "get_codex_tokens", lambda *_a, **_k: row)
    monkeypatch.setattr("app.core.encryption.decrypt_value", lambda _: "synthetic-token")
    requests = []
    state = {"status": 200, "reply": "null", "transport_error": False}

    def respond(request):
        requests.append(request)
        assert str(request.url) == CODEX_URL
        if state["transport_error"]:
            raise httpx.ConnectError("synthetic connection failure", request=request)
        model = state.get("served_model", json.loads(request.content)["model"])
        return httpx.Response(state["status"], headers={"content-type": "text/event-stream",
                                                        "location": "https://example.invalid/fallback"},
                              text=_event_stream(state["reply"], model))

    class MockClient(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **{**kwargs, "transport": httpx.MockTransport(respond)})

    monkeypatch.setattr(httpx, "AsyncClient", MockClient)
    return row, requests, state


@pytest.fixture
async def source(wire, monkeypatch, tmp_path):
    monkeypatch.setenv(guidance_surface.GUIDANCE_HOME_ENV, str(tmp_path))
    monkeypatch.setattr(settings, "fluid_continual", True)
    monkeypatch.setattr(learning, "DEBOUNCE_SECONDS", 0)
    monkeypatch.setattr(learning, "_queue", OrderedDict())
    monkeypatch.setattr(learning, "_worker", None)
    monkeypatch.setattr(provenance, "_recent", OrderedDict())
    monkeypatch.setattr("app.core.auth.auth_service.get_admin_username", lambda: "test-admin")
    monkeypatch.setattr("app.core.preference_identity.principal_preference_identity", lambda _: ("admin", "owner"))
    monkeypatch.setattr("app.core.generation_options.text_generation_config", lambda: ROUTE)
    review_call = AsyncMock(return_value="null")
    monkeypatch.setattr(generation, "generate_with_claude", review_call)
    database.create_session(session_id=SID, profile_id="default", provider_id=PROVIDER, model=MODEL)
    learning.start()
    yield review_call
    await learning.stop()
    session_runtime.register_scheduler(None)
    session_runtime.register_broadcaster(None)


async def _settle(user, *, model=MODEL, leg="native", reply="I only checked its status."):
    row = turns.enqueue_turn(SID, "user", prompt=user, person_entered=True)

    async def pump(*_args, **_kwargs):
        # Only WS translation is synthetic: the actual runtime starts the
        # pump, the real submit barrier starts NativeLoop's task, and the
        # actual authorized Responses HTTP hook observes its request.
        if leg != "unattested":
            options = SessionOptions(model=model, extra={"provider_id": PROVIDER, "app_session_id": SID})
            adapter = harness.HarnessAdapter()
            adapter._options = options
            if leg == "delegated":
                # Deliberately keep the saved session's provider/model pin.
                # The effective engine is not native, even if a pin says so.
                adapter._provider_kind = "delegated"
                adapter._is_delegated = True
                adapter._engine = AsyncMock(spec=harness.DelegatedLoop)
                await adapter._submit_turn(UserTurn(content=user))
            else:
                adapter._provider_kind = "openai_responses"
                provider, http = await harness._build_provider_client("openai_responses", options)
                loop = NativeLoop(provider=provider, store=MemoryStore(), model=model,
                                  system="Synthetic test", allowed_tools=[])
                adapter._engine = loop
                try:
                    await adapter._submit_turn(UserTurn(content=user))
                    await asyncio.wait_for(loop.wait_turn(), timeout=3)
                    assert any(type(e).__name__ == "TurnComplete" and e.stop_reason != "error"
                               for e in loop.bus.log(loop.session_id))
                finally:
                    await loop.aclose()
                    await http.aclose()
        yield {"type": "stream_delta", "delta_type": "text", "content": reply}
        yield {"type": "done", "metadata": {"stop_reason": "end_turn"}}

    real_recorder = session_runtime.TurnRecorder.create_for_run
    with _runtime_env(pump=pump), patch.object(session_runtime.TurnRecorder, "create_for_run", new=real_recorder):
        assert await session_runtime.SessionRuntime(row).execute() == "completed"
    if learning._worker:
        await learning._worker
    return row["id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["same", "account", "model", "served", "unattested", "expired", "delegated"])
async def test_review_only_uses_a_matching_recent_native_window(source, wire, monkeypatch, boundary):
    row, requests, state = wire
    if boundary == "served":
        state["served_model"] = "gpt-other"
    first_model = "gpt-other" if boundary == "model" else MODEL
    first_leg = boundary if boundary in ("delegated", "unattested") else "native"
    first = await _settle("Check this change.", model=first_model, leg=first_leg)
    state.pop("served_model", None)
    if first_leg == "native" and boundary != "served":
        assert provenance.settled_binding(SID, first) == provenance.Binding(PROVIDER, ACCOUNT, first_model)
    else:
        assert provenance.settled_binding(SID, first) is None
    if boundary == "expired":
        now = provenance.monotonic() + provenance.MAX_AGE_SECONDS + 1
        monkeypatch.setattr(provenance, "monotonic", lambda: now)
    if boundary == "account":
        # Same app session, provider row, and model; the signed-in account changes.
        row["codex_account_id"] = "test-account-b"
    second = await _settle(PRIVATE)
    assert requests[-1].headers["chatgpt-account-id"] == row["codex_account_id"]
    assert provenance.settled_binding(SID, second) == provenance.Binding(PROVIDER, row["codex_account_id"], MODEL)
    if boundary == "same":
        assert source.await_count >= 1
        assert PRIVATE in source.call_args_list[0].args[0]
    else:
        source.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["expired", "account", "unattested"])
async def test_review_keeps_only_the_fresh_matching_suffix(source, wire, monkeypatch, boundary):
    row, _, _ = wire
    account_a = row["codex_account_id"]
    account_b = "test-account-b" if boundary == "account" else account_a
    row["codex_account_id"] = account_b
    await _settle("Earlier matching owner text.", reply="Earlier matching reply.")
    row["codex_account_id"] = account_a
    barrier = await _settle("Old owner text.", reply="Old reply text.",
                            leg="unattested" if boundary == "unattested" else "native")
    if boundary == "expired":
        now = provenance.monotonic() + provenance.MAX_AGE_SECONDS + 1
        monkeypatch.setattr(provenance, "monotonic", lambda: now)
    expected = provenance.Binding(PROVIDER, account_a, MODEL) if boundary == "account" else None
    assert provenance.settled_binding(SID, barrier) == expected
    row["codex_account_id"] = account_b
    source.reset_mock()

    await _settle("First fresh owner text.", reply="First fresh reply.")
    source.assert_not_awaited()  # One fresh turn is not an exchange.
    await _settle(PRIVATE, reply="Second fresh reply.")

    source.assert_awaited_once()
    (prompt,) = source.await_args.args
    assert prompt.index("First fresh reply.") < prompt.index(PRIVATE)
    assert "Old owner text." not in prompt and "Old reply text." not in prompt
    assert "Earlier matching" not in prompt

    newest = await _settle("Unattested newest owner text.", leg="unattested")
    assert not learning._transcript(SID, newest, learning.Scope("", MODEL, PROVIDER, account_b))
    source.assert_awaited_once()


@pytest.mark.asyncio
async def test_review_wire_is_stateless_tool_free_and_has_no_secondary_persistence(wire, monkeypatch, tmp_path, caplog):
    _, requests, _ = wire
    trace_dir = tmp_path / "trace"
    monkeypatch.setenv("FLUID_TRACE_REQUESTS", str(trace_dir))
    monkeypatch.setenv("FLUID_RESPONSES_ROUTING_ARM", "D")
    adapter = Mock(side_effect=AssertionError("review may not create a harness session"))
    usage = Mock(side_effect=AssertionError("review must not traverse turn accounting"))
    quota = Mock(side_effect=AssertionError("review must not persist provider quota"))
    monkeypatch.setattr(generation, "make_adapter", adapter)
    monkeypatch.setattr(database, "log_usage", usage)
    monkeypatch.setattr("app.core.provider_usage.capture_codex_headers", quota)
    route = {**ROUTE, "extra": {"store": True, "tools": [{"type": "web_search"}]}}
    assert await generation.generate_with_claude(PRIVATE, review_binding=(route, ACCOUNT)) == "null"
    request, = requests
    assert request.headers["chatgpt-account-id"] == ACCOUNT
    assert not {"session-id", "thread-id", "x-codex-turn-state"} & set(request.headers)
    body = json.loads(request.content)
    assert body["model"] == ROUTE["model"]
    assert body["input"] == [{"role": "user", "content": [{"type": "input_text", "text": PRIVATE}]}]
    assert body["store"] is False
    assert not {"tools", "previous_response_id", "prompt_cache_key", "metadata"} & body.keys()
    adapter.assert_not_called()
    usage.assert_not_called()
    quota.assert_not_called()
    assert not trace_dir.exists()
    assert PRIVATE not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [302, 401, 429, 503, None])
async def test_review_never_replays_or_falls_back(wire, status):
    _, requests, state = wire
    state["status"] = status
    state["transport_error"] = status is None
    with pytest.raises(httpx.ConnectError if status is None else RuntimeError):
        await generation.generate_with_claude(PRIVATE, review_binding=(ROUTE, ACCOUNT))
    assert len(requests) == 1
    assert requests[0].headers["chatgpt-account-id"] == ACCOUNT


@pytest.mark.asyncio
async def test_account_changed_after_prepare_sends_nothing(wire):
    row, requests, _ = wire
    row["codex_account_id"] = "test-account-b"
    with pytest.raises(RuntimeError, match="account is unavailable"):
        await generation.generate_with_claude(PRIVATE, review_binding=(ROUTE, ACCOUNT))
    assert not requests
