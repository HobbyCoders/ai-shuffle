"""Monitor completion summaries — round-3 audit items R8 and R16.

The app overrides fluid's Monitor supervision (ruling A13), so fluid's
supervisor-side ANSI strip never runs for these buffers and the app owns what
the parent is woken with. Two audited failures come from that:

* R8 — colour codes reached the model verbatim; a baseline session wrote an
  ANSI-naive regex against them and paid for a full 34s vitest re-run.
* R16 — 8 of 9 monitor notifications said "(no output; exit code 0)" because
  the monitored command redirected everything into a log file.
"""

import os

import pytest

from app.core import monitor_spawn as ms


# ---------------------------------------------------------------------------
# R8 — no escape sequence survives into a summary
# ---------------------------------------------------------------------------

def test_summarize_strips_ansi():
    lines = [
        "\x1b[32mPASS\x1b[0m src/a.test.ts",
        "\x1b[31mFAIL\x1b[0m src/b.test.ts",
    ]

    summary = ms._summarize(lines)

    assert "\x1b[" not in summary
    assert summary == "PASS src/a.test.ts\nFAIL src/b.test.ts"


def test_summarize_strips_ansi_before_slicing():
    """Scrub first, THEN bound: counting escape bytes against the head/tail
    budget evicts real output the parent needed."""
    payload = "\x1b[36m" + ("x" * 40_000) + "\x1b[0m"

    summary = ms._summarize([payload])

    assert "\x1b[" not in summary
    assert "chars truncated" in summary


def test_summarize_handles_osc_and_two_byte_escapes():
    """Beyond plain colour CSI: OSC title strings (the sequence a package
    manager writes to the terminal title) and the lone two-byte escapes."""
    summary = ms._summarize(["\x1b]0;building\x07done\x1bM"])

    assert "\x1b" not in summary
    assert summary == "done"


@pytest.mark.asyncio
async def test_spawn_background_buffers_clean_lines(monkeypatch):
    """The scrub happens at the SOURCE, so TaskOutput/TaskGet and the summary
    all see clean text."""
    import asyncio

    from app.mcp import builtin_tools as bt

    chunks = [b"\x1b[32mok\x1b[0m\n", b"\x1b[1mbold\x1b[22m done\n", b""]

    class _Stdout:
        async def readline(self):
            return chunks.pop(0)

    class _Proc:
        pid = 4242
        stdout = _Stdout()
        returncode = 0

        async def wait(self):
            return 0

    async def _fake_exec(*args, **kwargs):
        return _Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
    task = await bt._spawn_background("npm test", None, None)
    await task._reader_task

    assert task.lines == ["ok", "bold done"]
    assert not any("\x1b[" in line for line in task.lines)
    bt._background_tasks.pop(task.task_id, None)


# ---------------------------------------------------------------------------
# R16 — a redirected command's log tail, instead of "(no output)"
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "command, expected",
    [
        ("npm test > build.log 2>&1", ["build.log"]),
        ("make all >>build.log", ["build.log"]),
        ("pytest 2> errors.log", ["errors.log"]),
        ("npm test | tee -a run.log", ["run.log"]),
        ('cmd > "my logs/out.log"', ["my logs/out.log"]),
        ("noisy > /dev/null", []),
        ("plain command with no redirect", []),
        ("a > one.log && b > two.log", ["one.log", "two.log"]),
    ],
)
def test_redirect_targets(command, expected):
    assert ms._redirect_targets(command) == expected


def test_log_tail_reads_the_commands_own_sink(tmp_path):
    log = tmp_path / "build.log"
    log.write_text("compiling\n\x1b[31merror TS2304\x1b[0m\n", encoding="utf-8")

    tail = ms._log_tail("npm run build > build.log 2>&1", str(tmp_path))

    assert "error TS2304" in tail
    assert "\x1b[" not in tail
    assert "build.log" in tail


