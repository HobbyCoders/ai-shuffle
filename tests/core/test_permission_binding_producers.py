"""Permission bindings written by the real producers, sent through the engine.

Every test here builds its session and turn through the code that writes them
in production (local Task dispatch, StartSession, network receipt, scheduled
wakes, limit resumes, automations) and then runs the produced turn through the
query engine's final permission reconciliation, with only the provider
boundary mocked. Nothing seeds a binding or a provenance marker by hand,
except where a test names the legacy row it stands in for.

The rule under test: a turn's own permission mode counts only when the owner
chose it at enqueue (``_permission_explicit``); every other turn uses the
session's CURRENT binding, capped by the session's durable dispatch ceiling
and any ceiling the turn carries. A NULL binding means the session's agent
profile decides (the configuration the app used before turn provenance),
and only a missing session or profile fails closed to Ask.
"""

from contextlib import ExitStack
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

import app.core.query_engine as qe
from app.core.provider.events import ResultMessage
from tests.core.engine_harness import collect, make_state, stream_response
from tests.core.query_engine_flow_harness import RecordingPool, engine_patches, frames_of

BYPASS = "bypassPermissions"


def _profile(profile_id, permission_mode=None, **config):
    from app.db import database

    if permission_mode:
        config["permission_mode"] = permission_mode
    database.create_profile(profile_id, profile_id, None, config)
    return database.get_profile(profile_id)


async def _engine_mode(session_id, turn, tmp_path, *, connect_mode="auto"):
    """Run ``turn`` through the engine's reconciliation; return the mode it
    applied to the client right before the send.

    The session row and the profile are read live from the database, as the
    engine does; the client starts in ``connect_mode`` so any switch shows.
    """
    from unittest.mock import MagicMock

    from app.db import database

    row = database.get_session(session_id)
    profile = database.get_profile(row["profile_id"]) if row and row.get("profile_id") else None
    state = make_state()
    state.adapter.has_permission_callback = True
    state.adapter.receive_events = stream_response([ResultMessage(subtype="success")])
    state.adapter.set_model = AsyncMock()
    state.connect_options = MagicMock(model=None, permission_mode=connect_mode, aih_provider_id=None)
    with ExitStack() as stack:
        b = engine_patches(stack, pool=RecordingPool(state), cwd=str(tmp_path), session_row=row,
                           profile=profile or {"id": "gone", "config": {}})
        previous = qe.run_sync

        async def read_live(fn, *args, **kwargs):
            if fn == database.get_session:
                return database.get_session(*args, **kwargs)
            return await previous(fn, *args, **kwargs)

        stack.enter_context(patch.object(qe, "run_sync", side_effect=read_live))

        def build(**kwargs):
            b.options.permission_mode = kwargs["overrides"].get("permission_mode", "default")
            return b.options, {}

        stack.enter_context(patch("app.core.sdk_options.build_options_from_profile", side_effect=build))
        events = await collect(qe._stream_to_websocket_impl(
            turn.get("prompt") or "go", session_id, row["profile_id"] if row else "gone",
            overrides=turn.get("overrides"), turn_source=turn.get("source"), loop_guard_turn=turn))
    assert frames_of(events, "done"), events
    state.adapter.send_query.assert_awaited_once()
    return state.callback_refs["permission_mode"]


# ─── the NULL-binding rule ──────────────────────────────────────────────────


@pytest.mark.parametrize("profile_mode, expected", [
    (BYPASS, BYPASS), ("acceptEdits", "acceptEdits"), ("default", "default"), (None, "default"),
])
async def test_session_without_a_binding_runs_in_its_agents_mode(mock_db, tmp_path, profile_mode, expected):
    """A chat created without a composer pick (NULL binding) runs in the mode
    its agent is configured for, as it did before turn provenance."""
    from app.core import turn_manager
    from app.db import database, sessions, turns

    _profile("agent", profile_mode)
    sessions.create_session("plain", "agent", title="plain")
    assert database.get_session("plain")["permission_mode"] is None
    tid = turn_manager.submit("plain", "user", prompt="go", profile_id="agent",
                              explicit_permission=True, no_kick=True)
    assert await _engine_mode("plain", turns.get_turn(tid), tmp_path) == expected


