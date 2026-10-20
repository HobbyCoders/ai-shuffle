"""Regression coverage for PR 298's independent correctness review."""
import time
from unittest.mock import AsyncMock

import pytest

from app.core import session_handoff as sh, wakeup_service
from app.db import database, session_events, session_moves, wakeups
from app.db.base import get_db
from app.network import control, handoff
from tests.core.network.test_handoff import pair  # noqa: F401
from tests.core.test_checkout_transfer import repositories  # noqa: F401
from tests.core.network.harness import converge


def test_divergent_event_compaction_reconciles_ids_and_rewinds(mock_db):
    sid = "review-compaction"
    database.create_session(sid, "default")
    session_events.append_events(sid, [{"type": "message.assistant.delta", "payload": {"text": str(i)}}
                                      for i in range(5)])
    data = {"sdk_session_id": None, "history": handoff._history(database.get_session(sid))}
    rows = data["history"]["session_events"]
    survivor = dict(rows[-1], id=rows[0]["id"], payload={"text": "01234"})
    data["history"]["session_events"] = [survivor]
    handoff.install_history(sid, data, ".")
    actual = handoff._history(database.get_session(sid))["session_events"]
    assert [(row["id"], row["seq"]) for row in actual] == [(survivor["id"], survivor["seq"])]


def test_messages_reconcile_changed_and_deleted_rows_without_duplicates(mock_db):
    sid = "review-messages"
    database.create_session(sid, "default")
    database.add_session_message(sid, "user", "old content")
    database.add_session_message(sid, "assistant", "rewound content")
    data = {"sdk_session_id": None, "history": handoff._history(database.get_session(sid))}
    data["history"]["session_messages"] = [dict(data["history"]["session_messages"][0], content="new content")]
    handoff.install_history(sid, data, ".")
    handoff.install_history(sid, data, ".")
    assert [r["content"] for r in handoff._history(database.get_session(sid))["session_messages"]] == ["new content"]


@pytest.mark.asyncio
async def test_source_restart_uses_durable_export_decision(mock_db, monkeypatch):
    sid = "review-resume-boot"
    database.create_session(sid, "default")
    session_moves.save(sid, target_id="target", label="Target", note="continue", requested_by="agent",
                       requested_at=time.time(), expires_at=time.time() + 1800)
    row = session_moves.get(sid)
    session_moves.set_resume_exported(sid, row["handoff_id"], "target")
    monkeypatch.setattr(control, "owner_of", lambda _: "target")
    monkeypatch.setattr(sh, "resume_plan", lambda *args: "send")
    # Capability changes after export must not change the decision on boot.
    monkeypatch.setattr(handoff, "target_resume", lambda _: False)
    monkeypatch.setattr(sh, "_check_source_membership", lambda: None)
    monkeypatch.setattr(sh, "pending_for", lambda _: None)
    spawned = []
    monkeypatch.setattr(sh, "_spawn", spawned.append)
    deliver = AsyncMock()
    monkeypatch.setattr(sh, "deliver_held", deliver)
    await sh.resume_queued()
    await spawned[0]
    assert deliver.await_args.args[3] is None




def test_export_marker_is_scoped_to_one_move_and_survives_resave(mock_db):
    kwargs = dict(target_id="target", label="Target", note="note", requested_by="agent",
                  requested_at=time.time(), expires_at=time.time() + 1800)
    session_moves.save("marker-chat", handoff_id="move-1", **kwargs)
    session_moves.set_resume_exported("marker-chat", "move-1", "wrong-target")
    assert not session_moves.get("marker-chat")["resume_exported"]
    session_moves.set_resume_exported("marker-chat", "move-1", "target")
    session_moves.save("marker-chat", handoff_id="move-1", **kwargs)
    assert session_moves.get("marker-chat")["resume_exported"]
    session_moves.save("marker-chat", handoff_id="move-2", **kwargs)
    assert not session_moves.get("marker-chat")["resume_exported"]


def test_stale_staging_cannot_overwrite_a_newer_committed_snapshot(pair):
    import json

    a, b, _, _ = pair
    sid = "handoff-test"
    with a.active():
        data = {"token": "obsolete-lease", "sdk_session_id": "ses_source",
                "history": handoff._history(database.get_session(sid)), "_prepare_history": True}
    with b.active():
        sdk = handoff.install_history(sid, data, ".")
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("UPDATE sessions SET owner_device_id=%s,placement=%s WHERE id=%s",
                        (b.device_id, json.dumps({"where": "runner:" + b.device_id,
                                                 "handoff": {"token": "newer-lease"}}), sid))
            cur.execute("UPDATE handoff_history_chunks SET created_at=%s WHERE token=%s",
                        ("2000-01-01T00:00:00+00:00", data["token"]))
        handoff.recover_history_staging()
        assert database.get_session(sid)["sdk_session_id"] is None
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT count(*) AS n FROM fluid_session_nodes WHERE session_id=%s", (sdk,))
            assert cur.fetchone()["n"] == 0


