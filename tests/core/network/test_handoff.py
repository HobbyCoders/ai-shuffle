import contextlib
import json
from unittest.mock import patch

import pytest
from app.core import checkout_transfer
from app.core.config import settings
from app.db import database, projects_v2, checkouts, runs
from app.db.base import get_db
from app.network import control, engine, handoff, tokens, transport
from tests.core.network.harness import Worker, join, converge
from tests.core.test_checkout_transfer import repositories, git


@pytest.fixture
def pair(tmp_path, repositories, monkeypatch):
    # Worker.active isolates databases and network identities. Also isolate the
    # legacy install id: the test process may inherit one explicit INSTANCE_ID.
    original_active = Worker.active

    @contextlib.contextmanager
    def active(worker):
        with original_active(worker), patch.object(settings, "instance_id", "handoff-" + worker.name):
            yield worker

    monkeypatch.setattr(Worker, "active", active)
    a, b = Worker("a", tmp_path), Worker("b", tmp_path)
    source, target = repositories
    with a.active():
        engine.create_network("Handoff test")
    with b.active():
        target_instance = settings.effective_instance_id
    join(a, b, legacy_instance_id=target_instance)
    for worker, cwd in ((a, source), (b, target)):
        with worker.active():
            project = projects_v2.upsert_project("git:github.com/example/repo", name="Repo")
            checkouts.upsert_checkout(project["id"], settings.effective_instance_id, str(cwd))
    with a.active():
        checkouts.upsert_checkout(project["id"], settings.effective_instance_id, str(source), branch="feature/only-on-worker")
        database.create_session("handoff-test", "default", title="Handoff", project_v2_id=project["id"])
        database.update_session("handoff-test", sdk_session_id="ses_source")
        with get_db() as conn:
            conn.cursor().execute("UPDATE sessions SET branch=%s WHERE id=%s", ("feature/only-on-worker", "handoff-test"))
        database.add_session_message("handoff-test", "user", "Keep this conversation")
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("INSERT INTO fluid_sessions (session_id,head_node_id,created_at) VALUES (%s,%s,%s)", ("ses_source","node1",1.0))
            cur.execute("INSERT INTO fluid_session_nodes (session_id,node_id,ts,message) VALUES (%s,%s,%s,%s)",
                        ("ses_source","node1",1.0,json.dumps({"role":"user","content":"Native history"})))
        engine.scan_all()
    converge([a,b])
    yield a,b,source,target
    a.close()
    b.close()
    engine.reset_for_tests()
    tokens.reset_for_tests()
    transport.reset_for_tests()



@pytest.mark.asyncio
async def test_deferred_handoff_returns_before_work_finishes_and_scopes_receipt(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    member = SimpleNamespace(kind="worker", status="active")
    monkeypatch.setattr(engine, "require_context", lambda: SimpleNamespace(member=lambda peer: member))
    monkeypatch.setattr(handoff, "_operations", {})
    monkeypatch.setattr(handoff, "_POLL_WAIT_S", 0.01)
    ready = asyncio.Event()

    async def work(payload, peer):
        await ready.wait()
        return {"moved": True}

    handler = AsyncMock(side_effect=work)
    monkeypatch.setattr(handoff, "_handle", handler)
    payload = {"session_id": "s1", "phase": "receive", "deferred": True}
    receipt = await asyncio.wait_for(handoff.handle(payload, "source"), 0.1)
    status = {"session_id": "s1", "phase": "status", **receipt}
    assert await handoff.handle(status, "source") == receipt
    for peer, sid in [("other-worker", "s1"), ("source", "other-session")]:
        with pytest.raises(ValueError, match="receipt expired"):
            await handoff.handle({**status, "session_id": sid}, peer)
    monkeypatch.setattr(handoff, "_POLL_WAIT_S", 10)
    waiter = asyncio.create_task(handoff.handle(status, "source"))
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert not handoff._operations[("source", "s1", receipt["operation"])].done()
    ready.set()
    await handoff._operations[("source", "s1", receipt["operation"])]
    for _ in range(2):  # Lost result replies can be read again, never re-executed.
        assert await handoff.handle(status, "source") == {"moved": True}
    handler.assert_awaited_once()


@pytest.mark.asyncio
async def test_deferred_client_retries_only_status_and_surfaces_terminal_failure(monkeypatch):
    from unittest.mock import AsyncMock
    from app.network import runners

    monkeypatch.setattr(runners, "find", lambda device: {"capabilities": {"handoff_poll": 1}})
    call = AsyncMock(side_effect=[{"operation": "op"}, control.MaybeApplied("lost status reply"),
                                 {"operation": "op"}, {"error": "dirty receiving checkout"}])
    monkeypatch.setattr(control, "call", call)
    monkeypatch.setattr(handoff.asyncio, "sleep", AsyncMock())
    with pytest.raises(ValueError, match="dirty receiving checkout"):
        await handoff._call("target", {"session_id": "s1", "phase": "receive"}, timeout=900)
    assert [c.args[2]["phase"] for c in call.await_args_list] == ["receive", "status", "status", "status"]
    assert call.await_args_list[0].args[2]["deferred"] is True


@pytest.mark.asyncio
async def test_deferred_client_never_retries_unknown_start(monkeypatch):
    from unittest.mock import AsyncMock
    from app.network import runners

    monkeypatch.setattr(runners, "find", lambda device: {"capabilities": {"handoff_poll": 1}})
    call = AsyncMock(side_effect=control.MaybeApplied("lost start reply"))
    monkeypatch.setattr(control, "call", call)
    with pytest.raises(control.MaybeApplied, match="lost start reply"):
        await handoff._call("target", {"session_id": "s1", "phase": "begin"}, timeout=600)
    call.assert_awaited_once()


def test_history_import_batches_rows_and_tolerates_an_event_mirrored_after_delete(mock_db, monkeypatch):
    from app.db import session_events

    sid = "bulk-handoff"
    database.create_session(sid, "default")
    session_events.append_events(sid, [{"type": "message.user", "payload": {"content": str(i)}}
                                      for i in range(1000)])
    data = {"sdk_session_id": None, "history": handoff._history(database.get_session(sid))}
    original_db = handoff.get_db
    inserts = []

    class Cursor:
        def __init__(self, cursor):
            self.cursor = cursor

        def execute(self, sql, params=()):
            if sql.startswith("INSERT INTO session_events "):
                if not inserts:
                    # Exactly the row the live mirror may insert between the
                    # DELETE and import. The old plain INSERT raises here.
                    self.cursor.execute(sql, params)
                inserts.append(sql)
            return self.cursor.execute(sql, params)

        def __getattr__(self, name):
            return getattr(self.cursor, name)

    @contextlib.contextmanager
    def importing_db():
        with original_db() as conn:
            class Connection:
                def cursor(self):
                    return Cursor(conn.cursor())
            yield Connection()

    monkeypatch.setattr(handoff, "get_db", importing_db)
    handoff.install_history(sid, data, ".")
    assert len(inserts) == 5  # Was 1,000 round trips; bounded to 200 rows each.
    events = session_events.get_events(sid, 0, limit=2000)
    assert len(events) == 1000
    assert events[-1]["payload"]["content"] == "999"
    assert session_events.get_max_seq(sid) == 1000


@pytest.mark.asyncio
async def test_model_receipts_move_under_the_handoff_lease_but_stale_prompts_do_not(pair, monkeypatch):
    from app.core import model_override as mo
    from app.db import interactions

    a, b, _, _ = pair
    from app.db import turns
    with a.active():
        assert mo._remember_approval("handoff-test", "opus")
        turn = turns.enqueue_turn("handoff-test", "user", prompt="use Haiku")
        with get_db() as conn:
            conn.cursor().execute("UPDATE turns SET state='completed' WHERE id=%s", (turn["id"],))
        assert await mo.confirm("handoff-test", "default", model="haiku", baseline="opus", action="start") == mo.NAMED
        launch = turns.enqueue_turn("handoff-test", "agent_message", prompt="start brief",
                                    command_id="orchestration:start:launch",
                                    payload={"orchestration_sender": "origin-chat"})
        with get_db() as conn:
            conn.cursor().execute("UPDATE turns SET state='completed' WHERE id=%s", (launch["id"],))
        # Expired but never answered: it no longer refuses the move (#1819),
        # and it stays on this worker.
        interactions.create_interaction("handoff-test", "permission", {"tool_name": "Bash"}, -1,
                                        interaction_id="live-prompt")
    phases = []

    async def call(device, action, body, **kwargs):
        assert device == a.device_id and action == "handoff"
        phases.append(body["phase"])
        with a.active():
            return await control.handle(action, body, from_device=b.device_id)

    monkeypatch.setattr(control, "call", call)
    with b.active():
        assert not mo._has_approval("handoff-test", "opus")
        await handoff.receive(database.get_session("handoff-test"))
        assert mo._has_approval("handoff-test", "opus")
        assert mo._has_named("handoff-test", "haiku")
        from app.core.session_orchestration import _start_turn
        assert _start_turn("handoff-test")["payload"]["orchestration_sender"] == "origin-chat"
        assert not interactions.get_interaction("live-prompt")
        # Re-importing the same leased snapshot cannot duplicate receipts or
        # launch markers, and cannot erase unrelated session receipts.
        database.create_session("unrelated", "default", title="Unrelated")
        assert mo._remember_approval("unrelated", "haiku")
        snapshot = {"sdk_session_id": database.get_session("handoff-test")["sdk_session_id"],
                    "history": handoff._history(database.get_session("handoff-test"))}
        for _ in range(2):
            handoff.install_history("handoff-test", snapshot, ".")
        assert mo._has_approval("unrelated", "haiku")
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) AS n FROM pending_interactions WHERE session_id='handoff-test'")
            assert cursor.fetchone()["n"] == 2
            cursor.execute("SELECT COUNT(*) AS n FROM turns WHERE session_id='handoff-test' "
                           "AND command_id LIKE 'orchestration:start:%%'")
            assert cursor.fetchone()["n"] == 1
    with a.active():
        assert interactions.get_interaction("live-prompt")["state"] == "pending"
    assert phases == ["begin", "commit"]


