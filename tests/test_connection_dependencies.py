"""Connection ownership is independent of plugin presentation."""
import pytest

pytestmark = pytest.mark.usefixtures("bundled_connector_catalog")
from unittest.mock import AsyncMock, patch
from app.core.connection_dependencies import connection_dependencies, require_unreferenced_connection


def test_dependency_scan_covers_all_wiring(monkeypatch):
    automations = [
        {"id": "trigger", "name": "Inbox", "is_enabled": True,
         "triggers": [{"type": "connector_trigger", "config": {"connector_id": "gmail-work", "trigger_id": "email_received"}}]},
        {"id": "action", "action_steps": [{"type": "connector_action", "connector_id": "gmail-work", "action_id": "send_email"}]},
        {"id": "output", "output_config": {"type": "connector", "connector_id": "gmail-work"}},
        {"id": "legacy", "trigger_type": "connector_event", "trigger_config": {"connector_id": "gmail-work"}},
        {"id": "wildcard", "triggers": [{"type": "connector_trigger", "config": {"trigger_id": "email_received"}}]},
        {"id": "other", "triggers": [{"type": "connector_trigger", "config": {"connector_id": "gmail-personal"}}]},
        {"id": "plain", "trigger_type": "cron"},
    ]
    monkeypatch.setattr("app.core.connection_dependencies.database.get_all_automations", lambda: automations)
    dependencies = connection_dependencies("gmail-work")
    assert [item["id"] for item in dependencies] == ["trigger", "action", "output", "legacy", "wildcard"]
    assert [item["id"] for item in connection_dependencies("gmail-work", enabled_only=True)] == ["trigger"]
    with pytest.raises(ValueError, match="Inbox"):
        require_unreferenced_connection("gmail-work")
    assert automations[0]["triggers"][0]["config"]["connector_id"] == "gmail-work"


def test_filters_and_duplicate_references(monkeypatch):
    monkeypatch.setattr("app.core.connection_dependencies.database.get_all_automations", lambda: [{
        "id": "a", "triggers": [{"type": "connector_event", "config": {"filter": {"connector_id": "other"}}}],
        "action_steps": [{"type": "connector_action", "connector_id": "work"}] * 2,
    }])
    assert connection_dependencies("work")[0]["uses"] == ["action"]


@pytest.mark.asyncio
async def test_delete_does_not_stop_or_delete_a_referenced_connection(monkeypatch):
    from app.core.connector_service import delete_connector
    monkeypatch.setattr("app.core.connector_service.database.get_connector", lambda _: {"id": "work"})
    monkeypatch.setattr("app.core.connection_dependencies.database.get_all_automations", lambda: [{
        "id": "a", "output_config": {"type": "connector", "connector_id": "work"},
    }])
    with patch("app.core.connectors.manager.connector_manager.stop_connector", new_callable=AsyncMock) as stop:
        with patch("app.core.connector_service.database.delete_connector") as delete:
            with pytest.raises(ValueError, match="Update those automations first"):
                await delete_connector("work")
            stop.assert_not_awaited()
            delete.assert_not_called()


def test_api_created_automation_protects_its_output_connection(authenticated_client, mock_encryption):
    from app.db import database
    client = authenticated_client
    connection = client.post("/api/v1/connectors", json={
        "name": "Work", "type": "webhook", "config": {"mode": "inbound", "inbound_path": "dependency"}, "events": [],
    })
    assert connection.status_code == 201
    cid = connection.json()["id"]
    database.create_profile(profile_id="dependency-profile", name="Dependency test", description=None, config={})
    created = client.post("/api/v1/automations", json={
        "name": "Report", "trigger_type": "webhook", "trigger_config": {}, "prompt": "Report status",
        "profile_id": "dependency-profile", "output_config": {"type": "connector", "connector_id": cid},
        "max_cost_per_run": 0.01, "max_runs_per_day": 1,
    })
    assert created.status_code == 201, created.text
    aid = created.json()["id"]
    deps = client.get(f"/api/v1/connectors/{cid}/dependencies")
    assert deps.status_code == 200
    assert deps.json() == [{"id": aid, "name": "Report", "is_enabled": False, "uses": ["output"]}]
    assert client.delete(f"/api/v1/connectors/{cid}").status_code == 409
    # Paused automations preserve wiring but do not prevent a deliberate stop.
    assert client.post(f"/api/v1/connectors/{cid}/deactivate").status_code == 200
    assert client.patch(f"/api/v1/automations/{aid}", json={"is_enabled": True}).status_code == 200
    assert client.post(f"/api/v1/connectors/{cid}/deactivate").status_code == 409
    assert client.patch(f"/api/v1/connectors/{cid}", json={"is_active": False}).status_code == 400
    current = client.get(f"/api/v1/automations/{aid}").json()
    assert current["output_config"]["connector_id"] == cid
    assert client.get(f"/api/v1/connectors/{cid}").json()["id"] == cid
