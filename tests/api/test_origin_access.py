"""``require_origin_access`` — XOR project ids + origin-based scoped-user gate.

The helper takes a FastAPI ``request`` OR an ``api_user`` dict (WS has no
Request). These tests drive it directly against the pg_shim; git-state /
with-worktree / WS call sites have their own XOR and compose tests.
"""

from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from app.api.common import (
    XOR_PROJECT_IDS_DETAIL,
    assigned_origin,
    require_origin_access,
)
from app.db import database
from app.db import projects as projects_db
from app.db import projects_v2


ORIGIN = "git:example.com/shared.git"
OTHER_ORIGIN = "git:example.com/other.git"


@pytest.fixture
def linked_project(mock_db, tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    database.create_project(
        project_id="proj-assigned", name="Assigned", description=None, path=str(path)
    )
    projects_db.set_project_link_group("proj-assigned", ORIGIN)
    pv2 = projects_v2.upsert_project(ORIGIN, name="Shared")
    return {"legacy_id": "proj-assigned", "pv2": pv2}


def _scoped(project_id="proj-assigned"):
    return {"id": "user-1", "project_id": project_id}


def test_xor_neither_is_400(mock_db):
    with pytest.raises(HTTPException) as exc:
        require_origin_access(api_user=None)
    assert exc.value.status_code == 400
    assert exc.value.detail == XOR_PROJECT_IDS_DETAIL


def test_xor_both_is_400(linked_project):
    with pytest.raises(HTTPException) as exc:
        require_origin_access(
            api_user=None,
            project_id=linked_project["legacy_id"],
            project_v2_id=linked_project["pv2"]["id"],
        )
    assert exc.value.status_code == 400
    assert exc.value.detail == XOR_PROJECT_IDS_DETAIL


def test_admin_with_legacy_id(linked_project):
    access = require_origin_access(
        api_user=None, project_id=linked_project["legacy_id"]
    )
    assert access["origin"] == ORIGIN
    assert access["legacy"]["id"] == linked_project["legacy_id"]
    assert access["project_v2"]["id"] == linked_project["pv2"]["id"]


def test_admin_with_project_v2_id(linked_project):
    access = require_origin_access(
        api_user=None, project_v2_id=linked_project["pv2"]["id"]
    )
    assert access["origin"] == ORIGIN
    assert access["legacy"] is None
    assert access["project_v2"]["id"] == linked_project["pv2"]["id"]


def test_unscoped_api_user_is_allowed(linked_project):
    access = require_origin_access(
        api_user={"id": "user-1", "project_id": None},
        project_id=linked_project["legacy_id"],
    )
    assert access["origin"] == ORIGIN


def test_scoped_same_origin_legacy_id(linked_project):
    access = require_origin_access(
        api_user=_scoped(), project_id=linked_project["legacy_id"]
    )
    assert access["origin"] == ORIGIN


def test_scoped_same_origin_remote_only(linked_project, tmp_path):
    """Scoped user may compose by ``project_v2_id`` of their origin even
    when this install has no extra local projects row for that request."""
    access = require_origin_access(
        api_user=_scoped(), project_v2_id=linked_project["pv2"]["id"]
    )
    assert access["origin"] == ORIGIN
    assert access["legacy"] is None


def test_scoped_other_origin_is_403(linked_project):
    other = projects_v2.upsert_project(OTHER_ORIGIN, name="Other")
    with pytest.raises(HTTPException) as exc:
        require_origin_access(api_user=_scoped(), project_v2_id=other["id"])
    assert exc.value.status_code == 403
    assert "Access denied" in exc.value.detail


def test_scoped_unresolvable_assigned_project_is_403(linked_project):
    with pytest.raises(HTTPException) as exc:
        require_origin_access(
            api_user=_scoped("no-such-project"),
            project_id=linked_project["legacy_id"],
        )
    assert exc.value.status_code == 403


def test_helper_accepts_api_user_without_a_request(linked_project):
    """WS has no Request — passing ``api_user=`` must not look at request."""
    access = require_origin_access(
        api_user=_scoped(),
        request=None,
        project_v2_id=linked_project["pv2"]["id"],
    )
    assert access["origin"] == ORIGIN
    # A MagicMock request would make getattr(state, "api_user") truthy;
    # with api_user passed, that path is not taken.
    access = require_origin_access(
        api_user=_scoped(),
        request=MagicMock(),
        project_id=linked_project["legacy_id"],
    )
    assert access["legacy"]["id"] == linked_project["legacy_id"]


def test_unknown_ids_404(mock_db):
    with pytest.raises(HTTPException) as exc:
        require_origin_access(api_user=None, project_id="nope")
    assert exc.value.status_code == 404
    with pytest.raises(HTTPException) as exc:
        require_origin_access(api_user=None, project_v2_id="pv2-nope")
    assert exc.value.status_code == 404


def test_assigned_origin_unscoped_is_none(mock_db):
    assert assigned_origin(None) is None
    assert assigned_origin({"id": "u", "project_id": None}) is None


def test_assigned_origin_scoped(linked_project):
    assert assigned_origin(_scoped()) == ORIGIN


def test_assigned_origin_unresolvable_is_none(mock_db):
    assert assigned_origin(_scoped("missing")) is None
