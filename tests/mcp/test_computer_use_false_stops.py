"""Regression tests: computer use must only halt on a real user action.

Owner report: agents controlling the Windows PC stopped and said the user had
stopped them while nobody touched the PC. Each test pins one path that used
to latch a session-wide halt (or hid the cause) without a real user action.
No real input is sent; the desktop is the fake from test_computer_use_tools.
"""

import asyncio
import json
import logging
import sys
from types import SimpleNamespace

import pytest

from app.mcp import computer_use_sdk as sdk
from app.mcp.computer_use import abort, activity, safety
from tests.mcp import test_computer_use_tools as _base

pytest.importorskip("PIL.Image")

# Shared fake desktop: displays, input devices and windows (no real input).
desktop = _base.desktop
payload = _base.payload

# Display 1 is 200x100 shown at 150x75 (scale 0.75): [40, 30] -> (53, 40).
TARGET = (53, 40)


def scripted_sleep(monkeypatch, desktop, positions):
    """Each SafeInput.sleep call sets the pointer to the next scripted value
    (a person or the OS moving it); after the script runs out, sleeps are no-ops."""
    script = list(positions)

    def sleep(self, seconds):
        self.check()
        if script:
            desktop.mouse.position = script.pop(0)
    monkeypatch.setattr(safety.SafeInput, "sleep", sleep)


# ----- pointer verification ---------------------------------------------------

async def test_pointer_jitter_within_tolerance_still_clicks(desktop, monkeypatch):
    await desktop.tools["screenshot"]({})
    scripted_sleep(monkeypatch, desktop, [(TARGET[0] + 1, TARGET[1] - 2)])
    result = await desktop.tools["left_click"]({"coordinate": [40, 30]})
    assert payload(result)["success"]
    desktop.mouse.press.assert_called_once_with("left")
    assert not abort.is_stopped("test")


async def test_pointer_off_target_once_is_replaced_and_clicks(desktop, monkeypatch, caplog):
    await desktop.tools["screenshot"]({})
    # Settle and the stability sample both read a stable wrong spot (e.g. a
    # DPI rounding or clamping discrepancy); the single re-place fixes it.
    scripted_sleep(monkeypatch, desktop, [(90, 90), (90, 90)])
    with caplog.at_level(logging.INFO, logger="app.mcp.computer_use_sdk"):
        result = await desktop.tools["left_click"]({"coordinate": [40, 30]})
    assert payload(result)["success"]
    desktop.mouse.press.assert_called_once()
    assert desktop.mouse.position == TARGET
    assert "re-placed" in caplog.text
    assert not abort.is_stopped("test")


async def test_pointer_mismatch_is_recoverable_and_logged_with_evidence(desktop, monkeypatch, caplog):
    await desktop.tools["screenshot"]({})
    scripted_sleep(monkeypatch, desktop, [(90, 90), (90, 90), (90, 90)])
    with caplog.at_level(logging.WARNING):
        result = await desktop.tools["left_click"]({"coordinate": [40, 30]})
    body = payload(result)
    assert body["code"] == "POINTER_MISMATCH" and body["outcome"] == "rejected"
    assert "Nothing was clicked" in body["error"] and "stopped" not in body["error"]
    desktop.mouse.press.assert_not_called()
    assert not abort.is_stopped("test")
    assert activity.snapshot()["stop_reason"] is None
    assert "expected=(53, 40)" in caplog.text and "after_retry=(90, 90)" in caplog.text
    # The very next action works: nothing was latched.
    scripted_sleep(monkeypatch, desktop, [])
    assert payload(await desktop.tools["left_click"]({"coordinate": [40, 30]}))["success"]


async def test_person_moving_mouse_rejects_action_and_pauses_input_without_halt(desktop, monkeypatch):
    monkeypatch.setattr(sdk, "_USER_MOUSE_COOLDOWN_S", 0.3)
    await desktop.tools["screenshot"]({})
    scripted_sleep(monkeypatch, desktop, [(90, 90), (120, 70)])
    result = await desktop.tools["left_click"]({"coordinate": [40, 30]})
    body = payload(result)
    assert body["code"] == "USER_INTERRUPTED" and "moved the mouse" in body["error"]
    desktop.mouse.press.assert_not_called()
    # Not re-placed while someone is using the mouse.
    assert desktop.mouse.position == (120, 70)
    assert not abort.is_stopped("test")
    # Input waits briefly instead of fighting the person; observing still works.
    paused = payload(await desktop.tools["type"]({"text": "x"}))
    assert paused["code"] == "USER_INTERRUPTED"
    desktop.keyboard.type.assert_not_called()
    assert payload(await desktop.tools["screenshot"]({}))["success"]
    await asyncio.sleep(0.35)
    assert payload(await desktop.tools["type"]({"text": "x"}))["success"]


