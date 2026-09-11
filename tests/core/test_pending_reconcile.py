"""Engine pending-set hygiene (2026-09-05 audit F1/F7).

fluid rebuilds ``_background_pending`` from the ``SubagentEvent`` log on every
rehydrate and pops an id only on a terminal event, so a completion that never
reached the engine is re-created as a phantom forever and listed on every
``SessionParked``. These cover both repair arms: the live-engine reconcile
(resume/turn end) and the boot repair that writes into the durable log.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core import pending_reconcile
from app.core.config import settings
from app.db import background_tasks as background_tasks_db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _task_row(session_id, task_id, status, kind="agent"):
    background_tasks_db.upsert_background_task({
        "task_id": task_id,
        "session_id": session_id,
        "kind": kind,
        "status": status,
        "title": task_id,
        "started_at": "2026-09-05T00:00:00+00:00",
        "updated_at": "2026-09-05T00:00:01+00:00",
    })


def _fake_engine(pending_ids):
    """An engine stand-in with fluid's background-lifecycle surface."""
    import asyncio

    engine = MagicMock(name="engine")
    engine._background_pending = {t: MagicMock() for t in pending_ids}
    engine._background_lock = asyncio.Lock()
    engine._rehydrate_background_lifecycle_unlocked = AsyncMock()
    return engine


def _fake_adapter(pending_ids):
    adapter = MagicMock(name="adapter")
    adapter._is_delegated = False
    adapter._engine = _fake_engine(pending_ids)
    adapter.prepare_background_completions = AsyncMock()
    return adapter


def _fluid_event(session_id, seq, task_id, kind):
    return (session_id, seq, json.dumps({
        "_cls": "SubagentEvent",
        "_v": 1,
        "session_id": session_id,
        "seq": seq,
        "task_id": task_id,
        "kind": kind,
    }))


