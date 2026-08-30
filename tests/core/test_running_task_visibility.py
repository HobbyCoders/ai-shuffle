"""A RUNNING background task must be durably visible (audit item 4).

The audited failure: "I asked the agent to run the app and no task chip showed
the running instance". The dev server WAS launched through Monitor every time —
what was missing was any durable record of a task while it RUNS. Only terminal
transitions appended anything to the session event log (and only failures at
that), so the chip strip painted from the live feed of the spawning turn alone
and went empty on the next reload or session switch.

These tests pin the whole path end to end, on a simulated long-running shell:

    spawn  → ONE durable `task.updated` row, state=running, kind=shell,
             carrying the launching tool_use_id
    mirror → the in-stream TaskStarted for the SAME task records nothing
             (shared `mark_chipped(..., "start")` latch — no duplicate rows)
    exit   → ONE terminal `task.updated` row resolving that same task_id
             (shared `mark_chipped(..., "terminal")` latch with the
             terminal-notification guarantee — no duplicate history entries)
"""

import asyncio

import pytest

from app.core import turn_recorder
from app.core.task_registry import task_registry
from app.mcp import builtin_tools as bt


SESSION = "sess-vis"


@pytest.fixture(autouse=True)
def _isolate():
    bt._background_tasks.clear()
    task_registry.reset_all()
    yield
    bt._background_tasks.clear()
    task_registry.reset_all()


@pytest.fixture
def appended(monkeypatch):
    """Capture every contract row the recorder appends, without a database."""
    rows: list = []

    def _append(session_id, events):
        rows.extend(events)

    monkeypatch.setattr(
        turn_recorder.session_events_db, "append_events", _append
    )
    return rows


def _task_rows(rows):
    """The `task.updated` payloads, in append order (the envelope carries the
    type; every typed field lives in `payload`)."""
    return [r["payload"] for r in rows if r.get("type") == "task.updated"]


async def _spawn_fake_shell(monkeypatch, *, exit_code=0, lines=(b"",), **kwargs):
    """``_spawn_background`` over a fake subprocess that exits immediately."""
    chunks = list(lines)

    class _Stdout:
        async def readline(self):
            return chunks.pop(0) if chunks else b""

    class _Proc:
        pid = 999
        stdout = _Stdout()
        returncode = exit_code

        async def wait(self):
            return exit_code

    async def _fake_exec(*args, **kw):
        return _Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
    return await bt._spawn_background("npm run dev", None, SESSION, **kwargs)


# ---------------------------------------------------------------------------
# start — the durable running row
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_spawn_records_a_running_task_row(monkeypatch, appended):
    task = await _spawn_fake_shell(monkeypatch, tool_use_id="toolu_launch")
    await task._reader_task

    starts = [r for r in _task_rows(appended) if r["state"] == "running"]
    assert len(starts) == 1
    row = starts[0]
    assert row["task_id"] == task.task_id
    # kind drives the SHELL badge AND the reducer's turn-completed staleness
    # exemption — a shell legitimately outlives the turn that spawned it.
    assert row["kind"] == "shell"
    # The card join key: without it a terminal chip cannot settle the launching
    # card (reducer.ts task.updated → findSubagentByTask).
    assert row["tool_use_id"] == "toolu_launch"
    assert row["description"] == "npm run dev"


@pytest.mark.asyncio
async def test_spawn_binds_the_launch_tool_use_id_on_the_registry(monkeypatch, appended):
    task = await _spawn_fake_shell(monkeypatch, tool_use_id="toolu_launch")
    await task._reader_task

    assert (
        task_registry.get_task_tool_use_id(SESSION, task.task_id) == "toolu_launch"
    )


@pytest.mark.asyncio
async def test_in_stream_mirror_does_not_duplicate_the_running_row(
    monkeypatch, appended
):
    """The native leg emits BOTH: the spawner's frame and fluid's SubagentEvent
    mirror. The shared start latch keeps that to one durable row."""
    task = await _spawn_fake_shell(monkeypatch, tool_use_id="toolu_launch")
    await task._reader_task
    before = len(_task_rows(appended))

    # The in-stream shape query_engine yields for a TaskStartedMessage.
    await turn_recorder.record_drained_event(SESSION, {
        "type": "task_started",
        "task_id": task.task_id,
        "description": "npm run dev",
        "tool_use_id": None,
    })

    assert len(_task_rows(appended)) == before


@pytest.mark.asyncio
async def test_module_level_spawn_records_nothing(monkeypatch, appended):
    """No session → no parent to show a chip to."""
    async def _fake_exec(*args, **kw):
        class _Stdout:
            async def readline(self):
                return b""

        class _Proc:
            pid = 1
            stdout = _Stdout()
            returncode = 0

            async def wait(self):
                return 0

        return _Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
    task = await bt._spawn_background("npm run dev", None, None)
    await task._reader_task

    assert _task_rows(appended) == []


# ---------------------------------------------------------------------------
# settle — the SAME task resolves, once
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_clean_exit_settles_the_same_task(monkeypatch, appended):
    """complete_task's guaranteed notification covers error/killed only, so
    before this a cleanly-finished shell left its running chip painted for
    good — the reducer deliberately exempts shells from the turn-completed
    staleness heal."""
    task = await _spawn_fake_shell(monkeypatch, tool_use_id="toolu_launch")
    await task._reader_task

    rows = _task_rows(appended)
    assert [r["state"] for r in rows] == ["running", "completed"]
    assert {r["task_id"] for r in rows} == {task.task_id}
    assert rows[-1]["tool_use_id"] == "toolu_launch"


