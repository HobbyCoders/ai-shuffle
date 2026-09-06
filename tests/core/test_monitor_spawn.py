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
        async def read(self, _n):
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
        self.cwd = None
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
        task.cwd = cwd
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


# ---------------------------------------------------------------------------
# The session's working directory (round-6 audit): Monitor inherited the
# SERVER's cwd, so `gh pr checks … --watch` died with "fatal: not a git
# repository" in four audited worktree sessions — a park and a wake each.
# ---------------------------------------------------------------------------

class _Ctx:
    """The slice of fluid's ToolCtx the spawner reads."""

    def __init__(self, cwd=None, tool_use_id=None):
        self.cwd = cwd
        self.extra = {"tool_use_id": tool_use_id} if tool_use_id else {}


@pytest.mark.asyncio
async def test_monitor_falls_back_to_the_session_cwd(spawner_harness):
    task, _frames = spawner_harness

    spawn = ms.make_fluid_monitor_spawner("s-spawn")
    await spawn({"command": "gh pr checks 7 --watch"}, _Ctx(cwd="/work/tree"))

    # Fluid's Bash does `input.get("cwd") or ctx.cwd`; so does Monitor now.
    assert task.cwd == "/work/tree"


@pytest.mark.asyncio
async def test_an_explicit_cwd_still_wins(spawner_harness):
    task, _frames = spawner_harness

    spawn = ms.make_fluid_monitor_spawner("s-spawn")
    await spawn(
        {"command": "make ci", "cwd": "/elsewhere"}, _Ctx(cwd="/work/tree")
    )

    assert task.cwd == "/elsewhere"


@pytest.mark.asyncio
async def test_no_session_cwd_leaves_the_spawn_unpinned(spawner_harness):
    task, _frames = spawner_harness

    spawn = ms.make_fluid_monitor_spawner("s-spawn")
    await spawn({"command": "make ci"}, None)

    assert task.cwd is None


# ---------------------------------------------------------------------------
# wait_seconds — a short command settles inline instead of costing a park, a
# wake, a fresh turn and a full prompt re-send.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "value,expected",
    [
        (None, 0), ("", 0), ("nonsense", 0), (-5, 0), (0, 0),
        (7, 7), ("12", 12), (7.9, 7), (10_000, ms.MAX_WAIT_SECONDS),
    ],
)
def test_wait_seconds_is_bounded(value, expected):
    assert ms.clamp_wait_seconds(value) == expected


@pytest.mark.asyncio
async def test_a_command_that_finishes_in_the_wait_settles_inline(
    spawner_harness,
):
    import asyncio

    task, frames = spawner_harness
    task._running = False
    task.exit_code = 0
    task.lines = ["4 passed in 5.91s"]

    spawn = ms.make_fluid_monitor_spawner("s-spawn")
    outcome = await spawn(
        {"command": "pytest tests/core/test_x.py -q", "wait_seconds": 30}, None
    )

    # The result is right here: the output and the exit code, with no
    # instruction to end the turn waiting for a completion.
    assert "4 passed in 5.91s" in outcome.text
    assert "exit code 0" in outcome.text
    assert "End the turn now" not in outcome.text
    # Nothing to park on, and no wake was queued.
    assert outcome.background_task is None
    assert not outcome.is_error
    await asyncio.sleep(0.05)
    assert frames == []


@pytest.mark.asyncio
async def test_an_inline_failure_is_reported_as_one(spawner_harness):
    task, _frames = spawner_harness
    task._running = False
    task.exit_code = 1
    task.lines = ["FAILED tests/test_a.py::test_b"]

    spawn = ms.make_fluid_monitor_spawner("s-spawn")
    outcome = await spawn({"command": "pytest -q", "wait_seconds": 30}, None)

    # NOT is_error: a non-zero exit is the command's own verdict, and an
    # is_error outcome is raised by the code-mode runner, which used to abort
    # the whole cell over a red TDD baseline.
    assert not outcome.is_error
    assert "finished with a failure" in outcome.text
    assert "not a tool failure" in outcome.text
    assert "FAILED tests/test_a.py::test_b" in outcome.text


