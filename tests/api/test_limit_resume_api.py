"""Routes for #1831: the per-provider resume settings, Compact now, the
cold-send check and a chat's scheduled resume (status and Cancel)."""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.core import limit_resume
from tests.api.helpers import app  # noqa: F401  (fixture)

SETTINGS = "/api/v1/settings/limit-resume"


# ─── Settings ───────────────────────────────────────────────────────────────


def test_settings_list_every_provider_with_defaults(authenticated_client):
    body = authenticated_client.get(SETTINGS).json()
    assert body["defaults"] == {"resume_on_reset": False, "compact_before_resume": True}
    assert body["providers"]["claude"] == body["defaults"]


def test_settings_update_one_switch_and_keep_the_other(authenticated_client):
    r = authenticated_client.put(f"{SETTINGS}/claude", json={"resume_on_reset": True})
    assert r.status_code == 200, r.text
    assert r.json()["providers"]["claude"] == {"resume_on_reset": True, "compact_before_resume": True}
    r = authenticated_client.put(f"{SETTINGS}/claude", json={"compact_before_resume": False})
    assert r.json()["providers"]["claude"] == {"resume_on_reset": True, "compact_before_resume": False}


@pytest.mark.parametrize("body", [{}, {"resume_on_reset": None}])
def test_settings_refuse_an_empty_change(authenticated_client, body):
    assert authenticated_client.put(f"{SETTINGS}/claude", json=body).status_code == 400


@pytest.mark.parametrize("body", [{"resume_on_reset": "yes"}, {"compact_before_resume": 1}])
def test_settings_refuse_a_value_that_is_not_a_bool(authenticated_client, body):
    assert authenticated_client.put(f"{SETTINGS}/claude", json=body).status_code == 422


def test_settings_refuse_an_unknown_provider(authenticated_client):
    r = authenticated_client.put(f"{SETTINGS}/no-such-provider", json={"resume_on_reset": True})
    assert r.status_code == 404


# ─── Session routes ─────────────────────────────────────────────────────────


@pytest.fixture
def mock_database():
    with patch("app.api.sessions.database") as mock_db, \
            patch("app.api.sessions_helpers.database", mock_db), \
            patch("app.api.common.database", mock_db):
        mock_db.get_session_tags.return_value = []
        mock_db.session_has_forks.return_value = False
        mock_db.get_installs_map.return_value = {}
        mock_db.get_project.return_value = None
        yield mock_db


@pytest.fixture
def mock_api_user():
    with patch("app.api.sessions.get_api_user_from_request") as mock, \
            patch("app.api.sessions_helpers.get_api_user_from_request", new=mock), \
            patch("app.api.common.get_api_user_from_request", new=mock):
        mock.return_value = None
        yield mock


@pytest.fixture
def client(app):
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def session_row():
    return {
        "id": "s1", "title": "Chat", "profile_id": "default", "project_id": "proj-1",
        "status": "active", "is_active": False, "sdk_session_id": "ses_1",
        "created_at": "2026-10-04T10:00:00", "updated_at": "2026-10-04T10:00:00",
    }


@pytest.fixture
def submit_seam():
    with patch("app.core.turn_manager.submit_async", new=AsyncMock(return_value="turn-c")) as submit, \
            patch("app.core.turn_claim.notify_turns_changed") as notify:
        yield submit, notify


@pytest.mark.parametrize("leg", ["delegated", "openai_responses"])
def test_compact_now_submits_a_compact_turn(
    client, mock_database, mock_api_user, session_row, submit_seam, leg,
):
    mock_database.get_session.return_value = session_row
    with patch.object(limit_resume, "session_leg", lambda s: leg), \
            patch("app.db.turns.session_has_unsettled_turns", return_value=False):
        r = client.post("/api/v1/sessions/s1/compact")
    assert r.status_code == 200, r.text
    submit, notify = submit_seam
    assert submit.call_args.args == ("s1", "user")
    assert submit.call_args.kwargs["prompt"] == "/compact"
    notify.assert_called_once_with("s1")


def test_compact_now_is_refused_while_a_turn_runs(
    client, mock_database, mock_api_user, session_row, submit_seam,
):
    mock_database.get_session.return_value = session_row
    with patch.object(limit_resume, "session_leg", lambda s: "delegated"), \
            patch("app.db.turns.session_has_unsettled_turns", return_value=True):
        r = client.post("/api/v1/sessions/s1/compact")
    assert r.status_code == 409
    submit_seam[0].assert_not_called()


def test_compact_now_is_refused_for_a_voice_chat(
    client, mock_database, mock_api_user, session_row, submit_seam,
):
    mock_database.get_session.return_value = session_row
    with patch.object(limit_resume, "session_leg", lambda s: "voice"):
        assert client.post("/api/v1/sessions/s1/compact").status_code == 400
    submit_seam[0].assert_not_called()


def test_compact_now_is_refused_for_an_archived_chat(
    client, mock_database, mock_api_user, session_row, submit_seam,
):
    mock_database.get_session.return_value = {**session_row, "status": "archived"}
    with patch.object(limit_resume, "session_leg", lambda s: "delegated"):
        assert client.post("/api/v1/sessions/s1/compact").status_code == 409
    submit_seam[0].assert_not_called()


def test_cache_state_reports_the_verdict(client, mock_database, mock_api_user, session_row):
    mock_database.get_session.return_value = session_row
    verdict = {"warn": True, "cold": True, "large": True, "context_tokens": 400_000}
    with patch("app.core.send_cost.cache_state", return_value=verdict) as state:
        r = client.get("/api/v1/sessions/s1/cache-state")
    assert r.status_code == 200 and r.json() == verdict
    assert state.call_args.args[0]["id"] == "s1"


def test_limit_resume_status_and_cancel(client, mock_database, mock_api_user, session_row):
    mock_database.get_session.return_value = session_row
    pending = {"fire_at": 1.0, "limit_key": "claude:1"}
    with patch.object(limit_resume, "pending", return_value=pending), \
            patch.object(limit_resume, "paused_workers_of", return_value=["w1"]):
        body = client.get("/api/v1/sessions/s1/limit-resume").json()
    assert body == {"session_id": "s1", "pending": pending, "workers": ["w1"]}

    cancel = AsyncMock(return_value=True)
    with patch.object(limit_resume, "cancel", cancel), \
            patch.object(limit_resume, "paused_workers_of", return_value=["w1"]):
        assert client.post("/api/v1/sessions/s1/limit-resume/cancel").json()["cancelled"] is True
        cancel.assert_awaited_with("s1")
        r = client.post("/api/v1/sessions/s1/limit-resume/cancel?task_id=w1")
        assert r.json()["cancelled"] is True
        cancel.assert_awaited_with("w1")
        assert client.post("/api/v1/sessions/s1/limit-resume/cancel?task_id=other").status_code == 404