def test_log_tail_is_bounded(tmp_path):
    log = tmp_path / "big.log"
    log.write_text("y" * (ms._LOG_TAIL_BYTES * 4), encoding="utf-8")

    tail = ms._log_tail("run > big.log", str(tmp_path))

    assert len(tail) < ms._LOG_TAIL_BYTES * 2
    assert "earlier lines omitted" in tail


def test_log_tail_is_empty_when_there_is_nothing_to_read(tmp_path):
    assert ms._log_tail("run > missing.log", str(tmp_path)) == ""
    assert ms._log_tail("run", str(tmp_path)) == ""
    (tmp_path / "empty.log").write_text("", encoding="utf-8")
    assert ms._log_tail("run > empty.log", str(tmp_path)) == ""


def test_log_tail_accepts_an_absolute_target(tmp_path):
    log = tmp_path / "abs.log"
    log.write_text("finished\n", encoding="utf-8")

    tail = ms._log_tail(f"run > {log}", os.getcwd())

    assert "finished" in tail


# ---------------------------------------------------------------------------
# R6 — the receipt commands the park
# ---------------------------------------------------------------------------

def test_receipt_text_commands_the_park():
    text = ms.monitor_receipt_text("t-9")

    assert text.startswith("Monitoring started (task t-9).")
    assert "End the turn now" in text
    assert "woken when it comes to rest" in text


# ---------------------------------------------------------------------------
# Round-4 audit: a time bound, an honest reap, a bounded wake summary
# ---------------------------------------------------------------------------

def test_receipt_restores_the_do_not_poll_clause():
    # The app's copy had dropped fluid's final clause and the same sessions
    # went on to sleep-poll with wait+curl after every spawn.
    assert "Do not sleep, poll, or check on it yourself" in ms.monitor_receipt_text("t-1")


def test_serving_receipt_says_the_session_is_not_parked():
    text = ms.monitor_serving_receipt_text("t-2")

    assert "Serving task t-2" in text
    assert "NOT parked on it" in text
    assert "Do not sleep, poll" in text


@pytest.mark.parametrize("command", [
    "timeout 900 bash -c 'gh pr checks 12'",
    "curl --max-time 30 http://localhost:8010/",
    "deadline=$(($(date +%s)+600)); while true; do sleep 5; done",
    "for i in $(seq 1 60); do sleep 10; done",
    "for ((i=0; i<60; i++)); do sleep 10; done",
    "i=0; while [ $i -lt 60 ]; do i=$((i+1)); sleep 10; done",
])
def test_commands_that_carry_their_own_bound_are_left_alone(command):
    assert ms.command_has_own_time_bound(command)
    assert ms.monitor_deadline_seconds(command, serves=False) == 0


@pytest.mark.parametrize("command", [
    "while true; do sleep 30; done",
    "npm run build",
    "pytest -q",
])
def test_unbounded_commands_get_the_default_budget(command, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "monitor_command_timeout_seconds", 1200)
    assert not ms.command_has_own_time_bound(command)
    assert ms.monitor_deadline_seconds(command, serves=False) == 1200


def test_serves_and_the_kill_switch_both_disable_the_budget(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "monitor_command_timeout_seconds", 1200)
    assert ms.monitor_deadline_seconds("npm run dev", serves=True) == 0
    monkeypatch.setattr(settings, "monitor_command_timeout_seconds", 0)
    assert ms.monitor_deadline_seconds("npm run dev", serves=False) == 0


def _settle(**kwargs):
    base = dict(
        exit_code=0, lines=[], command="run", cwd=None,
        session_id=None, task_id="t", serves=False,
    )
    base.update(kwargs)
    return ms.settle_monitor_task(**base)


def test_clean_exit_and_failure_are_unchanged():
    assert _settle(exit_code=0, lines=["all good"]) == ("completed", "all good")
    status, summary = _settle(exit_code=1, lines=["boom"])
    assert status == "error" and summary == "boom"