@pytest.mark.asyncio
async def test_a_command_still_running_after_the_wait_parks_as_before(
    spawner_harness, monkeypatch
):
    import asyncio

    from app.core.config import settings

    task, frames = spawner_harness
    monkeypatch.setattr(settings, "monitor_command_timeout_seconds", 0)
    _jump_clock(monkeypatch)  # the wait window expires on its first reading

    spawn = ms.make_fluid_monitor_spawner("s-spawn")
    outcome = await spawn({"command": "npm run build", "wait_seconds": 30}, None)

    # The existing receipt wording is untouched; the wait only adds a clause
    # saying why the model is reading it.
    assert ms.monitor_receipt_text(task.task_id) in outcome.text
    assert "still running after the 30s" in outcome.text
    assert outcome.background_task is not None

    # And the background path is intact: the completion still wakes the parent.
    task._running = False
    task.exit_code = 0
    for _ in range(50):
        if frames:
            break
        await asyncio.sleep(0.01)
    assert frames and frames[0]["status"] == "completed"


@pytest.mark.asyncio
async def test_a_serving_process_ignores_the_wait(spawner_harness, monkeypatch):
    from app.core.config import settings

    task, _frames = spawner_harness
    monkeypatch.setattr(settings, "monitor_command_timeout_seconds", 0)

    spawn = ms.make_fluid_monitor_spawner("s-spawn")
    outcome = await spawn(
        {"command": "npm run dev", "serves": True, "wait_seconds": 60}, None
    )

    assert "NOT parked on it" in outcome.text
    assert "still running after" not in outcome.text


def test_the_native_spec_advertises_the_wait():
    from fluid.tools.builtins import register_builtins
    from fluid.tools.registry import ToolRegistry

    registry = ToolRegistry()
    register_builtins(registry)

    ms.amend_native_monitor_spec(registry)

    spec = registry.get("Monitor")
    assert spec.input_schema["properties"]["wait_seconds"]["maximum"] == (
        ms.MAX_WAIT_SECONDS
    )
    assert "wait_seconds" in spec.description
    # Idempotent: a second amendment must not stack the clause.
    ms.amend_native_monitor_spec(registry)
    assert registry.get("Monitor").description.count("probably quick") == 1


def test_amending_never_adds_monitor_to_a_surface_without_it():
    from fluid.tools.registry import ToolRegistry

    registry = ToolRegistry()
    ms.amend_native_monitor_spec(registry)

    assert registry.get("Monitor") is None


# ---------------------------------------------------------------------------
# Status honesty (round-6 audit): the settled status is the OUTCOME the summary
# shows, not the process's disposition — and the notification says what the
# task row says.
# ---------------------------------------------------------------------------

def test_a_swallowed_failure_is_not_a_completion():
    # `…; gh pr checks 42 || true` exits 0 while its own last word says it
    # failed. The notification used to carry <status>completed</status> above a
    # summary that read "failed to run git … EXIT:1".
    status, summary = _settle(
        exit_code=0,
        lines=["failed to run git: not a git repository", "EXIT:1"],
    )

    assert status == "error"
    assert "EXIT:1" in summary


def test_an_explicit_zero_marker_is_still_a_completion():
    status, _ = _settle(exit_code=0, lines=["all checks passed", "EXIT:0"])
    assert status == "completed"


def test_the_last_marker_is_the_outcome():
    # A watcher that retries prints the failed attempt and then the good one.
    assert _settle(exit_code=0, lines=["EXIT:1", "retrying", "EXIT:0"])[0] == "completed"
    assert _settle(exit_code=0, lines=["EXIT:0", "EXIT:2"])[0] == "error"


def test_a_marker_never_rescues_a_real_failure():
    assert _settle(exit_code=1, lines=["EXIT:0"])[0] == "error"


def test_the_row_is_written_from_the_settle():
    from app.core import task_registry as tr

    tr.task_registry.start_task("s-row", "t-row", "shell", "watch ci")
    # The output reader already settled it from the exit code alone.
    tr.task_registry.complete_task("s-row", "t-row", "completed")

    status, _summary = ms.record_settled_status(
        "s-row", "t-row", "error", "gh: EXIT:1", serves=False
    )

    assert status == "error"
    row = tr.task_registry.get_task("s-row", "t-row")
    assert row["status"] == "error"
    assert row["summary"] == "gh: EXIT:1"
    tr.task_registry.clear_session("s-row")


