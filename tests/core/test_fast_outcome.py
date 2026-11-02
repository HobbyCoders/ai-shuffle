"""Fast mode on the app layer: the toggle's answer and the chat's note (issue 991)."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fluid.events import FastModeOutcome

from app.api import sessions_permissions as api
from app.core.fast_outcome import fast_outcome_note
from app.core.provider.events import SystemMessage
from app.core.provider.harness_adapter import HarnessAdapter
from app.core.provider.types import SessionOptions
from app.core.session_pool import SessionState
from app.db import database as db


TURN = SimpleNamespace(turn_id="turn-1", turn={})


def _adapter(*, delegated: bool, waits: bool = False) -> HarnessAdapter:
    adapter = HarnessAdapter()
    adapter._options = SessionOptions(model="opus", effort="high", fast=False)
    adapter._sdk_options = SimpleNamespace(effort="high", aih_fast=False)
    if delegated:
        adapter._is_delegated = True
        adapter._engine = SimpleNamespace(
            set_run_settings=AsyncMock(), fast_waits_for_next_turn=lambda: waits, ctx=None)
    else:
        adapter._provider_kind = "openai_responses"
        adapter._options = SessionOptions(model="gpt-5.4", effort="high", fast=False)
        adapter._engine = SimpleNamespace(effort="high", request_extra={}, ctx=SimpleNamespace(extra={}))
    return adapter


@pytest.fixture
def live(monkeypatch):
    def install(adapter, *, streaming=True):
        state = SessionState(adapter=adapter, is_connected=True, is_streaming=streaming,
                             connect_options=adapter._sdk_options)
        monkeypatch.setattr("app.core.session_pool.get_session_pool",
                            lambda: SimpleNamespace(peek=lambda sid: state))
        monkeypatch.setattr("app.core.session_runtime.get_live_runtime",
                            lambda sid: TURN if streaming else None)
        monkeypatch.setattr(api, "_store_live_overrides", AsyncMock())
        monkeypatch.setattr(api.control, "_require_owned",
                            lambda sid: {"id": sid, "model": adapter._options.model, "effort": "high"})
        monkeypatch.setattr(db, "set_session_run_binding", MagicMock())
        monkeypatch.setattr(db, "list_worker_children", lambda sid: [])
        return state
    return install


@pytest.mark.asyncio
async def test_delegated_toggle_during_a_turn_answers_next_message(live):
    live(_adapter(delegated=True, waits=True))
    answer = await api.apply_run_settings("s", api.RunSettingsUpdate(fast=True), propagate=False)
    assert answer["fast"] is True
    assert answer["fast_applies_turn"] == "turn-1"


@pytest.mark.asyncio
async def test_delegated_toggle_between_turns_has_no_notice(live):
    live(_adapter(delegated=True, waits=False), streaming=False)
    answer = await api.apply_run_settings("s", api.RunSettingsUpdate(fast=True), propagate=False)
    assert "fast_applies_turn" not in answer


@pytest.mark.asyncio
async def test_native_lane_switches_inside_the_turn_and_has_no_notice(live):
    adapter = _adapter(delegated=False)
    live(adapter, streaming=True)
    answer = await api.apply_run_settings("s", api.RunSettingsUpdate(fast=True), propagate=False)
    assert "fast_applies_turn" not in answer
    assert adapter._engine.request_extra == {"service_tier": "priority"}


@pytest.mark.asyncio
async def test_no_live_adapter_has_no_notice(monkeypatch):
    monkeypatch.setattr("app.core.session_pool.get_session_pool",
                        lambda: SimpleNamespace(peek=lambda sid: None))
    monkeypatch.setattr("app.core.session_runtime.get_live_runtime", lambda sid: None)
    monkeypatch.setattr(api.control, "_require_owned", lambda sid: {"id": sid, "model": "opus", "effort": "high"})
    monkeypatch.setattr("app.core.provider.harness_adapter._decide_provider_kind", lambda options: "delegated")
    monkeypatch.setattr("app.core.model_effort.normalize_effort", lambda effort, *a, **k: effort)
    monkeypatch.setattr(db, "set_session_run_binding", MagicMock())
    answer = await api.apply_run_settings("s", api.RunSettingsUpdate(fast=True), propagate=False)
    assert answer == {"effort": "high", "fast": True}


@pytest.mark.asyncio
async def test_the_peer_action_carries_the_owner_workers_answer(live):
    live(_adapter(delegated=True, waits=True))
    answer = await api.handle_run_settings({"session_id": "s", "fast": True}, "other-worker")
    assert answer["fast_applies_turn"] == "turn-1"


@pytest.mark.asyncio
async def test_the_route_returns_a_remote_owners_answer_unchanged(monkeypatch):
    remote = {"effort": "high", "fast": True, "fast_applies_turn": "turn-1"}
    call = AsyncMock(return_value=remote)
    monkeypatch.setattr(api.control, "owner_of", lambda session: "worker-2")
    monkeypatch.setattr(api.control, "call", call)
    monkeypatch.setattr(api, "require_session", AsyncMock(return_value={"id": "s"}))
    monkeypatch.setattr(api, "_announce_context_window", AsyncMock())
    assert await api.update_run_settings(MagicMock(), "s", api.RunSettingsUpdate(fast=True), token="t") == remote
    call.assert_awaited_once_with("worker-2", "run-settings", {"session_id": "s", "fast": True})


@pytest.mark.asyncio
async def test_a_parents_answer_speaks_only_for_the_parent(live, monkeypatch):
    """An idle parent over a busy child: no notice at the parent's toggle, and
    the child's own answer (and its chat's note) carries the wait."""
    child_adapter = _adapter(delegated=True, waits=True)
    parent_adapter = _adapter(delegated=True, waits=False)
    states = {
        "parent": SessionState(adapter=parent_adapter, is_connected=True, is_streaming=False,
                               connect_options=parent_adapter._sdk_options),
        "child": SessionState(adapter=child_adapter, is_connected=True, is_streaming=True,
                              connect_options=child_adapter._sdk_options),
    }
    monkeypatch.setattr("app.core.session_pool.get_session_pool",
                        lambda: SimpleNamespace(peek=states.get))
    monkeypatch.setattr("app.core.session_runtime.get_live_runtime",
                        lambda sid: TURN if sid == "child" else None)
    monkeypatch.setattr(api, "_store_live_overrides", AsyncMock())
    monkeypatch.setattr(api.control, "_require_owned",
                        lambda sid: {"id": sid, "model": "opus", "effort": "high"})
    monkeypatch.setattr(api.control, "owner_of", lambda session: None)
    monkeypatch.setattr(api, "_inherited_fast_skip", lambda session: None)
    monkeypatch.setattr(db, "set_session_run_binding", MagicMock())
    monkeypatch.setattr(db, "list_worker_children",
                        lambda sid: [{"id": "child"}] if sid == "parent" else [])
    answer = await api.apply_run_settings("parent", api.RunSettingsUpdate(fast=True), propagate=False)
    assert "fast_applies_turn" not in answer
    await api.propagate_fast("parent", True)
    child_adapter._engine.set_run_settings.assert_awaited_once()
    child_answer = await api.apply_run_settings(
        "child", api.RunSettingsUpdate(fast=True), inherited=True, propagate=False)
    assert child_answer["fast_applies_turn"] == "turn-1"


# ── The wait, read again: reloads and other devices ────────────────────────


@pytest.mark.asyncio
async def test_a_toggle_succeeds_even_if_the_wait_cannot_be_read(live):
    adapter = _adapter(delegated=True)

    def broken():
        raise RuntimeError("no idea")

    adapter._engine.fast_waits_for_next_turn = broken
    live(adapter)
    answer = await api.apply_run_settings("s", api.RunSettingsUpdate(fast=True), propagate=False)
    assert answer["fast"] is True and "fast_applies_turn" not in answer


def test_an_adapter_that_cannot_say_has_nothing_to_report(live):
    state = live(_adapter(delegated=True, waits=True))
    state.adapter = SimpleNamespace()  # a provider without the query
    assert api.fast_waiting_turn("s") is None


def test_the_wait_names_the_running_turn(live):
    live(_adapter(delegated=True, waits=True))
    assert api.fast_waiting_turn("s") == "turn-1"


def test_no_running_turn_means_no_wait(live):
    live(_adapter(delegated=True, waits=True), streaming=False)
    assert api.fast_waiting_turn("s") is None


def test_the_native_lane_never_waits(live):
    live(_adapter(delegated=False))
    assert api.fast_waiting_turn("s") is None


@pytest.mark.asyncio
async def test_a_chat_on_another_worker_asks_its_owner(monkeypatch):
    call = AsyncMock(return_value={"fast_applies_turn": "turn-9"})
    monkeypatch.setattr(api.control, "owner_of", lambda session: "worker-2")
    monkeypatch.setattr(api.control, "call", call)
    assert await api.read_fast_waiting_turn({"id": "s"}, "s") == "turn-9"
    call.assert_awaited_once_with("worker-2", "fast-wait", {"session_id": "s"})
    assert api.control.known_action("fast-wait")


@pytest.mark.parametrize("failure", [TimeoutError(), RuntimeError("unknown action"), ConnectionError()])
@pytest.mark.asyncio
async def test_an_owner_that_cannot_answer_means_no_notice(monkeypatch, failure):
    monkeypatch.setattr(api.control, "owner_of", lambda session: "worker-2")
    monkeypatch.setattr(api.control, "call", AsyncMock(side_effect=failure))
    assert await api.read_fast_waiting_turn({"id": "s"}, "s") is None


@pytest.mark.asyncio
async def test_a_slow_owner_cannot_hold_the_chat_open(monkeypatch):
    async def never(*_args):
        await asyncio.Event().wait()

    monkeypatch.setattr(api, "_FAST_WAIT_READ_SECONDS", 0.01)
    monkeypatch.setattr(api.control, "owner_of", lambda session: "worker-2")
    monkeypatch.setattr(api.control, "call", never)
    assert await api.read_fast_waiting_turn({"id": "s"}, "s") is None


@pytest.mark.asyncio
async def test_the_peer_read_refuses_a_chat_this_worker_does_not_run(monkeypatch):
    def refuse(_sid):
        raise ValueError("not here")

    monkeypatch.setattr(api.control, "_require_owned", refuse)
    with pytest.raises(ValueError):
        await api.handle_fast_wait({"session_id": "s"}, "other-worker")


def test_the_engines_outcome_reaches_the_app_as_a_system_message():
    adapter = HarnessAdapter()
    [message] = adapter._translate(FastModeOutcome(
        session_id="s", turn_id="t", requested=True, requested_at_end=True, fast_calls=1,
        standard_calls=2, unknown_calls=3, state="cooldown", disabled_reason="brand_new_reason"))
    assert isinstance(message, SystemMessage)
    assert message.subtype == "fast_outcome"
    assert message.data == {
        "requested": True, "requested_at_end": True, "fast_calls": 1, "standard_calls": 2,
        "unknown_calls": 3, "state": "cooldown", "disabled_reason": "brand_new_reason",
    }


def _note(**data):
    return fast_outcome_note({"requested": True, "requested_at_end": True, **data})


def test_a_turn_that_ran_fast_says_so():
    note = _note(fast_calls=3, state="on")
    assert note["outcome"] == "fast"
    assert note["content"] == "Ran on Fast."


def test_a_requested_fast_turn_that_ran_standard_says_so_plainly():
    note = _note(standard_calls=2, state="off")
    assert note["outcome"] == "standard"
    assert note["content"] == "Ran at standard speed. Claude Code gave no reason."


def test_a_partly_fast_turn_counts_its_requests():
    note = _note(fast_calls=1, standard_calls=3, state="on")
    assert note["outcome"] == "partial"
    assert note["content"] == (
        "Ran on Fast for 1 of 4 requests; the other 3 ran at standard speed. Claude Code gave no reason.")


def test_a_request_with_no_speed_is_never_counted_as_either():
    assert _note(unknown_calls=2)["outcome"] == "unknown"
    assert _note(unknown_calls=2)["content"] == "Claude Code didn't report what speed this turn ran at."
    assert _note(fast_calls=1, unknown_calls=1)["content"].startswith(
        "Ran on Fast for 1 of 2 requests; 1 didn't report a speed")
    assert _note(fast_calls=1, unknown_calls=1)["outcome"] == "partial"
    assert _note(standard_calls=1, unknown_calls=1)["content"].startswith(
        "Ran at standard speed for 1 of 2 requests; 1 didn't report a speed")


def test_a_refused_fast_turn_toggled_off_claims_no_speed_it_never_ran():
    note = fast_outcome_note({"requested": True, "requested_at_end": False, "standard_calls": 2,
                              "state": "off", "disabled_reason": "extra_usage_disabled"})
    assert note["content"].startswith("Ran at standard speed.")
    assert note["content"].endswith("Fast mode was turned off during this turn, so it applies from your next message.")
    assert "stops" not in note["content"]


def test_turned_on_during_the_turn():
    note = fast_outcome_note({"requested": False, "requested_at_end": True, "standard_calls": 3, "state": "off"})
    assert note["content"] == (
        "Ran at standard speed. Fast mode was turned on during this turn, so it applies from your next message.")


def test_turned_off_during_the_turn():
    note = fast_outcome_note({"requested": True, "requested_at_end": False, "fast_calls": 2, "state": "on"})
    assert note["content"] == (
        "Ran on Fast. Fast mode was turned off during this turn, so it applies from your next message.")


def test_turned_on_midway_does_not_blame_a_reason_for_this_turn():
    note = fast_outcome_note({"requested": False, "requested_at_end": True, "standard_calls": 1,
                              "disabled_reason": "sdk_opt_in_required"})
    assert "enabled" not in note["content"]
    assert note["disabled_reason"] == "sdk_opt_in_required"


def test_fast_off_never_reports_a_refusal():
    note = fast_outcome_note({"requested": False, "requested_at_end": False, "fast_calls": 1,
                              "disabled_reason": "sdk_opt_in_required"})
    assert note["content"] == "Ran on Fast."


@pytest.mark.parametrize("reason, words", [
    ("extra_usage_disabled", "usage credits"),
    ("free", "paid plan"),
    ("preference", "organization"),
    ("network_error", "network problem"),
    ("not_first_party", "Anthropic API"),
    ("disabled_by_env", "environment"),
    ("model_not_allowed", "allowed models"),
    ("sdk_opt_in_required", "hasn't been enabled"),
    ("pending", "still checking"),
    ("unknown", "unavailable right now"),
])
def test_known_reasons_read_in_plain_words(reason, words):
    content = _note(standard_calls=1, disabled_reason=reason)["content"]
    assert content.startswith("Ran at standard speed. ")
    assert words in content
    assert reason not in content or reason == "unknown"


def test_an_unknown_reason_shows_verbatim():
    content = _note(standard_calls=1, disabled_reason="brand_new_reason")["content"]
    assert content == "Ran at standard speed. Claude Code's reason: brand_new_reason."


def test_cooldown_is_a_temporary_rate_limit():
    content = _note(fast_calls=1, standard_calls=1, state="cooldown", disabled_reason="unknown")["content"]
    assert "paused for now after a rate limit" in content


@pytest.mark.parametrize("bad", [None, "2", -1, True, 1.5])
def test_a_malformed_count_is_not_a_request(bad):
    assert fast_outcome_note({"fast_calls": bad, "standard_calls": bad})["outcome"] == "unknown"
