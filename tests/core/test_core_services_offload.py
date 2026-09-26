"""Core services must not do blocking DB work on the asyncio event loop.

The background loops and long-lived services in ``app/core`` are the worst
place for a synchronous ``psycopg2`` call: a scheduled automation, an inbound
connector message or a webhook fan-out runs while every other WebSocket frame,
SSE stream and HTTP request shares the same worker. These tests pin the fix by
IDENTITY, not by wire shape — each one records the thread a blocking callable
actually ran on and requires it to differ from the loop thread.

Two further properties are asserted here, because they are what makes the fix
correct rather than merely fast:

* **One hop per logical operation.** Where several DB calls were contiguous
  they now share a single ``run_sync`` closure, so every call in the group must
  land on the SAME worker thread — that is also what keeps a read-decide-write
  (the automation budget gate, the delivery dedup gate) atomic.
* **Loop-affinity splits still fire their loop side.** Anything that needs a
  running loop — ``asyncio.create_task``, the scheduler kick, a connector
  broadcast — was deliberately left on the caller's loop (the hazard
  ``turn_manager.submit_async`` documents), so those must be observed running
  ON the loop thread.

Companion patterns: tests/api/test_chat_ws_offload.py, tests/core/test_turn_manager.py.
"""

import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.automation_manager import AutomationManager
from app.core.connectors.base import IncomingMessage
from app.core.connectors.manager import ConnectorManager
from app.db import database


class _ThreadSpy:
    """Wrap a callable, recording the thread each call ran on."""

    def __init__(self, fn, label=""):
        self._fn = fn
        self.label = label
        self.threads = []

    def __call__(self, *args, **kwargs):
        self.threads.append(threading.get_ident())
        return self._fn(*args, **kwargs)


@pytest.fixture
def manager():
    return AutomationManager()


@pytest.fixture
def conn_manager():
    return ConnectorManager()


def _make_automation(automation_id="auto-off", **kwargs):
    database.create_automation(
        automation_id=automation_id,
        name="Offload Test",
        trigger_type="webhook",
        trigger_config={},
        prompt="do the thing",
        profile_id="default",
        **kwargs,
    )
    database.update_automation(automation_id, is_enabled=True)
    return automation_id


# ═══ automation_manager._execute — the budget read-decide-write ══════════════


async def test_execute_admits_the_run_off_the_loop_in_one_hop(
    manager, mock_db, mock_encryption, loop_thread
):
    """``check_budget`` READS the shared spend aggregate and the run INSERT
    that follows is what the next read counts. It must run off-loop, and all of
    it in ONE hop: split across two hops, two concurrent fires could both see
    the pre-run total and both admit themselves (a budget double-spend)."""
    _make_automation("auto-budget-hop")

    from app.core import automation_service

    budget_spy = _ThreadSpy(automation_service.check_budget, "check_budget")
    insert_spy = _ThreadSpy(database.create_automation_run, "create_automation_run")

    with patch.object(automation_service, "check_budget", budget_spy), \
         patch.object(database, "create_automation_run", insert_spy), \
         patch.object(manager, "_execute_run", new_callable=AsyncMock):
        await manager._execute("auto-budget-hop", {"trigger": "manual"})

    # the run really was admitted...
    assert budget_spy.threads and insert_spy.threads
    # ...off the loop thread...
    assert loop_thread not in budget_spy.threads + insert_spy.threads
    # ...and the read and the write it decides shared ONE worker thread.
    assert set(budget_spy.threads) == set(insert_spy.threads)
    assert len(set(budget_spy.threads + insert_spy.threads)) == 1


async def test_execute_over_budget_records_off_the_loop(
    manager, mock_db, mock_encryption, loop_thread
):
    """The refusal path writes two rows; they belong to the same hop as the
    budget read that decided them."""
    _make_automation("auto-budget-no")

    from app.core import automation_service

    insert_spy = _ThreadSpy(database.create_automation_run)
    update_spy = _ThreadSpy(database.update_automation)

    with patch.object(automation_service, "check_budget", return_value=(False, "nope")), \
         patch.object(database, "create_automation_run", insert_spy), \
         patch.object(database, "update_automation", update_spy):
        await manager._execute("auto-budget-no", {"trigger": "manual"})

    runs = database.get_automation_runs("auto-budget-no")
    assert any(r.get("status") == "budget_exceeded" for r in runs)
    assert insert_spy.threads and loop_thread not in insert_spy.threads
    assert set(insert_spy.threads) == set(update_spy.threads)


