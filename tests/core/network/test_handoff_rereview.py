"""Crash/expiry and ordering regressions from PR 298's second review."""
import json
import time
from unittest.mock import AsyncMock

import pytest

from app.core import session_handoff as sh, wakeup_service
from app.db import database, runs, session_moves, wakeups
from app.db.base import get_db
from app.network import control, handoff
from tests.core.network.test_handoff import pair  # noqa: F401
from tests.core.test_checkout_transfer import repositories  # noqa: F401
from tests.core.network.harness import converge


@pytest.mark.asyncio
async def test_p6_committed_import_survives_source_outage_past_lease(pair, monkeypatch):
    a, b, _, _ = pair
    sid = "handoff-test"
    for worker in (a, b):
        with worker.active(), get_db() as conn:
            conn.cursor().execute("UPDATE sessions SET project_v2_id=NULL,branch=NULL WHERE id=%s", (sid,))
    monkeypatch.setattr(handoff.asyncio, "sleep", AsyncMock())

    async def call(device, action, body, **kwargs):
        if body.get("phase") == "cancel":
            raise control.RunnerUnreachable("source down")
        with a.active():
            result = await control.handle(action, body, from_device=b.device_id)
            if body.get("phase") == "commit":
                raise control.RunnerUnreachable("source crashed after committing")
            return result

    monkeypatch.setattr(control, "call", call)
    with b.active():
        with pytest.raises(control.RunnerUnreachable):
            await handoff.receive(database.get_session(sid))
        from datetime import datetime, timezone
        old = datetime.fromtimestamp(time.time() - handoff.TTL - 120, timezone.utc).isoformat()
        with get_db() as conn:
            conn.cursor().execute("UPDATE handoff_history_chunks SET created_at=%s WHERE session_id=%s", (old, sid))
            conn.cursor().execute("UPDATE runs SET lease_expires_at=%s WHERE session_id=%s", (old, sid))
        handoff.recover_history_staging()
        assert runs.get_active_run(sid)["runner_id"].startswith(handoff.HOLD)
        assert handoff.admission_held(sid)  # A marked import fences admission even after lease expiry.
    converge([a, b])
    with b.active():
        assert handoff.admission_held(sid)  # Sync alone cannot admit work on the old context.
        assert handoff.recover_history_staging() == 1
        session = database.get_session(sid)
        assert session["owner_device_id"] == b.device_id
        assert session["sdk_session_id"]
        assert not handoff.admission_held(sid)
        assert runs.get_active_run(sid) is None
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT count(*) AS n FROM fluid_session_nodes WHERE session_id=%s", (session["sdk_session_id"],))
            assert cur.fetchone()["n"] == 1
            cur.execute("SELECT count(*) AS n FROM handoff_history_chunks WHERE session_id=%s", (sid,))
            assert cur.fetchone()["n"] == 0


@pytest.mark.parametrize("owner,receipt_token,kept", [
    ("source", None, True), ("target", None, True), ("target", "marked-token", True),
    ("target", "newer-token", False), ("other-worker", None, False),
])
def test_commit_marker_cleanup_requires_positive_ownership_evidence(pair, owner, receipt_token, kept):
    a, b, _, _ = pair
    sid = "handoff-test"
    with a.active():
        data = {"token": "marked-token", "sdk_session_id": "ses_source",
                "history": handoff._history(database.get_session(sid)), "_prepare_history": True}
    with b.active():
        sdk = handoff.install_history(sid, data, ".")
        handoff._mark_commit_sent(sid, data["token"], a.device_id)
        device = {"source": a.device_id, "target": b.device_id}.get(owner, owner)
        placement = {"where": "runner:" + device}
        if receipt_token:
            placement["handoff"] = {"token": receipt_token}
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("UPDATE sessions SET owner_device_id=%s,placement=%s WHERE id=%s",
                        (device, json.dumps(placement), sid))
            cur.execute("UPDATE handoff_history_chunks SET created_at=%s WHERE session_id=%s",
                        ("2000-01-01T00:00:00+00:00", sid))
        handoff._discard_history(sid, data["token"], expired_only=True)
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT count(*) AS n FROM fluid_session_nodes WHERE session_id=%s", (sdk,))
            assert bool(cur.fetchone()["n"]) == kept
        if owner == "source":
            handoff._discard_history(sid, data["token"])  # Confirmed source cancellation still retires it.
            with get_db() as conn:
                cur = conn.cursor()
                cur.execute("SELECT count(*) AS n FROM fluid_session_nodes WHERE session_id=%s", (sdk,))
                assert cur.fetchone()["n"] == 0


