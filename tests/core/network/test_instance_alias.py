# ruff: noqa: F811
"""A worker's install (instance) id on its network membership.

Rows keyed by an install's instance id, and lookups such as
``get_install(<this install's id>)`` at startup, find a worker only through
the legacy instance id on its member row. Account (hub) joins never sent it,
so every worker admitted through the account had none: its own startup
treated it as an unapproved install and its peers dropped its usage rows.
"""
import contextlib
from datetime import datetime, timedelta, timezone
from itertools import count
from unittest.mock import patch

import httpx
import pytest

from app.core.config import settings
from app.db import database
from app.db import network as store
from app.network import engine, hub, runners
from app.network import startup as network_startup
from tests.core.network.harness import exchange, join
from tests.core.network.test_hub import _signed_in, fake, workers  # noqa: F401


@pytest.fixture(autouse=True)
def ordered_membership_timestamps(monkeypatch):
    # Windows' clock can return the same timestamp for successive membership
    # writes. The SQLite harness then sorts certificates by random device id
    # before their issuer, unrelated to the alias behavior under test.
    start = datetime.now(timezone.utc)
    ticks = count()
    monkeypatch.setattr(store, "_now", lambda: (start + timedelta(microseconds=next(ticks))).isoformat())


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
        engine.create_network("Home", device_name="ServerWorker", legacy_instance_id="inst-a")
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
    # A later join receives the inviter's existing legacy map, but snapshot
    # presence must not synthesize a member alias from an unauthenticated item.
    join(a, c, name="Laptop", legacy_instance_id="inst-c")
    assert _legacy(c, b.device_id) is None
    with as_install(c, "inst-c"):
        assert runners.device_for_instance("inst-b") == b.device_id
        assert c.sql("SELECT * FROM network_instance_aliases") == []
    with as_install(b, "inst-b"):
        engine.publish_presence(version="republished")
    exchange(b, c)
    assert _legacy(c, b.device_id) == "inst-b"


def test_repair_never_approves_another_install_or_takes_another_devices_id(workers):
    a, b, c = workers("a"), workers("b"), workers("c")
    with as_install(a, "inst-a"):
        engine.create_network("Home", device_name="ServerWorker", legacy_instance_id="inst-a")
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


def _forge_presence(worker, device_id, row):
    """Sign a presence op about ``device_id`` from ``worker``'s own stream."""
    with worker.active():
        with store.get_db() as conn:
            assert engine.record(conn.cursor(), "presence", device_id, row, ctx=engine.context()) is not None


def test_presence_applies_only_from_the_device_it_describes(workers):
    """A member's signed presence op about another device is refused on apply.

    Presence carries a device's endpoints and install id. Without this, any
    member could replace another device's reachability on every peer, and
    the install id in presence could not be trusted for the #1716 lookups.
    """
    a, b, c = workers("a"), workers("b"), workers("c")
    with as_install(a, "inst-a"):
        engine.create_network("Home", device_name="ServerWorker", legacy_instance_id="inst-a")
    join(a, b, name="DesktopWorker", legacy_instance_id="inst-b")
    join(a, c, name="Laptop", legacy_instance_id="inst-c")
    with as_install(b, "inst-b"):
        engine.publish_presence(version="b-1", endpoints=["http://10.0.0.2:8000"])
    # Accepted: the device's own presence.
    assert exchange(b, a).applied >= 1
    exchange(b, c)
    with a.active():
        assert engine.presence_of(b.device_id)["endpoints"] == ["http://10.0.0.2:8000"]

    forged = {"device_id": b.device_id, "endpoints": ["http://10.6.6.6:8000"], "capabilities": {},
              "version": "b-1", "platform": "linux", "incarnation": 1, "instance_id": "inst-forged",
              "updated_at": "2099-01-01T00:00:00+00:00"}
    _forge_presence(c, b.device_id, forged)
    # A row whose key names the sender but whose body names another device.
    _forge_presence(c, c.device_id, dict(forged, device_id=b.device_id))
    result = exchange(c, a)
    assert result.rejected == 2
    assert result.reasons.count("presence for another device") == 2
    with a.active():
        assert engine.presence_of(b.device_id)["endpoints"] == ["http://10.0.0.2:8000"]
        assert engine.presence_of(c.device_id) is None or \
            engine.presence_of(c.device_id)["device_id"] == c.device_id
        assert runners.device_for_instance("inst-forged") is None
        assert database.get_install("inst-forged") is None
    # Nor can a member retract another device's presence.
    with c.active():
        with store.get_db() as conn:
            assert engine.record(conn.cursor(), "presence", b.device_id, None, ctx=engine.context()) is not None
    assert exchange(c, a).rejected == 1
    with a.active():
        assert engine.presence_of(b.device_id)["endpoints"] == ["http://10.0.0.2:8000"]