async def test_execute_approval_notification_stays_on_the_loop(
    manager, mock_db, mock_encryption, loop_thread
):
    """LOOP-AFFINITY SPLIT: the approval records are written off-loop, but the
    notification talks to live connector clients over the network and is an
    ``await`` — it must fire back on the caller's loop."""
    _make_automation("auto-appr", requires_approval=True)

    approval_spy = _ThreadSpy(database.create_automation_approval)
    notify_threads = []

    async def _notify(automation, approval_id, prompt_preview):
        notify_threads.append(threading.get_ident())

    with patch.object(database, "create_automation_approval", approval_spy), \
         patch.object(manager, "_notify_approval_needed", _notify), \
         patch.object(manager, "_execute_run", new_callable=AsyncMock) as run_spy:
        await manager._execute("auto-appr", {"trigger": "manual"})

    assert approval_spy.threads and loop_thread not in approval_spy.threads
    assert notify_threads == [loop_thread]
    run_spy.assert_not_called()  # approval short-circuits execution


# ═══ automation_manager._on_schedule_trigger — the cross-install fire claim ══


async def test_schedule_trigger_gate_and_claim_run_off_the_loop(
    manager, mock_db, mock_encryption, loop_thread
):
    """The scheduled tick's gate (row read + runner election) and the atomic
    fire claim are the hottest recurring path in this file. LOOP-AFFINITY
    SPLIT: the execution task is created back on the loop — ``create_task``
    from a worker thread would raise/be dropped and the fire would be lost."""
    _make_automation("auto-tick")

    from app.core import automation_service

    election_spy = _ThreadSpy(
        lambda automation: (True, "elected"), "should_run_scheduled_here"
    )
    claim_spy = _ThreadSpy(lambda aid, bucket: True, "claim_automation_fire")
    prune_spy = _ThreadSpy(lambda: None, "prune_automation_fires")
    task_threads = []

    def _spy_task(coro, *, name):
        task_threads.append(threading.get_ident())
        coro.close()  # never actually execute the run
        return MagicMock()

    with patch.object(automation_service, "should_run_scheduled_here", election_spy), \
         patch.object(database, "claim_automation_fire", claim_spy), \
         patch.object(database, "prune_automation_fires", prune_spy), \
         patch.object(manager, "_create_tracked_task", _spy_task):
        await manager._on_schedule_trigger("auto-tick", "cron", {})

    assert election_spy.threads and loop_thread not in election_spy.threads
    assert claim_spy.threads and loop_thread not in claim_spy.threads
    # claim + prune are one logical operation → one hop.
    assert prune_spy.threads and set(prune_spy.threads) == set(claim_spy.threads)
    # ...and the fire is still dispatched ON the loop.
    assert task_threads == [loop_thread]


async def test_schedule_trigger_skips_when_the_fire_is_already_claimed(
    manager, mock_db, mock_encryption
):
    """Semantics unchanged by the offload: losing the claim still skips, and
    the prune (which only ran after a won claim) does not run."""
    _make_automation("auto-tick-lost")

    from app.core import automation_service

    prune_spy = _ThreadSpy(lambda: None)
    task_calls = []

    with patch.object(automation_service, "should_run_scheduled_here",
                      return_value=(True, "elected")), \
         patch.object(database, "claim_automation_fire", return_value=False), \
         patch.object(database, "prune_automation_fires", prune_spy), \
         patch.object(manager, "_create_tracked_task",
                      lambda coro, *, name: task_calls.append(name) or coro.close()):
        await manager._on_schedule_trigger("auto-tick-lost", "cron", {})

    assert task_calls == []
    assert prune_spy.threads == []


# ═══ automation_manager._deliver_output — the dedup read-decide-write ═══════


async def test_delivery_dedup_gate_runs_off_the_loop_in_one_hop(
    manager, mock_db, mock_encryption, loop_thread
):
    """The dedup gate reads recent runs, decides, and stamps the run row. Both
    halves must be off-loop and in the same hop or two runs settling together
    could each read the other as undelivered."""
    automation_id = _make_automation("auto-dedupe")
    automation = database.get_automation(automation_id)
    automation["output_config"] = {"type": "session"}

    read_spy = _ThreadSpy(database.get_automation_runs)
    write_spy = _ThreadSpy(database.update_automation_run)

    database.create_automation_run(
        run_id="run-dedupe", automation_id=automation_id, status="running",
        trigger_data={},
    )
    with patch.object(database, "get_automation_runs", read_spy), \
         patch.object(database, "update_automation_run", write_spy):
        await manager._deliver_output(automation, "run-dedupe", "hello")

    assert write_spy.threads and loop_thread not in write_spy.threads


