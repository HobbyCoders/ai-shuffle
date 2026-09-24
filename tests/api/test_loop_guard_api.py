"""Settings > Loop guard endpoints and the automation "use an API key" notice."""
from unittest.mock import AsyncMock, patch

from app.core import claude_auth_mode, loop_guard
from app.db import database as db

ENDPOINT = "/api/v1/settings/loop-guard"


def test_reports_defaults_and_whether_it_applies(authenticated_client):
    claude_auth_mode.set_mode("subscription")
    body = authenticated_client.get(ENDPOINT).json()
    assert body["defaults"] == loop_guard.DEFAULTS
    assert body["enabled"] is True and body["applies"] is True
    assert body["held"] == []
    claude_auth_mode.set_mode("api_key")
    assert authenticated_client.get(ENDPOINT).json()["applies"] is False


def test_put_updates_and_validates(authenticated_client):
    r = authenticated_client.put(ENDPOINT, json={"session_turns_per_hour": 5, "worker_max_minutes": 30})
    assert r.status_code == 200, r.text
    assert r.json()["session_turns_per_hour"] == 5 and r.json()["worker_max_minutes"] == 30
    assert authenticated_client.put(ENDPOINT, json={"install_turns_per_hour": 0}).status_code == 400


def test_confirm_and_dismiss_held_turns(authenticated_client):
    first = loop_guard.hold({"id": "t1", "session_id": "s1", "source": "wakeup", "prompt": "go"}, "held")
    second = loop_guard.hold({"id": "t2", "session_id": "s2", "source": "connector", "prompt": "hi"}, "held")
    with patch("app.core.turn_manager.submit_async", AsyncMock(return_value="t3")):
        r = authenticated_client.post(f"{ENDPOINT}/held/{first['id']}/confirm")
    assert r.status_code == 200 and r.json()["turn_id"] == "t3"
    assert authenticated_client.post(f"{ENDPOINT}/held/{second['id']}/dismiss").status_code == 200
    assert authenticated_client.post(f"{ENDPOINT}/held/{second['id']}/dismiss").status_code == 404
    assert authenticated_client.get(ENDPOINT).json()["held"] == []


def _automation(client):
    db.create_profile(profile_id="auto-profile", name="Auto", description=None, config={})
    return client.post("/api/v1/automations", json={
        "name": "Nightly", "trigger_type": "webhook", "trigger_config": {},
        "prompt": "summarize", "profile_id": "auto-profile",
    })


def test_automation_create_in_subscription_mode_recommends_an_api_key(authenticated_client, mock_encryption):
    claude_auth_mode.set_mode("subscription")
    r = _automation(authenticated_client)
    assert r.status_code == 201, r.text
    assert r.json()["notices"] == [loop_guard.SCHEDULED_RUN_GUIDANCE]
    r = authenticated_client.patch(f"/api/v1/automations/{r.json()['id']}", json={"name": "Nightly 2"})
    assert r.json()["notices"] == [loop_guard.SCHEDULED_RUN_GUIDANCE]


def test_automation_create_in_api_key_mode_has_no_notice(authenticated_client, mock_encryption):
    claude_auth_mode.set_mode("api_key")
    r = _automation(authenticated_client)
    assert r.status_code == 201, r.text
    assert r.json()["notices"] == []
