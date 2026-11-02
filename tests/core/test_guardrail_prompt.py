"""Provider safety checks, app side (``app.core.guardrail_prompt``).

Covers the pieces between fluid's ``ModelGuardrail`` prompt / ``ModelServed`` /
``GuardrailBlocked`` events and the frames the frontend reads
(docs/internal/guardrail-contract.md):

* the adapter turns ``ModelServed`` and ``GuardrailBlocked`` into system
  messages, and answers a safety-check prompt nobody can see with Stop;
* the offer rule (:func:`next_best_model`) and :func:`ask`'s answers;
* the chat turn's permission callback asks in every mode, Bypass and a profile
  allow-list included;
* the pump's ``model_info`` note carries the served model and a reroute leaves
  one ``model_rerouted`` card.
"""

import asyncio
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import pytest

import app.core.query_engine as qe
from app.core import guardrail_prompt as gp
from app.core.agent_types import PermissionResultAllow, PermissionResultDeny
from app.core.permission_handler import _DeferSentinel, permission_handler
from app.core.provider.events import AssistantMessage, SystemMessage, TextBlock
from app.core.provider.harness_adapter import HarnessAdapter
from app.core.provider.types import PermissionRequestEvent
from fluid.engine.guardrail import GUARDRAIL_TOOL, prompt_input
from fluid.events import AttemptDiscarded, GuardrailBlocked, ModelServed
from tests.core.engine_harness import StubPool, collect, make_state, stream_response
from tests.core.helpers import new_session
from tests.core.test_model_info_event import _engine_patches
from tests.core.test_query_engine_permission_flows import (
    SID,
    _allowed,
    _callback,
    _ctx,
    _next_prompt,
    _prompt_count,
)


@pytest.fixture(autouse=True)
def _clean_rules():
    permission_handler.clear_session_rules(SID)
    with patch("app.db.database.get_permission_rules", return_value=[]):
        yield
    permission_handler.clear_session_rules(SID)


def _prompt(**overrides):
    data = prompt_input(
        provider="anthropic", requested_model="claude-opus-5-5", category="cyber",
        explanation="This request looks like offensive security work.",
        suggested_model="claude-opus-4-8", request_id="req_1",
    )
    data.update(overrides)
    return data


# ─── adapter translation ────────────────────────────────────────────────────


def test_model_served_becomes_a_model_served_system_message():
    adapter = HarnessAdapter()
    [msg] = adapter._translate(ModelServed(
        session_id="s", turn_id="t", requested_model="gpt-5.5", served_model="gpt-5.2",
        provider="openai", rerouted=True, category="cyber",
    ))
    assert isinstance(msg, SystemMessage)
    assert msg.subtype == "model_served"
    assert msg.data == {
        "requested_model": "gpt-5.5", "served_model": "gpt-5.2", "provider": "openai",
        "rerouted": True, "reason": None, "category": "cyber",
    }
    # Later assistant messages carry the served id.
    assert adapter._current_model == "gpt-5.2"


def test_guardrail_blocked_becomes_a_persistable_note():
    [msg] = HarnessAdapter()._translate(GuardrailBlocked(
        session_id="s", turn_id="t", provider="anthropic", requested_model="claude-opus-5-5",
        outcome="stopped", category="cyber", suggested_model="claude-opus-4-8",
        reason="User denied permission",
    ))
    assert msg.subtype == "guardrail_blocked"
    assert msg.data["content"] == "Blocked by Anthropic's safety check (cyber). The turn stopped."
    assert msg.data["outcome"] == "stopped"
    assert msg.data["provider_label"] == "Anthropic"
    assert msg.data["reason"] == "User denied permission"
    assert msg.data["turn_id"] == "t"


def test_continued_note_names_the_retry_model():
    note = gp.guardrail_blocked_note(GuardrailBlocked(
        session_id="s", turn_id="t", provider="anthropic", requested_model="claude-opus-5-5",
        outcome="continued", category="bio", retry_model="claude-opus-5",
    ))
    assert note["content"] == (
        "Blocked by Anthropic's safety check (bio). Continued this turn on claude-opus-5."
    )


