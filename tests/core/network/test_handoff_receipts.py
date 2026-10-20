"""Mixed-version receipts and retained staging regressions for PR 298."""
import json
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from app.db import database, runs
from app.db.base import get_db
from app.network import control, engine, handoff
from tests.core.network.test_handoff import pair  # noqa: F401
from tests.core.test_checkout_transfer import repositories  # noqa: F401
from tests.core.network.harness import converge

SID = "handoff-test"


def plain(a, b):
    for worker in (a, b):
        with worker.active(), get_db() as conn:
            conn.cursor().execute("UPDATE sessions SET project_v2_id=NULL,branch=NULL WHERE id=%s", (SID,))


def stage(a, b, token):
    with a.active():
        data = {"token": token, "sdk_session_id": "ses_source",
                "history": handoff._history(database.get_session(SID)), "_prepare_history": True}
    with b.active():
        sdk = handoff.install_history(SID, data, ".")
        handoff._mark_commit_sent(SID, token, a.device_id)
    return sdk


def age(seconds):
    old = datetime.fromtimestamp(time.time() - seconds, timezone.utc).isoformat()
    with get_db() as conn:
        conn.cursor().execute("UPDATE handoff_history_chunks SET created_at=%s WHERE session_id=%s", (old, SID))


@pytest.mark.asyncio
async def test_old_source_lost_reply_publishes_context_and_releases_admission(pair, monkeypatch):
    a, b, _, _ = pair
    plain(a, b)
    monkeypatch.setattr(handoff.asyncio, "sleep", AsyncMock())
    open_export = handoff._open_export

    def old_export(*args):
        data, size = open_export(*args)
        data.pop("handoff_receipt", None)  # Released sources don't advertise receipts.
        return data, size

    monkeypatch.setattr(handoff, "_open_export", old_export)

    async def call(device, action, body, **kwargs):
        if body.get("phase") == "cancel":
            raise control.RunnerUnreachable("source down")
        with a.active():
            result = await control.handle(action, body, from_device=b.device_id)
            if body.get("phase") == "commit":
                # Drive the real commit/outbox path, with the old source's placement shape.
                with get_db() as conn:
                    cur = conn.cursor()
                    cur.execute("UPDATE sessions SET placement=%s WHERE id=%s",
                                (json.dumps({"where": "runner:" + b.device_id}), SID))
                    cur.execute("SELECT * FROM sessions WHERE id=%s", (SID,))
                    engine.record(cur, "sessions", SID, dict(cur.fetchone()), force=True)
                raise control.RunnerUnreachable("lost old-source commit reply")
            return result

    monkeypatch.setattr(control, "call", call)
    with b.active():
        with pytest.raises(control.RunnerUnreachable):
            await handoff.receive(database.get_session(SID))
    converge([a, b])
    with b.active():
        age(handoff.TTL + 120)
        assert handoff.recover_history_staging() == 1
        row = database.get_session(SID)
        assert row["owner_device_id"] == b.device_id
        assert row["sdk_session_id"]
        assert not handoff.admission_held(SID)
        assert runs.get_active_run(SID) is None
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT count(*) AS n FROM fluid_session_nodes WHERE session_id=%s", (row["sdk_session_id"],))
            assert cur.fetchone()["n"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("refusal", ["exception", "reply"])
async def test_first_commit_refusal_retires_private_copy_even_if_cancel_refused(pair, monkeypatch, refusal):
    a, b, _, _ = pair
    plain(a, b)

    async def call(device, action, body, **kwargs):
        if body.get("phase") == "commit":
            if refusal == "reply":
                return {"committed": False, "reason": "reservation expired"}
            raise control.ControlRefused("reservation expired")
        if body.get("phase") == "cancel":
            raise control.ControlRefused("reservation expired")
        with a.active():
            return await control.handle(action, body, from_device=b.device_id)

    monkeypatch.setattr(control, "call", call)
    with b.active():
        with pytest.raises((ValueError, control.ControlRefused)):
            await handoff.receive(database.get_session(SID))
        assert not handoff.admission_held(SID)
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT count(*) AS n FROM handoff_history_chunks WHERE session_id=%s", (SID,))
            assert cur.fetchone()["n"] == 0
            cur.execute("SELECT count(*) AS n FROM fluid_session_nodes WHERE session_id<>'ses_source'")
            assert cur.fetchone()["n"] == 0


def test_retained_copy_warns_once_at_lease_and_again_after_days(pair, caplog):
    a, b, _, _ = pair
    stage(a, b, "retained")
    with b.active():
        age(handoff.TTL + 120)
        for _ in range(3):
            handoff.recover_history_staging()
        warnings = [r.getMessage() for r in caplog.records if "Retaining uncertain hand-off" in r.getMessage()]
        assert len(warnings) == 1
        assert SID in warnings[0] and a.device_id in warnings[0]
        caplog.clear()
        age(8 * 24 * 60 * 60)
        for _ in range(3):
            handoff.recover_history_staging()
        warnings = [r.getMessage() for r in caplog.records if "Retaining uncertain hand-off" in r.getMessage()]
        assert len(warnings) == 1
        assert "days" in warnings[0]
        assert handoff.admission_held(SID)  # A warning is never evidence of cancellation.


@pytest.mark.parametrize("legacy_recovery", [False, True])
def test_accept_retires_older_marked_tokens_without_waiting_for_sweep(pair, legacy_recovery):
    a, b, _, _ = pair
    old_sdk = stage(a, b, "older-marked")
    new_sdk = stage(a, b, "newer-marked")
    with b.active():
        if legacy_recovery:
            for token in ("older-marked", "newer-marked"):
                handoff._mark_commit_sent(SID, token, a.device_id, receipt_required=False)
            with get_db() as conn:
                cur = conn.cursor()
                cur.execute("UPDATE sessions SET owner_device_id=%s,placement=%s WHERE id=%s",
                            (b.device_id, json.dumps({"where": "runner:" + b.device_id}), SID))
                cur.execute("UPDATE handoff_history_chunks SET created_at=%s WHERE token=%s",
                            ("2000-01-01T00:00:00+00:00", "older-marked"))
            assert handoff.recover_history_staging() == 1
        else:
            handoff._accept_owner(SID, b.device_id, token="newer-marked")
        assert database.get_session(SID)["sdk_session_id"] == new_sdk
        assert not handoff.admission_held(SID)
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT count(*) AS n FROM handoff_history_chunks WHERE session_id=%s", (SID,))
            assert cur.fetchone()["n"] == 0
            cur.execute("SELECT count(*) AS n FROM fluid_session_nodes WHERE session_id=%s", (old_sdk,))
            assert cur.fetchone()["n"] == 0