async def test_unreadable_pointer_reports_locked_desktop(desktop, monkeypatch):
    await desktop.tools["screenshot"]({})
    scripted_sleep(monkeypatch, desktop, [None])
    body = payload(await desktop.tools["left_click"]({"coordinate": [40, 30]}))
    assert body["code"] == "DESKTOP_LOCKED"
    desktop.mouse.press.assert_not_called()
    assert not abort.is_stopped("test")


# ----- cancellation -----------------------------------------------------------

async def test_cancelled_call_does_not_halt_and_later_calls_work(desktop, caplog):
    started = asyncio.Event()
    loop = asyncio.get_running_loop()
    desktop.keyboard.press.side_effect = lambda key: loop.call_soon_threadsafe(started.set)
    running = asyncio.create_task(desktop.tools["hold_key"]({"text": "ctrl", "duration": 60}))
    await asyncio.wait_for(started.wait(), timeout=1)
    with caplog.at_level(logging.WARNING):
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
    assert "cancelled" in caplog.text and "stays armed" in caplog.text
    assert not abort.is_stopped("test")
    desktop.keyboard.press.side_effect = None
    # Wait for the orphaned worker thread to see the invalidation and exit.
    assert safety.DESKTOP_LOCK.acquire(timeout=1)
    safety.DESKTOP_LOCK.release()
    # The outcome of the cancelled call is unknown, so old coordinates are void.
    stale = payload(await desktop.tools["left_click"]({"coordinate": [40, 30]}))
    assert stale["success"] is False and "screenshot" in stale["error"]
    await desktop.tools["screenshot"]({})
    assert payload(await desktop.tools["left_click"]({"coordinate": [40, 30]}))["success"]


async def test_batch_longer_than_the_call_budget_is_refused_up_front(desktop):
    result = await desktop.tools["computer_batch"]({"actions": [
        {"action": "key", "text": "a"}] + [{"action": "wait", "duration": 60}] * 5})
    body = payload(result)
    assert body["code"] == "INVALID_ARGUMENT" and body["outcome"] == "rejected"
    desktop.keyboard.press.assert_not_called()


async def test_bridge_cancel_stops_the_action_and_frees_the_desktop(desktop):
    # Through the native bridge wrapper: a cancelled dispatch (steering, a
    # Stop) must not leave the action running after it was reported
    # cancelled, must release held keys and the desktop lock, and must not
    # halt the session.
    from app.core.provider.fluid_tool_bridge import detach_on_timeout

    started = asyncio.Event()
    loop = asyncio.get_running_loop()
    desktop.keyboard.press.side_effect = lambda key: loop.call_soon_threadsafe(started.set)
    call = detach_on_timeout("hold_key", desktop.tools["hold_key"], lambda: 30)
    running = asyncio.create_task(call({"text": "ctrl", "duration": 60}))
    await asyncio.wait_for(started.wait(), timeout=1)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    desktop.keyboard.press.side_effect = None
    assert safety.DESKTOP_LOCK.acquire(timeout=1)
    safety.DESKTOP_LOCK.release()
    desktop.keyboard.release.assert_called_with("ctrl")
    assert not abort.is_stopped("test")


async def test_bridge_timer_answers_unknown_and_keeps_the_desktop_busy(desktop):
    started = asyncio.Event()
    loop = asyncio.get_running_loop()
    desktop.keyboard.press.side_effect = lambda key: loop.call_soon_threadsafe(started.set)
    from app.core.provider.fluid_tool_bridge import detach_on_timeout

    call = detach_on_timeout("hold_key", desktop.tools["hold_key"], lambda: 0.2)
    body = payload(await call({"text": "ctrl", "duration": 0.6}))
    assert body["code"] == "EXECUTION_OUTCOME_UNKNOWN" and body["outcome"] == "unknown"
    # Still running: a second action is refused as busy, not performed.
    busy = payload(await desktop.tools["key"]({"text": "a"}))
    assert busy["code"] == "DESKTOP_BUSY"
    desktop.keyboard.press.side_effect = None
    assert safety.DESKTOP_LOCK.acquire(timeout=2)
    safety.DESKTOP_LOCK.release()
    desktop.keyboard.release.assert_called_with("ctrl")
    assert not abort.is_stopped("test")