@pytest.mark.asyncio
async def test_receive_transfers_unpushed_dirty_branch_and_native_history(pair, monkeypatch):
    from unittest.mock import Mock
    from app.core import streaming_status
    from app.core.global_events import global_events

    busy_changed, sessions_changed, lifecycle = Mock(), Mock(), Mock()
    monkeypatch.setattr(global_events, "lifecycle", lifecycle)
    monkeypatch.setattr(streaming_status, "notify", busy_changed)
    monkeypatch.setattr(global_events, "resources_changed", sessions_changed)
    a,b,source,target = pair
    (source / "file.txt").write_text("local staged change")
    git(source,"add",".")
    (source / "new.txt").write_text("untracked work")
    before = git(source, "rev-parse", "HEAD").decode().strip()
    from app.db import session_events
    with a.active():
        session_events.append_events("handoff-test", [{"type": "message.user", "payload": {"content": "unsynced final event"}}])
    phases=[]
    commits=[]
    async def call(device, action, body, **kwargs):
        assert device == a.device_id and action == "handoff"
        phases.append(body["phase"])
        with a.active():
            if body["phase"] == "commit":
                busy_changed.reset_mock()
                sessions_changed.reset_mock()
                lifecycle.reset_mock()
            result = await control.handle(action, body, from_device=b.device_id)
            if body["phase"] == "commit":
                # Ownership refresh cannot depend on the outer receive reply.
                busy_changed.assert_called_once_with("handoff-test")
                sessions_changed.assert_called_once_with("sessions")
                lifecycle.assert_called_once_with(None, {
                    "type": "user_session_updated", "session_id": "handoff-test",
                    "updates": {"owner_device_id": b.device_id, "instance_id": b.device_id,
                                "placement": {"where": "runner:" + b.device_id}},
                }, user_type="admin")
                commits.append(body)
            return result
    monkeypatch.setattr(control, "call", call)
    with b.active():
        session=database.get_session("handoff-test")
        cwd=await handoff.receive(session)
        assert checkout_transfer.fingerprint(cwd) == checkout_transfer.fingerprint(source)
        assert checkout_transfer.fingerprint(cwd) != before
        assert (source / "new.txt").read_text() == "untracked work"
        row=database.get_session("handoff-test")
        assert row["owner_device_id"] == b.device_id
        assert row["sdk_session_id"].startswith("ses_") and row["sdk_session_id"] != "ses_source"
        with get_db() as conn:
            cur=conn.cursor()
            cur.execute("SELECT * FROM fluid_session_nodes WHERE session_id=%s",(row["sdk_session_id"],))
            assert len(cur.fetchall()) == 1
        assert database.get_session_messages("handoff-test")[0]["content"] == "Keep this conversation"
        assert session_events.get_events("handoff-test", 0)[-1]["payload"]["content"] == "unsynced final event"
        assert session_events.get_max_seq("handoff-test") == 1
    with a.active():
        assert database.get_session("handoff-test")["owner_device_id"] == b.device_id
        assert checkout_transfer.fingerprint(source) != before
    assert phases == ["begin","commit"]
    with a.active():
        lifecycle.reset_mock()
        assert handoff.finish("handoff-test", b.device_id, commits[0]) == {"committed": True}
        assert lifecycle.call_args.args[1]["updates"]["owner_device_id"] == b.device_id
    converge([a,b])
    with b.active():
        assert database.get_session("handoff-test")["owner_device_id"] == b.device_id
        assert database.get_session("handoff-test")["sdk_session_id"].startswith("ses_")


@pytest.mark.asyncio
async def test_offline_worker_never_falls_back_to_remote_ref(pair, monkeypatch):
    a,b,source,target=pair
    async def offline(*args,**kwargs):
        raise control.RunnerUnreachable("Worker offline")
    monkeypatch.setattr(control,"call",offline)
    with b.active():
        with pytest.raises(control.RunnerUnreachable):
            await handoff.receive(database.get_session("handoff-test"))
        assert database.get_session("handoff-test")["owner_device_id"] == a.device_id
        assert not git(target,"branch","--list","feature/only-on-worker").strip()


@pytest.mark.asyncio
async def test_changed_source_aborts_commit_and_keeps_owner(pair,monkeypatch):
    a,b,source,target=pair
    async def call(device,action,body,**kwargs):
        with a.active():
            if body["phase"] == "commit":
                (source/"file.txt").write_text("changed during transfer")
            return await control.handle(action,body,from_device=b.device_id)
    monkeypatch.setattr(control,"call",call)
    with b.active():
        with pytest.raises(ValueError,match="files changed"):
            await handoff.receive(database.get_session("handoff-test"))
        assert database.get_session("handoff-test")["owner_device_id"] == a.device_id
    with a.active():
        assert database.get_session("handoff-test")["owner_device_id"] == a.device_id
        with get_db() as conn:
            cur=conn.cursor()
            cur.execute("SELECT state FROM runs WHERE session_id=%s",("handoff-test",))
            assert cur.fetchone()["state"] == "released"


@pytest.mark.asyncio
async def test_fluid_symlink_outside_target_checkout_refuses_and_keeps_owner(pair, monkeypatch):
    a,b,source,target=pair
    outside = target.parent / "outside-fluid"
    outside.mkdir()
    try:
        (target / ".fluid").symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        if getattr(exc, "winerror", None) == 1314:
            pytest.skip("Windows symlink privilege is unavailable")
        raise
    phases=[]
    async def call(device,action,body,**kwargs):
        phases.append(body["phase"])
        with a.active():
            return await control.handle(action,body,from_device=b.device_id)
    monkeypatch.setattr(control,"call",call)
    with b.active():
        # A ValueError, so the resume endpoint answers 409 rather than 500.
        with pytest.raises(ValueError,match="outside the project checkout"):
            await handoff.receive(database.get_session("handoff-test"))
        assert database.get_session("handoff-test")["owner_device_id"] == a.device_id
    # The receiving path is checked before the owner is asked to commit/push.
    assert phases == []
    assert not git(source, "ls-remote", "origin", "refs/heads/feature/only-on-worker").strip()
    assert list(outside.iterdir()) == []


def test_live_source_run_blocks_snapshot(pair):
    a,b,source,target=pair
    with a.active():
        run=runs.claim_run("handoff-test",settings.effective_instance_id,300)
        assert run
        with pytest.raises(ValueError,match="busy"):
            handoff.begin("handoff-test", b.device_id, checkout_transfer.remote_identity(target))
        assert database.get_session("handoff-test")["owner_device_id"] == a.device_id

def _task_row(task_id, remote=None):
    from app.db.background_tasks import upsert_background_tasks
    upsert_background_tasks([{
        "task_id": task_id, "session_id": "handoff-test", "kind": "agent",
        "title": "Worker", "status": "running", "terminal_notified": True,
        "started_at": "2026-10-03T17:24:00+00:00", "updated_at": "2026-10-03T17:24:00+00:00",
        "remote_device_id": remote,
    }])


def test_a_stranded_task_row_does_not_pin_the_session(pair):
    """#1758: a running row whose task this worker no longer runs refused every
    hand-off; live local work and a child on another worker still do."""
    from app.core.task_registry import task_registry
    a, b, source, target = pair
    with a.active():
        _task_row("stranded")
        with get_db() as conn:
            handoff._idle(conn.cursor(), "handoff-test")
            handoff._quiet(conn.cursor(), "handoff-test")
        task_registry.start_task("handoff-test", "stranded", "agent", "Worker", tool_use_id="stranded")
        try:
            with get_db() as conn, pytest.raises(ValueError, match="background work"):
                handoff._idle(conn.cursor(), "handoff-test")
        finally:
            task_registry.clear_session("handoff-test")
        _task_row("remote-child", remote=b.device_id)
        with get_db() as conn, pytest.raises(ValueError, match="background work"):
            handoff._idle(conn.cursor(), "handoff-test")


def _owner_calls(a, b, monkeypatch):
    async def call(device, action, body, **kwargs):
        with a.active():
            return await control.handle(action, body, from_device=b.device_id)
    monkeypatch.setattr(control, "call", call)


@pytest.mark.asyncio
async def test_an_open_prompt_refuses_the_move(pair, monkeypatch):
    """#1819 rule 4: an open approval card cannot follow the chat."""
    from app.db import interactions
    a, b, _, _ = pair
    with a.active():
        interactions.create_interaction("handoff-test", "permission", {"tool_name": "Bash"}, None,
                                        interaction_id="open-card")
    _owner_calls(a, b, monkeypatch)
    with b.active():
        with pytest.raises(ValueError, match="Answer or dismiss the open prompt first"):
            await handoff.receive(database.get_session("handoff-test"))
    with a.active():
        assert database.get_session("handoff-test")["owner_device_id"] == a.device_id
        with get_db() as conn:
            assert handoff.open_prompt(conn.cursor(), "handoff-test")
        assert interactions.cancel_interaction("open-card")
        with get_db() as conn:
            assert not handoff.open_prompt(conn.cursor(), "handoff-test")


@pytest.mark.asyncio
@pytest.mark.parametrize("prompt", ["a" * 2000, "漢字" * 1000], ids=["ascii", "cjk"])
async def test_failed_held_delivery_records_bounded_note_on_receiving_worker(pair, monkeypatch, prompt):
    """#1964: submit -> durable holds -> move -> failed delivery -> receiver event."""
    from unittest.mock import AsyncMock
    from app.core import session_handoff as sh, turn_manager
    from app.db import session_events, session_moves

    a, b, _, _ = pair
    sid = "handoff-test"
    monkeypatch.setattr(sh, "_pending", {})
    monkeypatch.setattr(sh, "_workers", lambda: {"runners": [
        {"device_id": b.device_id, "device_name": "B", "online": True, "is_self": False},
    ]})
    monkeypatch.setattr(turn_manager, "_kick", lambda session_id: None)
    notify = AsyncMock()
    monkeypatch.setattr(sh, "_notify", notify)

    async def call(device, action, body, **kwargs):
        worker, sender = (a, b) if device == a.device_id else (b, a)
        assert device == worker.device_id
        with worker.active():
            return await control.handle(action, body, from_device=sender.device_id)

    monkeypatch.setattr(control, "call", call)
    with a.active():
        sh.request(sid, b.device_id)
        pending = sh.pending_for(sid)
        for _ in range(50):
            assert turn_manager._submit_blocking(sid, "user", prompt=prompt,
                                                  profile_id="default").startswith(sh.HELD_PREFIX)
        assert len(session_moves.get(sid)["held"]) == 50
    with b.active():
        await handoff.receive(database.get_session(sid))
        assert database.get_session(sid)["owner_device_id"] == b.device_id
    with a.active(), patch.object(control, "submit_turn", AsyncMock(side_effect=control.ControlRefused("refused"))):
        await sh._moved(sid, pending, b.device_id)
        assert session_moves.get(sid) is None
        # No local fallback: the row must be where the chat actually runs.
        assert session_events.get_events(sid, 0) == []
    with b.active():
        notes = [row["payload"]["data"] for row in session_events.get_events(sid, 0)
                 if row["event_type"] == "system.note" and row["payload"].get("subtype") == sh.SUBTYPE]
        assert [note["status"] for note in notes] == ["moved", "held_failed"]
        assert len(notes[-1]["content"]) == 8000 and notes[-1]["content"].endswith("…")
        assert notes[-1]["count"] == 50 and notes[-1]["messages"] == [prompt] * 50
    notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_pending_wakes_move_with_the_chat(pair, monkeypatch):
    """#1819 rule 3: a ScheduleWakeup and a limit resume run on the new worker, not here."""
    import time
    from app.core import wakeup_service
    from app.db import wakeups
    a, b, _, _ = pair
    fire_at = time.time() + 3600
    with a.active():
        wakeups.create_wakeup(id="wake-1", session_id="handoff-test", prompt="check CI", fire_at=fire_at,
                              reason="CI", instance_id=settings.effective_instance_id)
        wakeups.create_wakeup(id="wake-2", session_id="handoff-test", prompt="continue", fire_at=fire_at + 60,
                              source=wakeup_service.LIMIT_RESUME_SOURCE,
                              instance_id=settings.effective_instance_id)
    _owner_calls(a, b, monkeypatch)
    monkeypatch.setattr(handoff, "wake_carry", lambda target: True)
    armed = []
    monkeypatch.setattr(wakeup_service, "arm_persisted", lambda row: armed.append(row["id"]))
    with b.active():
        await handoff.receive(database.get_session("handoff-test"))
        moved = {row["id"]: row for row in wakeups.get_session_wakeups("handoff-test")}
        assert set(moved) == {"wake-1", "wake-2"}
        assert moved["wake-1"]["prompt"] == "check CI" and moved["wake-1"]["fire_at"] == pytest.approx(fire_at)
        assert moved["wake-2"]["source"] == wakeup_service.LIMIT_RESUME_SOURCE
        assert {row["instance_id"] for row in moved.values()} == {settings.effective_instance_id}
    assert sorted(armed) == ["wake-1", "wake-2"]
    with a.active():
        assert wakeups.get_session_wakeups("handoff-test") == []


