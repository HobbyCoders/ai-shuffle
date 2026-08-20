"""Tests for the push-direction cross-device move
(POST /api/v1/sessions/{id}/move in app/api/sessions.py).

Move is adopt's mirror image: instead of "resume THIS session HERE" it says
"run this session on THAT device from now on", so it can be driven from any
install. Same single write (``database.adopt_session`` — owner column +
``placement`` pin dual-write), different gates:
  - target already owns it → 200 no-op (``moved=False``);
  - unknown install → 404; offline/incapable install → 409
    (``capable_runners(None, {})`` — online + capability only);
  - project session whose target has no ACTIVE BASE checkout → 409
    (``has_active_base_checkout``; a branch-only worktree is not a base);
  - **actively streaming → NOT refused**. Placement is consulted only at turn
    start, so the live turn finishes on the old owner and later turns follow the
    new pin; the response reports ``pending_live_run=True``.

Fixtures mirror tests/api/test_adopt_preflight_v2.py. Checkout-gate tests seed
real ``checkouts`` rows (mirror tests/api/test_worktree_remote_target.py) and
do not patch ``capable_runners`` for the host predicate.
"""

from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.auth import require_auth
from app.api.sessions import router
from app.db import checkouts as checkouts_db
from app.db import projects_v2 as projects_v2_db


@pytest.fixture
def mock_database():
    with patch("app.api.sessions.database") as mock_db, \
            patch("app.api.sessions_helpers.database", mock_db):
        mock_db.get_session_tags.return_value = []
        mock_db.session_has_forks.return_value = False
        mock_db.get_installs_map.return_value = {}
        mock_db.get_project.return_value = None
        yield mock_db


@pytest.fixture
def mock_api_user():
    with patch("app.api.sessions.get_api_user_from_request") as mock, \
            patch("app.api.sessions_helpers.get_api_user_from_request", new=mock):
        mock.return_value = None  # admin
        yield mock


@pytest.fixture
def app(mock_database, mock_api_user):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_auth] = lambda: "test-session-token"
    yield app


@pytest.fixture
def client(app):
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def _not_streaming():
    """No lease-valid active run unless a test says otherwise."""
    with patch("app.core.streaming_status.has_active_run", return_value=False):
        yield


TARGET = "target-install-abc"


def _seed_pv2():
    """A real projects_v2 row so checkout inserts satisfy the FK."""
    return projects_v2_db.upsert_project("git:move-test-repo", name="Move Test")


def _give_target_a_checkout(tmp_path, project_v2_id, *, branch=None, runner=TARGET):
    """Seed a checkout row on the move target (real DB, not a mock)."""
    checkouts_db.upsert_checkout(
        project_v2_id,
        runner,
        str(tmp_path / "repo") if branch is None else str(tmp_path / branch),
        branch=branch,
        status="active",
    )


def _session(**overrides):
    row = {
        "id": "test-session-id",
        "title": "T",
        "profile_id": "default",
        "instance_id": "other-install-xyz",
        "project_id": None,
        "project_v2_id": None,
        "worktree_id": None,
        "worktree_branch": None,
        "status": "active",
        "is_active": False,
        "sdk_session_id": "sdk-1",
        "created_at": "2024-01-15T10:30:00",
        "updated_at": "2024-01-15T10:30:00",
    }
    row.update(overrides)
    return row


def _move(client, target=TARGET):
    return client.post(
        "/api/v1/sessions/test-session-id/move",
        json={"target_instance_id": target},
    )


def test_same_owner_is_a_noop(client, mock_database):
    """Target already owns the session → 200, moved=False, no write."""
    mock_database.get_session.return_value = _session(instance_id=TARGET)

    response = _move(client)

    assert response.status_code == 200
    data = response.json()
    assert data["moved"] is False
    assert data["status"] == "already_there"
    assert data["instance_id"] == TARGET
    mock_database.adopt_session.assert_not_called()


def test_unknown_target_404s(client, mock_database):
    """No registry row for the target install → 404, no write."""
    mock_database.get_session.return_value = _session()
    mock_database.get_install.return_value = None

    response = _move(client)

    assert response.status_code == 404
    assert "Unknown device" in response.json()["detail"]
    mock_database.adopt_session.assert_not_called()


def test_offline_target_409s(client, mock_database):
    """Target is registered but not an online+capable runner → 409, no write."""
    mock_database.get_session.return_value = _session()
    mock_database.get_install.return_value = {"device_name": "Studio Mac"}

    with patch("app.core.placement.capable_runners", return_value=[]):
        response = _move(client)

    assert response.status_code == 409
    assert "Studio Mac is offline" in response.json()["detail"]
    mock_database.adopt_session.assert_not_called()


