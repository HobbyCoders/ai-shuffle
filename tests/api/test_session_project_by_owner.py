"""Who runs a chat decides whether its legacy project id names a project here (#1659).

Legacy project ids are per worker. A chat another worker runs keeps that
worker's id on this worker's copy of the row whenever a project with the same
id exists here, related or not, so the page holds the shared id alone for a
row marked ``is_remote``. A chat this worker runs keeps its legacy id whatever
its shared id is: a relinked or unlinked project's chats keep their first
shared id by design, so the two ids disagreeing is not evidence of anything.

These pin the server side of that rule: the marker the page reads, and that a
chat this worker runs stays in its project after the project's identity moved.
"""
import pytest

from app.api import projects as projects_api
from app.api import sessions_helpers
from app.core import project_resolver
from app.db import database, projects_v2
from tests.shared_fixtures import LOCAL_ME, pin_runner  # noqa: F401

RELINKED = ("git:aaaaaaaaaaaaaaaa", "manual:bbbbbbbbbbbbbbbb")
UNLINKED = ("git:aaaaaaaaaaaaaaaa", None)


def _own_chat_of_moved_project(tmp_path, first, second):
    """A chat this worker runs in ``proj``, whose shared identity then moves."""
    folder = tmp_path / "proj"
    folder.mkdir()
    database.create_project("proj", "Proj", None, str(folder))
    database.set_project_link_group("proj", first)
    before = project_resolver.resolve_project_v2({"project_id": "proj"})
    database.create_session("mine", "default", project_id="proj", project_v2_id=before["id"])
    database.set_project_link_group("proj", second)
    after = project_resolver.resolve_project_v2({"project_id": "proj"})
    assert before["id"] != after["id"], "the project's shared identity must have moved"
    return folder, before


@pytest.mark.parametrize("first,second", [RELINKED, UNLINKED], ids=["relinked", "unlinked"])
def test_own_chat_of_a_moved_project_stays_in_its_project(mock_db, tmp_path, first, second):
    folder, before = _own_chat_of_moved_project(tmp_path, first, second)
    session = database.get_session("mine")

    # The row keeps its legacy id and its first shared id: correct data.
    assert (session["project_id"], session["project_v2_id"]) == ("proj", before["id"])
    # It still lists in its project, runs in the project's folder, and passes
    # the project-scoped routes (the composer upload gate among them).
    assert [s["id"] for s in database.get_sessions(project_id="proj")] == ["mine"]
    assert project_resolver.resolve_existing_cwd(session) == str(folder)
    assert projects_api._get_project_session("mine", "proj", None)["id"] == "mine"
    # A key scoped to the project still reaches its own chat there.
    sessions_helpers.check_session_access_for_api_user(
        {"id": "key", "project_id": "proj", "is_active": True}, {**session, "api_user_id": "key"})


@pytest.mark.parametrize("first,second", [RELINKED, UNLINKED], ids=["relinked", "unlinked"])
def test_own_chat_of_a_moved_project_is_not_marked_as_run_elsewhere(mock_db, tmp_path, first, second):
    """The page keeps a row's legacy id unless ``is_remote``: this is that marker."""
    _own_chat_of_moved_project(tmp_path, first, second)
    row = dict(database.get_session("mine"))
    assert row["instance_id"] == LOCAL_ME
    sessions_helpers._annotate_cluster_fields([row])
    assert row["is_remote"] is False
    assert row["project_id"] == "proj"
    # The sidebar names and groups it by its local project, as before: the
    # project's current link group wins over the chat's first shared identity.
    assert row["project_name"] == "Proj"
    assert row.get("link_group_id") == second


def _mirrored_row(project_v2_id):
    """Another worker's chat as this worker's copy of the row carries it."""
    return {
        "id": "mirror", "project_id": "app", "project_v2_id": project_v2_id,
        "instance_id": "peer-device", "owner_device_id": "peer-device",
    }


def _unrelated_local_app(tmp_path, link_group=None):
    folder = tmp_path / "app"
    folder.mkdir()
    database.create_project("app", "Unrelated local project", None, str(folder))
    if link_group:
        database.set_project_link_group("app", link_group)


@pytest.mark.parametrize("local_link_group", [None, "git:dddddddddddddddd"], ids=["unlinked", "linked"])
def test_mirrored_row_with_a_colliding_legacy_id_goes_by_its_shared_project(
        mock_db, tmp_path, local_link_group):
    """Another worker's chat whose legacy id is also an unrelated project's id here."""
    _unrelated_local_app(tmp_path, local_link_group)
    theirs = projects_v2.upsert_project("git:cccccccccccccccc", "Their project")
    row = _mirrored_row(theirs["id"])
    sessions_helpers._annotate_cluster_fields([row])
    # The marker, and the shared id the page then holds alone.
    assert row["is_remote"] is True
    assert row["project_v2_id"] == theirs["id"]
    # The sidebar's label and grouping key (link group, else origin) are the
    # shared project's, never the unrelated local project's.
    assert row["project_name"] == "Their project"
    assert "link_group_id" not in row
    assert row["origin"] == "git:cccccccccccccccc"


def test_mirrored_row_without_a_known_shared_project_keeps_the_local_reading(mock_db, tmp_path):
    """Nothing else names the project, so the row reads as it did before."""
    _unrelated_local_app(tmp_path, "git:dddddddddddddddd")
    for row in (_mirrored_row(None), _mirrored_row("pv2-not-synced-here")):
        sessions_helpers._annotate_cluster_fields([row])
        assert row["is_remote"] is True
        assert row["project_name"] == "Unrelated local project"
        assert row["link_group_id"] == "git:dddddddddddddddd"