@pytest.mark.asyncio
async def test_a_wake_an_older_worker_cannot_keep_is_named_in_the_chat(pair, monkeypatch):
    """#1819 rule 3: never drop a wake silently."""
    import time
    from app.core import wakeup_service
    from app.db import wakeups
    a, b, _, _ = pair
    with a.active():
        wakeups.create_wakeup(id="wake-old", session_id="handoff-test", prompt="check CI",
                              fire_at=time.time() + 3600, instance_id=settings.effective_instance_id)
    monkeypatch.setattr(handoff, "wake_carry", lambda target: False)
    monkeypatch.setattr(handoff, "_noted_wakes", set())
    recorded = []

    async def record(sid, event):
        recorded.append((sid, event))
    monkeypatch.setattr("app.core.turn_recorder.record_drained_event", record)
    with a.active():
        await handoff._note_uncarried_wakes("handoff-test", b.device_id)
        # A retried move does not repeat it.
        await handoff._note_uncarried_wakes("handoff-test", b.device_id)
    assert len(recorded) == 1
    sid, event = recorded[0]
    assert sid == "handoff-test" and event["subtype"] == wakeup_service.DROPPED_NOTE_SUBTYPE
    assert "the scheduled wake-up at" in event["data"]["message"]
    assert "It will not run" in event["data"]["message"]


def _wake_notes(monkeypatch):
    from app.core import wakeup_service
    recorded = []
    original = __import__("app.core.turn_recorder", fromlist=["record_drained_event"]).record_drained_event

    async def record(sid, event):
        if event.get("subtype") == wakeup_service.DROPPED_NOTE_SUBTYPE:
            recorded.append(event["data"]["message"])
        return await original(sid, event)
    monkeypatch.setattr("app.core.turn_recorder.record_drained_event", record)
    monkeypatch.setattr(handoff, "_noted_wakes", set())
    return recorded


@pytest.mark.asyncio
async def test_a_refused_move_says_nothing_about_wakes(pair, monkeypatch):
    """F9: begin refuses the move (an open prompt): every wake stays here, so no note."""
    import time
    from app.db import interactions, wakeups
    a, b, _, _ = pair
    with a.active():
        wakeups.create_wakeup(id="wake-old", session_id="handoff-test", prompt="check CI",
                              fire_at=time.time() + 3600, instance_id=settings.effective_instance_id)
        interactions.create_interaction("handoff-test", "permission", {"tool_name": "Bash"}, None,
                                        interaction_id="open-card")
    monkeypatch.setattr(handoff, "wake_carry", lambda target: False)
    notes = _wake_notes(monkeypatch)
    _owner_calls(a, b, monkeypatch)
    with b.active():
        with pytest.raises(ValueError, match="Answer or dismiss the open prompt first"):
            await handoff.receive(database.get_session("handoff-test"))
    assert notes == []
    with a.active():
        assert [row["id"] for row in wakeups.get_session_wakeups("handoff-test")] == ["wake-old"]


def test_unknown_capabilities_are_not_read_as_an_older_worker(monkeypatch):
    """F9: no runner row (or no capabilities) is unknown, not "cannot keep"."""
    from app.network import runners
    monkeypatch.setattr(runners, "find", lambda target: None)
    assert handoff.wake_carry("dev-x") is None
    monkeypatch.setattr(runners, "find", lambda target: {"capabilities": {}})
    assert handoff.wake_carry("dev-x") is None
    monkeypatch.setattr(runners, "find", lambda target: {"capabilities": {"git_resume": 1}})
    assert handoff.wake_carry("dev-x") is False
    monkeypatch.setattr(runners, "find", lambda target: {"capabilities": {"move_wakes": 1}})
    assert handoff.wake_carry("dev-x") is True


@pytest.mark.asyncio
async def test_an_unknown_receiver_gets_a_conditional_wake_note(pair, monkeypatch):
    import time
    from app.db import wakeups
    a, b, _, _ = pair
    with a.active():
        wakeups.create_wakeup(id="wake-old", session_id="handoff-test", prompt="check CI",
                              fire_at=time.time() + 3600, instance_id=settings.effective_instance_id)
    monkeypatch.setattr(handoff, "wake_carry", lambda target: None)
    notes = _wake_notes(monkeypatch)
    with a.active():
        await handoff._note_uncarried_wakes("handoff-test", b.device_id)
    assert len(notes) == 1 and "cannot tell whether" in notes[0] and "It will not run" not in notes[0]


@pytest.mark.asyncio
async def test_a_wake_created_during_the_move_refuses_the_commit(pair, monkeypatch):
    """F4: begin sent its snapshot of the wakes; one added since would be deleted
    by the commit without running anywhere. The commit is refused instead."""
    import time
    from app.db import wakeups
    a, b, _, _ = pair
    fire_at = time.time() + 3600
    with a.active():
        wakeups.create_wakeup(id="wake-1", session_id="handoff-test", prompt="check CI", fire_at=fire_at,
                              instance_id=settings.effective_instance_id)
    _owner_calls(a, b, monkeypatch)
    monkeypatch.setattr(handoff, "wake_carry", lambda target: True)
    original = handoff.install_history

    def install_history(sid, data, cwd):
        # Between begin and commit, the agent on the owner schedules a wake.
        with a.active():
            wakeups.create_wakeup(id="wake-late", session_id="handoff-test", prompt="later",
                                  fire_at=fire_at + 60, instance_id=settings.effective_instance_id)
        return original(sid, data, cwd)
    monkeypatch.setattr(handoff, "install_history", install_history)
    with b.active():
        with pytest.raises(ValueError, match="A wake-up of this chat changed during the move"):
            await handoff.receive(database.get_session("handoff-test"))
        assert wakeups.get_session_wakeups("handoff-test") == []
    with a.active():
        assert database.get_session("handoff-test")["owner_device_id"] == a.device_id
        assert sorted(row["id"] for row in wakeups.get_session_wakeups("handoff-test")) == ["wake-1", "wake-late"]


@pytest.mark.asyncio
@pytest.mark.parametrize("projectless", [False, True], ids=["project", "plain-chat"])
@pytest.mark.parametrize("case", ["before_commit", "after_commit", "lost_commit", "lost_finalize",
                                  "source_restart", "receiver_restart", "no_message", "removed_message",
                                  "receiver_restart_after_finalize", "receiver_message", "held_peer"])
async def test_moved_wake_waits_for_durable_human_supersession(pair, monkeypatch, projectless, case):
    """#1963: a due wake cannot beat a held message, even across lost replies/boot."""
    import time
    from unittest.mock import AsyncMock
    from app.core import session_handoff as sh, turn_manager, turn_scheduler, wakeup_service
    from app.db import session_moves, turns, wakeups

    a, b, _, _ = pair
    sid = "handoff-test"
    if projectless:
        for worker in (a, b):
            with worker.active(), get_db() as conn:
                conn.cursor().execute("UPDATE sessions SET project_v2_id=NULL,branch=NULL WHERE id=%s", (sid,))
    pending = sh.PendingHandoff(b.device_id, "B", "", time.time(), by="owner")
    monkeypatch.setattr(sh, "_pending", {})
    monkeypatch.setattr(sh, "_moving", {sid: pending})
    monkeypatch.setattr(sh, "_delivering", set())
    monkeypatch.setattr(turn_manager, "_kick", lambda session_id: None)
    monkeypatch.setattr(turn_scheduler, "kick", lambda session_id: None)
    monkeypatch.setattr(sh, "_notify", AsyncMock())
    monkeypatch.setattr(sh, "_resend_safe", lambda target: True)
    monkeypatch.setattr(sh, "FORWARD_RETRY_S", 0)
    armed = []
    monkeypatch.setattr(wakeup_service, "arm_persisted", lambda row: armed.append(row))
    with a.active():
        session_moves.save(sid, target_id=b.device_id, label="B", note="", requested_by="owner",
                           requested_at=pending.requested_at, expires_at=pending.expires_at)
        wakeups.create_wakeup(id="wake-1", session_id=sid, prompt="stale wake", profile_id="default",
                              fire_at=time.time() + 90, instance_id=settings.effective_instance_id)
        if case == "held_peer":
            wakeups.create_wakeup(id="wake-peer", session_id=sid, prompt="peer instruction", profile_id="default",
                                  source=wakeup_service.HELD_PEER_SOURCE, fire_at=time.time() + 90,
                                  instance_id=settings.effective_instance_id)

    def speak():
        message_id = turn_manager._submit_blocking(sid, "user", prompt="new human instruction", profile_id="default")
        assert message_id.startswith(sh.HELD_PREFIX)
        assert session_moves.get(sid)["wakes_superseded"]
        if case == "removed_message":
            assert sh.drop_held(sid, message_id)
            assert session_moves.get(sid)["wakes_superseded"]

    commits = []
    finalizes = []
    async def call(device, action, body, **kwargs):
        worker, sender = (a, b) if device == a.device_id else (b, a)
        with worker.active():
            if action == "handoff" and body["phase"] == "commit":
                commits.append(body)
                if case not in ("no_message", "after_commit", "receiver_restart_after_finalize", "receiver_message"):
                    speak()
                result = await control.handle(action, body, from_device=sender.device_id)
                if case == "after_commit":
                    speak()
                if case == "lost_commit":
                    raise TimeoutError("commit response lost after ownership changed")
                return result
            if action == "handoff" and body["phase"] == "finalize-wakes":
                if body.get("release"):
                    finalizes.append(body)
                if case == "receiver_restart_after_finalize" and body.get("release") and len(finalizes) == 1:
                    # Receiver died after the DB transaction, before arming or
                    # returning. Boot must recover the real published rows.
                    handoff._finalize_wakes(sid, sender.device_id, body)
                    monkeypatch.setattr(wakeup_service, "_arm", lambda row, delay: armed.append(row))
                    assert await wakeup_service.rehydrate_pending() == 1
                    raise TimeoutError("receiver restarted before arming")
                result = await control.handle(action, body, from_device=sender.device_id)
                if case == "lost_finalize" and body.get("release") and len(finalizes) == 1:
                    raise TimeoutError("finalize response lost after receiver transaction committed")
                return result
            return await control.handle(action, body, from_device=sender.device_id)
    monkeypatch.setattr(control, "call", call)
    # The move outlasts the original 90-second timer. Boot must still see zero
    # executable wakes, not arm one ahead of supersession.
    monkeypatch.setattr(wakeup_service, "_now_epoch", lambda: time.time() + 120)
    with b.active():
        if case == "lost_commit":
            with pytest.raises(TimeoutError, match="commit response lost"):
                await handoff.receive(database.get_session(sid))
        else:
            await handoff.receive(database.get_session(sid))
        assert armed == []
        assert wakeups.get_session_wakeups(sid) == []
        assert await wakeup_service.rehydrate_pending() == 0
        if case == "receiver_restart":
            # No process-local state is needed to keep staged wakes dormant.
            monkeypatch.setattr(wakeup_service, "_timers", {})
            assert await wakeup_service.rehydrate_pending() == 0
        if case == "receiver_message":
            pending.holding = False
            turn_manager._submit_blocking(sid, "user", prompt="new human instruction", profile_id="default")
    with a.active():
        assert database.get_session(sid)["owner_device_id"] == b.device_id
        assert wakeups.get_session_wakeups(sid) == []
        if case == "source_restart":
            sh._moving.clear()
            spawned = []
            monkeypatch.setattr(sh, "_spawn", spawned.append)
            assert await sh.resume_queued() == 0
            assert len(spawned) == 1
            await spawned[0]
        else:
            await sh._moved(sid, pending, b.device_id)
        assert session_moves.get(sid) is None
    with b.active():
        queued = turns.get_queued_turns(sid)
        if case in ("no_message", "receiver_restart_after_finalize"):
            assert {row["prompt"] for row in armed} == {"stale wake"}
            await wakeup_service._fire(armed[0])
            assert [row["prompt"] for row in turns.get_queued_turns(sid)] == ["stale wake"]
        else:
            expected = [] if case == "removed_message" else ["new human instruction"]
            assert [row["prompt"] for row in queued] == expected
            if case == "held_peer":
                assert [row["prompt"] for row in armed] == ["peer instruction"]
                await wakeup_service._fire(armed[0])
                promoted = turns.get_queued_turns(sid)
                assert [row["prompt"] for row in promoted] == [
                    "new human instruction", turn_scheduler._held_peer_block("peer instruction", promoted=True),
                ]
                assert [row["source"] for row in promoted] == ["user", "promotion"]
            else:
                assert armed == []
        assert wakeups.get_session_wakeups(sid) == []
        # A retry after the row fired / was cancelled cannot resurrect it.
        assert await control.handle("handoff", finalizes[-1], from_device=a.device_id) == {"finalized": True}
        assert wakeups.get_session_wakeups(sid) == []
    assert len(finalizes) == (2 if case in ("lost_finalize", "receiver_restart_after_finalize") else 1)



