"""POST /api/v1/sessions/{session_id}/resume — re-run the last turn from the head.

The Retry button on an exhausted provider-retry card posts here. The route adds
no message: it enqueues a ``resume``-source turn whose prompt is empty, and the
engine picks the loop back up from the user node the failed turn left at the
head. It refuses when the session is busy (a resume would race a live turn) and
when the session is not fluid (the Claude CLI owns its own transcript and has no
head node to continue from).
"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from tests.api.helpers import app  # noqa: F401  (fixture)


# =============================================================================
# Fixtures — mirrors tests/api/test_sessions.py, which covers /clear and /archive
# =============================================================================

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
        mock.return_value = None  # admin (no API user)
        yield mock


@pytest.fixture
def client(app):
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def fluid_session():
    """A session the engine can resume: its sdk_session_id is a fluid id."""
    return {
        "id": "test-session-id",
        "title": "Test Session",
        "profile_id": "default",
        "project_id": "proj-1",
        "status": "active",
        "is_active": False,
        "sdk_session_id": "ses_abc123",
        "created_at": "2024-01-15T10:30:00",
        "updated_at": "2024-01-15T10:30:00",
    }


@pytest.fixture
def turns_idle():
    """No unsettled turn — the session is free to resume. The route's busy
    check is ONE atomic read (``session_has_unsettled_turns``), not separate
    live/queued reads a queued→running flip could slip between."""
    with patch(
        "app.db.turns.session_has_unsettled_turns", return_value=False
    ) as busy:
        yield busy


@pytest.fixture
def submit_seam():
    """The enqueue + NOTIFY pair the route drives.

    The route enqueues through ``submit_async`` — the off-loop twin of
    ``submit`` — so the queue writes never block the event loop.
    """
    with patch("app.core.turn_manager.submit_async",
               new=AsyncMock(return_value="turn-1")) as submit, \
            patch("app.core.turn_claim.notify_turns_changed") as notify:
        yield submit, notify


# =============================================================================
# Tests
# =============================================================================

class TestResumeSession:

    def test_unknown_session_is_404(self, client, mock_database, turns_idle, submit_seam):
        mock_database.get_session.return_value = None

        response = client.post("/api/v1/sessions/non-existent/resume")

        assert response.status_code == 404
        submit_seam[0].assert_not_called()

    def test_unsettled_turn_queues_instead_of_refusing(
        self, client, mock_database, fluid_session, submit_seam
    ):
        """The busy path enqueues. Refusing lost the user's Retry entirely —
        an error toast and nothing durable — while every other mid-turn
        submission in the app rides the queue."""
        submit, notify = submit_seam
        mock_database.get_session.return_value = fluid_session

        with patch("app.db.turns.session_has_unsettled_turns", return_value=True),                 patch("app.db.turns.get_queued_turns", return_value=[]):
            response = client.post("/api/v1/sessions/test-session-id/resume")

        assert response.status_code == 200
        assert response.json() == {
            "turn_id": "turn-1",
            "session_id": "test-session-id",
            "queued": True,
        }
        submit.assert_called_once()
        assert submit.call_args.args == ("test-session-id", "resume")
        notify.assert_called_once_with("test-session-id")

    def test_busy_queue_skips_a_resume_already_waiting(
        self, client, mock_database, fluid_session, submit_seam
    ):
        """Two Retry presses are one request: a resume carries no prompt, so
        stacking a second would re-run the head twice — the duplicate the old
        409 existed to prevent."""
        submit, notify = submit_seam
        mock_database.get_session.return_value = fluid_session

        with patch("app.db.turns.session_has_unsettled_turns", return_value=True),                 patch(
                    "app.db.turns.get_queued_turns",
                    return_value=[
                        {"id": "turn-user", "source": "user"},
                        {"id": "turn-resume", "source": "resume"},
                    ],
                ):
            response = client.post("/api/v1/sessions/test-session-id/resume")

        assert response.status_code == 200
        assert response.json() == {
            "turn_id": "turn-resume",
            "session_id": "test-session-id",
            "queued": True,
            "duplicate": True,
        }
        submit.assert_not_called()
        notify.assert_not_called()

    def test_if_busy_conflict_restores_the_refusal(
        self, client, mock_database, fluid_session, submit_seam
    ):
        """The explicit opt-out for a caller that must not have a resume land
        later. The busy check stays ONE atomic read — a turn flipping
        queued->running between two reads is invisible to both (the
        wait_session_idle race, 2026-08-28)."""
        mock_database.get_session.return_value = fluid_session

        with patch("app.db.turns.session_has_unsettled_turns", return_value=True):
            response = client.post(
                "/api/v1/sessions/test-session-id/resume?if_busy=conflict"
            )

        assert response.status_code == 409
        assert response.json()["detail"] == "a turn is already running"
        submit_seam[0].assert_not_called()

    def test_unknown_if_busy_value_is_rejected(
        self, client, mock_database, fluid_session, turns_idle, submit_seam
    ):
        mock_database.get_session.return_value = fluid_session

        response = client.post(
            "/api/v1/sessions/test-session-id/resume?if_busy=explode"
        )

        assert response.status_code == 422
        submit_seam[0].assert_not_called()

    def test_non_fluid_session_still_refuses_when_busy(
        self, client, mock_database, fluid_session, submit_seam
    ):
        """The fluid check is not softened by queueing — there is no head node
        to resume from either way."""
        fluid_session["sdk_session_id"] = "sdk-123"
        mock_database.get_session.return_value = fluid_session

        with patch("app.db.turns.session_has_unsettled_turns", return_value=True):
            response = client.post("/api/v1/sessions/test-session-id/resume")

        assert response.status_code == 409
        assert response.json()["detail"] == "resume needs a fluid session"
        submit_seam[0].assert_not_called()

    @pytest.mark.parametrize("sdk_session_id", ["sdk-123", "", None])
    def test_non_fluid_session_is_409(
        self, client, mock_database, fluid_session, turns_idle, submit_seam, sdk_session_id
    ):
        """Only fluid sessions have a head node to continue from."""
        fluid_session["sdk_session_id"] = sdk_session_id
        mock_database.get_session.return_value = fluid_session

        response = client.post("/api/v1/sessions/test-session-id/resume")

        assert response.status_code == 409
        assert response.json()["detail"] == "resume needs a fluid session"
        submit_seam[0].assert_not_called()

    def test_success_enqueues_a_resume_turn_and_notifies(
        self, client, mock_database, fluid_session, turns_idle, submit_seam
    ):
        submit, notify = submit_seam
        mock_database.get_session.return_value = fluid_session

        response = client.post("/api/v1/sessions/test-session-id/resume")

        assert response.status_code == 200
        assert response.json() == {
            "turn_id": "turn-1",
            "session_id": "test-session-id",
            "queued": False,
        }

        submit.assert_called_once()
        args, kwargs = submit.call_args
        assert args == ("test-session-id", "resume")
        assert kwargs["prompt"] == ""
        assert kwargs["profile_id"] == "default"
        assert kwargs["project_id"] == "proj-1"
        # Placed-elsewhere sessions are handed off by the durable row + NOTIFY.
        assert kwargs["no_kick"] is True
        notify.assert_called_once_with("test-session-id")

    def test_claude_code_profile_sentinel_is_dropped(
        self, client, mock_database, fluid_session, turns_idle, submit_seam
    ):
        """The no-profile sentinel is install-local; it must not ride the turn."""
        submit, _ = submit_seam
        fluid_session["profile_id"] = "claude-code"
        mock_database.get_session.return_value = fluid_session

        response = client.post("/api/v1/sessions/test-session-id/resume")

        assert response.status_code == 200
        assert submit.call_args.kwargs["profile_id"] is None

    def test_api_user_rides_the_payload(
        self, client, mock_database, mock_api_user, fluid_session, turns_idle, submit_seam
    ):
        submit, _ = submit_seam
        fluid_session["project_id"] = None
        mock_api_user.return_value = {"id": "api-user-1", "project_id": None}
        mock_database.get_session.return_value = fluid_session

        response = client.post("/api/v1/sessions/test-session-id/resume")

        assert response.status_code == 200
        assert submit.call_args.kwargs["payload"] == {"api_user_id": "api-user-1"}
