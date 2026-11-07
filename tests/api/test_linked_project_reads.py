"""In-flight project reads survive the first Git poll retiring a local identity."""
import subprocess

import pytest
from fastapi import HTTPException, Request

from app.api import git, git_helpers, git_owner, git_worktrees
from app.api.common import require_origin_access
from app.core import project_resolver, project_setup
from app.db import checkouts, database, projects_v2
from tests.shared_fixtures import LOCAL_ME as ME, pin_runner  # noqa: F401


@pytest.fixture
def project(mock_db, tmp_path):
    folder = tmp_path / "late-link"
    subprocess.run(["git", "init", "-q", str(folder)], check=True)
    subprocess.run(["git", "-C", str(folder), "remote", "add", "origin",
                    "https://github.com/acme/late-link.git"], check=True)
    database.create_project("late-link", "Late link", None, str(folder))
    original = project_resolver.resolve_project_v2({"project_id": "late-link"})
    assert original["origin"] == "local:late-link"
    git_helpers._repo_record_cache.clear()
    git_helpers._working_dir_cache.clear()
    yield original, folder
    git_helpers._repo_record_cache.clear()
    git_helpers._working_dir_cache.clear()


@pytest.fixture
def retained_branch(project, tmp_path):
    original, folder = project
    subprocess.run(["git", "-C", str(folder), "-c", "user.name=Tests", "-c",
                    "user.email=tests@example.com", "commit", "--allow-empty", "-qm", "Base"], check=True)
    branch_folder = tmp_path / "topic"
    subprocess.run(["git", "-C", str(folder), "worktree", "add", "-qb", "topic/keep",
                    str(branch_folder)], check=True)
    checkout = checkouts.upsert_checkout(original["id"], ME, str(branch_folder), branch="topic/keep")
    database.create_session("bound-topic", "default", project_id="late-link",
                            project_v2_id=original["id"], worktree_branch="topic/keep")
    return checkout, branch_folder


async def read(project_id, action="get_git_status", *, branch=None, **params):
    return await git_owner.handle_project_git({
        "action": action, "project_id": project_id, "branch": branch,
        "params": {"project_id": project_id, **params},
    }, ME)


async def link(original):
    # Real producing path: no chat or hand-seeded replacement. The first Git
    # read learns the remote and changes the catalog while other reads carry
    # the original ID. No intervening catalog response is needed to prove it.
    request = Request({"type": "http", "method": "GET", "query_string": b"", "headers": [],
                       "state": {"api_user": None}})
    response = await git.get_git_status(request, "late-link", token="test")
    assert response.is_git_repo
    current = project_resolver.resolve_project_v2({"project_id": "late-link"})
    assert current["id"] != original["id"]
    assert checkouts.get_checkout(original["id"], ME)["status"] == "removed"
    return current


@pytest.mark.parametrize("action", ["get_git_status", "list_branches", "get_diff_stats", "get_changed_files"])
async def test_inflight_git_reads_follow_exact_linked_identity(project, action):
    original, folder = project
    current = await link(original)
    response = await read(original["id"], action)
    assert response["status"] == 200, response
    assert project_resolver.resolve_catalog_project(original["id"])["id"] == current["id"]
    # The obsolete ID stays out of the catalog; no checkout is revived.
    assert original["id"] not in checkouts.eligible_base_project_ids(ME)
    assert current["id"] in checkouts.eligible_base_project_ids(ME)


@pytest.mark.parametrize("use_original_id", [True, False])
@pytest.mark.parametrize("compact", [True, False])
async def test_workers_detail_and_worktree_list_keep_pre_link_branch(
    project, retained_branch, use_original_id, compact
):
    original, folder = project
    checkout, branch_folder = retained_branch
    current = await link(original)
    access = require_origin_access(project_v2_id=original["id"], api_user={"project_id": "late-link"})
    assert access["project_v2"]["id"] == current["id"]
    assert project_setup._checkout_detail(original["id"], ME)["folder"] == str(folder)
    project_id = original["id"] if use_original_id else current["id"]
    request = Request({"type": "http", "method": "GET", "headers": [], "state": {"api_user": None}})
    result = await git_worktrees.list_worktrees(
        request, project_id, include_status=False, compact=compact, token="test"
    )
    assert checkout["id"] in [row.id for row in result.worktrees]
    if not compact:
        branch = next(row for row in result.worktrees if row.id == checkout["id"])
        assert branch.session_count == 1
    session = database.get_session("bound-topic")
    assert session["project_v2_id"] == original["id"]
    assert project_resolver.resolve_existing_cwd(session) == str(branch_folder)
    assert checkouts.get_checkout(original["id"], ME)["status"] == "removed"