@pytest.mark.asyncio
async def test_finalization_retry_does_not_cancel_a_claimed_real_timer(pair, monkeypatch):
    import asyncio
    import time
    from unittest.mock import AsyncMock
    from app.core import turn_manager, wakeup_service
    from app.db import session_moves, wakeups

    a, b, _, _ = pair
    sid = "handoff-test"
    for worker in (a, b):
        with worker.active(), get_db() as conn:
            conn.cursor().execute("UPDATE sessions SET project_v2_id=NULL,branch=NULL WHERE id=%s", (sid,))
    with a.active():
        session_moves.save(sid, target_id=b.device_id, label="B", note="", requested_by="owner",
                           requested_at=time.time(), expires_at=time.time() + 600)
        wakeups.create_wakeup(id="wake-real", session_id=sid, prompt="run exactly once",
                              profile_id="default", fire_at=time.time() - 1)
    async def call(device, action, body, **kwargs):
        with a.active():
            return await control.handle(action, body, from_device=b.device_id)
    monkeypatch.setattr(control, "call", call)
    monkeypatch.setattr(wakeup_service, "_timers", {})
    monkeypatch.setattr(wakeup_service, "_retry_attempts", {})
    claimed = asyncio.Event()
    finish_lookup = asyncio.Event()
    async def profile_after_claim(row):
        assert wakeups.get_session_wakeups(sid) == []
        claimed.set()
        await finish_lookup.wait()
        return "default"
    monkeypatch.setattr(wakeup_service, "_resolve_profile_id", profile_after_claim)
    monkeypatch.setattr(wakeup_service, "_session_api_user_id", lambda sid: None)
    submitted = AsyncMock(return_value="wake-turn")
    monkeypatch.setattr(turn_manager, "submit_async", submitted)
    with b.active():
        await handoff.receive(database.get_session(sid))
        token = session_moves.get(sid)  # Source move evidence is not replicated.
        assert token is None
        token = b.sql("SELECT token FROM handoff_wakes WHERE session_id=?", (sid,))[0]["token"]
        body = {"session_id": sid, "phase": "finalize-wakes", "token": token, "release": True}
        row = handoff._finalize_wakes(sid, a.device_id, body)[0]
        wakeup_service.arm_persisted(row)  # Real _arm and _fire; no collected fake timers.
        original = wakeup_service._timers["wake-real"]
        # Capture the retry's DB snapshot BEFORE the first timer claims it.
        snapshot = handoff._finalize_wakes(sid, a.device_id, body)
        real_run_sync = handoff.run_sync
        async def delayed_snapshot(fn, *args, **kwargs):
            if fn is handoff._finalize_wakes:
                await asyncio.wait_for(claimed.wait(), 2)
                return snapshot
            return await real_run_sync(fn, *args, **kwargs)
        monkeypatch.setattr(handoff, "run_sync", delayed_snapshot)
        try:
            assert await control.handle("handoff", body, from_device=a.device_id) == {"finalized": True}
            assert wakeup_service._timers["wake-real"] is original
            assert not original.cancelling()
            finish_lookup.set()
            await asyncio.wait_for(original, 2)
            submitted.assert_awaited_once()
            assert submitted.await_args.kwargs["prompt"] == "run exactly once"
            assert wakeups.get_session_wakeups(sid) == []
        finally:
            finish_lookup.set()
            for task in list(wakeup_service._timers.values()):
                task.cancel()
            await asyncio.gather(*wakeup_service._timers.values(), return_exceptions=True)


@pytest.mark.asyncio
async def test_successive_move_waits_for_original_reservation_and_late_finalize_cannot_retake(pair, tmp_path, monkeypatch):
    import time
    from unittest.mock import AsyncMock
    from app.core import session_handoff as sh, turn_manager, wakeup_service
    from app.db import session_moves, turns, wakeups

    a, b, _, _ = pair
    c = Worker("c", tmp_path)
    sid = "handoff-test"
    try:
        with c.active():
            instance = settings.effective_instance_id
        join(a, c, legacy_instance_id=instance)
        converge([a, b, c])
        for worker in (a, b, c):
            with worker.active(), get_db() as conn:
                conn.cursor().execute("UPDATE sessions SET project_v2_id=NULL,branch=NULL WHERE id=%s", (sid,))
        monkeypatch.setattr(sh, "_pending", {})
        monkeypatch.setattr(sh, "_moving", {})
        monkeypatch.setattr(sh, "_waiting", {})
        monkeypatch.setattr(sh, "_running", set())
        monkeypatch.setattr(sh, "_delivering", set())
        monkeypatch.setattr(sh, "_notify", AsyncMock())
        monkeypatch.setattr(sh, "_resend_safe", lambda target: True)
        monkeypatch.setattr(turn_manager, "_kick", lambda sid: None)
        destinations = []
        workers = {worker.device_id: worker for worker in (a, b, c)}
        async def call(device, action, body, **kwargs):
            sender = engine.require_context().device_id
            if body.get("phase") == "finalize-wakes":
                destinations.append(device)
            with workers[device].active():
                return await control.handle(action, body, from_device=sender)
        monkeypatch.setattr(control, "call", call)
        with a.active():
            session_moves.save(sid, target_id=b.device_id, label="B", note="", requested_by="owner",
                               requested_at=time.time(), expires_at=time.time() + 600)
            session_moves.append_held(sid, {"id": "held-first", "source": "user", "prompt": "human first",
                                            "profile_id": "default"})
            wakeups.create_wakeup(id="wake-successive", session_id=sid, prompt="stale wake",
                                  profile_id="default", fire_at=time.time() - 1)
        with b.active():
            await handoff.receive(database.get_session(sid))
            assert await wakeup_service.rehydrate_pending() == 0
            with pytest.raises(ValueError, match="previous move"):
                sh.request(sid, c.device_id)
            with pytest.raises(ValueError, match="previous move"):
                await sh.queue_move(sid, c.device_id)
        # Source crashes before finalization, while C sees B's new ownership.
        converge([a, b, c])
        with c.active():
            with pytest.raises(ValueError, match="previous move"):
                await handoff.receive(database.get_session(sid))
            assert database.get_session(sid)["owner_device_id"] == b.device_id
        with a.active():
            token = session_moves.get(sid)["wake_token"]
            # Even if recovery passes a different replicated owner, the token
            # is routed to its durable destination B, never to C.
            await sh._finalize_moved_wakes(sid, c.device_id, release=False)
            assert destinations == [b.device_id]
            spawned = []
            monkeypatch.setattr(sh, "_spawn", spawned.append)
            assert await sh.resume_queued() == 0
            assert len(spawned) == 1
            await spawned[0]
            assert session_moves.get(sid) is None
        with b.active():
            assert [row["prompt"] for row in turns.get_queued_turns(sid)] == ["human first"]
            assert wakeups.get_session_wakeups(sid) == []
            with get_db() as conn:
                conn.cursor().execute("UPDATE turns SET state='completed' WHERE session_id=%s", (sid,))
        converge([a, b, c])
        with c.active():
            await handoff.receive(database.get_session(sid))  # B -> C now succeeds.
            assert database.get_session(sid)["owner_device_id"] == c.device_id
        body = {"session_id": sid, "phase": "finalize-wakes", "token": token, "release": True}
        with b.active():
            assert await control.handle("handoff", body, from_device=a.device_id) == {"finalized": True}
            assert database.get_session(sid)["owner_device_id"] == c.device_id
            # Also reject a stale unfinished receipt from an older/bypassed move.
            with get_db() as conn:
                conn.cursor().execute("UPDATE handoff_wakes SET finalized=FALSE WHERE token=%s", (token,))
            with pytest.raises(ValueError, match="earlier move"):
                await control.handle("handoff", body, from_device=a.device_id)
            assert database.get_session(sid)["owner_device_id"] == c.device_id
    finally:
        c.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("lost_cancel_reply", [False, True])