def test_null_binding_rule_table():
    from app.core.session_modes import turn_permission_mode as resolve

    bypass_profile = {"permission_mode": BYPASS}
    assert resolve({}, {"permission_mode": None}, bypass_profile) == BYPASS
    assert resolve({}, {"permission_mode": None}, {"permission_mode": "default"}) == "default"
    # The profile could not be resolved: fail closed.
    assert resolve({}, {"permission_mode": None}, None) == "default"
    # The session row is missing: fail closed, whatever the profile says.
    assert resolve({}, None, bypass_profile) == "default"
    # A saved tightening wins over the agent's configured mode.
    assert resolve({}, {"permission_mode": "default"}, bypass_profile) == "default"
    # A stale, unmarked turn override never decides.
    assert resolve({"permission_mode": BYPASS}, {"permission_mode": None},
                   {"permission_mode": "default"}) == "default"
    # An owner's per-message pick never exceeds the current binding (a later
    # tightening wins); a tighter pick, or one on an unbound chat, stands.
    explicit = {"permission_mode": BYPASS, "_permission_explicit": True}
    assert resolve(explicit, {"permission_mode": "default"}, bypass_profile) == "default"
    assert resolve({"permission_mode": "default", "_permission_explicit": True},
                   {"permission_mode": BYPASS}, bypass_profile) == "default"
    assert resolve(explicit, {"permission_mode": None}, {"permission_mode": "default"}) == BYPASS
    # Ceilings cap the agent's mode too: the turn's and the session's.
    assert resolve({"_permission_ceiling": "acceptEdits"}, {"permission_mode": None},
                   bypass_profile) == "acceptEdits"
    assert resolve({}, {"permission_mode": None, "permission_ceiling": "acceptEdits"},
                   bypass_profile) == "acceptEdits"


async def test_null_binding_with_a_deleted_profile_fails_closed(mock_db, tmp_path):
    from app.core.session_modes import turn_permission_mode
    from app.db import database, sessions

    _profile("agent", BYPASS)
    sessions.create_session("orphan", "agent", title="orphan")
    row = database.get_session("orphan")
    database.delete_profile("agent")
    assert database.get_profile("agent") is None
    assert turn_permission_mode({}, row, None) == "default"


async def test_automation_session_with_a_null_binding_still_uses_the_agent_mode(mock_db, tmp_path):
    """A legacy automation session minted before automations stored a binding."""
    from app.core import turn_manager
    from app.db import sessions, turns

    _profile("auto-agent", "acceptEdits")
    sessions.create_session("legacy-auto", "auto-agent", title="legacy")
    tid = turn_manager.submit("legacy-auto", "automation", prompt="write", profile_id="auto-agent",
                              overrides={"max_turns": 1}, no_kick=True)
    assert await _engine_mode("legacy-auto", turns.get_turn(tid), tmp_path) == "acceptEdits"


# ─── finding 3: local dispatch persists the child's binding ─────────────────


async def _local_dispatch(monkeypatch, parent_id, profile_id="worker-agent", **kwargs):
    """Drive the real ``dispatch_worker``: real session creation and real
    durable turn submission; only the worker's run is skipped."""
    from app.core import provider_agent as pa, turn_manager, turn_scheduler

    monkeypatch.setattr(turn_manager, "_kick", lambda session_id: None)
    monkeypatch.setattr(pa, "_await_worker_idle", AsyncMock(return_value=None))
    # The run is skipped, so its first turn stays queued; a queued turn counts
    # as still working (#1868) and the dispatch would wait for it forever.
    monkeypatch.setattr(pa, "_worker_still_working", lambda sid: False)
    monkeypatch.setattr(pa, "final_turn_outcome", lambda sid: {
        "usage": {}, "error": None, "interrupted": False, "terminal_found": True})
    monkeypatch.setattr(pa, "final_assistant_text", lambda sid: "report")
    monkeypatch.setattr(turn_scheduler, "on_task_terminal", AsyncMock())
    outcome = await pa.dispatch_worker(parent_session_id=parent_id, profile_id=profile_id,
                                       prompt="do the work", **kwargs)
    assert outcome.ok, outcome.error
    return outcome.session_id


def _first_turn(session_id):
    from app.db import turns

    return turns.get_latest_turn(session_id)