# ----- every refusal names the true cause -------------------------------------

@pytest.mark.parametrize("reason,code,words", [
    ("escape", "USER_INTERRUPTED", "pressed Esc on the host computer"),
    ("user", "USER_INTERRUPTED", "pressed Stop computer use in the app"),
    ("stopped", "TURN_INTERRUPTED", "turn was stopped"),
    ("interrupt:user_stop", "USER_INTERRUPTED", "pressed Stop in the chat"),
    ("interrupt:server_shutdown", "TURN_INTERRUPTED", "the user did not stop it"),
    ("disabled", "PERMISSION_REQUIRED", "turned off in Settings"),
])
async def test_halt_refusal_states_the_cause(desktop, reason, code, words):
    await desktop.tools["screenshot"]({})
    abort.request_stop("test", reason=reason)
    for result in (await desktop.tools["left_click"]({"coordinate": [40, 30]}),
                   await desktop.tools["computer_batch"]({"actions": [{"action": "screenshot"}]}),
                   await desktop.tools["switch_display"]({"display": "1"}),
                   await desktop.tools["request_access"]({"apps": ["Notepad"], "reason": "x"})):
        body = payload(result)
        assert body["code"] == code and body["stop_reason"] == reason
        assert words in body["error"]


async def test_escape_mid_batch_reports_cause_and_completed_steps(desktop):
    def on_character(ch):
        if desktop.keyboard.type.call_count == 2:
            abort.stop_active("escape")
    desktop.keyboard.type.side_effect = on_character
    result = await desktop.tools["computer_batch"]({"actions": [
        {"action": "wait", "duration": 0}, {"action": "type", "text": "abcdef"},
        {"action": "key", "text": "Return"}]})
    body = json.loads(result["content"][-1]["text"])
    assert body["code"] == "USER_INTERRUPTED" and "Esc on the host" in body["error"]
    assert body["action_index"] == 1 and body["completed_actions"] == [0]
    assert body["not_run"] == [2] and body["outcome"] == "partial"


async def test_batch_failure_lists_which_steps_ran(desktop):
    result = await desktop.tools["computer_batch"]({"actions": [
        {"action": "key", "text": "a"}, {"action": "invalid"}, {"action": "type", "text": "no"}]})
    body = json.loads(result["content"][-1]["text"])
    assert body["completed_actions"] == [0] and body["not_run"] == [2]
    assert body["outcome"] == "partial"
    desktop.keyboard.type.assert_not_called()



async def test_batch_refusal_after_only_looking_stays_rejected(desktop):
    # A screenshot step sends no input, so a refusal after it changed nothing.
    result = await desktop.tools["computer_batch"]({"actions": [
        {"action": "screenshot"}, {"action": "left_click", "coordinate": [9000, 9000]},
        {"action": "type", "text": "no"}]})
    body = json.loads(result["content"][-1]["text"])
    assert body["completed_actions"] == [0] and body["not_run"] == [2]
    assert body["code"] == "INVALID_ARGUMENT" and body["outcome"] == "rejected"
    desktop.mouse.press.assert_not_called()


async def test_batch_keeps_an_unknown_outcome(desktop):
    def fail_after_sending(ch):
        raise OSError("input queue full")
    desktop.keyboard.type.side_effect = fail_after_sending
    result = await desktop.tools["computer_batch"]({"actions": [
        {"action": "key", "text": "a"}, {"action": "type", "text": "abc"}]})
    body = json.loads(result["content"][-1]["text"])
    assert body["completed_actions"] == [0]
    assert body["code"] == "EXECUTION_OUTCOME_UNKNOWN" and body["outcome"] == "unknown"

# ----- stop reasons -------------------------------------------------------------

def test_first_stop_reason_wins_and_resume_clears_it(monkeypatch):
    monkeypatch.setattr(abort, "_HALTED", {})
    monkeypatch.setattr(abort, "_REASONS", {})
    abort.request_stop("s", reason="escape", detail="flags=0x0")
    abort.request_stop("s", reason="completed")
    assert abort.stop_reason("s") == "escape"
    abort.resume("s")
    assert abort.stop_reason("s") is None and not abort.is_stopped("s")


