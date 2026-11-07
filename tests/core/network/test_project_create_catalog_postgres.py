"""Project creation is the catalog producer, before a first chat can resolve it."""
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
import threading
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
        assert peer.sql("SELECT * FROM project_catalog_aliases") == []
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
        assert peer.sql("SELECT * FROM project_catalog_aliases") == []


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
        assert project_resolver.resolve_catalog_project(local_id)["id"] == linked_id
        assert project_resolver.catalog_project_read_ids(projects_v2.get_project_v2(linked_id)) == [linked_id, local_id]
        assert len(checkouts.list_checkouts(linked_id)) == 1
        assert checkouts.list_checkouts(local_id)[0]["id"] != checkouts.list_checkouts(linked_id)[0]["id"]
        database.delete_project("project")
        assert checkouts.eligible_base_project_ids(settings.effective_instance_id) == []
        database.create_project("project", "Re-added", None, "/private/project")
        database.set_project_link_group("project", "git:example/repo")
        assert len(checkouts.list_checkouts(local_id)) == len(checkouts.list_checkouts(linked_id)) == 1
        assert checkouts.has_active_base_checkout(linked_id, settings.effective_instance_id)
        assert project_resolver.resolve_catalog_project(local_id)["id"] == local_id
        assert project_resolver.catalog_project_read_ids(projects_v2.get_project_v2(linked_id)) == [linked_id]
        database.set_project_link_group("project", None)
        assert checkouts.eligible_base_project_ids(settings.effective_instance_id) == [local_id]
        assert len(checkouts.list_checkouts(local_id)) == len(checkouts.list_checkouts(linked_id)) == 1


def test_upgrade_adds_local_provenance_without_guessing_old_links(pg_workers):
    from app.db import base

    worker = pg_workers("upgrade")
    with worker.active():
        database.create_project("old", "Old project", None, "/private/old")
        original = projects_v2.get_project_v2_by_origin("local:old")
        # The old version had the existing project/checkout but no provenance.
        worker.sql("DROP TABLE project_catalog_aliases")
        with base.get_db() as conn:
            class SchemaCursor:
                # Boot metadata checks target public; this harness owns a named
                # schema. Keep the real migration SQL, scoped to that schema.
                def execute(self, sql, params=None):
                    return cursor.execute(sql.replace("table_schema = 'public'",
                                                      "table_schema = current_schema()"), params)

                def __getattr__(self, name):
                    return getattr(cursor, name)

            cursor = conn.cursor()
            database._run_column_migrations(SchemaCursor())
            database._run_column_migrations(SchemaCursor())  # idempotent upgrade
        assert worker.sql("SELECT * FROM project_catalog_aliases") == []
        database.set_project_link_group("old", "git:example/old")
        current = project_resolver.resolve_project_v2({"project_id": "old"})
        assert project_resolver.catalog_project_read_ids(current) == [current["id"]]
        assert project_resolver.resolve_catalog_project(original["id"])["id"] == original["id"]


@pytest.mark.parametrize("first_change", ["move", "relink"])
def test_concurrent_path_update_uses_locked_live_row(pg_workers, monkeypatch, first_change):
    """The second move starts while the first write is uncommitted, not after it."""
    from app.db import base

    worker = pg_workers("moves")
    first_written, second_attempt, release = (threading.Event() for _ in range(3))
    role = threading.local()

    class Cursor:
        def __init__(self, cursor):
            self.cursor = cursor

        def execute(self, sql, params=None):
            statement = " ".join(str(sql).split()).upper()
            writing_project = statement.startswith("UPDATE PROJECTS SET")
            locking_project = statement.startswith("SELECT") and "FROM PROJECTS WHERE" in statement and "FOR UPDATE" in statement
            if getattr(role, "name", None) == "second" and (writing_project or locking_project):
                second_attempt.set()
            result = self.cursor.execute(sql, params)
            if getattr(role, "name", None) == "first" and writing_project:
                first_written.set()
                assert release.wait(5), "test did not release the first writer"
            return result

        def __getattr__(self, name):
            return getattr(self.cursor, name)

    class Connection:
        def __init__(self, connection):
            self.connection = connection

        def cursor(self, *args, **kwargs):
            return Cursor(self.connection.cursor(*args, **kwargs))

        def __getattr__(self, name):
            return getattr(self.connection, name)

    def first():
        role.name = "first"
        if first_change == "move":
            return database.update_project("moving", path="/private/B")
        return database.set_project_link_group("moving", "git:example/moving")

    def second():
        role.name = "second"
        return database.update_project("moving", path="/private/C")

    with worker.active():
        database.create_project("moving", "Moving", None, "/private/A")
        connect = base.get_connection
        with monkeypatch.context() as patcher:
            patcher.setattr(base, "get_connection", lambda: Connection(connect()))
            with ThreadPoolExecutor(max_workers=2) as pool:
                first_result = pool.submit(copy_context().run, first)
                try:
                    assert first_written.wait(5), "first writer did not reach its project write"
                    second_result = pool.submit(copy_context().run, second)
                    assert second_attempt.wait(5), "second writer did not reach the contested row"
                    assert not second_result.done()
                finally:
                    release.set()
                first_result.result(timeout=5)
                second_result.result(timeout=5)
        assert database.get_project("moving")["path"] == "/private/C"
        current = project_resolver.resolve_project_v2({"project_id": "moving"})
        live_bases = [row for row in checkouts.list_checkouts(current["id"])
                      if not row.get("branch") and row["status"] == "active"]
        assert [row["path"] for row in live_bases] == ["/private/C"]


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