@pytest.fixture
def dispatch_parent(mock_db, tmp_path):
    from app.db import sessions

    from app.db import database

    database.create_project("task-project", "Task project", None, str(tmp_path))
    _profile("parent-agent", "default")
    _profile("worker-agent", "default")

    def make(binding):
        sessions.create_session("parent", "parent-agent", title="parent", permission_mode=binding, project_id="task-project")
        return "parent"

    return make


@pytest.mark.parametrize("parent_mode", [BYPASS, "acceptEdits", "default"])
async def test_local_dispatch_child_runs_in_the_callers_mode(dispatch_parent, monkeypatch, tmp_path, parent_mode):
    from app.db import database

    worker = await _local_dispatch(monkeypatch, dispatch_parent(parent_mode))
    row = database.get_session(worker)
    assert row["is_worker"]
    assert row["permission_mode"] == parent_mode
    assert row["permission_ceiling"] == parent_mode
    turn = _first_turn(worker)
    assert turn["source"] == "provider_agent"
    assert await _engine_mode(worker, turn, tmp_path) == parent_mode


async def test_voice_task_child_runs_in_the_calls_binding(mock_db, monkeypatch, tmp_path):
    """Voice Task dispatch (``voice_bridge`` → ``task_dispatch`` → the same
    ``dispatch_worker``): a voice call never runs turns, so its binding decides."""
    from app.db import database, sessions

    _profile("voice-agent", "default")
    _profile("worker-agent", "default")
    sessions.create_session("call", "voice-agent", title="call", mode="voice", permission_mode=BYPASS)
    worker = await _local_dispatch(monkeypatch, "call")
    assert database.get_session(worker)["permission_mode"] == BYPASS
    assert await _engine_mode(worker, _first_turn(worker), tmp_path) == BYPASS


async def test_nested_dispatch_is_capped_by_the_calling_workers_ceiling(dispatch_parent, monkeypatch, tmp_path):
    """A worker dispatched at Accept Edits, later loosened, dispatches a
    grandchild: the grandchild never exceeds the worker's dispatch ceiling."""
    from app.db import database

    child = await _local_dispatch(monkeypatch, dispatch_parent("acceptEdits"))
    database.set_session_run_binding(child, permission_mode=BYPASS)
    grandchild = await _local_dispatch(monkeypatch, child)
    row = database.get_session(grandchild)
    assert (row["permission_mode"], row["permission_ceiling"]) == ("acceptEdits", "acceptEdits")
    assert await _engine_mode(grandchild, _first_turn(grandchild), tmp_path) == "acceptEdits"


async def test_local_dispatch_from_a_tightened_parent_uses_its_current_binding(dispatch_parent, monkeypatch, tmp_path):
    """The parent's last turn ran in bypass, then the owner tightened it to
    Ask: the child is dispatched at Ask, not at the stale turn's bypass."""
    from app.core import turn_manager
    from app.db import database, turns

    parent = dispatch_parent(BYPASS)
    tid = turn_manager.submit(parent, "user", prompt="go", profile_id="parent-agent",
                              overrides={"permission_mode": BYPASS}, explicit_permission=True, no_kick=True)
    turns.start_turn(tid)
    turns.settle_turn(tid, "completed")
    database.set_session_run_binding(parent, permission_mode="default")
    worker = await _local_dispatch(monkeypatch, parent)
    assert database.get_session(worker)["permission_mode"] == "default"
    assert await _engine_mode(worker, _first_turn(worker), tmp_path) == "default"


async def test_dispatched_child_tightened_later_stays_tightened(dispatch_parent, monkeypatch, tmp_path):
    from app.core import turn_scheduler
    from app.db import database, turns

    worker = await _local_dispatch(monkeypatch, dispatch_parent(BYPASS))
    first = _first_turn(worker)
    turns.start_turn(first["id"])
    turns.park_turn(first["id"], ["job"])
    database.set_session_run_binding(worker, permission_mode="default")
    wake = await turn_scheduler._enqueue_context_wake(worker, "unpark", [], ["job"], "Background work finished")
    assert await _engine_mode(worker, wake, tmp_path) == "default"


# ─── finding 5: scheduled wakes and limit resumes keep the stored ceiling ───


