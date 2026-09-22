"""Usage authority follows durable membership, not online-peer election."""

import pytest

from app.core.usage_polling import UsageDeferred
from app.db import network as store
from app.network import engine, usage_budget
from tests.core.network.harness import Worker, join


def test_coordinator_uses_durable_founder_and_fences_revoked_workers(tmp_path):
    founder, peer = Worker("founder", tmp_path), Worker("peer", tmp_path)
    try:
        with founder.active():
            engine.create_network("budget-test")
            assert usage_budget.coordinator().device_id == founder.device_id
        join(founder, peer)
        with peer.active():
            assert usage_budget.coordinator().founder_device_id == founder.device_id
            # Changing only durable membership also invalidates a cached context.
            peer.raw.execute("UPDATE network_members SET status='revoked' WHERE device_id=?", (peer.device_id,))
            with pytest.raises(UsageDeferred):
                usage_budget.coordinator()
    finally:
        founder.close()
        peer.close()
        engine.reset_for_tests()


def test_missing_founder_never_elects_the_remaining_worker(tmp_path):
    worker = Worker("only-online", tmp_path)
    try:
        with worker.active():
            engine.create_network("budget-test")
            store.update_state(founder_device_id="missing-founder")
            with pytest.raises(UsageDeferred):
                usage_budget.coordinator()
    finally:
        worker.close()
        engine.reset_for_tests()
