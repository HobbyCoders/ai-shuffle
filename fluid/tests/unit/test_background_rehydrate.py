"""Background lifecycle after a restart: what the event log alone says is
still pending, owed a wake, early, or finished — and how close settles it."""

from __future__ import annotations

import asyncio

import pytest

import fluid.engine.background as background_mod
from fluid.engine.native import NativeLoop
from fluid.events import (
    BackgroundTaskFinished,
    SessionParked,
    SessionWoken,
    SubagentEvent,
    UserTurn,
)
from fluid.events.model import WAKE_AUTO, WAKE_SUPPRESSED
from fluid.permissions.engine import PermissionEngine
from fluid.store.base import MemoryStore
from fluid.store.jsonl import JsonlStore
from fluid.tools.builtins import register_builtins
from fluid.tools.registry import BackgroundTaskReceipt, ToolCtx, ToolRegistry
from tests.unit.test_tool_loop import ScriptedProvider, _text

SID = "parent"


def _loop(store, *, provider=None, **extra) -> NativeLoop:
    registry = ToolRegistry()
    register_builtins(registry)
    return NativeLoop(
        provider=provider or ScriptedProvider([]),
        store=store,
        session_id=SID,
        model="test-model",
        registry=registry,
        permissions=PermissionEngine("never_ask"),
        ctx=ToolCtx(extra=dict(extra)),
    )


def _sub(task_id: str, kind: str, **fields) -> SubagentEvent:
    return SubagentEvent(session_id=SID, task_id=task_id, kind=kind, **fields)


async def _store_with(*events, store=None):
    store = store or MemoryStore()
    await store.create_session(SID)
    for seq, event in enumerate(events, start=1):
        await store.append_event(SID, event.__class__(**{**event.__dict__, "seq": seq}))
    return store


async def _rehydrate(loop: NativeLoop) -> None:
    async with loop._background_lock:
        await loop._rehydrate_background_lifecycle_unlocked()


def _emitted(loop: NativeLoop, task_id: str) -> list[SubagentEvent]:
    return [e for e in loop.bus.log(SID) if isinstance(e, SubagentEvent) and e.task_id == task_id]


# ─── what the log says after a restart ───────────────────────────────────────


async def test_restart_rebuilds_pending_owed_early_and_terminal_state() -> None:
    store = await _store_with(
        # orphan: started, never finished — still pending
        _sub("orphan", "started", description="long job", worker_session_id="w-orphan"),
        # finished with a wake nobody delivered — owed
        _sub("owed", "started"),
        _sub("owed", "finished", summary="42", wake=True, report_path="/r/owed.md"),
        # finished and woken — nothing owed
        _sub("woken", "started"),
        _sub("woken", "finished", summary="done", wake=True),
        SessionWoken(session_id=SID, task_ids=("woken",)),
        # cancelled with the wake suppressed — host owns it
        _sub("quiet", "started"),
        _sub("quiet", "cancelled", wake=False),
        # completion recorded before the receipt, then the receipt: finalize now
        _sub("early", "completion_pending", status="failed", summary="boom", wake=True),
        _sub("early", "started", worker_session_id="w-early"),
        # completion with no receipt yet — stays early, never phantom-pending
        _sub("unreceipted", "completion_pending", summary="ok"),
        # a stray replayed "started" after the terminal row must not resurrect it
        _sub("replayed", "started"),
        _sub("replayed", "failed", wake=False),
        _sub("replayed", "started"),
        # progress rows are not lifecycle transitions
        _sub("orphan", "progress", summary="50%"),
    )
    loop = _loop(store)

    await _rehydrate(loop)

    assert set(loop._background_pending) == {"orphan"}
    receipt = loop._background_pending["orphan"]
    assert (receipt.description, receipt.worker_session_id) == ("long job", "w-orphan")
    assert set(loop._background_completions) == {"owed", "early"}
    owed = loop._background_completions["owed"]
    assert (owed.status, owed.summary, owed.report_path, owed.wake) == ("finished", "42", "/r/owed.md", True)
    assert set(loop._background_early) == {"unreceipted"}
    assert {"owed", "woken", "quiet", "early", "replayed"} <= loop._background_terminal
    assert "orphan" not in loop._background_terminal

    early = _emitted(loop, "early")
    assert [e.kind for e in early] == ["failed"]
    assert early[0].worker_session_id == "w-early"
    assert early[0].wake_disposition == WAKE_AUTO
    assert loop._background_completions["early"].worker_session_id == "w-early"
    # A completion owed from the log alone is exactly what the wake turn carries.
    content = loop._background_wake_content(loop._background_completions.values())
    assert "Task owed (finished)" in content and "/r/owed.md" in content


