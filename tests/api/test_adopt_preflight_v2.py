"""Tests for the V2-projects cutover in the adopt preflight
(POST /api/v1/sessions/{id}/adopt in app/api/sessions.py).

When ``settings.v2_projects`` is ON and the session carries a ``project_v2_id``,
the legacy ``_adopt_preflight_sync`` (link-group resolution + branch-push
heuristic that rewrites ``sessions.project_id``) is REPLACED by a single
``project_resolver.resolve_local_checkout`` gate:
  - no local checkout on this runner → 409 (mirrors the legacy blocker's shape).
  - checkout present → adopt proceeds, ``instance_id`` flips, ``project_id`` is
    NEVER rewritten (``set_session_project`` untouched, ``local_project_id`` None).
Legacy sessions (no ``project_v2_id``) or flag OFF keep the legacy preflight.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.auth import require_auth
from app.api.sessions import router
from app.core.config import settings


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
    """No lease-valid active run so the streaming gate never blocks adopt."""
    with patch("app.core.streaming_status.has_active_run", return_value=False):
        yield


def _v2_session():
    return {
        "id": "test-session-id",
        "title": "T",
        "profile_id": "default",
        "instance_id": "other-install-xyz",
        "project_id": "proj-1",
        "project_v2_id": "pv2-abc",
        "worktree_id": None,
        "worktree_branch": None,
        "status": "active",
        "is_favorite": False,
        "sdk_session_id": "sdk-1",
        "created_at": "2024-01-15T10:30:00",
        "updated_at": "2024-01-15T10:30:00",
    }


def test_v2_no_local_checkout_conflicts(client, mock_database, monkeypatch):
    """v2 session + resolver finds no local checkout → 409, no adopt write."""
    monkeypatch.setattr(settings, "v2_projects", True)
    mock_database.get_session.return_value = _v2_session()
    mock_database.get_install.return_value = {"device_name": "Studio Mac", "online": False}

    with patch(
        "app.core.project_resolver.resolve_local_checkout", return_value=None
    ) as rlc:
        response = client.post("/api/v1/sessions/test-session-id/adopt")

    assert response.status_code == 409
    assert "checked out on this device" in response.json()["detail"]
    rlc.assert_called_once()
    mock_database.adopt_session.assert_not_called()


def test_v2_checkout_present_adopts_without_project_rewrite(client, mock_database, monkeypatch):
    """v2 session + local checkout → 200, instance_id flipped, project_id UNCHANGED."""
    monkeypatch.setattr(settings, "v2_projects", True)
    session = _v2_session()
    mock_database.get_session.return_value = session
    mock_database.get_install.return_value = {"device_name": "Studio Mac", "online": False}
    adopted_row = {**session, "instance_id": settings.effective_instance_id}
    mock_database.adopt_session.return_value = adopted_row

    checkout = {"id": "ck-1", "project_id": "pv2-abc", "runner_id": settings.effective_instance_id,
                "path": "/local/checkout", "branch": None}
    with patch(
        "app.core.project_resolver.resolve_local_checkout", return_value=checkout
    ), patch(
        "app.core.project_links.resolve_runnable_local_project"
    ) as legacy_preflight:
        response = client.post("/api/v1/sessions/test-session-id/adopt")

    assert response.status_code == 200
    data = response.json()
    assert data["adopted"] is True
    assert data["status"] == "adopted"
    assert data["instance_id"] == settings.effective_instance_id
    # project_id is NEVER rewritten under v2 — the legacy rebind must not run.
    assert data["local_project_id"] is None
    assert data["warning"] is None
    mock_database.set_session_project.assert_not_called()
    legacy_preflight.assert_not_called()
    mock_database.adopt_session.assert_called_once_with(
        "test-session-id", settings.effective_instance_id
    )


def test_flag_off_uses_legacy_preflight(client, mock_database, monkeypatch):
    """Flag OFF: even a project_v2_id-carrying session takes the legacy preflight
    (resolve_runnable_local_project), NOT the v2 resolver gate."""
    monkeypatch.setattr(settings, "v2_projects", False)
    session = _v2_session()
    mock_database.get_session.return_value = session
    mock_database.get_install.return_value = {"device_name": "Studio Mac", "online": False}
    adopted_row = {**session, "instance_id": settings.effective_instance_id}
    mock_database.adopt_session.return_value = adopted_row

    # Legacy preflight resolves onto THIS install's own project row (id unchanged)
    # → no sibling rebind, adopt proceeds.
    stored = {"id": "proj-1", "name": "MyRepo", "path": "/local/clone",
              "instance_id": settings.effective_instance_id}
    with patch(
        "app.core.project_links.resolve_runnable_local_project",
        return_value=(stored, stored),
    ) as legacy_preflight, patch(
        "app.core.project_resolver.resolve_local_checkout"
    ) as v2_gate:
        response = client.post("/api/v1/sessions/test-session-id/adopt")

    assert response.status_code == 200
    assert response.json()["adopted"] is True
    legacy_preflight.assert_called_once()
    v2_gate.assert_not_called()
