"""Chats made after a project is linked to its remote can use its checkout (#802).

A project's first chat seeds its origin-keyed identity under ``local:<id>``
while the project has no remote, with this worker's base checkout row
``ck-<id>``. When the Git panel's first poll then links the project to its
remote, later chats resolve to the remote's identity. Its base checkout insert
reused the id ``ck-<id>``, which the placeholder's row already held, so it
inserted nothing and those chats' Git panel answered 409 "This session's
checkout is not available on this device".
"""

import subprocess

import pytest

from app.api import git_helpers
from app.api.common import require_origin_access
from app.core import project_resolver
from app.db import checkouts, database
from tests.shared_fixtures import LOCAL_ME as ME, pin_runner  # noqa: F401  (autouse fixture)


PROJECT_ID = "sandbox"


@pytest.fixture
def repo(mock_db, tmp_path):
    path = tmp_path / PROJECT_ID
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(
        ["git", "-C", str(path), "remote", "add", "origin", "https://github.com/acme/sandbox.git"],
        check=True,
    )
    database.create_project(project_id=PROJECT_ID, name="Sandbox", description=None, path=str(path))
    git_helpers._repo_record_cache.clear()
    git_helpers._working_dir_cache.clear()
    yield path
    git_helpers._repo_record_cache.clear()
    git_helpers._working_dir_cache.clear()


def _chat(session_id):
    """What starting a chat in the project does (see ``sessions_create``)."""
    pv2 = require_origin_access(project_id=PROJECT_ID)["project_v2"]
    database.create_session(session_id, "default", project_id=PROJECT_ID, project_v2_id=pv2["id"])
    return pv2


def _git_panel_dir(session_id):
    git_helpers._working_dir_cache.clear()
    return git_helpers._resolve_working_dir(PROJECT_ID, session_id=session_id)


def test_chat_after_late_link_resolves_its_checkout(repo):
    first = _chat("first")
    assert first["origin"] == f"local:{PROJECT_ID}"
    assert _git_panel_dir("first") == (str(repo), True)

    # The Git panel's first poll records the repository and links the remote.
    assert git_helpers.ensure_git_repository_record(PROJECT_ID, str(repo)) is not None
    assert database.get_project(PROJECT_ID)["link_group_id"]

    second = _chat("second")
    assert second["id"] != first["id"]
    # No 409: the remote identity has this device's base checkout.
    assert _git_panel_dir("second") == (str(repo), True)
    # The first chat keeps its placeholder identity and checkout.
    assert _git_panel_dir("first") == (str(repo), True)
    assert checkouts.get_checkout(first["id"], ME)["id"] == f"ck-{PROJECT_ID}"

    # Resolving again (or the boot collapse re-running) adds nothing.
    before = len(checkouts.list_checkouts_with_legacy_fallback(second["id"], ME))
    _chat("third")
    project_resolver._seed_pv2_from_legacy(database.get_project(PROJECT_ID), second["origin"])
    assert len(checkouts.list_checkouts_with_legacy_fallback(second["id"], ME)) == before == 1


def _late_link(repo):
    """The two-row state: the folder's base checkout under the placeholder and
    under the remote identity. Returns ``(placeholder, remote)``."""
    first = _chat("first")
    git_helpers.ensure_git_repository_record(PROJECT_ID, str(repo))
    return first, _chat("second")


def test_path_lookup_treats_a_late_linked_folder_as_one_project(repo):
    placeholder, remote = _late_link(repo)
    rows = checkouts.get_checkouts_by_path(str(repo))
    assert [r["project_id"] for r in rows] == [remote["id"], placeholder["id"]]
    assert checkouts.get_checkout_by_path(str(repo))["project_id"] == remote["id"]


def test_path_lookup_stays_ambiguous_for_different_projects(repo):
    _late_link(repo)
    from app.db import projects_v2

    other = projects_v2.upsert_project("git:github.com/acme/other", "Other")
    checkouts.upsert_checkout(other["id"], ME, str(repo))
    assert checkouts.get_checkouts_by_path(str(repo)) == []
    assert checkouts.get_checkout_by_path(str(repo)) is None


def test_cleanup_busy_check_sees_chats_on_either_identity(repo, monkeypatch):
    from app.api import git_operations

    placeholder, remote = _late_link(repo)
    # A busy chat bound only to the placeholder identity, checked from a route
    # that names the remote identity.
    database.create_session("busy", "default", project_v2_id=placeholder["id"])
    monkeypatch.setattr(git_operations, "_session_is_busy", lambda sid: sid == "busy")
    assert git_operations._checkout_busy_session_ids(remote["id"], str(repo)) == ["busy"]