async def test_scheduled_wake_on_a_bypass_worker_stays_bypass(dispatch_parent, monkeypatch, tmp_path):
    from app.core import wakeup_service
    from app.db import turns

    worker = await _local_dispatch(monkeypatch, dispatch_parent(BYPASS))
    first = _first_turn(worker)
    turns.start_turn(first["id"])
    turns.settle_turn(first["id"], "completed")
    monkeypatch.setattr(wakeup_service, "_arm", lambda row, delay: None)
    row = await wakeup_service.schedule(session_id=worker, profile_id="worker-agent", project_id=None,
                                        prompt="check again", delay_seconds=0, model="haiku")
    await wakeup_service._fire(row)
    wake = turns.get_queued_turns(worker)[-1]
    assert wake["source"] == "wakeup" and wake["overrides"] == {"model": "haiku"}
    assert await _engine_mode(worker, wake, tmp_path) == BYPASS


async def test_limit_resume_on_a_bypass_worker_session_stays_bypass(dispatch_parent, monkeypatch, tmp_path):
    from app.core import limit_resume
    from app.db import turns

    worker = await _local_dispatch(monkeypatch, dispatch_parent(BYPASS))
    first = _first_turn(worker)
    turns.start_turn(first["id"])
    turns.settle_turn(first["id"], "completed")
    turn_id = await limit_resume._resume_chat(
        {"session_id": worker, "profile_id": "worker-agent", "project_id": None, "model": "haiku"},
        {"limit_key": "k", "reset_at": 0})
    assert turn_id
    assert await _engine_mode(worker, turns.get_turn(turn_id), tmp_path) == BYPASS


async def test_scheduled_wake_never_exceeds_the_stored_ceiling(dispatch_parent, monkeypatch, tmp_path):
    """The worker was dispatched at Accept Edits; a later loosening of its
    binding cannot lift a ceiling-less wake above that."""
    from app.core import wakeup_service
    from app.db import database, turns

    worker = await _local_dispatch(monkeypatch, dispatch_parent("acceptEdits"))
    first = _first_turn(worker)
    turns.start_turn(first["id"])
    turns.settle_turn(first["id"], "completed")
    database.set_session_run_binding(worker, permission_mode=BYPASS)
    monkeypatch.setattr(wakeup_service, "_arm", lambda row, delay: None)
    row = await wakeup_service.schedule(session_id=worker, profile_id="worker-agent", project_id=None,
                                        prompt="again", delay_seconds=0, model="haiku")
    await wakeup_service._fire(row)
    assert await _engine_mode(worker, turns.get_queued_turns(worker)[-1], tmp_path) == "acceptEdits"


async def test_legacy_worker_without_a_stored_ceiling_wakes_at_ask(mock_db, monkeypatch, tmp_path):
    """A worker row from before the durable ceiling (stands in for one created
    by an older version): no stored ceiling, so a ceiling-less wake is Ask."""
    from app.core import wakeup_service
    from app.db import sessions, turns

    _profile("worker-agent", BYPASS)
    sessions.create_session("old-worker", "worker-agent", title="w", permission_mode=BYPASS, is_worker=True)
    monkeypatch.setattr(wakeup_service, "_arm", lambda row, delay: None)
    monkeypatch.setattr("app.core.turn_manager._kick", lambda session_id: None)
    row = await wakeup_service.schedule(session_id="old-worker", profile_id="worker-agent", project_id=None,
                                        prompt="again", delay_seconds=0, model="haiku")
    await wakeup_service._fire(row)
    assert await _engine_mode("old-worker", turns.get_queued_turns("old-worker")[-1], tmp_path) == "default"


# ─── finding 1: StartSession takes the sender's current binding ─────────────


@pytest.fixture
def tightened_sender(mock_db, monkeypatch):
    """A chat whose owner sent a bypass turn, which parked, then tightened the
    chat to Ask; its continuation is what calls StartSession."""
    from app.core import session_orchestration as orch, turn_manager, turn_scheduler
    from app.db import database, sessions, turns

    _profile("agent", "default")
    monkeypatch.setattr(orch, "_principal", lambda sid: None)
    monkeypatch.setattr(turn_manager, "_kick", lambda session_id: None)
    from app.core.session_modes import CALLER_AUTHENTICATED

    sessions.create_session("caller", "agent", title="caller", is_active=True, permission_mode="default")
    # The owner's own send, stamped as the chat socket stamps it (#641).
    tid = turn_manager._submit_blocking("caller", "user", prompt="work", profile_id="agent",
                                        overrides={"permission_mode": BYPASS},
                                        explicit_permission=True, caller=CALLER_AUTHENTICATED)
    turns.start_turn(tid)
    turns.park_turn(tid, ["job"])
    database.set_session_run_binding("caller", permission_mode="default")
    return turn_scheduler


