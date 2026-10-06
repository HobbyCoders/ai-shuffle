# ruff: noqa: F811
"""A worker's install (instance) id on its network membership.

Rows keyed by an install's instance id, and lookups such as
``get_install(<this install's id>)`` at startup, find a worker only through
the legacy instance id on its member row. Account (hub) joins never sent it,
so every worker admitted through the account had none: its own startup
treated it as an unapproved install and its peers dropped its usage rows.
"""
import contextlib
from unittest.mock import patch

import httpx

from app.core.config import settings
from app.db import database
from app.db import network as store
from app.network import engine, hub, runners
from app.network import startup as network_startup
from tests.core.network.harness import exchange, join
from tests.core.network.test_hub import _signed_in, fake, workers  # noqa: F401


@contextlib.contextmanager
def as_install(worker, instance_id):
    """Run as ``worker`` with its own install id (the real one is process-cached)."""
    with worker.active(), patch.object(settings, "instance_id", instance_id):
        yield worker


def _legacy(worker, device_id):
    with worker.active():
        return (store.get_member(device_id) or {}).get("legacy_instance_id")


async def test_an_account_join_records_the_joiners_install_id(workers, fake, monkeypatch):
    """Through the real account path: register, approval by a member, bootstrap."""
    a, b = workers("a"), workers("b")
    with as_install(a, "inst-a"):
        _signed_in(fake, a)
        assert await hub._loop_body() is True  # founds the account's network
    with as_install(b, "inst-b"):
        _signed_in(fake, b)
        hub.register()
    with as_install(a, "inst-a"):
        _signed_in(fake, a)
        hub.approve_device(b.device_id)
    from tests.core.network.test_transport import ServedBy
    served = ServedBy(a, "https://a.example.com")
    fake.approvals[b.device_id]["issuer"]["endpoints"] = [served.base_url]
    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real_client(transport=served.transport(), **kwargs))
    with as_install(b, "inst-b"):
        _signed_in(fake, b)
        assert await hub._loop_body() is True
        assert engine.context().bootstrap_state == "ready"
        # The joiner finds itself by its own install id: startup approval.
        assert database.is_install_approved("inst-b")
    assert _legacy(b, b.device_id) == "inst-b"
    assert _legacy(a, b.device_id) == "inst-b"
    with as_install(a, "inst-a"):
        assert runners.device_for_instance("inst-b") == b.device_id


def test_startup_repairs_a_membership_without_an_install_id(workers):
    """A worker admitted before the fix: its certificate names no install id."""
    a, b, c = workers("a"), workers("b"), workers("c")
    with as_install(a, "inst-a"):
        engine.create_network("Home", device_name="Unraid", legacy_instance_id="inst-a")
    with as_install(b, "inst-b"):
        pass
    # Admitted directly without an install id: the record such a join left.
    join(a, b, name="DesktopWorker", legacy_instance_id=None)
    with as_install(b, "inst-b"):
        # The gap as found on the affected worker. Lookups of its own id
        # resolve through the ``is_self`` fallbacks (#1710, #1716); the alias
        # itself is missing, so its peers cannot map the id yet.
        assert _legacy(b, b.device_id) is None
        assert "inst-b" not in engine.context().legacy_instance_map
        assert database.get_installs_map()["inst-b"]["id"] == b.device_id
        network_startup.initialize()
        assert _legacy(b, b.device_id) == "inst-b"
        assert database.is_install_approved("inst-b")
        assert database.get_installs_map()["inst-b"]["id"] == b.device_id
        assert engine.context().legacy_instance_map["inst-b"] == b.device_id
        # Idempotent: a second boot changes nothing.
        assert engine.heal_own_instance_alias() is False
        engine.publish_presence(version=settings.version)
    # Its peers learn the id from that device's own presence record.
    assert _legacy(a, b.device_id) is None
    exchange(b, a)
    assert _legacy(a, b.device_id) == "inst-b"
    with as_install(a, "inst-a"):
        assert runners.device_for_instance("inst-b") == b.device_id
        assert database.get_installs_map()["inst-b"]["id"] == b.device_id
    # A worker that joins later learns it from the snapshot.
    with as_install(c, "inst-c"):
        pass
    join(a, c, name="Laptop", legacy_instance_id="inst-c")
    assert _legacy(c, b.device_id) == "inst-b"


