"""Subscription loop guard (plan item C11)."""

from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core import claude_auth_mode, loop_guard, provider_agent, session_runtime
from app.db import database
from app.db import turns as turns_db


@pytest.fixture(autouse=True)
def _fresh_counts():
    loop_guard.reset_counts()
    yield
    loop_guard.reset_counts()


def _subscription():
    claude_auth_mode.set_mode("subscription")


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------

def test_defaults_are_documented_values(mock_db):
    cfg = loop_guard.config()
    assert (cfg.enabled, cfg.session_turns_per_hour, cfg.install_turns_per_hour, cfg.worker_max_minutes) == (
        True, 12, 60, 60)


def test_facts_the_settings_explainer_states(mock_db):
    """LoopGuardPanel's explainer repeats these; change both together."""
    _subscription()
    assert loop_guard.UNATTENDED_SOURCES == ("automation", "wakeup", "provider_agent", "connector")
    loop_guard.update_config(session_turns_per_hour=1)
    # A confirmed turn starts past the budget and still counts toward the hour.
    assert loop_guard.check("s", "automation", "delegated", confirmed=True, now=1.0) is None
    assert loop_guard.usage("s", now=1.0) == {"install_last_hour": 1, "session_last_hour": 1}
    # Turns a person started and turns on another provider's leg are never counted.
    assert loop_guard.check("s", "chat", "delegated", now=2.0) is None
    assert loop_guard.check("s", "automation", "openai_responses", now=2.0) is None
    assert loop_guard.usage("s", now=2.0) == {"install_last_hour": 1, "session_last_hour": 1}


def test_session_budget_holds_the_next_unattended_turn(mock_db):
    _subscription()
    loop_guard.update_config(session_turns_per_hour=3)
    for _ in range(3):
        assert loop_guard.check("s1", "automation", "delegated", now=1000.0) is None
    reason = loop_guard.check("s1", "wakeup", "delegated", now=1001.0)
    assert reason and "waiting for your confirmation" in reason and "budget 3" in reason
    # Another session still has room; a confirmed turn always starts.
    assert loop_guard.check("s2", "automation", "delegated", now=1001.0) is None
    assert loop_guard.check("s1", "wakeup", "delegated", confirmed=True, now=1002.0) is None
    # The window rolls: an hour later the session has room again.
    assert loop_guard.check("s1", "wakeup", "delegated", now=1000.0 + 3600.5) is None


def test_install_budget_counts_every_session(mock_db):
    _subscription()
    loop_guard.update_config(install_turns_per_hour=2)
    assert loop_guard.check("a", "connector", "delegated", now=10.0) is None
    assert loop_guard.check("b", "provider_agent", "delegated", now=11.0) is None
    reason = loop_guard.check("c", "automation", "delegated", now=12.0)
    assert reason and "This worker already ran 2" in reason


@pytest.mark.parametrize("source", ["user", "queue", "resume", "api", "worktree", "unpark"])
def test_attended_sources_are_never_counted(mock_db, source):
    _subscription()
    loop_guard.update_config(session_turns_per_hour=1, install_turns_per_hour=1)
    for _ in range(3):
        assert loop_guard.check("s", source, "delegated") is None
    assert loop_guard.usage("s") == {"install_last_hour": 0, "session_last_hour": 0}


def test_api_key_mode_disabled_guard_and_provider_legs_are_exempt(mock_db):
    loop_guard.update_config(session_turns_per_hour=1)
    claude_auth_mode.set_mode("api_key")
    for _ in range(3):
        assert loop_guard.check("s", "automation", "delegated") is None
    _subscription()
    for _ in range(3):
        assert loop_guard.check("p", "automation", "openai_responses") is None
    loop_guard.update_config(enabled=False)
    for _ in range(3):
        assert loop_guard.check("s", "automation", "delegated") is None


def test_update_config_validates(mock_db):
    with pytest.raises(ValueError):
        loop_guard.update_config(session_turns_per_hour=0)
    with pytest.raises(ValueError):
        loop_guard.update_config(bogus=1)


# ---------------------------------------------------------------------------
# Worker wall-clock cap
# ---------------------------------------------------------------------------

def test_background_claude_worker_gets_the_default_cap_in_subscription_mode(mock_db):
    _subscription()
    assert provider_agent._worker_deadlines(True, None, None)[1] == 3600.0
    # A worker on another provider, or in API key mode, keeps the lane default.
    assert provider_agent._worker_deadlines(True, None, None, "prov-1")[1] == 0.0
    claude_auth_mode.set_mode("api_key")
    assert provider_agent._worker_deadlines(True, None, None)[1] == 0.0


def test_explicit_worker_cap_wins(mock_db):
    _subscription()
    assert provider_agent._worker_deadlines(True, None, 120.0)[1] == 120.0
    loop_guard.update_config(worker_max_minutes=0)
    assert provider_agent._worker_deadlines(True, None, None)[1] == 0.0


# ---------------------------------------------------------------------------
# Guidance
# ---------------------------------------------------------------------------