@pytest.mark.parametrize("row,label", [
    ({"provider_type": "custom", "name": "Fake AI (dev)"}, "Fake AI (dev)"),
    ({"provider_type": "chatgpt", "name": "My Codex"}, "OpenAI"),
    ({"provider_type": "openrouter", "name": "OR"}, "OpenRouter"),
    (None, None),
])
def test_provider_rows_name_the_company_or_the_custom_server(row, label):
    with patch.object(gp, "_provider_row", return_value=row):
        assert gp.account_label("prov-1") == label


def test_blocked_note_is_relabelled_for_a_custom_server():
    note = gp.guardrail_blocked_note(GuardrailBlocked(
        session_id="s", turn_id="t", provider="openrouter", requested_model="fake-ai",
        outcome="continued", category="content_filter", retry_model="fake-ai-safe",
    ))
    with patch.object(gp, "_provider_row", return_value={"provider_type": "custom", "name": "Lab"}):
        named = gp.relabel_blocked_note(note, "prov-1")
    assert named["provider_label"] == "Lab"
    assert named["content"] == (
        "Blocked by Lab's safety check (content filter). Continued this turn on fake-ai-safe."
    )
    assert gp.relabel_blocked_note(note, None) is note


async def test_safety_check_prompt_with_no_approver_is_answered_stop():
    adapter = HarnessAdapter()
    adapter.respond_to_permission = AsyncMock()
    await adapter._deny_guardrail_without_approver(PermissionRequestEvent(
        tool_use_id="guardrail-1", name=GUARDRAIL_TOOL, input=_prompt(), risk="safe",
    ))
    adapter.respond_to_permission.assert_awaited_once_with(
        "guardrail-1", False, message=gp.GUARDRAIL_NO_APPROVER_REASON,
    )


# ─── the offer rule ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("requested,expected", [
    ("opus", "claude-opus-4-8"),
    ("claude-opus-5-5", "claude-opus-5"),
    ("claude-fable-5", "claude-opus-5"),
    ("sonnet", None),  # claude-sonnet-5 is the end of its family's table
    ("claude-sonnet-5-5[1m]", "claude-sonnet-5"),
])
def test_claude_offer_is_one_step_down_in_the_family(requested, expected):
    assert gp.next_best_model(requested, provider_id=None, category=None) == expected


def test_reasoning_extraction_gets_no_offer():
    assert gp.next_best_model("opus", provider_id=None, category="reasoning_extraction") is None


@pytest.mark.parametrize("requested,expected", [
    ("gpt-5.5", "gpt-5.4"),
    ("openai/gpt-5.5", "gpt-5.4"),
    ("gpt-5.4", "gpt-5.5"),  # last on the row: the first other one
    ("unlisted", "gpt-5.5"),
])
def test_provider_offer_stays_on_the_same_row(requested, expected):
    with patch.object(gp, "_provider_models", return_value=["gpt-5.5", "gpt-5.4"]):
        assert gp.next_best_model(requested, provider_id="prov-1", category="cyber") == expected


def test_provider_row_with_one_model_offers_nothing():
    with patch.object(gp, "_provider_models", return_value=["gpt-5.5"]):
        assert gp.next_best_model("gpt-5.5", provider_id="prov-1", category=None) is None


# ─── ask() ─────────────────────────────────────────────────────────────────


async def _ask(answer, tool_input=None, provider_id=None):
    request = AsyncMock(return_value=answer)
    result = await gp.ask(
        tool_input if tool_input is not None else _prompt(),
        session_id="s1", profile_id="p", provider_id=provider_id,
        broadcast_func=AsyncMock(), tool_use_id="g-1", request_permission=request,
    )
    return result, request