async def test_aborted_wake_reservation_does_not_block_a_later_completed_move(pair, monkeypatch, lost_cancel_reply):
    import time
    from unittest.mock import AsyncMock
    from app.core import session_handoff as sh, turn_manager, wakeup_service
    from app.db import session_moves, wakeups

    a, b, _, _ = pair
    sid = "handoff-test"
    for worker in (a, b):
        with worker.active(), get_db() as conn:
            conn.cursor().execute("UPDATE sessions SET project_v2_id=NULL,branch=NULL WHERE id=%s", (sid,))
    with a.active():
        session_moves.save(sid, target_id=b.device_id, label="B", note="", requested_by="owner",
                           requested_at=time.time(), expires_at=time.time() + 600)
        wakeups.create_wakeup(id="wake-aborted", session_id=sid, prompt="later",
                              profile_id="default", fire_at=time.time() + 3600)
    reject_commit = True
    workers = {worker.device_id: worker for worker in (a, b)}
    async def call(device, action, body, **kwargs):
        sender = engine.require_context().device_id
        if body.get("phase") == "commit" and reject_commit:
            raise ValueError("injected refusal before commit")
        with workers[device].active():
            result = await control.handle(action, body, from_device=sender)
        if body.get("phase") == "cancel" and lost_cancel_reply:
            raise ValueError("cancel reply lost")
        return result
    monkeypatch.setattr(control, "call", call)
    monkeypatch.setattr(sh, "_notify", AsyncMock())
    monkeypatch.setattr(sh, "_resend_safe", lambda target: True)
    monkeypatch.setattr(turn_manager, "_kick", lambda sid: None)
    monkeypatch.setattr(wakeup_service, "arm_persisted", lambda row: None)
    with b.active():
        with pytest.raises(ValueError, match="injected refusal"):
            await handoff.receive(database.get_session(sid))
        old = b.sql("SELECT * FROM handoff_wakes WHERE session_id=?", (sid,))[0]
        assert bool(old["finalized"]) is not lost_cancel_reply
        assert database.get_session(sid)["owner_device_id"] == a.device_id
    reject_commit = False
    with b.active():
        await handoff.receive(database.get_session(sid))
    with a.active():
        pending = sh.PendingHandoff(b.device_id, "B", "", time.time(), by="owner")
        await sh._moved(sid, pending, b.device_id)
        assert session_moves.get(sid) is None
    with b.active():
        assert all(row["finalized"] for row in b.sql("SELECT finalized FROM handoff_wakes WHERE session_id=?", (sid,)))
        handoff.check_unfinished_wakes(sid)
        wakeup_service.cancel_session_wakeups(sid)
    with a.active():
        await handoff.receive(database.get_session(sid))  # B -> A is not stranded by the aborted import.
        assert database.get_session(sid)["owner_device_id"] == a.device_id


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["offline", "online_reset", "outbound_reset", "poll_contact", "late_contact", "abandon_discard", "abandon_resume", "revoked"])
async def test_incoming_transfer_recovers_without_the_source(pair, monkeypatch, scenario):
    from types import SimpleNamespace
    from app.api import network as network_api
    from app.core import wakeup_service
    from app.db import session_moves, wakeups
    from app.network import hub

    a, b, _, _ = pair
    sid = "handoff-test"
    clock = [1000.0]
    monkeypatch.setattr(handoff, "time", SimpleNamespace(time=lambda: clock[0]))
    for worker in (a, b):
        with worker.active(), get_db() as conn:
            conn.cursor().execute("UPDATE sessions SET project_v2_id=NULL,branch=NULL WHERE id=%s", (sid,))
    with a.active():
        session_moves.save(sid, target_id=b.device_id, label="B", note="", requested_by="owner",
                           requested_at=clock[0], expires_at=clock[0] + 600)
        session_moves.append_held(sid, {"id": "held-on-lost-source", "source": "user",
                                      "prompt": "human instruction still on A", "profile_id": "default"})
        wakeups.create_wakeup(id="wake-abandon", session_id=sid, prompt="possibly superseded",
                              profile_id="default", fire_at=clock[0] - 1)
    async def call(device, action, body, **kwargs):
        with a.active():
            return await control.handle(action, body, from_device=b.device_id)
    monkeypatch.setattr(control, "call", call)
    armed = []
    monkeypatch.setattr(wakeup_service, "arm_persisted", armed.append)
    with b.active():
        await handoff.receive(database.get_session(sid))
        async def disappeared(*args, **kwargs):
            raise control.RunnerUnreachable("source permanently gone")
        monkeypatch.setattr(control, "call", disappeared)
        online = set()
        monkeypatch.setattr(transport, "online_devices", lambda: online)
        info = handoff.transfer_recovery_status()["pending"][0]
        token = info["token"]
        assert not info["can_abandon"]
        with pytest.raises(ValueError, match="five minutes"):
            handoff.abandon_transfer(token)
        if scenario == "poll_contact":
            from contextlib import contextmanager
            original_db = handoff.get_db
            contact = [False]
            class Cursor:
                def __init__(self, cursor):
                    self.cursor = cursor
                def __getattr__(self, name):
                    return getattr(self.cursor, name)
                def fetchall(self):
                    rows = self.cursor.fetchall()  # Status has captured the old timestamp.
                    if not contact[0]:
                        contact[0] = True
                        clock[0] = 1299.0
                        transport.note_contact(a.device_id)  # Real contact commits before status UPDATE.
                    return rows
            class Connection:
                def __init__(self, conn):
                    self.conn = conn
                def cursor(self):
                    return Cursor(self.conn.cursor())
            @contextmanager
            def interleaved_db():
                with original_db() as conn:
                    yield Connection(conn)
            clock[0] = 1298.0
            with monkeypatch.context() as scoped:
                scoped.setattr(handoff, "get_db", interleaved_db)
                assert not handoff.transfer_recovery_status()["pending"][0]["can_abandon"]
            assert contact[0]
            with get_db() as conn:
                cur = conn.cursor()
                cur.execute("SELECT offline_since FROM handoff_wakes WHERE token=%s", (token,))
                assert cur.fetchone()["offline_since"] == 1299.0
            # The presence cache has expired; only 101 seconds have elapsed
            # since contact. Neither status nor locked abandonment may use 0.
            clock[0] = 1400.0
            monkeypatch.setattr(transport, "_last_contact", {})
            assert not handoff.transfer_recovery_status()["pending"][0]["can_abandon"]
            with pytest.raises(ValueError, match="five minutes"):
                handoff.abandon_transfer(token, release_wakes=True)
            assert wakeups.get_session_wakeups(sid) == []
            assert armed == []
            return
        if scenario in ("online_reset", "outbound_reset"):
            # Devices is closed: a real authenticated transport contact must
            # reset the durable clock without anyone reading recovery status.
            clock[0] += 200
            if scenario == "outbound_reset":
                from unittest.mock import AsyncMock
                async def synced(*args):
                    # Applying membership during sync can invalidate context;
                    # contact persistence must not depend on a warm cache.
                    engine.reset_for_tests()
                    return {"ok": True}
                link = SimpleNamespace(device_id=a.device_id, endpoints=["http://peer"],
                                       base_url="http://peer", active_url="http://peer", backoff_until=0, failures=0,
                                       contact_kick_used=False, sync_once=AsyncMock(side_effect=synced))
                monkeypatch.setattr(transport, "_links", {a.device_id: link})
                monkeypatch.setattr(transport, "_announce_own_endpoints_if_changed", AsyncMock())
                monkeypatch.setattr(transport, "_refresh_links", AsyncMock())
                assert await transport.sync_pass(None) == {a.device_id: {"ok": True}}
            else:
                transport.note_contact(a.device_id)
            clock[0] += 100
            assert not handoff.transfer_recovery_status()["pending"][0]["can_abandon"]
            with pytest.raises(ValueError, match="five minutes"):
                handoff.abandon_transfer(token)
            # Persisted contact evidence also survives a transport restart.
            monkeypatch.setattr(transport, "_last_contact", {})
            clock[0] += 199
            assert not handoff.transfer_recovery_status()["pending"][0]["can_abandon"]
            clock[0] += 1
            assert handoff.transfer_recovery_status()["pending"][0]["can_abandon"]
            return
        clock[0] += handoff.ABANDON_AFTER_S - 1
        assert not handoff.transfer_recovery_status()["pending"][0]["can_abandon"]
        clock[0] += 1
        assert handoff.transfer_recovery_status()["pending"][0]["can_abandon"]
        assert wakeups.get_session_wakeups(sid) == []
        assert armed == []  # A timeout alone NEVER releases a possibly stale wake.
        if scenario == "offline":
            with pytest.raises(ValueError, match="previous move"):
                handoff.check_unfinished_wakes(sid)
            return
        if scenario == "late_contact":
            finalize = handoff._finalize_wakes
            def contact_before_lock(*args, **kwargs):
                transport.note_contact(a.device_id)
                return finalize(*args, **kwargs)
            monkeypatch.setattr(handoff, "_finalize_wakes", contact_before_lock)
            with pytest.raises(ValueError, match="five minutes"):
                handoff.abandon_transfer(token, release_wakes=True)
            assert wakeups.get_session_wakeups(sid) == []
            assert armed == []
            assert handoff.transfer_recovery_status()["pending"]
            return
        if scenario == "revoked":
            monkeypatch.setattr(hub, "notify_revoked", lambda device: None)
            engine.revoke_device(a.device_id)  # Real membership transition/notification; A never returns.
            assert handoff.transfer_recovery_status()["pending"] == []
            assert handoff.transfer_recovery_status()["history"][0]["reason"] == "source_revoked"
            assert wakeups.get_session_wakeups(sid) == []
            assert armed == []
        else:
            release = scenario == "abandon_resume"
            result = await network_api.abandon_handoff(token, network_api.AbandonTransfer(release_wakes=release), _="owner")
            assert result["released_wakes"] == int(release)
            assert len(armed) == int(release)
            assert len(wakeups.get_session_wakeups(sid)) == int(release)
            reason = "owner_resumed_wakes" if release else "owner_discarded_wakes"
            assert handoff.transfer_recovery_status()["history"][0]["reason"] == reason
        assert database.get_session(sid)["owner_device_id"] == b.device_id
        handoff.check_unfinished_wakes(sid)  # Recovery opens the move gate locally.
        assert handoff.transfer_recovery_status()["pending"] == []
        # A late completion cannot resurrect discarded wakes or undo the audit.
        if scenario != "revoked":
            handoff._finalize_wakes(sid, a.device_id, {"token": token, "release": True})
            assert len(wakeups.get_session_wakeups(sid)) == int(scenario == "abandon_resume")


@pytest.mark.asyncio
async def test_old_receiver_cannot_arm_a_queued_moves_wakes_early(pair, monkeypatch):
    import time
    from app.db import session_moves, wakeups

    a, b, _, _ = pair
    sid = "handoff-test"
    with a.active():
        session_moves.save(sid, target_id=b.device_id, label="B", note="", requested_by="owner",
                           requested_at=time.time(), expires_at=time.time() + 600)
        wakeups.create_wakeup(id="wake-old", session_id=sid, prompt="later", fire_at=time.time() + 90)
    async def call(device, action, body, **kwargs):
        with a.active():
            body = {k: v for k, v in body.items() if k != "wake_barrier"}
            return await control.handle(action, body, from_device=b.device_id)
    monkeypatch.setattr(control, "call", call)
    with b.active():
        with pytest.raises(ValueError, match="Update both workers"):
            await handoff.receive(database.get_session(sid))
        assert wakeups.get_session_wakeups(sid) == []
    with a.active():
        assert database.get_session(sid)["owner_device_id"] == a.device_id
        assert [row["id"] for row in wakeups.get_session_wakeups(sid)] == ["wake-old"]