def test_a_reaped_shell_is_a_cancellation_not_a_failure():
    # The audited regression: SIGKILL leaves no exit code, the old code
    # derived "error" from it, and the model read "(no output; exit code
    # None)" as a crash and re-checked the work by hand.
    status, summary = _settle(exit_code=None)

    assert status == "killed"
    assert "no output; exit code None" not in summary
    assert "not by a crash" in summary
    assert "do not re-run" in summary.lower()


def test_the_recorded_status_beats_the_exit_code(monkeypatch):
    from app.core import task_registry as tr

    tr.task_registry.start_task("s-reap", "t-reap", "shell", "npm run dev")
    tr.task_registry.complete_task(
        "s-reap", "t-reap", "killed",
        summary=tr.USER_STOP_SUMMARY, reason=tr.USER_STOP_REASON,
    )
    # A terminate()d process on Windows exits 1 — indistinguishable from a
    # genuine failure by exit code alone.
    status, summary = _settle(
        exit_code=1, session_id="s-reap", task_id="t-reap",
        lines=["Traceback (most recent call last):", "…12KB of frames…"],
    )

    assert status == "killed"
    assert "Stopped by the user" in summary
    # The traceback tail is REPLACED by the attribution, not appended to it.
    assert "Traceback" not in summary
    assert len(summary) < 400
    tr.task_registry.clear_session("s-reap")


def test_a_timed_out_task_reads_as_a_timeout(monkeypatch):
    from app.core import task_registry as tr

    tr.task_registry.start_task("s-to", "t-to", "shell", "watch ci")
    tr.task_registry.complete_task(
        "s-to", "t-to", "killed",
        summary="Stopped: no result after 1200s, the monitor's time budget.",
        reason=ms.MONITOR_TIMEOUT_REASON,
    )
    status, summary = _settle(exit_code=None, session_id="s-to", task_id="t-to")

    assert status == "killed"
    assert "1200s" in summary and "time budget" in summary
    tr.task_registry.clear_session("s-to")


def test_a_signalled_process_is_a_stop_even_with_no_registry_record():
    status, summary = _settle(exit_code=-15, lines=["Traceback ..."])

    assert status == "killed"
    assert "Traceback" not in summary


def test_a_serving_process_that_exits_cleanly_is_not_an_error():
    status, summary = _settle(exit_code=0, serves=True, lines=["bye"])

    assert status == "completed"
    assert "exited cleanly on its own" in summary


def test_a_serving_process_stop_names_the_service():
    status, summary = _settle(exit_code=-15, serves=True, lines=["stack…"])

    assert status == "killed"
    assert "service is no longer running" in summary


def test_wake_summary_is_capped_at_about_a_kilobyte(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "monitor_summary_max_chars", 1000)
    status, summary = _settle(exit_code=1, lines=["x" * 50_000])

    assert status == "error"
    assert len(summary) < 1200
    assert "TaskOutput has the full buffer" in summary


# ---------------------------------------------------------------------------
# The spawner end to end: the deadline fires, `serves` opts out of it
# ---------------------------------------------------------------------------

class _FakeTask:
    def __init__(self, task_id="t-fake"):
        self.task_id = task_id
        self.lines = []
        self.exit_code = None
        self._running = True

    def is_running(self):
        return self._running


@pytest.fixture
def spawner_harness(monkeypatch):
    """Wire the spawner to a fake shell, a fake stop, and a captured wake."""
    from app.core import task_registry as tr
    from app.core import turn_scheduler
    from app.mcp import builtin_tools as bt

    task = _FakeTask()
    frames = []

    async def _fake_spawn_background(command, cwd, session_id, tool_use_id=None):
        tr.task_registry.start_task(session_id, task.task_id, "shell", command)
        return task

    async def _fake_stop(task_id, session_id=None):
        task._running = False
        return {"task_id": task_id}

    async def _capture(session_id, frame):
        frames.append(frame)

    monkeypatch.setattr(bt, "_spawn_background", _fake_spawn_background)
    monkeypatch.setattr(bt, "stop_background_shell_task", _fake_stop)
    monkeypatch.setattr(turn_scheduler, "on_task_terminal", _capture)
    yield task, frames
    tr.task_registry.clear_session("s-spawn")


