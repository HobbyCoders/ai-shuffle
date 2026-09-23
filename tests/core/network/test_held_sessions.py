"""A held session is published once, not retracted and republished every scan.

A held session has a foreign owner that has not published it yet, so this
worker publishes it on the owner's behalf. The owner-only scan used to retract
that record (its origin is this device, but the row is not owned) and the held
pass published it straight back, so every two-second scan wrote a delete and an
upsert for as long as the owner stayed silent.
"""
import pytest

from app.core.config import settings
from app.db import database
from app.db import network as store
from app.network import catalog, engine, transport
from tests.core.network.harness import Worker


@pytest.fixture
def worker(tmp_path):
    instance = Worker("held-sessions", tmp_path)
    yield instance
    instance.close()
    engine.reset_for_tests()
    transport.reset_for_tests()


def _session(session_id, owner, instance):
    database.create_session(session_id=session_id, profile_id="default", project_id=None, title=session_id)
    with store.get_db() as conn:
        conn.cursor().execute("UPDATE sessions SET owner_device_id = %s, instance_id = %s WHERE id = %s",
                              (owner, instance, session_id))


def _ops(record_id):
    with store.get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT op FROM sync_ops WHERE collection = 'sessions' AND record_id = %s ORDER BY seq",
                    (record_id,))
        return [r["op"] for r in cur.fetchall()]


def test_held_session_is_published_once_and_left_alone(worker):
    with worker.active():
        engine.create_network("Held sessions")
        _session("held", "silent-owner-device", settings.effective_instance_id)
        _session("own", worker.device_id, settings.effective_instance_id)

        catalog.publish_owned()
        assert _ops("held") == ["upsert"]
        own_ops = _ops("own")

        for _ in range(3):
            catalog.publish_owned()

        assert _ops("held") == ["upsert"]
        assert _ops("own") == own_ops


def test_session_that_is_neither_owned_nor_held_is_retracted_once(worker):
    with worker.active():
        engine.create_network("Held sessions")
        _session("gone", worker.device_id, settings.effective_instance_id)
        catalog.publish_owned()
        assert _ops("gone") == ["upsert"]

        # A foreign owner and no install id: nothing for this worker to hold.
        with store.get_db() as conn:
            conn.cursor().execute("UPDATE sessions SET owner_device_id = %s, instance_id = NULL WHERE id = %s",
                                  ("new-owner-device", "gone"))
        for _ in range(3):
            catalog.publish_owned()

        assert _ops("gone") == ["upsert", "delete"]


def test_handed_off_session_stays_published_until_its_owner_speaks(worker):
    with worker.active():
        engine.create_network("Held sessions")
        _session("moved", worker.device_id, settings.effective_instance_id)
        catalog.publish_owned()

        with store.get_db() as conn:
            conn.cursor().execute("UPDATE sessions SET owner_device_id = %s WHERE id = %s",
                                  ("new-owner-device", "moved"))
        for _ in range(3):
            catalog.publish_owned()

        # Held for the new owner: one upsert carries the new owner, then quiet.
        assert _ops("moved") == ["upsert", "upsert"]