@pytest.mark.asyncio
async def test_history_maintenance_survives_a_transient_db_failure(monkeypatch, caplog):
    import asyncio

    sleeps = 0

    async def sleep(delay):
        nonlocal sleeps
        sleeps += 1
        if sleeps == 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(handoff.asyncio, "sleep", sleep)
    run = AsyncMock(side_effect=RuntimeError("temporary database outage"))
    monkeypatch.setattr(handoff, "run_sync", run)
    with pytest.raises(asyncio.CancelledError):
        await handoff.history_recovery_loop()
    run.assert_awaited_once()
    assert "Could not sweep hand-off history staging" in caplog.text


def test_legacy_resume_command_uses_handoff_identity(mock_db):
    database.create_session("review-legacy", "default")
    pending = sh.PendingHandoff("target", "Target", "note", time.time())
    assert sh._resume_turn("review-legacy", pending, "Target")["command_id"] == "handoff-resume-" + pending.handoff_id


@pytest.mark.asyncio
async def test_manual_move_does_not_inherit_unrelated_agent_note(pair, monkeypatch):
    a, b, _, _ = pair
    sid = "handoff-test"
    with a.active():
        session_moves.save(sid, target_id="another-worker", label="Other", note="unrelated note", requested_by="agent",
                           requested_at=time.time(), expires_at=time.time() + 1800)
    monkeypatch.setattr(sh, "_moving", {})
    for name in ("wake_carry", "resume_carry", "target_resume"):
        monkeypatch.setattr(handoff, name, lambda _: True)
    armed = []
    monkeypatch.setattr(wakeup_service, "arm_persisted", armed.append)

    async def call(device, action, body, **kwargs):
        with a.active():
            return await control.handle(action, body, from_device=b.device_id)

    monkeypatch.setattr(control, "call", call)
    with b.active():
        await handoff.receive(database.get_session(sid))
        assert wakeups.get_session_wakeups(sid) == []
    assert armed == []


@pytest.mark.asyncio
@pytest.mark.parametrize("all_replies_lost", [False, True])
@pytest.mark.parametrize("projectless", [False, True])
async def test_lost_commit_reply_retries_or_recovers_after_sync(pair, monkeypatch, all_replies_lost, projectless):
    a, b, _, _ = pair
    sid = "handoff-test"
    if projectless:
        for worker in (a, b):
            with worker.active(), get_db() as conn:
                conn.cursor().execute("UPDATE sessions SET project_v2_id=NULL,branch=NULL WHERE id=%s", (sid,))
    commits = 0

    async def call(device, action, body, **kwargs):
        nonlocal commits
        with a.active():
            result = await control.handle(action, body, from_device=b.device_id)
            if body.get("phase") == "commit":
                commits += 1
                if all_replies_lost or commits == 1:
                    raise TimeoutError("commit applied but reply lost")
            return result

    monkeypatch.setattr(control, "call", call)
    with b.active():
        if all_replies_lost:
            with pytest.raises((TimeoutError, control.MaybeApplied)):
                await handoff.receive(database.get_session(sid))
            from app.db import runs
            active = runs.get_active_run(sid)
            assert active and active["runner_id"].startswith(handoff.HOLD)
        else:
            await handoff.receive(database.get_session(sid))
    converge([a, b])
    with b.active():
        handoff.recover_history_staging()
        session = database.get_session(sid)
        assert session["owner_device_id"] == b.device_id
        assert session["sdk_session_id"]
        from app.db import runs
        assert runs.get_active_run(sid) is None
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT count(*) AS n FROM handoff_history_chunks WHERE session_id=%s", (sid,))
            assert cur.fetchone()["n"] == 0
            cur.execute("SELECT count(*) AS n FROM fluid_session_nodes WHERE session_id=%s", (session["sdk_session_id"],))
            assert cur.fetchone()["n"] == 1
    assert commits >= 2