def test_scan_never_republishes_another_devices_presence(workers):
    """The scanner publishes and retracts only this device's own presence row."""
    a, b = workers("a"), workers("b")
    with as_install(a, "inst-a"):
        engine.create_network("Home", device_name="ServerWorker", legacy_instance_id="inst-a")
    join(a, b, name="DesktopWorker", legacy_instance_id="inst-b")
    with as_install(b, "inst-b"):
        engine.publish_presence(version="b-1", endpoints=["http://10.0.0.2:8000"])
    exchange(b, a)
    with as_install(a, "inst-a"):
        # A local copy of a peer's row that drifted from the network's view.
        a.sql("UPDATE network_presence SET version = 'drifted' WHERE device_id = ?", (b.device_id,))
        assert engine.scan_collection("presence") == 0
        a.sql("DELETE FROM network_presence WHERE device_id = ?", (b.device_id,))
        assert engine.scan_collection("presence") == 0


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
        engine.create_network("Home", device_name="ServerWorker", legacy_instance_id="inst-a")
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


@pytest.mark.parametrize("legacy", [None, "inst-old"])
@pytest.mark.parametrize("lookup", ["install", "approved", "db-map", "runner-map", "device", "runner", "git", "owner", "backfill", "placement", "enrichment", "adopt"])
def test_signed_current_instance_resolves_on_a_peer(workers, monkeypatch, legacy, lookup):
    from app.network import control, git as network_git, transport

    a, b = workers("a"), workers("b")
    with as_install(a, "inst-a"):
        engine.create_network("Home", legacy_instance_id="inst-a")
    join(a, b, legacy_instance_id=legacy)
    with as_install(b, "inst-current"):
        engine.publish_presence()
    assert exchange(b, a).applied >= 1
    monkeypatch.setattr(transport, "online_devices", lambda: {b.device_id})
    with as_install(a, "inst-a"):
        # Reload to prove the signed claim survives a process restart.
        engine.load_context()
        if lookup == "install":
            assert database.get_install("inst-current")["id"] == b.device_id
        elif lookup == "approved":
            assert database.is_install_approved("inst-current")
        elif lookup == "db-map":
            assert database.get_installs_map()["inst-current"]["id"] == b.device_id
        elif lookup == "runner-map":
            assert runners.installs_map()["inst-current"]["device_id"] == b.device_id
        elif lookup == "device":
            assert runners.device_for_instance("inst-current") == b.device_id
        elif lookup == "runner":
            assert runners.resolve_runner_id("inst-current") == b.device_id
        elif lookup == "git":
            assert network_git.device_id("inst-current") == b.device_id
        elif lookup == "owner":
            assert control.owner_of({"instance_id": "inst-current"}) == b.device_id
        elif lookup == "placement":
            from app.core import placement
            assert placement.resolve_placement({"instance_id": "inst-current"}).kind == "runner"
        elif lookup == "enrichment":
            from app.api.sessions_helpers import _annotate_cluster_fields
            session = {"id": "legacy-owner", "instance_id": "inst-current"}
            _annotate_cluster_fields([session])
            assert session["device_name"] == "b"
            assert session["controllable"]
        elif lookup == "adopt":
            from app.api.sessions import _adopt_device_name
            assert _adopt_device_name("inst-current") == "b"
        else:
            database.create_session("legacy-owner", "default", title="Legacy owner")
            a.sql("UPDATE sessions SET owner_device_id = NULL, instance_id = ? WHERE id = ?",
                  ("inst-current", "legacy-owner"))
            network_startup.backfill_owner_device_ids(a.device_id, include_unscoped=False)
            assert a.sql("SELECT owner_device_id FROM sessions WHERE id = ?", ("legacy-owner",))[0]["owner_device_id"] == b.device_id
        if legacy:
            assert runners.device_for_instance(legacy) == b.device_id
            assert _legacy(a, b.device_id) == legacy