async def test_rehydrate_reads_the_log_once(monkeypatch) -> None:
    store = await _store_with(_sub("a", "started"))
    loop = _loop(store)
    reads = 0
    real = background_mod.iter_events

    def counting(*args, **kwargs):
        nonlocal reads
        reads += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(background_mod, "iter_events", counting)
    await _rehydrate(loop)
    await _rehydrate(loop)
    assert reads == 1


async def test_store_without_replay_degrades_to_memory_only() -> None:
    class NoReplay(MemoryStore):
        async def last_event_seq(self, session_id):
            return 0

        async def iter_events(self, session_id, seq):
            raise NotImplementedError("no event log")
            yield  # pragma: no cover - makes this an async generator

    loop = _loop(await _store_with(store=NoReplay()))

    await _rehydrate(loop)
    assert loop._background_rehydrated is True
    assert loop._background_pending == {}

    # The lifecycle still works from memory.
    await loop._background_task_started(BackgroundTaskReceipt(task_id="t1"))
    assert "t1" in loop._background_pending


# ─── the host's recovery hook ────────────────────────────────────────────────


async def test_recovery_hook_settles_orphans_it_can_prove_dead() -> None:
    store = await _store_with(
        _sub("dead", "started", worker_session_id="w-dead"),
        _sub("alive", "started"),
        _sub("weird", "started"),
    )
    asked: list[tuple[str, ...]] = []

    async def recover(pending):
        asked.append(tuple(sorted(pending)))
        return [
            BackgroundTaskFinished(task_id="dead", status="failed", summary="host restarted",
                                   wake=False),
            BackgroundTaskFinished(task_id="never-started", status="finished"),
            # Regression: a non-terminal status was recorded as terminal in
            # memory but replayed as pending after the next restart.
            BackgroundTaskFinished(task_id="weird", status="running"),
        ]

    loop = _loop(store, recover_background=recover)
    await _rehydrate(loop)

    assert asked == [("alive", "dead", "weird")]
    assert set(loop._background_pending) == {"alive", "weird"}
    dead = _emitted(loop, "dead")
    assert [(e.kind, e.wake_disposition, e.worker_session_id) for e in dead] == [
        ("failed", WAKE_SUPPRESSED, "w-dead")
    ]
    assert "dead" not in loop._background_completions  # wake=False: host owns it
    assert _emitted(loop, "never-started") == []
    assert _emitted(loop, "weird") == []
    assert "weird" not in loop._background_terminal


async def test_recovered_state_survives_a_second_restart(tmp_path) -> None:
    store = await _store_with(_sub("dead", "started"), _sub("alive", "started"),
                              store=JsonlStore(tmp_path))

    async def recover(pending):
        return [BackgroundTaskFinished(task_id="dead", status="cancelled", wake=False)]

    first = _loop(store, recover_background=recover)
    await _rehydrate(first)
    await first.aclose()
    store.close()

    second = _loop(JsonlStore(tmp_path))
    await _rehydrate(second)
    assert set(second._background_pending) == {"alive"}
    assert "dead" in second._background_terminal
    await second.aclose()


# ─── parking after a restart, and close ──────────────────────────────────────