def test_repair_never_approves_another_install_or_takes_another_devices_id(workers):
    a, b, c = workers("a"), workers("b"), workers("c")
    with as_install(a, "inst-a"):
        engine.create_network("Home", device_name="Unraid", legacy_instance_id="inst-a")
    join(a, b, name="DesktopWorker", legacy_instance_id=None)
    join(a, c, name="Laptop", legacy_instance_id=None)
    with as_install(b, "inst-b"):
        assert engine.heal_own_instance_alias() is True
        # A different install pointed at this database is still unapproved.
        assert database.get_install("inst-stray") is None
        assert not database.is_install_approved("inst-stray")
    with as_install(a, "inst-a"):
        # The repair fills only this worker's own row, never a peer's.
        assert engine.heal_own_instance_alias() is False
        assert _legacy(a, b.device_id) is None
        ctx = engine.context()
        with store.get_db() as conn:
            cursor = conn.cursor()
            # A device cannot name another device's alias...
            engine._learn_presence_alias(cursor, ctx, {"device_id": b.device_id, "instance_id": "inst-x"},
                                         c.device_id)
            # ...nor claim an install id an active member already holds.
            engine._learn_presence_alias(cursor, ctx, {"device_id": c.device_id, "instance_id": "inst-a"},
                                         c.device_id)
        assert _legacy(a, b.device_id) is None
        assert _legacy(a, c.device_id) is None
        assert engine.context().legacy_instance_map.get("inst-a") == a.device_id
        assert runners.device_for_instance("inst-x") is None


async def test_a_worker_awaiting_admission_stays_unapproved(workers, fake):
    a, b = workers("a"), workers("b")
    with as_install(a, "inst-a"):
        _signed_in(fake, a)
        assert await hub._loop_body() is True
    with as_install(b, "inst-b"):
        _signed_in(fake, b)
        assert hub.register()["status"] == "pending"
        # Registered with the account but not admitted: no network, nothing to repair.
        assert engine.context() is None
        assert engine.heal_own_instance_alias() is False
    with as_install(a, "inst-a"):
        assert database.get_install("inst-b") is None
        assert not database.is_install_approved("inst-b")
        assert store.get_member(b.device_id) is None


def test_a_changed_install_id_still_finds_this_worker(workers):
    """#1716 case 2: the certificate names an older install id than the one running.

    The repair never replaces a recorded alias, so every lookup of this
    install's own current id must fall back to its own row, the way
    ``get_install`` and ``device_for_instance`` already did.
    """
    from app.network import git as network_git

    a, b = workers("a"), workers("b")
    with as_install(a, "inst-a"):
        engine.create_network("Home", device_name="Unraid", legacy_instance_id="inst-a")
    join(a, b, name="DesktopWorker", legacy_instance_id="inst-old")
    with as_install(b, "inst-new"):
        assert engine.heal_own_instance_alias() is False
        assert _legacy(b, b.device_id) == "inst-old"
        assert database.get_install("inst-new")["id"] == b.device_id
        assert runners.device_for_instance("inst-new") == b.device_id
        # Session enrichment, placement and usage rows keyed by the current id.
        assert database.get_installs_map()["inst-new"]["id"] == b.device_id
        assert runners.installs_map()["inst-new"]["device_id"] == b.device_id
        assert network_git.device_id("inst-new") == b.device_id
        # The older alias and the peer still resolve as before.
        assert database.get_installs_map()["inst-old"]["id"] == b.device_id
        assert database.get_installs_map()["inst-a"]["id"] == a.device_id
        assert network_git.device_id("inst-a") == a.device_id
    with as_install(a, "inst-a"):
        # Only the worker itself knows its new id; this is the open part of #1716.
        assert "inst-new" not in database.get_installs_map()
        assert runners.device_for_instance("inst-new") is None