def _seed_fluid_events(conn, rows):
    conn.executemany(
        "INSERT INTO fluid_session_events (session_id, seq, event) "
        "VALUES (?, ?, ?)",
        rows,
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Durable truth readers
# ---------------------------------------------------------------------------

def test_settled_ids_reports_only_rows_that_are_actually_terminal(mock_db):
    _task_row("s-set", "done", "completed")
    _task_row("s-set", "live", "running")

    settled = pending_reconcile.settled_task_ids(
        "s-set", ["done", "live", "never-persisted"]
    )

    # A row that has not been written yet is a task too young to have been
    # persisted, never a settled one — silence must not narrow a park.
    assert settled == {"done"}


def test_durable_agent_arm_ignores_shells(mock_db):
    _task_row("s-arm", "agent-1", "running", kind="agent")
    _task_row("s-arm", "shell-1", "running", kind="shell")
    _task_row("s-arm", "agent-2", "completed", kind="agent")

    # Only sub-agents park: a Monitor shell can be a ``serves`` process meant
    # to stay up, and parking on one would pin the session forever.
    assert pending_reconcile.nonterminal_agent_task_ids("s-arm") == ["agent-1"]


@pytest.mark.parametrize("status", ["completed", "error", "killed"])
def test_durable_agent_arm_ignores_unflushed_local_completion(mock_db, monkeypatch, status):
    from app.core import task_persistence, task_registry as registry_module

    registry = registry_module.TaskRegistry()
    monkeypatch.setattr(registry_module, "task_registry", registry)
    with patch.object(task_persistence, "enqueue_task_snapshot") as enqueue:
        registry.start_task("s-lag", "finished", "agent", "worker")
        # Persist the real producer's running snapshot, but hold its later
        # terminal snapshot in the queue so the database deliberately lags.
        background_tasks_db.upsert_background_task(enqueue.call_args.args[2])
        registry.complete_task("s-lag", "finished", status, notify=False)
        assert enqueue.call_args.args[2]["status"] == status
        registry.start_task("s-lag", "live", "agent", "still working")
        background_tasks_db.upsert_background_task(enqueue.call_args.args[2])
        _task_row("s-lag", "durable-only", "running")
        registry.start_task("other-session", "durable-only", "agent", "other")
        registry.complete_task("other-session", "durable-only", status, notify=False)

        assert pending_reconcile.durable_task_statuses("s-lag", ["finished"]) == {
            "finished": "running",
        }
        assert set(pending_reconcile.nonterminal_agent_task_ids("s-lag")) == {
            "live", "durable-only",
        }


# ---------------------------------------------------------------------------
# Live-engine reconcile (resume / turn end)
# ---------------------------------------------------------------------------

async def test_resume_retires_the_poisoned_pending_ids(mock_db):
    """The audited shape: ids fluid still counts pending whose completions the
    app recorded long ago are retired on resume; live work is left alone."""
    _task_row("s-poison", "dead-1", "completed")
    _task_row("s-poison", "dead-2", "error")
    _task_row("s-poison", "alive", "running")
    adapter = _fake_adapter(["dead-1", "dead-2", "alive", "unknown"])

    with patch(
        "app.core.turn_scheduler.retire_task_terminals",
        new=AsyncMock(return_value=True),
    ) as retire:
        retired = await pending_reconcile.reconcile_engine_pending(
            "s-poison", adapter=adapter
        )

    assert sorted(retired) == ["dead-1", "dead-2"]
    # Submitted through the round-6 helper, with each id's real status.
    frames = retire.await_args.args[1]
    assert {f["task_id"]: f["status"] for f in frames} == {
        "dead-1": "completed", "dead-2": "error",
    }
    # Rehydrate was forced: the pending set is meaningless before it runs.
    adapter._engine._rehydrate_background_lifecycle_unlocked.assert_awaited()


async def test_reconcile_falls_back_to_the_adapter_during_connect(mock_db):
    """At connect time the pool has no entry yet, so the helper cannot reach an
    adapter — the one we were handed makes the same submission."""
    _task_row("s-connect", "dead", "killed")
    adapter = _fake_adapter(["dead"])

    with patch(
        "app.core.turn_scheduler.retire_task_terminals",
        new=AsyncMock(return_value=False),
    ):
        retired = await pending_reconcile.reconcile_engine_pending(
            "s-connect", adapter=adapter
        )

    assert retired == ["dead"]
    frames = adapter.prepare_background_completions.await_args.args[0]
    assert [f["task_id"] for f in frames] == ["dead"]


async def test_reconcile_never_retires_work_it_cannot_prove_is_over(mock_db):
    """A pending id with no evidence anywhere is left pending: retiring a live
    worker would drop the very park holding its session open."""
    adapter = _fake_adapter(["mystery"])

    with patch(
        "app.core.turn_scheduler.retire_task_terminals", new=AsyncMock()
    ) as retire:
        retired = await pending_reconcile.reconcile_engine_pending(
            "s-mystery", adapter=adapter
        )

    assert retired == []
    retire.assert_not_awaited()
    adapter.prepare_background_completions.assert_not_awaited()


async def test_delegated_leg_keeps_its_own_pending_set(mock_db):
    adapter = _fake_adapter(["dead"])
    adapter._is_delegated = True
    _task_row("s-cli", "dead", "completed")

    assert await pending_reconcile.reconcile_engine_pending(
        "s-cli", adapter=adapter
    ) == []


# ---------------------------------------------------------------------------
# Boot repair — straight at the durable event log
# ---------------------------------------------------------------------------

def test_orphan_ids_follow_fluids_own_rehydrate_rule(mock_db, test_db):
    _seed_fluid_events(test_db, [
        _fluid_event("ses_a", 1, "t1", "started"),
        _fluid_event("ses_a", 2, "t2", "started"),
        _fluid_event("ses_a", 3, "t2", "finished"),
        _fluid_event("ses_a", 4, "t3", "started"),
        _fluid_event("ses_a", 5, "t3", "completion_pending"),
    ])

    # ``completion_pending`` is not terminal — fluid does not pop on it either.
    assert pending_reconcile.orphan_pending_ids("ses_a") == ["t1", "t3"]


def test_boot_repair_writes_the_missing_terminal_events(mock_db, test_db):
    _seed_fluid_events(test_db, [
        _fluid_event("ses_b", 1, "gone", "started"),
        _fluid_event("ses_b", 2, "live", "started"),
        _fluid_event("ses_b", 9, "other", "started"),
    ])
    _task_row("app-b", "gone", "completed")
    _task_row("app-b", "live", "running")

    repaired = pending_reconcile.repair_fluid_pending_log("app-b", "ses_b")

    assert repaired == ["gone"]
    rows = test_db.execute(
        "SELECT seq, event FROM fluid_session_events "
        "WHERE session_id = ? AND seq > 9 ORDER BY seq",
        ("ses_b",),
    ).fetchall()
    assert len(rows) == 1
    # Appended AFTER the current maximum, which is where fluid resumes
    # numbering (native._seed_bus_seq) — never on top of an existing row.
    assert rows[0]["seq"] == 10
    event = json.loads(rows[0]["event"]) if isinstance(
        rows[0]["event"], str
    ) else rows[0]["event"]
    assert event["_cls"] == "SubagentEvent"
    assert event["task_id"] == "gone"
    assert event["kind"] == "finished"
    # The app's durable turn queue owns wake admission, so the repair asks for
    # no turn of its own.
    assert event["wake"] is False
    assert event["wake_disposition"] == "suppressed"
    # Same shape a live retirement submits, so a replay drops it as host-owned
    # rather than mirroring a second task card.
    assert event["worker_session_id"] == "gone"

    # A rehydrate reading the repaired log now sees only the live task.
    assert pending_reconcile.orphan_pending_ids("ses_b") == ["live", "other"]


def test_boot_repair_is_idempotent(mock_db, test_db):
    _seed_fluid_events(test_db, [_fluid_event("ses_c", 1, "gone", "started")])
    _task_row("app-c", "gone", "error")

    assert pending_reconcile.repair_fluid_pending_log("app-c", "ses_c") == ["gone"]
    assert pending_reconcile.repair_fluid_pending_log("app-c", "ses_c") == []


def test_boot_sweep_only_visits_this_installs_open_sessions(mock_db, test_db):
    test_db.executemany(
        "INSERT INTO sessions (id, sdk_session_id, instance_id, status) "
        "VALUES (?, ?, ?, ?)",
        [
            ("open", "ses_open", settings.effective_instance_id, "active"),
            ("gone", "ses_gone", settings.effective_instance_id, "archived"),
            ("noeng", None, settings.effective_instance_id, "active"),
            ("theirs", "ses_theirs", "some-other-install", "active"),
        ],
    )
    test_db.commit()

    assert pending_reconcile.list_sessions_for_pending_repair() == [
        ("open", "ses_open")
    ]

async def test_terminal_evidence_survives_registry_restart(mock_db):
    from app.core.task_registry import task_registry, TaskRegistry
    from app.core import task_persistence

    sid, tid = "reconcile-evidence", "report-task"
    task_registry.start_task(sid, tid, "agent", "run checks")
    task_registry.complete_task(
        sid, tid, "error", summary="FAILED test_checkout: retained traceback",
        reason="worker_error", report_path="/tmp/task-report.txt", report_chars=321,
    )
    await task_persistence.flush()
    # A fresh registry exercises the durable producer/consumer path, not seeded metadata.
    with patch("app.core.task_registry.task_registry", TaskRegistry()):
        (frame,) = pending_reconcile.terminal_frames_for(sid, [tid])
    assert frame["summary"] == "FAILED test_checkout: retained traceback"
    assert frame["report_path"] == "/tmp/task-report.txt"
    assert frame["report_chars"] == 321
    assert frame["reason"] == "worker_error"
    assert frame["status"] == "error"