def test_a_stop_that_races_the_settle_wins_both_places():
    from app.core import task_registry as tr

    tr.task_registry.start_task("s-race", "t-race", "shell", "npm test")
    # The TaskStop landed while the watcher was settling a clean self-exit.
    tr.task_registry.complete_task(
        "s-race", "t-race", "killed",
        summary=tr.USER_STOP_SUMMARY, reason=tr.USER_STOP_REASON,
    )

    status, summary = ms.record_settled_status(
        "s-race", "t-race", "completed", "done", serves=False
    )

    # The notification no longer says "completed" over a row that says killed.
    assert status == "killed"
    assert "Stopped by the user" in summary
    assert tr.task_registry.get_task("s-race", "t-race")["status"] == "killed"
    tr.task_registry.clear_session("s-race")


@pytest.mark.asyncio
async def test_the_wake_frame_and_the_row_agree(spawner_harness, monkeypatch):
    import asyncio

    from app.core import task_registry as tr
    from app.core.config import settings

    task, frames = spawner_harness
    monkeypatch.setattr(settings, "monitor_command_timeout_seconds", 0)

    spawn = ms.make_fluid_monitor_spawner("s-spawn")
    await spawn({"command": "gh pr checks 42 --watch || true"}, None)

    task.lines = ["fatal: not a git repository", "EXIT:1"]
    task.exit_code = 0
    task._running = False
    for _ in range(100):
        if frames:
            break
        await asyncio.sleep(0.01)

    assert frames and frames[0]["status"] == "error"
    assert tr.task_registry.get_task("s-spawn", task.task_id)["status"] == "error"


@pytest.mark.asyncio
async def test_fluid_passes_an_inline_settle_through_without_parking(
    spawner_harness,
):
    """End to end through fluid's own Monitor handler: a settled inline result
    carries no receipt, so nothing is registered as pending and no park
    accounting starts."""
    from fluid.tools.builtins.monitor import handler as monitor_handler
    from fluid.tools.registry import ToolCtx

    task, frames = spawner_harness
    task._running = False
    task.exit_code = 0
    task.lines = ["ok"]

    started = []
    ctx = ToolCtx(cwd="/work/tree", session_id="s-spawn")
    ctx.extra["monitor_spawn"] = ms.make_fluid_monitor_spawner("s-spawn")
    ctx.extra["background_task_started"] = started.append

    outcome = await monitor_handler(
        {"command": "pytest -q", "wait_seconds": 10}, ctx
    )

    assert outcome.background_task is None
    assert started == []
    assert "ok" in outcome.text
    # And the cwd fallback survives the real ctx.
    assert task.cwd == "/work/tree"


# ---------------------------------------------------------------------------
# R7-1 — the wake summary keeps the verdict it was woken to deliver
# ---------------------------------------------------------------------------

def test_repeated_status_blocks_collapse():
    """A `--watch` command re-prints the same block every poll; one audited
    summary was ~8KB of it with the real result at the very end."""
    lines = []
    for _ in range(20):
        lines.append("Refreshing checks status every 10 seconds.")
        lines.append("Backend tests  pending  0  https://example/run")
    lines.append("Backend tests  pass  7m24s")

    collapsed = ms.collapse_repeated_lines(lines)

    assert collapsed[0] == "Refreshing checks status every 10 seconds."
    assert collapsed[1] == "Backend tests  pending  0  https://example/run"
    assert "repeated 19 more times" in collapsed[2]
    assert collapsed[-1] == "Backend tests  pass  7m24s"
    # Two lines plus a notice plus the verdict — not 41 lines.
    assert len(collapsed) == 4


def test_a_short_run_of_lines_is_left_alone():
    """Collapsing a line that merely appears twice costs more than it saves."""
    lines = ["building", "building", "done"]

    assert ms.collapse_repeated_lines(lines) == lines


def test_blank_runs_are_never_collapsed_into_a_notice():
    assert ms.collapse_repeated_lines(["", "", "", "x"]) == ["", "", "", "x"]


