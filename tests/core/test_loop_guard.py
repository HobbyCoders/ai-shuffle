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
    assert loop_guard.check("b", "provider_agent", "delegated", origin="automation", now=11.0) is None
    reason = loop_guard.check("c", "automation", "delegated", now=12.0)
    assert reason and "This worker already ran 2" in reason


@pytest.mark.parametrize("origin", ["user", "queue", "resume", "api", "worktree", None])
def test_a_worker_a_person_started_is_never_counted_or_held(mock_db, origin):
    """A chat's subagents are the owner's own work, however many there are."""
    _subscription()
    loop_guard.update_config(session_turns_per_hour=1, install_turns_per_hour=1)
    for _ in range(3):
        assert loop_guard.check("w", "provider_agent", "delegated", origin=origin) is None
    assert loop_guard.usage("w") == {"install_last_hour": 0, "session_last_hour": 0}


@pytest.mark.parametrize("origin", ["automation", "wakeup", "connector"])
def test_a_worker_unattended_work_started_is_counted_and_held(mock_db, origin):
    _subscription()
    loop_guard.update_config(session_turns_per_hour=1)
    assert loop_guard.check("w", "provider_agent", "delegated", origin=origin) is None
    assert loop_guard.usage("w")["session_last_hour"] == 1
    reason = loop_guard.check("w", "provider_agent", "delegated", origin=origin)
    assert reason and "background worker turn is waiting" in reason


def test_budgets_read_the_current_setting_on_every_check(mock_db):
    _subscription()
    loop_guard.update_config(session_turns_per_hour=2)
    assert loop_guard.check("s", "automation", "delegated", now=1.0) is None
    loop_guard.update_config(session_turns_per_hour=1)  # lowered mid-run
    assert loop_guard.check("s", "automation", "delegated", now=2.0)
    loop_guard.update_config(session_turns_per_hour=5)  # raised mid-run
    assert loop_guard.check("s", "automation", "delegated", now=3.0) is None
    loop_guard.update_config(session_turns_per_hour=1)
    loop_guard.update_config(enabled=False)  # switched off mid-run
    assert loop_guard.check("s", "automation", "delegated", now=4.0) is None


# ---------------------------------------------------------------------------
# Origin: what started a worker's chain of work
# ---------------------------------------------------------------------------

def _turn(sid, source, parent=None, payload=None, running=False):
    row = turns_db.enqueue_turn(sid, source, prompt="x", parent_turn_id=parent, payload=payload)
    if running:
        assert turns_db.start_turn(row["id"], "runner-1") is not None
    return row


def test_origin_follows_wakes_retries_and_peer_messages(mock_db):
    database.create_profile("op", "Origin", None, {})
    database.create_session(session_id="chat", profile_id="op", title="t")
    user = _turn("chat", "user")
    wake = _turn("chat", "unpark", parent=user["id"])
    retry = _turn("chat", "failover", parent=wake["id"])
    assert loop_guard.turn_origin(retry) == "user"
    assert loop_guard.turn_origin(_turn("chat", "agent_message", parent=retry["id"])) == "user"
    assert loop_guard.turn_origin(_turn("chat", "auto_continue", parent=_turn("chat", "wakeup")["id"])) == "wakeup"
    # A worker's turn answers with what was stamped on it at dispatch.
    stamped = {loop_guard.ORIGIN_KEY: "automation"}
    assert loop_guard.turn_origin(_turn("chat", "provider_agent", payload=stamped)) == "automation"
    # Unknown: a relay with nothing behind it, or an unstamped worker turn.
    assert loop_guard.turn_origin(_turn("chat", "agent_message")) is None
    assert loop_guard.turn_origin(_turn("chat", "provider_agent")) is None


def test_session_origin_reads_the_turn_calling_task(mock_db):
    database.create_profile("op", "Origin", None, {})
    database.create_session(session_id="auto", profile_id="op", title="t")
    _turn("auto", "automation", running=True)
    # A later queued person message does not change what the running turn is.
    _turn("auto", "queue")
    assert loop_guard.session_origin("auto") == "automation"
    assert loop_guard.session_origin(None) is None
    assert loop_guard.session_origin("no-such-session") is None


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


def test_off_mode_leaves_the_guard_inactive(mock_db):
    loop_guard.update_config(session_turns_per_hour=1, install_turns_per_hour=1)
    claude_auth_mode.set_mode("off")
    assert loop_guard.applies() is False
    for _ in range(3):
        assert loop_guard.check("s", "automation", "delegated") is None
    assert loop_guard.usage("s") == {"install_last_hour": 0, "session_last_hour": 0}


def test_update_config_validates(mock_db):
    with pytest.raises(ValueError):
        loop_guard.update_config(session_turns_per_hour=0)
    with pytest.raises(ValueError):
        loop_guard.update_config(bogus=1)


# ---------------------------------------------------------------------------
# Worker wall-clock cap
# ---------------------------------------------------------------------------

def test_unattended_claude_worker_gets_the_default_cap_in_subscription_mode(mock_db):
    _subscription()
    assert provider_agent._worker_deadlines(True, None, None, None, "automation")[1:] == (3600.0, True)
    # A worker on another provider keeps the lane default.
    assert provider_agent._worker_deadlines(True, None, None, "prov-1", "automation")[1:] == (0.0, False)
    # In API key mode it is still the guard's to time, but the guard is off.
    claude_auth_mode.set_mode("api_key")
    assert provider_agent._worker_deadlines(True, None, None, None, "wakeup")[1:] == (0.0, True)


@pytest.mark.parametrize("origin", ["user", "queue", "resume", "api", "worktree", None])
def test_a_worker_a_person_started_gets_no_loop_guard_cap(mock_db, origin):
    _subscription()
    assert provider_agent._worker_deadlines(True, None, None, None, origin)[1:] == (0.0, False)
    # The synchronous lane's own cap is unchanged.
    sync = provider_agent._worker_deadlines(False, None, None, None, origin)
    assert sync[1] > 0 and sync[2] is False


def test_explicit_worker_cap_wins(mock_db):
    _subscription()
    assert provider_agent._worker_deadlines(True, None, 120.0, None, "automation")[1:] == (120.0, False)
    loop_guard.update_config(worker_max_minutes=0)
    assert provider_agent._worker_deadlines(True, None, None, None, "automation")[1] == 0.0


def test_the_cap_failure_names_the_loop_guard_and_its_setting(mock_db):
    text = loop_guard.worker_cap_error("w-1", 3600.0, "automation")
    assert "per-worker time limit (60 minutes)" in text
    assert "an automation" in text
    assert "Settings > Providers > Claude > Policy" in text
    assert "w-1" in text
    assert "synchronous" not in text and "Re-dispatch" not in text
    cap = provider_agent._WallCap(0.0, 3600.0, origin="wakeup", loop_guarded=True)
    assert cap.error("w-1") == loop_guard.worker_cap_error("w-1", 3600.0, "wakeup")
    # The synchronous lane keeps its own text.
    assert "synchronous Task" in provider_agent._WallCap(0.0, 60.0).error("w-1")


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