# ═══ connectors/manager — inbound platform messages ═════════════════════════


async def test_dispatch_event_reads_subscribers_off_the_loop(
    conn_manager, mock_db, loop_thread
):
    read_spy = _ThreadSpy(lambda event_type: [])
    with patch.object(database, "get_connectors_for_event", read_spy):
        await conn_manager.dispatch_event("session.complete", {})

    assert read_spy.threads and loop_thread not in read_spy.threads


async def test_incoming_message_routing_resolves_off_the_loop_in_one_hop(
    conn_manager, mock_db, loop_thread
):
    """The inbound path's routing reads (connector row, API user, connector
    session) share ONE hop; the "no session yet → create
    one" decision inside it stays atomic. LOOP-AFFINITY SPLIT: the durable turn
    submit stays on the loop, because its scheduler kick needs a running loop
    (see turn_manager.submit_async)."""
    session_spy = _ThreadSpy(
        lambda cid, chat: {"id": "cs-1", "session_id": "sess-ai"}
    )
    connector_spy = _ThreadSpy(lambda cid: {"id": cid, "type": "webhook",
                                            "name": "wh", "api_user_id": "api-1"})
    api_user_spy = _ThreadSpy(lambda uid: {"id": uid, "profile_ids": ["default"]})
    log_spy = _ThreadSpy(lambda **kw: None)
    submit_threads = []

    async def _submit(*args, **kwargs):
        submit_threads.append(threading.get_ident())
        return "turn-1"

    message = IncomingMessage(
        connector_id="conn-1",
        platform_chat_id="chat-1",
        platform_user_id="u-1",
        platform_user_name="User",
        text="hello there",
        platform_message_id=None,
    )

    with patch.object(database, "get_connector_session", session_spy), \
         patch.object(database, "get_connector", connector_spy), \
         patch.object(database, "get_api_user", api_user_spy), \
         patch.object(database, "create_connector_event_log", log_spy), \
         patch.object(database, "update_connector_event_dispatched",
                      lambda cid, success: None), \
         patch("app.core.turn_manager.submit_async", _submit), \
         patch("app.core.turn_manager.wait_session_idle",
               new=AsyncMock(return_value=True)), \
         patch("app.core.turn_outcome.final_turn_outcome",
               return_value={"usage": {}, "error": None, "interrupted": False}), \
         patch("app.core.turn_outcome.extract_final_assistant_text",
               return_value="hi back"):
        await conn_manager.handle_incoming_message(message)

    routing = session_spy.threads + connector_spy.threads + api_user_spy.threads
    assert routing, "the routing reads never ran"
    assert loop_thread not in routing
    assert len(set(routing)) == 1, "routing reads were split across hops"
    # the inbound event log is off-loop too...
    assert log_spy.threads and loop_thread not in log_spy.threads
    # ...and the turn submit still happened ON the loop.
    assert submit_threads == [loop_thread]


# ═══ hook_executor / worktree_manager ═══════════════════════════════════════


async def test_execute_hooks_for_event_reads_off_the_loop(mock_db, loop_thread):
    from app.core import hook_executor

    read_spy = _ThreadSpy(lambda event: [])
    with patch.object(database, "get_app_hooks_for_event", read_spy):
        await hook_executor.execute_hooks_for_event(None, "SessionStart", {})

    assert read_spy.threads and loop_thread not in read_spy.threads


async def test_execute_hook_marks_triggered_off_the_loop(mock_db, loop_thread):
    from app.core import hook_executor

    write_spy = _ThreadSpy(lambda hook_id, success: None)
    with patch.object(database, "update_hook_triggered", write_spy):
        result = await hook_executor.execute_hook(
            {"id": "hook-1", "hook_type": "unknown"}, {"event": "X", "data": {}}
        )

    assert result["success"] is False
    assert write_spy.threads and loop_thread not in write_spy.threads


async def test_terminate_worktree_sessions_reads_off_the_loop(mock_db, loop_thread):
    from app.core.worktree_manager import WorktreeManager

    read_spy = _ThreadSpy(lambda worktree_id: [])
    with patch.object(database, "get_sessions_for_worktree", read_spy):
        terminated = await WorktreeManager().terminate_worktree_sessions("wt-1")

    assert terminated == []
    assert read_spy.threads and loop_thread not in read_spy.threads