@pytest.mark.parametrize("use_original_id", [True, False])
@pytest.mark.parametrize("action", ["get_git_status", "list_branches", "get_diff_stats", "get_changed_files"])
async def test_git_reads_keep_pre_link_branch(project, retained_branch, use_original_id, action):
    original, folder = project
    checkout, branch_folder = retained_branch
    current = await link(original)
    project_id = original["id"] if use_original_id else current["id"]
    response = await read(project_id, action, branch="topic/keep")
    assert response["status"] == 200, response
    assert checkouts.get_checkout(original["id"], ME, branch="topic/keep")["id"] == checkout["id"]
    assert database.get_session("bound-topic")["project_v2_id"] == original["id"]


async def test_deleted_linked_project_stays_unavailable_and_does_not_match_by_path(project):
    original, folder = project
    await link(original)
    database.delete_project("late-link")
    # Even a live, unrelated identity at exactly the same path is not an alias.
    unrelated = projects_v2.upsert_project("git:github.com/acme/unrelated", "Late link")
    checkouts.upsert_checkout(unrelated["id"], ME, str(folder))
    assert (await read(original["id"]))["status"] in (404, 409)
    assert project_resolver.resolve_catalog_project(original["id"])["id"] == original["id"]
    assert project_setup._checkout_detail(original["id"], ME)["folder"] is None


@pytest.mark.parametrize("delete", [True, False])
async def test_branch_sharing_requires_the_live_legacy_link(project, retained_branch, delete):
    original, folder = project
    checkout, branch_folder = retained_branch
    current = await link(original)
    if delete:
        database.delete_project("late-link")
    else:
        database.set_project_link_group("late-link", "git:example/relinked")
    assert original["id"] not in project_resolver.catalog_project_read_ids(current)
    assert (await read(current["id"], branch="topic/keep"))["status"] in (404, 409)
    request = Request({"type": "http", "method": "GET", "headers": [], "state": {"api_user": None}})
    result = await git_worktrees.list_worktrees(request, current["id"], include_status=False, token="test")
    assert checkout["id"] not in [row.id for row in result.worktrees]
    assert checkouts.get_checkout(original["id"], ME, branch="topic/keep")["id"] == checkout["id"]
    session = database.get_session("bound-topic")
    if delete:
        assert session is None  # Explicit project deletion still removes its legacy-owned chats.
    else:
        assert session["project_v2_id"] == original["id"]
        assert project_resolver.resolve_existing_cwd(session) == str(branch_folder)


async def test_unrelated_scoped_user_is_not_authorized_by_alias(project):
    original, folder = project
    await link(original)
    database.create_project("other", "Other", None, str(folder))
    with pytest.raises(HTTPException) as error:
        require_origin_access(project_v2_id=original["id"], api_user={"project_id": "other"})
    assert error.value.status_code == 403


async def test_bound_session_keeps_its_original_identity(project):
    original, folder = project
    database.create_session("bound", "default", project_id="late-link", project_v2_id=original["id"])
    await link(original)
    session = database.get_session("bound")
    assert project_resolver.resolve_project_v2(session)["id"] == original["id"]
    assert project_resolver.resolve_existing_cwd(session) == str(folder)
    assert database.get_session("bound")["project_v2_id"] == original["id"]


async def test_link_committing_between_identity_and_checkout_reads_is_retried(project, monkeypatch):
    original, folder = project
    real_list = checkouts.list_checkouts
    linked = False

    def retire_during_checkout_read(project_id):
        nonlocal linked
        if not linked:
            linked = True
            git_helpers.ensure_git_repository_record("late-link", str(folder))
            assert checkouts.get_checkout(original["id"], ME)["status"] == "removed"
        return real_list(project_id)

    monkeypatch.setattr(checkouts, "list_checkouts", retire_during_checkout_read)
    response = await read(original["id"])
    assert response["status"] == 200, response
    assert linked