def test_missing_ownership_receipt_never_authorizes_staged_publication(pair):
    a, b, _, _ = pair
    sid = "handoff-test"
    with a.active():
        data = {"token": "no-receipt", "sdk_session_id": "ses_source",
                "history": handoff._history(database.get_session(sid)), "_prepare_history": True}
    with b.active():
        handoff.install_history(sid, data, ".")
        with get_db() as conn:
            conn.cursor().execute("UPDATE sessions SET owner_device_id=%s,placement=%s WHERE id=%s",
                                  (b.device_id, json.dumps({"where": "runner:" + b.device_id}), sid))
        assert handoff.recover_history_staging() == 0
        handoff._accept_owner(sid, b.device_id, token=data["token"], recovering=True)
        assert database.get_session(sid)["sdk_session_id"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("refusal", ["The resume reservation expired. Retry from the owning worker.", "already transferred"])
@pytest.mark.parametrize("final_success", [False, True])
async def test_commit_retry_refusal_stays_uncertain_and_is_paced(monkeypatch, refusal, final_success):
    result = {"committed": True} if final_success else control.ControlRefused(refusal)
    call = AsyncMock(side_effect=[TimeoutError("first attempt still committing"), control.ControlRefused(refusal), result])
    sleep = AsyncMock()
    monkeypatch.setattr(control, "call", call)
    monkeypatch.setattr(handoff.asyncio, "sleep", sleep)
    if final_success:
        assert await handoff._owner_commit("owner", {"session_id": "retry"}) == {"committed": True}
    else:
        with pytest.raises(control.MaybeApplied):
            await handoff._owner_commit("owner", {"session_id": "retry"})
    assert call.await_count == 3
    assert [c.args[0] for c in sleep.await_args_list] == [1.5, 1.5]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["moved", "boot"])
@pytest.mark.parametrize("owner", ["target", "different-owner"])
async def test_export_decision_only_applies_to_its_recorded_target(mock_db, monkeypatch, path, owner):
    sid = "stale-export"
    database.create_session(sid, "default")
    pending = sh.PendingHandoff("target", "Target", "note", time.time())
    session_moves.save(sid, target_id="target", label="Target", note="note", requested_by="agent",
                       requested_at=pending.requested_at, expires_at=pending.expires_at, handoff_id=pending.handoff_id)
    session_moves.set_resume_exported(sid, pending.handoff_id, "target")
    monkeypatch.setattr(sh, "resume_plan", lambda *args: "send")
    monkeypatch.setattr(sh, "_resume_turn", lambda *args: {"prompt": "fallback note"})
    monkeypatch.setattr(sh, "_label", lambda _: "Other")
    deliver = AsyncMock()
    monkeypatch.setattr(sh, "deliver_held", deliver)
    if path == "moved":
        monkeypatch.setattr(sh, "_tell", AsyncMock())
        await sh._moved(sid, pending, owner)
    else:
        monkeypatch.setattr(control, "owner_of", lambda _: owner)
        monkeypatch.setattr(sh, "_check_source_membership", lambda: None)
        monkeypatch.setattr(sh, "pending_for", lambda _: None)
        spawned = []
        monkeypatch.setattr(sh, "_spawn", spawned.append)
        await sh.resume_queued()
        await spawned[0]
    assert (deliver.await_args.args[3] is not None) == (owner != "target")


@pytest.mark.asyncio
async def test_commit_ends_holding_before_reply_and_counts_all_precommit_messages(pair, monkeypatch):
    a, b, _, _ = pair
    sid = "handoff-test"
    pending = sh.PendingHandoff(b.device_id, "Target", "late note", time.time())
    monkeypatch.setattr(sh, "_moving", {sid: pending})
    for name in ("wake_carry", "resume_carry", "target_resume"):
        monkeypatch.setattr(handoff, name, lambda _: True)
    with a.active():
        session_moves.save(sid, target_id=b.device_id, label=pending.label, note=pending.note, requested_by="agent",
                           requested_at=pending.requested_at, expires_at=pending.expires_at, handoff_id=pending.handoff_id)
    monkeypatch.setattr(wakeup_service, "arm_persisted", lambda _: pytest.fail("resume armed before held message release"))

    async def call(device, action, body, **kwargs):
        with a.active():
            if body.get("phase") == "commit":
                assert sh.hold(sid, "user", {"prompt": "before commit"})
            result = await control.handle(action, body, from_device=b.device_id)
            if body.get("phase") == "commit":
                assert not pending.holding
                assert sh.hold(sid, "user", {"prompt": "after commit"}) is None
                assert len(session_moves.get(sid)["held"]) == result["held_messages"] == 1
            return result

    monkeypatch.setattr(control, "call", call)
    with b.active():
        await handoff.receive(database.get_session(sid))
        assert wakeups.get_session_wakeups(sid) == []