def _jump_clock(monkeypatch):
    """A monotonic clock that leaps a full budget between every reading, so
    the deadline branch is exercised without a real wait."""
    state = {"n": 0}

    def _monotonic():
        state["n"] += 1
        return state["n"] * 10_000.0

    monkeypatch.setattr(ms.time, "monotonic", _monotonic)


@pytest.mark.asyncio
async def test_deadline_stops_the_task_and_wakes_with_a_timeout(
    spawner_harness, monkeypatch
):
    import asyncio

    from app.core.config import settings

    task, frames = spawner_harness
    monkeypatch.setattr(settings, "monitor_command_timeout_seconds", 1200)
    _jump_clock(monkeypatch)

    spawn = ms.make_fluid_monitor_spawner("s-spawn")
    outcome = await spawn({"command": "while true; do sleep 30; done"}, None)

    assert "1200s time budget" in outcome.text
    for _ in range(50):
        if frames:
            break
        await asyncio.sleep(0.01)

    assert frames, "the watcher never fired a terminal frame"
    # Parked forever became woken with a timeout — and a timeout is a stop,
    # not a failure.
    assert frames[0]["status"] == "killed"
    assert "1200s" in frames[0]["summary"]
    assert "time budget" in frames[0]["summary"]
    assert not task.is_running()


@pytest.mark.asyncio
async def test_serves_skips_the_deadline_and_marks_the_receipt(
    spawner_harness, monkeypatch
):
    import asyncio

    from app.core.config import settings

    task, frames = spawner_harness
    monkeypatch.setattr(settings, "monitor_command_timeout_seconds", 1200)
    _jump_clock(monkeypatch)

    spawn = ms.make_fluid_monitor_spawner("s-spawn")
    outcome = await spawn({"command": "npm run dev", "serves": True}, None)

    assert "time budget" not in outcome.text
    assert "NOT parked on it" in outcome.text
    # The flag reaches fluid's park accounting through the receipt — but the
    # vendored harness is bot-synced and may predate the field, in which case
    # the spawn must still succeed (receipt built without it) rather than
    # failing over a version skew.
    import dataclasses

    receipt = outcome.background_task
    if any(f.name == "serves" for f in dataclasses.fields(receipt)):
        assert receipt.serves is True
    # The clock has leapt far past any budget; the service is still up.
    await asyncio.sleep(0.05)
    assert task.is_running()
    assert frames == []

    # Its eventual death is still a notification — and a stop, not a failure.
    task._running = False
    for _ in range(50):
        if frames:
            break
        await asyncio.sleep(0.01)
    assert frames and frames[0]["status"] == "killed"
    assert "service is no longer running" in frames[0]["summary"]


@pytest.mark.asyncio
async def test_a_command_with_its_own_bound_gets_no_second_one(
    spawner_harness, monkeypatch
):
    import asyncio

    from app.core.config import settings

    task, frames = spawner_harness
    monkeypatch.setattr(settings, "monitor_command_timeout_seconds", 1200)
    _jump_clock(monkeypatch)
    spawn = ms.make_fluid_monitor_spawner("s-spawn")
    outcome = await spawn({"command": "timeout 600 make ci"}, None)

    assert "time budget" not in outcome.text
    # The model's own bound is the only one: the clock leaps past any default
    # budget and nothing stops the task.
    await asyncio.sleep(0.05)
    assert task.is_running()
    task._running = False
    task.exit_code = 0
    for _ in range(50):
        if frames:
            break
        await asyncio.sleep(0.01)
    assert frames and frames[0]["status"] == "completed"
