"""A chat another worker runs names a local project only when it is the same project (#1659).

Legacy project ids are per-worker slugs: two workers can each have a project
called ``sandbox`` that are unrelated. A mirrored chat's row used to keep the
owner's legacy id whenever a project of that id existed here, so this worker's
sidebar, header, uploads and file listings all used the wrong project.
"""
from app.api.cluster import _build_cluster_catalog
from app.core.project_resolver import is_checkout_of, local_rows_by_origin
from app.db import database
from app.db.base import get_db
from app.db.projects_v2 import project_v2_id_for_origin
from app.network import catalog

OWNER = "dev-owner"
VIEWER = "dev-viewer"
OWNERS_PROJECT = project_v2_id_for_origin("git:acme/owners-repo")


def _mirror(record):
    with get_db() as conn:
        return catalog.prepare_session_mirror(conn.cursor(), record, origin_device_id=OWNER, device_id=VIEWER)


def _record(**extra):
    return {"id": "remote-chat", "project_id": "sandbox", "project_v2_id": OWNERS_PROJECT,
            "owner_device_id": OWNER, **extra}


def test_an_unrelated_project_with_the_owners_id_is_not_named(workers):
    with workers("viewer").active():
        database.create_project("sandbox", "Viewer sandbox", None, "/v", link_group_id="git:acme/viewers-repo")
        assert _mirror(_record())["project_id"] is None


def test_this_workers_checkout_of_the_same_project_is_named(workers):
    with workers("viewer").active():
        database.create_project("sandbox", "Sandbox", None, "/v", link_group_id="git:acme/owners-repo")
        assert _mirror(_record())["project_id"] == "sandbox"


def test_a_chat_this_worker_runs_keeps_its_own_binding(workers):
    viewer = workers("viewer")
    with viewer.active():
        database.create_project("sandbox", "Viewer sandbox", None, "/v", link_group_id="git:acme/viewers-repo")
        database.create_project("mine", "Mine", None, "/m", link_group_id="git:acme/owners-repo")
        database.create_session(session_id="remote-chat", profile_id="default", title="t")
        viewer.sql("UPDATE sessions SET project_id = 'mine', project_v2_id = ?, owner_device_id = ? WHERE id = ?",
                   (OWNERS_PROJECT, VIEWER, "remote-chat"))
        # A late copy from the previous owner names its own slug: ignored.
        assert _mirror(_record(owner_device_id=VIEWER))["project_id"] == "mine"


def _viewer_with_both_sandboxes(viewer):
    """The viewer's ``sandbox`` is unrelated; ``owners-checkout`` is its checkout of the chat's project."""
    database.create_project("sandbox", "Viewer sandbox", None, "/v", link_group_id="git:acme/viewers-repo")
    database.create_project("owners-checkout", "Owners", None, "/o", link_group_id="git:acme/owners-repo")
    database.create_session(session_id="remote-chat", profile_id="default", title="t")
    viewer.sql("UPDATE sessions SET project_id = NULL, project_v2_id = ?, owner_device_id = ? WHERE id = ?",
               (OWNERS_PROJECT, OWNER, "remote-chat"))


def test_handoff_sync_record_first_binds_this_workers_checkout(workers):
    """The owner publishes its ownership flip with its own slug before this worker accepts."""
    viewer = workers("viewer")
    with viewer.active():
        _viewer_with_both_sandboxes(viewer)
        assert _mirror(_record(owner_device_id=VIEWER))["project_id"] == "owners-checkout"
        viewer.sql("DELETE FROM projects WHERE id = 'owners-checkout'")
        # No checkout of the shared project here: none rather than the unrelated slug.
        assert _mirror(_record(owner_device_id=VIEWER))["project_id"] is None


def test_handoff_accept_first_binds_this_workers_checkout(workers):
    from app.network.handoff import _accept_owner_row

    viewer = workers("viewer")
    with viewer.active():
        _viewer_with_both_sandboxes(viewer)
        with get_db() as conn:
            _accept_owner_row(conn, conn.cursor(), "remote-chat", VIEWER, None)
        assert database.get_session("remote-chat")["project_id"] == "owners-checkout"
        # The owner's sync record arrives after: it does not restore its slug.
        assert _mirror(_record(owner_device_id=VIEWER))["project_id"] == "owners-checkout"