async def test_start_session_child_takes_the_senders_current_binding(tightened_sender, tmp_path):
    from app.core import session_orchestration as orch
    from app.db import database, turns

    wake = await tightened_sender._enqueue_context_wake("caller", "unpark", [], ["job"], "done")
    assert wake["overrides"]["permission_mode"] == BYPASS  # the stale copy the bug promoted
    turns.start_turn(wake["id"])
    result = orch._queue("caller", {"title": "Child", "prompt": "go", "request_id": "r1"}, "start")
    child = database.get_session(result["session_id"])
    assert child["permission_mode"] == "default"
    assert await _engine_mode(child["id"], turns.get_turn(result["turn_id"]), tmp_path) == "default"


async def test_remote_start_session_body_carries_the_senders_current_binding(tightened_sender):
    from app.core import session_orchestration as orch

    wake = await tightened_sender._enqueue_context_wake("caller", "unpark", [], ["job"], "done")
    from app.db import turns
    turns.start_turn(wake["id"])
    remote = {"device_id": "dev-2", "device_name": "DesktopWorker",
              "capabilities": {"start_session_permissions": 1}}
    with patch.object(orch, "_remote_worker", return_value=remote):
        planned = orch._queue("caller", {"title": "Child", "prompt": "go", "request_id": "r2",
                                         "worker": "DesktopWorker"}, "start")
    assert planned["body"]["permission_mode"] == "default"


def test_remote_start_session_from_an_unknown_callers_turn_is_refused(
    tightened_sender: Any,
) -> None:
    """#641: a send nobody vouched for keeps the chat on its ceiling, so it
    can't start a project-less code session on another worker."""
    from app.core import session_orchestration as orch, turn_manager
    from app.db import turns

    tid = turn_manager._submit_blocking("caller", "user", prompt="again", profile_id="agent")
    turns.start_turn(tid)
    remote = {"device_id": "dev-2", "device_name": "DesktopWorker",
              "capabilities": {"start_session_permissions": 1}}
    with patch.object(orch, "_remote_worker", return_value=remote), \
            pytest.raises(ValueError, match="must name a registered project"):
        orch._queue("caller", {"title": "Child", "prompt": "go", "request_id": "r4",
                               "worker": "DesktopWorker"}, "start")


async def test_start_session_child_of_an_unbound_sender_follows_its_agent(mock_db, monkeypatch, tmp_path):
    from app.core import session_orchestration as orch, turn_manager
    from app.db import database, sessions, turns

    _profile("agent", BYPASS)
    monkeypatch.setattr(orch, "_principal", lambda sid: None)
    monkeypatch.setattr(turn_manager, "_kick", lambda session_id: None)
    sessions.create_session("caller", "agent", title="caller", is_active=True)
    tid = turn_manager.submit("caller", "user", prompt="work", profile_id="agent", no_kick=True)
    turns.start_turn(tid)
    result = orch._queue("caller", {"title": "Child", "prompt": "go", "request_id": "r3"}, "start")
    child = database.get_session(result["session_id"])
    assert child["permission_mode"] is None
    assert await _engine_mode(child["id"], turns.get_turn(result["turn_id"]), tmp_path) == BYPASS


# ─── #486: a no-project chat passes its mode on (owner decision) ────────────
#
# Replaces the #415/#416 rule that started every agent a chat starts on Ask.
# Each start path, each mode the chat offers: the child's binding (and, for
# a Task or Swarm worker, its durable ceiling) is the chat's current mode.

CHAT_MODES = ["default", "auto", BYPASS]


@pytest.fixture
def chat_parent(mock_db, monkeypatch):
    """A no-project chat in ``mode``, with a running owner turn."""
    from app.core import session_orchestration as orch, turn_manager
    from app.db import sessions, turns

    _profile("chat-agent", "default")
    _profile("worker-agent", "default")
    _profile("reader", "default", allowed_tools=["Read", "Grep", "Glob"])
    monkeypatch.setattr(orch, "_principal", lambda sid: None)
    monkeypatch.setattr(turn_manager, "_kick", lambda session_id: None)

    def make(mode, **fields):
        sessions.create_session("chat-parent", "chat-agent", title="chat", is_active=True, mode="chat",
                                permission_mode=mode, **fields)
        tid = turn_manager.submit("chat-parent", "user", prompt="work", profile_id="chat-agent",
                                  explicit_permission=True, no_kick=True)
        turns.start_turn(tid)
        return "chat-parent"

    return make