def test_cancel_inflight_never_halts(monkeypatch):
    monkeypatch.setattr(abort, "_HALTED", {})
    monkeypatch.setattr(abort, "_GENERATIONS", {})
    before = abort.generation("s")
    abort.cancel_inflight("s", detail="test")
    assert abort.generation("s") == before + 1
    assert not abort.is_stopped("s") and abort.stop_reason("s") is None


# ----- physical Escape ----------------------------------------------------------

@pytest.fixture
def esc_state(monkeypatch):
    monkeypatch.setattr(abort, "_HALTED", {})
    monkeypatch.setattr(abort, "_REASONS", {})
    monkeypatch.setattr(abort, "_GENERATIONS", {})
    monkeypatch.setattr(activity, "_sessions", {})
    monkeypatch.setattr(safety, "_agent_escape_at", 0.0)
    monkeypatch.setitem(sys.modules, "pynput.keyboard",
                        SimpleNamespace(Key=SimpleNamespace(esc="esc")))
    activity.record(1, session_id="s")


def test_non_windows_listener_ignores_injected_escape(esc_state):
    safety._on_press("esc", injected=True)
    assert not abort.is_stopped("s")


def test_non_windows_listener_ignores_the_agents_own_escape_without_flag(esc_state):
    # pynput < 1.8 (and some backends) never report injected=True.
    inputs = safety.SafeInput("s", object(), SimpleNamespace(press=lambda k: None),
                              escape_key="esc")
    inputs.press(inputs.keyboard, "esc")
    safety._on_press("esc")
    assert not abort.is_stopped("s")


def test_macos_trusts_the_injected_flag_so_a_real_escape_is_never_swallowed(esc_state, monkeypatch):
    # pynput 1.8+ on macOS marks the agent's own keys injected, so an
    # uninjected Esc right after the agent's Escape is the person's.
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setitem(sys.modules, "pynput._info", SimpleNamespace(__version__=(1, 8, 2)))
    safety.note_agent_escape()
    safety._on_press("esc", injected=False)
    assert abort.stop_reason("s") == "escape"


def test_linux_keeps_the_window_because_its_flag_is_unreliable(esc_state, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setitem(sys.modules, "pynput._info", SimpleNamespace(__version__=(1, 8, 2)))
    safety.note_agent_escape()
    safety._on_press("esc", injected=False)
    assert not abort.is_stopped("s")


def test_non_windows_listener_stops_on_physical_escape(esc_state):
    safety._on_press("esc")
    assert abort.stop_reason("s") == "escape"


def test_windows_escape_records_evidence(esc_state):
    safety._win32_event_filter(0x100, SimpleNamespace(vkCode=0x1B, flags=0, scanCode=1))
    assert abort.stop_reason("s") == "escape"
    assert "flags=0x0" in abort._REASONS["s"]["detail"]


def test_windows_lower_il_injected_escape_is_ignored(esc_state):
    # LLKHF_INJECTED | LLKHF_LOWER_IL_INJECTED
    safety._win32_event_filter(0x100, SimpleNamespace(vkCode=0x1B, flags=0x12, scanCode=1))
    assert not abort.is_stopped("s")


async def test_agent_escape_key_marks_its_own_escape(desktop, monkeypatch):
    monkeypatch.setattr(safety, "_agent_escape_at", 0.0)
    assert payload(await desktop.tools["key"]({"text": "Escape"}))["success"]
    assert safety._recent_agent_escape()


# ----- turn interrupts name their source ----------------------------------------

@pytest.mark.parametrize("source,code,words", [
    ("user_stop", "USER_INTERRUPTED", "the user pressed Stop in the chat"),
    ("server_shutdown", "TURN_INTERRUPTED", "the user did not stop it"),
    ("usage_limit", "TURN_INTERRUPTED", "the app interrupted this turn (usage_limit)"),
])
async def test_turn_interrupt_reason_names_its_source(desktop, source, code, words):
    abort.request_stop("test", reason=f"interrupt:{source}")
    body = payload(await desktop.tools["screenshot"]({}))
    assert body["code"] == code and words in body["error"]