@pytest.mark.asyncio
async def test_projectless_session_transfers_history_only(pair, monkeypatch):
    a,b,source,target=pair
    for worker in (a,b):
        with worker.active(), get_db() as conn:
            conn.cursor().execute("UPDATE sessions SET project_v2_id=NULL,branch=NULL WHERE id=%s",("handoff-test",))
    async def call(device,action,body,**kwargs):
        with a.active():
            return await control.handle(action,body,from_device=b.device_id)
    monkeypatch.setattr(control,"call",call)
    monkeypatch.setattr(checkout_transfer, "export_checkout",
                        lambda *a, **k: pytest.fail("plain chat must not export a checkout"))
    with b.active():
        cwd = await handoff.receive(database.get_session("handoff-test"))
        assert cwd == str(settings.effective_workspace_dir)
        assert database.get_session("handoff-test")["owner_device_id"] == b.device_id
    with a.active():
        assert database.get_session("handoff-test")["owner_device_id"] == b.device_id

@pytest.mark.asyncio
async def test_dirty_default_branch_releases_holds_without_moving_owner(pair, monkeypatch):
    a, b, source, _ = pair
    _base_session_on_clean_main(pair, monkeypatch)
    (source / "other-session.txt").write_text("keep untracked")
    before = git(source, "rev-parse", "HEAD")
    with b.active():
        with pytest.raises(ValueError, match="default branch main.*task branch.*Ownership has not moved"):
            await handoff.receive(database.get_session("handoff-test"))
        assert database.get_session("handoff-test")["owner_device_id"] == a.device_id
    with a.active(), get_db() as conn:
        assert database.get_session("handoff-test")["owner_device_id"] == a.device_id
        cur = conn.cursor()
        cur.execute("SELECT state FROM runs WHERE session_id=%s", ("handoff-test",))
        assert cur.fetchone()["state"] == "released"
        cur.execute("SELECT 1 FROM runs WHERE runner_id LIKE %s AND state='active'", (handoff.HOLD + "%",))
        assert cur.fetchone() is None
    assert git(source, "rev-parse", "HEAD") == before
    assert git(source, "status", "--porcelain").strip() == b"?? other-session.txt"


@pytest.mark.asyncio
async def test_push_failure_releases_reservation_without_moving_owner(pair, monkeypatch):
    a, b, source, target = pair
    original_git = checkout_transfer.git
    def fail_push(cwd, *args):
        if args[0] == "push":
            raise ValueError("Git push failed during resume")
        return original_git(cwd, *args)
    monkeypatch.setattr(checkout_transfer, "git", fail_push)
    async def call(device, action, body, **kwargs):
        with a.active():
            return await control.handle(action, body, from_device=b.device_id)
    monkeypatch.setattr(control, "call", call)
    (source / "new.txt").write_text("saved despite push failure")
    with b.active():
        with pytest.raises(ValueError, match="push failed"):
            await handoff.receive(database.get_session("handoff-test"))
        assert database.get_session("handoff-test")["owner_device_id"] == a.device_id
    with a.active(), get_db() as conn:
        assert database.get_session("handoff-test")["owner_device_id"] == a.device_id
        cur = conn.cursor()
        cur.execute("SELECT state FROM runs WHERE session_id=%s", ("handoff-test",))
        assert cur.fetchone()["state"] == "released"
    assert not git(source, "status", "--porcelain")


@pytest.mark.asyncio
async def test_missing_origin_refuses_before_contacting_owner(pair, monkeypatch):
    a, b, source, target = pair
    git(target, "remote", "remove", "origin")
    from unittest.mock import AsyncMock
    call = AsyncMock()
    monkeypatch.setattr(control, "call", call)
    with b.active():
        with pytest.raises(ValueError):
            await handoff.receive(database.get_session("handoff-test"))
        assert database.get_session("handoff-test")["owner_device_id"] == a.device_id
    call.assert_not_called()


def test_legacy_receiver_cannot_trigger_publication(pair):
    a, b, source, target = pair
    before = git(source, "rev-parse", "HEAD")
    with a.active():
        with pytest.raises(ValueError, match="Update both"):
            handoff.begin("handoff-test", b.device_id)
    assert git(source, "rev-parse", "HEAD") == before

@pytest.mark.asyncio
@pytest.mark.parametrize("reuse_base", [False, True])
async def test_base_session_without_branch_metadata_resolves_after_resume(pair, monkeypatch, reuse_base):
    from app.core import project_resolver
    a, b, source, target = pair
    if reuse_base:
        git(source, "checkout", "main")
        (source / "file.txt").write_text("new base checkout work")
        git(source, "add", "file.txt")
        git(source, "commit", "-m", "published base checkout work")
        git(source, "push", "origin", "main")
    # The shared fixture registers this path as a branch checkout. A base
    # session instead has a branch-NULL checkout row at that path.
    with a.active():
        project_id = database.get_session("handoff-test")["project_v2_id"]
        checkouts.upsert_checkout(project_id, settings.effective_instance_id, str(source), branch="")
    for worker in (a, b):
        with worker.active(), get_db() as conn:
            conn.cursor().execute("UPDATE sessions SET branch=NULL WHERE id=%s", ("handoff-test",))
    async def call(device, action, body, **kwargs):
        with a.active():
            return await control.handle(action, body, from_device=b.device_id)
    monkeypatch.setattr(control, "call", call)
    with b.active():
        cwd = await handoff.receive(database.get_session("handoff-test"))
        assert checkout_transfer.fingerprint(cwd) == checkout_transfer.fingerprint(source)
        row = database.get_session("handoff-test")
        assert row["branch"] == (None if reuse_base else "feature/only-on-worker")
        assert project_resolver.has_local_base_checkout(row)
        assert project_resolver.resolve_existing_cwd(row) == cwd
        if reuse_base:
            assert cwd == str(target)
            assert row["worktree_branch"] is None
    with a.active():
        assert database.get_session("handoff-test")["branch"] == (None if reuse_base else "feature/only-on-worker")


def _base_session_on_clean_main(pair, monkeypatch):
    """The owner's base-checkout session on a clean main that matches origin."""
    a, b, source, _target = pair
    git(source, "checkout", "main")
    with a.active():
        project_id = database.get_session("handoff-test")["project_v2_id"]
        checkouts.upsert_checkout(project_id, settings.effective_instance_id, str(source), branch="")
    for worker in (a, b):
        with worker.active(), get_db() as conn:
            conn.cursor().execute("UPDATE sessions SET branch=NULL WHERE id=%s", ("handoff-test",))

    async def call(device, action, body, **kwargs):
        with a.active():
            return await control.handle(action, body, from_device=b.device_id)
    monkeypatch.setattr(control, "call", call)


@pytest.mark.asyncio
async def test_continue_here_clean_main_with_receiver_work_already_merged(pair, monkeypatch):
    """Owner report 2026-10-03: Continue here on a session bound to a clean,
    in-sync main refused. The owner had nothing to publish; the receiving
    worker's main held a commit that origin already has through a squash merge."""
    _a, b, source, target = pair
    (target / "fix.txt").write_text("fixed on the receiver")
    git(target, "add", ".")
    git(target, "commit", "-m", "receiver fix")
    (source / "fix.txt").write_text("fixed on the receiver")
    (source / "review.txt").write_text("review follow-up")
    git(source, "checkout", "main")
    git(source, "add", ".")
    git(source, "commit", "-m", "Squash-merge the receiver fix (#1)")
    git(source, "push", "origin", "main")
    _base_session_on_clean_main(pair, monkeypatch)
    head = git(source, "rev-parse", "HEAD").decode().strip()
    with b.active():
        cwd = await handoff.receive(database.get_session("handoff-test"))
        assert cwd == str(target)
        assert checkout_transfer.fingerprint(target) == head
        assert database.get_session("handoff-test")["owner_device_id"] == b.device_id
    assert git(source, "rev-parse", "HEAD").decode().strip() == head


@pytest.mark.asyncio
async def test_continue_here_names_unpublished_receiver_commit_and_keeps_owner(pair, monkeypatch):
    a, b, source, target = pair
    (target / "mine.txt").write_text("only on the receiver")
    git(target, "add", ".")
    git(target, "commit", "-m", "receiver only")
    git(source, "checkout", "main")
    git(source, "push", "origin", "main")
    _base_session_on_clean_main(pair, monkeypatch)
    with b.active():
        with pytest.raises(ValueError, match=r"Branch main in the receiving worker's checkout .* has 1 commit .* not on origin"):
            await handoff.receive(database.get_session("handoff-test"))
        assert database.get_session("handoff-test")["owner_device_id"] == a.device_id
    with a.active():
        assert database.get_session("handoff-test")["owner_device_id"] == a.device_id
    assert (target / "mine.txt").exists()


@pytest.mark.asyncio
async def test_source_branch_switch_at_same_commit_refuses_ownership(pair, monkeypatch):
    a, b, source, target = pair
    async def call(device, action, body, **kwargs):
        with a.active():
            if body["phase"] == "commit":
                git(source, "checkout", "-b", "different-branch")
            return await control.handle(action, body, from_device=b.device_id)
    monkeypatch.setattr(control, "call", call)
    with b.active():
        with pytest.raises(ValueError, match="different branch"):
            await handoff.receive(database.get_session("handoff-test"))
        assert database.get_session("handoff-test")["owner_device_id"] == a.device_id
    with a.active():
        assert database.get_session("handoff-test")["owner_device_id"] == a.device_id