def test_scheduled_run_guidance_only_in_subscription_mode_for_claude_profiles(mock_db):
    _subscription()
    assert loop_guard.scheduled_run_guidance({"config": {}}) == loop_guard.SCHEDULED_RUN_GUIDANCE
    assert "use an API key for scheduled runs" in loop_guard.SCHEDULED_RUN_GUIDANCE
    assert loop_guard.scheduled_run_guidance({"config": {"provider": "prov-1"}}) is None
    claude_auth_mode.set_mode("api_key")
    assert loop_guard.scheduled_run_guidance({"config": {}}) is None


# ---------------------------------------------------------------------------
# Held turns and the confirm step
# ---------------------------------------------------------------------------

async def test_confirm_resubmits_the_held_turn_past_the_budget(mock_db):
    held = loop_guard.hold({
        "id": "t1", "session_id": "s1", "source": "automation", "prompt": "check the build",
        "profile_id": "p", "project_id": None, "overrides": {"max_turns": 1}, "payload": None,
    }, "Loop guard: waiting")
    assert [h["id"] for h in loop_guard.list_held()] == [held["id"]]
    with patch("app.core.turn_manager.submit_async", AsyncMock(return_value="t2")) as submit:
        assert await loop_guard.confirm_held(held["id"]) == "t2"
    submit.assert_awaited_once_with(
        "s1", "automation", prompt="check the build", profile_id="p", project_id=None,
        overrides={"max_turns": 1}, payload={loop_guard.CONFIRMED_FLAG: True},
    )
    assert loop_guard.list_held() == []
    assert await loop_guard.confirm_held(held["id"]) is None


def _pump_env(stack, recorder):
    registry = MagicMock(name="task_registry")
    registry.is_user_stopped.return_value = False
    registry.stop_generation.return_value = 0
    registry.stop_invalidated.return_value = False
    registry.has_unresolved_background_launches.return_value = False
    registry.has_active_background_work.return_value = False
    registry.get_active_background_work.return_value = []
    registry.get_background_launches.return_value = set()
    session_runtime.register_broadcaster(AsyncMock(name="broadcaster"))
    session_runtime.register_scheduler(MagicMock(name="on_turn_settled"))
    stack.enter_context(patch.object(
        session_runtime.TurnRecorder, "create_for_run", new=AsyncMock(return_value=recorder)))
    stack.enter_context(patch("app.core.session_runtime.record_parked", new=AsyncMock()))
    stack.enter_context(patch("app.core.session_runtime.start_idle_reader", new=MagicMock()))
    stack.enter_context(patch("app.core.session_runtime.stop_idle_reader", new=AsyncMock()))
    stack.enter_context(patch("app.core.task_registry.task_registry", new=registry))
    stack.enter_context(patch("app.core.turn_finalize.persist_turn_end_state", new=AsyncMock()))


class _Spawned(Exception):
    pass


async def _run_turn(tmp_path, sid, source, payload=None):
    recorder = MagicMock(name="recorder")
    recorder.start = AsyncMock()
    recorder.on_event = AsyncMock()
    recorder.close = AsyncMock()
    turn = turns_db.enqueue_turn(sid, source, prompt="loop", profile_id="guard-profile", payload=payload)

    def _spawn(*_a, **_k):
        raise _Spawned()

    with ExitStack() as stack:
        _pump_env(stack, recorder)
        stack.enter_context(patch("app.core.provider.factory.make_adapter", new=_spawn))
        await session_runtime.execute(turn)
    session_runtime._live_runtimes.clear()
    session_runtime.register_scheduler(None)
    session_runtime.register_broadcaster(None)
    return [c.args[0] for c in recorder.on_event.await_args_list]


async def test_turn_start_holds_an_automation_past_its_budget(mock_db, tmp_path):
    _subscription()
    loop_guard.update_config(session_turns_per_hour=1)
    database.create_profile("guard-profile", "Guard", None, {"cwd": str(tmp_path)})
    database.create_session(session_id="guard-s", profile_id="guard-profile", title="t")

    # The first unattended turn passes the guard and reaches the spawn seam.
    first = await _run_turn(tmp_path, "guard-s", "automation")
    assert not any(f.get("code") == loop_guard.HOLD_CODE for f in first)

    # The second waits for confirmation and is kept for the owner.
    second = await _run_turn(tmp_path, "guard-s", "automation")
    assert [f.get("code") for f in second] == [loop_guard.HOLD_CODE]
    held = loop_guard.list_held()
    assert len(held) == 1 and held[0]["source"] == "automation" and held[0]["prompt"] == "loop"

    # A person's own message is never held.
    mine = await _run_turn(tmp_path, "guard-s", "user")
    assert not any(f.get("code") == loop_guard.HOLD_CODE for f in mine)

    # The confirmed re-run passes the guard.
    confirmed = await _run_turn(tmp_path, "guard-s", "automation", payload={loop_guard.CONFIRMED_FLAG: True})
    assert not any(f.get("code") == loop_guard.HOLD_CODE for f in confirmed)