def test_a_chained_commands_test_summary_survives_truncation(monkeypatch):
    """`npm test && npm run check` puts its test verdict in the MIDDLE of the
    buffer, which is exactly what head+tail truncation deleted — twice in one
    audited session, costing a TaskOutput call each time."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "monitor_summary_max_chars", 600)
    lines = (
        ["npm test"]
        + [f"filler line {i} of the vitest run" for i in range(200)]
        + ["Test Files  7 passed (7)", "Tests  122 passed (122)"]
        + [f"svelte-check noise {i}" for i in range(200)]
        + ["svelte-check found 1 error and 3 warnings"]
    )

    summary = ms._summarize(lines)

    assert "Tests  122 passed (122)" in summary
    assert "Test Files  7 passed (7)" in summary
    # And the tail — the thing that actually failed — is still there.
    assert "found 1 error and 3 warnings" in summary
    assert "chars truncated" in summary
    assert len(summary) <= 700


def test_truncation_is_tail_biased_not_head_biased(monkeypatch):
    """Failures name themselves at the end. The head window is gone."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "monitor_summary_max_chars", 400)
    lines = [f"step {i} running some build command" for i in range(200)]

    summary = ms._summarize(lines)

    assert "step 199" in summary
    assert "step 0 " not in summary


def test_a_summary_with_no_verdict_lines_is_pure_tail(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "monitor_summary_max_chars", 300)
    summary = ms._summarize(["x" * 5_000])

    assert "chars truncated" in summary
    assert len(summary) < 500


# ---------------------------------------------------------------------------
# R7-2 — a service that goes away is a stop, not a failure
# ---------------------------------------------------------------------------

def test_a_service_that_exits_after_serving_is_a_stop():
    status, summary = _settle(
        exit_code=1, serves=True, ran_seconds=4000,
        lines=["INFO: Application startup complete."],
    )

    assert status == "completed"
    assert "no longer running" in summary
    assert "restart it only if you still need it" in summary


def test_a_service_that_never_came_up_is_still_a_failure():
    """A port clash kills uvicorn in two seconds; that must stay visible."""
    status, summary = _settle(
        exit_code=1, serves=True, ran_seconds=2,
        lines=["ERROR: [Errno 98] address already in use"],
    )

    assert status == "error"
    assert "address already in use" in summary


def test_a_stopped_services_summary_is_capped():
    """One audited settle carried 34k characters of access log into a
    notification whose whole news was "the service is gone"."""
    status, summary = _settle(
        exit_code=0, serves=True, ran_seconds=4000,
        lines=[f'INFO: 127.0.0.1 - "GET /api/v1/x{i} HTTP/1.1" 200' for i in range(2000)],
    )

    assert status == "completed"
    assert len(summary) < 800


def test_permission_denied_names_the_sh_fallback():
    status, summary = _settle(
        exit_code=126,
        lines=["/bin/sh: 1: /tmp/run/launch-backend.sh: Permission denied"],
    )

    assert status == "error"
    assert "sh <path>" in summary


def test_an_ordinary_failure_gets_no_exec_bit_hint():
    assert ms.not_executable_hint(1, "FAILED tests/test_a.py") == ""
    assert ms.not_executable_hint(126, "some other failure") == ""


# ---------------------------------------------------------------------------
# R7-3 — a serving task is bounded by its SESSION, not by its own age
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_an_idle_session_stops_its_serving_task(spawner_harness, monkeypatch):
    """A uvicorn fixture and a Vite dev server ran unattended for 8h15m on an
    audited runner after their session's wake died."""
    import asyncio

    from app.core.config import settings

    task, frames = spawner_harness
    monkeypatch.setattr(settings, "monitor_serving_idle_seconds", 3600)
    monkeypatch.setattr(ms, "session_idle_seconds", lambda sid: 30_000.0)
    _jump_clock(monkeypatch)

    spawn = ms.make_fluid_monitor_spawner("s-spawn")
    await spawn({"command": "npm run dev", "serves": True}, None)

    for _ in range(100):
        if frames:
            break
        await asyncio.sleep(0.01)

    assert frames, "the serving watcher never bounded the task"
    assert frames[0]["status"] == "killed"
    assert "idle" in frames[0]["summary"]
    assert "did not fail" in frames[0]["summary"]
    assert not task.is_running()


