"""Abrupt server restart: what the next boot must make true again.

Simulates the 2026-10-03 incident: the server process died while a parent had
background Task workers and Monitor shells in flight. The store is left the way
a dead process leaves it (task rows ``running``, worker turns ``running`` on a
lease nobody renews, the parent parked) and the registry starts empty, as a
fresh process does. Then the boot reconcile runs and these must hold:

- nothing still reads as running (task rows, worker turns, the parked marker);
- the parent is woken ONCE with a plain-language list of what was interrupted;
- the interrupted workers stay addressable: TaskList / TaskGet / ListAgents
  list them as "interrupted", and SendMessage to a task id resumes the worker;
- a networked worker whose membership has no legacy instance id is not
  quarantined (which is what skipped every one of these sweeps in the incident).

The process-ledger cleanup of orphaned children lives in
``test_process_ledger.py``.
"""

from __future__ import annotations

import json
import uuid
from unittest.mock import patch

import pytest
from fluid.tools.registry import ToolCtx

from app.core import agent_messaging, turn_scheduler
from app.core.config import settings
from app.core.startup_reconcile import reconcile_from_durable_truth
from app.core.task_registry import task_registry
from app.db import database
from app.db import turns as turns_db
from app.db.background_tasks import get_background_task, upsert_background_task


def _session(test_db, *, parked_task_kind=None) -> str:
    session_id = f"restart-{uuid.uuid4().hex[:10]}"
    database.create_session(session_id=session_id, profile_id="default")
    test_db.execute(
        "UPDATE sessions SET instance_id = ?, parked_task_kind = ? WHERE id = ?",
        (settings.effective_instance_id, parked_task_kind, session_id),
    )
    test_db.commit()
    return session_id


def _row(session_id, task_id, status, *, kind="agent", title=None, name=None, agent=None):
    upsert_background_task({
        "task_id": task_id,
        "session_id": session_id,
        "kind": kind,
        "tool_use_id": task_id if kind == "agent" else None,
        "title": title or task_id,
        "status": status,
        "summary": title or task_id,
        "name": name,
        "agent": agent,
        "model": "Claude · fable" if kind == "agent" else None,
    })


def _dead_lease_turn(session_id, source, test_db, *, state="running") -> str:
    turn_id = turns_db.enqueue_turn(session_id, source, prompt="work", profile_id="default")["id"]
    turns_db.start_turn(turn_id, run_id=f"run-dead-{uuid.uuid4().hex[:6]}")  # no runs row
    if state != "running":
        test_db.execute("UPDATE turns SET state = ? WHERE id = ?", (state, turn_id))
        test_db.commit()
    return turn_id


@pytest.fixture
def incident(mock_db, test_db, monkeypatch):
    """The state a killed server leaves behind, plus a fresh empty registry."""
    import app.core.turn_recorder as tr

    task_registry.reset_all()
    frames = []

    async def _capture(session_id, event):
        frames.append((session_id, event))

    monkeypatch.setattr(tr, "record_drained_event", _capture)
    kicked = []
    monkeypatch.setattr(turn_scheduler, "kick", kicked.append)
    monkeypatch.setattr(turn_scheduler, "_kick_after_batch_window", kicked.append)

    parent = _session(test_db, parked_task_kind="agent")
    w1, w2, nested, finished = (_session(test_db) for _ in range(4))
    # The parent's own turn ended parked on its background work.
    parent_turn = _dead_lease_turn(parent, "user", test_db, state="parked")
    # Two background workers mid-run, one Monitor shell of the parent's.
    _row(parent, w1, "running", title="Fix the flaky login test", name="fix-flaky-login", agent="General Purpose")
    _row(parent, w2, "running", title="Audit the sidebar", name="audit-sidebar", agent="Explore")
    _row(parent, "shell-1", "running", kind="shell", title="npm run dev")
    # A worker that had already finished before the restart.
    _row(parent, finished, "completed", title="Summarise the logs", name="summarise-logs")
    # w1 had its own Monitor running; w2 had dispatched a nested worker, which
    # is recorded on w2 (its owner) and on the parent (the UI routing copy).
    _row(w1, "shell-w1", "running", kind="shell", title="pytest -x")
    _row(w2, nested, "running", title="Nested helper")
    _row(parent, nested, "running", title="Nested helper")
    worker_turns = {sid: _dead_lease_turn(sid, "provider_agent", test_db) for sid in (w1, w2, nested)}
    task_registry.reset_all()  # the dead process's memory is gone
    return {
        "parent": parent, "w1": w1, "w2": w2, "nested": nested,
        "finished": finished, "frames": frames, "kicked": kicked,
        "parent_turn": parent_turn, "worker_turns": worker_turns,
    }


