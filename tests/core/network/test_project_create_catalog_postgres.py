"""Project creation is the catalog producer, before a first chat can resolve it."""
from unittest.mock import patch

import pytest

from app.api.cluster import _build_cluster_catalog
from app.core import project_resolver, resource_sync
from app.core.config import settings
from app.db import checkouts, database, projects_v2
from app.network import engine, startup, transport
from tests.core.network.test_transcript_op_retention_postgres import (  # noqa: F401
    admit, cluster, pg_workers, sync,
)
from tests.core.network.test_transport import ServedBy


@pytest.fixture
def wakes(monkeypatch):
    # Install the real post-commit resource bridge, without leaking its wrapper
    # into unrelated tests. Capture scheduling, not publication, here.
    monkeypatch.setattr(resource_sync, "_emit", resource_sync._emit)
    monkeypatch.setattr(resource_sync, "_network_hooked",
                        getattr(resource_sync, "_network_hooked", False), raising=False)
    startup._install_resource_hook()
    calls = []
    monkeypatch.setattr(transport, "request_scan", lambda collections, **kw: calls.append(collections))
    return calls


async def test_create_commits_catalog_then_wakes_and_reaches_peer_without_chat(pg_workers, wakes):
    source, peer = pg_workers("source"), pg_workers("peer")
    with source.active():
        engine.create_network("Projects")
    admit(source, peer)
    wakes.clear()
    origin = "local:new-project"
    pv2_id = projects_v2.project_v2_id_for_origin(origin)
    with source.active():
        # At the wake boundary, a different connection must see all three rows.
        def wake(collections, **kw):
            assert source.sql("SELECT id FROM projects WHERE id = 'new-project'")
            assert source.sql("SELECT id FROM projects_v2 WHERE id = %s", (pv2_id,))
            assert source.sql("SELECT id FROM checkouts WHERE project_id = %s", (pv2_id,))
            wakes.append(collections)

        with patch.object(transport, "request_scan", wake):
            database.create_project("new-project", "New project", None, "/private/project")
        assert wakes == [["projects_v2"]]
        assert not source.sql("SELECT id FROM sessions")
        # Only the requested targeted pass; no full reconciliation or resolver.
        assert engine.scan_collection(wakes[0][0]) == 1
        owner_bases = checkouts.eligible_base_project_ids(settings.effective_instance_id)
    await sync(peer, ServedBy(source, "https://source.example.com"))
    with peer.active():
        assert projects_v2.get_project_v2(pv2_id)["name"] == "New project"
        assert not checkouts.list_checkouts(pv2_id)  # paths never replicate
        visible = _build_cluster_catalog(
            bases=[{"project_id": pid, "runner_id": source.device_id} for pid in owner_bases],
            pv2s=projects_v2.list_projects_v2(),
            installs=[{"id": source.device_id, "online": True, "approved": True}],
            local_projects=database.get_all_projects(), me=peer.device_id,
        )
        assert [row["project_v2_id"] for row in visible] == [pv2_id]
        wakes.clear()
        assert engine.scan_collection("projects_v2") == 0  # no import echo
        assert wakes == []

    # Eager creation also means every later link starts with a placeholder.
    # The owner must stop advertising it, including to a peer with no legacy row.
    with source.active():
        database.set_project_link_group("new-project", "git:example/repo")
        linked_id = projects_v2.project_v2_id_for_origin("git:example/repo")
        assert engine.scan_collection("projects_v2") == 1
        owner_bases = checkouts.eligible_base_project_ids(settings.effective_instance_id)
        assert owner_bases == [linked_id]
    await sync(peer, ServedBy(source, "https://source.example.com"))
    with peer.active():
        visible = _build_cluster_catalog(
            bases=[{"project_id": pid, "runner_id": source.device_id} for pid in owner_bases],
            pv2s=projects_v2.list_projects_v2(),
            installs=[{"id": source.device_id, "online": True, "approved": True}],
            local_projects=database.get_all_projects(), me=peer.device_id,
        )
        assert [row["project_v2_id"] for row in visible] == [linked_id]


def test_create_resolve_relink_delete_and_readd_are_idempotent(pg_workers, wakes):
    worker = pg_workers("owner")
    with worker.active():
        project = database.create_project("project", "Project", None, "/private/project")
        local_id = projects_v2.project_v2_id_for_origin("local:project")
        assert projects_v2.get_project_v2(local_id)
        assert project_resolver.resolve_project_v2({"project_id": project["id"]})["id"] == local_id
        assert project_resolver.resolve_project_v2({"project_id": project["id"]})["id"] == local_id
        assert len(checkouts.list_checkouts(local_id)) == 1
        database.set_project_link_group("project", "git:example/repo")
        linked_id = projects_v2.project_v2_id_for_origin("git:example/repo")
        assert project_resolver.resolve_project_v2({"project_id": "project"})["id"] == linked_id
        assert len(checkouts.list_checkouts(linked_id)) == 1
        assert checkouts.list_checkouts(local_id)[0]["id"] != checkouts.list_checkouts(linked_id)[0]["id"]
        database.delete_project("project")
        assert checkouts.eligible_base_project_ids(settings.effective_instance_id) == []
        database.create_project("project", "Re-added", None, "/private/project")
        database.set_project_link_group("project", "git:example/repo")
        assert len(checkouts.list_checkouts(local_id)) == len(checkouts.list_checkouts(linked_id)) == 1
        assert checkouts.has_active_base_checkout(linked_id, settings.effective_instance_id)
        database.set_project_link_group("project", None)
        assert checkouts.eligible_base_project_ids(settings.effective_instance_id) == [local_id]
        assert len(checkouts.list_checkouts(local_id)) == len(checkouts.list_checkouts(linked_id)) == 1


def test_catalog_failure_rolls_back_creation_without_wake(pg_workers, wakes, monkeypatch):
    worker = pg_workers("owner")
    with worker.active():
        def fail(*args):
            raise RuntimeError("checkout insert failed")

        monkeypatch.setattr(checkouts, "seed_legacy_base_checkout", fail)
        wakes.clear()
        with pytest.raises(RuntimeError, match="checkout insert failed"):
            database.create_project("broken", "Broken", None, "/private/broken")
        assert database.get_project("broken") is None
        assert projects_v2.get_project_v2_by_origin("local:broken") is None
        assert wakes == []


def test_old_worker_lazy_seed_still_wakes_once_after_commit(pg_workers, wakes):
    worker = pg_workers("owner")
    with worker.active():
        worker.sql("INSERT INTO projects (id, name, path, instance_id) VALUES (%s, %s, %s, %s)",
                   ("old", "Old worker", "/private/old", settings.effective_instance_id))
        wakes.clear()
        row = project_resolver.resolve_project_v2({"project_id": "old"})
        assert row["origin"] == "local:old"
        assert wakes == [["projects_v2"]]
        project_resolver.resolve_project_v2({"project_id": "old"})
        assert wakes == [["projects_v2"]]
        assert len(checkouts.list_checkouts(row["id"])) == 1