@pytest.mark.asyncio
@pytest.mark.parametrize("dirty", [False, True])
async def test_move_endpoint_produces_git_and_native_context_handoff(pair, monkeypatch, dirty):
    from app.api.sessions import move_session_endpoint
    from app.api.sessions_helpers import MoveSessionRequest
    from app.db import session_events
    from app.network import crypto
    from tests.api.helpers import fake_request

    a, b, source, target = pair
    with b.active():
        target_instance = settings.effective_instance_id
    with a.active():
        database.register_install(target_instance, "Target", None, "linux", "test")
        session_events.append_events("handoff-test", [{"type": "message.user", "payload": {"content": "Final unsynced context"}}])
    if dirty:
        (source / "file.txt").write_text("dirty staged work")
        git(source, "add", "file.txt")
        (source / "new.txt").write_text("untracked source work")
    (source / ".env").write_text("NEVER_COPY_THIS=private")
    phases = []
    exported = []

    # Exercise the legacy synchronous peer as well as actual challenge/signature
    # tokens and HTTP control routing. ServedBy swaps process-wide identities
    # per request and cannot host tasks spanning requests. The deferred protocol
    # is exercised above with one stable server identity.
    from app.core import runner_capabilities
    detect = runner_capabilities.detect_capabilities
    monkeypatch.setattr(runner_capabilities, "detect_capabilities",
                        lambda: {**detect(), "handoff_poll": 0})
    import httpx
    from app.core import cluster
    from app.db import network as network_store
    from tests.core.network.test_transport import ServedBy

    served = {a.device_id: ServedBy(a, "http://a.local:8000"),
              b.device_id: ServedBy(b, "http://b.local:8000")}
    real_async, real_call = httpx.AsyncClient, control.call
    transport.reset_for_tests()
    tokens.reset_for_tests()
    # Authentication publishes the caller's own address. Use each actual test
    # server's URL, not a host/CI LAN address that the MockTransport cannot serve.
    monkeypatch.setattr(settings, "cluster_advertise_url", None)
    for worker, peer in ((a, b), (b, a)):
        with worker.active():
            cluster.set_advertise_url_override(served[worker.device_id].base_url)
            assert engine._own_endpoints() == [served[worker.device_id].base_url]
            network_store.touch_member(peer.device_id, endpoints=[served[peer.device_id].base_url])

    def route(request):
        server = served[a.device_id] if request.url.host == "a.local" else served[b.device_id]
        return server.handler(request)

    def async_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(route)
        return real_async(*args, **kwargs)

    async def call(device, action, body, **kwargs):
        assert action in ("handoff", "git-state")
        if action == "handoff":
            phases.append(body["phase"])
            if body["phase"] == "receive":
                assert body == {"session_id": "handoff-test", "phase": "receive"}
        reply = await real_call(device, action, body, **kwargs)
        if action == "git-state":
            assert reply["eligible"] is True
            assert all("path" not in row for row in reply["checkouts"])
        if action == "handoff" and body["phase"] == "begin":
            ctx = engine.require_context()
            raw = crypto.decrypt_payload(ctx.network_key, reply["payload"], handoff._aad("handoff-test", b.device_id))
            exported.append(json.loads(raw))
        return reply

    monkeypatch.setattr(httpx, "AsyncClient", async_client)
    monkeypatch.setattr(control, "call", call)
    monkeypatch.setattr("app.core.placement.capable_runners", lambda *args: [target_instance])
    with a.active():
        # The target's real checkout was registered on B, not seeded into A.
        project_id = database.get_session("handoff-test")["project_v2_id"]
        assert not checkouts.has_active_base_checkout(project_id, target_instance)
        result = await move_session_endpoint(fake_request(), "handoff-test", MoveSessionRequest(target_instance_id=target_instance), token="test")
        assert result.moved is True
        assert database.get_session("handoff-test")["owner_device_id"] == b.device_id
        head = checkout_transfer.fingerprint(source)
        assert git(source, "ls-remote", "origin", "refs/heads/feature/only-on-worker").decode().split()[0] == head
    assert phases == ["receive", "begin", "commit"]
    for server in served.values():
        assert ("POST", "/api/v1/network/peer/auth/challenge") in server.requests
        assert ("POST", "/api/v1/network/peer/auth/token") in server.requests
        assert ("POST", "/api/v1/network/peer/control/handoff") in server.requests
    assert set(exported[0]["checkout"]) == {"protocol", "branch", "head", "fingerprint", "remote"}
    with b.active():
        from app.core import project_resolver
        assert network_store.get_member(a.device_id)["endpoints"] == [served[a.device_id].base_url]
        row = database.get_session("handoff-test")
        cwd = project_resolver.resolve_existing_cwd(row)
        assert checkout_transfer.fingerprint(cwd) == head
        assert row["owner_device_id"] == b.device_id
        placement = row["placement"]
        assert (json.loads(placement) if isinstance(placement, str) else placement) == {"where": "runner:" + b.device_id}
        assert row["sdk_session_id"].startswith("ses_") and row["sdk_session_id"] != "ses_source"
        assert database.get_session_messages("handoff-test")[0]["content"] == "Keep this conversation"
        assert session_events.get_events("handoff-test", 0)[-1]["payload"]["content"] == "Final unsynced context"
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT message FROM fluid_session_nodes WHERE session_id=%s", (row["sdk_session_id"],))
            message = cur.fetchone()["message"]
            assert (json.loads(message) if isinstance(message, str) else message)["content"] == "Native history"
        from pathlib import Path
        assert not (Path(cwd) / ".env").exists()
        if dirty:
            assert (Path(cwd) / "new.txt").read_text() == "untracked source work"
    converge([a, b])
    with b.active():
        assert database.get_session("handoff-test")["sdk_session_id"] == row["sdk_session_id"]
        assert project_resolver.resolve_existing_cwd(database.get_session("handoff-test")) == cwd


@pytest.mark.asyncio
async def test_move_preserves_dirty_destination_and_source_ownership(pair, monkeypatch, tmp_path):
    a, b, source, target = pair
    state = checkout_transfer.export_checkout(source, "feature/only-on-worker")
    from pathlib import Path
    destination = Path(checkout_transfer.import_checkout(target, tmp_path / "existing", state))
    (destination / "file.txt").write_text("Keep destination edits")
    phases = []

    async def call(device, action, body, **kwargs):
        caller = engine.require_context().device_id
        phases.append(body["phase"])
        with (a if device == a.device_id else b).active():
            return await control.handle(action, body, from_device=caller)

    monkeypatch.setattr(control, "call", call)
    with a.active():
        with pytest.raises(ValueError, match="receiving checkout has local changes"):
            await handoff.move(database.get_session("handoff-test"), b.device_id)
        assert database.get_session("handoff-test")["owner_device_id"] == a.device_id
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT state FROM runs WHERE session_id=%s", ("handoff-test",))
            assert cur.fetchone()["state"] == "released"
    assert phases == ["receive", "begin", "cancel"]
    assert (destination / "file.txt").read_text() == "Keep destination edits"
    with b.active():
        assert database.get_session("handoff-test")["owner_device_id"] == a.device_id


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,status", [("worker", "revoked"), ("client", "active")])
async def test_move_rejects_ineligible_network_destination_before_git(pair, monkeypatch, kind, status):
    from dataclasses import replace
    from unittest.mock import AsyncMock
    a, b, source, target = pair
    call = AsyncMock()
    monkeypatch.setattr(control, "call", call)
    with a.active():
        ctx = engine.require_context()
        ctx.members[b.device_id] = replace(ctx.member(b.device_id), kind=kind, status=status)
        with pytest.raises(ValueError, match="active worker"):
            await handoff.move(database.get_session("handoff-test"), b.device_id)
        assert database.get_session("handoff-test")["owner_device_id"] == a.device_id
    call.assert_not_called()
    assert not git(source, "ls-remote", "origin", "refs/heads/feature/only-on-worker").strip()


@pytest.mark.asyncio
@pytest.mark.parametrize("caller", ["foreign-network-device", "revoked", "client"])
async def test_receive_request_requires_authenticated_active_worker(pair, monkeypatch, caller):
    from dataclasses import replace
    from unittest.mock import AsyncMock
    a, b, source, target = pair
    receive = AsyncMock()
    monkeypatch.setattr(handoff, "receive", receive)
    with b.active():
        ctx = engine.require_context()
        peer = caller
        if caller in ("revoked", "client"):
            peer = a.device_id
            ctx.members[peer] = replace(ctx.member(peer), status="revoked" if caller == "revoked" else "active", kind="client" if caller == "client" else "worker")
        with pytest.raises(PermissionError, match="authenticated receiving worker"):
            await control.handle("handoff", {"session_id": "handoff-test", "phase": "receive"}, from_device=peer)
        assert database.get_session("handoff-test")["owner_device_id"] == a.device_id
    receive.assert_not_called()


@pytest.mark.asyncio
async def test_move_to_this_worker_uses_the_same_leased_protocol(pair, monkeypatch):
    a, b, source, target = pair
    phases = []

    async def call(device, action, body, **kwargs):
        assert device == a.device_id
        phases.append(body["phase"])
        with a.active():
            return await control.handle(action, body, from_device=b.device_id)

    monkeypatch.setattr(control, "call", call)
    with b.active():
        assert await handoff.move(database.get_session("handoff-test"), b.device_id) is True
        assert database.get_session("handoff-test")["owner_device_id"] == b.device_id
    assert phases == ["begin", "commit"]


@pytest.mark.asyncio
async def test_move_cannot_publish_while_source_has_live_run(pair, monkeypatch):
    a, b, source, target = pair

    async def call(device, action, body, **kwargs):
        caller = engine.require_context().device_id
        with (a if device == a.device_id else b).active():
            return await control.handle(action, body, from_device=caller)

    monkeypatch.setattr(control, "call", call)
    with a.active():
        assert runs.claim_run("handoff-test", settings.effective_instance_id, 300)
        with pytest.raises(ValueError, match="busy"):
            await handoff.move(database.get_session("handoff-test"), b.device_id)
        assert database.get_session("handoff-test")["owner_device_id"] == a.device_id
    assert not git(source, "ls-remote", "origin", "refs/heads/feature/only-on-worker").strip()


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [
    control.RunnerUnreachable("Source offline"),
    control.ControlRefused("Source refused the handoff"),
])
async def test_receive_control_hop_preserves_owner_refusal(pair, monkeypatch, error):
    from unittest.mock import AsyncMock
    a, b, source, target = pair
    monkeypatch.setattr(handoff, "receive", AsyncMock(side_effect=error))
    with b.active():
        with pytest.raises(ValueError, match=str(error)):
            await control.handle("handoff", {"session_id": "handoff-test", "phase": "receive"}, from_device=a.device_id)
        assert database.get_session("handoff-test")["owner_device_id"] == a.device_id


@pytest.mark.asyncio
async def test_receiver_that_cannot_reach_the_owner_names_both_workers(pair, monkeypatch):
    """The live #1524 failure: the destination's call back to the owner never connects."""
    import httpx
    from app.db import network as store
    a, b, source, target = pair
    real_async = httpx.AsyncClient
    dialed = []

    def refuse(request):
        dialed.append(str(request.url))
        raise httpx.ConnectError("[Errno -3] Temporary failure in name resolution", request=request)

    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda *args, **kwargs: real_async(**{**kwargs, "transport": httpx.MockTransport(refuse)}))
    monkeypatch.setattr(control, "CONNECT_RETRY_DELAY", 0)
    head = git(source, "rev-parse", "HEAD")
    with b.active():
        transport._links[a.device_id] = transport.PeerLink(a.device_id, ["https://a.tunnel.example"])
        with pytest.raises(ValueError) as raised:
            await control.handle("handoff", {"session_id": "handoff-test", "phase": "receive"},
                                 from_device=a.device_id)
        owner_name = store.get_member(a.device_id)["name"]  # the founder defaults to its host name
        assert str(raised.value) == f"Couldn't reach {owner_name} from b (address lookup failed on its only route)."
        assert database.get_session("handoff-test")["owner_device_id"] == a.device_id
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) AS n FROM runs WHERE state='active' AND runner_id LIKE %s",
                        (handoff.HOLD + "%",))
            assert cur.fetchone()["n"] == 0
    assert len(dialed) == control.CONNECT_ROUNDS
    assert git(source, "rev-parse", "HEAD") == head