@pytest.mark.asyncio
async def test_nonzero_exit_settles_once(monkeypatch, appended):
    """The failure lane also runs the terminal-notification guarantee. Both
    share the `terminal` latch, so history shows ONE entry per task."""
    task = await _spawn_fake_shell(monkeypatch, exit_code=2)
    await task._reader_task
    # Let any scheduled terminal-surface work run.
    await asyncio.sleep(0)

    terminal = [
        r for r in _task_rows(appended)
        if r["state"] in ("completed", "error", "killed")
    ]
    assert len(terminal) == 1
    assert terminal[0]["state"] == "error"


@pytest.mark.asyncio
async def test_settle_is_skipped_when_a_stop_already_chipped(monkeypatch, appended):
    """A TaskStop that already latched `terminal` (and already emitted its own
    frame) must not be doubled by the reader's exit."""
    task = await _spawn_fake_shell(monkeypatch)
    # Simulate the stop path having claimed the latch before the reader ran.
    task_registry.mark_chipped(SESSION, task.task_id, "terminal")
    appended.clear()
    await bt._emit_shell_settle_frame(SESSION, task.task_id, "killed")

    assert _task_rows(appended) == []


# ---------------------------------------------------------------------------
# translation seams
# ---------------------------------------------------------------------------

def test_start_row_enriches_tool_use_id_from_the_registry():
    """Host Monitor shells announce themselves through a fluid SubagentEvent,
    which has no tool_use_id field at all — the registry is the only place the
    launching id survives."""
    task_registry.start_task(
        SESSION, "t-enrich", "shell", "npm run dev", tool_use_id="toolu_x"
    )

    rows = turn_recorder._translate_shared(
        {"type": "task_started", "task_id": "t-enrich", "description": "npm run dev"},
        SESSION,
    )

    assert len(rows) == 1
    assert rows[0]["payload"]["tool_use_id"] == "toolu_x"
    assert rows[0]["payload"]["kind"] == "shell"


def test_start_row_is_recorded_once_per_task():
    task_registry.start_task(SESSION, "t-dup", "agent", "worker")
    frame = {"type": "task_started", "task_id": "t-dup", "description": "worker"}

    first = turn_recorder._translate_shared(dict(frame), SESSION)
    second = turn_recorder._translate_shared(dict(frame), SESSION)

    assert len(first) == 1
    assert second == []


def test_a_resumed_task_paints_a_fresh_running_row():
    """A background agent continued under the same task_id is genuinely running
    again, so the start latch has to re-arm with the rest of the revival."""
    task_registry.start_task(SESSION, "t-resume", "agent", "worker")
    frame = {"type": "task_started", "task_id": "t-resume", "description": "worker"}
    assert turn_recorder._translate_shared(dict(frame), SESSION)

    task_registry.complete_task(SESSION, "t-resume", "completed")
    task_registry.start_task(SESSION, "t-resume", "agent", "worker")

    assert turn_recorder._translate_shared(dict(frame), SESSION)


def test_progress_and_notification_frames_are_not_latched():
    """The latch is scoped to the START phase only."""
    task_registry.start_task(SESSION, "t-prog", "agent", "worker")
    frame = {"type": "task_progress", "task_id": "t-prog", "description": "step"}

    assert turn_recorder._translate_shared(dict(frame), SESSION)
    assert turn_recorder._translate_shared(dict(frame), SESSION)


# ---------------------------------------------------------------------------
# the fluid Monitor leg threads its launching tool_use_id through
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fluid_monitor_spawner_passes_the_launch_tool_use_id(monkeypatch):
    from app.core import monitor_spawn as ms

    seen: dict = {}

    class _FakeTask:
        task_id = "mon-1"

        def is_running(self):
            return False

    async def _fake_spawn(command, cwd, session_id, tool_use_id=None):
        seen["tool_use_id"] = tool_use_id
        return _FakeTask()

    monkeypatch.setattr(bt, "_spawn_background", _fake_spawn)

    class _Ctx:
        extra = {"tool_use_id": "toolu_monitor"}

    spawner = ms.make_fluid_monitor_spawner(SESSION)
    await spawner({"command": "npm run dev"}, _Ctx())

    assert seen["tool_use_id"] == "toolu_monitor"


@pytest.mark.asyncio
async def test_fluid_monitor_spawner_survives_a_ctx_without_extra(monkeypatch):
    from app.core import monitor_spawn as ms

    seen: dict = {}

    class _FakeTask:
        task_id = "mon-2"

        def is_running(self):
            return False

    async def _fake_spawn(command, cwd, session_id, tool_use_id=None):
        seen["tool_use_id"] = tool_use_id
        return _FakeTask()

    monkeypatch.setattr(bt, "_spawn_background", _fake_spawn)

    spawner = ms.make_fluid_monitor_spawner(SESSION)
    await spawner({"command": "npm run dev"}, object())

    assert seen["tool_use_id"] is None