def test_accepting_a_chat_already_bound_to_a_checkout_keeps_it(workers):
    from app.network.handoff import _accept_owner_row

    viewer = workers("viewer")
    with viewer.active():
        _viewer_with_both_sandboxes(viewer)
        database.create_project("another-checkout", "Owners 2", None, "/o2", link_group_id="git:acme/owners-repo")
        viewer.sql("UPDATE sessions SET project_id = 'owners-checkout' WHERE id = 'remote-chat'")
        with get_db() as conn:
            _accept_owner_row(conn, conn.cursor(), "remote-chat", VIEWER, None)
        assert database.get_session("remote-chat")["project_id"] == "owners-checkout"


def test_a_copy_without_a_shared_id_keeps_the_owners_id(workers):
    with workers("viewer").active():
        database.create_project("sandbox", "Viewer sandbox", None, "/v", link_group_id="git:acme/viewers-repo")
        assert _mirror(_record(project_v2_id=None))["project_id"] == "sandbox"


def test_rows_mirrored_before_the_rule_are_cleared_once(workers):
    viewer = workers("viewer")
    with viewer.active():
        database.create_project("sandbox", "Viewer sandbox", None, "/v", link_group_id="git:acme/viewers-repo")
        database.create_project("same", "Same", None, "/s", link_group_id="git:acme/owners-repo")
        rows = (("stale", "sandbox", OWNER), ("same-project", "same", OWNER),
                ("runs-here", "sandbox", VIEWER), ("unowned", "sandbox", None))
        for session_id, project_id, owner in rows:
            database.create_session(session_id=session_id, profile_id="default", title=session_id)
            viewer.sql("UPDATE sessions SET project_id = ?, project_v2_id = ?, owner_device_id = ?, "
                       "updated_at = '2026-01-01T00:00:00+00:00' WHERE id = ?",
                       (project_id, OWNERS_PROJECT, owner, session_id))

        assert catalog.reconcile_mirrored_project_ids(VIEWER) == 1
        assert catalog.reconcile_mirrored_project_ids(VIEWER) == 0
        assert catalog.reconcile_mirrored_project_ids(None) == 0
        after = {s: database.get_session(s) for s, _, _ in rows}
        assert {s: row["project_id"] for s, row in after.items()} == {
            "stale": None, "same-project": "same", "runs-here": "sandbox", "unowned": "sandbox"}
        # The shared id stays and the owner's timestamp is not bumped.
        assert after["stale"]["project_v2_id"] == OWNERS_PROJECT
        assert str(after["stale"]["updated_at"]).startswith("2026-01-01")


def test_is_checkout_of_compares_shared_identity():
    assert is_checkout_of({"id": "a", "link_group_id": "git:acme/owners-repo"}, OWNERS_PROJECT)
    assert not is_checkout_of({"id": "a", "link_group_id": "git:acme/other"}, OWNERS_PROJECT)
    assert not is_checkout_of(None, OWNERS_PROJECT)
    assert not is_checkout_of({"id": "a"}, None)


def test_one_local_checkout_is_picked_per_origin():
    """This worker's row before any other, otherwise the first in name order."""
    origin = "git:acme/owners-repo"
    peer = {"id": "a-peer", "instance_id": "peer", "link_group_id": origin}
    unowned = {"id": "b-unowned", "instance_id": None, "link_group_id": origin}
    mine = {"id": "c-mine", "instance_id": "me", "link_group_id": origin}
    assert local_rows_by_origin([peer, unowned, mine], ("me",))[origin]["id"] == "c-mine"
    assert local_rows_by_origin([peer, unowned], ("me",))[origin]["id"] == "a-peer"
    assert local_rows_by_origin([unowned, peer], ("me", None))[origin]["id"] == "b-unowned"


def test_cluster_catalog_names_this_workers_checkout():
    """The catalog matched ``me`` (the registry id) against project rows'
    instance ids, so it named whichever checkout sorted first."""
    from app.core.config import settings

    origin = "git:acme/owners-repo"
    pv2 = {"id": OWNERS_PROJECT, "origin": origin, "name": "Owners"}
    device = "dev_registry_id"
    installs = [{"id": device, "device_name": "Me", "approved": True, "online": True}]
    local = [{"id": "a-peer", "instance_id": "peer", "link_group_id": origin},
             {"id": "b-mine", "instance_id": settings.effective_instance_id, "link_group_id": origin}]
    rows = _build_cluster_catalog(bases=[{"project_id": OWNERS_PROJECT, "runner_id": device}], pv2s=[pv2],
                                  installs=installs, local_projects=local, me=device)
    assert [row["local_project_id"] for row in rows] == ["b-mine"]
