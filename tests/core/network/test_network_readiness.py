"""Focused production-readiness regressions for worker-network security paths."""

import asyncio

import pytest

from app.db import network as store
from app.network import engine, enrollment, hub, ownership, recovery
from tests.core.network.harness import Worker, exchange, join


@pytest.fixture
def workers(tmp_path):
    made = []

    def make(name):
        worker = Worker(name, tmp_path)
        made.append(worker)
        return worker

    yield make
    for worker in made:
        worker.close()
    engine.reset_for_tests()
    enrollment._task = None


def _join_response(inviter, joiner):
    with inviter.active():
        engine.create_network("Home", device_name="A")
        response = engine.admit_device(
            device_id=joiner.device_id,
            signing_public=joiner.identity.signing_public,
            encryption_public=joiner.identity.encryption_public,
            name="B",
        )
    return response


def test_rotation_marks_existing_recovery_wrap_unusable(workers):
    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Home")
    join(a, b)
    with a.active():
        created = recovery.create()
        engine.revoke_device(b.device_id, reason="lost")
        saved = store.get_state()["recovery"]
        assert recovery.status()["configured"] is False
        assert recovery.status()["stale"] is True
        with pytest.raises(recovery.RecoveryError, match="stale"):
            recovery.unwrap(created["recovery_key"], saved)


def test_learned_self_revocation_disables_ownership(workers):
    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Home")
    join(a, b)
    with a.active():
        engine.revoke_device(b.device_id, reason="lost")
    exchange(a, b)
    with b.active():
        assert engine.context().member(b.device_id).status == "revoked"
        row = {"id": "scheduled", "owner_device_id": b.device_id}
        assert ownership.execution_eligible() is False
        assert ownership.owns(row) is False
        assert ownership.decide_automation(row)[0] is False
        assert ownership.decide_connector(row) == "skip"


def test_approved_snapshot_failure_keeps_resumable_join(workers, monkeypatch):
    a, b = workers("a"), workers("b")
    response = _join_response(a, b)
    join_id = "resume-after-snapshot-failure"
    with b.active():
        store.create_join({
            "id": join_id, "role": "joiner", "status": "approved",
            "device_id": b.device_id, "name": "B", "endpoint_url": "http://a.invalid",
            "poll_secret_hash": "unused", "response": response,
        })
        monkeypatch.setattr(enrollment, "_copy_snapshot_sync", lambda *_args, **_kwargs: (_ for _ in ()).throw(
            enrollment.EnrollmentError("snapshot unavailable")))
        with pytest.raises(enrollment.EnrollmentError, match="snapshot unavailable"):
            enrollment.complete_enrollment(response, client=None, endpoint="http://a.invalid", device_name="B",
                                          legacy_instance_id=None, join_id=join_id)
        saved = store.get_join(join_id)
        assert saved["status"] == "syncing"
        assert saved["response"] == response
        assert engine.context().bootstrap_state == engine.BOOTSTRAP_SYNCING
        # Simulate process restart: state and the approved response remain,
        # while the in-memory context/task are gone.  Resuming must schedule
        # snapshot bootstrap rather than attempt a second enrollment.
        engine.reset_for_tests()
        calls = []

        async def resumed(*args):
            calls.append(args)

        monkeypatch.setattr(enrollment, "_bootstrap_join", resumed)

        async def drive_resume():
            assert enrollment.resume_enrollment() is True
            await enrollment._task

        asyncio.run(drive_resume())
        assert calls == [(join_id, "http://a.invalid", "B", None)]
        assert enrollment.cancel_enrollment() is False
        assert store.get_join(join_id)["response"] == response


def test_stale_recovery_is_tombstoned_remotely_and_retried_durably(workers, monkeypatch):
    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Home")
    join(a, b)
    with a.active():
        recovery.create()
        engine.revoke_device(b.device_id, reason="lost")
        requests = []
        monkeypatch.setattr(hub, "_call", lambda method, path, body, **_kwargs: requests.append((method, path, body)))
        hub._upload_recovery()
        assert requests == [("POST", "recovery", {"recovery": None})]
        assert store.get_state()["recovery"]["hub_tombstoned"] is True