def test_project_session_without_checkout_on_target_409s(client, mock_database):
    """Project session + target has no checkout row at all → 409, no write.

    The online check (``capable_runners(None, {})``) passes; the host
    predicate is the real ``has_active_base_checkout``, not a mock of
    ``capable_runners(project_v2_id)``.
    """
    pv2 = _seed_pv2()
    mock_database.get_session.return_value = _session(
        project_id="proj-1", project_v2_id=pv2["id"]
    )
    mock_database.get_install.return_value = {"device_name": "Studio Mac"}

    with patch("app.core.placement.capable_runners", return_value=[TARGET]):
        response = _move(client)

    assert response.status_code == 409
    assert "isn't checked out on Studio Mac" in response.json()["detail"]
    mock_database.adopt_session.assert_not_called()


def test_branch_only_checkout_on_target_409s(client, mock_database, tmp_path):
    """``capable_runners`` would accept this row; the base-checkout gate must
    not — Phase 3 never clones, so there is nothing to fork from on the target.
    """
    pv2 = _seed_pv2()
    mock_database.get_session.return_value = _session(
        project_id="proj-1", project_v2_id=pv2["id"]
    )
    mock_database.get_install.return_value = {"device_name": "Studio Mac"}
    _give_target_a_checkout(tmp_path, pv2["id"], branch="some/other-branch")

    with patch("app.core.placement.capable_runners", return_value=[TARGET]):
        response = _move(client)

    assert response.status_code == 409
    assert "isn't checked out on Studio Mac" in response.json()["detail"]
    mock_database.adopt_session.assert_not_called()


def test_active_base_checkout_on_target_moves(client, mock_database, tmp_path):
    """Idle project session + active BASE checkout on the target → 200."""
    pv2 = _seed_pv2()
    session = _session(project_id="proj-1", project_v2_id=pv2["id"])
    mock_database.get_session.return_value = session
    mock_database.get_install.return_value = {"device_name": "Studio Mac"}
    mock_database.adopt_session.return_value = {
        **session,
        "instance_id": TARGET,
        "placement": {"where": f"runner:{TARGET}"},
    }
    _give_target_a_checkout(tmp_path, pv2["id"])

    with patch("app.core.placement.capable_runners", return_value=[TARGET]), \
            patch("app.core.turn_claim.notify_turns_changed") as notify:
        response = _move(client)

    assert response.status_code == 200
    data = response.json()
    assert data["moved"] is True
    assert data["status"] == "moved"
    assert data["instance_id"] == TARGET
    mock_database.adopt_session.assert_called_once_with("test-session-id", TARGET)
    notify.assert_called_once_with("test-session-id")


def test_happy_path_moves_and_repins(client, mock_database):
    """Idle projectless session → 200, owner + placement pin re-pointed."""
    session = _session()
    mock_database.get_session.return_value = session
    mock_database.get_install.return_value = {"device_name": "Studio Mac"}
    moved_row = {
        **session,
        "instance_id": TARGET,
        "placement": {"where": f"runner:{TARGET}"},
    }
    mock_database.adopt_session.return_value = moved_row

    with patch("app.core.placement.capable_runners", return_value=[TARGET]), \
            patch("app.core.turn_claim.notify_turns_changed") as notify:
        response = _move(client)

    assert response.status_code == 200
    data = response.json()
    assert data["moved"] is True
    assert data["status"] == "moved"
    assert data["instance_id"] == TARGET
    assert data["pending_live_run"] is False
    mock_database.adopt_session.assert_called_once_with("test-session-id", TARGET)
    notify.assert_called_once_with("test-session-id")

    # The single write is the ownership + placement dual-write.
    updated = mock_database.adopt_session.return_value
    assert updated["instance_id"] == TARGET
    assert updated["placement"] == {"where": f"runner:{TARGET}"}


def test_live_run_still_moves_and_flags_pending(client, mock_database):
    """A streaming session is NOT refused — the move is queued behind the live
    turn and reported with ``pending_live_run=True``."""
    session = _session()
    mock_database.get_session.return_value = session
    mock_database.get_install.return_value = {"device_name": "Studio Mac"}
    mock_database.adopt_session.return_value = {**session, "instance_id": TARGET}

    with patch("app.core.streaming_status.has_active_run", return_value=True), \
            patch("app.core.placement.capable_runners", return_value=[TARGET]), \
            patch("app.core.turn_claim.notify_turns_changed"):
        response = _move(client)

    assert response.status_code == 200
    data = response.json()
    assert data["moved"] is True
    assert data["pending_live_run"] is True
    mock_database.adopt_session.assert_called_once_with("test-session-id", TARGET)


def test_blank_target_is_rejected(client, mock_database):
    """An empty target_instance_id never reaches the write path."""
    mock_database.get_session.return_value = _session()

    response = _move(client, target="")

    assert response.status_code in (400, 422)
    mock_database.adopt_session.assert_not_called()
