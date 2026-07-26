"""Tests for the push-direction cross-device move
(POST /api/v1/sessions/{id}/move in app/api/sessions.py).

Move is adopt's mirror image: instead of "resume THIS session HERE" it says
"run this session on THAT device from now on", so it can be driven from any
install. Same single write (``database.adopt_session`` — owner column +
``placement`` pin dual-write), different gates:
  - target already owns it → 200 no-op (``moved=False``);
  - unknown install → 404; offline/incapable install → 409;
  - project session whose project isn't checked out on the target → 409
    (``capable_runners(project_v2_id, {})`` returns only runners with an active
    checkout row);
  - **actively streaming → NOT refused**. Placement is consulted only at turn
    start, so the live turn finishes on the old owner and later turns follow the
    new pin; the response reports ``pending_live_run=True``.

Fixtures mirror tests/api/test_adopt_preflight_v2.py.
"""

from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.auth import require_auth
from app.api.sessions import router


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
    """Project session + target has no active checkout row → 409, no write.

    The online check (``capable_runners(None, {})``) passes; only the
    project-scoped call excludes the target.
    """
    mock_database.get_session.return_value = _session(
        project_id="proj-1", project_v2_id="pv2-abc"
    )
    mock_database.get_install.return_value = {"device_name": "Studio Mac"}

    def _capable(project_v2_id, constraints=None):
        return [] if project_v2_id else [TARGET]

    with patch("app.core.placement.capable_runners", side_effect=_capable):
        response = _move(client)

    assert response.status_code == 409
    assert "isn't checked out on Studio Mac" in response.json()["detail"]
    mock_database.adopt_session.assert_not_called()


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