def _queued_unparks(session_id):
    return [t for t in turns_db.get_queued_turns(session_id) if t.get("source") == "unpark"]


async def test_nothing_reads_as_running_after_the_boot_reconcile(incident, test_db):
    from app.db.background_tasks import get_running_task_presence

    counts = await reconcile_from_durable_truth()

    parent, w1, w2, nested = (incident[k] for k in ("parent", "w1", "w2", "nested"))
    assert counts["background_task_rows_killed"] == 6
    for session_id, task_id in (
        (parent, w1), (parent, w2), (parent, "shell-1"), (w1, "shell-w1"),
        (w2, nested), (parent, nested),
    ):
        row = get_background_task(task_id, session_id)
        assert row["status"] == "killed" and row["reason"] == "server_restart"
    assert get_background_task(incident["finished"], parent)["status"] == "completed"
    presence = get_running_task_presence([parent, w1, w2, nested])
    assert not any(p["has_running_tasks"] for p in presence.values())
    # Worker turns on a dead lease are settled, not left running or requeued.
    for turn_id in incident["worker_turns"].values():
        assert turns_db.get_turn(turn_id)["state"] == "aborted"
    # The parked marker is gone and every task chip got its terminal frame.
    marker = test_db.execute(
        "SELECT parked_task_kind FROM sessions WHERE id = ?", (parent,)
    ).fetchone()["parked_task_kind"]
    assert marker is None
    settled = {ev["data"]["task_id"] for _sid, ev in incident["frames"]}
    assert settled == {w1, w2, "shell-1", "shell-w1", nested}
    assert all(ev["data"]["status"] == "killed" for _sid, ev in incident["frames"])


async def test_parent_is_told_once_what_was_interrupted(incident):
    await reconcile_from_durable_truth()

    parent, w1, w2, nested = (incident[k] for k in ("parent", "w1", "w2", "nested"))
    wakes = _queued_unparks(parent)
    assert len(wakes) == 1
    wake = wakes[0]
    assert set(wake["payload"]["task_ids"]) == {w1, w2, "shell-1"}
    prompt = wake["prompt"]
    assert prompt.count("<task-notification>") == 3
    assert prompt.count("<status>interrupted</status>") == 3
    assert f"SendMessage to {w1} continues it" in prompt
    assert "1 background task(s) of its own" in prompt  # w1's Monitor, w2's nested worker
    assert "background command was running" in prompt  # the parent's shell
    # Inherits the parked turn's context, so it runs as the parent.
    assert wake["parent_turn_id"] == incident["parent_turn"]
    assert parent in incident["kicked"]
    # Interrupted workers are not woken on their own; the nested worker's
    # routing copy on the parent is not announced to the parent either.
    for session_id in (w1, w2, nested):
        assert _queued_unparks(session_id) == []
    assert nested not in wake["payload"]["task_ids"]

    # A second boot finds nothing new and adds no second notice.
    counts = await reconcile_from_durable_truth()
    assert counts["background_task_rows_killed"] == 0
    assert len(_queued_unparks(parent)) == 1


async def test_a_queued_wake_from_before_the_restart_absorbs_the_notice(incident):
    parent = incident["parent"]
    earlier = turns_db.enqueue_turn(
        parent, "unpark", prompt="<task-notification>earlier</task-notification>",
        profile_id="default",
        payload={"task_ids": ["done-before"], "pending_frames": [
            {"task_id": "done-before", "status": "completed", "summary": "done"},
        ]},
    )["id"]

    await reconcile_from_durable_truth()

    wakes = _queued_unparks(parent)
    assert [w["id"] for w in wakes] == [earlier]
    assert set(wakes[0]["payload"]["task_ids"]) == {"done-before", incident["w1"], incident["w2"], "shell-1"}