async def test_ask_goes_through_the_one_shot_safety_queue():
    result, request = await _ask(PermissionResultAllow(updated_input=_prompt()))
    kwargs = request.await_args.kwargs
    assert kwargs["request_id"] == "guardrail-g-1"
    assert kwargs["tool_name"] == GUARDRAIL_TOOL
    assert kwargs["skip_saved_rules"] is True and kwargs["safety_check"] is True
    assert kwargs["tool_input"]["suggested_by"] == "provider"
    assert isinstance(result, PermissionResultAllow)
    assert result.updated_input["model"] == "claude-opus-4-8"


async def test_ask_allow_keeps_a_model_the_responder_picked():
    result, _ = await _ask(PermissionResultAllow(updated_input={**_prompt(), "model": "claude-opus-5"}))
    assert result.updated_input["model"] == "claude-opus-5"


async def test_ask_fills_the_app_offer_when_the_provider_named_none():
    result, request = await _ask(
        PermissionResultAllow(updated_input=None),
        tool_input=_prompt(category=None, suggested_model=None),
    )
    sent = request.await_args.kwargs["tool_input"]
    assert sent["suggested_model"] == "claude-opus-5"
    assert sent["suggested_by"] == "app"
    assert sent["message"].endswith("Continue this turn on claude-opus-5?")
    assert result.updated_input["model"] == "claude-opus-5"


async def test_ask_with_nothing_to_offer_stops_without_prompting():
    result, request = await _ask(
        None, tool_input=_prompt(category="reasoning_extraction", suggested_model=None),
    )
    request.assert_not_awaited()
    assert isinstance(result, PermissionResultDeny)
    assert result.message == gp.NO_MODEL_REASON


async def test_ask_defer_and_deny_stop():
    deferred, _ = await _ask(_DeferSentinel())
    assert isinstance(deferred, PermissionResultDeny)
    denied, _ = await _ask(PermissionResultDeny(message="User denied permission"))
    assert denied.message == "User denied permission"


# ─── the chat turn's permission callback ───────────────────────────────────


async def test_bypass_still_asks_and_allow_returns_the_retry_model(tmp_path):
    cb, broadcast, bounds = await _callback(
        tmp_path, overrides={"permission_mode": "bypassPermissions"},
        profile_config={"tool_selection_mode": "allow", "allowed_tools": ["Read"]},
    )
    with patch.object(qe, "sync_engine", new=bounds.sync_engine):
        call = asyncio.create_task(cb(GUARDRAIL_TOOL, _prompt(), _ctx("g-allow")))
        prompt = await _next_prompt(broadcast)
        assert prompt["tool_name"] == GUARDRAIL_TOOL
        assert prompt["request_id"] == "guardrail-g-allow"
        assert prompt["tool_input"]["suggested_model"] == "claude-opus-4-8"
        assert prompt.get("deferrable") is False
        await permission_handler.respond(prompt["request_id"], SID, "allow")
        result = await asyncio.wait_for(call, 5)
    assert _allowed(result)
    assert result.updated_input["model"] == "claude-opus-4-8"


def test_safety_check_prompt_cannot_be_deferred():
    from app.core.permission_handler import is_deferrable

    assert not is_deferrable(GUARDRAIL_TOOL)
    assert is_deferrable("Bash")


async def test_deny_answer_stops_and_is_never_remembered(tmp_path):
    cb, broadcast, bounds = await _callback(tmp_path)
    with patch.object(qe, "sync_engine", new=bounds.sync_engine):
        first = asyncio.create_task(cb(GUARDRAIL_TOOL, _prompt(), _ctx("g-1")))
        prompt = await _next_prompt(broadcast)
        await permission_handler.respond(prompt["request_id"], SID, "deny")
        assert not _allowed(await asyncio.wait_for(first, 5))
        # The next block asks again: no answer carries over.
        second = asyncio.create_task(cb(GUARDRAIL_TOOL, _prompt(), _ctx("g-2")))
        prompt = await _next_prompt(broadcast, seen=1)
        await permission_handler.respond(prompt["request_id"], SID, "deny")
        assert not _allowed(await asyncio.wait_for(second, 5))
    assert _prompt_count(broadcast) == 2


