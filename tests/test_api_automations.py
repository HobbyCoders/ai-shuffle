"""
Tests for automation API endpoints.
"""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock


# =============================================================================
# Helpers
# =============================================================================


def _create_profile(client):
    """Create a profile via the API so automations can reference it."""
    from app.db import database
    database.create_profile(
        profile_id="test-profile",
        name="Test Profile",
        description=None,
        config={"model": "claude-sonnet-4-20250514"},
    )
    return "test-profile"


# =============================================================================
# List & Trigger Types
# =============================================================================


class TestListEndpoints:
    def test_list_automations_empty(self, authenticated_client):
        response = authenticated_client.get("/api/v1/automations")
        assert response.status_code == 200
        assert response.json() == []

    def test_list_trigger_types(self, authenticated_client):
        response = authenticated_client.get("/api/v1/automations/trigger-types")
        assert response.status_code == 200
        types = response.json()
        assert "cron" in types
        assert "interval" in types
        assert "webhook" in types
        assert "connector_event" in types


# =============================================================================
# CRUD
# =============================================================================


class TestAutomationCRUD:
    def test_create_automation(self, authenticated_client, mock_encryption):
        profile_id = _create_profile(authenticated_client)
        response = authenticated_client.post(
            "/api/v1/automations",
            json={
                "name": "Test Auto",
                "trigger_type": "webhook",
                "trigger_config": {},
                "prompt": "Say hello",
                "profile_id": profile_id,
            },
        )
        assert response.status_code == 201
        data = response.json()
        assert data["name"] == "Test Auto"
        assert data["trigger_type"] == "webhook"

    def test_create_automation_invalid_trigger(self, authenticated_client, mock_encryption):
        profile_id = _create_profile(authenticated_client)
        response = authenticated_client.post(
            "/api/v1/automations",
            json={
                "name": "Bad Trigger",
                "trigger_type": "invalid",
                "trigger_config": {},
                "prompt": "test",
                "profile_id": profile_id,
            },
        )
        # Pydantic validation should catch this at 422
        assert response.status_code == 422

    def test_create_automation_invalid_profile(self, authenticated_client, mock_encryption):
        response = authenticated_client.post(
            "/api/v1/automations",
            json={
                "name": "Bad Profile",
                "trigger_type": "webhook",
                "trigger_config": {},
                "prompt": "test",
                "profile_id": "nonexistent",
            },
        )
        assert response.status_code == 400

    def test_get_automation(self, authenticated_client, mock_encryption):
        profile_id = _create_profile(authenticated_client)
        create_resp = authenticated_client.post(
            "/api/v1/automations",
            json={
                "name": "Fetch Me",
                "trigger_type": "webhook",
                "trigger_config": {},
                "prompt": "test",
                "profile_id": profile_id,
            },
        )
        aid = create_resp.json()["id"]

        response = authenticated_client.get(f"/api/v1/automations/{aid}")
        assert response.status_code == 200
        assert response.json()["name"] == "Fetch Me"

    def test_get_automation_not_found(self, authenticated_client):
        response = authenticated_client.get("/api/v1/automations/nonexistent")
        assert response.status_code == 404

    def test_delete_automation(self, authenticated_client, mock_encryption):
        profile_id = _create_profile(authenticated_client)
        create_resp = authenticated_client.post(
            "/api/v1/automations",
            json={
                "name": "Delete Me",
                "trigger_type": "webhook",
                "trigger_config": {},
                "prompt": "test",
                "profile_id": profile_id,
            },
        )
        aid = create_resp.json()["id"]

        response = authenticated_client.delete(f"/api/v1/automations/{aid}")
        assert response.status_code == 204

        # Confirm gone
        response = authenticated_client.get(f"/api/v1/automations/{aid}")
        assert response.status_code == 404

    def test_delete_automation_not_found(self, authenticated_client):
        response = authenticated_client.delete("/api/v1/automations/nonexistent")
        assert response.status_code == 404

    def test_update_automation(self, authenticated_client, mock_encryption):
        profile_id = _create_profile(authenticated_client)
        create_resp = authenticated_client.post(
            "/api/v1/automations",
            json={
                "name": "Update Me",
                "trigger_type": "webhook",
                "trigger_config": {},
                "prompt": "original",
                "profile_id": profile_id,
            },
        )
        aid = create_resp.json()["id"]

        response = authenticated_client.patch(
            f"/api/v1/automations/{aid}",
            json={"name": "Updated Name"},
        )
        assert response.status_code == 200
        assert response.json()["name"] == "Updated Name"

    def test_update_automation_not_found(self, authenticated_client):
        response = authenticated_client.patch(
            "/api/v1/automations/nonexistent",
            json={"name": "New Name"},
        )
        assert response.status_code == 404


# =============================================================================
# Lifecycle Actions
# =============================================================================