def _builtin_tools(session_id):
    import app.mcp.builtin_tools as bt
    import app.mcp.inproc as sdk

    captured = {}
    real_create = sdk.create_tool_server

    def _spy(name, version, tools):
        captured["tools"] = tools
        return real_create(name=name, version=version, tools=tools)

    with patch.object(sdk, "create_tool_server", side_effect=_spy):
        bt.create_builtin_tools_server(session_id=session_id, profile_id="default", project_id=None)
    return {t.name: t.handler for t in captured["tools"]}


def _payload(result):
    return json.loads(result["content"][0]["text"])


async def test_interrupted_workers_stay_listed_after_a_fresh_start(incident):
    await reconcile_from_durable_truth()
    task_registry.reset_all()  # and survive a later process too, via the rows

    parent, w1, w2 = incident["parent"], incident["w1"], incident["w2"]
    tools = _builtin_tools(parent)

    listing = _payload(await tools["TaskList"]({}))
    by_id = {t["id"]: t for t in listing["tasks"]}
    assert listing["running_count"] == 0
    assert by_id[w1]["status"] == "interrupted" and by_id[w1]["reason"] == "server_restart"
    assert by_id[w1]["name"] == "fix-flaky-login" and by_id[w1]["agent"] == "General Purpose"
    assert by_id["shell-1"]["status"] == "interrupted"
    assert by_id[incident["finished"]]["status"] == "completed"

    task = _payload(await tools["TaskGet"]({"task_id": w2}))
    assert task["success"] is True
    assert task["task"]["status"] == "interrupted"

    roster = await agent_messaging.make_roster(lambda: parent)({}, ToolCtx())
    rows = {r["task_id"]: r for r in roster if r.get("kind") == "subagent"}
    assert rows[w1]["status"] == "interrupted" and rows[w1]["name"] == "fix-flaky-login"
    assert rows[incident["finished"]]["status"] == "completed"
    # The /live display view is unchanged: nothing interrupted shows as active.
    assert task_registry.get_visible(parent) == []


async def test_send_message_resumes_an_interrupted_worker_by_task_id(incident):
    await reconcile_from_durable_truth()
    task_registry.reset_all()  # a later process: only the durable rows remain

    parent, w1 = incident["parent"], incident["w1"]
    send = agent_messaging.make_message(lambda: parent)
    result = await send({"to": w1, "message": "Carry on from where you stopped"}, ToolCtx())

    assert not result.is_error, result.text
    assert "Resume queued for fix-flaky-login" in result.text
    queued = [t for t in turns_db.get_queued_turns(w1) if t.get("source") == "agent_message"]
    assert len(queued) == 1
    assert queued[0]["payload"]["agent_message_reply_to"] == parent
    # The parent's registry tracks the worker as running again.
    assert task_registry.get_task(parent, w1)["status"] == "running"

    # Addressing it by its roster name works too.
    task_registry.reset_all()
    result = await send({"to": "audit-sidebar", "message": "status?"}, ToolCtx())
    assert not result.is_error, result.text


def test_own_instance_id_names_the_self_member_without_a_legacy_alias(tmp_path):
    """The incident's root cause for 'nothing was reconciled': the worker's
    membership had no legacy instance id, so ``get_install(instance_id)``
    found no row, ``register_self`` returned unapproved, and startup skipped
    the boot reconcile and every periodic sweep (``Runner services are
    disabled while this install awaits approval``)."""
    from app.core import cluster
    from app.network import engine, runners
    from tests.core.network.harness import Worker

    worker = Worker("restart-self", tmp_path)
    try:
        with worker.active():
            engine.create_network("Restart self test")  # joins with no legacy id
            self_row = next(r for r in runners.directory()["runners"] if r["is_self"])
            assert not self_row.get("legacy_instance_id")

            row = database.get_install(settings.effective_instance_id)
            assert row is not None and row["id"] == self_row["id"]
            assert row["approved"] is True
            assert database.get_install("ai-shuffle-someone-else") is None
            assert cluster.register_self() is True
    finally:
        worker.close()