@pytest.mark.asyncio
async def test_superseded_resume_waits_for_held_delivery_release(mock_db, monkeypatch):
    from types import SimpleNamespace
    from app.network import engine
    from app.db import turns

    sid = "review-held-order"
    database.create_session(sid, "default")
    wake = {"id": "handoff-resume-held", "source": "handoff_resume", "prompt": "original note",
            "fire_at": time.time(), "profile_id": "default"}
    handoff._stage_wakes(sid, "source", {"token": "held-lease", "wakes": [wake]}, None)
    # A held human receipt can arrive before the receiver accepts ownership.
    with get_db() as conn:
        conn.cursor().execute("UPDATE handoff_wakes SET superseded=TRUE WHERE token=%s", ("held-lease",))
    handoff._accept_owner(sid, "target", None, "held-lease")
    assert wakeups.get_session_wakeups(sid) == []
    human = turns.enqueue_turn(sid, "user", prompt="new human instruction")
    monkeypatch.setattr(engine, "require_context", lambda: SimpleNamespace(device_id="target"))
    rows = handoff._finalize_wakes(sid, "source", {"token": "held-lease", "release": True, "superseded": True})
    assert [row["id"] for row in rows] == [wake["id"]]
    # Drive the normal wake producer through durable admission, not just read it.
    from app.core import turn_scheduler
    monkeypatch.setattr(turn_scheduler, "kick", lambda _: None)
    monkeypatch.setattr(wakeup_service, "_session_user_stopped", lambda _: False)
    monkeypatch.setattr(wakeup_service, "_resolve_profile_id", AsyncMock(return_value="default"))
    monkeypatch.setattr(wakeup_service, "_session_api_user_id", lambda _: None)
    monkeypatch.setattr("app.core.archive_guard.is_archived_async", AsyncMock(return_value=False))
    await wakeup_service._fire(rows[0])
    queued = turns.get_queued_turns(sid)
    assert [turn["id"] for turn in queued][0] == human["id"]
    assert [turn["prompt"] for turn in queued] == ["new human instruction", "original note"]


@pytest.mark.asyncio
async def test_commit_holds_resume_until_source_releases_held_messages(pair, monkeypatch):
    a, b, _, _ = pair
    sid = "handoff-test"
    pending = sh.PendingHandoff(b.device_id, "Target", "release note", time.time())
    monkeypatch.setattr(sh, "_moving", {sid: pending})
    with a.active():
        session_moves.save(sid, target_id=b.device_id, label=pending.label, note=pending.note, requested_by="agent",
                           requested_at=pending.requested_at, expires_at=pending.expires_at, handoff_id=pending.handoff_id)
    for name in ("wake_carry", "resume_carry", "target_resume"):
        monkeypatch.setattr(handoff, name, lambda _: True)
    armed = []
    monkeypatch.setattr(wakeup_service, "arm_persisted", armed.append)

    async def call(device, action, body, **kwargs):
        with a.active():
            if body.get("phase") == "commit":
                session_moves.append_held(sid, {"id": "held-human", "source": "user", "prompt": "changed instructions"})
            result = await control.handle(action, body, from_device=b.device_id)
            if body.get("phase") == "commit":
                assert result["held_messages"] == 1
                assert handoff.finish(sid, b.device_id, body) == result
            return result

    monkeypatch.setattr(control, "call", call)
    with b.active():
        await handoff.receive(database.get_session(sid))
        assert wakeups.get_session_wakeups(sid) == []
        assert armed == []
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT token FROM handoff_wakes WHERE session_id=%s", (sid,))
            token = cur.fetchone()["token"]
        # An acceptance retry/boot must not arm it ahead of release, either.
        await handoff._accept_and_arm(sid, b.device_id, None, token)
        assert armed == []
        rows = handoff._finalize_wakes(sid, a.device_id, {"token": token, "superseded": True, "release": True})
        assert len(rows) == 1 and "release note" in rows[0]["prompt"]




@pytest.mark.asyncio
async def test_failed_post_commit_publication_keeps_run_admission_closed(pair, monkeypatch):
    from app.db import runs

    a, b, _, _ = pair
    sid = "handoff-test"
    original = handoff._publish_history
    fail = True

    def publish(*args):
        nonlocal fail
        if fail:
            fail = False
            raise RuntimeError("transient publication outage")
        return original(*args)

    monkeypatch.setattr(handoff, "_publish_history", publish)

    async def call(device, action, body, **kwargs):
        with a.active():
            return await control.handle(action, body, from_device=b.device_id)

    monkeypatch.setattr(control, "call", call)
    with b.active():
        with pytest.raises(RuntimeError, match="publication outage"):
            await handoff.receive(database.get_session(sid))
        assert runs.get_active_run(sid)["runner_id"].startswith(handoff.HOLD)
    converge([a, b])
    with b.active():
        handoff.recover_history_staging()
        assert database.get_session(sid)["sdk_session_id"]
        assert runs.get_active_run(sid) is None