class TestLifecycleActions:
    def test_enable_not_found(self, authenticated_client):
        response = authenticated_client.post("/api/v1/automations/nonexistent/enable")
        assert response.status_code == 404

    def test_disable_not_found(self, authenticated_client):
        response = authenticated_client.post("/api/v1/automations/nonexistent/disable")
        assert response.status_code == 404

    def test_enable_automation(self, authenticated_client, mock_encryption):
        profile_id = _create_profile(authenticated_client)
        create_resp = authenticated_client.post(
            "/api/v1/automations",
            json={
                "name": "Enable Me",
                "trigger_type": "webhook",
                "trigger_config": {},
                "prompt": "test",
                "profile_id": profile_id,
            },
        )
        aid = create_resp.json()["id"]

        with patch("app.core.automation_manager.automation_manager.schedule_automation", new_callable=AsyncMock):
            response = authenticated_client.post(f"/api/v1/automations/{aid}/enable")

        assert response.status_code == 200
        assert response.json()["is_enabled"] is True

    def test_disable_automation(self, authenticated_client, mock_encryption):
        profile_id = _create_profile(authenticated_client)
        create_resp = authenticated_client.post(
            "/api/v1/automations",
            json={
                "name": "Disable Me",
                "trigger_type": "webhook",
                "trigger_config": {},
                "prompt": "test",
                "profile_id": profile_id,
            },
        )
        aid = create_resp.json()["id"]

        with patch("app.core.automation_manager.automation_manager.unschedule_automation", new_callable=AsyncMock):
            response = authenticated_client.post(f"/api/v1/automations/{aid}/disable")

        assert response.status_code == 200
        assert response.json()["is_enabled"] is False


# =============================================================================
# Trigger
# =============================================================================


class TestTrigger:
    def test_trigger_not_found(self, authenticated_client):
        response = authenticated_client.post("/api/v1/automations/nonexistent/trigger")
        assert response.status_code == 404

    def test_trigger_webhook(self, authenticated_client, mock_encryption):
        profile_id = _create_profile(authenticated_client)
        create_resp = authenticated_client.post(
            "/api/v1/automations",
            json={
                "name": "Trigger Me",
                "trigger_type": "webhook",
                "trigger_config": {},
                "prompt": "test",
                "profile_id": profile_id,
            },
        )
        aid = create_resp.json()["id"]

        with patch(
            "app.core.automation_manager.automation_manager.trigger_webhook",
            new_callable=AsyncMock,
            return_value={"automation_id": aid, "status": "triggered"},
        ):
            response = authenticated_client.post(
                f"/api/v1/automations/{aid}/trigger",
                json={"test": "data"},
            )

        assert response.status_code == 200
        assert response.json()["status"] == "triggered"


# =============================================================================
# Test (Dry Run)
# =============================================================================


class TestDryRun:
    def test_test_not_found(self, authenticated_client):
        response = authenticated_client.post("/api/v1/automations/nonexistent/test")
        assert response.status_code == 404

    def test_test_automation(self, authenticated_client, mock_encryption):
        profile_id = _create_profile(authenticated_client)
        create_resp = authenticated_client.post(
            "/api/v1/automations",
            json={
                "name": "Test Me",
                "trigger_type": "webhook",
                "trigger_config": {},
                "prompt": "Hello {{name}}",
                "profile_id": profile_id,
            },
        )
        aid = create_resp.json()["id"]

        response = authenticated_client.post(f"/api/v1/automations/{aid}/test")
        assert response.status_code == 200
        data = response.json()
        assert "prompt_rendered" in data
        assert "budget_ok" in data


# =============================================================================
# Runs & Stats
# =============================================================================


class TestRunsAndStats:
    def test_list_runs_not_found(self, authenticated_client):
        response = authenticated_client.get("/api/v1/automations/nonexistent/runs")
        assert response.status_code == 404

    def test_list_runs_empty(self, authenticated_client, mock_encryption):
        profile_id = _create_profile(authenticated_client)
        create_resp = authenticated_client.post(
            "/api/v1/automations",
            json={
                "name": "Runs Test",
                "trigger_type": "webhook",
                "trigger_config": {},
                "prompt": "test",
                "profile_id": profile_id,
            },
        )
        aid = create_resp.json()["id"]

        response = authenticated_client.get(f"/api/v1/automations/{aid}/runs")
        assert response.status_code == 200
        assert response.json() == []

    def test_stats_not_found(self, authenticated_client):
        response = authenticated_client.get("/api/v1/automations/nonexistent/stats")
        assert response.status_code == 404

    def test_stats_basic(self, authenticated_client, mock_encryption):
        profile_id = _create_profile(authenticated_client)
        create_resp = authenticated_client.post(
            "/api/v1/automations",
            json={
                "name": "Stats Test",
                "trigger_type": "webhook",
                "trigger_config": {},
                "prompt": "test",
                "profile_id": profile_id,
            },
        )
        aid = create_resp.json()["id"]

        response = authenticated_client.get(f"/api/v1/automations/{aid}/stats")
        assert response.status_code == 200
        data = response.json()
        assert data["automation_id"] == aid
        assert data["total_runs"] == 0


# =============================================================================
# Approvals
# =============================================================================


class TestApprovals:
    def test_list_pending_approvals(self, authenticated_client):
        response = authenticated_client.get("/api/v1/automations/approvals/pending")
        assert response.status_code == 200
        assert response.json() == []

    def test_approve_not_found(self, authenticated_client):
        response = authenticated_client.post("/api/v1/automations/approvals/nonexistent/approve")
        assert response.status_code == 404

    def test_deny_not_found(self, authenticated_client):
        response = authenticated_client.post("/api/v1/automations/approvals/nonexistent/deny")
        assert response.status_code == 404