@pytest.mark.parametrize("mode", CHAT_MODES)
async def test_task_worker_of_a_chat_starts_in_the_chats_mode(chat_parent, monkeypatch, tmp_path, mode):
    from app.db import database

    worker = await _local_dispatch(monkeypatch, chat_parent(mode), "reader")
    row = database.get_session(worker)
    assert (row["permission_mode"], row["permission_ceiling"]) == (mode, mode)
    assert await _engine_mode(worker, _first_turn(worker), tmp_path) == mode


@pytest.mark.parametrize("mode", CHAT_MODES)
async def test_swarm_worker_of_a_chat_starts_in_the_chats_mode(chat_parent, monkeypatch, tmp_path, mode):
    """The real Swarm tool and Task dispatcher; only the worker's run is skipped."""
    import asyncio

    from fluid.tools.registry import ToolCtx

    from app.core import provider_agent as pa, swarm, task_dispatch
    from app.db import database

    parent = chat_parent(mode)
    # The owner's subagent default, so the start does not stop to ask for one.
    from app.core import subagent_default
    subagent_default.write("sonnet")
    real_dispatch = pa.dispatch_worker
    started = []

    async def dispatch_without_run(**kwargs):
        started.append(kwargs["worker_session_id"])
        outcome = await real_dispatch(**kwargs)
        # The skipped run never reports; report as test_swarm's producer test does.
        wid = kwargs["worker_session_id"]
        await pa._notify_caller_terminal(parent, wid, wid, "completed", "report")
        return outcome

    for name, value in (("_await_worker_idle", AsyncMock(return_value=None)),
                        ("_worker_still_working", lambda sid: False),
                        ("final_turn_outcome", lambda sid: {"usage": {}, "error": None, "interrupted": False,
                                                            "terminal_found": True}),
                        ("final_assistant_text", lambda sid: "report"),
                        ("dispatch_worker", dispatch_without_run)):
        monkeypatch.setattr(pa, name, value)
    monkeypatch.setattr("app.core.turn_scheduler.on_task_terminal", AsyncMock())
    monkeypatch.setattr(task_dispatch, "_await_worker_registration", AsyncMock(return_value=True))
    monkeypatch.setattr(swarm, "persist", AsyncMock())
    swarm.coordinators.pop(parent, None)
    ctx = ToolCtx(extra={"task_dispatch": task_dispatch.make_fluid_dispatcher(parent),
                         "background_task_started": AsyncMock()})
    try:
        outcome = await swarm.make_handler(lambda: parent)(
            {"action": "start", "tasks": [{"id": "look", "agent": "reader", "prompt": "read it"}]}, ctx)
        assert not outcome.is_error, outcome
        await asyncio.wait_for(swarm.coordinators[parent].jobs[outcome.background_task.task_id], 30)
    finally:
        swarm.coordinators.pop(parent, None)
        swarm.workers.clear()
        swarm.worker_jobs.clear()
    [worker] = started
    row = database.get_session(worker)
    assert (row["permission_mode"], row["permission_ceiling"]) == (mode, mode)
    assert await _engine_mode(worker, _first_turn(worker), tmp_path) == mode


@pytest.mark.parametrize("mode", CHAT_MODES)
async def test_start_session_child_of_a_chat_starts_in_the_chats_mode(chat_parent, tmp_path, mode):
    from app.core import session_orchestration as orch
    from app.db import database, turns

    result = orch._queue(chat_parent(mode), {"title": "Child", "prompt": "go", "request_id": "c1"}, "start")
    child = database.get_session(result["session_id"])
    assert child["permission_mode"] == mode
    assert await _engine_mode(child["id"], turns.get_turn(result["turn_id"]), tmp_path) == mode