@pytest.mark.asyncio
async def test_a_tended_session_keeps_its_serving_task(spawner_harness, monkeypatch):
    import asyncio

    from app.core.config import settings

    task, frames = spawner_harness
    monkeypatch.setattr(settings, "monitor_serving_idle_seconds", 3600)
    monkeypatch.setattr(ms, "session_idle_seconds", lambda sid: 5.0)
    _jump_clock(monkeypatch)

    spawn = ms.make_fluid_monitor_spawner("s-spawn")
    await spawn({"command": "npm run dev", "serves": True}, None)
    await asyncio.sleep(0.05)

    assert frames == []
    assert task.is_running()

    # Let the watcher settle so it does not outlive the test.
    task._running = False
    task.exit_code = 0
    await asyncio.sleep(0.05)


def test_the_idle_bound_is_off_when_the_setting_is_zero(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "monitor_serving_idle_seconds", 0)
    assert ms.serving_idle_budget_seconds() == 0


# ---------------------------------------------------------------------------
# R7-4 — the output reader survives a line longer than the pipe limit
# ---------------------------------------------------------------------------

def _fake_proc(monkeypatch, chunks):
    """Wire ``_spawn_background`` to a process whose stdout yields ``chunks``."""
    import asyncio

    queue = list(chunks)

    class _Stdout:
        async def read(self, _n):
            return queue.pop(0) if queue else b""

    class _Proc:
        pid = 4242
        stdout = _Stdout()
        returncode = 0

        async def wait(self):
            return 0

    async def _fake_exec(*args, **kwargs):
        return _Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)


@pytest.mark.asyncio
async def test_a_line_over_the_pipe_limit_does_not_kill_the_reader(monkeypatch):
    """`StreamReader.readline()` raises ValueError past 64 KiB and clears its
    buffer; nothing caught it, so the reader died and the task's output froze
    while the process kept running (live production occurrence)."""
    from app.mcp import builtin_tools as bt

    giant = b"m" * 300_000
    _fake_proc(monkeypatch, [giant, b"\nafter the giant line\n", b""])

    task = await bt._spawn_background("npm run build", None, None)
    await task._reader_task

    assert task.exit_code == 0
    # The long line survives, in bounded pieces, and — the point — the lines
    # AFTER it are still buffered.
    assert task.lines[-1] == "after the giant line"
    assert sum(line.count("m") for line in task.lines) == 300_000
    bt._background_tasks.pop(task.task_id, None)


@pytest.mark.asyncio
async def test_output_split_across_chunks_still_forms_whole_lines(monkeypatch):
    from app.mcp import builtin_tools as bt

    _fake_proc(monkeypatch, [b"Tests  45 pa", b"ssed (45)\ndone\n", b""])

    task = await bt._spawn_background("npm test", None, None)
    await task._reader_task

    assert task.lines == ["Tests  45 passed (45)", "done"]
    bt._background_tasks.pop(task.task_id, None)


# ---------------------------------------------------------------------------
# R7-5 — a script with no exec bit is run through sh instead of failing
# ---------------------------------------------------------------------------

@pytest.mark.skipif(os.name == "nt", reason="Windows has no exec bit")
def test_a_non_executable_script_is_run_through_sh(tmp_path):
    from app.mcp.builtin_tools import sh_fallback_command

    script = tmp_path / "launch-backend.sh"
    script.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    script.chmod(0o644)

    assert sh_fallback_command(str(script), None) == f"sh {script}"


@pytest.mark.skipif(os.name == "nt", reason="Windows has no exec bit")
def test_an_executable_script_is_left_alone(tmp_path):
    from app.mcp.builtin_tools import sh_fallback_command

    script = tmp_path / "run.sh"
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    script.chmod(0o755)

    assert sh_fallback_command(str(script), None) == str(script)


def test_a_real_command_is_never_rewritten(tmp_path):
    from app.mcp.builtin_tools import sh_fallback_command

    for command in (
        "npm test",
        "pytest tests/core -q",
        "sh ./launch.sh",
        "",
    ):
        assert sh_fallback_command(command, str(tmp_path)) == command
