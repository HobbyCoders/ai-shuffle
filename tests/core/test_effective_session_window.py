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


def test_native_pick_uses_its_own_window_not_the_agent_provider(mock_db, authenticated_client, monkeypatch):
    from app.core import query_options

    monkeypatch.setattr(query_options, "get_security_instructions", lambda *a, **kw: "")
    for sid, model, provider_max, pin, expected in (
        ("small", "claude-haiku-4-5", 1_000_000, 1_000_000, None),
        ("large", "claude-opus-4-6", 200_000, 250_000, 250_000),
    ):
        db.create_model_provider(sid, sid, "openai", "https://example.invalid", models=[{"id": "other"}], context_window=provider_max)
        make_profile(sid, {"provider": sid})
        db.create_session(sid, profile_id=sid, model=model, provider_id=sid, context_window=pin)
        options, _ = query_options.build_options_from_profile(db.get_profile(sid), session_id=sid)
        url = f"/api/v1/sessions/{sid}"
        assert authenticated_client.get(url).json()["context_window"] == options.aih_context_window == expected
        saved = authenticated_client.put(url + "/run-settings", json={"context_window": pin})
        assert saved.status_code == (200 if expected else 409)
        assert db.get_session(sid)["context_window"] == pin