@pytest.mark.parametrize("mode", CHAT_MODES)
async def test_remote_start_session_from_a_chat_carries_the_chats_mode(chat_parent, mode):
    from app.core import session_orchestration as orch
    from app.db import projects_v2

    sender = chat_parent(mode)
    # A chat's start on another worker names a project checked out there:
    # the placement :func:`_project_placement` answers for it.
    project = projects_v2.upsert_project("folder:remote-only", name="Remote project")
    remote = {"device_id": "dev-2", "device_name": "DesktopWorker",
              "capabilities": {"start_session_permissions": 1}}
    planned = orch._queue(sender, {"title": "Child", "prompt": "go", "request_id": "c2",
                                   "worker": "DesktopWorker", "project": "Remote project"}, "start",
                          placement={"remote": remote, "project": project})
    assert planned["body"]["permission_mode"] == mode


async def test_start_session_child_of_a_chat_stays_under_its_turn_ceiling(chat_parent, tmp_path):
    """A chat in Bypass whose turn carries an Auto ceiling starts children in Auto."""
    import json

    from app.core import session_orchestration as orch
    from app.db import database, turns
    from app.db.base import get_db

    sender = chat_parent(BYPASS)
    live = turns.get_latest_turn(sender)
    with get_db() as conn:
        conn.cursor().execute("UPDATE turns SET overrides=%s WHERE id=%s", (
            json.dumps({**(live.get("overrides") or {}), "_permission_ceiling": "auto"}), live["id"]))
    result = orch._queue(sender, {"title": "Child", "prompt": "go", "request_id": "c3"}, "start")
    assert database.get_session(result["session_id"])["permission_mode"] == "auto"


async def test_task_worker_of_a_chat_stays_under_its_stored_ceiling(chat_parent, monkeypatch):
    from app.db import database

    worker = await _local_dispatch(monkeypatch, chat_parent(BYPASS, permission_ceiling="auto"), "reader")
    row = database.get_session(worker)
    assert (row["permission_mode"], row["permission_ceiling"]) == ("auto", "auto")


async def test_bypass_children_still_get_the_paid_generation_confirmation(chat_parent, monkeypatch):
    """Bypass passing down never waives the paid-generation confirmation: a
    Bypass chat's Task worker and its StartSession chat run unattended for
    generation, so a priced-above-threshold run is skipped, never submitted,
    even when the call claims it was confirmed."""
    from dataclasses import replace

    from app.core import session_orchestration as orch
    from app.core.generation_resolver import Candidate, Resolver, RunContext
    from app.core.query_options import _is_interactive_run
    from app.db import database, turns

    parent = chat_parent(BYPASS)
    worker = await _local_dispatch(monkeypatch, parent, "reader")
    started = orch._queue(parent, {"title": "Child", "prompt": "go", "request_id": "g1"}, "start")["session_id"]
    expensive = Candidate("fal", "account-a", "worker-a", "model-a", "image", True, options={},
                          price={"currency": "USD", "unit": "item", "amount": "5.00", "total": "5.00",
                                 "status": "estimated"})
    calls = []

    class Backend:
        async def catalog(self, kind, **kw):
            return [expensive]

        async def prepare(self, candidate, options, prompt):
            return replace(candidate, options=options)

        async def reserve(self, candidate, context):
            calls.append("reserve")
            return True

        async def execute(self, candidate, prompt, context):
            calls.append("execute")
            return {"success": True}

    for child in (worker, started):
        row = database.get_session(child)
        assert row["permission_mode"] == BYPASS
        turn = turns.get_latest_turn(child)
        # Built as query_options builds it for the child's turn.
        context = RunContext(session_id=child, owner=True, remembered_choice=expensive.choice(),
                             unattended=not _is_interactive_run({"turn_source": turn["source"]}, row))
        assert context.unattended
        output = await Resolver(Backend()).generate("image", {"prompt": "a cat", "confirmed": True}, context)
        assert (output["status"], output["reason"]) == ("skipped", "needs_confirmation")
        assert "confirmation_token" not in output
    # The Bypass chat itself, typed by the owner, is asked first too.
    row = database.get_session(parent)
    assert _is_interactive_run({"turn_source": "user"}, row)
    context = RunContext(session_id=parent, owner=True, unattended=False)
    output = await Resolver(Backend()).generate("image", {"prompt": "a cat", "source": "fal"}, context)
    assert output["status"] == "needs-confirmation"
    assert calls == []


# ─── finding 2: network receipt grants only a stated owner send ─────────────


