"""A peer's Claude usage row is attributed to the device that published it.

Usage rows are keyed by the publisher's instance id. Peers used to name the
row's device only through the legacy instance id in the publisher's
membership certificate, which account joins left empty, so the row was
dropped as a departed install and the session's usage read "not reported".
"""
import contextlib
from unittest.mock import patch

from app.core.config import settings
from app.core.usage_limits import UsageLimitsMonitor
from app.db import database
from app.network import engine
from tests.core.network.harness import exchange, join

WINDOWS = [{"type": "five_hour", "utilization": 64.0, "resets_at": None}]


@contextlib.contextmanager
def _as(worker, instance_id):
    with worker.active(), patch.object(settings, "instance_id", instance_id):
        yield worker


def _publish_usage(worker, instance_id):
    with _as(worker, instance_id):
        database.upsert_usage_snapshot(instance_id, WINDOWS, True, 1, 1_000)
        engine.scan_all()


def _peer_entries(worker, instance_id):
    with _as(worker, instance_id):
        rows = UsageLimitsMonitor()._read_cluster_rows(instance_id)
    return {row["instance_id"]: row for row in rows or []}


def test_a_row_from_a_member_without_a_legacy_id_names_its_publisher(workers):
    a, b = workers("a"), workers("b")
    with _as(a, "ai-shuffle-a"):
        engine.create_network("Home", device_name="Unraid", legacy_instance_id="ai-shuffle-a")
    # An account join: the certificate carries no instance id for B.
    join(a, b, name="DesktopWorker")
    _publish_usage(b, "ai-shuffle-b")
    exchange(b, a)

    entry = _peer_entries(a, "ai-shuffle-a")["ai-shuffle-b"]
    assert entry["device_id"] == b.device_id
    assert entry["device_name"] == "DesktopWorker"
    assert entry["available"] is True and entry["windows"][0]["utilization"] == 64.0


def test_rows_stored_before_the_update_are_attributed_on_read(workers):
    """Nothing is rewritten: a row a peer already holds resolves on its next read."""
    a, b = workers("a"), workers("b")
    with _as(a, "ai-shuffle-a"):
        engine.create_network("Home", device_name="Unraid", legacy_instance_id="ai-shuffle-a")
    join(a, b, name="DesktopWorker")
    _publish_usage(b, "ai-shuffle-b")
    exchange(b, a)
    # The old read path had no publisher lookup and dropped the row.
    with patch.object(database, "get_usage_snapshot_publishers", return_value={}):
        assert "ai-shuffle-b" not in _peer_entries(a, "ai-shuffle-a")
    assert _peer_entries(a, "ai-shuffle-a")["ai-shuffle-b"]["device_id"] == b.device_id


def test_a_member_with_a_legacy_id_still_resolves_either_way(workers):
    """A publisher on an older build sends the same row; the legacy alias and
    the publisher lookup agree, and either alone is enough."""
    a, b = workers("a"), workers("b")
    with _as(a, "ai-shuffle-a"):
        engine.create_network("Home", device_name="Unraid", legacy_instance_id="ai-shuffle-a")
    join(a, b, name="DesktopWorker", legacy_instance_id="ai-shuffle-b")
    _publish_usage(b, "ai-shuffle-b")
    exchange(b, a)
    assert _peer_entries(a, "ai-shuffle-a")["ai-shuffle-b"]["device_id"] == b.device_id
    with patch.object(database, "get_usage_snapshot_publishers", return_value={}):
        assert _peer_entries(a, "ai-shuffle-a")["ai-shuffle-b"]["device_id"] == b.device_id


def test_the_founders_row_reaches_a_joiner_with_its_device(workers):
    a, b = workers("a"), workers("b")
    with _as(a, "ai-shuffle-a"):
        engine.create_network("Home", device_name="Unraid", legacy_instance_id="ai-shuffle-a")
    join(a, b, name="DesktopWorker")
    _publish_usage(a, "ai-shuffle-a")
    exchange(a, b)
    assert _peer_entries(b, "ai-shuffle-b")["ai-shuffle-a"]["device_id"] == a.device_id


def test_a_row_from_a_removed_device_is_still_dropped(workers):
    a, b = workers("a"), workers("b")
    with _as(a, "ai-shuffle-a"):
        engine.create_network("Home", device_name="Unraid", legacy_instance_id="ai-shuffle-a")
    join(a, b, name="DesktopWorker")
    _publish_usage(b, "ai-shuffle-b")
    exchange(b, a)
    with _as(a, "ai-shuffle-a"):
        engine.revoke_device(b.device_id)
    assert "ai-shuffle-b" not in _peer_entries(a, "ai-shuffle-a")


def test_a_row_this_device_published_under_an_old_instance_id_is_not_a_second_device(workers):
    """The publisher lookup must not turn this device's stale row into a peer."""
    a, b = workers("a"), workers("b")
    with _as(a, "ai-shuffle-a"):
        engine.create_network("Home", device_name="Unraid", legacy_instance_id="ai-shuffle-a")
    join(a, b, name="DesktopWorker")
    _publish_usage(b, "ai-shuffle-b-old")
    _publish_usage(b, "ai-shuffle-b")
    entries = _peer_entries(b, "ai-shuffle-b")
    assert "ai-shuffle-b-old" not in entries
    assert entries["ai-shuffle-b"]["device_id"] == b.device_id