@pytest.mark.asyncio
async def test_session_handed_back_to_its_first_worker_lists_as_local(pair, monkeypatch):
    """A -> B -> A: the first worker owns the row again and must not list it as
    remote with an unknown state because instance_id still named B."""
    from app.api import sessions_helpers

    a,b,source,target=pair
    for worker in (a,b):
        with worker.active(), get_db() as conn:
            conn.cursor().execute("UPDATE sessions SET project_v2_id=NULL,branch=NULL WHERE id=%s",("handoff-test",))

    def owner_call(owner):
        async def call(device,action,body,**kwargs):
            with owner.active():
                return await control.handle(action,body,from_device=(b if owner is a else a).device_id)
        return call

    monkeypatch.setattr(control,"call",owner_call(a))
    with b.active():
        await handoff.receive(database.get_session("handoff-test"))
    monkeypatch.setattr(control,"call",owner_call(b))
    with a.active():
        await handoff.receive(database.get_session("handoff-test"))
        row = database.get_session("handoff-test")
        assert row["owner_device_id"] == a.device_id
        assert row["instance_id"] == settings.effective_instance_id
        sessions_helpers._annotate_cluster_fields([row])
        assert row["is_remote"] is False
        assert "remote_runtime_state" not in row


# --- Other sessions sharing the published or receiving checkout (#1524) ---

BRANCH = "feature/only-on-worker"


def _route_between(a, b, phases):
    async def call(device, action, body, **kwargs):
        caller = engine.require_context().device_id
        phases.append(body["phase"])
        with (a if device == a.device_id else b).active():
            return await control.handle(action, body, from_device=caller)
    return call


def _bind_session(sid, branch, cwd=None):
    """Bind another session to this worker's canonical checkout of ``branch``."""
    from app.core import project_resolver
    project_id = database.get_session("handoff-test")["project_v2_id"]
    database.create_session(sid, "default", title=sid, project_v2_id=project_id)
    database.set_session_branch(sid, branch)
    row = database.get_session(sid)
    assert control.owner_of(row) is None
    if cwd is not None:
        assert project_resolver.resolve_existing_cwd(row) == str(cwd)
    return row


def _start_work(sid, live_lease):
    from app.db import turns
    run = runs.claim_run(sid, settings.effective_instance_id, 300 if live_lease else -1)
    assert run
    turn = turns.enqueue_turn(sid, "user", prompt="Working in this checkout")
    started = turns.start_turn(turn["id"], run["id"], settings.effective_instance_id)
    assert started["state"] == "running"
    return run, turn


def _assert_still_working(sid, run, turn, live_lease):
    from app.db import turns
    assert turns.get_turn(turn["id"])["state"] == "running"
    active = runs.get_active_run(sid)
    if live_lease:
        assert active and active["id"] == run["id"]
    else:
        assert active is None or active["id"] == run["id"]


def _active_holds():
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT session_id, runner_id FROM runs WHERE state='active'")
        return [row["session_id"] for row in cur.fetchall() if row["runner_id"].startswith(handoff.HOLD)]


def _registered_receiving_worktree(a, b, source, target, tmp_path):
    """B has a real registered checkout of the branch, behind the source."""
    from pathlib import Path
    with a.active():
        state = checkout_transfer.export_checkout(source, BRANCH)
    destination = Path(checkout_transfer.import_checkout(target, tmp_path / "existing", state))
    with b.active():
        project_id = database.get_session("handoff-test")["project_v2_id"]
        checkouts.upsert_checkout(project_id, settings.effective_instance_id, str(destination), branch=BRANCH)
    (source / "file.txt").write_text("newer source work")
    git(source, "commit", "-am", "newer source work")
    return destination, state["head"]


@pytest.mark.asyncio
@pytest.mark.parametrize("dirty", [False, True])
@pytest.mark.parametrize("live_lease", [True, False])
async def test_move_refuses_publication_while_another_session_works_in_source_checkout(pair, monkeypatch, dirty, live_lease):
    a, b, source, target = pair
    if dirty:
        (source / "file.txt").write_text("dirty staged work")
        git(source, "add", "file.txt")
        (source / "new.txt").write_text("untracked source work")
    head = git(source, "rev-parse", "HEAD")
    status = git(source, "status", "--porcelain")
    with a.active():
        _bind_session("sibling", BRANCH, cwd=source)
        run, turn = _start_work("sibling", live_lease)
    phases = []
    monkeypatch.setattr(control, "call", _route_between(a, b, phases))
    with a.active():
        with pytest.raises(ValueError, match="Another session is working in this checkout"):
            await handoff.move(database.get_session("handoff-test"), b.device_id)
        assert database.get_session("handoff-test")["owner_device_id"] == a.device_id
        _assert_still_working("sibling", run, turn, live_lease)
        assert runs.get_active_run("handoff-test") is None
        assert _active_holds() == []
    assert phases == ["receive", "begin"]
    assert git(source, "rev-parse", "HEAD") == head
    assert git(source, "status", "--porcelain") == status
    assert not git(source, "ls-remote", "origin", "refs/heads/" + BRANCH).strip()
    with b.active():
        assert database.get_session("handoff-test")["owner_device_id"] == a.device_id
        assert not git(target, "branch", "--list", BRANCH).strip()


@pytest.mark.asyncio
@pytest.mark.parametrize("dirty", [False, True])
@pytest.mark.parametrize("live_lease", [True, False])
async def test_move_refuses_receiving_checkout_used_by_another_session(pair, monkeypatch, tmp_path, dirty, live_lease):
    a, b, source, target = pair
    destination, published = _registered_receiving_worktree(a, b, source, target, tmp_path)
    if dirty:
        (source / "new.txt").write_text("untracked source work")
    source_head = git(source, "rev-parse", "HEAD")
    with b.active():
        _bind_session("receiver-sibling", BRANCH, cwd=destination)
        run, turn = _start_work("receiver-sibling", live_lease)
    phases = []
    monkeypatch.setattr(control, "call", _route_between(a, b, phases))
    with a.active():
        with pytest.raises(ValueError, match="Another session is working in this checkout"):
            await handoff.move(database.get_session("handoff-test"), b.device_id)
        assert database.get_session("handoff-test")["owner_device_id"] == a.device_id
        assert runs.get_active_run("handoff-test") is None
    # Refused before the owner was asked to commit or push anything.
    assert phases == ["receive"]
    assert git(destination, "rev-parse", "HEAD").decode().strip() == published
    assert git(source, "rev-parse", "HEAD") == source_head
    assert git(source, "ls-remote", "origin", "refs/heads/" + BRANCH).decode().split()[0] == published
    if dirty:
        assert (source / "new.txt").read_text() == "untracked source work"
    with b.active():
        assert database.get_session("handoff-test")["owner_device_id"] == a.device_id
        _assert_still_working("receiver-sibling", run, turn, live_lease)
        assert _active_holds() == []


@pytest.mark.asyncio
async def test_receiving_checkout_work_started_during_publication_blocks_import(pair, monkeypatch, tmp_path):
    """Work that starts on the receiving checkout after the first check still blocks the import."""
    a, b, source, target = pair
    destination, published = _registered_receiving_worktree(a, b, source, target, tmp_path)
    with b.active():
        _bind_session("late-sibling", BRANCH, cwd=destination)
    phases = []
    route = _route_between(a, b, phases)
    started = {}

    async def call(device, action, body, **kwargs):
        reply = await route(device, action, body, **kwargs)
        if body["phase"] == "begin":
            with b.active():
                # The pre-publication hold blocks run admission...
                assert runs.claim_run("late-sibling", "probe", 60) is None
                # ...so model a hold that lapsed while the owner published and
                # a turn that started in that gap.
                with get_db() as conn:
                    conn.cursor().execute(
                        "UPDATE runs SET lease_expires_at=NOW() + (%s * INTERVAL '1 second') "
                        "WHERE session_id=%s AND state='active'", (-1, "late-sibling"))
                started["run"], started["turn"] = _start_work("late-sibling", True)
        return reply

    monkeypatch.setattr(control, "call", call)
    with a.active():
        with pytest.raises(ValueError, match="Another session is working in this checkout"):
            await handoff.move(database.get_session("handoff-test"), b.device_id)
        assert database.get_session("handoff-test")["owner_device_id"] == a.device_id
    assert phases == ["receive", "begin", "cancel"]
    assert git(destination, "rev-parse", "HEAD").decode().strip() == published
    with b.active():
        _assert_still_working("late-sibling", started["run"], started["turn"], True)
        assert _active_holds() == []
    with a.active():
        assert runs.get_active_run("handoff-test") is None
        assert _active_holds() == []


@pytest.mark.asyncio
async def test_idle_source_checkout_users_cannot_start_until_the_handoff_ends(pair, monkeypatch):
    from app.core import turn_claim
    a, b, source, _ = pair
    with a.active():
        _bind_session("idle-sibling", BRANCH, cwd=source)
    real_export = checkout_transfer.export_checkout
    probes = {}

    def export(*args, **kwargs):
        # Inside the owner's publication: admission is held for every session
        # on this checkout/branch, including one bound after the scan.
        probes["sibling_claim"] = runs.claim_run("idle-sibling", "probe", 60)
        _bind_session("new-session", BRANCH)
        _bind_session("other-branch", "unrelated-branch")
        probes["new_held"] = handoff.admission_held("new-session")
        probes["other_held"] = handoff.admission_held("other-branch")
        return real_export(*args, **kwargs)

    kicks = []
    monkeypatch.setattr(turn_claim, "kick", lambda name=None: kicks.append(name))
    monkeypatch.setattr(checkout_transfer, "export_checkout", export)
    monkeypatch.setattr(control, "call", _route_between(a, b, []))
    with a.active():
        assert await handoff.move(database.get_session("handoff-test"), b.device_id) is True
        assert probes == {"sibling_claim": None, "new_held": True, "other_held": False}
        assert database.get_session("handoff-test")["owner_device_id"] == b.device_id
        assert _active_holds() == []
        assert not handoff.admission_held("new-session")
        assert runs.claim_run("idle-sibling", settings.effective_instance_id, 60)
    assert "turn_claim" in kicks


@pytest.mark.asyncio
async def test_idle_receiving_checkout_users_cannot_start_until_the_import_ends(pair, monkeypatch, tmp_path):
    a, b, source, target = pair
    destination, _ = _registered_receiving_worktree(a, b, source, target, tmp_path)
    with b.active():
        _bind_session("idle-receiver", BRANCH, cwd=destination)
    real_import = checkout_transfer.import_checkout
    probes = {}

    def import_checkout(*args, **kwargs):
        probes["sibling_claim"] = runs.claim_run("idle-receiver", "probe", 60)
        _bind_session("new-receiver", BRANCH)
        probes["new_held"] = handoff.admission_held("new-receiver")
        return real_import(*args, **kwargs)

    monkeypatch.setattr(checkout_transfer, "import_checkout", import_checkout)
    monkeypatch.setattr(control, "call", _route_between(a, b, []))
    with a.active():
        assert await handoff.move(database.get_session("handoff-test"), b.device_id) is True
    assert probes == {"sibling_claim": None, "new_held": True}
    assert git(destination, "rev-parse", "HEAD") == git(source, "rev-parse", "HEAD")
    with b.active():
        assert database.get_session("handoff-test")["owner_device_id"] == b.device_id
        assert _active_holds() == []
        assert runs.get_active_run("handoff-test") is None
        assert not handoff.admission_held("new-receiver")
        assert runs.claim_run("idle-receiver", settings.effective_instance_id, 60)