# ═══ provider_agent — worker dispatch ═══════════════════════════════════════


@pytest.fixture
def worker_dispatch_env(monkeypatch):
    """Stub every out-of-process dependency of ``dispatch_worker``.

    Mirrors ``tests/core/test_provider_agent.py::dispatch_env``, but hands back
    thread spies instead of payloads: what is under test here is WHERE the
    dispatch's DB work runs, not what it writes.
    """
    from app.core import project_resolver, provider_agent as pa
    from app.core import turn_manager as tm_mod
    from app.core import turn_scheduler as ts_mod
    from app.db import database as db

    get_session_spy = _ThreadSpy(lambda sid: {
        "id": sid, "project_id": "proj-1", "worktree_id": "wt-1",
        "worktree_branch": "feat/x", "worktree_path": "/repo/feat-x",
    })
    provider_spy = _ThreadSpy(lambda pid, include_token=False: {"name": "OpenAI"})
    create_session_spy = _ThreadSpy(lambda **kw: {"id": kw.get("session_id")})
    update_session_spy = _ThreadSpy(lambda sid, **kw: None)
    submit_threads = []

    async def _submit_async(session_id, source, **kwargs):
        submit_threads.append(threading.get_ident())
        return "turn-1"

    async def _wait_idle(session_id, timeout=300.0):
        return True

    async def _on_task_terminal(session_id, frame):
        return None

    monkeypatch.setattr(db, "get_session", get_session_spy)
    monkeypatch.setattr(db, "get_model_provider", provider_spy)
    monkeypatch.setattr(db, "create_session", create_session_spy)
    monkeypatch.setattr(db, "update_session", update_session_spy)
    monkeypatch.setattr(ts_mod, "on_task_terminal", _on_task_terminal)
    monkeypatch.setattr(tm_mod, "submit_async", _submit_async)
    monkeypatch.setattr(tm_mod, "wait_session_idle", _wait_idle)
    monkeypatch.setattr(
        project_resolver, "resolve_existing_cwd", lambda row: "/repo/feat-x"
    )
    monkeypatch.setattr(pa, "final_assistant_text", lambda sid: "the worker's report")

    class _Env:
        get_session = get_session_spy
        get_model_provider = provider_spy
        create_session = create_session_spy
        update_session = update_session_spy
        submits = submit_threads

    return _Env()


async def test_worker_dispatch_prologue_runs_off_the_loop_in_one_hop(
    worker_dispatch_env, monkeypatch, loop_thread
):
    """Worker dispatch's prologue is three contiguous blocking calls — the
    parent-session inheritance read, the provider-name lookup (a DB read of its
    own that the scanner cannot see) and the worker-session INSERT. They share
    ONE hop off the loop. LOOP-AFFINITY SPLIT: the durable turn submit stays on
    the loop, where its scheduler kick can be scheduled."""
    from app.core import provider_agent as pa

    monkeypatch.setattr(pa, "final_turn_outcome", lambda sid: {
        "usage": {"tokens_in": 5}, "error": None, "interrupted": False,
        "terminal_found": True,
    })

    outcome = await pa.dispatch_worker(
        parent_session_id="parent-1",
        profile_id="default",
        provider_id="prov-openai",
        model_id="gpt-5.5",
        prompt="do the thing",
    )

    assert outcome.ok
    env = worker_dispatch_env
    prologue = (
        env.get_session.threads + env.get_model_provider.threads
        + env.create_session.threads
    )
    assert prologue, "the prologue never ran"
    assert loop_thread not in prologue
    assert len(set(prologue)) == 1, "the prologue was split across thread hops"
    assert env.submits == [loop_thread]


async def test_worker_failure_status_write_is_off_the_loop_and_still_lands(
    worker_dispatch_env, monkeypatch, loop_thread
):
    """The ``finally`` block's failed-status write used to be synchronous, so it
    always completed. It is now awaited under a shield — it must still land, and
    off the loop."""
    from app.core import provider_agent as pa

    monkeypatch.setattr(pa, "final_turn_outcome", lambda sid: {
        "usage": {}, "error": "the worker died", "interrupted": False,
        "terminal_found": True,
    })

    outcome = await pa.dispatch_worker(
        parent_session_id="parent-1",
        profile_id="default",
        provider_id="prov-openai",
        model_id="gpt-5.5",
        prompt="do the thing",
    )

    assert not outcome.ok
    writes = worker_dispatch_env.update_session.threads
    assert writes, "the failed-status verdict was never written"
    assert loop_thread not in writes