# ─── the pump: served model and the reroute card ───────────────────────────


async def _frames(messages, tmp_path, model="gpt-5.5", provider="prov-openai"):
    state = make_state()
    state.adapter.receive_events = stream_response(messages)
    with ExitStack() as stack:
        _engine_patches(stack, StubPool(state), str(tmp_path), model=model, provider=provider)
        return await collect(qe._stream_to_websocket_impl("hi", new_session(), "default"))


def _served(served, rerouted, requested="gpt-5.5"):
    return SystemMessage(subtype="model_served", data={
        "requested_model": requested, "served_model": served, "provider": "openai",
        "rerouted": rerouted, "reason": None, "category": None,
    })


def _asst(model, uuid_):
    return AssistantMessage(content=[TextBlock(text="hello")], model=model, uuid=uuid_)


def _system(events, subtype):
    return [e for e in events if e.get("type") == "system" and e.get("subtype") == subtype]


async def test_reroute_labels_model_info_and_leaves_one_card(mock_db, tmp_path):
    events = await _frames([
        SystemMessage(subtype="init", data={"session_id": "sdk-1"}),
        _served("gpt-5.2", True),
        _asst("gpt-5.2", "a-1"),
        _served("gpt-5.2", True),
        _asst("gpt-5.2", "a-2"),
    ], tmp_path)
    infos = _system(events, "model_info")
    assert [f["data"] for f in infos] == [{
        "model": "gpt-5.2", "requested_model": "gpt-5.5", "provider": "prov-openai",
        "served_model": "gpt-5.2", "rerouted": True,
    }]
    [card] = _system(events, "model_rerouted")
    assert card["data"]["content"] == "OpenAI answered with gpt-5.2 instead of gpt-5.5."
    assert card["data"]["served_model"] == "gpt-5.2"
    assert not _system(events, "model_served")  # internal, never forwarded


async def test_matching_served_model_leaves_no_card(mock_db, tmp_path):
    events = await _frames([
        SystemMessage(subtype="init", data={"session_id": "sdk-1"}),
        _served("gpt-5.5-2026-08-01", False),
        _asst("gpt-5.5-2026-08-01", "a-1"),
    ], tmp_path)
    [info] = _system(events, "model_info")
    assert info["data"]["rerouted"] is False
    assert not _system(events, "model_rerouted")


async def test_guardrail_blocked_note_is_forwarded(mock_db, tmp_path):
    note = gp.guardrail_blocked_note(GuardrailBlocked(
        session_id="s", turn_id="t", provider="anthropic", requested_model="opus",
        outcome="stopped", category="cyber",
    ))
    events = await _frames([
        SystemMessage(subtype="init", data={"session_id": "sdk-1"}),
        SystemMessage(subtype="guardrail_blocked", data=note),
    ], tmp_path, model="opus", provider=None)
    [card] = _system(events, "guardrail_blocked")
    assert card["data"]["content"] == "Blocked by Anthropic's safety check (cyber). The turn stopped."


def test_a_discarded_attempt_maps_to_a_persisted_note_without_a_card():
    [msg] = HarnessAdapter()._translate(
        AttemptDiscarded(session_id="s", turn_id="t", reason="guardrail"),
    )
    assert isinstance(msg, SystemMessage)
    assert msg.subtype == "attempt_discarded"
    assert msg.data == {"reason": "guardrail"}


async def test_attempt_discarded_note_is_forwarded(mock_db, tmp_path):
    events = await _frames([
        SystemMessage(subtype="init", data={"session_id": "sdk-1"}),
        _asst("opus", "a-1"),
        SystemMessage(subtype="attempt_discarded", data={"reason": "guardrail"}),
    ], tmp_path, model="opus", provider=None)
    [note] = _system(events, "attempt_discarded")
    assert note["data"] == {"reason": "guardrail"}