@pytest.fixture
def owned_session(mock_db, monkeypatch):
    from app.db import database, sessions
    from app.network import control

    _profile("agent", "default")
    sessions.create_session("net", "agent", title="net", permission_mode="default")
    monkeypatch.setattr("app.core.turn_manager._kick", lambda session_id: None)
    monkeypatch.setattr(control, "_require_owned", lambda sid: database.get_session(sid))
    return "net"


@pytest.mark.parametrize("source, owner_send", [
    ("wakeup", False), ("wakeup", True), ("agent_message", True), ("user", False), ("queue", False),
])
async def test_received_turn_without_a_stated_owner_send_inherits(owned_session, tmp_path, source, owner_send):
    from app.db import turns
    from app.network import control

    body = {"session_id": owned_session, "source": source, "prompt": "continue",
            "overrides": {"permission_mode": BYPASS}}
    if owner_send:
        body["owner_send"] = True
    result = await control.handle("turns", body, from_device="paired-peer")
    turn = turns.get_turn(result["turn_id"])
    assert not turn["overrides"].get("_permission_explicit")
    assert await _engine_mode(owned_session, turn, tmp_path) == "default"


@pytest.mark.parametrize("source", ["user", "queue", "worktree"])
async def test_received_owner_send_keeps_its_choice(owned_session, tmp_path, source):
    from app.db import turns
    from app.network import control

    result = await control.handle("turns", {
        "session_id": owned_session, "source": source, "prompt": "hi", "owner_send": True,
        "overrides": {"permission_mode": BYPASS}}, from_device="paired-peer")
    turn = turns.get_turn(result["turn_id"])
    assert turn["overrides"]["_permission_explicit"] is True
    assert await _engine_mode(owned_session, turn, tmp_path) == BYPASS


async def test_submit_turn_states_an_owner_send_only_when_asked(monkeypatch):
    from app.network import control

    sent = []

    async def call(device_id, action, body):
        sent.append(body)
        return {"turn_id": "t"}

    monkeypatch.setattr(control, "call", call)
    await control.submit_turn("dev", "s", prompt="hi", owner_send=True)
    await control.submit_turn("dev", "s", prompt="hi")
    assert sent[0]["owner_send"] is True
    assert "owner_send" not in sent[1]


# ─── a queued owner send after the owner changed the chat ──────────────────


async def test_queued_owner_send_runs_at_ask_after_the_picker_tightens(owned_session, tmp_path):
    """A message queued in Bypass must not run in Bypass after the owner
    tightened the chat to Ask with the picker (the permission-mode route)."""
    from app.api.sessions_permissions import apply_permission_mode
    from app.core import turn_manager
    from app.db import database, turns

    database.set_session_run_binding(owned_session, permission_mode=BYPASS)
    tid = turn_manager.submit(owned_session, "queue", prompt="queued while busy", profile_id="agent",
                              overrides={"permission_mode": BYPASS}, explicit_permission=True, no_kick=True)
    assert await apply_permission_mode(owned_session, "default") == {"permission_mode": "default"}
    assert await _engine_mode(owned_session, turns.get_turn(tid), tmp_path) == "default"


async def test_queued_owner_send_runs_at_ask_after_a_later_ask_message(owned_session, tmp_path):
    """Queued in Bypass, then the owner sends another message with Ask
    picked: the earlier message runs first, and runs at Ask."""
    from app.core import turn_manager
    from app.db import turns

    first = turn_manager.submit(owned_session, "queue", prompt="A", profile_id="agent",
                                overrides={"permission_mode": BYPASS}, explicit_permission=True, no_kick=True)
    turn_manager.submit(owned_session, "queue", prompt="B", profile_id="agent",
                        overrides={"permission_mode": "default"}, explicit_permission=True, no_kick=True)
    assert await _engine_mode(owned_session, turns.get_turn(first), tmp_path) == "default"


async def test_owner_send_that_loosens_the_chat_keeps_its_choice(owned_session, tmp_path):
    from app.core import turn_manager
    from app.db import turns

    tid = turn_manager.submit(owned_session, "user", prompt="go", profile_id="agent",
                              overrides={"permission_mode": BYPASS}, explicit_permission=True, no_kick=True)
    assert await _engine_mode(owned_session, turns.get_turn(tid), tmp_path) == BYPASS
