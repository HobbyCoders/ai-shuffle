"""Explicit project path edits reconcile only the current, live base registration."""
import pytest

from app.core import project_resolver
from app.db import checkouts, database, projects_v2
from tests.shared_fixtures import LOCAL_ME as ME, pin_runner  # noqa: F401


@pytest.mark.parametrize("old_still_exists", [True, False])
def test_create_update_path_then_first_session_uses_new_folder(mock_db, tmp_path, old_still_exists):
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    database.create_project("moving", "Moving", None, str(old))
    identity = projects_v2.get_project_v2_by_origin("local:moving")
    original = checkouts.get_checkout(identity["id"], ME)
    if old_still_exists:
        new.mkdir()
    else:
        old.rename(new)

    database.update_project("moving", path=str(new))
    database.create_session("first", "default", project_id="moving")
    session = database.get_session("first")
    assert project_resolver.resolve_cwd(session) == str(new)
    session = database.get_session("first")
    assert session["project_v2_id"] == identity["id"]
    assert project_resolver.resolve_existing_cwd(session) == str(new)
    base = checkouts.get_checkout(identity["id"], ME)
    assert base["id"] == original["id"]
    assert base["path"] == str(new)
    assert len(checkouts.list_checkouts(identity["id"])) == 1


def test_path_update_leaves_retired_identity_and_branch_checkouts_untouched(mock_db, tmp_path):
    old, new, branch = (tmp_path / name for name in ("old", "new", "topic"))
    for folder in (old, new, branch):
        folder.mkdir()
    database.create_project("moving", "Moving", None, str(old))
    original = projects_v2.get_project_v2_by_origin("local:moving")
    database.create_session("retained", "default", project_id="moving", project_v2_id=original["id"])
    database.set_project_link_group("moving", "git:example/moving")
    current = projects_v2.get_project_v2_by_origin("git:example/moving")
    topic = checkouts.upsert_checkout(current["id"], ME, str(branch), branch="topic")
    other_runner = checkouts.upsert_checkout(current["id"], "other-runner", str(old))
    database.update_project("moving", path=str(new))

    retired = checkouts.get_checkout(original["id"], ME)
    assert (retired["path"], retired["status"]) == (str(old), "removed")
    assert project_resolver.resolve_existing_cwd(database.get_session("retained")) == str(old)
    assert checkouts.get_checkout(current["id"], ME)["path"] == str(new)
    assert checkouts.get_checkout(current["id"], ME, branch="topic") == topic
    assert checkouts.get_checkout(current["id"], "other-runner") == other_runner


def test_path_update_does_not_revive_or_move_removed_base(mock_db, tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    new.mkdir()
    database.create_project("moving", "Moving", None, str(old))
    identity = projects_v2.get_project_v2_by_origin("local:moving")
    checkouts.upsert_checkout(identity["id"], ME, str(old), status="removed")
    database.update_project("moving", path=str(new))
    base = checkouts.get_checkout(identity["id"], ME)
    assert (base["path"], base["status"]) == (str(old), "removed")
    assert not checkouts.has_active_base_checkout(identity["id"], ME)