def test_snapshot_presence_does_not_authorize_a_second_alias(workers):
    a, b, c = workers("a"), workers("b"), workers("c")
    with as_install(a, "inst-a"):
        engine.create_network("Home", legacy_instance_id="inst-a")
    join(a, b, legacy_instance_id="inst-old")
    with as_install(b, "inst-current"):
        engine.publish_presence()
    exchange(b, a)
    join(a, c, legacy_instance_id="inst-c")
    with as_install(c, "inst-c"):
        assert engine.presence_of(b.device_id)["instance_id"] == "inst-current"
        assert runners.device_for_instance("inst-current") is None
        assert database.get_install("inst-current") is None
    # A fresh envelope, including one forwarded by another peer, supplies proof.
    with as_install(b, "inst-current"):
        engine.publish_presence(version="republished")
    exchange(b, a)
    exchange(a, c)
    with as_install(c, "inst-c"):
        assert runners.device_for_instance("inst-current") == b.device_id


def test_current_alias_collisions_and_revocation_fail_closed(workers):
    from app.network import git as network_git

    a, b, c = workers("a"), workers("b"), workers("c")
    with as_install(a, "inst-a"):
        engine.create_network("Home", legacy_instance_id="inst-a")
    join(a, b, legacy_instance_id="inst-b")
    join(a, c, legacy_instance_id="inst-c")
    with as_install(b, "inst-a"):
        engine.publish_presence()
    exchange(b, a)
    with as_install(a, "inst-a"):
        assert runners.device_for_instance("inst-a") == a.device_id
    for worker in (b, c):
        with as_install(worker, "inst-shared"):
            engine.publish_presence()
        exchange(worker, a)
    with as_install(a, "inst-a"):
        assert runners.device_for_instance("inst-shared") is None
        assert "inst-shared" not in database.get_installs_map()
        assert "inst-shared" not in runners.installs_map()
        with pytest.raises(ValueError, match="Unknown or revoked"):
            network_git.device_id("inst-shared")
        engine.revoke_device(c.device_id)
        assert runners.device_for_instance("inst-shared") == b.device_id
        engine.revoke_device(b.device_id)
        assert runners.device_for_instance("inst-shared") is None
        assert runners.device_for_instance("inst-b") is None


def test_only_the_latest_signed_current_alias_is_kept(workers):
    a, b = workers("a"), workers("b")
    with as_install(a, "inst-a"):
        engine.create_network("Home", legacy_instance_id="inst-a")
    join(a, b, legacy_instance_id="inst-old")
    for alias in ("inst-first", "inst-second"):
        with as_install(b, alias):
            engine.publish_presence()
        exchange(b, a)
    with as_install(a, "inst-a"):
        assert runners.device_for_instance("inst-old") == b.device_id
        assert runners.device_for_instance("inst-first") is None
        assert runners.device_for_instance("inst-second") == b.device_id
    # Only a signed deletion by that device retracts its verified current id.
    _forge_presence(b, b.device_id, None)
    exchange(b, a)
    with as_install(a, "inst-a"):
        assert runners.device_for_instance("inst-second") is None
        assert runners.device_for_instance("inst-old") == b.device_id