async def test_first_turn_after_restart_parks_on_the_orphan() -> None:
    store = await _store_with(_sub("orphan", "started"))
    provider = ScriptedProvider([_text("still waiting")])
    loop = _loop(store, provider=provider)

    await loop.submit(UserTurn(content="status?"))
    await loop.wait_turn()

    parked = [e for e in loop.bus.log(SID) if isinstance(e, SessionParked)]
    assert [p.pending_task_ids for p in parked] == [("orphan",)]
    assert provider.calls == 1  # no autonomous wake for a task still running


async def test_owed_wake_is_delivered_after_restart_without_a_new_completion() -> None:
    store = await _store_with(
        _sub("owed", "started"),
        _sub("owed", "finished", summary="the result", wake=True),
    )
    provider = ScriptedProvider([_text("picked it up"), _text("unused")])
    loop = _loop(store, provider=provider)

    await loop.submit(UserTurn(content="hello again"))
    await loop.wait_turn()

    woken = [e for e in loop.bus.log(SID) if isinstance(e, SessionWoken)]
    assert [w.task_ids for w in woken] == [("owed",)]
    assert any("the result" in r.messages[-1].text() for r in provider.requests)
    assert loop._background_completions == {}


async def test_close_parks_live_tasks_when_no_turn_ended() -> None:
    loop = _loop(await _store_with())
    await loop._background_task_started(BackgroundTaskReceipt(task_id="live"))
    await loop._background_task_started(BackgroundTaskReceipt(task_id="server", serves=True))

    await loop.aclose()

    parked = [e for e in loop.bus.log(SID) if isinstance(e, SessionParked)]
    assert [(p.pending_task_ids, p.reason) for p in parked] == [(("live",), "engine_closed")]


async def test_close_cancels_the_wake_retry_and_unsettled_spawns(monkeypatch) -> None:
    monkeypatch.setattr(background_mod, "_BACKGROUND_SPAWN_SETTLE_TIMEOUT", 0.01)
    loop = _loop(await _store_with())
    await _rehydrate(loop)
    stuck_spawn = asyncio.create_task(asyncio.sleep(3600))
    settled_spawn = asyncio.create_task(asyncio.sleep(0))
    loop._background_spawn_tasks.update({stuck_spawn, settled_spawn})
    wake = asyncio.create_task(asyncio.sleep(3600))
    loop._background_wake_task = wake
    question_wake = asyncio.create_task(asyncio.sleep(3600))
    loop._question_wake_task = question_wake
    loop._background_wake_failures = 1
    loop._background_completions["x"] = BackgroundTaskFinished(task_id="x")
    loop._schedule_background_wake_retry()
    retry = loop._background_wake_retry
    assert retry is not None

    await loop.aclose()

    assert stuck_spawn.cancelled() and not settled_spawn.cancelled()
    assert wake.cancelled() and question_wake.cancelled()
    assert retry.cancelled() and loop._background_wake_retry is None
    # Closed: nothing schedules or kicks another wake.
    loop._schedule_background_wake_retry()
    assert loop._background_wake_retry is None
    loop._kick_background_wake()
    assert loop._background_wake_task is wake


async def test_finish_rejects_a_non_terminal_status() -> None:
    loop = _loop(await _store_with())
    with pytest.raises(ValueError, match="finished, failed, or cancelled"):
        await loop._finish_background_task(BackgroundTaskFinished(task_id="t", status="running"))


async def test_retry_timer_fires_a_new_wake_attempt() -> None:
    loop = _loop(await _store_with())
    await _rehydrate(loop)
    kicked: list[bool] = []
    loop._kick_background_wake = lambda: kicked.append(True)  # type: ignore[method-assign]
    loop._background_wake_failures = 1
    loop._background_wake_retry = None

    loop._background_wake_retry_due()

    assert kicked == [True] and loop._background_wake_retry is None
    await loop.aclose()