@pytest.mark.asyncio
async def test_progress_flows_from_move_polling_to_get_handoff(mock_db, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    import httpx
    from fastapi import FastAPI
    from app.api import sessions
    from app.api.auth import require_auth
    from app.network import engine, runners

    sid = "review-progress"
    database.create_session(sid, "default")
    session = database.get_session(sid)
    pending = sh.PendingHandoff("target", "Target", "", time.time())
    monkeypatch.setattr(sh, "_moving", {sid: pending})
    monkeypatch.setattr(sh, "busy_state", lambda _: {})
    monkeypatch.setattr(sessions, "require_session", AsyncMock(return_value=session))
    monkeypatch.setattr(control, "owner_of", lambda _: None)
    member = SimpleNamespace(kind="worker", status="active")
    ctx = SimpleNamespace(device_id="source", member=lambda _: member)
    monkeypatch.setattr(engine, "context", lambda: ctx)
    monkeypatch.setattr(engine, "require_context", lambda: ctx)
    monkeypatch.setattr(runners, "resolve_runner_id", lambda _: "target")
    monkeypatch.setattr(runners, "find", lambda _: {"capabilities": {"handoff_poll": 1}})
    monkeypatch.setattr(handoff, "_operations", {})
    monkeypatch.setattr(handoff, "_operation_progress", {})
    monkeypatch.setattr(handoff, "_remote_progress", {})
    monkeypatch.setattr(handoff, "_POLL_WAIT_S", 0.01)
    finish = asyncio.Event()
    progress_polled = asyncio.Event()
    polls = 0

    async def receive(payload, peer):
        timing = handoff._Timing("import", sid)
        timing.waiting("install_history.session_messages")
        await finish.wait()
        timing.log("committed")
        return {"moved": True}

    monkeypatch.setattr(handoff, "_handle", receive)

    async def call(device, action, body, **kwargs):
        nonlocal polls
        if body["phase"] == "status":
            polls += 1
            if polls == 2:
                progress_polled.set()
                await finish.wait()
        return await handoff.handle(body, "source")

    monkeypatch.setattr(control, "call", call)
    app = FastAPI()
    app.include_router(sessions.router)
    app.dependency_overrides[require_auth] = lambda: "test-owner"
    moving = asyncio.create_task(handoff.move(session, "target"))
    try:
        await asyncio.wait_for(progress_polled.wait(), 2)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(f"/api/v1/sessions/{sid}/handoff")
        assert response.status_code == 200
        progress = response.json()["pending"]["progress"]
        assert progress["step"] == "install_history.session_messages"
        assert progress["elapsed_seconds"] >= progress["step_elapsed_seconds"] >= 0
    finally:
        finish.set()
        await moving
    assert polls >= 2
    assert handoff.progress(sid) is None


@pytest.mark.parametrize("owned", [False, True])
@pytest.mark.parametrize("expired", [False, True])
def test_boot_sweep_publishes_owned_or_collects_expired_private_staging(pair, owned, expired):
    a, b, _, _ = pair
    sid = "handoff-test"
    with a.active():
        data = {"token": "review-orphan", "sdk_session_id": "ses_source",
                "history": handoff._history(database.get_session(sid)), "_prepare_history": True}
    with b.active():
        sdk = handoff.install_history(sid, data, ".")
        with get_db() as conn:
            cur = conn.cursor()
            if expired:
                cur.execute("UPDATE handoff_history_chunks SET created_at=%s WHERE token=%s",
                            ("2000-01-01T00:00:00+00:00", data["token"]))
            if owned:
                cur.execute("UPDATE sessions SET owner_device_id=%s WHERE id=%s", (b.device_id, sid))
        handoff.recover_history_staging()
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT count(*) AS n FROM handoff_history_chunks WHERE token=%s", (data["token"],))
            assert (cur.fetchone()["n"] == 0) == (owned or expired)
            cur.execute("SELECT count(*) AS n FROM fluid_session_nodes WHERE session_id=%s", (sdk,))
            assert cur.fetchone()["n"] == int(owned or not expired)
        assert database.get_session(sid)["sdk_session_id"] == (sdk if owned else None)
