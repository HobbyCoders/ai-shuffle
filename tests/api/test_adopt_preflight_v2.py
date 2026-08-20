"""Tests for the V2-projects cutover in the adopt preflight
(POST /api/v1/sessions/{id}/adopt in app/api/sessions.py).

PR-B deleted the legacy ``_adopt_preflight_sync`` (link-group resolution +
branch-push heuristic that rewrote ``sessions.project_id``). EVERY session with a
project now takes a single ``project_resolver.has_local_base_checkout`` gate — no
``project_v2_id`` special-casing:
  - no local BASE checkout on this runner → 409 (the one deliberate blocker). The
    gate is base availability, NOT the branch checkout — a branch session adopts
    on the base being present (its branch materializes lazily at spawn).
  - base checkout present → adopt proceeds, ``instance_id`` flips, ``project_id``
    is NEVER rewritten (``set_session_project`` untouched).
A pre-cutover session (only a legacy ``project_id``) goes through the SAME gate,
which self-heals its project_v2 binding off the legacy id.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.auth import require_auth
from app.api.sessions import router
from app.api.sessions_helpers import is_pinned
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
        "is_active": False,
        "sdk_session_id": "sdk-1",
        "created_at": "2024-01-15T10:30:00",
        "updated_at": "2024-01-15T10:30:00",
    }


def test_is_pinned_is_explicit_runner_placement_only():
    assert is_pinned({"placement": {"where": "runner:abc"}}) is True
    assert is_pinned({"placement": '{"where": "runner:abc"}'}) is True
    assert is_pinned({"placement": None}) is False
    assert is_pinned({"placement": {"where": "anywhere"}}) is False
    assert is_pinned({}) is False


def test_v2_no_local_checkout_conflicts(client, mock_database):
    """v2 session + no local BASE checkout → 409, no adopt write."""
    mock_database.get_session.return_value = _v2_session()
    mock_database.get_install.return_value = {"device_name": "Studio Mac", "online": False}

    with patch(
        "app.core.project_resolver.has_local_base_checkout", return_value=False
    ) as gate:
        response = client.post("/api/v1/sessions/test-session-id/adopt")

    assert response.status_code == 409
    assert "checked out on this device" in response.json()["detail"]
    gate.assert_called_once()
    mock_database.adopt_session.assert_not_called()


def test_v2_checkout_present_adopts_without_project_rewrite(client, mock_database):
    """v2 session + local checkout → 200, instance_id flipped, project_id UNCHANGED."""
    session = _v2_session()
    mock_database.get_session.return_value = session
    mock_database.get_install.return_value = {"device_name": "Studio Mac", "online": False}
    adopted_row = {**session, "instance_id": settings.effective_instance_id}
    mock_database.adopt_session.return_value = adopted_row

    with patch(
        "app.core.project_resolver.has_local_base_checkout", return_value=True
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


def test_follow_already_local_nulls_placement(client, mock_database):
    """``follow=true`` on a local pinned session NULLs placement only."""
    session = {
        **_v2_session(),
        "instance_id": settings.effective_instance_id,
        "placement": {"where": f"runner:{settings.effective_instance_id}"},
    }
    mock_database.get_session.return_value = session
    mock_database.unpin_session.return_value = {**session, "placement": None}

    response = client.post(
        "/api/v1/sessions/test-session-id/adopt", json={"follow": True}
    )

    assert response.status_code == 200
    data = response.json()
    assert data["adopted"] is True
    assert data["status"] == "unpinned"
    assert data["instance_id"] == settings.effective_instance_id
    mock_database.unpin_session.assert_called_once_with(
        "test-session-id", settings.effective_instance_id
    )
    mock_database.adopt_session.assert_not_called()


def test_follow_while_streaming_409s(client, mock_database):
    """Unpin during a live turn 409s, including already-local."""
    session = {
        **_v2_session(),
        "instance_id": settings.effective_instance_id,
        "placement": {"where": f"runner:{settings.effective_instance_id}"},
    }
    mock_database.get_session.return_value = session
    mock_database.get_install.return_value = {"device_name": "This box"}

    with patch("app.core.streaming_status.has_active_run", return_value=True):
        response = client.post(
            "/api/v1/sessions/test-session-id/adopt", json={"follow": True}
        )

    assert response.status_code == 409
    mock_database.unpin_session.assert_not_called()
    mock_database.adopt_session.assert_not_called()


def test_follow_other_owner_unpins_after_checkout_gate(client, mock_database):
    """follow + other owner: same checkout gate as pin-to-here, then unpin write."""
    session = _v2_session()
    mock_database.get_session.return_value = session
    mock_database.unpin_session.return_value = {
        **session,
        "instance_id": settings.effective_instance_id,
        "placement": None,
    }

    with patch(
        "app.core.project_resolver.has_local_base_checkout", return_value=True
    ):
        response = client.post(
            "/api/v1/sessions/test-session-id/adopt", json={"follow": True}
        )

    assert response.status_code == 200
    assert response.json()["status"] == "unpinned"
    mock_database.unpin_session.assert_called_once_with(
        "test-session-id", settings.effective_instance_id
    )
    mock_database.adopt_session.assert_not_called()


def test_fluid_adopt_keeps_sdk_session_id(client, mock_database):
    """Fluid ids live in shared ``fluid_sessions`` — adopt keeps them."""
    session = {**_v2_session(), "sdk_session_id": "ses_deadbeefcafe"}
    mock_database.get_session.return_value = session
    mock_database.adopt_session.return_value = {
        **session, "instance_id": settings.effective_instance_id
    }

    with patch(
        "app.core.project_resolver.has_local_base_checkout", return_value=True
    ), patch(
        "app.core.provider.fluid_transcript.is_fluid_session", return_value=True
    ):
        response = client.post("/api/v1/sessions/test-session-id/adopt")

    assert response.status_code == 200
    mock_database.update_session.assert_not_called()


def test_adopt_keeps_sdk_when_jsonl_exists(client, mock_database, tmp_path):
    """CLI resume id is kept on adopt when the JSONL file is on this disk."""
    jsonl = tmp_path / "cli-uuid-1.jsonl"
    jsonl.write_text("{}\n")
    session = {**_v2_session(), "sdk_session_id": "cli-uuid-1"}
    mock_database.get_session.return_value = session
    mock_database.adopt_session.return_value = {
        **session, "instance_id": settings.effective_instance_id
    }

    with patch(
        "app.core.project_resolver.has_local_base_checkout", return_value=True
    ), patch(
        "app.core.provider.fluid_transcript.is_fluid_session", return_value=False
    ), patch(
        "app.core.jsonl_parser.get_session_jsonl_path", return_value=jsonl
    ):
        response = client.post("/api/v1/sessions/test-session-id/adopt")

    assert response.status_code == 200
    mock_database.update_session.assert_not_called()


def test_adopt_clears_sdk_when_jsonl_missing(client, mock_database):
    """CLI resume id is cleared on adopt when no local JSONL exists."""
    session = {**_v2_session(), "sdk_session_id": "cli-uuid-1"}
    mock_database.get_session.return_value = session
    mock_database.adopt_session.return_value = {
        **session, "instance_id": settings.effective_instance_id
    }

    with patch(
        "app.core.project_resolver.has_local_base_checkout", return_value=True
    ), patch(
        "app.core.provider.fluid_transcript.is_fluid_session", return_value=False
    ), patch(
        "app.core.jsonl_parser.get_session_jsonl_path", return_value=None
    ):
        response = client.post("/api/v1/sessions/test-session-id/adopt")

    assert response.status_code == 200
    mock_database.update_session.assert_called_once_with(
        "test-session-id", sdk_session_id=""
    )


def test_legacy_session_also_uses_resolver_gate(client, mock_database):
    """PR-B: a session WITHOUT project_v2_id (pre-cutover / un-upgraded install)
    is NOT special-cased — it goes through the SAME has_local_base_checkout gate
    (which self-heals project_v2 off the legacy project_id). No legacy link-group
    preflight, no project_id rewrite."""
    session = {**_v2_session(), "project_v2_id": None}
    mock_database.get_session.return_value = session
    mock_database.get_install.return_value = {"device_name": "Studio Mac", "online": False}
    adopted_row = {**session, "instance_id": settings.effective_instance_id}
    mock_database.adopt_session.return_value = adopted_row

    with patch(
        "app.core.project_resolver.has_local_base_checkout", return_value=True
    ) as v2_gate:
        response = client.post("/api/v1/sessions/test-session-id/adopt")

    assert response.status_code == 200
    assert response.json()["adopted"] is True
    v2_gate.assert_called_once()
    mock_database.set_session_project.assert_not_called()
