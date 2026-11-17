"""GET presents the effective context ceiling without forgetting a saved pick (#1640)."""
from app.db import database as db
from tests.core.helpers import make_profile


def test_model_switch_changes_the_readout_not_the_saved_ceiling(mock_db, authenticated_client):
    make_profile("agent", {})
    big, small = "claude-opus-4-6", "claude-haiku-4-5"
    db.create_session("s", profile_id="agent", model=big, context_window=1_000_000)

    def readout():
        response = authenticated_client.get("/api/v1/sessions/s")
        assert response.status_code == 200
        return response.json()["context_window"]

    assert readout() == 1_000_000
    db.set_session_run_binding("s", model=small)
    assert readout() is None
    assert db.get_session("s")["context_window"] == 1_000_000
    db.set_session_run_binding("s", model=big)
    assert readout() == 1_000_000
    db.set_session_run_binding("s", context_window=250_000)
    assert readout() == 250_000
